# -*- coding: utf-8 -*-
"""Pin the paid-response recovery path: actionable errors, quarantine without deletion.

Why: xlc-01 XL22b1 2026-10-13 -- the provider returned 1086x1448 for a
1080x1920 request, the receipt blocked every retry, and the error said only
"saved response requires recovery: ValueError". Paid evidence was stuck with
no disposition path except deleting the proof that money was spent.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"D:\hv")
from app.services.image import quarantine_saved_response

pytestmark = pytest.mark.production


def _receipt(tmp_path, name="XL99.png.response.json"):
    d = tmp_path / "fp-abc123"
    d.mkdir()
    rp = d / name
    rp.write_text(json.dumps({"request_fingerprint": "fp-abc123",
                              "channel": "cxk", "response": {"data": []}}),
                  encoding="utf-8")
    (d / "XL99.png").write_bytes(b"not-a-png")
    return rp


def test_quarantine_moves_everything_and_records_the_reason(tmp_path):
    rp = _receipt(tmp_path)
    dst = quarantine_saved_response(rp, "provider returned 1086x1448 for 1080x1920")
    assert dst.name == "fp-abc123"
    assert dst.parent.name == "_quarantine"
    assert not rp.exists()
    assert (dst / rp.name).is_file()
    assert (dst / "XL99.png").is_file()
    event = json.loads((dst / "quarantine.json").read_text(encoding="utf-8"))
    assert event["reason"] == "provider returned 1086x1448 for 1080x1920"
    assert event["request_fingerprint"] == "fp-abc123"
    assert event["channel"] == "cxk"
    assert event["quarantined_at"]


def test_quarantine_refuses_silence_and_double_jeopardy(tmp_path):
    rp = _receipt(tmp_path)
    with pytest.raises(ValueError, match="reason is required"):
        quarantine_saved_response(rp, "  ")
    with pytest.raises(ValueError, match="not a saved image response"):
        quarantine_saved_response(tmp_path / "notes.txt", "whatever")
    with pytest.raises(FileNotFoundError):
        quarantine_saved_response(tmp_path / "nope" / "X.png.response.json", "r")
    dst = quarantine_saved_response(rp, "bad dims")
    with pytest.raises(ValueError, match="already quarantined"):
        quarantine_saved_response(dst / rp.name, "again")


def test_quarantine_handles_the_real_xl22b1_receipt_shape(tmp_path):
    """The real stuck receipt (4.15MB, valid JSON, wrong dims) must survive
    quarantine byte-identical: it is the proof that the call was paid for."""
    real = list(Path(r"D:\hv/episodes/xlc-01/production/v2/images").glob(
        "4078b929e8248410e27458665fb3c96828aa39cb3197f7e608fac94fe5379618"
        "/*.response.json"))
    if not real:
        pytest.skip("xlc-01 receipts not on disk")
    work = tmp_path / "w"
    shutil.copytree(real[0].parent, work / real[0].parent.name)
    rp = work / real[0].parent.name / real[0].name
    before = rp.stat().st_size
    dst = quarantine_saved_response(rp, "provider returned 1086x1448 for 1080x1920")
    assert (dst / rp.name).stat().st_size == before
    event = json.loads((dst / "quarantine.json").read_text(encoding="utf-8"))
    assert len(event["response_sha256"]) == 64


def test_dimension_rejection_names_the_numbers(tmp_path):
    """The gate that rejected XL22b1 must say what it saw, not just ValueError."""
    from PIL import Image
    from app.services.image import ImageJob
    from app.models import ImageGenRequest

    p = tmp_path / "odd.png"
    Image.new("RGB", (1086, 1448), (10, 10, 10)).save(p)
    job = ImageJob("odd", ImageGenRequest(model="gpt-image-2", prompt="x", size="1080x1920"),
                   tmp_path / "odd.png")
    with pytest.raises(ValueError, match=r"1086.*1448.*1080x1920"):
        job.validate_output(p)
