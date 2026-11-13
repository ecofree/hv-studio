"""Image-provider request models and observed quota records."""
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
        """Use the same preflight rules at the actual dispatch boundary."""
        from app.services.image import validate_image_params
        problems = validate_image_params(self.size, self.quality, self.model)
        if self.n != 1:
            problems.append('one image per request is required for budget accounting')
        if problems:
            raise ValueError('; '.join(problems))


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


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
