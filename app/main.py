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
    # Startup is local and bounded. Provider quota checks belong to an explicit
    # operator action, so provider downtime cannot stall health or recovery.
    # 安全提示：token 闸是 opt-in（未配 HV_API_TOKEN 即不校验）。直连 uvicorn
    # 启动时没有 cli.py 那道「非回环 host 必须有 token」的硬闸，这里补一句提醒。
    try:
        from app.controllers.v1.auth import auth_enabled
        if not auth_enabled():
            print("[api] 提示：未设 HV_API_TOKEN，写/付费端点（/v1/image/test、"
                  "/produce?stage=assets）不校验；监听非回环地址前请先配置。")
    except Exception:
        pass
    yield


app = FastAPI(
    title="hv — 历史解说短视频流水线",
    description="历史解说短视频生产流水线",
    version="0.1.0",
    lifespan=lifespan,
)

from app.paths import InvalidEpisodeId
from app.services.provider_budget import BudgetExceeded
from fastapi.responses import JSONResponse


@app.exception_handler(InvalidEpisodeId)
async def invalid_episode_id(request, exc):
    return JSONResponse(status_code=422, content={'detail': str(exc)})


@app.exception_handler(BudgetExceeded)
async def missing_budget(request, exc):
    return JSONResponse(status_code=409, content={'detail': str(exc)})

# CORS 收紧（审计 P1）：本服务含可触达付费生图的端点，默认只允许本地回环来源，
# 不再用 allow_origins=["*"]（配合 allow_credentials=True 是危险组合）。
# 生产部署需显式经 env HV_CORS_ORIGINS 白名单放开，并由反向代理做鉴权。
import os

from app.config.config import env_get

# I 批（2026-09-05）：改走 env_get（.env 优先、真实环境变量兜底）。
# 此前直读 os.environ，这个键写进 .env 是无效的。
_cors_origins = [o.strip() for o in env_get("HV_CORS_ORIGINS", "").split(",") if o.strip()] \
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
