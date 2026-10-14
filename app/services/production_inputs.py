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
        # The fallback used to skip every pre-spend gate: a placeholder narration
        # or an illegal size/quality would burn quota before anyone noticed.
        # Screen here with the same wordlists as build_image_jobs (length and
        # placeholder rules do NOT transfer: narration is not a prompt, and
        # short units are legitimate). Style-word hits block; text-request
        # hints only warn.
        from app.orchestrator import _word_hit, _STYLE_BANNED_LATIN, _STYLE_BANNED_CJK
        from app.orchestrator import _TEXT_REQUEST_CJK, _TEXT_REQUEST_LATIN
        from app.services.image import validate_image_params
        bad_params = validate_image_params(primary.size, primary.quality,
                                           primary.model or primary.engine)
        if bad_params:
            raise ValueError("image params invalid before any paid call: " + "; ".join(bad_params))
        storyboard = {'schema_version': 'source-storyboard-v1', 'source_sha256': plan['source_sha256'],
                      'review_required': True, 'unit_scenes': []}
        jobs, prompts = [], {}
        blocked, warned = [], []
        for i, unit in enumerate(plan['units']):
            text = unit['source_text'] or ""
            norm = " ".join(text.split())
            if not norm:
                blocked.append((f'unit-{i:03}', 'empty narration'))
                continue
            low = norm.lower()
            hit = _word_hit(low, _STYLE_BANNED_LATIN) or next(
                (w for w in _STYLE_BANNED_CJK if w in norm), None)
            if hit:
                blocked.append((f'unit-{i:03}', f'风格词疑似与 house look 冲突（{hit}）'))
                continue
            for w in _TEXT_REQUEST_CJK:
                if w in norm:
                    warned.append((f'unit-{i:03}', f'疑似要求图中渲染文字（{w}）'))
                    break
            else:
                hit = _word_hit(low, _TEXT_REQUEST_LATIN)
                if hit:
                    warned.append((f'unit-{i:03}', f'疑似要求图中渲染文字（{hit}）'))
        if blocked:
            raise ValueError(
                f"{len(blocked)} 个 unit 的旁白含风格词，不适合直接做生图主体（先写 scene_plan）： "
                + ", ".join(f"{k}({r})" for k, r in blocked[:8]))
        if warned:
            print(f"  [production_inputs] {len(warned)} 个 unit 疑似要求图中文字，"
                  f"已保留但请复核: {warned[:5]}")
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
