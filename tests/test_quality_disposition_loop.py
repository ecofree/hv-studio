# -*- coding: utf-8 -*-
"""Pin the QA disposition loop: pending units clear one by one, never by bulk.

Why: xlc-01 shipped with 51/159 transcript findings pending. The only honest
way out is a human listening to each take and recording a verdict; the loop
(record_disposition -> pending) must work, and a bulk "approve all" must not
exist. Verified against the real report shape on a tmp copy, 2026-10-13.
"""
import json
import sys
from pathlib import Path

import pytest

from app.services.production_quality import record_disposition, pending

pytestmark = pytest.mark.production


def _report(tmp_path):
    from app.services.provenance import file_sha256

    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.wav").write_bytes(b"a")
    (tmp_path / "b.wav").write_bytes(b"b")
    rp = tmp_path / "quality.json"
    rp.write_text(json.dumps({
        "manifest_path": str(tmp_path / "manifest.json"),
        "manifest_sha256": file_sha256(tmp_path / "manifest.json"),
        "checker_sha256": file_sha256(
            Path(__file__).resolve().parents[1]/"app/services/asr_check.py"),
        "takes": [
            {"unit_id": "narr-a",
             "audio_path": str(tmp_path / "a.wav"),
             "audio_sha256": file_sha256(tmp_path / "a.wav"),
             "source_text": "贞观二十三年", "source_sha256": "s1",
             "quality": {"status": "failed"}},
            {"unit_id": "narr-b",
             "audio_path": str(tmp_path / "b.wav"),
             "audio_sha256": file_sha256(tmp_path / "b.wav"),
             "source_text": "翠微宫", "source_sha256": "s2",
             "quality": {"status": "failed"}},
        ],
        "pending_units": ["narr-a", "narr-b"],
    }), encoding="utf-8")
    return rp


def test_disposition_clears_exactly_one_unit(tmp_path, monkeypatch):
    # The manifest binding inside record_disposition is pinned elsewhere;
    # here the loop itself is under test.
    import app.services.production_quality as Q
    monkeypatch.setattr(
        Q, "validate_assembly",
        lambda p: {"takes": [{"unit_id": "narr-a"}, {"unit_id": "narr-b"}]})
    rp = _report(tmp_path)
    assert pending(str(rp)) == ["narr-a", "narr-b"]
    record_disposition(str(rp), "narr-a", "reviewer",
                       "master says 贞观, ASR misheard; take kept", "asr_false_positive")
    assert pending(str(rp)) == ["narr-b"]


def test_batch_review_validates_all_selected_audio_before_writing(tmp_path,monkeypatch):
    import app.services.production_quality as Q
    from app.services.provenance import file_sha256
    monkeypatch.setattr(Q,'validate_assembly',lambda p:{'takes':[{'unit_id':'narr-a'},{'unit_id':'narr-b'}]})
    rp=_report(tmp_path); expected=file_sha256(rp)
    (tmp_path/'b.wav').write_bytes(b'changed')
    with pytest.raises(ValueError,match='audio changed'):
        Q.record_dispositions(rp,['narr-a','narr-b'],'reviewer','actually listened','asr_false_positive',expected_report_sha256=expected)
    assert not list((rp.parent/'dispositions').glob('*.json'))
    (tmp_path/'b.wav').write_bytes(b'b')
    Q.record_dispositions(rp,['narr-a'],'reviewer','actually listened','asr_false_positive',expected_report_sha256=expected)
    assert pending(rp)==['narr-b']


def test_disposition_requires_a_named_reviewer_and_finding(tmp_path, monkeypatch):
    import app.services.production_quality as Q
    monkeypatch.setattr(
        Q, "validate_assembly",
        lambda p: {"takes": [{"unit_id": "narr-a"}, {"unit_id": "narr-b"}]})
    rp = _report(tmp_path)
    with pytest.raises(ValueError, match="reviewer"):
        record_disposition(str(rp), "narr-a", "  ", "something", "asr_false_positive")
    with pytest.raises(ValueError, match="reviewer"):
        record_disposition(str(rp), "narr-a", "who", "  ", "asr_false_positive")
    with pytest.raises(ValueError, match="disposition"):
        record_disposition(str(rp), "narr-a", "who", "something", "approve-all")
    assert pending(str(rp)) == ["narr-a", "narr-b"]


def test_spoken_error_rejects_the_take(tmp_path, monkeypatch):
    import app.services.production_quality as Q
    from app.services import audio_disposition as AD

    monkeypatch.setattr(
        Q, "validate_assembly",
        lambda p: {"takes": [{"unit_id": "narr-a"}, {"unit_id": "narr-b"}]})
    monkeypatch.setattr(AD, "reject_take",
                        lambda take, reviewer, finding, evidence_path=None: None)
    rp = _report(tmp_path)
    record_disposition(str(rp), "narr-a", "reviewer",
                       "take really misreads the line", "spoken_error")
    # A spoken error keeps the unit pending: the take must be replaced, and only
    # a fresh take with a passing recheck can clear it. Clearing it here would
    # be exactly the fake-green this repo forbids.
    assert pending(str(rp)) == ["narr-a", "narr-b"]
    recs = sorted((rp.parent / "dispositions").glob("*.json"))
    assert len(recs) == 1
    assert json.loads(recs[0].read_text(encoding="utf-8"))["decision"] == "spoken_error"
