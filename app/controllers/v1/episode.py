"""Episode REST 路由（查询/初始化/真实生产编排）。

P0-3 修复：此前的 /run 直接调 run_stage(ep) 且不传真实执行函数，状态机仍逐级
空推到 done —— 可伪造完成。现在 /run 与 /produce 都走 app.orchestrator.produce
（真实 script/tts/compose/verify），状态只由产物真值导出；无产物即失败不推进。
run_stage 也不再允许空 stage_fn 推进（managers/episode.py 已硬拦）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.controllers.v1.auth import require_token
from app.managers import load, save
from app.managers.episode import episode_path

router = APIRouter()


@router.get("/{episode_id}")
def get_episode(episode_id: str):
    ep = load(episode_id)
    return ep.model_dump(mode="json")


@router.post("/{episode_id}/init")
def init_episode(episode_id: str, _tok=Depends(require_token)):
    """新建 episode。已存在 → 409，绝不静默覆盖。

    旧行为是 `Episode(...)` 后无条件 `save()`：一个 POST 就能把已有集的全部
    frames/shots/状态清空，且服务端只回 200 {"created": ...}，调用方毫无察觉。
    要「重置」请显式先删 episode.json。
    """
    from app.models import Episode
    if episode_path(episode_id).exists():
        raise HTTPException(
            status_code=409,
            detail=f"episode {episode_id} 已存在（{episode_path(episode_id)}）；"
                   f"init 不会覆盖，如需重置请先删除该 json")
    ep = Episode(id=episode_id, title=episode_id, episode_number=0)
    p = save(ep)
    return {"created": str(p)}


@router.post("/{episode_id}/produce")
def produce_episode(episode_id: str,
                    stage: list[str] | None = Query(default=None),
                    script_md: str | None = Query(default=None),
                    _tok=Depends(require_token)):
    """真实生产链（默认 script→tts→compose→verify 适用项）。状态由产物真值导出。

    ?stage=assets 会同时跑 TTS 与付费生图 → 挂 token 闸（见 auth.require_token）。
    stage 可传多次，如 ?stage=script&stage=verify。
    """
    from app.orchestrator import produce
    try:
        res = produce(episode_id, stages=stage, script_md=script_md)
    except Exception as e:
        raise HTTPException(400, str(e))
    return res


# 兼容旧客户端：/run 不再空推状态，改走真实编排（verify 只读，默认安全不触发付费）
@router.post("/{episode_id}/run")
def run_episode_stage(episode_id: str,
                      stage: list[str] | None = Query(default=None),
                      _tok=Depends(require_token)):
    """推进真实生产。默认只跑只读的 verify（验证产物真值），不自动触发付费生图。
    如需 script/tts/compose 请显式 ?stage=...（显式即可能付费 → 同样挂 token 闸）。
    """
    from app.orchestrator import produce
    try:
        # 默认 verify 只读校验（无害）；显式 stage 才做真实生产
        res = produce(episode_id, stages=stage or ["verify"])
    except Exception as e:
        raise HTTPException(400, str(e))
    return res
