# -*- coding: utf-8 -*-
"""Pin the subtitle fidelity gate with tests.

Locks the caomin-01 / lishimin-04 accident (2026-09-29): polyphone corrections
reached the burned subtitles and every existing gate passed them, because
verify only checks duration and frame counts.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from app.models import Episode, Frame
from app.services import polyphone
from app.services.subtitle_fidelity import (DRIFT_WARN_RATIO, assert_subtitle_fidelity,
                                            frame_text_report, polyphone_leaks)

pytestmark = pytest.mark.shared

ROOT = Path(__file__).resolve().parents[1]


def _episode(tmp_path, frames, script):
    """Build a real Episode on disk so the fidelity check reads script.md."""
    ep_id = "fidelity_probe"
    d = tmp_path / ep_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "script.md").write_text(script, encoding="utf-8")
    ep = Episode(id=ep_id, title="fidelity probe", episode_number=1)
    ep.script_path = str(d / "script.md")
    ep.frames = [Frame(id=f"frame-{i:03d}", text=t, narration_text=t)
                 for i, t in enumerate(frames)]
    return ep


SCRIPT = """# 测试剧本

### 【第一幕：起】

【语气：低沉】
东北亚那边就是高句丽。

他自称天可汗。
"""


def test_clean_episode_passes(tmp_path):
    ep = _episode(tmp_path, ["东北亚那边就是高句丽。", "他自称天可汗。"], SCRIPT)
    r = frame_text_report(ep)
    assert r["ok"], r["fails"]
    assert assert_subtitle_fidelity(ep)["ok"]


def test_polyphone_leak_in_subtitle_fails(tmp_path):
    """The actual accident: narration_text (polyphone) written into frame.text."""
    polyphone._load()
    ep = _episode(tmp_path, ["东北亚那边就是高句丽。", "他自称天可汗。"], SCRIPT)
    f = ep.frames[0]
    f.text = polyphone.preprocess(f.text, log_hits=False)
    assert f.text == "东北亚那边就是高勾离。"
    r = frame_text_report(ep)
    assert not r["ok"]
    assert any("高句丽" in x and "高勾离" in x for x in r["fails"]), r["fails"]
    with pytest.raises(Exception):
        assert_subtitle_fidelity(ep)


def test_leak_detector_flags_replacement_form():
    # dst present and src absent -> that is exactly the polyphone leak shape
    assert [h["dst"] for h in polyphone_leaks("高勾离")] == ["高勾离"]
    assert [h["dst"] for h in polyphone_leaks("高句丽")] == []
    # author legitimately writing both forms -> no leak (src is present)
    assert polyphone_leaks("高句丽又作高勾离") == []


def test_drift_threshold_is_documented():
    assert 0.9 < DRIFT_WARN_RATIO < 1.0
