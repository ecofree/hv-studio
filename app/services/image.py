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
    def generate(self, req: ImageGenRequest) -> ImageGenResponse:
        req.validate_for_subrouter()
        return self._call_with_failover(
            path="/v1/images/generations",
            body=req.model_dump(exclude_none=True),
        )

    def edit(self, req: ImageEditRequest) -> ImageGenResponse:
        req.validate_for_subrouter()
        return self._call_with_failover(
            path="/v1/images/edits",
            body=req.model_dump(exclude_none=True),
        )

    # ---- 内部 ----
    def _call_with_failover(self, path: str, body: dict) -> ImageGenResponse:
        """依次尝试每个渠道，直到成功或全部失败。"""
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
# 工具
# ---------------------------------------------------------------------------
def save_b64_image(b64: str, path: str | Path) -> Path:
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
