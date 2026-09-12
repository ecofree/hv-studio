"""
帧数口径一致性测试（2026-09-04 补）。

锁住「渲染写出帧数 / 段完成判据 / 时序断言 #5」三者必须同口径。
背景：曾出现一次舍入与逐镜舍入两种算法并存，逐镜舍入会累积差额
（laogongtuan 实测差 26 帧：31461 vs 31487），按 90s 切 15 段时 11 段不等，
使分段渲染的「已完成段跳过」永远判不中，断点续跑静默退化成全量重编。

故本测试是分段渲染能否真正续跑的守门测试，勿删。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


from app.services.motion import (
    FPS,
    frames_for_duration_ms,
    plan_frames,
)
from app.services.compose import expected_frames
from app.services.timing_assert import _shot_frames, MIN_FRAMES, MAX_FRAMES


class _Shot:
    """最小 shot 替身：只需 duration_ms / id。"""

    def __init__(self, sid: str, duration_ms: int):
        self.id = sid
        self.duration_ms = duration_ms


# 会制造舍入分叉的时长：2016ms → 50.4 帧 → 逐镜 round 到 50，
# 但 5 段累加后 10080ms → 252.0 帧，一次舍入得 252。两者差 2 帧。
DIVERGING_MS = 2016
N_DIVERGING = 5


def test_frames_for_duration_matches_plan_frames():
    """单镜：frames_for_duration_ms 必须等于 plan_frames 实际产出的帧数。"""
    for ms in (1600, 1601, 2000, 2016, 2430, 3999, 4000, 8000):
        got = frames_for_duration_ms(ms, FPS)
        want = len(plan_frames(1882, 3344, ms / 1000.0, None, FPS))
        assert got == want, f"duration={ms}ms: 口径分裂 {got} != {want}"


def test_per_shot_sum_differs_from_one_shot_round():
    """逐镜舍入与一次舍入确实会分叉——这是本测试要防的根因。

    若某天这两者变得恒等，说明边界条件被改坏了（或 FPS 变了），
    应当重新审视，而不是删掉断言。
    """
    shots = [_Shot(f"s{i}", DIVERGING_MS) for i in range(N_DIVERGING)]
    per_shot = sum(frames_for_duration_ms(s.duration_ms) for s in shots)
    one_shot = round(sum(s.duration_ms for s in shots) / 1000.0 * FPS)
    assert per_shot == 250 and one_shot == 252, (
        f"预期分叉数据失效: per_shot={per_shot} one_shot={one_shot}"
    )
    assert per_shot != one_shot


def test_expected_frames_uses_per_shot_rounding():
    """expected_frames 必须逐镜求和，而非一次舍入。"""
    shots = [_Shot(f"s{i}", DIVERGING_MS) for i in range(N_DIVERGING)]
    tl = [(s, 0, 0) for s in shots]
    assert expected_frames(tl) == 250
    assert expected_frames(tl) != round(
        sum(s.duration_ms for s in shots) / 1000.0 * FPS
    )


def test_expected_frames_accepts_plain_shot_list():
    """兼容 [(shot,s0,s1)] 与 [shot] 两种入参形态。"""
    shots = [_Shot("a", 2400), _Shot("b", 3600)]
    assert expected_frames([(s, 0, 0) for s in shots]) == expected_frames(shots)


def test_timing_assert_shares_the_same_rule():
    """时序断言 #5 与 compose 段判据必须共用同一口径。"""
    shots = [_Shot(f"s{i}", 1600 + i * 7) for i in range(30)]
    assert sum(_shot_frames(s) for s in shots) == expected_frames(shots)


def test_no_divergence_when_durations_are_frame_aligned():
    """帧对齐的时长（2000ms=50帧整）不应产生分叉——反向确认分叉只在有
    小数残余时出现，避免把测试写成「永远不相等」的假断言。"""
    shots = [_Shot(f"s{i}", 2000) for i in range(10)]
    per_shot = sum(frames_for_duration_ms(s.duration_ms) for s in shots)
    one_shot = round(sum(s.duration_ms for s in shots) / 1000.0 * FPS)
    assert per_shot == one_shot == 500
