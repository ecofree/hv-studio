"""
gpt-image-2 生图服务（subrouter.ai 中转）。

核心：
  - 三渠道池轮询 + 健康度降级（managers/relay.py）
  - 严格参数校验（对抗 subrouter 静默忽略非法 size/quality 的陷阱）
  - 实际像素预测（predict_actual_dims）+ 真实下载与 base64 解码
  - 失败自动切渠道重试

使用：
    from app.services.image import get_image
    img = get_image()
    resp = img.generate(ImageGenRequest(model='gpt-image-2', prompt='...', size='1080x1920'))
    item = resp.data[0]
    raw = base64.b64decode(item.b64_json)
"""
from __future__ import annotations

import base64
import os
import time
from datetime import datetime
from pathlib import Path

import requests

from app.config.config import Channel, env_get
from app.managers.relay import STATE_DIR, get_relay
from app.models import ImageEditRequest, ImageGenRequest, ImageGenResponse


class ImageError(RuntimeError):
    pass


class TransientImageError(ImageError):
    """可重试的瞬时故障（429 限流 / 5xx / 超时 / 连接被重置）。

    语义：**同一渠道**退避后重试即可恢复，不必（也不应）立刻换路烧别家配额。
    与 ImageError 的区别决定了 generate_many 是「原地重试」还是「换渠道」。
    """

    def __init__(self, msg: str, http_status: int | None = None):
        super().__init__(msg)
        self.http_status = http_status


# 瞬时故障指纹（小写匹配；中转的错误文案不统一，只能指纹法）
_TRANSIENT_SIG = (
    "429", "rate limit", "rate_limit", "too many requests", "quota exceeded",
    "500", "502", "503", "504", "bad gateway", "service unavailable",
    "gateway time", "internal server error", "server error",
    "timeout", "timed out", "temporarily unavailable", "try again",
    "empty body", "空音频",
    "connection reset", "connection aborted", "connection refused",
    "remote end closed", "remotedisconnected", "broken pipe",
    "read operation timed out", "max retries exceeded",
)


def is_transient_error(e: BaseException) -> bool:
    """判定是否瞬时故障：是 → 同渠道退避重试；否 → 直接换路/放弃。

    判据优先级：
      1. TransientImageError 显式类型
      2. HTTP 状态码（挂在 .http_status / .response.status_code 上）
      3. requests 异常类型（Timeout/ConnectionError）
      4. 文案指纹兜底
    """
    if isinstance(e, TransientImageError):
        return True
    if isinstance(e, ImageError):
        # 非 Transient 子类：仅文案兜底（如自建 ImageError("... timeout ...")）
        return _sig_hit(str(e))
    # requests 层：超时/连接类一律可重试；其余（SSL、InvalidURL 等）不可
    import requests as _rq
    if isinstance(e, _rq.exceptions.SSLError):
        # 证书/握手问题重试一万次也一样，且它继承 ConnectionError，必须前置排除
        return False
    if isinstance(e, (_rq.Timeout, _rq.ConnectionError)):
        return True
    for attr in ("http_status", "status_code"):
        st = getattr(e, attr, None)
        if isinstance(st, int) and (st == 429 or 500 <= st < 600):
            return True
    resp = getattr(e, "response", None)
    st = getattr(resp, "status_code", None)
    if isinstance(st, int) and (st == 429 or 500 <= st < 600):
        return True
    return _sig_hit(str(e))


def _sig_hit(text: str) -> bool:
    low = (text or "").lower()
    return any(s in low for s in _TRANSIENT_SIG)


def _mk_error(ch_name: str, status: int | None, detail: str) -> ImageError:
    """按状态码分流异常类型：429/5xx → Transient（可原地重试），其余 → 致命。"""
    msg = f"[{ch_name}]" + (f" HTTP {status}" if status else "") + f": {detail}"
    if status is not None and (status == 429 or 500 <= status < 600):
        return TransientImageError(msg, http_status=status)
    # 无状态码（网络层异常）时按文案兜底
    if status is None and _sig_hit(detail):
        return TransientImageError(msg, http_status=None)
    return ImageError(msg)


