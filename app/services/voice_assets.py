"""Resolve explicitly registered voices without creating or silently replacing one."""
from pathlib import Path
from app.config.config import ROOT, get_settings
from app.services.provenance import fingerprint


def resolve_voice_asset(model=None, path=None):
    from app.services.narration_pipeline import read
    config = get_settings().get_profile().voice
    path = path or config.voice_asset
    if not path:
        raise ValueError("No registered VoiceAsset; import an existing voice before production")
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    asset = read(path)
    unsigned = {k: v for k, v in asset.items() if k != "version"}
    if asset.get("version") != fingerprint(unsigned):
        raise ValueError("VoiceAsset content differs from its version")
    if asset.get("model") != (model or config.cosyvoice_model):
        raise ValueError("VoiceAsset model differs from the selected model")
    if not asset.get("voice_id") or asset.get("status") in ("rejected", "revoked"):
        raise ValueError("VoiceAsset is missing or revoked")
    return path.resolve(), asset
