"""Read the same production truth in CLI, API and monitoring UI."""
from pathlib import Path
import psutil
from app.services.narration_pipeline import read
from app.services.provenance import fingerprint


def get_status(ep_id):
    from app.orchestrator import _episode_dir, _load_script_md
    from app.managers import load
    from app.services.production_identity import artifacts_current
    root = _episode_dir(ep_id)/'production/v2'; state_path = root/'run.json'
    if not state_path.exists():
        # No v2 run state. If this episode was delivered through the
        # legacy-preview pipeline, report *that* truthfully instead of
        # NOT_STARTED/approved=False -- a delivered episode must never look
        # unapproved to every reader of get_status (2026-09-29: 5 delivered
        # episodes all reported approved=False here).
        legacy = _legacy_status(ep_id)
        if legacy is not None:
            return legacy
        return dict(pipeline_version='production_v2', status='NOT_STARTED', approved=False,
                    production_ready=False, content_approved=False, blockers=[])
    try:
        state = read(state_path); last = state.get('last_result', {})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return dict(pipeline_version='production_v2', status='BLOCKED', approved=False, production_ready=False,
                    delivery_ready=False, content_approved=False,
                    blockers=['production state unreadable: '+type(exc).__name__],
                    delivery_pending=[], quality_pending_units=[])
    result = dict(pipeline_version='production_v2', status=last.get('status','PREPARED'), approved=False, production_ready=False,
                  stages_done=last.get('stages_done', []), active_stage=state.get('active_stage'),
                  candidate_path=None, blockers=[], delivery_pending=[], quality_pending_units=[])
    result.update(delivery_ready=False, dimensions={}, source_current=False,
                  content_approved=False, delivered_approval=dict(
                      approved=False, pipeline=None, output_sha256=None, approval_path=None))
    if state.get('running'):
        try:
            alive = psutil.Process(state.get('owner_pid',-1)).create_time()==state.get('owner_created_at')
        except (psutil.Error, ValueError): alive=False
        result['status'] = 'RUNNING' if alive else 'INTERRUPTED'
    if last.get('error') and not state.get('running'): result['blockers'].append(last['error'])
    if state.get('running'):result.update(progress=state.get('progress'),updated_at=state.get('updated_at'))
    # Scene plates live outside every receipt: a built episode whose plates are
    # gone cannot be rebuilt, so this is checked before (and independently of)
    # the receipt validation below, which aborts early on any drift.
    result['scene_images_missing'] = missing_scene_images(
        ep_id, only_when_built=bool(state.get('receipt_path') or (root / 'published.json').exists()))
    # An outdated candidate must not hide independent review queues. These
    # readers validate their own audio/source bindings, separately from render.
    for dimension, key, reader, output in (
        ('quality', 'quality_path', _quality_pending, 'quality_pending_units'),
        ('delivery', 'manifest_path', _delivery_pending, 'delivery_pending'),
    ):
        if not state.get(key): continue
        try:
            result[output] = reader(state[key])
            result['quality_report_path' if key=='quality_path' else key] = state[key]
            result['dimensions'][dimension] = 'pending' if result[output] else 'passed'
            if result[output]: result['blockers'].append('audio findings await disposition' if dimension=='quality' else 'delivery directions await review or revision')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['dimensions'][dimension] = 'stale_or_invalid'
            result['blockers'].append(dimension+': '+str(exc))
            result.update(approved=False, delivery_ready=False)
    try:
        if fingerprint(_load_script_md(load(ep_id))) != state['source_sha256']: raise ValueError('source changed')
        result['source_current'] = True
        if state.get('manifest_path'):
            from app.services.studio_workflow import folder
            workflow=folder(ep_id)/'workflow.json'
            if workflow.is_file():
                selected=Path(read(workflow)['voice'].get('master') or '.')
                edit=selected.parent/'edit.json'
                if selected.is_file() and edit.is_file() and read(edit).get('kind')=='external_pickup_v1':
                    from app.services.provenance import file_sha256
                    if read(state['manifest_path']).get('source_recording',{}).get('delivered_sha256')!=file_sha256(selected):
                        raise ValueError('已选用局部补录，请制作当前旁白或视频以更新字幕时序')
        from app.services.production_inputs import input_files
        if 'image_inputs' in state and state['image_inputs'] != input_files(ep_id):
            raise ValueError('visual inputs changed; rebuild images and timeline')
        if state.get('receipt_path'):
            receipt = read(state['receipt_path'])
            if receipt.get('kind') == 'metadata_amendment_v1':
                from app.services.production_revision import verify_amendment
                verify_amendment(state['receipt_path'])
                registration = state.get('full_episode_registration', {})
                from app.services.studio_workflow import identity
                if registration.get('source_identity') != identity(ep_id):
                    raise ValueError('registered full episode design changed')
                from app.services.provenance import file_sha256
                if (registration.get('receipt_path') != state['receipt_path'] or
                        registration.get('receipt_sha256') != file_sha256(state['receipt_path'])):
                    raise ValueError('registered full episode receipt changed')
                result['full_episode_registration'] = registration
                result['audio_master_path'] = state.get('audio_master_path')
            from app.services.production_identity import artifacts_current, split_receipt_drift
            if not artifacts_current(receipt['inputs']):
                content_drift, renderer_drift = split_receipt_drift(receipt['inputs'])
                if content_drift:
                    raise ValueError('candidate inputs changed; rebuild required')
                # Renderer-only drift: the approved bytes and their reviews are
                # untouched (a source edit cannot rewrite a file). The candidate
                # stands; the next rebuild renders with new code and needs a
                # fresh review. Recorded, not hidden.
                result['renderer_changed'] = renderer_drift
            from app.services.provenance import file_sha256
            if file_sha256(receipt['candidate_path']) != receipt['candidate_sha256']: raise ValueError('candidate is missing or changed')
            result['candidate_path'] = receipt['candidate_path']
            # Visibility only, never a blocker: a mid-render NVENC->software
            # fallback leaves a mixed-encoder film that decodes and verifies
            # fine. Old receipts predate the field and read as uniform.
            result['dimensions']['encoder'] = (
                'mixed' if receipt.get('encoder_mixed') else 'uniform')
        if state.get('manifest_path'):
            manifest = read(state['manifest_path'])
            from app.services.audio_disposition import require_usable
            require_usable(manifest)
            if not state.get('quality_path'):
                result['quality_pending_units'] = [t['unit_id'] for t in manifest['takes'] if t['quality']['status']!='passed']
            if 'delivery_dispositions' not in manifest and manifest.get('provenance')!='external_recording':
                result['blockers'].append('delivery cues have not been processed')
        if not state.get('quality_path') and result['quality_pending_units']:
            result['blockers'].append('audio findings await disposition')
        # External narration only: units the whole-track alignment could not
        # convince. They are cut, but each one's slice still needs the forced
        # aligner's verdict before the film can be published -- the same
        # "unadjudicated finding blocks approval" rule as quality and delivery.
        result['alignment_review_units'] = alignment_review_pending(state)
        if result['alignment_review_units']:
            result['blockers'].append(
                f"{len(result['alignment_review_units'])} unit 的切片边界待强制对齐裁决")
        published = root/'published.json'
        approved = False
        if published.exists() and result.get('candidate_path'):
            from app.services.provenance import file_sha256
            pointer = read(published)
            if pointer.get('approved') and pointer.get('sha256')==file_sha256(result['candidate_path']) and pointer.get('receipt')==state.get('receipt_path'):
                visual, audio = read(pointer['review']), read(pointer['listening_review'])
                approved = (visual.get('decision')=='approved' and visual.get('receipt_sha256')==file_sha256(state['receipt_path'])
                            and audio.get('decision')=='approved' and audio.get('manifest_sha256')==file_sha256(state['manifest_path'])
                            and not result['quality_pending_units'] and not result['delivery_pending']
                            and not result['alignment_review_units']
                            and 'stale_or_invalid' not in result['dimensions'].values())
        result['approved'] = approved
        result['delivery_ready'] = approved
        if approved: result['status']='APPROVED'
        else: result['blockers'].append('current candidate requires full listening, visual and fact review')
    except (OSError, ValueError, KeyError) as exc:
        if result['status'] != 'RUNNING': result['status'] = 'STALE'
        result['blockers'].append(str(exc))
    # Missing scene plates block regardless of which path above was taken: a
    # receipt drift must not hide the fact that the episode cannot be rebuilt.
    if result.get('scene_images_missing'):
        apply_scene_freeze(result)
    result['alignment'] = alignment_summary(state)
    if result['alignment']['status']=='stale_or_invalid':
        result.update(approved=False,delivery_ready=False)
        result['blockers'].append('subtitle timing evidence: '+result['alignment']['error'])
        if result['status']=='APPROVED':result['status']='STALE'
    if state.get('budget_path'):
        try:
            budget = read(state['budget_path']); result['budget'] = dict(limits=budget['limits'], used=budget['used'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['blockers'].append('budget ledger unreadable: '+type(exc).__name__)
    result['blockers'] = list(dict.fromkeys(result['blockers']))
    # Report delivery-level truth alongside the v2 attempt. `approved` stays
    # narrow (this v2 candidate), `content_approved` answers "is there a current
    # hash-bound approval for a delivered video by either pipeline".
    try:
        delivery = delivered_approval(ep_id, current=result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        delivery = dict(approved=False, pipeline=None, output_sha256=None, approval_path=None)
    result['delivered_approval'] = delivery
    result['content_approved'] = bool(delivery['approved'])
    return result


def delivered_approval(ep_id, *, current=None):
    """The strongest currently-valid approval for a delivered video, across pipelines.

    An episode can carry a hash-bound legacy delivery *and* a newer v2 attempt
    that is merely stale or still short of review. Reporting only the v2 attempt
    made three delivered episodes read as never-approved (2026-10-01 audit:
    laogongtuan, lishimin-01, lishimin-03), and readiness then reported two false
    `requires current content approval` blockers for episodes whose approval
    demonstrably existed.

    This is deliberately a separate field from `approved`, which keeps its narrow
    meaning: "this v2 candidate is approved". `content_approved` answers the
    different question "is there a current, hash-bound approval for a delivered
    video by either pipeline".

    v2 is preferred when both are valid because its evidence model is stronger
    (published pointer + two hash-bound human reviews on top of the receipt).
    """
    if current is None:
        current = get_status(ep_id)
    out = dict(approved=False,pipeline=None,output_sha256=None,approval_path=None)
    if current.get('approved') and current.get('candidate_path'):
        from app.services.provenance import file_sha256
        from app import paths
        return dict(approved=True,pipeline='production_v2',output_sha256=file_sha256(current['candidate_path']),
                    approval_path=str(paths.episode_dir(ep_id)/'production/v2/published.json'))
    # A valid legacy delivery remains available when there is no current v2 candidate.
    if not current.get('candidate_path'):
        legacy=_legacy_status(ep_id)
        if legacy and legacy.get('approved'):
            return dict(approved=True,pipeline='legacy_preview_v1',output_sha256=legacy.get('output_sha256'),approval_path=legacy.get('approval_path'))
    return out


def _legacy_status(ep_id):
    """Truthful status for episodes delivered through the legacy-preview pipeline.

    Same approval semantics as the v2 path, reached through the gates that path
    actually enforces: input approval + external audio binding + subtitle
    fidelity + output binding. Returns None when the episode has no composed
    video and no recorded approval, so the caller keeps reporting NOT_STARTED.

    Why a separate reader instead of teaching the v2 reader about legacy: the v2
    chain (manifest -> forced alignment -> quality -> published pointer) is the
    stronger evidence model and stays untouched. This only stops legacy
    deliveries from being invisible (2026-09-29).
    """
    from app.orchestrator import _episode_dir
    from app.services import output_binding
    from app.services.narration_pipeline import read
    from app.services.production_admission import check
    from app.services.subtitle_fidelity import (_narration_source,
                                                 frame_text_report)
    from app.managers import load

    approval = _episode_dir(ep_id)/'production/input-approval.json'
    mp4 = output_binding.final_mp4(ep_id)
    if not approval.is_file() and not mp4.is_file():
        return None
    # A composed video can legitimately have no approval yet (not reviewed).
    # Reported as PREPARED so it is not confused with an unstarted episode,
    # but never as approved.
    reviewed = approval.is_file()
    blockers, dimensions = [], {}
    # `fid` must exist before the approval test below. It used to be bound only
    # inside the fidelity branch, so an episode with no comparable frames took
    # the `not_applicable` path (or raised) and left it unbound -> the approval
    # expression raised UnboundLocalError. assess() catches only
    # (OSError,ValueError,KeyError,TypeError,AttributeError), and
    # UnboundLocalError is a NameError subclass, so it escaped into
    # check_project instead of being reported as a blocker.
    #
    # Two distinct defaults, and the difference matters:
    #   no baseline   -> {'ok': True}   a missing comparison is not a defect
    #   could not run -> {'ok': False}  unverifiable must never read as approved
    fid = {'ok': True, 'fails': []}
    try:
        adm = check(ep_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        adm = dict(eligible=False, blockers=['admission check failed: '+type(exc).__name__])
    for b in adm.get('blockers', []):
        blockers.append(b)
    out = output_binding.verify(ep_id)
    if not out.get('ok'):
        blockers.extend(out.get('fails', []))
    if not out.get('skipped') and not mp4.is_file():
        blockers.append('bound video is missing')
    # Name the unbound window explicitly: skipped-ok means "no binding
    # established yet", not "bytes verified". Approval-before-compose is
    # legitimate (record_approval refuses to fabricate), but no caller should
    # read this dimension as a byte guarantee.
    dimensions['output_binding'] = (
        'failed' if not out.get('ok')
        else 'unbound' if out.get('skipped') else 'bound')
    try:
        ep = load(ep_id)
        if ep.frames and _narration_source(ep).strip():
            fid = frame_text_report(ep)
            dimensions['subtitle_fidelity'] = 'passed' if fid['ok'] else 'failed'
            if not fid['ok']:
                blockers.extend(fid['fails'])
        else:
            # No source script to compare against (synthetic/smoke episodes).
            # Report as not-applicable rather than failing: a missing baseline is
            # not a fidelity defect (2026-09-29: smoke fixture has frames but no
            # script.md, and the gate correctly found nothing to compare).
            dimensions['subtitle_fidelity'] = 'not_applicable'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        dimensions['subtitle_fidelity'] = 'stale_or_invalid'
        blockers.append('subtitle fidelity: '+str(exc))
        fid = {'ok': False, 'fails': []}
    approved = (reviewed and bool(adm.get('eligible')) and out.get('ok', True)
                and bool(fid['ok']))
    if approved:
        status = 'APPROVED'
    elif blockers:
        status = 'STALE'
    else:
        status = 'PREPARED'
    result = dict(
        pipeline_version='legacy_preview_v1',
        status=status,
        approved=approved, production_ready=approved, delivery_ready=approved,
        # For this pipeline the two coincide: an approved legacy delivery *is* a
        # current hash-bound approval.
        content_approved=approved,
        stages_done=(['script', 'images', 'compose', 'verify']
                     if mp4.is_file() else []),
        active_stage=None, candidate_path=str(mp4) if mp4.is_file() else None,
        output_sha256=(out.get('sha256') if not out.get('skipped') else None),
        output_frames=(out.get('frames') if not out.get('skipped') else None),
        source_current=True, dimensions=dimensions,
        delivered_approval=dict(
            approved=approved, pipeline='legacy_preview_v1',
            output_sha256=(out.get('sha256') if not out.get('skipped') else None),
            approval_path=str(approval.resolve()) if approval.is_file() else None),
        approval_path=str(approval) if approval.is_file() else None,
        input_fingerprint=adm.get('input_fingerprint'),
        delivery_pending=[], quality_pending_units=[],
        blockers=list(dict.fromkeys(blockers)))
    return result


def _quality_pending(path):
    from app.services.production_quality import pending
    return pending(path)


def _delivery_pending(path):
    from app.services.delivery import pending
    return pending(path)


def apply_scene_freeze(result):
    """Revoke approval when scene plates are missing.

    An approval for bytes that can never be rebuilt is not a deliverable.
    Callers must not need to read blockers to learn this: the lamp itself
    goes out. Returns the mutated result dict.
    """
    missing = result.get('scene_images_missing') or []
    if not missing:
        return result
    result['blockers'].append(
        f"scene images missing ({len(missing)}): "
        + ", ".join(missing[:8])
        + (" ..." if len(missing) > 8 else "")
        + " —— 底图丢失，该集无法重建")
    result.update(approved=False, delivery_ready=False)
    if result.get('status') == 'APPROVED':
        result['status'] = 'STALE'
    return result


def missing_scene_images(ep_id, *, only_when_built=True):
    """Scene image keys that do not resolve to a file on disk.

    Returns a sorted list (empty when everything resolves, or when there is
    nothing to check). `only_when_built=False` also reports plates that were
    never generated; the status reader keeps it True so a fresh episode is
    never blamed for ungenerated plates.
    """
    from app.orchestrator import _episode_dir
    from app.services.narration_pipeline import read
    from app.services.shot_builder import _resolve_image

    plan_path = _episode_dir(ep_id) / "scene_plan.json"
    if not plan_path.is_file():
        return []
    try:
        plan = read(plan_path)
    except (OSError, ValueError):
        return []
    images = plan.get("images", {})
    rows = plan.get("scenes", []) or plan.get("unit_scenes", [])
    if only_when_built and not rows:
        return []
    # v2 episodes keep plates under production/v2/images/<fingerprint>/, not at
    # the `shots/` path the scene plan declares. run.json's image_assets is the
    # authority for what was actually generated; the scene-plan path is only the
    # fallback for legacy layouts.
    generated = {}
    try:
        from app.orchestrator import _episode_dir as _dir
        state_path = _dir(ep_id) / "production" / "v2" / "run.json"
        if state_path.is_file():
            assets = read(state_path).get("image_assets") or {}
            generated = {k: v for k, v in assets.items() if Path(v).is_file()}
    except (OSError, ValueError):
        generated = {}
    # A key absent from the images map is missing, not a crash: _resolve_image
    # raises KeyError for unknown keys (correct for the composer, which must
    # fail closed), while this reader must report.
    missing = set()
    for r in rows:
        key = r.get("image")
        if not key or key in generated:
            continue
        try:
            exists = _resolve_image(images, key).exists()
        except (KeyError, ValueError, TypeError, AttributeError):
            exists = False
        if not exists:
            missing.add(key)
    return sorted(missing)


def alignment_review_pending(state):
    """External-narration units whose cut still needs a forced-alignment verdict.

    A unit lands here when the whole-track banded DP aligned it weakly. That is
    not proof of a bad cut -- it also happens when ASR simply cannot recognise
    classical or rare script text -- so the verdict comes from the only judge
    that looks at the slice itself: `forced_alignment.build`, which raises if a
    take does not cover its unit's text.

    So a unit is cleared exactly when the current alignment evidence contains
    word timings for it AND that evidence is still bound to the current manifest.
    A manifest with no `alignment_review` (every TTS-provenance manifest) is
    empty and costs nothing.
    """
    manifest_path = state.get('manifest_path')
    alignment_path = state.get('alignment_path')
    if not manifest_path:
        return []
    if not alignment_path:
        return sorted(read(manifest_path).get('alignment_review') or [])
    try:
        manifest = read(manifest_path)
        wanted = set(manifest.get('alignment_review') or [])
        if not wanted:
            return []
        evidence = read(alignment_path)
    except (OSError, ValueError, KeyError, TypeError):
        # Unreadable manifest or evidence is not a verdict. Be conservative:
        # anything the manifest asked for stays pending.
        try:
            wanted = set(read(manifest_path).get('alignment_review') or [])
        except (OSError, ValueError, KeyError, TypeError):
            return []
        return sorted(wanted)
    if evidence.get('method') != 'acoustic_source_alignment_v1':
        return sorted(wanted)
    if evidence.get('manifest_content_sha256') != fingerprint(manifest):
        return sorted(wanted)
    from app.services.provenance import file_sha256
    if any(file_sha256(p) != sha for p, sha in evidence.get('inputs', {}).items()):
        return sorted(wanted)
    # Evidence that does not cover every unit with word timings cannot clear any.
    timed = set(evidence.get('words') or {})
    return sorted(wanted - timed)


def alignment_summary(state):
    """Separate acoustic timing evidence from the transcript listening queue."""
    out = dict(status='not_started', timing_issues=[], transcript_review_units=[])
    if not state.get('alignment_path'): return out
    try:
        evidence = read(state['alignment_path'])
        manifest = read(state['manifest_path'])
        from app.services.forced_alignment import require_evidence
        require_evidence(manifest, dict(alignment_evidence_path=state['alignment_path'],
                                       subtitles=evidence['subtitles']))
        issues = evidence.get('issues', [])
        out.update(status=evidence['status'], source_coverage_complete=evidence.get('source_coverage_complete',False),
                   subtitle_count=len(evidence['subtitles']),
                   timing_issues=[x for x in issues if x.get('kind')!='transcript_quality_pending'],
                   transcript_review_units=evidence.get('transcript_review_units',
                       [x['unit_id'] for x in issues if x.get('kind')=='transcript_quality_pending']))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        out.update(status='stale_or_invalid', error=str(exc))
    return out
