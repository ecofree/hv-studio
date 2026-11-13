"""Source-bound visual preparation; reuse explicit existing jobs first."""
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services.scene_models import plan_scene_models


def input_files(ep_id):
    """Authored visual inputs, including absence so newly added briefs invalidate reuse."""
    from app.paths import episode_dir
    directory = episode_dir(ep_id)
    return {str((directory/name).resolve()): file_sha256(directory/name) if (directory/name).is_file() else None
            for name in ('scene_plan.json', 'image_prompts.json', 'directing.json',
                         'production/v2/source_image_briefs.json', 'studio/image-reuse.json')}


def prepare(ep, plan_path, root, *, preview=False):
    from app.orchestrator import build_image_jobs, _episode_dir
    from app.services.image import ImageJob
    from app.models import ImageGenRequest
    from app.config.config import get_settings
    plan = plan_path if isinstance(plan_path, dict) else read(plan_path)
    episode_dir = _episode_dir(ep.id)
    scene_path = episode_dir/'scene_plan.json'
    prompt_path = episode_dir/'image_prompts.json'
    directing_path = episode_dir/'directing.json'
    directing = read(directing_path) if directing_path.is_file() else {}
    if scene_path.exists() != prompt_path.exists():
        raise ValueError('scene_plan.json and image_prompts.json must be supplied together')
    if scene_path.exists():
        jobs = build_image_jobs(ep.id, strict=True, create_output_dir=not preview)
        from app.services.existing_images import include_registered
        jobs = include_registered(ep.id,jobs)
        scene = read(scene_path)
        image_briefs = read(prompt_path)
        from app.services.studio_image_reuse import validate_scene_source
        validate_scene_source(scene, plan)
        if 'scenes' in scene:
            from app.services.production_timeline import scene_bindings
            source_manifest = {'alignment': [dict(unit_id=u['unit_id'], text=u['source_text']) for u in plan['units']]}
            scene_bindings(ep, source_manifest, scene)
        used = {s['image'] for s in scene.get('scenes', scene.get('unit_scenes', []))}
        used.update(e['image_id'] for e in directing.get('visual_events', []))
        local={k for k,v in scene.get('images',{}).items() if v.get('local_only')}
        unmet=sorted((used&local)-{j.id for j in jobs if getattr(j,'imported',False)})
        if unmet:raise ValueError('请先登记分镜中的本地素材：'+'、'.join(unmet))
        if not used or not used.issubset({j.id for j in jobs}): raise ValueError('scene references missing image jobs')
        jobs=[j for j in jobs if j.id in used]
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
        # Authored briefs are inputs; generated storyboard snapshots are outputs.
        brief_path = root/'source_image_briefs.json'
        briefs = read(brief_path) if brief_path.exists() else {}
        if briefs:
            if briefs.get('source_sha256') != plan['source_sha256']:
                raise ValueError('image briefs belong to another narration source')
            unknown = set(briefs.get('images', {})) - {f'unit-{i:03}' for i in range(len(plan['units']))}
            if unknown:
                raise ValueError('unknown image brief keys: '+', '.join(sorted(unknown)))
            storyboard['image_briefs_sha256'] = file_sha256(brief_path)
        blocked, warned = [], []
        for i, unit in enumerate(plan['units']):
            text = briefs.get('images', {}).get(f'unit-{i:03}', {}).get('en', unit['source_text']) or ""
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
            if key in briefs.get('images', {}):
                from app.orchestrator import screen_prompt, _compose_prompt
                en = briefs['images'][key]['en']
                ok, reason, norm = screen_prompt(key, en)
                if not ok or reason.startswith('风格词'):
                    raise ValueError(f'image brief {key}: {reason}')
                prompt = _compose_prompt(norm, style.style_suffix, style.negative_clause)
            model=briefs.get('images',{}).get(key,{}).get('model') or primary.model or primary.engine
            req = ImageGenRequest(model=model, prompt=prompt, size=primary.size, quality=primary.quality)
            job = ImageJob(key, req, root/'images'/(key+'.png'))
            # Old narration-derived fingerprints remain readable for cache recovery.
            # New paid work must use an authored visual brief, not raw narration.
            job.source_brief_missing = key not in briefs.get('images', {})
            jobs.append(job)
            prompts[key] = {'prompt': prompt, 'unit_id': unit['unit_id']}
            storyboard['unit_scenes'].append(dict(image=key, unit_id=unit['unit_id']))
        folder = root/'storyboards'/fingerprint([storyboard, prompts])
        scene_path = folder/'scene_plan.json'
        scene = storyboard
        image_briefs = briefs.get('images', {})
        if not preview:
            atomic_json(scene_path, storyboard); atomic_json(folder/'image_prompts.json', prompts)
    if not jobs: raise ValueError('no source-bound image jobs')
    from app.services.studio_image_reuse import apply
    from app.services.production_admission import approved_image_requests
    jobs = apply(ep.id, jobs)
    plan_scene_models(root, jobs, image_briefs, scene=scene, directing=directing,
                      approved=approved_image_requests(ep.id), persist=not preview)
    for job in jobs:
        if not getattr(job,'imported',False):
            job.out_path = root/'images'/job.request_fingerprint()/job.out.name
            job.dispatch_directory = root/'images'/'.dispatch'
        if not preview and not getattr(job,'imported',False):
            quarantined=root/'images/_quarantine'/job.request_fingerprint()/(job.out.name+'.response.json')
            if quarantined.exists() and not job.out.with_suffix(job.out.suffix+'.response.json').exists() and not job.is_done():
                from app.services.image import recover_saved_response
                recover_saved_response(job,quarantined)
    return jobs, scene_path
