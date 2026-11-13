# -*- coding: utf-8 -*-
"""An ambiguous TTS dispatch must never be silently retried."""
import pytest

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