# ---------------------------------------------------------------------------
# 参数合法性校验（H 批 2026-09-05）
# ---------------------------------------------------------------------------
# subrouter 对非法 size/quality 的行为是**静默忽略**：不报错、不拒绝，照收钱
# 照生成，只是按它自己的默认值出图（例如请求 1080x1920 回来 1024x1024）。
# 事后只表现为「某几张图比例不对」，排查成本极高，而且钱已经花了。
#
# config.image.primary.strict_param_validation 就是为这个准备的开关，但此前
# 全仓零消费者 —— 设成 true 也不会有任何事发生（H 批让它真正生效）。
_ALLOWED_SIZES = {"1024x1024", "1024x1536", "1536x1024", "1080x1920", "auto"}
_ALLOWED_QUALITY = {"low", "medium", "high", "auto"}


def validate_image_params(size: str, quality: str | None = None,
                          model: str | None = None) -> list[str]:
    """返回参数问题列表（空 = 合法）。**不做任何网络调用**。

    调用方（build_image_jobs）在 strict_param_validation=true 时拿这个结果
    决定是否在花钱之前拒绝整批任务。
    """
    problems: list[str] = []
    if size not in _ALLOWED_SIZES:
        problems.append(
            f"size={size!r} 不在已知合法集 {sorted(_ALLOWED_SIZES)}；"
            f"中转站会静默忽略并按其默认值出图（照常计费）")
    if quality is not None and quality not in _ALLOWED_QUALITY:
        problems.append(
            f"quality={quality!r} 不在已知合法集 {sorted(_ALLOWED_QUALITY)}")
    if model is not None and not str(model).strip():
        problems.append("model 为空")
    return problems


class ImageGenerator:
    def __init__(self, max_attempts: int = 3):
        self.relay = get_relay()
        self.max_attempts = max_attempts

    # ---- 公共 ----
    def generate(self, req: ImageGenRequest,
                 channel: "Channel | None" = None) -> ImageGenResponse:
        """生图。

        channel 为 None  → 原 failover 语义（按健康度排序依次尝试）
        channel 非 None  → **只打该渠道**，失败直接抛，由调用方决定换路。

        后者是三路并行的前置条件：并发 worker 若都走 failover，
        会全部命中同一个 score 最高的渠道，三路退化成单路并把它打 429。
        """
        req.validate_for_subrouter()
        return self._call_with_failover(
            path="/v1/images/generations",
            body=req.model_dump(exclude_none=True),
            channel=channel,
        )

    def edit(self, req: ImageEditRequest,
             channel: "Channel | None" = None) -> ImageGenResponse:
        req.validate_for_subrouter()
        return self._call_with_failover(
            path="/v1/images/edits",
            body=req.model_dump(exclude_none=True),
            channel=channel,
        )

    # ---- 内部 ----
    def _call_with_failover(self, path: str, body: dict,
                            channel: "Channel | None" = None) -> ImageGenResponse:
        """依次尝试每个渠道，直到成功或全部失败。

        channel 非 None 时**只打该渠道**（不排序、不换路），失败即抛。
        这是 generate_many 三路并行的依赖：worker 各自持有一个渠道，
        避免并发请求全部涌向同一个最优渠道。
        """
        if channel is not None:
            t0 = time.time()
            try:
                resp = self._post(channel, path, body)
                self.relay.record(channel.name, ok=True,
                                  latency_s=time.time() - t0)
                return resp
            except Exception as e:
                self.relay.record(channel.name, ok=False,
                                  latency_s=time.time() - t0)
                raise

        channels: list[Channel] = self.relay.all()
        # 把最健康的放最前
        health = {c.name: self.relay.health(c.name) for c in channels}
        channels.sort(key=lambda c: (health[c.name].score() if health[c.name] else 100),
                       reverse=True)

        last_err: Exception | None = None
        for ch in channels[:self.max_attempts]:
            t0 = time.time()
            try:
                resp = self._post(ch, path, body)
                dt = time.time() - t0
                self.relay.record(ch.name, ok=True, latency_s=dt)
                return resp
            except Exception as e:
                dt = time.time() - t0
                self.relay.record(ch.name, ok=False, latency_s=dt)
                last_err = e
                continue
        raise ImageError(
            f"所有渠道失败（{self.max_attempts} 尝试）。最后错误: {last_err}"
        )

    def _post(self, ch: Channel, path: str, body: dict) -> ImageGenResponse:
        url = f"{ch.base_url}{path}"
        try:
            r = requests.post(
                url,
                headers={**ch.auth_header, "Content-Type": "application/json"},
                json=body,
                timeout=300,
            )
        except Exception as e:
            # 网络层异常：requests 的 Timeout/ConnectionError 基本都可重试。
            # 原先是裸抛 requests 异常，上层只能当致命错误换路 → 白白烧别家配额。
            raise _mk_error(
                ch.name, getattr(e, "response", None) and
                getattr(e.response, "status_code", None),
                f"{type(e).__name__}: {str(e)[:200]}",
            ) from e
        if r.status_code != 200:
            # 解析 subrouter 的两类错误 schema
            try:
                err = r.json()
                # 图像端 schema
                if "error" in err and isinstance(err["error"], dict):
                    raise _mk_error(
                        ch.name, r.status_code,
                        str(err['error'].get('message', r.text[:200])),
                    )
                # 视频端 schema
                if "code" in err and "message" in err:
                    raise _mk_error(
                        ch.name, r.status_code,
                        f"{err.get('code')}: {err.get('message')}",
                    )
            except ValueError:
                pass
            raise _mk_error(ch.name, r.status_code, r.text[:200])
        return ImageGenResponse.model_validate(r.json())


