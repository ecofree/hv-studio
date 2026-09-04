"""
subrouter.ai（中转）的请求/响应模型。

重要：
  1. subrouter 会**静默忽略非法 size/quality** 并真去生成
     （已计费 $0.012）—— 请求时严格按 ImageGenRequest 校验字段值。
  2. 错误响应有**两套 schema**：
     - 图像端：OpenAI 风格 {error: {message, type, code}}
     - 视频端：{code, message, data}
  3. size 仅控制长宽比，**总像素固定 1,572,864 = 1024×1536**。
     返回图实际像素由 predict_actual_dims() 算。
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field


# 来自实测：subrouter 图像端实际产出固定 1,572,864 像素
# (= 1024 × 1536)，按请求 size 的长宽比分配
SUBROUTER_PIXEL_CAP = 1024 * 1536  # 1,572,864


# ---------------------------------------------------------------------------
# 生图
# ---------------------------------------------------------------------------
class ImageGenRequest(BaseModel):
    """POST /v1/images/generations 请求体。"""
    model: str
    prompt: str
    size: str = "1080x1920"
    quality: str = "low"
    n: int = 1

    def validate_for_subrouter(self) -> None:
        """
        ★ 严格校验：subrouter 会**静默忽略**非法 size/quality 并真去生成。
        本地校验是规避这个陷阱的最后一关。
        """
        allowed_sizes = {
            # 实测确认的 size 集合
            "1024x1024", "1024x1536", "1024x1792", "1536x1024",
            "1024x1856", "1856x1024", "1536x2048", "2048x1536",
            # 推荐（9:16 精确）
            "1080x1920",
        }
        if self.size not in allowed_sizes:
            raise ValueError(
                f"size={self.size!r} 风险：subrouter 可能静默忽略并真去生成。"
                f"白名单: {sorted(allowed_sizes)}"
            )
        if self.quality not in ("low", "medium", "high", "auto"):
            raise ValueError(f"quality={self.quality!r} 不在 low/medium/high/auto")


class ImageEditRequest(BaseModel):
    """POST /v1/images/edits 请求体（参考图图生图）。

    ★ subrouter schema（盲探钉死，勿改）：
       - 顶层字段是 `image`（不是 image_b64，也不是 images 数组）
       - `image` 传单张参考图的 base64 字符串 → HTTP 200 真实生成
       - images 数组、image_b64 字段 subrouter 不认识 → 400
    多图参考（多张定妆照）暂未确认，M2 先用单锚图。
    """
    model: str
    prompt: str
    # 参考图 base64（单张）。模型序列化时输出为顶层 `image`
    image: str | None = None
    size: str = "1080x1920"
    quality: str = "low"
    n: int = 1

    def validate_for_subrouter(self) -> None:
        ImageGenRequest(
            model=self.model, prompt=self.prompt,
            size=self.size, quality=self.quality, n=self.n,
        ).validate_for_subrouter()


class ImageGenItem(BaseModel):
    b64_json: str | None = None
    url: str | None = None
    revised_prompt: str | None = None

    def has_image(self) -> bool:
        return self.b64_json is not None or self.url is not None


class ImageGenResponse(BaseModel):
    created: int = 0
    data: list[ImageGenItem] = Field(default_factory=list)
    usage: dict | None = None


# ---------------------------------------------------------------------------
# 视频
# ---------------------------------------------------------------------------
class VideoTaskRequest(BaseModel):
    """
    POST /v1/videos 请求体。

    ★ image_field 待 T2 探测确定：subrouter 视频端的 image 字段命名
      未知（可能 image / image_url / image_urls / file）。先用 image_b64
      试一次（与生图一致），失败时回退 image_url。
    """
    model: str
    prompt: str
    seconds: int = 5
    image_b64: str | None = None
    image_b64_list: list[str] | None = None
    image_url: str | None = None


class VideoTaskResponse(BaseModel):
    """POST /v1/videos 响应（任务制）。"""
    id: str
    status: str = "queued"
    url: str | None = None
    error: str | None = None


class VideoTaskStatus(BaseModel):
    """GET /v1/videos/{id} 响应。"""
    id: str
    status: str                          # queued | processing | completed | failed
    progress: float | None = None
    url: str | None = None               # 完成后的视频 URL
    error: str | None = None
    duration_sec: int | None = None


# ---------------------------------------------------------------------------
# 配额 / 健康度
# ---------------------------------------------------------------------------
class QuotaSnapshot(BaseModel):
    """GET /v1/dashboard/billing/subscription 单 key 响应。"""
    has_payment_method: bool
    soft_limit_usd: float
    hard_limit_usd: float
    system_hard_limit_usd: float
    access_until: int
    channel: str                         # 渠道名（aicost / qcyc / cxk）
    fetched_at: datetime = Field(default_factory=datetime.now)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.soft_limit_usd - 0.0)  # 当前仅暴露上限，不暴露余额


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def predict_actual_dims(requested_size: str) -> tuple[int, int]:
    """
    预测 subrouter 返回图的真实像素。
    实测验证：1024x1024→1254x1254, 1024x1536→1024x1536, 1024x1792→948x1659
    """
    w, h = requested_size.split("x")
    w, h = int(w), int(h)
    ratio = w / h
    h_actual = int(math.sqrt(SUBROUTER_PIXEL_CAP / ratio))
    w_actual = int(ratio * h_actual)
    return w_actual, h_actual
