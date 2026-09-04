"""Video 路由（当前仅暴露 relay 状态；M1 接入 I2V 任务查询）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.managers import get_relay

router = APIRouter()


@router.get("/relay/status")
def relay_status():
    return {
        "channels": get_relay().snapshot(),
        "n_channels": len(get_relay().all()),
    }


@router.post("/relay/sync")
def relay_sync():
    get_relay().sync_quota(force=True)
    return {"synced": True, "channels": get_relay().snapshot()}
