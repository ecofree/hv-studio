"""The shared CLI/API production entry point: immutable v2 candidates, explicit budgets."""
from pathlib import Path
from app.services import narration_pipeline as N, production_v2 as P
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services.voice_assets import resolve_voice_asset
from app.services.production_storage import production_storage


def check_narration_binding(manifest, plan, *, voice_asset=None):
    """Gate a narration manifest by how its takes were produced. Fails closed.

    A synthesized take is only meaningful against the request that produced it
    and the voice asset it was rendered with, so both are re-derived here. A take
    cut out of a recording has neither -- no voice asset, and no compile_request
    was ever issued for it -- so it is held to evidence that can actually be
    measured instead (see `external_narration.validate_external`).

    Writing a CosyVoice fingerprint onto an external take to satisfy the
    synthesized branch would make the manifest assert something untrue, which is
    the one failure mode this repo's gates exist to prevent. Hence the explicit
    `provenance` field and the fail-closed default.
    """
    provenance = manifest.get("provenance", "tts")
    if provenance == 'continuous_local_tts_candidate':
        from app.services.narration_continuity import validate_continuous
        continuous = manifest.get('continuous_tts', {})
        if continuous.get('lineage_version') != 1 or continuous.get('source_sha256') != plan['source_sha256']:
            raise ValueError('continuous narration lacks current source-bound lineage')
        validate_continuous(manifest)
        if ([(u['unit_id'],u['text']) for u in manifest['alignment']] !=
                [(u['unit_id'],u['display_text']) for u in plan['units']] or
                [t['unit_id'] for t in manifest['takes']] != [u['unit_id'] for u in plan['units']]):
            raise ValueError('continuous narration differs from current plan')
        return
    if provenance == "external_recording":
        from app.services.external_narration import validate_external
        validate_external(manifest)
        if len(manifest["takes"]) != len(plan["units"]):
            raise ValueError("external narration does not cover every plan unit; run stage tts")
        if [t["unit_id"] for t in manifest["takes"]] != [u["unit_id"] for u in plan["units"]]:
            raise ValueError("external narration unit order differs from the plan; run stage tts")
        return
    if provenance != "tts":
        raise ValueError(f"unknown narration provenance {provenance!r}; refusing to continue")
    _, asset = resolve_voice_asset(path=voice_asset)
    if any(t["voice_asset_version"] != asset["version"] for t in manifest["takes"]):
        raise ValueError("voice asset changed; run stage tts")
    from app.services.delivery import effective_unit
    if len(manifest['takes']) != len(plan['units']) or any(
            t['request_fp'] != fingerprint(N.compile_request(effective_unit(u, plan, asset), asset))
            for u, t in zip(plan['units'], manifest['takes'])):
        raise ValueError('compiled narration request changed; run stage tts')


