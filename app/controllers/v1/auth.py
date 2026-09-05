"""付费/写端点的 token 闸（整改 B1，审计 P0-1）。

背景
----
`/v1/image/test` 直接付费生图、`/v1/episode/{id}/produce?stage=assets` 可触发
TTS + 生图双付费、`/init` 会无条件覆盖已有 episode.json —— 三者此前零鉴权，
唯一防线是 `--host` 默认绑 127.0.0.1。一旦有人用 `--host 0.0.0.0`（容器/局域网/
公网）就是裸奔：任何人都能刷你的生图配额。

设计：opt-in，不给自己找麻烦
----------------------------
- **未配置 `HV_API_TOKEN` → 完全不校验**，本地 127.0.0.1 开发零负担、零破坏。
- 配了才启用；此时所有写/付费端点必须带 `X-HV-Token` 头。
- 另在 CLI 启动侧加硬闸：`--host` 非回环且未配 token 时**拒绝启动**
  （见 app/cli.py cmd_api）——堵住「绑了 0.0.0.0 却忘了配 token」这条最可能的路。

为什么不用 OAuth / API-Key 中间件全家桶
--------------------------------------
这是本地单人流水线，威胁模型只有一条：服务被绑到非回环地址后被人扫到。
一个共享静态 token + 常量时间比较就够，引入依赖反而增加维护面。
真要放到公网应当走反向代理（nginx/caddy）做鉴权与 TLS，本闸只是最后一道兜底。
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException

ENV_TOKEN = "HV_API_TOKEN"
HEADER_NAME = "X-HV-Token"


def configured_token() -> str:
    """当前生效的 token；空串代表「未启用鉴权」。"""
    return (os.environ.get(ENV_TOKEN) or "").strip()


def auth_enabled() -> bool:
    return bool(configured_token())


def require_token(
    x_hv_token: Optional[str] = Header(default=None, alias=HEADER_NAME),
) -> None:
    """FastAPI 依赖：token 未配置时放行；配置后校验 X-HV-Token。

    用 hmac.compare_digest 而非 `==`：后者短路返回会泄露「前几位对了多少」，
    理论上可被计时攻击逐字符爆破。
    """
    expected = configured_token()
    if not expected:
        return
    if not x_hv_token or not hmac.compare_digest(x_hv_token.strip(), expected):
        raise HTTPException(
            status_code=401,
            detail=f"缺少或无效的 {HEADER_NAME}（服务端已启用 HV_API_TOKEN）",
        )


def is_loopback_host(host: str) -> bool:
    """是否只监听本机（含 IPv6 回环与「全零」通配的本地语义）。

    注意 0.0.0.0 / :: **不是**回环——它是「所有网卡」，正是要拦的那种。
    """
    h = (host or "").strip().strip("[]").lower()
    return h in ("127.0.0.1", "localhost", "::1", "")
