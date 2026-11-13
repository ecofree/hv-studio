"""付费/写端点的 token 闸（整改 B1，审计 P0-1）。

背景
----
`/v1/image/test` 直接付费生图、`/v1/episode/{id}/produce?stage=assets` 可触发
TTS + 生图双付费、`/init` 会无条件覆盖已有 episode.json —— 三者此前零鉴权，
唯一防线是 `--host` 默认绑 127.0.0.1。一旦有人用 `--host 0.0.0.0`（容器/局域网/
公网）就是裸奔：任何人都能刷你的生图配额。

设计：opt-in，不给自己找麻烦
----------------------------
- **未配置 `HV_API_TOKEN` → 仅允许回环客户端**，远程写入请求一律拒绝。
- 配置后，所有写/付费端点（包括本机请求）都必须带 `X-HV-Token` 头。
- 另在 CLI 启动侧加硬闸：`--host` 非回环且未配 token 时**拒绝启动**
  （见 app/cli.py cmd_api）——堵住「绑了 0.0.0.0 却忘了配 token」这条最可能的路。

为什么不用 OAuth / API-Key 中间件全家桶
--------------------------------------
这是本地单人流水线，威胁模型只有一条：服务被绑到非回环地址后被人扫到。
一个共享静态 token + 常量时间比较，再加未配置时的回环客户端限制，就能覆盖本地单用户威胁模型。
真要放到公网应当走反向代理（nginx/caddy）做鉴权与 TLS，本闸只是最后一道兜底。
"""
from __future__ import annotations

import hmac
import ipaddress
from typing import Optional

from fastapi import Header, HTTPException, Request

from app.config.config import env_get

ENV_TOKEN = "HV_API_TOKEN"
HEADER_NAME = "X-HV-Token"


def configured_token() -> str:
    """当前生效的 token；空串代表「未启用鉴权」。"""
    # I 批（2026-09-05）：改走 env_get —— 此前直读 os.environ，token 写进
    # .env 是不生效的（.env 是本仓推荐的本地配置落点，不入库）。
    # 语义：.env 优先，真实环境变量兜底。
    return (env_get(ENV_TOKEN) or "").strip()


def auth_enabled() -> bool:
    return bool(configured_token())


def require_token(
    request: Request,
    x_hv_token: Optional[str] = Header(default=None, alias=HEADER_NAME),
) -> None:
    """Protect every write/provider endpoint.

    A configured token is always required, including for loopback callers.
    Without a token, only a loopback client is allowed. This request-level
    fallback also protects direct Uvicorn launches that bypass the CLI guard.
    """
    expected = configured_token()
    if expected:
        if not x_hv_token or not hmac.compare_digest(x_hv_token.strip(), expected):
            raise HTTPException(
                status_code=401,
                detail=f"缺少或无效的 {HEADER_NAME}（服务端已启用 HV_API_TOKEN）",
            )
        return

    client_host = request.client.host if request.client else ""
    if not is_loopback_host(client_host):
        raise HTTPException(
            status_code=401,
            detail=f"远程写入请求必须配置 HV_API_TOKEN 并提供 {HEADER_NAME}",
        )


def is_loopback_host(host: str) -> bool:
    """Recognize IPv4/IPv6 loopback addresses; wildcard binds are never loopback."""
    value = (host or "").strip().strip("[]").lower()
    if value in ("localhost", "testclient"):
        return True
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return address.is_loopback
