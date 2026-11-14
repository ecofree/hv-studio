# -*- coding: utf-8 -*-
"""An ambiguous TTS dispatch must never be silently retried."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.production

from app.services.narration_pipeline import _assert_no_uncertain_attempt
from app.services.provenance import atomic_json, file_sha256, fingerprint


def _attempt(tmp_path, status, *, effective=None):
    base = tmp_path / "production"
    folder = base / "takes" / "unit-000-previous"
    folder.mkdir(parents=True)
    request = {"text": "A fixed narration request", "voice": "fixture"}
    request_fp = fingerprint(request)
    atomic_json(folder / "compiled_request.json", request)
    atomic_json(folder / "attempt.json", {"status": status, "request_fp": request_fp})
    if effective is not None:
        raw_path = folder / "raw.wav"
        raw_path.write_bytes(b"raw-audio")
        atomic_json(folder / "effective_request.json", {
            "status": effective, "audio_sha256": file_sha256(raw_path)
        })
    return base, request_fp


@pytest.mark.parametrize("status", ["started", "dispatching", "failed", "failed_or_uncertain"])
def test_ambiguous_tts_attempt_blocks_automatic_retry(tmp_path, status):
    base, request_fp = _attempt(tmp_path, status)
    with pytest.raises(ValueError, match="outcome is uncertain"):
        _assert_no_uncertain_attempt(base, "unit-000", request_fp)


def test_budget_refusal_before_dispatch_does_not_block_retry(tmp_path):
    base, request_fp = _attempt(tmp_path, "not_dispatched")
    _assert_no_uncertain_attempt(base, "unit-000", request_fp)


def test_durable_successful_provider_response_can_be_reused(tmp_path):
    base, request_fp = _attempt(tmp_path, "postprocess_failed", effective="succeeded")
    _assert_no_uncertain_attempt(base, "unit-000", request_fp)


def test_corrupt_attempt_receipt_blocks_paid_retry(tmp_path):
    base, request_fp = _attempt(tmp_path, "dispatching")
    receipt = base / "takes" / "unit-000-previous" / "attempt.json"
    receipt.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="receipt is unreadable"):
        _assert_no_uncertain_attempt(base, "unit-000", request_fp)


def test_failed_tts_attempt_retains_provider_request_identifier_without_payload():
    from app.services.narration_pipeline import _provider_reconciliation_details

    value = _provider_reconciliation_details({
        "status": "failed_or_uncertain",
        "provider_request_id": "task-123",
        "provider_request_path": "/private/state/tts_requests/attempt.json",
        "text": "must not be surfaced",
        "api_key": "must not be surfaced",
    })
    assert value == {
        "request_id": "task-123",
        "request_path": "/private/state/tts_requests/attempt.json",
    }


def test_missing_provider_request_identifier_is_explicitly_unavailable():
    from app.services.narration_pipeline import _provider_reconciliation_details

    assert _provider_reconciliation_details({"status": "failed_or_uncertain"}) == {
        "request_id": None, "request_path": None,
    }


def test_provider_adapter_attaches_request_id_from_sanitized_receipt(tmp_path, monkeypatch):
    import json
    from app.services import narration_pipeline as pipeline

    receipt = tmp_path / "tts-request.json"
    receipt.write_text(json.dumps({
        "status": "failed", "request_id": "provider-task-456",
        "text": "private narration", "api_key": "never-copy-this",
    }), encoding="utf-8")

    class FakeSynth:
        last_request_path = receipt
        voice_asset_version = None

        def __init__(self, **kwargs):
            pass

        def synthesize(self, text, raw):
            raise TimeoutError("simulated offline timeout")

    monkeypatch.setattr("app.services.cosyvoice.DashScopeCosyVoiceSynthesizer", FakeSynth)
    request = {
        "model": "offline-model", "voice_id": "fixture", "sample_rate": 24000,
        "pitch_rate": 1.0, "speech_rate": 1.0, "volume": 50,
        "instruction": "", "pronunciation": "", "seed": 1,
        "voice_asset_version": "fixture-v1", "text": "private narration",
    }
    with pytest.raises(TimeoutError) as exc:
        pipeline._provider(request, tmp_path / "raw.wav")
    assert exc.value.hv_provider_request_id == "provider-task-456"
    assert exc.value.hv_provider_request_path == str(receipt.resolve())


def test_budget_refusal_remains_not_dispatched_through_real_pipeline(tmp_path):
    from app.services import narration_pipeline as N
    from app.services.provider_budget import ProviderBudget, BudgetExceeded
    from test_production_v2 import assembly
    _,plan,asset,calls,provider,checker=assembly(tmp_path)
    count=len(calls)
    denied=ProviderBudget(tmp_path/'denied.json',tts_requests=0,characters=0)
    with pytest.raises(BudgetExceeded):
        N.synthesize(plan,asset,max_requests=100,max_characters=100000,
                     force=True,provider=provider,checker=checker,budget=denied)
    attempts=[N.read(p) for p in (Path(plan).parent/'takes').glob('*/attempt.json')]
    assert any(r['status']=='not_dispatched' for r in attempts)
    assert not any(r['status']=='failed_or_uncertain' for r in attempts)
    assert len(calls)==count and denied.snapshot()['used']['tts_requests']==0
    allowed=ProviderBudget(tmp_path/'allowed.json',tts_requests=100,characters=100000)
    N.synthesize(plan,asset,max_requests=100,max_characters=100000,
                 force=True,provider=provider,checker=checker,budget=allowed)
    assert len(calls)>count


def test_provider_failure_before_receipt_preserves_original_error(tmp_path, monkeypatch):
    from app.services import narration_pipeline as N
    class FakeSynth:
        def __init__(self,**kwargs):pass
        def synthesize(self,*args):raise ValueError('configuration before dispatch')
    monkeypatch.setattr('app.services.cosyvoice.DashScopeCosyVoiceSynthesizer',FakeSynth)
    request=dict(model='offline',voice_id='fixture',sample_rate=24000,pitch_rate=1.,
                 speech_rate=1.,volume=50,instruction='',pronunciation='',seed=1,
                 voice_asset_version='fixture',text='test')
    with pytest.raises(ValueError,match='configuration before dispatch'):
        N._provider(request,tmp_path/'raw.wav')
