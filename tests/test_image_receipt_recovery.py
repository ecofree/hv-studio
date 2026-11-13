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


def test_source_dimensions_do_not_have_to_equal_video_canvas(tmp_path):
    """A decodable source plate is locally fitted at compose time."""
    from PIL import Image
    from app.services.image import ImageJob
    from app.models import ImageGenRequest

    p = tmp_path / "odd.png"
    Image.new("RGB", (1086, 1448), (10, 10, 10)).save(p)
    job = ImageJob("odd", ImageGenRequest(model="gpt-image-2", prompt="x", size="1080x1920"),
                   tmp_path / "odd.png")
    assert job.validate_output(p)==(1086,1448)


def test_offline_recovery_reconciles_cost_once_without_dispatch(tmp_path, monkeypatch):
    import base64, io
    from PIL import Image
    from app.models import ImageGenRequest
    from app.services import image as m
    from app.services.provenance import atomic_json
    job=m.ImageJob('recovery',ImageGenRequest(model='gpt-image-2',prompt='camp'),tmp_path/'source.png')
    data=io.BytesIO();Image.new('RGB',(300,400)).save(data,format='PNG')
    receipt=job.out.with_suffix('.png.response.json')
    atomic_json(receipt,dict(request_fingerprint=job.request_fingerprint(),channel='offline',
                            attempt_id='original-attempt',response={'data':[{'b64_json':base64.b64encode(data.getvalue()).decode()}]}))
    monkeypatch.setattr(m,'_COST_PATH',tmp_path/'cost.json')
    monkeypatch.setattr(m,'_PRICES_PATH',tmp_path/'prices.json')
    monkeypatch.setattr(m,'get_image',lambda:pytest.fail('recovery cannot dispatch'))
    m.recover_saved_response(job,receipt)
    first=job.out.read_bytes()
    m.recover_saved_response(job,receipt)
    rows=json.loads((tmp_path/'cost.json').read_text())
    assert job.is_done() and job.out.read_bytes()==first and len(rows)==1
    assert rows[0]['attempt_id']=='original-attempt'
    assert json.loads(job.out.with_suffix('.png.recovery.json').read_text())['new_provider_calls']==0
    # Same prompt can return different pixels. A valid existing plate cannot vouch for another receipt.
    prior=json.loads(receipt.read_text())
    replacement=io.BytesIO();Image.new('RGB',(300,400),'navy').save(replacement,format='PNG')
    prior['response']['data']=[{'b64_json':base64.b64encode(replacement.getvalue()).decode()}]
    prior['attempt_id']='second-attempt'
    atomic_json(receipt,prior)
    m.recover_saved_response(job,receipt)
    assert job.is_done() and job.out.read_bytes()==replacement.getvalue()!=first
    assert len(json.loads((tmp_path/'cost.json').read_text()))==2
    prior['response']['data']=[]
    atomic_json(receipt,prior)
    with pytest.raises(m.ImageError,match='empty paid response'):
        m.recover_saved_response(job,receipt)
    assert job.out.read_bytes()==replacement.getvalue() and len(json.loads((tmp_path/'cost.json').read_text()))==2
