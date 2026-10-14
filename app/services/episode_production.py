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
        image_keys=None, external_master=None, max_image_retries=0):
    import os, time
    from app.managers import load
    from app.orchestrator import _episode_dir, _load_script_md, _episode_lock, _check_disk_space
    from app.config.config import get_settings
    from app.services.production_identity import artifacts_current
    order = ['script', 'images', 'tts', 'delivery', 'quality', 'alignment', 'timeline', 'compose', 'verify']
    requested = list(stages) if stages is not None else order.copy()
    allowed = set(order)
    if not requested or any(s not in allowed for s in requested) or len(set(requested)) != len(requested):
        raise ValueError(f"v2 stages must be unique and chosen from {sorted(allowed)}")
    if requested != sorted(requested, key=order.index):
        raise ValueError('stages must follow '+', '.join(order)+' order')
    if min(max_requests, max_characters, max_images, max_image_retries) < 0:
        raise ValueError("generation budgets cannot be negative")
    verify_only = requested == ["verify"]
    with _episode_lock(ep_id):
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
            state = {"source_sha256": source_sha}
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
        if plan["source_sha256"] != source_sha:
            raise ValueError("narration plan is not for the current source")
        completed, artifacts = [], [str(plan_path.resolve()), str((plan_path.parent / "document.json").resolve())]
        result = {"ep_id": ep_id, "pipeline_version": "production_v2", "requested_stages": requested,
                  "production_ready": False, "approved": False, "stages_done": completed,
                  "artifacts": artifacts, "final_mp4": None, "plan_path": str(plan_path.resolve())}

        def save():
            if not verify_only: atomic_json(state_path, state)

        def current_manifest():
            path = state.get("manifest_path")
            if not path: raise ValueError("missing v2 narration; run stage tts with an explicit budget")
            manifest = N.validate_assembly(path)
            N.require_usable(manifest)
            if manifest["plan_sha256"] != file_sha256(plan_path):
                raise ValueError("narration plan changed; run stage tts")
            check_narration_binding(manifest, plan, voice_asset=voice_asset)
            return path, manifest

        def current_candidate():
            manifest_path, manifest = current_manifest()
            path = state.get("receipt_path")
            if not path: raise ValueError("missing v2 candidate; run stage compose with a reviewed timeline")
            receipt = N.read(path)
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

        save()
        try:
            if not verify_only and budget is None:
                from app.services.provider_budget import for_run
                limits = dict(max_requests=max_requests, max_characters=max_characters, max_images=max_images,
                              max_image_retries=max_image_retries)
                budget = for_run(root, budget_id or [source_sha, limits], limits)
            if budget: state['budget_path'] = str(budget.path.resolve())
            for stage in requested:
                if not verify_only:
                    import psutil
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
                    from app.services.production_inputs import prepare, preflight_media
                    from app.services.image import generate_many
                    jobs, scene_path = prepare(ep, plan_path, root)
                    if not jobs: raise ValueError("no image jobs are defined")
                    # Scoped regeneration: regenerate only these keys.
                    #
                    # `--image-key` used to be consumed exclusively by the
                    # legacy-preview branch of cli.py, so on the v2 path -- the only
                    # path that can still spend money -- it was silently dropped.
                    # That is the exact "同坑 #38" shape this repo warns about
                    # three times in cli.py: a parameter that exists, is
                    # documented in --help, and changes nothing. Regenerating one
                    # plate meant paying for every missing plate, and the budget
                    # was consumed before the wanted image was reached (measured on
                    # caomin-01, 2026-09-29: a 3-slot budget split across 4 plates).
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
                    missing = [j for j in selected if not j.is_done()]
                    dispatch = [j for j in missing if not j.out.with_suffix(j.out.suffix+'.response.json').exists()]
                    if len(dispatch) > max_images:
                        raise ValueError(f"image budget exceeded: {len(dispatch)} jobs need generation, budget {max_images}")
                    if 'tts' in requested and not external_master:
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
                    if any(s in requested for s in ('tts','alignment','timeline','compose')): preflight_media()
                    generated = generate_many([j for j in selected if not getattr(j,'imported',False)], budget=budget)
                    generated.update({j.id:str(j.out) for j in selected if getattr(j,'imported',False) and j.is_done()})
                    if any(not generated.get(j.id) or not j.is_done() for j in selected):
                        reasons = [str(generated.get(j.id+'__error','invalid output')) for j in selected if not generated.get(j.id)]
                        raise ValueError("image generation incomplete: "+'; '.join(reasons[:3]))
                    artifacts.extend(str(j.out.resolve()) for j in selected)
                    artifacts.extend(str(j.receipt_path.resolve()) for j in selected)
                    # Merge, never replace: a scoped run must not erase the plates
                    # it did not touch, or `timeline` would fail later with a
                    # missing-image error that points at the wrong stage.
                    assets = dict(state.get('image_assets') or {})
                    assets.update({j.id: str(j.out.resolve()) for j in selected})
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
                    artifacts.append(str(scene_path.resolve()))
                elif stage == "tts":
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
                            def _progress(message):
                                state["narration_progress"] = message
                                save()
                            manifest_path = build_external(
                                plan_path, external_master, root / "assemblies",
                                cache_dir=root / "assemblies" / "asr-cache",
                                progress=_progress)
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
                    report = build(manifest_path, root/'quality')
                    state['quality_path'] = str(report.resolve()); result['quality_pending_units'] = pending(report)
                    artifacts.append(str(report.resolve()))
                elif stage == 'alignment':
                    from app.services.forced_alignment import build
                    manifest_path, _ = current_manifest()
                    evidence = build(manifest_path, root/'alignment')
                    state['alignment_path'] = str(evidence.resolve()); artifacts.append(str(evidence.resolve()))
                elif stage == 'timeline':
                    from app.services.production_timeline import build
                    manifest_path, _ = current_manifest()
                    if not all(state.get(k) for k in ('alignment_path','image_assets','scene_plan_path')):
                        raise ValueError('timeline requires images and alignment stages')
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
                    manifest_path, _ = current_manifest()
                    timeline_path = Path(timeline or state.get("timeline_path", root / "timeline.json")).resolve()
                    if not timeline_path.is_file():
                        raise ValueError(f"missing reviewed shot/subtitle timeline: {timeline_path}")
                    state["timeline_path"] = str(timeline_path)
                    try:
                        receipt_path, receipt = current_candidate()
                        if str(timeline_path) not in receipt["inputs"]:
                            raise ValueError("different timeline selected")
                    except (ValueError, OSError, KeyError):
                        receipt_path = P.render(manifest_path, timeline_path, root / "renders", timeout=timeout)
                        state["receipt_path"] = str(receipt_path.resolve())
                        receipt = N.read(receipt_path)
                    artifacts.extend([str(receipt_path), receipt["candidate_path"], str(timeline_path)])
                    result["final_mp4"] = receipt["candidate_path"]
                elif stage == "verify":
                    from app.services.media_integrity import verify_media
                    receipt_path, receipt = current_candidate()
                    verify_media(receipt["candidate_path"], expected_frames=receipt["expected_frames"],
                                 expected_samples=receipt["samples"], sample_rate=receipt["sample_rate"], timeout=timeout)
                    artifacts.extend([str(receipt_path), receipt["candidate_path"]])
                    result["final_mp4"] = receipt["candidate_path"]
                completed.append(stage)
                save()
        except (ValueError, RuntimeError, OSError) as exc:
            result.update(status="BLOCKED", run_ok=False, error=str(exc))
            state.update(running=False, completed_at=time.time())
            state["last_result"] = result
            save()
            return result
        result.update(status="CANDIDATE" if result["final_mp4"] else "STAGES_COMPLETE", run_ok=True)
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
