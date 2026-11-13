"""
gpt-image-2 生图服务（subrouter.ai 中转）。

核心：
  - 三渠道池轮询 + 健康度降级（managers/relay.py）
  - 严格参数校验（对抗 subrouter 静默忽略非法 size/quality 的陷阱）
  - 实际像素测量、下载与响应解码
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
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class ReceivedImageError(ImageError):
    """Local persistence failed after a provider response; never dispatch it again."""


def stop_reason(exc):
    text = str(exc).lower()
    if any(s in text for s in ('content policy', 'content_policy', 'safety', 'moderation', '内容策略', '违反', '违规')):
        return '内容被拒绝：请调整画面描述，不自动换渠道重发'
    status = getattr(exc, 'http_status', None)
    if status is not None and 400 <= status < 500 and status != 429:
        return f'请求被拒绝（HTTP {status}）：先修正请求或渠道配置，不自动原样重发'
    if any(s in text for s in ('readtimeout', 'read timed out', 'outcome unknown', 'connection reset', 'remote end closed')):
        return '结果不确定：保留本次请求，先核对响应，不自动重复调用'
    return None


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
    return ImageError(msg, http_status=status)


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


def _dispatch_capacity(relay, channel):
    capacity = getattr(channel, 'max_concurrency', 1)
    health = relay.health(channel.name) if hasattr(relay,'health') else None
    if health is None:
        return capacity
    return getattr(health, 'dispatch_capacity', capacity if health.is_healthy else 0)


def _available(relay, channel, model):
    return channel.supports(model) and _dispatch_capacity(relay, channel) > 0


def capacity_estimate(model, count, relay=None, budget_path=None):
    """Estimate from completed provider attempts, excluding time waiting for a slot."""
    import heapq
    from statistics import median
    from app import paths
    from app.services.narration_pipeline import read
    relay = relay or get_relay()
    channels = [ch for ch in relay.all() if _available(relay, ch, model)]
    samples = {ch.name: [] for ch in channels}
    current = {ch.name: [] for ch in channels}
    budgets = {}
    preferred = Path(budget_path).resolve() if budget_path else None
    episodes_root = paths.episodes_dir().resolve()
    rows = read(_COST_PATH) if _COST_PATH.is_file() else []
    recent = [row for row in reversed(rows) if row.get('channel') in samples
              and row.get('model') == model and row.get('attempt_id') and row.get('budget_path')]
    for row in recent[:128]:
        channel = row['channel']
        path = Path(row['budget_path']).resolve()
        observations = current[channel] if path == preferred else samples[channel]
        if len(observations) >= 32 or (path != preferred and not path.is_relative_to(episodes_root)):
            continue
        if path not in budgets:
            attempts = read(path).get('attempts', []) if path.is_file() else []
            budgets[path] = {a['id']: a for a in attempts}
        attempt = budgets[path].get(row['attempt_id'])
        if not attempt or attempt.get('kind') != 'image' or attempt.get('status') != 'succeeded':
            continue
        if attempt.get('supplier', channel) != channel:
            continue
        start, end = attempt.get('reserved_at'), attempt.get('completed_at')
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            observations.append(end - start)
    routes = []
    for ch in channels:
        observed = current[ch.name] or samples[ch.name]
        routes.append(dict(name=ch.name, slots=_dispatch_capacity(relay, ch), samples=len(observed),
                           median_seconds=round(median(observed), 2) if observed else None))
    estimate = None
    if not count:
        estimate = 0.
    elif routes and all(route['median_seconds'] for route in routes):
        lanes = [(r['median_seconds'], r['median_seconds']) for r in routes for _ in range(r['slots'])]
        heapq.heapify(lanes)
        for _ in range(count):
            finish, period = heapq.heappop(lanes)
            heapq.heappush(lanes, (finish + period, period))
        estimate = round(finish, 1)
    return dict(routes=routes, seconds=estimate)


class ImageGenerator:
    def __init__(self, max_attempts: int = 3):
        self.relay = get_relay()
        self.max_attempts = max_attempts

    # ---- 公共 ----
    def generate(self, req: ImageGenRequest,
                 channel: "Channel | None" = None, *, budget=None) -> ImageGenResponse:
        """生图。

        channel 为 None  → 原 failover 语义（按健康度排序依次尝试）
        channel 非 None  → **只打该渠道**，失败直接抛，由调用方决定换路。

        后者是三路并行的前置条件：并发 worker 若都走 failover，
        会全部命中同一个 score 最高的渠道，三路退化成单路并把它打 429。
        """
        if budget is not None:
            from app.services.provider_budget import image_budget_scope
            with image_budget_scope(budget): return self.generate(req, channel)
        req.validate_for_subrouter()
        return self._call_with_failover(
            path="/v1/images/generations",
            body=req.model_dump(exclude_none=True),
            channel=channel,
        )

    def edit(self, req: ImageEditRequest,
             channel: "Channel | None" = None, *, budget=None) -> ImageGenResponse:
        if budget is not None:
            from app.services.provider_budget import image_budget_scope
            with image_budget_scope(budget): return self.edit(req, channel)
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
        from app.services.provider_budget import current_image_budget, BudgetExceeded
        current_image_budget()
        if channel is not None:
            if not channel.supports(body["model"]):
                raise ImageError(f"渠道 {channel.name} 未启用或不支持模型 {body['model']}")
            channels = [channel]
        else:
            channels = [c for c in self.relay.all() if _available(self.relay,c,body['model'])]
            if not channels: raise ImageError(f"无可用渠道支持模型 {body['model']}")
            channels.sort(key=lambda c: self.relay.health(c.name).score() if self.relay.health(c.name) else 100,
                          reverse=True)
            channels = channels[:self.max_attempts]

        last_err: Exception | None = None
        for ch in channels:
            t0 = time.time()
            try:
                resp = self._post(ch, path, body)
            except (BudgetExceeded, ReceivedImageError):
                raise
            except Exception as e:
                self._record_health(ch.name, False, time.time() - t0)
                if channel is not None or stop_reason(e): raise
                last_err = e
                continue
            self._record_health(ch.name, True, time.time() - t0)
            return resp
        raise ImageError(
            f"所有渠道失败（{self.max_attempts} 尝试）。最后错误: {last_err}"
        )

    def _record_health(self, channel, ok, latency):
        # A telemetry failure cannot turn a received response into another dispatch.
        try:
            self.relay.record(channel, ok=ok, latency_s=latency)
        except Exception as exc:
            from app.managers.relay import HEALTH_PATH
            _record_error(HEALTH_PATH.with_suffix('.error.json'), 'channel_statistics', exc)

    def _post(self, ch: Channel, path: str, body: dict) -> ImageGenResponse:
        from app.services.provider_budget import current_image_budget, _image_receipt, _image_guard
        from app.services.provenance import fingerprint, atomic_json
        budget = current_image_budget()
        with self.relay.request_slot(ch.name) as waited:
            token = budget.reserve('image', fingerprint([path, body]), supplier=ch.name, queue_seconds=waited)
            target, request_fp = _image_receipt.get() or (
                budget.path.parent/'responses'/(token+'.json'), fingerprint([path, body]))
            guard=_image_guard.get()
            if guard:
                atomic_json(guard,dict(request_fingerprint=request_fp,response_path=str(target),
                                       reason='结果不确定：先核对上次请求，可复用已有画面继续'))
            def finish(ok):
                try: budget.finish(token, ok)
                except Exception as exc:
                    # The durable reservation already consumed this attempt.
                    _record_error(budget.path.with_suffix('.error.json'), 'budget_finalization', exc)
            try:
                response = self._post_unlocked(ch, path, body)
            except Exception as exc:
                finish(False)
                if guard and not (stop_reason(exc) or '').startswith('结果不确定'):
                    guard.unlink(missing_ok=True)
                raise
            try:
                atomic_json(target, dict(request_fingerprint=request_fp, channel=ch.name,
                                         attempt_id=token, budget_path=str(budget.path.resolve()),
                                         response=response.model_dump(mode='json')))
            except Exception as exc:
                finish(False)
                _record_error(Path(target).with_suffix('.error.json'), 'response_persistence', exc)
                raise ReceivedImageError(f'outcome unknown: received response could not be saved; attempt {token}') from exc
            finish(True)
            if guard:guard.unlink(missing_ok=True)
            return response

    def _post_unlocked(self, ch: Channel, path: str, body: dict) -> ImageGenResponse:
        url = f"{ch.base_url}{path}"
        try:
            r = requests.post(
                url,
                headers={**ch.auth_header, "Content-Type": "application/json"},
                json=body,
                # connect/read 分开给：connect 卡住时 read 的长超时救不了
                # （2026-09-29 实测供应商 503 之后连接既不返回也不报错，整批
                #  15 分钟零输出；单一 300s 让一个挂死连接占住 worker 线程，
                #  拖住同批所有任务）。超时值可由环境变量收窄，便于按供应商调。
                timeout=(float(env_get("HV_IMAGE_CONNECT_TIMEOUT", "20")),
                         float(env_get("HV_IMAGE_READ_TIMEOUT", "180"))),
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise TransientImageError(f'{ch.name}: connect timeout before dispatch') from exc
        except requests.exceptions.SSLError as exc:
            if 'certificate_verify_failed' in str(exc).lower():
                raise ImageError(f'{ch.name}: certificate verification failed before dispatch') from exc
            raise ImageError(f'outcome unknown: {ch.name} TLS interruption') from exc
        except requests.exceptions.ReadTimeout as exc:
            raise ImageError(f'outcome unknown: {ch.name} {type(exc).__name__}') from exc
        except requests.exceptions.ConnectionError as exc:
            detail=str(exc).lower()
            if any(s in detail for s in ('failed to establish a new connection','connection refused','failed to resolve','getaddrinfo failed')):
                raise TransientImageError(f'{ch.name}: connection not established') from exc
            raise ImageError(f'outcome unknown: {ch.name} connection interrupted') from exc
        except Exception as e:
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
        try:
            return ImageGenResponse.model_validate(r.json())
        except ValueError as exc:
            # HTTP 200 may already be billable. Keep the original response on
            # disk for investigation; malformed JSON is not permission to retry.
            from app.services.provider_budget import current_image_budget, _image_receipt
            from app.services.provenance import atomic_json
            import uuid
            target, request_fp = _image_receipt.get() or (
                current_image_budget().path.parent/'responses'/(uuid.uuid4().hex+'.json'), None)
            raw_path = Path(target).with_suffix('.unparsed-response.json')
            atomic_json(raw_path, dict(channel=ch.name, request_fingerprint=request_fp,
                                       http_status=r.status_code, response_text=r.text))
            raise ImageError('outcome unknown: HTTP 200 response could not be parsed; saved at '+str(raw_path)) from exc


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
    dispatch_directory: Path | None = None
    model_bound: bool = field(default=False, init=False, repr=False)
    awaiting_response: bool = field(default=False, init=False, repr=False)

    # worker 内部用
    _channel_idx: int | None = field(default=None, repr=False)
    _attempts: int = field(default=0, repr=False)
    _transient: int = field(default=0, repr=False)
    _last_err: str | None = field(default=None, repr=False)

    @property
    def out(self) -> Path:
        return Path(self.out_path)

    def request_fingerprint(self, *, model=None):
        from app.services.provenance import fingerprint, file_sha256
        if isinstance(self.req, ImageEditRequest) and self.reference_paths:
            import hashlib
            if len(self.reference_paths)!=1 or not self.req.image:
                raise ValueError('image edit requires one bound reference')
            sent = hashlib.sha256(base64.b64decode(self.req.image, validate=True)).hexdigest()
            if sent != file_sha256(self.reference_paths[0]):
                raise ValueError('reference changed after the image request was prepared')
        request = self.req.model_dump(mode="json")
        if model is not None:
            request["model"] = model
        return fingerprint({"version": "image-cache-v2", "request": request,
                            "references": {str(Path(p).resolve()): file_sha256(p) for p in self.reference_paths}})

    def provider_fingerprint(self, *, model=None):
        from app.services.provenance import fingerprint
        endpoint = '/v1/images/edits' if isinstance(self.req, ImageEditRequest) else '/v1/images/generations'
        body = self.req.model_dump(exclude_none=True)
        if model is not None:
            body['model'] = model
        return fingerprint([endpoint, body])

    @property
    def receipt_path(self):
        return self.out.with_suffix(self.out.suffix + ".request.json")

    def validate_output(self, path):
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()
            actual = im.size
        # These are source plates, not encoded video frames. Both compositors
        # resize/crop locally; the provider need not emit the delivery canvas.
        # Match the existing-asset minimum instead of inventing a stricter gate.
        if min(actual) < 256:
            raise ValueError(f"Image dimensions {actual} are too small for a source plate (minimum edge 256)")
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
            receipt['video_fit'] = source_fit(dims)
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


class _SupplierQueue:
    """Reserve a free compatible supplier slot before removing its next job."""
    def __init__(self, jobs, channels, gen):
        self.channels = channels
        self.gen = gen
        self.pending = sorted(jobs, key=lambda j: sum(c.supports(j.req.model) for c in channels))
        self.active = [0] * len(channels)
        self.changed = threading.Condition()

    def take(self):
        with self.changed:
            while self.pending:
                now = time.monotonic()
                for pos, job in enumerate(self.pending):
                    if not any(c.supports(job.req.model) for c in self.channels):
                        return self.pending.pop(pos), None
                    routes = [i for i, c in enumerate(self.channels)
                              if _available(self.gen.relay, c, job.req.model)
                              and getattr(self.gen, '_unavailable_until', {}).get(c.name, 0) <= now]
                    free = [i for i in routes if self.active[i] < _dispatch_capacity(self.gen.relay, self.channels[i])]
                    if free:
                        slot = min(free, key=lambda i: self.active[i] / self.channels[i].max_concurrency)
                        self.active[slot] += 1
                        return self.pending.pop(pos), slot
                # A cooldown expires without a worker finishing. Wake also when
                # all suppliers are cooling down; keep unsent jobs in the queue.
                self.changed.wait(timeout=1.0)
            raise Empty

    def release(self, slot):
        with self.changed:
            self.active[slot] -= 1
            self.changed.notify_all()


def _generate_many_worker(
    gen: ImageGenerator,
    jobq: Queue | _SupplierQueue,
    result: dict,
    channels: list[Channel],
    force: bool,
    prog: "_Progress | None" = None,
    budget=None,
    provider_attempts_per_job: int | None = None,
) -> None:
    """Dispatch within budget; saved responses always take the local recovery path."""
    while True:
        try:
            if isinstance(jobq, _SupplierQueue):
                job, booked = jobq.take()
            else:
                job, booked = jobq.get_nowait(), None
        except Empty:
            return
        ok = False
        lease = None
        try:
            from app.services.process_lock import file_lock
            from contextlib import ExitStack
            from app.services.provenance import fingerprint
            shared_stop = None
            candidate_lease = ExitStack()
            try:
                if getattr(job,'dispatch_directory',None) is not None:
                    family = fingerprint([job.id, job.req.model_dump(exclude={'model'})])
                    shared_stop = job.dispatch_directory/(family+'.unknown.json')
                    candidate_lease.enter_context(file_lock(shared_stop.with_suffix('.lock')))
                candidate_lease.enter_context(file_lock(job.out.with_suffix(job.out.suffix + ".generation.lock")))
                lease = candidate_lease
            except RuntimeError as exc:
                candidate_lease.close()
                result[job.id] = None
                result[f"{job.id}__error"] = str(exc)
                continue
            # 幂等：已存在合格文件直接跳过（不占渠道）
            if not force and job.is_done():
                result[job.id] = str(job.out)
                ok = True
                continue
            # 领渠道：按 job._channel_idx 轮转首攻，失败再按序换路
            order = [i for i,c in enumerate(channels) if _available(getattr(gen,'relay',None),c,job.req.model)]
            if not order:
                result[job.id] = None
                result[f"{job.id}__error"] = f"无可用渠道支持模型 {job.req.model}"
                continue
            response_path = job.out.with_suffix(job.out.suffix + '.response.json')
            from app.services.narration_pipeline import read
            from app.services.provenance import atomic_json
            from app.services.provider_budget import image_budget_scope
            request_fp = job.request_fingerprint()
            stop_path = job.out.with_suffix(job.out.suffix+'.dispatch-stop.json')
            if shared_stop and shared_stop.exists() and not response_path.exists():
                previous=read(shared_stop)
                received=previous.get('response_path')
                if not received or not Path(received).is_file():
                    result[job.id] = None
                    result[job.id+'__error'] = previous['reason']
                    continue
            if stop_path.exists() and not response_path.exists():
                stopped = read(stop_path)
                if stopped.get('request_fingerprint') == request_fp:
                    result[job.id] = None
                    result[job.id+'__error'] = stopped['reason']
                    continue
            # A paid response survives download failures and process restarts.
            # Never make another generation request when this receipt exists.
            if response_path.exists():
                try:
                    _restore_response(job, response_path, allow_download=True)
                    result[job.id] = str(job.out)
                    ok = True
                except Exception as exc:
                    # The receipt is paid evidence: the message must say what is
                    # wrong, not just that recovery is needed. Measured 2026-10-13
                    # on XL22b1: a bare "ValueError" hid "provider returned
                    # 1086x1448 for a 1080x1920 request".
                    result[job.id] = None
                    result[f'{job.id}__error'] = (
                        f'saved response requires recovery: {type(exc).__name__}: {exc} '
                        f'(receipt {response_path}; recover saved pixels before requesting new images: '
                        f'bin/recover_image_receipt.py)')
                continue
            if job._channel_idx is not None:
                # 从 worker 钉住的渠道开始
                start = job._channel_idx % len(order)
                order = order[start:] + order[:start]
            if booked in order:
                order.remove(booked)
                order.insert(0, booked)
            if provider_attempts_per_job is not None:
                order = order[:provider_attempts_per_job]
            provider_attempts = 0
            ok = False
            for idx in order:
                if booked is not None and idx != booked:
                    jobq.release(booked)
                    booked = None
                if provider_attempts_per_job is not None and provider_attempts >= provider_attempts_per_job:
                    break
                ch = channels[idx]
                if (isinstance(jobq, _SupplierQueue) and booked != idx
                        and _dispatch_capacity(gen.relay, ch) < ch.max_concurrency):
                    # Recovery probes belong to a reserved queue slot, not to
                    # concurrent failovers from other suppliers.
                    continue
                if getattr(gen,'_unavailable_until',{}).get(ch.name,0)>time.monotonic():
                    job._last_err = '服务暂不可用，稍后恢复原任务；本次未发送请求'
                    continue
                attempt = 0
                while True:
                    if provider_attempts_per_job is not None and provider_attempts >= provider_attempts_per_job:
                        break
                    attempt += 1
                    provider_attempts += 1
                    response_received = False
                    try:
                        _throttle()
                        request_fp = job.request_fingerprint()
                        job._attempts += 1
                        with image_budget_scope(budget, response_path, request_fp, guard=shared_stop):
                            resp = gen.edit(job.req, channel=ch) if isinstance(job.req, ImageEditRequest) else gen.generate(job.req, channel=ch)
                        response_received = True
                        if not response_path.exists():
                            atomic_json(response_path, dict(request_fingerprint=request_fp, channel=ch.name,
                                                            response=resp.model_dump(mode='json')))
                        # 取首个 base64 / url
                        _install_response(job, resp, request_fp, ch.name)
                        result[job.id] = str(job.out)
                        ok = True
                        break
                    except Exception as e:
                        job._last_err = (
                            f"{ch.name}: {type(e).__name__} {str(e)[:120]}")
                        reason = stop_reason(e)
                        if getattr(e,'http_status',None)==503:
                            if not hasattr(gen,'_unavailable_until'):gen._unavailable_until={}
                            gen._unavailable_until[ch.name]=time.monotonic()+60
                            # A 503 did not deliver an image; try the next healthy
                            # supplier within the same durable attempt allowance.
                            response_received=False
                            break
                        if reason:
                            if shared_stop and reason.startswith('结果不确定') and not shared_stop.exists():
                                atomic_json(shared_stop,dict(request_fingerprint=request_fp,response_path=str(response_path),reason=reason))
                            atomic_json(stop_path,dict(request_fingerprint=request_fp,reason=reason,error=job._last_err))
                            job._last_err = reason
                            response_received = True  # Stop this request across channels.
                            break
                        if response_received or response_path.exists():
                            response_received = True
                            job._last_err = (
                                f'saved response requires recovery: {type(e).__name__}: {e} '
                                f'(receipt {response_path}, see bin/recover_image_receipt.py)')
                            break
                        # 瞬时故障先原地重试（不换路）；非瞬时或重试耗尽才换路
                        if (is_transient_error(e)
                                and attempt <= _MAX_TRANSIENT_RETRY
                                and (provider_attempts_per_job is None
                                     or provider_attempts < provider_attempts_per_job)):
                            job._transient += 1
                            if _VERBOSE:
                                print(f"    [{job.id}] {ch.name} 瞬时故障"
                                      f"（第{attempt}次），退避后原地重试…")
                            time.sleep(_backoff(attempt))
                            continue
                        break  # 换下一路
                from app.services.provider_budget import BudgetExceeded
                if ok or response_received or (job._last_err and 'BudgetExceeded' in job._last_err):
                    break
            if not ok:
                result[job.id] = None
                result[f"{job.id}__error"] = job._last_err or "未知错误"
        finally:
            if booked is not None:
                jobq.release(booked)
            if lease is not None: lease.__exit__(None, None, None)
            if prog is not None:
                prog.tick(ok=bool(result.get(job.id)))
            if not isinstance(jobq, _SupplierQueue):
                jobq.task_done()


def quarantine_saved_response(response_path, reason):
    """Move a paid-but-unusable saved response out of the way, without deleting it.

    Keep corrupt or otherwise unusable response evidence without deletion.
    Normal source-size/aspect differences are handled by local fitting and must
    be recovered first. Quarantine makes the job missing, so a later explicitly
    budgeted run may request a replacement.
    """
    import shutil
    from datetime import datetime, timezone
    from app.services.narration_pipeline import read
    from app.services.provenance import atomic_json, file_sha256

    response_path = Path(response_path)
    if response_path.suffix != ".json" or not response_path.name.endswith(".response.json"):
        raise ValueError(f"not a saved image response: {response_path}")
    if not response_path.is_file():
        raise FileNotFoundError(f"saved response not found: {response_path}")
    if not (reason or "").strip():
        raise ValueError("a quarantine reason is required; paid evidence is never moved silently")
    saved = read(response_path)
    fp = saved.get("request_fingerprint") or "unknown"
    src_dir = response_path.parent
    if src_dir.name == "_quarantine" or src_dir.parent.name == "_quarantine":
        raise ValueError("response is already quarantined")
    dst_dir = src_dir.parent / "_quarantine" / fp
    if dst_dir.exists():
        raise ValueError(f"quarantine target already exists: {dst_dir}")
    dst_dir.mkdir(parents=True)
    moved = []
    for p in sorted(src_dir.iterdir()):
        shutil.move(str(p), str(dst_dir / p.name))
        moved.append(p.name)
    try:
        src_dir.rmdir()
    except OSError:
        pass
    event = dict(request_fingerprint=fp, channel=saved.get("channel"),
                 reason=reason.strip(), moved_files=moved,
                 response_sha256=file_sha256(dst_dir / response_path.name),
                 quarantined_at=datetime.now(timezone.utc).isoformat())
    atomic_json(dst_dir / "quarantine.json", event)
    return dst_dir


def source_fit(size, target=(1080, 1920)):
    """Describe the existing v2 cover transform; raw pixels stay untouched."""
    w, h = size
    tw, th = target
    scale = max(tw / w, th / h)
    return dict(mode='fit_cover_v2', target=list(target), scale=scale,
                retained_area=(tw / scale) * (th / scale) / (w * h),
                content_approved=False)


def _record_error(path, phase, exc):
    """Store stack locations, never image payloads or credentials."""
    import traceback
    from app.services.provenance import atomic_json
    record = dict(phase=phase, error_type=type(exc).__name__, error=str(exc)[:600],
                  stack=traceback.format_tb(exc.__traceback__), recorded_at=time.time())
    try:
        atomic_json(path, record)
    except OSError:
        pass
    print(f"  [image][{phase}] {type(exc).__name__}; details: {path}", flush=True)


def _restore_response(job, response_path, *, allow_download, budget_path=None):
    from app.services.narration_pipeline import read
    from app.services.provenance import atomic_json, file_sha256
    saved = read(response_path)
    fp = job.request_fingerprint()
    if saved.get('request_fingerprint') != fp:
        raise ImageError('saved provider response belongs to another request')
    attempt_id = saved.get('attempt_id')
    budget_path = saved.get('budget_path') or budget_path
    if budget_path and not attempt_id:
        # Older receipts lack an attempt ID. Link only a unique successful dispatch.
        key = job.provider_fingerprint()
        matches = [a['id'] for a in read(budget_path)['attempts'] if a['kind']=='image'
                   and a['request_fingerprint']==key and a['status']=='succeeded']
        if len(matches)!=1: raise ImageError('no unique successful attempt in the supplied budget')
        attempt_id = matches[0]
    link = dict(attempt_id=attempt_id, budget_path=str(budget_path) if budget_path else None)
    response = ImageGenResponse.model_validate(saved['response'])
    _install_response(job, response, fp, saved['channel'], allow_download=allow_download,
                      response_path=response_path, link=link, reuse_identical=True)
    record = dict(receipt_path=str(Path(response_path).resolve()), receipt_sha256=file_sha256(response_path),
                  request_fingerprint=fp, output_sha256=file_sha256(job.out), new_provider_calls=0, **link)
    dest = job.out.with_suffix(job.out.suffix + '.recovery.json')
    if dest.exists():
        old = read(dest)
        if old.get('receipt_sha256')==record['receipt_sha256'] and old.get('output_sha256')==record['output_sha256']:
            return job.out
        atomic_json(dest.parent/'.recoveries'/(file_sha256(dest)+'.json'), old)
    atomic_json(dest, record)
    return job.out


def recover_saved_response(job, response_path, *, allow_download=False, budget_path=None):
    """Recover the original response; offline unless an existing URL download is allowed."""
    from app.services.process_lock import file_lock
    with file_lock(job.out.with_suffix(job.out.suffix + '.generation.lock')):
        return _restore_response(job, response_path, allow_download=allow_download, budget_path=budget_path)


def _install_response(job, response, request_fp, channel, *, allow_download=True, response_path=None, link=None, reuse_identical=False):
    import hashlib
    from app.services.provenance import file_sha256
    phase = 'response_decode'
    try:
        item = response.data[0] if response.data else None
        if item is None: raise ImageError('empty paid response; manual disposition required')
        if item.b64_json:
            raw = base64.b64decode(item.b64_json, validate=True)
        elif item.url:
            phase = 'image_download'
            if not allow_download:
                raise ImageError('no embedded image bytes; locate original downloaded image')
            for attempt in range(3):
                try:
                    raw = _download(item.url, channel)
                    break
                except TransientImageError:
                    if attempt == 2: raise
                    time.sleep(min(2 ** attempt, 4))
        else: raise ImageError('paid response has no image data')
        phase = 'image_install'
        if not (reuse_identical and job.is_done() and file_sha256(job.out) == hashlib.sha256(raw).hexdigest()):
            job.install(raw, request_fp)
    except Exception as exc:
        _record_error(job.out.with_suffix(job.out.suffix+'.error.json'), phase, exc)
        raise ImageError(f'{phase}: {type(exc).__name__}: {str(exc)[:240]}') from exc
    record_cost(job=job, channel=channel, n_bytes=len(raw), response_path=response_path, link=link)



def run_probe(request, budget_id, max_images):
    from app.paths import state_dir
    from app.services.provider_budget import for_run
    from app.services.provenance import fingerprint
    if not budget_id or len(budget_id)>100: raise ValueError('stable probe budget ID required')
    budget = for_run(state_dir(), ['image-probe',budget_id],dict(max_images=max_images))
    key = fingerprint([budget_id,request.model_dump(mode='json')])
    job = ImageJob('probe',request,state_dir()/'image-probes'/key/'image.png')
    result = generate_many([job],budget=budget,progress=False)
    if not result.get(job.id): raise ImageError(result.get('probe__error','image probe incomplete'))
    return job.out


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
    callback: object = None
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
            if self.callback:
                self.callback(dict(done=self.done, total=self.total, succeeded=self.ok, failed=self.failed))

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
    on_progress=None,
    provider_attempts_per_job: int | None = None,
) -> dict:
    """多任务并行生图。返回 {job.id: 落盘路径 或 None}。

    - workers 不超过可用供应商并发总额和待生成任务数
    - 请求指纹与图像校验通过的缓存直接复用（不占渠道）
    - 单任务：同渠道瞬时故障原地重试 ≤ HV_IMAGE_TRANSIENT_RETRY 次，
      仍失败才换路；全渠道失败回填 None，已成功文件保留
    - 任一侧 429/失败只影响该任务，不堵死其他两路
    - progress=True 时起 monitor 线程，每 15s 打一次「进度/ETA」心跳

    配置/不支持模型在整批发请求前拒绝；运行中的单图失败据 None 汇总。
    """
    if not jobs:
        return {}
    if (provider_attempts_per_job is not None
            and (type(provider_attempts_per_job) is not int or provider_attempts_per_job < 1)):
        raise ValueError("provider_attempts_per_job must be a positive integer")
    if len({j.id for j in jobs}) != len(jobs) or len({str(j.out.resolve()).casefold() for j in jobs}) != len(jobs):
        raise ValueError("duplicate image job IDs or output paths")
    if not force and all(j.is_done() for j in jobs):
        return {j.id: str(j.out) for j in jobs}
    recoverable = [j for j in jobs if (getattr(j, 'awaiting_response', False) or j.out.with_suffix(j.out.suffix+'.response.json').exists())
                   and (force or not j.is_done())]
    if recoverable:
        recovered = {}
        for job in recoverable:
            response_path = job.out.with_suffix(job.out.suffix+'.response.json')
            if getattr(job, 'awaiting_response', False) and not response_path.is_file():
                recovered[job.id] = None
                recovered[job.id+'__error'] = '上次请求已登记，结果待核对，未重复发送'
                continue
            try:
                recover_saved_response(job, response_path, allow_download=True)
                recovered[job.id] = str(job.out)
            except Exception as exc:
                recovered[job.id] = None
                recovered[job.id+'__error'] = (
                    f'saved response requires recovery: {type(exc).__name__}: {exc} '
                    f'(receipt {job.out.with_suffix(job.out.suffix+".response.json")}, '
                    f'see bin/recover_image_receipt.py)')
        remaining = [j for j in jobs if j.id not in recovered]
        if remaining: recovered.update(generate_many(remaining,force,workers,progress,budget,on_progress,provider_attempts_per_job))
        return recovered
    gen = get_image()
    channels: list[Channel] = gen.relay.all()
    if not channels:
        raise ImageError("无可生图渠道")
    pending = [j for j in jobs if force or not j.is_done()]
    if not pending:
        return {j.id: str(j.out) for j in jobs}
    # Same request, one bill: jobs with an identical request_fingerprint would
    # return byte-identical images. Generate once, copy to the rest. The copies
    # go through ImageJob.install (same receipt/sha/validation path) and never
    # touch the provider, so they consume no budget and write no cost ledger.
    primaries: list = []
    dup_of: dict = {}
    _fp_seen: dict = {}
    for j in pending:
        try:
            fp = j.request_fingerprint()
        except Exception:
            fp = None
        if fp is None or fp not in _fp_seen:
            if fp is not None:
                _fp_seen[fp] = j.id
            primaries.append(j)
        else:
            dup_of.setdefault(_fp_seen[fp], []).append(j)
    if dup_of:
        print(f"  [image] 去重 {sum(len(v) for v in dup_of.values())} 个重复请求 "
              f"({len(dup_of)} 组)，只付一次钱", flush=True)
    unsupported = sorted({j.req.model for j in primaries
                          if not any(c.supports(j.req.model) for c in channels)})
    if unsupported:
        raise ImageError(f"无可用渠道支持模型: {', '.join(unsupported)}；整批未发起请求")
    # Keep cooling suppliers in the batch so they can recover while it runs.
    channels = [c for c in channels if any(c.supports(j.req.model) for j in primaries)]
    capacity = sum(c.max_concurrency for c in channels)
    if workers is not None and workers < 1:
        raise ValueError("workers must be positive")
    n_worker = min(workers or capacity, capacity, len(primaries))
    jobq = _SupplierQueue(primaries, channels, gen)
    result: dict = {}
    prog = _Progress(total=len(jobs), callback=on_progress)
    # Jobs already done never enter the queue; record and count them here so
    # progress totals and result shape match the pre-dedup behavior.
    _pending_ids = {id(j) for j in pending}
    for j in jobs:
        if id(j) not in _pending_ids:
            result[j.id] = str(j.out)
            prog.tick(ok=True)
    stop = threading.Event()
    mon = None
    if progress and len(jobs) > 1:
        mon = threading.Thread(target=_progress_monitor, args=(prog, stop),
                               daemon=True)
        mon.start()
    threads = [
        threading.Thread(
            target=_generate_many_worker,
            args=(gen, jobq, result, channels, force, prog, budget, provider_attempts_per_job),
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
    # Fan out byte-identical results to the deduplicated jobs. Same install()
    # path (receipt/sha/validation/lock), zero provider calls, zero budget.
    for primary_id, dups in dup_of.items():
        primary_out = result.get(primary_id)
        for dup in dups:
            if not primary_out:
                result[dup.id] = None
                result[f"{dup.id}__error"] = (
                    result.get(f"{primary_id}__error", "unknown error")
                    + "（重复请求，主任务失败）")
                prog.tick(ok=False)
                continue
            try:
                raw = Path(primary_out).read_bytes()
                dup.install(raw, dup.request_fingerprint())
                result[dup.id] = str(dup.out)
                prog.tick(ok=True)
            except Exception as exc:
                result[dup.id] = None
                result[f"{dup.id}__error"] = f"重复请求落盘失败: {type(exc).__name__}: {exc}"
                prog.tick(ok=False)
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


def record_cost(*, job: ImageJob, channel: str, n_bytes: int, response_path=None, link=None) -> dict:
    """Idempotent installed-image accounting. Attempts remain in ProviderBudget."""
    from app.services.provenance import atomic_json, file_sha256
    from app.services.narration_pipeline import read
    from app.services.provider_budget import ledger_lock
    entry = {}
    try:
        response_path = Path(response_path or job.out.with_suffix(job.out.suffix+'.response.json'))
        saved = read(response_path) if response_path.is_file() else {}
        receipt_sha = file_sha256(response_path) if response_path.is_file() else None
        entry = dict(ts=datetime.now().isoformat(timespec='seconds'), job=job.id, channel=channel,
                     model=job.req.model, size=job.req.size, quality=job.req.quality,
                     bytes=int(n_bytes), attempts=job._attempts, transient_retries=job._transient,
                     out=str(job.out), request_fingerprint=job.request_fingerprint(),
                     response_sha256=receipt_sha, attempt_id=saved.get('attempt_id'),
                     budget_path=saved.get('budget_path'), recovered=job._attempts == 0)
        if link: entry.update(link)
        unit = _price_of(entry['model'] or '', entry['quality'] or '')
        entry['cost'] = round(unit, 6) if unit is not None else None
        with _COST_LOCK, ledger_lock(_COST_PATH):
            rows = read(_COST_PATH) if _COST_PATH.is_file() else []
            if not isinstance(rows, list): raise ValueError('cost ledger is not a list')
            prior = next((r for r in rows if
                          (receipt_sha and r.get('response_sha256') == receipt_sha) or
                          (not receipt_sha and not r.get('response_sha256') and r.get('out') == str(job.out) and
                           r.get('request_fingerprint') == entry['request_fingerprint'])), None)
            if prior is not None:
                if entry['attempt_id'] and not prior.get('attempt_id'):
                    prior.update(attempt_id=entry['attempt_id'], budget_path=entry['budget_path'])
                    atomic_json(_COST_PATH, rows)
                return prior
            rows.append(entry)
            atomic_json(_COST_PATH, rows[-_COST_MAX:])
    except Exception as exc:
        _record_error(_COST_PATH.with_suffix('.error.json'), 'cost_accounting', exc)
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
