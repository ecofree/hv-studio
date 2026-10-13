# -*- coding: utf-8 -*-
"""Pin the narration provenance gate: declare it, and never fake a TTS binding.

Why: v2's narration gate re-derived `request_fp` and `voice_asset_version` for
every take, because a synthesized take is meaningless without the request that
produced it. A take cut out of an external recording satisfies neither, and the
only way to make it pass was to write both fingerprints onto it -- i.e. to make
the manifest assert something untrue. These tests lock the honest alternative:
declare the provenance, gate each kind on evidence that can be measured, and
fail closed on anything unrecognised. caomin-02, 2026-10-13.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"D:\hv")
from app.services.episode_production import check_narration_binding
from app.services.external_narration import PROVENANCE

pytestmark = pytest.mark.production

PLAN = {"units": [{"unit_id": "narr-a"}, {"unit_id": "narr-b"}]}


def _external(takes=2):
    """An external manifest as `external_narration` emits it. Nothing here is a
    synthesis binding, and that is the point: these takes must not need one."""
    return {
        "provenance": PROVENANCE,
        "samples": 100, "sample_rate": 24000,
        "master_path": str(Path(__file__).resolve()),   # never read: rejected earlier
        "master_sha256": "0" * 64,
        "source_recording": {"delivered_sha256": "d" * 64, "sha256": "0" * 64},
        "takes": [{"unit_id": f"narr-{i}"} for i in range(takes)],
        "alignment": [{"unit_id": f"narr-{i}", "sample_start": i * 10,
                       "sample_end": i * 10 + 5, "coverage": 0.9} for i in range(takes)],
    }


def _tts():
    return {"takes": [{"unit_id": "narr-a", "voice_asset_version": "v1", "request_fp": "fp"},
                      {"unit_id": "narr-b", "voice_asset_version": "v1", "request_fp": "fp"}]}


# --- the dispatch itself -----------------------------------------------------

def test_unknown_provenance_fails_closed():
    """A manifest whose provenance this gate does not recognise is not evidence
    of anything. Refusing is the only honest answer."""
    for bogus in ("", "imported", "cosyvoice", "tts-v2", None):
        manifest = _tts(); manifest["provenance"] = bogus
        with pytest.raises(ValueError, match="unknown narration provenance"):
            check_narration_binding(manifest, PLAN)


def test_external_manifest_never_reaches_the_voice_asset_gate():
    """An external take has no voice asset. If this call tried to resolve one it
    would either crash or, worse, quietly compare against whatever asset happened
    to be current. It must not get that far: byte evidence fails first, and only
    a real recording would pass."""
    manifest = _external()
    with pytest.raises(ValueError):
        check_narration_binding(manifest, PLAN)


def test_external_manifest_must_cover_every_unit_in_order():
    manifest = _external(takes=1)
    with pytest.raises(ValueError):
        check_narration_binding(manifest, PLAN)


# --- backward compatibility: this must not weaken the existing gate ---------

def test_legacy_manifest_without_provenance_still_requires_a_voice_asset(monkeypatch):
    """Delivered episodes predate the field. Absence must mean 'synthesized', so
    they keep every original check -- the new gate cannot become a way around it."""
    import app.services.voice_assets as VA

    def boom(path=None):
        raise ValueError("no voice asset configured")

    monkeypatch.setattr(VA, "resolve_voice_asset", boom)
    import app.services.episode_production as EP
    monkeypatch.setattr(EP, "resolve_voice_asset", boom)

    with pytest.raises(ValueError, match="no voice asset"):
        check_narration_binding(_tts(), PLAN)


def test_legacy_manifest_rejects_a_stale_voice_asset(monkeypatch):
    import app.services.voice_assets as VA
    import app.services.episode_production as EP
    monkeypatch.setattr(VA, "resolve_voice_asset", lambda path=None: (None, {"version": "v2"}))
    monkeypatch.setattr(EP, "resolve_voice_asset", lambda path=None: (None, {"version": "v2"}))
    with pytest.raises(ValueError, match="voice asset changed"):
        check_narration_binding(_tts(), PLAN)


def test_legacy_manifest_still_binds_to_its_compiled_request(monkeypatch):
    """`request_fp` is re-derived from the plan, not read from the manifest, so a
    take cannot be re-pointed at a different unit by editing the manifest."""
    import app.services.narration_pipeline as N
    import app.services.episode_production as EP
    import app.services.delivery as D

    monkeypatch.setattr(EP, "resolve_voice_asset", lambda path=None: (None, {"version": "v1"}))
    monkeypatch.setattr(D, "effective_unit", lambda u, plan, asset: u)
    monkeypatch.setattr(N, "compile_request", lambda u, asset: {"text": u["unit_id"]})
    import app.services.provenance as P
    monkeypatch.setattr(EP, "fingerprint", lambda v: str(v))

    # A manifest that claims the wrong unit must not survive.
    manifest = _tts()
    manifest["takes"][0]["request_fp"] = "{'text': 'narr-b'}"
    with pytest.raises(ValueError, match="compiled narration request changed"):
        check_narration_binding(manifest, PLAN)


def test_legacy_manifest_wrong_unit_count_is_rejected(monkeypatch):
    import app.services.voice_assets as VA
    import app.services.episode_production as EP
    monkeypatch.setattr(VA, "resolve_voice_asset", lambda path=None: (None, {"version": "v1"}))
    monkeypatch.setattr(EP, "resolve_voice_asset", lambda path=None: (None, {"version": "v1"}))
    manifest = _tts(); manifest["takes"] = manifest["takes"][:1]
    with pytest.raises(ValueError, match="compiled narration request changed"):
        check_narration_binding(manifest, PLAN)