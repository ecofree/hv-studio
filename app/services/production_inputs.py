"""Source-bound visual preparation; reuse explicit existing jobs first."""
from pathlib import Path
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint


def prepare(ep, plan_path, root):
    from app.orchestrator import build_image_jobs, _episode_dir
    from app.services.image import ImageJob
    from app.models import ImageGenRequest
    from app.config.config import get_settings
    plan = read(plan_path); episode_dir = _episode_dir(ep.id)
    scene_path = episode_dir/'scene_plan.json'
    prompt_path = episode_dir/'image_prompts.json'
    if scene_path.exists() != prompt_path.exists():
        raise ValueError('scene_plan.json and image_prompts.json must be supplied together')
    if scene_path.exists():
        jobs = build_image_jobs(ep.id, strict=True)
        from app.services.existing_images import include_registered
        jobs = include_registered(ep.id,jobs)
        scene = read(scene_path)
        if 'scenes' in scene:
            from app.services.production_timeline import scene_bindings
            source_manifest = {'alignment': [dict(unit_id=u['unit_id'], text=u['source_text']) for u in plan['units']]}
            scene_bindings(ep, source_manifest, scene)
        used = {s['image'] for s in scene.get('scenes', scene.get('unit_scenes', []))}
        if not used or not used.issubset({j.id for j in jobs}): raise ValueError('scene references missing image jobs')
    else:
        # Deterministic source-derived draft; semantic review remains explicit.
        # No added historical facts or silent LLM/provider calls.
        settings = get_settings(); style = settings.get_profile().image; primary = settings.image.primary
        storyboard = {'schema_version': 'source-storyboard-v1', 'source_sha256': plan['source_sha256'],
                      'review_required': True, 'unit_scenes': []}
        jobs, prompts = [], {}
        for i, unit in enumerate(plan['units']):
            key = f'unit-{i:03}'
            prompt = ('Historical documentary scene illustrating the following narration. '
                      'No written text, captions or watermark. Narration: '+unit['source_text']+' '+style.style_suffix+' '+style.negative_clause)
            req = ImageGenRequest(model=primary.model or primary.engine, prompt=prompt, size=primary.size, quality=primary.quality)
            jobs.append(ImageJob(key, req, root/'images'/(key+'.png')))
            prompts[key] = {'prompt': prompt, 'unit_id': unit['unit_id']}
            storyboard['unit_scenes'].append(dict(image=key, unit_id=unit['unit_id']))
        folder = root/'storyboards'/fingerprint([storyboard, prompts])
        scene_path = folder/'scene_plan.json'
        atomic_json(scene_path, storyboard); atomic_json(folder/'image_prompts.json', prompts)
    if not jobs: raise ValueError('no source-bound image jobs')
    for job in jobs:
        if not getattr(job,'imported',False): job.out_path = root/'images'/job.request_fingerprint()/job.out.name
    return jobs, scene_path


def preflight_media():
    import shutil
    from app.services.compose import check_font_available
    from app.services.local_asr import preflight
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): raise ValueError('ffmpeg and ffprobe are required before generation')
    check_font_available(); preflight()
