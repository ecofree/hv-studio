"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.controllers.v1 import episode as episode_ctrl
from app.controllers.v1 import image as image_ctrl
from app.controllers.v1 import video as video_ctrl
from app.managers.relay import get_relay


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时同步一次配额（不阻塞，慢也无妨）
    try:
        get_relay().sync_quota()
    except Exception:
        pass
    yield


app = FastAPI(
    title="hv — 历史解说短视频流水线",
    description="A 股数据中枢的史官：抖音历史解说短视频生产流水线",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 收紧（审计 P1）：本服务含可触达付费生图的端点，默认只允许本地回环来源，
# 不再用 allow_origins=["*"]（配合 allow_credentials=True 是危险组合）。
# 生产部署需显式经 env HV_CORS_ORIGINS 白名单放开，并由反向代理做鉴权。
import os
_cors_origins = [o.strip() for o in os.environ.get("HV_CORS_ORIGINS", "").split(",") if o.strip()] \
    or ["http://127.0.0.1", "http://localhost", "http://127.0.0.1:8501", "http://localhost:8501"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(episode_ctrl.router, prefix="/v1/episode", tags=["episode"])
app.include_router(image_ctrl.router, prefix="/v1/image", tags=["image"])
app.include_router(video_ctrl.router, prefix="/v1/video", tags=["video"])


@app.get("/")
def root():
    return {
        "service": "hv",
        "version": app.version,
        "endpoints": [
            "GET  /v1/episode/{id}",
            "POST /v1/episode/{id}/run?stage=...",
            "POST /v1/image/test",
            "GET  /v1/video/relay/status",
        ],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}