@production_storage
def run(ep_id, stages=None, script_md=None, *, max_requests=0, max_characters=0,
        max_images=0, timeline=None, voice_asset=None, timeout=300, budget=None, budget_id=None,
        image_keys=None, external_master=None, max_image_retries=0,
        provider_attempts_per_image=None, progress=None, audio_mode="auto"):
    import os, time
    from app.managers import load
    from app.orchestrator import _episode_dir, _load_script_md, _episode_lock, _check_disk_space
    from app.config.config import get_settings
    from app.services.production_identity import artifacts_current
    order = ['script', 'tts', 'delivery', 'quality', 'alignment', 'images', 'timeline', 'compose', 'verify']
    requested = list(stages) if stages is not None else order.copy()
    allowed = set(order)
    if not requested or any(s not in allowed for s in requested) or len(set(requested)) != len(requested):
        raise ValueError(f"v2 stages must be unique and chosen from {sorted(allowed)}")
    if requested != sorted(requested, key=order.index):
        raise ValueError('stages must follow '+', '.join(order)+' order')
    if min(max_requests, max_characters, max_images, max_image_retries) < 0:
        raise ValueError("generation budgets cannot be negative")
    verify_only = requested == ["verify"]
    from app.services.studio_workflow import require_production
    if external_master and (Path(external_master).is_dir() or audio_mode=='parts'):
        from app.services.recording_input import bind, materialize
        from app.services.studio_workspace import config, library_path
        from app import paths
        location=(library_path(external_master) if config(paths.ROOT) and _episode_dir(ep_id).resolve().is_relative_to(paths.ROOT.resolve()) else Path(external_master))
        external_master=str(materialize(bind(location,audio_mode),_episode_dir(ep_id)/'production/v2/recordings'))
    require_production(ep_id, requested, external_master=external_master)
    from app.services.production_metrics import measure
    from app.services.acoustic_session import job_session
    with _episode_lock(ep_id), measure() as metrics, job_session() as acoustic:
        ep = load(ep_id)
        root = _episode_dir(ep_id) / "production/v2"
        root.mkdir(parents=True, exist_ok=True)
        _check_disk_space(root)
        state_path = root / "run.json"
        state = N.read(state_path) if state_path.exists() else {}
        text = _load_script_md(ep) if script_md is None else script_md
        if not text.strip(): raise ValueError("episode has no source narration")
        source_sha = fingerprint(text)
        if state.get("source_sha256") != source_sha:
            if verify_only: raise ValueError("no current v2 candidate for this source")
            prior = state.get('manifest_path')
            state = {"source_sha256": source_sha}
            if prior: state['previous_manifest_path'] = prior
        plan_path = Path(state.get("plan_path", root / "missing-plan.json"))
        if not plan_path.is_file():
            if verify_only: raise ValueError("missing v2 plan")
            source = root / "sources" / (source_sha + ".md")
            source.parent.mkdir(exist_ok=True)
            if source.exists() and source.read_text(encoding="utf-8") != text:
                raise ValueError("immutable source snapshot differs")
            if not source.exists(): source.write_text(text, encoding="utf-8")
            plan_path = N.prepare(source, root / "plans")
            state["plan_path"] = str(plan_path.resolve())
        plan = N.read(plan_path)
        from app.services.production_inputs import input_files
        submitted_image_inputs = input_files(ep_id)
        if plan["source_sha256"] != source_sha:
            raise ValueError("narration plan is not for the current source")
        # A selected repaired recording is the explicit episode audio authority.
        # This also prevents a later run from silently returning to paid TTS.
        if external_master is None:
            from app.services.narration_master import resolve as resolve_master
            external_master = resolve_master(root, plan_path)
        from app.services.studio_voice_contract import path_for as voice_contract_path, load as load_voice_contract
        locked_voice=voice_contract_path(ep_id).exists()
        if locked_voice:
            from app.services import studio_workflow as W
            load_voice_contract(voice_contract_path(ep_id),source_sha256=source_sha,voice=W.load(ep_id)['voice'])
        from app.services import studio_narration_handoff as H
        selected_narration=H.selected(ep_id)
        if selected_narration:
            if external_master and file_sha256(external_master)!=selected_narration['master_sha256']:
                raise ValueError('传入音轨与页面选定旁白不同，禁止静默替换')
            external_master=selected_narration['master']
        if 'tts' in requested and not locked_voice:
            from app.services.studio_workflow import require_voice_binding
            require_voice_binding(ep_id, external_master, voice_asset)
        completed, artifacts = [], [str(plan_path.resolve()), str((plan_path.parent / "document.json").resolve())]
        result = {"ep_id": ep_id, "pipeline_version": "production_v2", "requested_stages": requested,
                  "production_ready": False, "approved": False, "stages_done": completed,
                  "artifacts": artifacts, "final_mp4": None, "plan_path": str(plan_path.resolve()),
                  "stage_timings": metrics.rows}

        def save():
            state['updated_at'] = time.time()
            if not verify_only: atomic_json(state_path, state)
            if progress:
                progress(dict(stage=state.get('active_stage'), stages_done=list(completed),
                              stage_started_at=state.get('stage_started_at'),
                              progress=state.get('progress'), stage_timings=metrics.rows, budget_path=state.get('budget_path')))

        def report_progress(event):
            state['progress'] = event if isinstance(event, dict) else dict(message=str(event))
            save()

        def current_manifest():
            path = state.get("manifest_path")
            if not path: raise ValueError("missing v2 narration; run stage tts with an explicit budget")
            manifest = N.validate_assembly(path)
            N.require_usable(manifest)
            if manifest["plan_sha256"] != file_sha256(plan_path):
                raise ValueError("narration plan changed; run stage tts")
            check_narration_binding(manifest, plan, voice_asset=voice_asset)
            if selected_narration and manifest.get('studio_narration',{}).get('selection_sha256')!=selected_narration['selection_sha256']:
                raise ValueError('整集尚未接入新选用的旁白，请运行交接工序')
            if external_master and manifest.get('source_recording', {}).get('delivered_sha256') != file_sha256(external_master):
                raise ValueError('selected external narration changed; re-import the selected master')
            return path, manifest

        decoded_here = None
        cards_checked = None

        def prepare_cards(manifest):
            nonlocal cards_checked
            from app.services.overlay import path_for
            sidecar=path_for(ep_id)
            if cards_checked and sidecar.is_file() and file_sha256(sidecar)==cards_checked:return
            quality_path = state.get("quality_path")
            if not quality_path:
                raise ValueError("title cards need the current quality report")
            from app.services import overlay_author
            from app.services import studio_workflow
            studio_title = (studio_workflow.load(ep_id)['brief']['title']
                            if (_episode_dir(ep_id)/'studio/workflow.json').exists() else None)
            sidecar = overlay_author.ensure(
                ep_id,
                plan_path.parent / "document.json",
                quality_path,
                series=getattr(ep, "series", "") or "",
                episode_number=getattr(ep, "episode_number", 0) or 0,
                episode_title=getattr(ep, "title", "") or "",
                title=studio_title,
                total_ms=int(round(manifest["samples"] * 1000 / manifest["sample_rate"])),
                sample_rate=manifest["sample_rate"],
            )
            overlay_author.check_sidecar(sidecar,total_ms=round(manifest['samples']*1000/manifest['sample_rate']))
            cards_checked=file_sha256(sidecar)

        def current_candidate():
            manifest_path, manifest = current_manifest()
            path = state.get("receipt_path")
            if not path: raise ValueError("missing v2 candidate; run stage compose with a reviewed timeline")
            receipt = N.read(path)
            from app.services.production_inputs import input_files
            if 'image_inputs' in state and state['image_inputs'] != input_files(ep_id):
                raise ValueError('visual inputs changed; run images and timeline before compose')
            if receipt.get('kind') == 'metadata_amendment_v1':
                from app.services.production_revision import verify_amendment
                verify_amendment(path)
            if receipt["manifest_path"] != str(Path(manifest_path).resolve()):
                raise ValueError("candidate uses stale narration")
            from app.services.production_identity import split_receipt_drift
            if not artifacts_current(receipt["inputs"]):
                content_drift, renderer_drift = split_receipt_drift(receipt["inputs"])
                if content_drift:
                    raise ValueError("candidate or its inputs changed; run stage compose")
                # Renderer-only drift: bytes and reviews intact; verify and later
                # stages may proceed, and the drift is recorded on the result.
                result["renderer_changed"] = renderer_drift
            if file_sha256(receipt["candidate_path"]) != receipt["candidate_sha256"]:
                raise ValueError("candidate is missing or changed; run stage compose")
            if state.get('timeline_path'): P.validate_timeline(manifest, N.read(state['timeline_path']))
            return path, receipt

        if locked_voice and 'tts' in requested and not selected_narration:
            # Fail before any image/provider stage if a locked new draft has no
            # selected track. The existing registered full-episode path is kept.
            current_manifest()
        save()
        try:
            if not verify_only and budget is None:
                from app.services.provider_budget import for_run
                limits = dict(max_requests=max_requests, max_characters=max_characters, max_images=max_images,
                              max_image_retries=max_image_retries)
                budget = for_run(root, budget_id or [source_sha, limits], limits)
            if budget: state['budget_path'] = str(budget.path.resolve())
            for stage in requested:
                metrics.begin(stage)
                acoustic.reusable = stage in ('quality','alignment') or (stage in ('tts','delivery') and bool(external_master))
                if not acoustic.reusable: acoustic.close_worker()
                if not verify_only:
                    import psutil
                    state.pop('progress', None)
                    state.update(active_stage=stage, owner_pid=os.getpid(), owner_created_at=psutil.Process().create_time(),
                                 stage_started_at=time.time(), running=True)
                    save()
                if stage == "script":
                    pass  # Preparation uses the source-preserving parser above, never legacy frames.
                    # This stage is intentionally a no-op in v2 (not legacy's
                    # stage_script, which really splits frames): the plan was
                    # already built before the stage loop. Recorded so the
                    # result does not look like work was done.
                    result["script_noop"] = True
                elif stage == "images":
                    from app.services.production_inputs import prepare, input_files
                    from app.services.image import generate_many
                    if submitted_image_inputs != input_files(ep_id):
                        raise ValueError('visual inputs changed during this run; resubmit with the current briefs')
                    jobs, scene_path = prepare(ep, plan_path, root)
                    if not jobs: raise ValueError("no image jobs are defined")
                    # Scoped regeneration: regenerate only these keys.
                    selected = jobs
                    if image_keys:
                        wanted = set(image_keys)
                        unknown = sorted(wanted - {j.id for j in jobs})
                        if unknown:
                            raise ValueError(
                                "requested image keys are not defined for this episode: "
                                + ", ".join(unknown))
                        selected = [j for j in jobs if j.id in wanted]
                        result['scoped_image_keys'] = sorted(j.id for j in selected)
                    # v2 never replaces the historical episode's approved images.
                    # Each effective request gets its own candidate directory.
                    for job in selected:
                        if not getattr(job,'imported',False): job.out_path = root / "images" / job.request_fingerprint() / job.out.name
                    from app.services.studio_image_reuse import coverage_report
                    visual = coverage_report(ep_id, state=state, document=plan, master=external_master, scene_path=scene_path, prepared=selected)
                    if visual['status']=='invalid':
                        raise ValueError('画面安排与当前输入不符：'+visual['reason'])
                    result['visual_coverage'] = visual
                    if 'compose' in requested or (state.get('quality_path') and state.get('manifest_path')):
                        _, card_manifest = current_manifest()
                        prepare_cards(card_manifest)
                    else:
                        from app.services.overlay import path_for
                        from app.services.overlay_author import check_sidecar
                        if path_for(ep_id).is_file():check_sidecar(path_for(ep_id))
                    missing = [j for j in selected if not j.is_done()]
                    dispatch = list({j.request_fingerprint():j for j in missing if not j.out.with_suffix(j.out.suffix+'.response.json').exists()}.values())
                    unbriefed = [j.id for j in dispatch if getattr(j, 'source_brief_missing', False)]
                    if unbriefed:
                        raise ValueError('missing visual briefs for '+', '.join(unbriefed[:8])+
                                         '; write source-bound production/v2/source_image_briefs.json before generation')
                    if len(dispatch) > max_images + max_image_retries:
                        raise ValueError(f"image budget exceeded: {len(dispatch)} jobs need generation, budget {max_images}")
                    if 'tts' in requested and not external_master and not locked_voice:
                        asset_path, _ = resolve_voice_asset(path=voice_asset)
                        quote = N.generation_quote(plan_path, asset_path)
                        if quote['requests'] > max_requests or quote['characters'] > max_characters:
                            raise ValueError('TTS budget insufficient; stopped before any image generation')
                        if get_settings().get_profile().voice.engine != 'cosyvoice':
                            raise ValueError('production requires CosyVoice before paid generation')
                        budget.check_capacity(tts_requests=quote['requests'], characters=quote['characters'], image_requests=len(dispatch))
                    elif budget:
                        # With an external master the TTS stage issues zero synthesis
                        # requests, so quoting (and reserving) TTS budget here would
                        # block a run that cannot spend a cent on speech. Image
                        # budget is still preflighted; the skip is recorded, not hidden.
                        budget.check_capacity(image_requests=len(dispatch))
                        if external_master:
                            result['tts_budget_not_quoted'] = 'external master: no synthesis requests will be issued'
                    before_attempts = len(budget.snapshot()['attempts']) if budget else 0
                    generated = generate_many(
                        [j for j in selected if not getattr(j,'imported',False)],
                        budget=budget, **({'provider_attempts_per_job':provider_attempts_per_image} if provider_attempts_per_image is not None else {}),
                        **({"on_progress": report_progress} if progress else {}))
                    if budget:
                        from app.services.production_metrics import count
                        spent=[a for a in budget.snapshot()['attempts'][before_attempts:] if a['kind']=='image']
                        count('image_attempts',len(spent))
                        count('image_slot_wait_seconds',round(sum(a.get('queue_seconds',0) for a in spent),3))
                        count('image_request_seconds',round(sum(a['completed_at']-a['reserved_at'] for a in spent if a.get('completed_at')),3))
                        count('existing_images',len(selected)-len(missing))
                    generated.update({j.id:str(j.out) for j in selected if getattr(j,'imported',False) and j.is_done()})
                    artifacts.extend(str(j.out.resolve()) for j in selected if j.is_done())
                    artifacts.extend(str(j.receipt_path.resolve()) for j in selected if j.is_done())
                    # Reconcile every declared request, including unselected keys.
                    # A same-name old plate is not a match after its brief changes.
                    prior_assets = state.get('image_assets') or {}
                    selected_ids = {j.id for j in selected}
                    assets = {j.id: str(j.out.resolve()) for j in jobs if j.is_done() and
                              (j.id in selected_ids or prior_assets.get(j.id) == str(j.out.resolve()))}
                    if (assets != state.get('image_assets') or
                            str(scene_path.resolve()) != state.get('scene_plan_path')):
                        for stale in ('timeline_path', 'receipt_path', 'full_episode_registration'):
                            state.pop(stale, None)
                    state["image_assets"] = assets
                    incomplete = sorted(set(j.id for j in jobs) - set(assets))
                    if incomplete:
                        # Fail-closed but honest: a scoped run is allowed, and the
                        # episode is explicitly not yet buildable.
                        result['image_assets_incomplete'] = incomplete
                    # Record what this episode declares, so `timeline` can tell a
                    # legitimately partial scoped run from a complete one without
                    # re-running prepare() (which is not a pure reader).
                    state['declared_image_keys'] = sorted(j.id for j in jobs)
                    state['scene_plan_path'] = str(scene_path.resolve())
                    state['image_inputs'] = input_files(ep_id)
                    artifacts.append(str(scene_path.resolve()))
                    save()
                    if any(not generated.get(j.id) or not j.is_done() for j in selected):
                        reasons = [str(generated.get(j.id+'__error','invalid output')) for j in selected if not generated.get(j.id)]
                        raise ValueError("image generation incomplete: "+'; '.join(reasons[:3]))
                elif stage == "tts":
                    if selected_narration:
                        try:
                            manifest_path,manifest=current_manifest()
                        except (ValueError,OSError,KeyError):
                            manifest_path=H.build(ep_id,plan_path,root/'assemblies')
                            from app.services.production_revision import write_once
                            write_once(root/'run-history'/(fingerprint(state)+'.json'),state)
                            state['manifest_path']=str(Path(manifest_path).resolve())
                            for stale in ('receipt_path','timeline_path','alignment_path','quality_path','full_episode_registration'):
                                state.pop(stale,None)
                            manifest_path,manifest=current_manifest()
                        artifacts.extend([str(Path(manifest_path).resolve()),manifest['master_path']])
                        result['technical_quality']=manifest['technical_quality']
                        result['narration_provenance']='source_bound_local_recording'
                        result['voice_reused_without_generation']=True
                        completed.append(stage)
                        continue
                    if locked_voice:
                        # The accepted local narrator cannot fall through to a
                        # different provider. A registered track needs no ASR or TTS.
                        manifest_path,manifest=current_manifest()
                        if manifest.get('provenance')!='continuous_local_tts_candidate':
                            raise ValueError('confirmed local voice needs an explicitly registered narration track')
                        artifacts.extend([str(Path(manifest_path).resolve()),manifest['master_path']])
                        result['technical_quality']=manifest['technical_quality']
                        result['narration_provenance']=manifest['provenance']
                        result['voice_reused_without_generation']=True
                        completed.append(stage)
                        continue
                    # ASR is required either way: TTS checks each take's
                    # transcript, and external assembly needs it to locate the
                    # units inside the recording at all.
                    from app.services.asr_check import enabled as asr_enabled
                    if asr_enabled() and get_settings().get_profile().asr.backend == 'local-whisper':
                        from app.services.local_asr import preflight
                        preflight()
                    asset_path = None
                    if not external_master:
                        if get_settings().get_profile().voice.engine != "cosyvoice":
                            raise ValueError("v2 requires the configured CosyVoice engine; no fallback is allowed")
                        asset_path, _ = resolve_voice_asset(path=voice_asset)
                    try:
                        manifest_path, manifest = current_manifest()
                    except (ValueError, OSError, KeyError):
                        if external_master:
                            # A delivered recording is not a synthesis request, so
                            # it consumes no TTS budget and needs no voice asset.
                            # Every other stage downstream is unchanged.
                            from app.services.external_narration import build as build_external
                            manifest_path = build_external(
                                plan_path, external_master, root / "assemblies",
                                cache_dir=root / "assemblies" / "asr-cache" / file_sha256(external_master),
                                progress=report_progress, previous_manifest=state.get("manifest_path") or state.get("previous_manifest_path"))
                        else:
                            manifest_path = N.synthesize(plan_path, asset_path, max_requests=max_requests,
                                                         max_characters=max_characters, budget=budget)
                        state["manifest_path"] = str(Path(manifest_path).resolve())
                        state.pop("receipt_path", None)
                        state.pop("narration_progress", None)
                        manifest = N.validate_assembly(manifest_path)
                    artifacts.extend([str(Path(manifest_path).resolve()), manifest["master_path"]])
                    if asset_path is not None:
                        artifacts.append(str(asset_path))
                    result["technical_quality"] = manifest["technical_quality"]
                    result["narration_provenance"] = manifest.get("provenance", "tts")
                elif stage == 'delivery':
                    from app.services.delivery import apply
                    manifest_path, manifest = current_manifest()
                    if manifest.get('provenance') == 'external_recording':
                        # `apply` exists to insert planned pauses into a synthesized
                        # master. A recording already contains the pauses the
                        # director actually took, so running it would stack a
                        # second silence on top of each one. Recorded, not silent.
                        result['delivery_skipped'] = (
                            'external recording: pauses are already in the delivered master')
                    else:
                        _, asset = resolve_voice_asset(path=voice_asset)
                        new_manifest = apply(manifest_path, asset)
                        if str(new_manifest.resolve()) != str(Path(manifest_path).resolve()):
                            state['manifest_path'] = str(new_manifest.resolve())
                            for key in ('receipt_path','alignment_path','quality_path'): state.pop(key, None)
                        artifacts.extend([str(new_manifest.resolve()), N.read(new_manifest)['master_path']])
                elif stage == 'quality':
                    from app.services.production_quality import build, pending
                    manifest_path, _ = current_manifest()
                    report = Path(state.get('quality_path') or root/'missing-quality.json')
                    try:
                        if N.read(report)['manifest_sha256'] != file_sha256(manifest_path): raise ValueError('audio changed')
                        pending(report)
                    except (OSError,ValueError,KeyError):
                        report = build(manifest_path, root/'quality', **({'progress': report_progress} if progress else {}))
                    state['quality_path'] = str(report.resolve()); result['quality_pending_units'] = pending(report)
                    artifacts.append(str(report.resolve()))
                elif stage == 'alignment':
                    from app.services.forced_alignment import build,require_evidence
                    manifest_path, manifest = current_manifest()
                    existing=state.get('alignment_path')
                    reused=False
                    if existing and Path(existing).is_file():
                        cached=N.read(existing)
                        try:
                            require_evidence(manifest,dict(alignment_evidence_path=existing,subtitles=cached['subtitles']))
                            evidence=Path(existing);reused=True
                        except (ValueError,KeyError,OSError):pass
                    if not reused:evidence = build(manifest_path, root/'alignment', **({'progress': report_progress} if progress else {}))
                    result['alignment_reused']=reused
                    state['alignment_path'] = str(evidence.resolve()); artifacts.append(str(evidence.resolve()))
                elif stage == 'timeline':
                    from app.services.production_timeline import build
                    from app.services.production_inputs import prepare, input_files
                    manifest_path, _ = current_manifest()
                    if not all(state.get(k) for k in ('alignment_path','image_assets','scene_plan_path')):
                        raise ValueError('timeline requires images and alignment stages')
                    jobs, current_scene = prepare(ep, plan_path, root)
                    current_assets = {j.id: str(j.out.resolve()) for j in jobs if j.is_done()}
                    if (len(current_assets) != len(jobs) or current_assets != state['image_assets'] or
                            str(current_scene.resolve()) != state['scene_plan_path']):
                        raise ValueError('timeline image requests changed or are incomplete; run images for the current briefs')
                    state['image_inputs'] = input_files(ep_id)
                    # A scoped image run leaves image_assets deliberately partial.
                    # Fail here, before the timeline is built, and name the missing
                    # plates instead of letting build() report a generic
                    # scene-references-missing error. `declared_image_keys` was
                    # recorded by the images stage; without it we cannot tell a
                    # partial set from a complete one, so stay quiet.
                    declared = state.get('declared_image_keys')
                    if declared:
                        absent = sorted(set(declared) - set(state['image_assets']))
                        if absent:
                            raise ValueError(
                                f"timeline needs every declared plate; {len(absent)} missing: "
                                + ", ".join(absent[:8])
                                + (" ..." if len(absent) > 8 else "")
                                + " 处置：hv episode produce <id> --stage images"
                                  " --image-key <KEY> 逐张补齐")
                    made = build(ep, manifest_path, state['alignment_path'], state['image_assets'],
                                 state['scene_plan_path'], root/'timelines', quality_path=state.get('quality_path'))
                    state['timeline_path'] = str(made.resolve()); artifacts.append(str(made.resolve()))
                elif stage == "compose":
                    manifest_path, manifest = current_manifest()
                    timeline_path = Path(timeline or state.get("timeline_path", root / "timeline.json")).resolve()
                    if not timeline_path.is_file():
                        raise ValueError(f"missing reviewed shot/subtitle timeline: {timeline_path}")
                    state["timeline_path"] = str(timeline_path)
                    prepare_cards(manifest)
                    try:
                        receipt_path, receipt = current_candidate()
                        # A receipt written before the sidecar existed cannot
                        # know about it: artifacts_current() only checks paths
                        # already recorded, and adding a card changes pixels
                        # without touching any of them. Force the rebuild.
                        from app.services import overlay as _overlay
                        sidecar = _overlay.path_for(ep_id)
                        if sidecar.is_file() and str(sidecar.resolve()) not in receipt["inputs"]:
                            raise ValueError("candidate predates the overlay sidecar")
                        if str(timeline_path) not in receipt["inputs"]:
                            raise ValueError("different timeline selected")
                    except (ValueError, OSError, KeyError):
                        fresh_decode = []
                        receipt_path = P.render(manifest_path, timeline_path, root / "renders", timeout=timeout,
                                                on_verified=fresh_decode.append, **({"progress": report_progress} if progress else {}))
                        decoded_here = fresh_decode[0] if fresh_decode else None
                        state["receipt_path"] = str(receipt_path.resolve())
                        receipt = N.read(receipt_path)
                    artifacts.extend([str(receipt_path), receipt["candidate_path"], str(timeline_path)])
                    result["final_mp4"] = receipt["candidate_path"]
                elif stage == "verify":
                    from app.services.media_integrity import verify_for_run
                    receipt_path, receipt = current_candidate()
                    witness=None if result.get('renderer_changed') else decoded_here
                    result['verification']=verify_for_run(receipt,witness,timeout=timeout)
                    artifacts.extend([str(receipt_path), receipt["candidate_path"]])
                    result["final_mp4"] = receipt["candidate_path"]
                completed.append(stage)
                save()
        except (ValueError, RuntimeError, OSError) as exc:
            metrics.finish()
            import traceback
            result.update(status="BLOCKED", run_ok=False, error=str(exc),
                          error_phase=state.get('active_stage'), error_type=type(exc).__name__,
                          error_stack=traceback.format_tb(exc.__traceback__))
            if budget:
                snapshot=budget.snapshot()
                result["budget"]=dict(path=str(budget.path.resolve()),limits=snapshot["limits"],used=snapshot["used"])
            state.update(running=False, completed_at=time.time())
            state["last_result"] = result
            save()
            return result
        metrics.finish()
        result.update(status="CANDIDATE" if result["final_mp4"] else "STAGES_COMPLETE", run_ok=True,
                      content_review='pending')
        if not verify_only: state.update(running=False, completed_at=time.time())
        if state.get('manifest_path'):
            result['delivery_pending'] = [r for r in N.read(state['manifest_path']).get('delivery_dispositions', []) if r['status']!='executed']
        if budget:
            snapshot = budget.snapshot()
            result['budget'] = dict(path=str(budget.path.resolve()), limits=snapshot['limits'], used=snapshot['used'])
        state["last_result"] = result
        save()
        result["artifacts"] = sorted(set(artifacts + [str(state_path.resolve())]))
        return result
