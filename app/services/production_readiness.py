"""Release evidence is independent of a successful offline code gate."""
import time
from app import paths
from app.services.narration_pipeline import read


def assess(source_fingerprint):
    from app.services.batch_worker import alive, queue_dir
    blockers = []
    worker_path = queue_dir()/'worker.json'
    try:
        worker = read(worker_path)
        if not alive(worker['pid'],worker['created_at']) or time.time()-worker['heartbeat']>120:
            blockers.append('production worker is not live with a fresh heartbeat')
    except (OSError,ValueError,KeyError): blockers.append('production worker deployment is unverified')
    evidence_path = paths.state_dir()/'acceptance/production-validation.json'
    try:
        evidence = read(evidence_path)
        if evidence.get('source_fingerprint') != source_fingerprint: blockers.append('production validation is for another source revision')
        from app.services.production_identity import artifacts_current
        if not artifacts_current(evidence.get('artifacts')): blockers.append('production validation artifacts are missing or changed')
        for key in ('multi_episode_current_rebuild','kill_resume','timeout_recovery','real_provider_models'):
            if evidence.get(key) is not True: blockers.append(key+' validation is pending')
        episodes = evidence.get('episodes',[])
        if len(set(episodes))<2: blockers.append('two current episode validations are required')
        from app.services.production_status import get_status
        for eid in episodes:
            if not get_status(eid).get('approved'): blockers.append(eid+' requires current content approval')
    except (OSError,ValueError,KeyError): blockers.append('current production validation evidence is missing')
    return dict(ready=not blockers, blockers=blockers)
