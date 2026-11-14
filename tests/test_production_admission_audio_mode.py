# -*- coding: utf-8 -*-
"""External recordings are not coupled to a TTS VoiceAsset."""
from pathlib import Path
import pytest

pytestmark = pytest.mark.production

from app.services import production_admission as admission


def test_external_recording_input_has_no_tts_voice_asset_dependency(tmp_path, monkeypatch):
    folder = tmp_path / "episode"
    binding = folder / "production" / "external-audio-binding.json"
    binding.parent.mkdir(parents=True)
    binding.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "app.services.voice_assets.resolve_voice_asset",
        lambda: (_ for _ in ()).throw(AssertionError("TTS asset must not be resolved")),
    )
    assert admission._voice_asset_version_for_inputs(folder) is None


def test_tts_input_still_requires_registered_voice_asset(tmp_path, monkeypatch):
    folder = tmp_path / "episode"
    folder.mkdir()
    monkeypatch.setattr(
        "app.services.voice_assets.resolve_voice_asset",
        lambda: (Path("voice.json"), {"version": "voice-v1"}),
    )
    assert admission._voice_asset_version_for_inputs(folder) == "voice-v1"
