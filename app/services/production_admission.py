"""Human input approval and machine eligibility for unattended production."""
from pathlib import Path
import time
from app.paths import episode_dir
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint, file_sha256


def inputs(ep_id):
    from app.managers import load
    from app.orchestrator import _load_script_md, build_image_jobs
    from app.services.script_document import parse_document
    folder = episode_dir(ep_id)
    text = _load_script_md(load(ep_id))
    if not text.strip(): raise ValueError('source narration missing')
    files = [folder/'scene_plan.json', folder/'image_prompts.json']
    if any(not p.is_file() for p in files):
        raise ValueError('explicit storyboard and image prompts required before unattended generation')
    jobs = build_image_jobs(ep_id, strict=True)
    from app.services.existing_images import include_registered
    jobs = include_registered(ep_id,jobs)
    if not jobs: raise ValueError('image jobs missing')
    document = parse_document(text)
    scene = read(files[0])
    if 'scenes' in scene:
        from app.services.production_timeline import scene_bindings
        scene_bindings(load(ep_id),{'alignment':[dict(unit_id=u['unit_id'],text=u['source_text']) for u in document['units']]},scene)
    used={s['image'] for s in scene.get('scenes',scene.get('unit_scenes',[]))}
    if not used or not used.issubset({j.id for j in jobs}):raise ValueError('scene references missing image jobs')
    unsupported = [c for c in document.get('delivery_cues', []) if c['intent'] != 'pause']
    from app.services.voice_assets import resolve_voice_asset
    _, asset = resolve_voice_asset()
    return dict(source_sha256=fingerprint(text), files={str(p): file_sha256(p) for p in files},
                image_requests={j.id:j.request_fingerprint() for j in jobs},
                voice_asset_version=asset['version'],
                direction_cues=[dict(cue_id=c['cue_id'],text=c['raw_text']) for c in unsupported],
                unsupported_cues=[c['cue_id'] for c in unsupported])


def review_materials(ep_id):
    """Return the actual source material shown to the reviewer, bound to inputs."""
    from app.managers import load
    from app.orchestrator import _load_script_md
    from app import paths
    value=inputs(ep_id);folder=episode_dir(ep_id)
    state_path=folder/'production/v2/run.json'
    state=read(state_path) if state_path.exists() else {}
    previews=[]
    for key,name in state.get('image_assets',{}).items():
        path=Path(name).resolve()
        if path.is_relative_to(paths.ROOT.resolve()) and path.is_file():
            previews.append(dict(id=key,path=str(path),sha256=file_sha256(path)))
    result=dict(input_fingerprint=fingerprint(value),source=_load_script_md(load(ep_id)),
                storyboard=read(folder/'scene_plan.json'),prompts=read(folder/'image_prompts.json'),
                directions=value['direction_cues'],previews=previews)
    if fingerprint(inputs(ep_id))!=result['input_fingerprint']:
        raise ValueError('review inputs changed while loading; reload material')
    return result


def record_approval(ep_id, reviewer, finding, direction_policy=None, *, expected_fingerprint=None):
    if not reviewer.strip() or not finding.strip(): raise ValueError('reviewer and findings are required')
    if direction_policy not in (None,'candidate_review_required'):
        raise ValueError('unknown direction execution policy')
    value = inputs(ep_id)
    if expected_fingerprint is not None and expected_fingerprint!=fingerprint(value):
        raise ValueError('review inputs changed; reload and review the current material')
    # Input approval never converts unsupported direction into implemented work.
    path = episode_dir(ep_id)/'production/input-approval.json'
    atomic_json(path, dict(input_fingerprint=fingerprint(value), reviewer=reviewer.strip(),
                           direction_policy=direction_policy,
                           direction_plan=[dict(cue_id=c,action='candidate_review_required') for c in value['unsupported_cues']] if direction_policy else [],
                           finding=finding.strip(), decision='approved_inputs', reviewed_at=time.time()))
    return path


def check(ep_id):
    blockers = []
    try:
        value = inputs(ep_id)
        path = episode_dir(ep_id)/'production/input-approval.json'
        review = read(path) if path.exists() else {}
        approved_inputs = (review.get('decision') == 'approved_inputs' and bool(review.get('reviewer'))
                and bool(review.get('finding')) and review.get('input_fingerprint') == fingerprint(value))
        if not approved_inputs:
            blockers.append('source facts and explicit storyboard require current input approval')
        if value['unsupported_cues']:
            state_path = episode_dir(ep_id)/'production/v2/run.json'
            state = read(state_path) if state_path.exists() else {}
            from app.services.delivery import pending
            planned = (approved_inputs and review.get('direction_policy')=='candidate_review_required'
                and review.get('direction_plan')==[dict(cue_id=c,action='candidate_review_required') for c in value['unsupported_cues']])
            if not planned and (state.get('source_sha256') != value['source_sha256'] or not state.get('manifest_path')
                    or pending(state['manifest_path'])):
                blockers.append('artistic directions require an explicit input execution plan or current content disposition')
        state_path = episode_dir(ep_id)/'production/v2/run.json'
        if state_path.exists():
            state = read(state_path)
            if state.get('quality_path') and state.get('source_sha256')==value['source_sha256']:
                from app.services.production_quality import pending
                if pending(state['quality_path']): blockers.append('existing audio findings require disposition')
        return dict(eligible=not blockers, blockers=blockers, input_fingerprint=fingerprint(value))
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        return dict(eligible=False, blockers=[str(exc)], input_fingerprint=None)