# ---------------------------------------------------------------------------
# 三路并发生图（2026-09-04）
# ---------------------------------------------------------------------------
import random
import threading
from dataclasses import dataclass, field
from queue import Empty, Queue

# 全局 QPS 闸：并发下保护中转，避免 3×并发把某 key 打 429。
# 用「时间窗限速」而非固定令牌数：每 _QPS_INTERVAL 秒放行一次。
# （曾误用固定 permit 的 Semaphore——acquire 后不归还，N 次后永久死锁。）
_QPS_MAX = float(env_get("HV_IMAGE_QPS_MAX", "2"))
_QPS_INTERVAL = 1.0 / max(1.0, _QPS_MAX)
_qps_lock = threading.Lock()
_qps_next = 0.0  # 下一次放行时刻


def _throttle():
    """时间窗限速：距上次放行不足 _QPS_INTERVAL 则等足。线程安全、不饥饿。"""
    global _qps_next
    import time as _t
    while True:
        with _qps_lock:
            now = _t.monotonic()
            if now >= _qps_next:
                _qps_next = now + _QPS_INTERVAL
                return
            wait = _qps_next - now
        # 锁外睡，避免持锁阻塞其他 worker
        _t.sleep(min(wait, 0.05))


def _backoff(attempt: int) -> float:
    """失败换路前的指数退避 + jitter，防 429 重试风暴。"""
    import time as _t
    base = min(0.5 * (2 ** attempt), 8.0)  # 0.5,1,2,4,8 封顶
    return base * (0.7 + 0.6 * random.random())


# 同一渠道上的瞬时故障重试次数（换路前的原地重试；总尝试 = 渠道数 × (1+本值)）
_MAX_TRANSIENT_RETRY = int(env_get("HV_IMAGE_TRANSIENT_RETRY", "2"))
# 打印每次瞬时重试细节（默认关，避免刷屏）
_VERBOSE = env_get("HV_IMAGE_VERBOSE", "0") not in ("0", "", "false")


