# -*- coding: utf-8 -*-
"""Pin the two approval bindings (output bytes) and the legacy status reader."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"D:\hv")
from app.services import output_binding
from app.services.production_admission import check
from app.services.production_status import get_status

pytestmark = pytest.mark.shared
ROOT = Path(r"D:\hv")


def _ep(ep_id: str) -> bool:
    return (ROOT / "episodes" / f"{ep_id}.json").is_file()


# ---------------- output binding ----------------

def test_bind_records_sha256_and_frames():
    import shutil
    for e in ("caomin-01", "lishimin-04"):
        if not _ep(e):
            continue
        rec = output_binding.bind(e)
        assert rec["kind"] == "output_binding"
        assert len(rec["sha256"]) == 64
        assert rec["bytes"] > 0
        # frames/duration come from ffprobe, which may be absent from PATH in
        # isolation. Only assert them when the probe actually ran.
        if shutil.which("ffprobe"):
            assert rec["frames"] and rec["frames"] > 0, rec
            assert rec["duration_s"] and rec["duration_s"] > 0, rec
        assert output_binding.verify(e)["ok"]


def test_bind_refuses_missing_video():
    with pytest.raises(FileNotFoundError):
        output_binding.bind("no_such_episode_xyz")


def test_verify_detects_replaced_video():
    e = "caomin-01"
    if not _ep(e) or not output_binding.final_mp4(e).is_file():
        pytest.skip("no delivered mp4")
    assert output_binding.verify(e)["ok"]
    rec = output_binding.binding_path(e).read_text(encoding="utf-8")
    import json
    saved = json.loads(rec)["sha256"]
    # rewrite the binding with a wrong hash rather than touching a 457MB file
    bad = json.loads(rec)
    bad["sha256"] = "0" * 64
    output_binding.binding_path(e).write_text(
        json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    try:
        chk = output_binding.verify(e)
        assert not chk["ok"]
        assert any("sha256" in x for x in chk["fails"])
    finally:
        output_binding.binding_path(e).write_text(rec, encoding="utf-8")
    assert output_binding.verify(e)["ok"]
    assert output_binding.verify(e)["sha256"] == saved


def test_admission_breaks_when_output_bytes_change():
    e = "caomin-01"
    if not (ROOT / "episodes" / e / "production" / "input-approval.json").is_file():
        pytest.skip("no recorded approval")
    assert check(e)["eligible"], check(e)["blockers"]
    import json
    p = output_binding.binding_path(e)
    rec = p.read_text(encoding="utf-8")
    bad = json.loads(rec)
    bad["sha256"] = "1" * 64
    p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    try:
        r = check(e)
        assert not r["eligible"], "changed video bytes must break admission"
        assert any("approved bytes" in b for b in r["blockers"]), r["blockers"]
    finally:
        p.write_text(rec, encoding="utf-8")
    assert check(e)["eligible"]


# ---------------- legacy status ----------------

def test_delivered_legacy_episodes_report_approved():
    for e in ("caomin-01", "lishimin-04"):
        if not _ep(e):
            continue
        s = get_status(e)
        assert s["pipeline_version"] == "legacy_preview_v1", s
        assert s["approved"] is True, s["blockers"]
        assert s["delivery_ready"] is True
        assert s["status"] == "APPROVED"
        # output_frames is populated by ffprobe; absent in PATH it stays None.
        assert s["dimensions"].get("subtitle_fidelity") == "passed"
        import shutil
        if shutil.which("ffprobe"):
            assert s["output_frames"] and s["output_frames"] > 0, s


def test_episode_without_video_or_approval_stays_not_started():
    s = get_status("smoke-1788923000")
    assert s["pipeline_version"] == "production_v2"
    assert s["status"] == "NOT_STARTED"
    assert s["approved"] is False


def test_legacy_status_surfaces_real_problems():
    """lishimin-duori still has subtitle typos; status must not call it approved."""
    e = "lishimin-duori"
    if not _ep(e):
        pytest.skip("not present")
    s = get_status(e)
    assert s["approved"] is False
    assert s["status"] == "STALE"
    assert s["dimensions"].get("subtitle_fidelity") == "failed"
    assert any("polyphone" in b for b in s["blockers"]), s["blockers"]


def test_v2_episodes_keep_their_own_semantics():
    e = "lishimin-03"
    if not _ep(e):
        pytest.skip("not present")
    s = get_status(e)
    assert s["pipeline_version"] == "production_v2"
    # v2 approval requires the full listening/visual review chain; untouched.
    assert "listening" in " ".join(s["blockers"]) or s["approved"] is False
