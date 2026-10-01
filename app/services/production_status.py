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
        return dict(pipeline_version='production_v2', status='NOT_STARTED', approved=False, production_ready=False, blockers=[])
    try:
        state = read(state_path); last = state.get('last_result', {})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return dict(pipeline_version='production_v2', status='BLOCKED', approved=False, production_ready=False,
                    delivery_ready=False, blockers=['production state unreadable: '+type(exc).__name__],
                    delivery_pending=[], quality_pending_units=[])
    result = dict(pipeline_version='production_v2', status=last.get('status','PREPARED'), approved=False, production_ready=False,
                  stages_done=last.get('stages_done', []), active_stage=state.get('active_stage'),
                  candidate_path=None, blockers=[], delivery_pending=[], quality_pending_units=[])
    result.update(delivery_ready=False, dimensions={}, source_current=False)
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
        published = root/'published.json'
        approved = False
        if published.exists() and result.get('candidate_path'):
            from app.services.provenance import file_sha256
            pointer = read(published)
            if pointer.get('approved') and pointer.get('sha256')==file_sha256(result['candidate_path']) and pointer.get('receipt')==state.get('receipt_path'):
                visual, audio = read(pointer['review']), read(pointer['listening_review'])
                approved = (visual.get('decision')=='approved' and visual.get('receipt_sha256')==file_sha256(state['receipt_path'])
                            and audio.get('decision')=='approved' and audio.get('manifest_sha256')==file_sha256(state['manifest_path'])
                            and not result['quality_pending_units'] and not result['delivery_pending'])
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
    return result


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
        stages_done=(['script', 'images', 'compose', 'verify']
                     if mp4.is_file() else []),
        active_stage=None, candidate_path=str(mp4) if mp4.is_file() else None,
        output_sha256=(out.get('sha256') if not out.get('skipped') else None),
        output_frames=(out.get('frames') if not out.get('skipped') else None),
        source_current=True, dimensions=dimensions,
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
