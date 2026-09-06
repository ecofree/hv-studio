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

import json
from datetime import datetime

from pydantic import BaseModel, Field


# H 批（2026-09-05）：SUBROUTER_PIXEL_CAP 搬到 app/services/spec.py 了
# （此前与 config.py 的 MP_FIXED 互为副本）。这里**不** re-export：全仓无消费者。
#
# 顺带记一个曾经踩到的坑：早先在这里 `from app.services.spec import ...` 会
# 直接循环导入——因为 app/services/__init__.py 顶层 import 了 compose，而
# compose 又 import app.managers，本模块正处在 managers 的初始化链上。
# 现在 services/__init__ 已改成惰性导入（PEP 562），从 models 侧引 spec 是安
# 全的；但既然没消费者，就不必多这一条 import。


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


# D2（2026-09-05）：VideoTask{Request,Response,Status} 已删。三个类零调用点——
# 项目里从来没有 services/video.py，config 的 video 段只被 `hv config` 打印出来。
# 它们是「打算做 I2V」时先写的 schema，留着会让人以为链路已经通了一半。
# 真要做 I2V 时从 git 历史里捞回来即可（本 commit 前一直存在）。


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

    H 批（2026-09-05）：公式与常量搬到 app/services/spec.py（此前这里与
    config.predict_actual_image_dims 各有一份 1024*1536 和一份照抄的公式）。
    本函数保留名字作为 "WxH" 字符串入口。
    """
    from app.services.spec import predict_actual_dims_of
    return predict_actual_dims_of(requested_size)
