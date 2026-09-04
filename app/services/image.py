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
from pathlib import Path

import requests

from app.config.config import Channel
from app.managers.relay import get_relay
from app.models import ImageEditRequest, ImageGenRequest, ImageGenResponse


class ImageError(RuntimeError):
    pass


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
        r = requests.post(
            url,
            headers={**ch.auth_header, "Content-Type": "application/json"},
            json=body,
            timeout=300,
        )
        if r.status_code != 200:
            # 解析 subrouter 的两类错误 schema
            try:
                err = r.json()
                # 图像端 schema
                if "error" in err and isinstance(err["error"], dict):
                    raise ImageError(
                        f"[{ch.name}] HTTP {r.status_code}: "
                        f"{err['error'].get('message', r.text[:200])}"
                    )
                # 视频端 schema
                if "code" in err and "message" in err:
                    raise ImageError(
                        f"[{ch.name}] HTTP {r.status_code}: "
                        f"{err.get('code')}: {err.get('message')}"
                    )
            except ValueError:
                pass
            raise ImageError(f"[{ch.name}] HTTP {r.status_code}: {r.text[:200]}")
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
_QPS_MAX = float(os.environ.get("HV_IMAGE_QPS_MAX", "2"))
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


@dataclass
class ImageJob:
    """一次生图任务。out_path 为期望落盘 PNG/JPG 路径。

    并发安全：workers 从 jobq 取任务；result 以 job.id 回填。
    """

    id: str
    req: "ImageGenRequest"
    out_path: str | Path

    # worker 内部用
    _channel_idx: int | None = field(default=None, repr=False)
    _attempts: int = field(default=0, repr=False)
    _last_err: str | None = field(default=None, repr=False)

    @property
    def out(self) -> Path:
        return Path(self.out_path)

    def is_done(self, min_bytes: int = 50 * 1024) -> bool:
        """out_path 已存在且 > min_bytes → 幂等跳过。"""
        try:
            return self.out.exists() and self.out.stat().st_size > min_bytes
        except OSError:
            return False


def _generate_many_worker(
    gen: ImageGenerator,
    jobq: Queue,
    result: dict,
    channels: list[Channel],
    force: bool,
) -> None:
    """单 worker：领一个固定渠道，串行消化队列任务。

    每渠道一个 worker 是三路并行的关键：若所有 worker 都走 failover，
    会全部命中 score 最高的同一渠道，退化成单路并把它打 429。
    """
    while True:
        try:
            job: ImageJob = jobq.get_nowait()
        except Empty:
            return
        try:
            # 幂等：已存在合格文件直接跳过（不占渠道）
            if not force and job.is_done():
                result[job.id] = str(job.out)
                continue
            # 领渠道：按 job._channel_idx 轮转首攻，失败再按序换路
            order = list(range(len(channels)))
            if job._channel_idx is not None:
                # 从 worker 钉住的渠道开始
                order = order[job._channel_idx:] + order[:job._channel_idx]
            ok = False
            for idx in order:
                job._attempts += 1
                try:
                    _throttle()
                    resp = gen.generate(job.req, channel=channels[idx])
                    # 取首个 base64 / url
                    item = resp.data[0] if resp.data else None
                    if item is None:
                        raise ImageError(f"[{channels[idx].name}] 空响应 data")
                    raw = None
                    if getattr(item, "b64_json", None):
                        raw = base64.b64decode(item.b64_json)
                    elif getattr(item, "url", None):
                        raw = _download(item.url, channels[idx].name)
                    if not raw:
                        raise ImageError(f"[{channels[idx].name}] 无图像数据")
                    job.out.parent.mkdir(parents=True, exist_ok=True)
                    job.out.write_bytes(raw)
                    result[job.id] = str(job.out)
                    ok = True
                    break
                except Exception as e:
                    job._last_err = f"{channels[idx].name}: {type(e).__name__} {str(e)[:120]}"
                    # 换路前退避（同一渠道连续 429 时不狂转）
                    import time as _t
                    _t.sleep(_backoff(job._attempts))
            if not ok:
                result[job.id] = None
                result[f"{job.id}__error"] = job._last_err or "未知错误"
        finally:
            jobq.task_done()


def _download(url: str, ch_name: str) -> bytes:
    """从 url 下载图像字节（部分中转返回图片 url 而非 b64）。"""
    import requests as _rq
    r = _rq.get(url, timeout=120)
    if r.status_code != 200:
        raise ImageError(f"[{ch_name}] 下载图失败 HTTP {r.status_code}")
    return r.content


def generate_many(
    jobs: list[ImageJob],
    force: bool = False,
    workers: int | None = None,
) -> dict:
    """多任务并行生图（三路 worker）。返回 {job.id: 落盘路径 或 None}。

    - workers 默认 = min(渠道数, 任务数)；每渠道各钉一路
    - 已存在且 >50KB 的任务幂等跳过（不占渠道）
    - 单任务换路 ≤ 渠道数；全渠道失败回填 None，已成功文件保留
    - 任一侧 429/失败只影响该任务，不堵死其他两路

    不 raise（部分失败也要把已成功的产物带回来）；调用方据 None 汇总。
    """
    if not jobs:
        return {}
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
    threads = [
        threading.Thread(
            target=_generate_many_worker,
            args=(gen, jobq, result, channels, force),
            daemon=True,
        )
        for _ in range(n_worker)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result



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
