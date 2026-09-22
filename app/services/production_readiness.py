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
    except (OSError,ValueError,KeyError,TypeError): blockers.append('production worker deployment is unverified')
    evidence_path = paths.state_dir()/'acceptance/production-validation.json'
    try:
        evidence = read(evidence_path)
        if not isinstance(evidence, dict): raise ValueError('invalid validation record')
        if evidence.get('source_fingerprint') != source_fingerprint: blockers.append('production validation is for another source revision')
        from app.services.production_identity import artifacts_current
        artifacts = evidence.get('artifacts')
        if (not isinstance(artifacts, dict) or not artifacts or
                not all(isinstance(p,str) and isinstance(sha,str) for p,sha in artifacts.items()) or
                not artifacts_current(artifacts)):
            blockers.append('production validation artifacts are missing or changed')
        for key in ('multi_episode_current_rebuild','kill_resume','timeout_recovery','real_provider_models',
                    'unattended_worker_full_batch'):
            if evidence.get(key) is not True: blockers.append(key+' validation is pending')
        episodes = evidence.get('episodes',[])
        if not isinstance(episodes,list): raise ValueError('episodes must be a list')
        for eid in episodes: paths.validate_episode_id(eid)
        if len(set(episodes))<2: blockers.append('two current episode validations are required')
        from app.services.production_status import get_status
        for eid in episodes:
            try:
                if get_status(eid).get('approved') is not True: blockers.append(eid+' requires current content approval')
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                blockers.append(eid+' content approval cannot be verified')
    except (OSError,ValueError,KeyError,TypeError): blockers.append('current production validation evidence is missing or malformed')
    return dict(ready=not blockers, blockers=blockers)
