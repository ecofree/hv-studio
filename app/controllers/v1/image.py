"""Image 调试路由（POST /v1/image/test）。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.controllers.v1.auth import require_token
from app.models import ImageGenRequest

router = APIRouter()


class TestRequest(BaseModel):
    prompt: str = "西汉戈壁黄昏，电影感，写实"
    size: str = "1080x1920"
    quality: str = "low"
    model: str = 'gpt-image-2'
    budget_id: str = Field(min_length=1, max_length=100)
    max_images: int = Field(default=0, ge=0)


@router.post("/test")
def test(req: TestRequest, _tok=Depends(require_token)):
    """真实付费生图（单次调用即扣配额）——故挂 token 闸（见 auth.require_token）。"""
    payload = ImageGenRequest(
        model=req.model, prompt=req.prompt,
        size=req.size, quality=req.quality,
    )
    t0 = time.time()
    from app.services.image import run_probe
    try:
        out = run_probe(payload, req.budget_id, req.max_images)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc))
    dt = time.time() - t0
    from PIL import Image
    with Image.open(out) as image:
        w, h = image.size
    return {
        "latency_s": round(dt, 2),
        "requested_size": req.size,
        "actual_size": [w, h],
        "saved_to": str(out),
    }
