"""Bind resumable work to implementation, effective settings, inputs and artifacts."""
import json
from pathlib import Path
from app import paths
from app.config.config import ROOT, get_settings, load_env
from app.services.provenance import file_sha256, fingerprint


def implementation_identity():
    files = list((ROOT / "app").rglob("*.py"))
    files += [ROOT / "requirements.txt", ROOT / "requirements.lock.txt"]
    files += [ROOT / "requirements.core.lock.txt", ROOT / "requirements.asr.lock.txt",
              ROOT / "bin/local_asr_worker.py", ROOT / "bin/local_alignment_worker.py"]
    files += list((ROOT / "app/config").glob("*.yaml"))
    return fingerprint({str(p.relative_to(ROOT)): file_sha256(p) for p in files if p.is_file()})


def input_identity(ep_id, stages, options=None):
    directory = paths.episode_dir(ep_id)
    files = {paths.episode_json(ep_id)}
    files.update((paths.state_dir()/'audio_rejections').glob('*.json'))
    for name in ("script.md", "scene_plan.json", "image_prompts.json"):
        files.add(directory / name)
    for folder in ("shots", "audio", "production/v2"):
        base = directory / folder
        if base.is_dir():
            files.update(p for p in base.rglob("*") if p.is_file() and
                         not any(x in p.parts for x in (".candidates", "build", "renders", "budgets")) and
                         p.suffix.lower() in (".json", ".md", ".wav", ".mp3", ".png", ".jpg", ".webp"))
    # Include path references outside the episode directory (voice, reused images,
    # approved manifests, fonts and final videos), without traversing arbitrary folders.
    seen = set()
    def visit(value):
        if isinstance(value, dict):
            for item in value.values(): visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, str) and len(value) < 4096 and "\n" not in value:
            try:
                p = Path(value)
                # Shared accounting changes as later episodes run. It is an
                # operational limit, not an input to already completed media.
                if p.parent.name == 'budgets': return
                if p.suffix.lower() not in (".json", ".md", ".wav", ".mp3", ".mp4", ".png", ".jpg", ".webp", ".ttf", ".otf"): return
                if not p.is_absolute(): p = ROOT / p
                if p.is_file(): files.add(p)
            except OSError:
                pass
    profile = get_settings().get_profile().model_dump(mode="json")
    visit(profile)
    while True:
        pending = [p for p in files if p.suffix == ".json" and p not in seen and p.is_file()]
        if not pending: break
        for p in pending:
            seen.add(p)
            try: visit(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError): pass
    return fingerprint({"stages": stages, "options": options, "implementation": implementation_identity(),
                        "profile": profile, "environment_hash": fingerprint(load_env()),
                        "files": {str(p.resolve()): file_sha256(p) if p.is_file() else None for p in files}})


def artifact_receipt(result):
    artifacts = list(result.get("artifacts", []))
    if result.get("final_mp4"): artifacts.append(result["final_mp4"])
    if not artifacts:
        raise ValueError("stage completion has no artifact evidence")
    receipt = {str(Path(p).resolve()): file_sha256(p) for p in artifacts}
    return receipt


def artifacts_current(receipt):
    try:
        return bool(receipt) and all(file_sha256(p) == sha for p, sha in receipt.items())
    except OSError:
        return False


# Renderer paths: their change cannot alter already-rendered bytes (the
# renderer itself, its pinned runtime, its configuration). Everything else
# (episode media, plans, bindings, fonts, voice assets) is content: if it
# drifted, the bytes on disk may no longer be what the inputs describe.
# NOTE: RENDERER_PARTS is documentation, not logic; _is_renderer decides.
RENDERER_PARTS = ("app/", "requirements", "bin/local_asr_worker.py",
                  "bin/local_alignment_worker.py", "app/config/")


def _is_renderer(path: str) -> bool:
    try:
        rel = str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return False
    if rel.startswith("app/") and rel.endswith(".py"):
        return True
    name = rel.rsplit("/", 1)[-1]
    if name.startswith("requirements") or name.endswith(".lock.txt"):
        return True
    if rel in ("bin/local_asr_worker.py", "bin/local_alignment_worker.py"):
        return True
    if rel in ("app/config/config.yaml", "app/config/voices.yaml",
               "app/config/polyphone.yaml"):
        return True
    return False


def split_receipt_drift(receipt):
    """Split drifted receipt inputs into content vs renderer paths.

    Returns (content_drift, renderer_drift), each a sorted list of paths whose
    recorded sha256 no longer matches the file (or which are unreadable).
    An approval binds candidate bytes plus their reviews; renderer sources
    cannot retroactively change bytes already on disk, so renderer-only drift
    must not revoke an approval -- it only means the next rebuild will render
    with new code and needs a fresh review. Content drift invalidates.
    """
    content, renderer = [], []
    for p, sha in (receipt or {}).items():
        try:
            current = file_sha256(p)
        except OSError:
            current = None
        if current != sha:
            (renderer if _is_renderer(p) else content).append(p)
    return sorted(content), sorted(renderer)
