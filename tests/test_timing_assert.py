# -*- coding: utf-8 -*-
"""时序断言测试（D5）。

锁两件事：
  1) 阈值常量与 spec 同源（HEAD_MS/TAIL_MS/FPS 不得再出现手抄副本）
  2) 逐镜帧数上下限（MIN/MAX_FRAMES）在真实 Shot 上生效：短镜/超长镜必须拦，
     合规镜必须放行。此前这块只有"常量未被改"的断言，没有真实对象的行为断言。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


from app.models import Episode, Frame, MotionSpec, Shot, ShotType
from app.services.timing_assert import (
    MAX_FRAMES, MIN_FRAMES, TimingError, assert_timing_or_raise, shot_report,
)


def _shot(sid: str, duration_ms: int, audio_ms: int | None = None):
    a = duration_ms if audio_ms is None else audio_ms
    return Shot(
        id=sid, frame_id="frame-000", index=0, type=ShotType.KEN_BURNS,
        prompt="p", duration_ms=duration_ms,
        audio_start_ms=0, audio_end_ms=a,
        motion=MotionSpec(),
    )


def _ep(shots, audio_ms: int) -> Episode:
    ep = Episode(id="ta", title="ta", episode_number=1)
    ep.frames = [Frame(id="frame-000", text="t", narration_text="t",
                       audio_path=None, audio_duration_ms=audio_ms)]
    ep.shots = {s.id: s for s in shots}
    return ep


def test_thresholds_match_spec():
    """HEAD/TAIL/FPS 必须与 spec 同源（此前是手抄副本，注释承诺同步但无机制）。"""
    from app.services import spec
    import app.services.timing_assert as ta
    assert (ta.HEAD_MS, ta.TAIL_MS) == (spec.HEAD_MS, spec.TAIL_MS)
    assert ta.FPS == spec.FPS
    # 阈值本身也是契约，改动需显式改这里
    assert (MIN_FRAMES, MAX_FRAMES) == (40, 200)


def test_shot_report_ok_in_bounds():
    sr = shot_report(_shot("s", 3000))          # 3s → 75 帧
    assert sr["ok"] and sr["frames"] == 75 and sr["fails"] == []


def test_too_short_shot_is_rejected():
    """0.8s 短镜（20 帧 < 40）必须被拦——这是 2026-09-03 审计里"短镜悄悄出片"的口子。"""
    sr = shot_report(_shot("s", 800))
    assert not sr["ok"] and sr["frames"] == 20
    with pytest.raises(TimingError, match="时序断言失败"):
        assert_timing_or_raise(_ep([_shot("s", 800)], audio_ms=800))


def test_overlong_shot_is_rejected():
    sr = shot_report(_shot("s", 9000))          # 9s → 225 帧 > 200
    assert not sr["ok"] and sr["frames"] == 225
    with pytest.raises(TimingError):
        assert_timing_or_raise(_ep([_shot("s", 9000)], audio_ms=9000))


def test_valid_episode_passes():
    ep = _ep([_shot("s1", 3000), _shot("s2", 4000)], audio_ms=7000)
    rep = assert_timing_or_raise(ep)
    assert rep["ok"], rep["fails"]


def test_audio_sum_mismatch_is_rejected():
    """Σ镜头旁白 ≠ Σ帧旁白（差 >100ms）→ 音频丢了或重复了。"""
    ep = _ep([_shot("s1", 3000, audio_ms=1000)], audio_ms=3000)
    with pytest.raises(TimingError, match="音频可能丢/重"):
        assert_timing_or_raise(ep)