@dataclass
class ImageJob:
    """一次生图任务。out_path 为期望落盘 PNG/JPG 路径。

    并发安全：workers 从 jobq 取任务；result 以 job.id 回填。
    """

    id: str
    req: "ImageGenRequest"
    out_path: str | Path
    reference_paths: tuple[str, ...] = ()

    # worker 内部用
    _channel_idx: int | None = field(default=None, repr=False)
    _attempts: int = field(default=0, repr=False)
    _transient: int = field(default=0, repr=False)
    _last_err: str | None = field(default=None, repr=False)

    @property
    def out(self) -> Path:
        return Path(self.out_path)

    def request_fingerprint(self):
        from app.services.provenance import fingerprint, file_sha256
        return fingerprint({"version": "image-cache-v2", "request": self.req.model_dump(mode="json"),
                            "references": {str(Path(p).resolve()): file_sha256(p) for p in self.reference_paths}})

    @property
    def receipt_path(self):
        return self.out.with_suffix(self.out.suffix + ".request.json")

    def validate_output(self, path):
        from PIL import Image
        from app.models.relay import predict_actual_dims
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
            actual = im.size
        requested = tuple(map(int, self.req.size.split("x")))
        expected = predict_actual_dims(self.req.size)
        # The configured relay may return its documented area-capped dimensions.
        if not any(all(abs(a / b - 1) <= .03 for a, b in zip(actual, dims))
                   for dims in (requested, expected)):
            raise ValueError(f"Image dimensions {actual} do not match request {self.req.size}")
        return actual

    def install(self, raw, request_fingerprint=None):
        import shutil
        import uuid
        from app.services.provenance import file_sha256, atomic_json
        from app.services.process_lock import file_lock
        expected = request_fingerprint or self.request_fingerprint()
        target = self.out.parent / ".candidates" / (self.out.stem + "-" + uuid.uuid4().hex)
        target.mkdir(parents=True)
        candidate = target / self.out.name
        with candidate.open("wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        dims = self.validate_output(candidate)
        with file_lock(self.out.with_suffix(self.out.suffix + ".lock")):
            if self.request_fingerprint() != expected:
                raise ValueError("Image request changed during generation")
            receipt = {"request_fingerprint": expected,
                       "output_sha256": file_sha256(candidate), "dimensions": list(dims),
                       "candidate_path": str(candidate.resolve())}
            if self.out.exists():
                shutil.copy2(self.out, target / ("previous" + self.out.suffix))
            if self.receipt_path.exists():
                shutil.copy2(self.receipt_path, target / "previous.request.json")
            pending = target / ("install" + self.out.suffix)
            shutil.copy2(candidate, pending)
            os.replace(pending, self.out)
            atomic_json(self.receipt_path, receipt)

    def is_done(self, min_bytes: int = 50 * 1024) -> bool:
        """Reuse only a decoded output bound to this complete request and its bytes."""
        import json
        from app.services.provenance import file_sha256
        try:
            receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            if receipt.get("request_fingerprint") != self.request_fingerprint(): return False
            if receipt.get("output_sha256") != file_sha256(self.out): return False
            self.validate_output(self.out)
            return True
        except (OSError, ValueError, SyntaxError, KeyError, TypeError):
            return False


def _generate_many_worker(
    gen: ImageGenerator,
    jobq: Queue,
    result: dict,
    channels: list[Channel],
    force: bool,
    prog: "_Progress | None" = None,
    budget=None,
) -> None:
    """单 worker：领一个固定渠道，串行消化队列任务。

    每渠道一个 worker 是三路并行的关键：若所有 worker 都走 failover，
    会全部命中 score 最高的同一渠道，退化成单路并把它打 429。

    重试分两层（2026-09-04 新增）：
      1. 同渠道原地重试：瞬时故障（429/5xx/超时/空 data）退避后**不换路**，
         否则一次抖动就把三家渠道的配额都烧一遍。
      2. 换路：非瞬时（参数非法/NSFW/余额不足）或原地重试耗尽后才换。
    """
    while True:
        try:
            job: ImageJob = jobq.get_nowait()
        except Empty:
            return
        ok = False
        lease = None
        try:
            from app.services.process_lock import file_lock
            candidate_lease = file_lock(job.out.with_suffix(job.out.suffix + ".generation.lock"))
            try:
                candidate_lease.__enter__()
                lease = candidate_lease
            except RuntimeError as exc:
                result[job.id] = None
                result[f"{job.id}__error"] = str(exc)
                continue
            # 幂等：已存在合格文件直接跳过（不占渠道）
            if not force and job.is_done():
                result[job.id] = str(job.out)
                ok = True
                continue
            # 领渠道：按 job._channel_idx 轮转首攻，失败再按序换路
            order = list(range(len(channels)))
            if job._channel_idx is not None:
                # 从 worker 钉住的渠道开始
                order = order[job._channel_idx:] + order[:job._channel_idx]
            ok = False
            for idx in order:
                ch = channels[idx]
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        _throttle()
                        request_fp = job.request_fingerprint()
                        reservation = budget.reserve('image', request_fp) if budget else None
                        job._attempts += 1
                        try:
                            resp = gen.edit(job.req, channel=ch) if isinstance(job.req, ImageEditRequest) else gen.generate(job.req, channel=ch)
                        except BaseException:
                            if reservation: budget.finish(reservation, False)
                            raise
                        if reservation: budget.finish(reservation, True)
                        # 取首个 base64 / url
                        item = resp.data[0] if resp.data else None
                        if item is None:
                            # 空 data 绝大多数是中转上游瞬时抽风（非 prompt 问题），
                            # 按瞬时处理：原地退避重试，不急着烧别家渠道。
                            raise TransientImageError(f"[{ch.name}] 空响应 data")
                        raw = None
                        if getattr(item, "b64_json", None):
                            raw = base64.b64decode(item.b64_json)
                        elif getattr(item, "url", None):
                            raw = _download(item.url, ch.name)
                        if not raw:
                            raise ImageError(f"[{ch.name}] 无图像数据")
                        record_cost(job=job, channel=ch.name, n_bytes=len(raw))
                        job.install(raw, request_fp)
                        result[job.id] = str(job.out)
                        ok = True
                        break
                    except Exception as e:
                        job._last_err = (
                            f"{ch.name}: {type(e).__name__} {str(e)[:120]}")
                        # 瞬时故障先原地重试（不换路）；非瞬时或重试耗尽才换路
                        if (is_transient_error(e)
                                and attempt <= _MAX_TRANSIENT_RETRY):
                            job._transient += 1
                            if _VERBOSE:
                                print(f"    [{job.id}] {ch.name} 瞬时故障"
                                      f"（第{attempt}次），退避后原地重试…")
                            time.sleep(_backoff(attempt))
                            continue
                        break  # 换下一路
                from app.services.provider_budget import BudgetExceeded
                if ok or (job._last_err and 'BudgetExceeded' in job._last_err):
                    break
            if not ok:
                result[job.id] = None
                result[f"{job.id}__error"] = job._last_err or "未知错误"
        finally:
            if lease is not None: lease.__exit__(None, None, None)
            if prog is not None:
                prog.tick(ok=bool(result.get(job.id)))
            jobq.task_done()


def _download(url: str, ch_name: str) -> bytes:
    """从 url 下载图像字节（部分中转返回图片 url 而非 b64）。"""
    import requests as _rq
    try:
        r = _rq.get(url, timeout=120)
    except Exception as e:
        raise TransientImageError(
            f"[{ch_name}] 下载图异常 {type(e).__name__}: {str(e)[:160]}") from e
    if r.status_code != 200:
        raise _mk_error(ch_name, r.status_code, f"下载图失败 {r.text[:160]}")
    return r.content


# ---------------------------------------------------------------------------
# 进度 / ETA（长任务不可静默：300 张图跑 1h 无输出会被当卡死）
# ---------------------------------------------------------------------------
_PROGRESS_EVERY = float(env_get("HV_IMAGE_PROGRESS_EVERY", "15"))


@dataclass
class _Progress:
    """并发安全的计数器 + ETA。monitor 线程按固定间隔打印心跳。"""

    total: int
    t0: float = field(default_factory=time.time)
    done: int = 0
    ok: int = 0
    failed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def tick(self, ok: bool) -> None:
        with self.lock:
            self.done += 1
            if ok:
                self.ok += 1
            else:
                self.failed += 1

    def snap(self) -> tuple[int, int, int, float, float | None]:
        with self.lock:
            d, o, f = self.done, self.ok, self.failed
        el = time.time() - self.t0
        if d <= 0:
            return d, o, f, el, None
        eta = el / d * (self.total - d)
        return d, o, f, el, eta


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60:02d}:{s % 60:02d}"


