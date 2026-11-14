"""Human input approval and machine eligibility for unattended production."""
from pathlib import Path
import time
from app.paths import episode_dir
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint, file_sha256


def approved_image_requests(ep_id):
    path = episode_dir(ep_id)/'production/input-approval.json'
    review = read(path) if path.is_file() else {}
    if review.get('decision') != 'approved_inputs':
        return {}
    if 'image_requests' in review:
        from app.services.production_inputs import input_files
        if 'image_source_files' in review and review['image_source_files'] != input_files(ep_id):
            return {}
        return review['image_requests']
    # Earlier approvals stored only the aggregate hash. Recover the previous
    # default only if the entire original review still matches.
    try:
        original = inputs(ep_id, _legacy=True)
    except (FileNotFoundError, ValueError, RuntimeError):
        return {}
    return original['image_requests'] if fingerprint(original) == review.get('input_fingerprint') else {}


def _voice_asset_version_for_inputs(folder):
    """External-recording admissions must not depend on an unrelated TTS voice.

    The caller has already verified external-audio-binding.json. None is an
    explicit external-recording provenance value, not a missing TTS setting.
    """
    if (Path(folder)/'production/external-audio-binding.json').is_file():
        return None
    from app.services.voice_assets import resolve_voice_asset
    _, asset = resolve_voice_asset()
    return asset['version']


def inputs(ep_id, *, _legacy=False):
    from app.managers import load
    from app.orchestrator import _load_script_md, build_image_jobs
    from app.services.script_document import parse_document
    folder = episode_dir(ep_id)
    text = _load_script_md(load(ep_id))
    if not text.strip(): raise ValueError('source narration missing')
    files = [folder/'scene_plan.json', folder/'image_prompts.json']
    if any(not p.is_file() for p in files):
        raise ValueError('explicit storyboard and image prompts required before unattended generation')
    # 外部音频集把音轨绑定一并纳入指纹：换文件、改帧序、动切分都会让已记录的
    # 审批立刻失效。v2 的 per-unit TTS 管线本就有 sha256 绑定，legacy/外部
    # 音频此前只有 Path(...).exists()（2026-09-29 实测缺口）。
    binding = folder/'production/external-audio-binding.json'
    if binding.is_file():
        from app.services.external_audio import verify_binding
        chk = verify_binding(load(ep_id))
        if not chk['ok']:
            raise ValueError('external audio binding invalid: ' + '; '.join(chk['fails']))
        files.append(binding)
    jobs = build_image_jobs(ep_id, strict=True, create_output_dir=False)
    from app.services.existing_images import include_registered
    jobs = include_registered(ep_id,jobs)
    if not jobs: raise ValueError('image jobs missing')
    document = parse_document(text)
    scene = read(files[0])
    from app.services.studio_image_reuse import validate_scene_source
    validate_scene_source(scene,document)
    if 'scenes' in scene:
        from app.services.production_timeline import scene_bindings
        scene_bindings(load(ep_id),{'alignment':[dict(unit_id=u['unit_id'],text=u['source_text']) for u in document['units']]},scene)
    used={s['image'] for s in scene.get('scenes',scene.get('unit_scenes',[]))}
    if not used or not used.issubset({j.id for j in jobs}):raise ValueError('scene references missing image jobs')
    unsupported = [c for c in document.get('delivery_cues', []) if c['intent'] != 'pause']
    briefs = read(files[1])
    if _legacy:
        from app.config.config import get_settings
        requests = {}
        for job in jobs:
            brief = briefs.get(job.id, {})
            if getattr(job, 'imported', False) or (isinstance(brief, dict) and brief.get('model')):
                requests[job.id] = job.request_fingerprint()
            else:
                requests[job.id] = job.request_fingerprint(model=get_settings().image.primary.engine)
    else:
        from app.services.scene_models import plan_scene_models
        from app.services.studio_image_reuse import apply
        directing = folder/'directing.json'
        jobs = apply(ep_id, jobs)
        directions = read(directing) if directing.is_file() else {}
        used.update(e['image_id'] for e in directions.get('visual_events', []))
        # Match the production slate's allocation before reviewing any unused briefs.
        jobs.sort(key=lambda j: j.id not in used)
        plan_scene_models(folder/'production/v2', jobs, briefs, scene=scene,
                          directing=directions,
                          approved=approved_image_requests(ep_id), persist=False)
        requests = {j.id: j.request_fingerprint() for j in jobs}
    voice_asset_version = _voice_asset_version_for_inputs(folder)
    return dict(source_sha256=fingerprint(text), files={str(p): file_sha256(p) for p in files},
                image_requests=requests,
                voice_asset_version=voice_asset_version,
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
    from app.services.production_inputs import input_files
    image_sources = input_files(ep_id)
    value = inputs(ep_id)
    if expected_fingerprint is not None and expected_fingerprint!=fingerprint(value):
        raise ValueError('review inputs changed; reload and review the current material')
    # Output binding: bind the approval to the exact bytes of the delivered mp4.
    # Without it, re-rendering or swapping the file keeps eligible=true -- the
    # approval only described the inputs. v2 has the same guarantee on its
    # published pointer; legacy-preview had none (2026-09-29).
    from app.services import output_binding
    output = None
    try:
        output = output_binding.bind(ep_id)
    except (FileNotFoundError, OSError):
        # A candidate can be approved before it is composed. The output binding
        # is then established at the next approval; do not fabricate one.
        pass
    # Input approval never converts unsupported direction into implemented work.
    path = episode_dir(ep_id)/'production/input-approval.json'
    atomic_json(path, dict(input_fingerprint=fingerprint(value), reviewer=reviewer.strip(),
                           image_requests=value.get('image_requests', {}), image_source_files=image_sources,
                           direction_policy=direction_policy,
                           direction_plan=[dict(cue_id=c,action='candidate_review_required') for c in value['unsupported_cues']] if direction_policy else [],
                           output_sha256=(output or {}).get('sha256'),
                           output_path=(output or {}).get('path'),
                           output_frames=(output or {}).get('frames'),
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
            state_path = episode_dir(ep_id) / 'production/v2/run.json'
            state = read(state_path) if state_path.exists() else {}
            from app.services.delivery import pending
            planned = (approved_inputs and review.get('direction_policy')=='candidate_review_required'
                and review.get('direction_plan')==[dict(cue_id=c,action='candidate_review_required') for c in value['unsupported_cues']])
            if not planned and (state.get('source_sha256') != value['source_sha256'] or not state.get('manifest_path')
                    or pending(state['manifest_path'])):
                blockers.append('artistic directions require an explicit input execution plan or current content disposition')
        # The approval must describe the bytes that were reviewed. If the mp4 was
        # replaced or re-rendered afterwards, the review no longer applies.
        bound = review.get('output_sha256')
        if bound:
            from app.services import output_binding
            chk = output_binding.verify(ep_id)
            if not chk['ok']:
                blockers.append('delivered video no longer matches the approved bytes: '
                                + '; '.join(chk['fails']))
        state_path = episode_dir(ep_id)/'production/v2/run.json'
        if state_path.exists():
            state = read(state_path)
            if state.get('quality_path') and state.get('source_sha256')==value['source_sha256']:
                from app.services.production_quality import pending
                if pending(state['quality_path']): blockers.append('existing audio findings require disposition')
        return dict(eligible=not blockers, blockers=blockers, input_fingerprint=fingerprint(value))
    except (OSError, ValueError, KeyError, RuntimeError, TypeError) as exc:
        return dict(eligible=False, blockers=[str(exc)], input_fingerprint=None)
