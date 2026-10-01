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

sys.path.insert(0, r"D:\hv")
from app.models import Episode, Frame
from app.services import polyphone
from app.services.subtitle_fidelity import (DRIFT_WARN_RATIO, assert_subtitle_fidelity,
                                            frame_text_report, polyphone_leaks)

pytestmark = pytest.mark.shared

ROOT = Path(r"D:\hv")


def _episode(tmp_path, frames, script):
    """Build a real Episode on disk so the fidelity check reads script.md."""
    ep_id = "fidelity_probe"
    d = ROOT / "episodes" / ep_id
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


def test_all_real_episodes_pass(tmp_path):
    """Guard the whole back catalogue, not just a synthetic case."""
    for eid in ("caomin-01", "lishimin-04", "lishimin-03", "lishimin-01",
                "laogongtuan", "lishimin-02"):
        f = ROOT / "episodes" / f"{eid}.json"
        if not f.is_file():
            continue
        from app.managers import load
        r = frame_text_report(load(eid))
        assert r["ok"], f"{eid}: {r['fails']}"


def test_known_typo_episodes_are_still_caught(tmp_path):
    """lishimin-duori was delivered before the gate existed; it must fail now."""
    f = ROOT / "episodes" / "lishimin-duori.json"
    if not f.is_file():
        pytest.skip("lishimin-duori not present")
    from app.managers import load
    r = frame_text_report(load("lishimin-duori"))
    assert not r["ok"], "pre-gate typo episode should be caught by the gate"
    assert r["leaks"], "typo episode should be caught by the polyphone rule"


def test_drift_threshold_is_documented():
    assert 0.9 < DRIFT_WARN_RATIO < 1.0


def test_verify_rejects_typo_subtitles_end_to_end():
    """stage_verify must fail on a bad subtitle even when the mp4 is valid."""
    ep = "lishimin-04"
    out = ROOT / "episodes" / ep / "out" / f"{ep}_m1.mp4"
    if not out.is_file():
        pytest.skip("no composed mp4 to verify")
    from app.managers import load, save
    real = load(ep)
    backup = [(f.id, f.text) for f in real.frames]
    try:
        target = next(f for f in real.frames if "高句丽" in (f.text or ""))
        target.text = polyphone.preprocess(target.text, log_hits=False)
        save(real)
        p = subprocess.run(
            [r"D:\hv\.venv-core\Scripts\python.exe", "-m", "app.cli", "episode",
             "produce", ep, "--stage", "verify", "--pipeline", "legacy-preview"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**__import__("os").environ,
                 "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
                 "PYTHONIOENCODING": "utf-8",
                 "PATH": (r"C:\Users\coumoo\AppData\Local\Microsoft\WinGet\Packages"
                          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
                          r"\ffmpeg-9.0.2-full_build\bin")
                        + __import__("os").pathsep + __import__("os").environ["PATH"]},
            timeout=1800)
        assert p.returncode != 0, "verify must fail on a typo subtitle"
        assert "高句丽" in (p.stdout + p.stderr) or "字面" in (p.stdout + p.stderr)
    finally:
        real = load(ep)
        for f in real.frames:
            f.text = next(t for i, t in backup if i == f.id)
        save(real)
    assert frame_text_report(load(ep))["ok"]