def _progress_monitor(prog: _Progress, stop: threading.Event) -> None:
    """每 _PROGRESS_EVERY 秒打一次心跳 + ETA（配合 python -u 才不缓冲）。"""
    while not stop.wait(_PROGRESS_EVERY):
        d, o, f, el, eta = prog.snap()
        if d >= prog.total:
            return
        pct = d / prog.total * 100 if prog.total else 0
        eta_txt = _fmt_dur(eta) if eta is not None else "--:--"
        print(f"  [image] 进度 {d}/{prog.total} ({pct:5.1f}%) "
              f"成功{o} 失败{f} 用时{_fmt_dur(el)} ETA {eta_txt}", flush=True)
    return


def generate_many(
    jobs: list[ImageJob],
    force: bool = False,
    workers: int | None = None,
    progress: bool = True,
    budget=None,
) -> dict:
    """多任务并行生图（三路 worker）。返回 {job.id: 落盘路径 或 None}。

    - workers 默认 = min(渠道数, 任务数)；每渠道各钉一路
    - 已存在且 >50KB 的任务幂等跳过（不占渠道）
    - 单任务：同渠道瞬时故障原地重试 ≤ HV_IMAGE_TRANSIENT_RETRY 次，
      仍失败才换路；全渠道失败回填 None，已成功文件保留
    - 任一侧 429/失败只影响该任务，不堵死其他两路
    - progress=True 时起 monitor 线程，每 15s 打一次「进度/ETA」心跳

    不 raise（部分失败也要把已成功的产物带回来）；调用方据 None 汇总。
    """
    if not jobs:
        return {}
    if len({j.id for j in jobs}) != len(jobs) or len({str(j.out.resolve()).casefold() for j in jobs}) != len(jobs):
        raise ValueError("duplicate image job IDs or output paths")
    if not force and all(j.is_done() for j in jobs):
        return {j.id: str(j.out) for j in jobs}
    gen = get_image()
    channels: list[Channel] = gen.relay.all()
    if not channels:
        raise ImageError("无可生图渠道")
    n_worker = workers or max(1, min(len(channels), len(jobs)))
    jobq: Queue = Queue()
    for i, j in enumerate(jobs):
        j._channel_idx = i % n_worker  # 轮转分配首攻渠道
        jobq.put(j)
    result: dict = {}
    prog = _Progress(total=len(jobs))
    stop = threading.Event()
    mon = None
    if progress and len(jobs) > 1:
        mon = threading.Thread(target=_progress_monitor, args=(prog, stop),
                               daemon=True)
        mon.start()
    threads = [
        threading.Thread(
            target=_generate_many_worker,
            args=(gen, jobq, result, channels, force, prog, budget),
            daemon=True,
        )
        for _ in range(n_worker)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        stop.set()
        if mon is not None:
            mon.join(timeout=1.0)
    _, o, f, el, _ = prog.snap()
    print(f"  [image] 完成 {o}/{len(jobs)} 成功，{f} 失败，用时 {_fmt_dur(el)}",
          flush=True)
    return result


# ---------------------------------------------------------------------------
# 成本台账（state/cost_ledger.json）
# ---------------------------------------------------------------------------
# 为什么要台账：三个渠道池各有独立 quota，生图是全程唯一真花钱的环节，
# 而在此之前「花了多少张、走的哪条路、什么规格」全靠记忆和渠道后台对账。
# 这里按 job 落一条不可变记录，供事后核算与异常排查（哪条路在烧钱）。
#
# 价格：不内置任何单价（中转报价会变，写死就是错账）。
#   需要金额时自建 state/cost_prices.json：
#     {"gpt-image-2:low": 0.02, "gpt-image-2:medium": 0.07}
#   键 = "model:quality"。缺失的键 → cost=null（仍计数，事后可补算）。
_COST_PATH = STATE_DIR / "cost_ledger.json"
_PRICES_PATH = STATE_DIR / "cost_prices.json"
_COST_LOCK = threading.Lock()
_COST_MAX = int(env_get("HV_COST_MAX_ENTRIES", "20000"))


def _load_prices() -> dict:
    """读价格表（可选）。文件不存在 → {}，所有条目 cost=null。"""
    try:
        if _PRICES_PATH.exists():
            import json as _json
            return _json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _price_of(model: str, quality: str) -> float | None:
    p = _load_prices()
    if not p:
        return None
    for key in (f"{model}:{quality}", f"{model}", f"*:{quality}"):
        if key in p:
            try:
                return float(p[key])
            except (TypeError, ValueError):
                return None
    return None


def record_cost(*, job: ImageJob, channel: str, n_bytes: int) -> dict:
    """记一条生图开销。失败静默（台账永远不该打断生产）。

    ★ 口径：只记【成功落盘】的图。中途失败的不记——中转对 429/超时不计费，
    但 5xx 是否计费取决于上游是否已生成，无法从本地判定，需以渠道后台为准。
    换言之本台账是「张数下界」，不是账单。

    字段：ts / job / channel / model / size / quality / bytes /
          attempts / transient_retries / out / cost(可空)
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "job": job.id,
        "channel": channel,
        "model": getattr(job.req, "model", None),
        "size": getattr(job.req, "size", None),
        "quality": getattr(job.req, "quality", None),
        "bytes": int(n_bytes),
        "attempts": job._attempts,
        "transient_retries": job._transient,
        "out": str(job.out),
    }
    unit = _price_of(entry["model"] or "", entry["quality"] or "")
    entry["cost"] = round(unit, 6) if unit is not None else None
    try:
        with _COST_LOCK:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            rows: list = []
            if _COST_PATH.exists():
                try:
                    import json as _json
                    rows = _json.loads(_COST_PATH.read_text(encoding="utf-8"))
                    if not isinstance(rows, list):
                        rows = []
                except Exception:
                    rows = []
            rows.append(entry)
            if len(rows) > _COST_MAX:  # 环形裁剪，防长片项目撑爆文件
                rows = rows[-_COST_MAX:]
            import json as _json
            _COST_PATH.write_text(
                _json.dumps(rows, ensure_ascii=False, indent=1),
                encoding="utf-8")
    except Exception as e:  # 台账失败不影响生图
        print(f"  [image][warn] cost 台账写入失败（不中断生图）: {e}")
    return entry


def cost_summary(since: str | None = None) -> dict:
    """汇总台账：总张数 / 按渠道 / 按规格 / 金额（有价表才统计）。

    since: 只统计 ts >= since 的条目（ISO 串，如 "2026-09-04"）。
    """
    import json as _json
    rows: list = []
    if _COST_PATH.exists():
        try:
            rows = _json.loads(_COST_PATH.read_text(encoding="utf-8"))
        except Exception:
            rows = []
    if since:
        rows = [r for r in rows if str(r.get("ts", "")) >= since]
    by_channel: dict = {}
    by_spec: dict = {}
    by_job_prefix: dict = {}
    total_cost = 0.0
    unpriced = 0
    for r in rows:
        by_channel[r.get("channel")] = by_channel.get(r.get("channel"), 0) + 1
        spec = f"{r.get('model')}:{r.get('quality')}@{r.get('size')}"
        by_spec[spec] = by_spec.get(spec, 0) + 1
        # job id 形如 LGT012 / LSM-15-001 → 取前 3 位字母前缀归集到「集」
        jid = str(r.get("job") or "")
        prefix = "".join(c for c in jid[:4] if c.isalpha()) or "?"
        by_job_prefix[prefix] = by_job_prefix.get(prefix, 0) + 1
        c = r.get("cost")
        if isinstance(c, (int, float)):
            total_cost += float(c)
        else:
            unpriced += 1
    return {
        "total_images": len(rows),
        "by_channel": dict(sorted(by_channel.items(), key=lambda kv: -kv[1])),
        "by_spec": dict(sorted(by_spec.items(), key=lambda kv: -kv[1])),
        "by_episode": dict(sorted(by_job_prefix.items(), key=lambda kv: -kv[1])),
        "cost_total": round(total_cost, 4) if total_cost else None,
        "unpriced": unpriced,
        "prices_file": str(_PRICES_PATH),
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def save_b64_image(b64: str, path: "str | Path") -> Path:
    """把中转返回的 base64 图解码落盘，返回落盘路径。

    注意参数顺序是 (b64, path)：cli.py 与 controllers/v1/image.py 均按此调用。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(b64))
    return p


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_image: ImageGenerator | None = None


def get_image() -> ImageGenerator:
    global _image
    if _image is None:
        _image = ImageGenerator()
    return _image
