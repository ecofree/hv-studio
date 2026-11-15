"""Bind resumable work to implementation, effective settings, inputs and artifacts."""
import json
import copy
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from app import paths
from app.config.config import ROOT, get_settings, load_env
from app.services.provenance import file_sha256, fingerprint


def implementation_identity():
    files = list((ROOT / "app").rglob("*.py"))
    files += [ROOT / "requirements.txt", ROOT / "requirements.lock.txt"]
    files += [ROOT / "requirements.core.lock.txt", ROOT / "requirements.asr.lock.txt",
              ROOT / "bin/local_asr_worker.py", ROOT / "bin/local_alignment_worker.py",
              ROOT / "bin/studio_job.py", ROOT / "bin/studio_narrate.py",
              ROOT / "bin/production_worker.py"]
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

def _is_renderer(path: str) -> bool:
    try:
        rel = str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return False
    # Only modules whose responsibility is limited to producing media bytes
    # may be classified as renderer drift. QA, admission, orchestration, provider,
    # identity and approval code must invalidate/recheck the evidence they govern.
    renderer_modules = {
        "app/services/compose.py",
        "app/services/motion.py",
        "app/services/upscale.py",
        "app/services/overlay.py",
        "app/services/karaoke.py",
        "app/services/subtitle_layout.py",
        "app/services/word_timing.py",
        "app/services/scene_animation.py",
        "app/services/video_segment_cache.py",
    }
    if rel in renderer_modules:
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


# A shared data contract, not another scheduler or authoritative status store.
# The existing Studio request, queue row and V2 run retain their own records.
V2_STAGES = ('script', 'tts', 'delivery', 'quality', 'alignment',
             'images', 'timeline', 'compose', 'verify')
_task_execution = ContextVar('production_task_execution', default=None)
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_ATTEMPT = re.compile(r'^[0-9a-f]{32}$')


class TaskMismatch(ValueError):
    """A saved execution no longer belongs to the authorized task."""


def make_task(ep_id, *, origin, owner, request, inputs, budget_path, limits, stages):
    """Freeze submission identity without hashing outputs or mutable spending."""
    value = dict(schema_version=1, episode_id=ep_id, origin=origin, owner=owner,
                 task_id=fingerprint(['production-task-v1', origin, owner, ep_id]),
                 request_sha256=fingerprint(request), inputs_sha256=fingerprint(inputs),
                 implementation_sha256=implementation_identity(),
                 settings_sha256=fingerprint([get_settings().model_dump(mode='json'), load_env()]),
                 stages=list(stages),
                 budget=dict(path=str(Path(budget_path).resolve()) if budget_path else None,
                             limits=copy.deepcopy(limits)))
    value['spec_sha256'] = fingerprint(value)
    validate_task(value)
    return value


def validate_task(value):
    """Validate version and shape before trusting persisted paths or identifiers."""
    keys = {'schema_version','episode_id','origin','owner','task_id','request_sha256',
            'inputs_sha256','implementation_sha256','settings_sha256','stages','budget','spec_sha256'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise TaskMismatch('unsupported or malformed task identity')
    paths.validate_episode_id(value['episode_id'])
    if value['origin'] not in ('studio', 'batch', 'direct'):
        raise TaskMismatch('invalid task origin')
    if not isinstance(value['owner'], str) or not value['owner'] or len(value['owner']) > 4096:
        raise TaskMismatch('invalid task owner')
    for key in ('task_id','request_sha256','inputs_sha256','implementation_sha256','settings_sha256','spec_sha256'):
        if not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key]):
            raise TaskMismatch('invalid task '+key)
    body = {k: v for k, v in value.items() if k != 'spec_sha256'}
    if fingerprint(body) != value['spec_sha256']:
        raise TaskMismatch('task specification changed')
    if value['task_id'] != fingerprint(['production-task-v1', value['origin'], value['owner'], value['episode_id']]):
        raise TaskMismatch('task owner or episode changed')
    stages = value['stages']
    if (not isinstance(stages, list) or not stages or any(s not in V2_STAGES for s in stages)
            or len(set(stages)) != len(stages) or stages != sorted(stages, key=V2_STAGES.index)):
        raise TaskMismatch('invalid task stages')
    budget = value['budget']
    if not isinstance(budget, dict) or set(budget) != {'path','limits'}:
        raise TaskMismatch('invalid task budget binding')
    limits = budget['limits']
    if budget['path'] is None:
        if limits != {}: raise TaskMismatch('unbound task budget has limits')
    else:
        if not isinstance(budget['path'], str) or not Path(budget['path']).is_absolute():
            raise TaskMismatch('task budget path must be absolute')
        required = {'tts_requests','characters','image_requests'}
        if (not isinstance(limits, dict) or not required.issubset(limits)
                or set(limits) - required - {'image_retries'}
                or any(type(v) is not int or v < 0 for v in limits.values())):
            raise TaskMismatch('invalid task budget limits')
    return value


