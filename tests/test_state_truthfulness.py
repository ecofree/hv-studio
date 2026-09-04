# -*- coding: utf-8 -*-
"""P0-3 / P0-4 验收：状态必须由真实产物驱动；schema 损坏必须硬阻断。

P0-3：API / run_stage 不得空推状态到 DONE（无产物）。
P0-4：schema 不干净（如 shot 引用缺失 frame）→ 生产前拒绝，绝不付费生图。
"""
from __future__ import annotations

import pytest


def test_run_stage_requires_real_stage_fn():
    """run_stage 不带 stage_fn 必须拒绝推进（否则是虚假完成）。"""
    from app.models import Episode
    from app.managers import run_stage
    ep = Episode(id="tmpfake", title="tmpfake", episode_number=0)
    with pytest.raises(Exception):
        run_stage(ep)  # 无真实执行 → 应 raise，不得空推


def test_validate_schema_detects_dangling_shot_ref():
    """构造 shot.frame_id 不在 frames 的 episode → schema 必须报出。"""
    from app.models import Episode, Shot, Frame
    ep = Episode(id="bad", title="bad", episode_number=0)
    ep.frames = [Frame(id="frame-000", text="a")]
    ep.shots = {
        "shot-000": Shot(
            id="shot-000", frame_id="frame-999", index=0,  # 引用了不存在的 frame
            duration_ms=1000, audio_start_ms=0, audio_end_ms=1000,
            prompt="p", image_path="x.png",
        ),
    }
    from app.managers.episode import validate_schema
    problems = validate_schema(ep)
    assert any("shot-000" in pr and "frame-999" in pr for pr in problems), problems


def test_produce_verify_derives_true_status():
    """verify 只读，应在真实产物上导出 COMPOSED（而非盲迁移）。"""
    from app.orchestrator import produce
    # hanwu-prologue 是已交付小集（有 mp4），verify-only 不重编码
    res = produce("hanwu-prologue", stages=["verify"])
    assert res["status"] == "COMPOSED", res
    assert "verify" in res["stages_done"]
    assert res.get("final_mp4"), "verify 应返回 final_mp4 路径"
