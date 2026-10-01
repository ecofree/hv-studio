# -*- coding: utf-8 -*-
"""Regression test: rebuilding the storyboard must not fake a render failure."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.production

from app.models import Episode, Frame, MotionSpec, Shot, ShotType


def _shot(sid="s0"):
    return Shot(id=sid, frame_id="frame-000", index=0, type=ShotType.KEN_BURNS,
                prompt="p", duration_ms=3000, audio_start_ms=0, audio_end_ms=3000,
                motion=MotionSpec())


def test_stale_video_is_reported_as_stale_not_as_timing_failure(tmp_episodes,
                                                                monkeypatch):
    """重装分镜但未重渲时，成片已过期 —— 必须直说「过期」，
    而不是抛「视频总帧数 ≠ Σ镜头帧」（那是口径差，读起来像渲染 bug）。"""
    import app.orchestrator as o
    from app.managers import save

    ep = Episode(id="stale", title="t", episode_number=1)
    ep.frames = [Frame(id="frame-000", text="t", narration_text="旁白",
                       audio_path=None, audio_duration_ms=3000)]
    save(ep)
    d = tmp_episodes / ep.id
    (d / "out").mkdir(parents=True, exist_ok=True)
    (d / "out" / f"{ep.id}_m1.mp4").write_bytes(b"x" * 2048)   # 只是个占位产物

    monkeypatch.setattr(o, "_check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(o, "_check_toolchain", lambda **k: None)
    called = []
    monkeypatch.setitem(o.STAGE_FN, "shots",
                        lambda e, **k: called.append(1) or e.shots.update({"s0": _shot()}))
    monkeypatch.setitem(o.STAGE_FN, "verify",
                        lambda e: pytest.fail("过期成片不应触发 verify"))

    out = o.produce_legacy(ep.id, stages=["shots"])
    assert called, "shots 应被调用"
    assert out["verify"].get("stale") is True, out["verify"]
    assert "过期" in out["verify"]["error"], out["verify"]
    assert "视频总帧数" not in out["verify"]["error"], "不该伪装成时序故障"
    assert out["status"] != "FAILED", "过期不是失败，是待重渲"
