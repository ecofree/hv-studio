"""Choose the model for each new plate and recover the identity of paid requests."""
from collections import Counter, defaultdict

from app.config.config import get_settings
from app.managers.relay import get_relay
from app.services.image import _available
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint


def _shot_roles(scene, directing):
    uses = defaultdict(list)
    for row in scene.get('scenes', scene.get('unit_scenes', [])):
        uses[row['image']].append(row)
    for row in directing.get('visual_events', []):
        uses[row['image_id']].append(row)
    roles = {}
    for key, rows in uses.items():
        if any(max((r.get('motion') or {}).get('start_zoom', 1),
                   (r.get('motion') or {}).get('end_zoom', 1)) >= 1.6 for r in rows):
            roles[key] = 'large_crop'
    return roles


def _recorded_model(job, candidates, images, folders, attempts, approved):
    # Check the exact request, including reference bytes. A different shot in a
    # partially generated episode must still be allowed to use the current policy.
    receipt = read(job.receipt_path).get('request_fingerprint') if job.receipt_path.is_file() else None
    if not (folders or attempts or approved or receipt):
        return None
    fallback = None
    for model in candidates:
        key = job.request_fingerprint(model=model)
        if key == receipt:
            return model
        if key in folders:
            for directory in (images/key, images/'_quarantine'/key):
                if (directory/(job.out.name + '.request.json')).is_file() or (directory/(job.out.name + '.response.json')).is_file():
                    return model
                if any((directory/(job.out.name + suffix)).is_file() for suffix in
                       ('', '.dispatch-stop.json', '.response.unparsed-response.json')):
                    fallback = fallback or model
        if key == approved:
            fallback = model
        elif not fallback and attempts and job.provider_fingerprint(model=model) in attempts:
            fallback = model
    return fallback


def plan_scene_models(root, jobs, briefs, *, scene=None, directing=None, approved=None, persist=True):
    settings = get_settings()
    primary = settings.image.primary
    policy = settings.image.scene_models
    known = [m for c in settings.channels for m in (c.models or [c.model])]
    normal = [primary.model or primary.engine, *(m for m in known if m not in policy.large_crop), primary.engine]
    roles = _shot_roles(scene or {}, directing or {})
    snapshot = root/'scene-models.json'
    saved = read(snapshot) if snapshot.is_file() else {}
    updates = {}
    images = root/'images'
    folders = {p.name for parent in (images, images/'_quarantine') if parent.is_dir()
               for p in parent.iterdir() if p.is_dir() and len(p.name) == 64}
    attempts = set()
    reserved = set()
    for path in (root/'budgets').glob('*.json'):
        for attempt in read(path).get('attempts', []):
            if attempt.get('kind') == 'image':
                attempts.add(attempt['request_fingerprint'])
                if attempt['status'] == 'reserved':
                    reserved.add(attempt['request_fingerprint'])
    routes = None
    channel_load, model_load = Counter(), Counter()
    duplicates = {}
    for job in jobs:
        brief = briefs.get(job.id, {})
        if getattr(job, 'imported', False):
            continue
        if isinstance(brief, dict) and brief.get('model'):
            job.awaiting_response = bool(reserved and job.provider_fingerprint() in reserved)
            continue
        previous = saved.get(job.id, {})
        base = job.request_fingerprint(model=primary.engine)
        pinned = previous.get('model') if previous.get('base') == base else None
        models = dict.fromkeys(m for m in [pinned, primary.engine, *normal, *policy.large_crop, *known] if m)
        recorded = _recorded_model(job, models, images, folders, attempts, (approved or {}).get(job.id))
        if recorded:
            job.model_bound = True
            job.req = job.req.model_copy(update={'model': recorded})
            job.awaiting_response = bool(reserved and job.provider_fingerprint() in reserved)
            continue
        if getattr(job, 'source_brief_missing', False):
            continue
        if routes is None:
            enabled = [c for c in settings.channels if c.enabled]
            relay = get_relay() if enabled else None
            routes = [c for c in enabled if any(_available(relay, c, m) for m in (c.models or [c.model]))]
        role = roles.get(job.id, 'normal')
        pool = list(dict.fromkeys(policy.large_crop if role == 'large_crop' else normal))
        suppliers = [c for c in routes if any(c.supports(m) for m in pool)]
        if not suppliers:
            pool = list(dict.fromkeys(normal))
            suppliers = [c for c in routes if any(c.supports(m) for m in pool)]
        chosen = duplicates.get((base, role))
        if chosen is None and suppliers:
            eligible = [m for m in pool if any(c.supports(m) for c in suppliers)]
            if previous.get('strategy') == 'shared_slots' and previous.get('role') == role and pinned in eligible:
                eligible = [pinned]
            supplier = min((c for c in suppliers if any(c.supports(m) for m in eligible)),
                           key=lambda c: channel_load[c.name] / c.max_concurrency)
            chosen = min((m for m in eligible if supplier.supports(m)), key=lambda m: model_load[m])
            channel_load[supplier.name] += 1
            model_load[chosen] += 1
            duplicates[base, role] = chosen
        chosen = chosen or next((m for m in pool if any(c.supports(m) for c in settings.channels)), pool[0])
        job.req = job.req.model_copy(update={'model': chosen})
        choice = dict(base=base, brief=fingerprint(brief), model=chosen, role=role, strategy='shared_slots')
        if choice != previous:
            updates[job.id] = choice
    if persist and updates:
        from app.services.process_lock import file_lock
        with file_lock(snapshot.with_suffix('.lock')):
            latest = read(snapshot) if snapshot.is_file() else {}
            atomic_json(snapshot, {**latest, **updates})
