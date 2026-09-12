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
    if not state_path.exists(): return dict(pipeline_version='production_v2', status='NOT_STARTED', approved=False, production_ready=False, blockers=[])
    state = read(state_path); last = state.get('last_result', {})
    result = dict(pipeline_version='production_v2', status=last.get('status','PREPARED'), approved=False, production_ready=False,
                  stages_done=last.get('stages_done', []), active_stage=state.get('active_stage'),
                  candidate_path=None, blockers=[], delivery_pending=[], quality_pending_units=[])
    if state.get('running'):
        try:
            alive = psutil.Process(state.get('owner_pid',-1)).create_time()==state.get('owner_created_at')
        except (psutil.Error, ValueError): alive=False
        result['status'] = 'RUNNING' if alive else 'INTERRUPTED'
    if last.get('error'): result['blockers'].append(last['error'])
    try:
        if fingerprint(_load_script_md(load(ep_id))) != state['source_sha256']: raise ValueError('source changed')
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
    if state.get('budget_path'):
        budget = read(state['budget_path']); result['budget'] = dict(limits=budget['limits'], used=budget['used'])
    return result