def require_same_task(saved, expected):
    validate_task(saved); validate_task(expected)
    if saved != expected:
        changed = [k for k in saved if saved[k] != expected[k] and k not in ('task_id','spec_sha256')]
        raise TaskMismatch('task binding changed ('+', '.join(changed)+'); submit a new task')


def execution_ref(task, attempt_id):
    validate_task(task)
    if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
        raise TaskMismatch('invalid execution attempt ID')
    return dict(task_id=task['task_id'], task_sha256=task['spec_sha256'], attempt_id=attempt_id)


def require_execution(value, expected):
    if not isinstance(value, dict) or value != expected:
        raise TaskMismatch('result belongs to a different task or execution attempt')


@contextmanager
def task_scope(task, attempt_id):
    """Pass the verified contract to V2/providers; no status or budget mutation."""
    reference = execution_ref(task, attempt_id)
    if task['implementation_sha256'] != implementation_identity():
        raise TaskMismatch('task implementation changed; submit a new task')
    if task['settings_sha256'] != fingerprint([get_settings().model_dump(mode='json'), load_env()]):
        raise TaskMismatch('task settings changed; submit a new task')
    existing = _task_execution.get()
    if existing is not None and existing[1] != reference:
        raise TaskMismatch('cannot replace an active task execution')
    token = _task_execution.set((copy.deepcopy(task), reference))
    try:
        yield copy.deepcopy(reference)
    finally:
        _task_execution.reset(token)


def current_execution():
    current = _task_execution.get()
    return copy.deepcopy(current[1]) if current else None


def current_task():
    current = _task_execution.get()
    return copy.deepcopy(current[0]) if current else None


def require_task_budget(path, limits):
    task = current_task()
    if task and task['budget'] != dict(path=str(Path(path).resolve()), limits=limits):
        raise TaskMismatch('budget differs from the authorized task; no ledger was changed')


def task_bound_run(fn):
    """Give direct V2 invocations the same contract; retain worker-owned identity.

    Direct calls are new invocations, not automatic resumptions of another job.
    Existing budget IDs and stage-specific media caches remain unchanged.
    """
    import inspect
    signature = inspect.signature(fn)
    @wraps(fn)
    def guarded(ep_id, *args, **kwargs):
        bound = signature.bind(ep_id, *args, **kwargs); bound.apply_defaults()
        params = bound.arguments
        stages = list(params['stages']) if params['stages'] is not None else list(V2_STAGES)
        task = current_task()
        if task:
            if task['episode_id'] != ep_id or task['stages'] != stages:
                raise TaskMismatch('V2 execution differs from authorized episode or stages')
            if params['budget'] is not None:
                require_task_budget(params['budget'].path, params['budget'].limits)
                bound.arguments['budget'] = params['budget'].for_current_task()
            return fn(*bound.args, **bound.kwargs)
        from app.managers import load
        from app.orchestrator import _load_script_md
        from app.services.provider_budget import budget_path_for, budget_limits
        text = params['script_md']
        if text is None: text = _load_script_md(load(ep_id))
        options = {k: params[k] for k in ('max_requests','max_characters','max_images','max_image_retries')}
        budget = params['budget']
        location = budget.path if budget is not None else (
            None if stages == ['verify'] else budget_path_for(paths.episode_dir(ep_id)/'production/v2',
                params['budget_id'] or [fingerprint(text), options]))
        limits = budget.limits if budget is not None else (budget_limits(options) if location else {})
        request = {k: v for k, v in params.items() if k not in ('budget','progress','script_md')}
        for k in ('timeline','voice_asset','external_master'):
            if request[k] is not None: request[k] = str(request[k])
        if request['image_keys'] is not None: request['image_keys'] = sorted(request['image_keys'])
        request['stages'] = stages
        request['source_sha256'] = fingerprint(text)
        # Direct calls do not promise later resumption. Stage-specific validators
        # still check full media/configuration evidence during this invocation.
        task = make_task(ep_id, origin='direct', owner=uuid.uuid4().hex,
                         request=request, inputs={'source':fingerprint(text)},
                         budget_path=location, limits=limits, stages=stages)
        with task_scope(task, uuid.uuid4().hex):
            if params['budget'] is not None:
                bound.arguments['budget'] = params['budget'].for_current_task()
            return fn(*bound.args, **bound.kwargs)
    return guarded
