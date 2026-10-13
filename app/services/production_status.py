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
    if last.get('error'): result['blockers'].append(last['error'])
    try:
        if fingerprint(_load_script_md(load(ep_id))) != state['source_sha256']: raise ValueError('source changed')
        result['source_current'] = True
        if state.get('receipt_path'):
            receipt = read(state['receipt_path'])
            if not artifacts_current(receipt['inputs']): raise ValueError('candidate inputs changed; rebuild required')
            from app.services.provenance import file_sha256
            if file_sha256(receipt['candidate_path']) != receipt['candidate_sha256']: raise ValueError('candidate is missing or changed')
            result['candidate_path'] = receipt['candidate_path']
        if state.get('quality_path'):
            from app.services.production_quality import pending
            result['quality_pending_units'] = pending(state['quality_path'])
            result['quality_report_path'] = state['quality_path']
        elif state.get('manifest_path'):
            result['quality_pending_units'] = [t['unit_id'] for t in read(state['manifest_path'])['takes'] if t['quality']['status']!='passed']
        if state.get('manifest_path'):
            manifest = read(state['manifest_path'])
            from app.services.delivery import pending as pending_directions
            result['delivery_pending'] = pending_directions(state['manifest_path'])
            result['manifest_path'] = state['manifest_path']
            if 'delivery_dispositions' not in manifest: result['blockers'].append('delivery cues have not been processed')
        if result['quality_pending_units']: result['blockers'].append('audio findings await disposition')
        if result['delivery_pending']: result['blockers'].append('delivery directions await review or revision')
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
                            and not result['alignment_review_units'])
        result['approved'] = approved
        result['delivery_ready'] = approved
        if approved: result['status']='APPROVED'
        else: result['blockers'].append('current candidate requires full listening, visual and fact review')
    except (OSError, ValueError, KeyError) as exc:
        if result['status'] != 'RUNNING': result['status'] = 'STALE'
        result['blockers'].append(str(exc))
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
            if result[output]: result['blockers'].append(dimension+' findings await disposition')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['dimensions'][dimension] = 'stale_or_invalid'
            result['blockers'].append(dimension+': '+str(exc))
            result.update(approved=False, delivery_ready=False)
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
        delivery = delivered_approval(ep_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        delivery = dict(approved=False, pipeline=None, output_sha256=None, approval_path=None)
    result['delivered_approval'] = delivery
    result['content_approved'] = bool(delivery['approved'])
    return result


def delivered_approval(ep_id):
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
    from app.orchestrator import _episode_dir
    from app.services.provenance import file_sha256
    out = dict(approved=False, pipeline=None, output_sha256=None, approval_path=None)
    root = _episode_dir(ep_id) / 'production/v2'
    published, state_path = root / 'published.json', root / 'run.json'
    if published.is_file() and state_path.is_file():
        try:
            state, pointer = read(state_path), read(published)
            receipt_path = state.get('receipt_path')
            if pointer.get('approved') and receipt_path and pointer.get('receipt') == receipt_path:
                receipt = read(receipt_path)
                candidate = receipt['candidate_path']
                if (Path(candidate).is_file()
                        and file_sha256(candidate) == receipt['candidate_sha256'] == pointer.get('sha256')
                        and file_sha256(receipt_path) == read(pointer['review']).get('receipt_sha256')
                        and read(pointer['listening_review']).get('decision') == 'approved'):
                    out.update(approved=True, pipeline='production_v2',
                               output_sha256=pointer['sha256'],
                               approval_path=str(published.resolve()))
                    return out
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    legacy = _legacy_status(ep_id)
    if legacy and legacy.get('approved'):
        out.update(approved=True, pipeline='legacy_preview_v1',
                   output_sha256=legacy.get('output_sha256'),
                   approval_path=legacy.get('approval_path'))
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
    if not manifest_path or not alignment_path:
        return []
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
