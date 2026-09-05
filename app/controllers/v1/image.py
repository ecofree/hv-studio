"""Image 调试路由（POST /v1/image/test）。"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.controllers.v1.auth import require_token
from app.models import ImageGenRequest, predict_actual_dims
from app.services.image import get_image, save_b64_image

router = APIRouter()


class TestRequest(BaseModel):
    prompt: str = "西汉戈壁黄昏，电影感，写实"
    size: str = "1080x1920"
    quality: str = "low"


@router.post("/test")
def test(req: TestRequest, _tok=Depends(require_token)):
    """真实付费生图（单次调用即扣配额）——故挂 token 闸（见 auth.require_token）。"""
    img = get_image()
    payload = ImageGenRequest(
        model="gpt-image-2", prompt=req.prompt,
        size=req.size, quality=req.quality,
    )
    w, h = predict_actual_dims(req.size)
    t0 = time.time()
    resp = img.generate(payload)
    dt = time.time() - t0
    item = resp.data[0]
    out = None
    if item.b64_json:
        out = f"assets/probe/api_{int(time.time())}.png"
        save_b64_image(item.b64_json, out)
    return {
        "latency_s": round(dt, 2),
        "requested_size": req.size,
        "actual_size": [w, h],
        "revised_prompt": (item.revised_prompt or "")[:200],
        "saved_to": out,
    }
