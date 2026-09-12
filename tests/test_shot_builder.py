# -*- coding: utf-8 -*-
"""通用「剧本→分镜」shot_builder 验收。

对应 B 项：声明式 scene_plan → 自动装配 shots，替代每章手写 bin/*_shots.py。
验证：合法 plan 产出与手写一致的帧→底图/grade/运镜映射；非法 plan 被硬拒绝。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import json


from app.managers.episode import episode_path
from app.services import shot_builder as SB


@pytest.fixture
def tiny_ep(tmp_path, tmp_episodes):
    """构造一个 2-frame 一次性 episode（写在 tmp 的 episodes 根下，P1-6）。

    真实底图依赖用 make_reuse_source 在 tmp 根里造同名占位图解决，
    因此完全不碰 D:/hv/episodes/。
    """
    from conftest import make_reuse_source
    make_reuse_source(tmp_episodes, "hw-002", "SC01")
    ep_id = f"sbguard-{tmp_path.name}"
    d = {
        "id": ep_id, "title": ep_id, "episode_number": 1, "schema_version": "1.0",
        "status": "draft",
        "frames": [
            {"id": "frame-000", "text": "测试一", "narration_text": "测试一",
             "audio_duration_ms": 2000},
            {"id": "frame-001", "text": "测试二", "narration_text": "测试二",
             "audio_duration_ms": 2000},
        ],
        "shots": {}, "state": {},
    }
    episode_path(ep_id).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    yield ep_id
    # 无清理：tmp_episodes 重定向后 json 也落在 tmp 下


def _plan(ep_id, image="SC01", grades=("dawn", "noon"), frames=(0, 1)):
    # images 键 = 底图文件名（reuse_from 用 {key}.png 定位）
    return {
        "images": {image: {"reuse_from": "hw-002"}},
        "scenes": [{"frames": list(frames), "image": image, "grades": list(grades)}],
    }


def _try(ep_id, plan, tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        SB.build_episode_shots(ep_id, plan_path=p, persist=False, preview_rows=0)
        return None
    except SystemExit as e:
        return str(e)


def test_gap_rejected(tiny_ep, tmp_path):
    e = _try(tiny_ep, {"images": {"SC01": {"reuse_from": "hw-002"}},
                       "scenes": [{"frames": [0, 0], "image": "SC01", "grades": ["dawn"]}]}, tmp_path)
    assert e and "未落任何" in e


def test_duplicate_rejected(tiny_ep, tmp_path):
    plan = {"images": {"SC01": {"reuse_from": "hw-002"}},
            "scenes": [{"frames": [0, 1], "image": "SC01", "grades": ["dawn"]},
                       {"frames": [0, 1], "image": "SC01", "grades": ["noon"]}]}
    e = _try(tiny_ep, plan, tmp_path)
    assert e and "重复覆盖" in e


def test_bad_grade_rejected(tiny_ep, tmp_path):
    e = _try(tiny_ep, _plan(tiny_ep, grades=("lava",)), tmp_path)
    assert e and "非法 grade" in e


def test_oor_rejected(tiny_ep, tmp_path):
    e = _try(tiny_ep, _plan(tiny_ep, frames=(0, 5)), tmp_path)
    assert e and "越界" in e


def test_valid_plan_builds_shots(tiny_ep, tmp_path):
    """合法 plan 能成功装配 2 镜，且无 SystemExit。"""
    # reuse_from: hw-002 的底图由 tiny_ep 在 tmp 根里造好（不再依赖真实集）
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(_plan(tiny_ep), ensure_ascii=False), encoding="utf-8")
    ep, stats = SB.build_episode_shots(tiny_ep, plan_path=p, persist=False, preview_rows=0)
    assert stats["shots"] == 2 and stats["base_images"] == 1
    # 每镜有有效底图路径、subtitle 取自 frame.text
    assert all(sh.image_path.endswith("SC01.png") for sh in ep.shots.values())
    assert all(sh.subtitle_text for sh in ep.shots.values())
