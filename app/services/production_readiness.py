"""Release evidence is independent of a successful offline code gate."""
import time
import math
from app import paths
from app.services.narration_pipeline import read


def assess(source_fingerprint):
    from app.services.batch_worker import alive, queue_dir
    blockers = []
    worker_path = queue_dir()/'worker.json'
    try:
        worker = read(worker_path)
        if not isinstance(worker, dict): raise ValueError('invalid worker record')
        age = time.time()-worker['heartbeat']
        if not math.isfinite(age) or not 0 <= age <= 120 or not alive(worker['pid'],worker['created_at']):
            blockers.append('production worker is not live with a fresh heartbeat')
    except (OSError,ValueError,KeyError,TypeError):
        blockers.append('production worker deployment is unverified')

    evidence_path = paths.state_dir()/'acceptance/production-validation.json'
    try:
        evidence = read(evidence_path)
        from app.services.production_acceptance import verify_validation
        verified = verify_validation(evidence, source_fingerprint)
        blockers.extend(verified['blockers'])
        episodes = evidence.get('episodes', []) if isinstance(evidence, dict) else []
        from app.services.production_status import get_status
        for eid in episodes if isinstance(episodes, list) else []:
            try:
                paths.validate_episode_id(eid)
                # Delivery-level truth includes a valid legacy delivery when the
                # current V2 attempt is stale; never key on the narrow v2 flag.
                if get_status(eid).get('content_approved') is not True:
                    blockers.append(eid+' requires current content approval')
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                blockers.append(str(eid)+' content approval cannot be verified')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        blockers.append('current production validation evidence is missing or malformed: '+str(exc))
    return dict(ready=not blockers, blockers=list(dict.fromkeys(blockers)))
