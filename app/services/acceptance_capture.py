"""Write immutable run evidence observed by the acceptance supervisor.

No caller-provided passed flag. Closing verifies the causal contract. Offline
runs are useful regression evidence but are never eligible for readiness.
"""
from pathlib import Path
import time
import uuid
from app.services.provenance import atomic_json, file_sha256
from app.services.narration_pipeline import read


def budget_snapshot(budget_path, folder):
    p=Path(folder)/'budgets'/(uuid.uuid4().hex+'.json')
    atomic_json(p,read(budget_path))
    return dict(budget_path=str(Path(budget_path).resolve()),
                budget_receipt_path=str(p.resolve()),budget_receipt_sha256=file_sha256(p))


class Capture:
    def __init__(self, folder, scenario, source_fingerprint, revision, *, mode):
        from app.services.production_acceptance import SCENARIOS
        if scenario not in SCENARIOS or mode not in ('offline','real'):
            raise ValueError('unsupported scenario or evidence mode')
        self.folder=Path(folder).resolve()
        self.folder.mkdir(parents=True,exist_ok=False)
        self.events=[];self.artifacts={}
        self.manifest=dict(schema_version=2,scenario=scenario,run_id=uuid.uuid4().hex,
            source_fingerprint=source_fingerprint,git_revision=revision,evidence_mode=mode,
            status='running',started_at=time.time())
        atomic_json(self.folder/'run.json',self.manifest)

    def emit(self, name, *, task=None, attempt=None, at=None, **details):
        forbidden={'run_id','sequence','event','task_id','attempt_id','episode_id'} & details.keys()
        if forbidden: raise ValueError('event identity cannot be overridden')
        event=dict(details,event=name,run_id=self.manifest['run_id'],sequence=len(self.events)+1,
                   at=time.time() if at is None else at)
        if task is not None:
            event.update(episode_id=task['episode_id'],task_id=task['task_id'],attempt_id=attempt)
        self.events.append(event)
        atomic_json(self.folder/'events.json',self.events)
        return event

    def completed(self, name, result, *, task, attempt, **details):
        from app.services.production_identity import require_execution, execution_ref
        require_execution(result.get('execution'),execution_ref(task,attempt))
        if result.get('run_ok') is not True: raise ValueError('execution did not complete')
        p=Path(result['final_mp4']).resolve();sha=file_sha256(p)
        if result.get('candidate_sha256')!=sha: raise ValueError('result candidate changed')
        self.artifacts[str(p)]=sha
        atomic_json(self.folder/'artifacts.json',self.artifacts)
        return self.emit(name,task=task,attempt=attempt,artifact_path=str(p),artifact_sha256=sha,**details)

    def close(self):
        from app.services.production_acceptance import verify_scenario
        self.manifest.update(status='passed',finished_at=time.time())
        atomic_json(self.folder/'run.json',self.manifest)
        atomic_json(self.folder/'events.json',self.events)
        atomic_json(self.folder/'artifacts.json',self.artifacts)
        record=self.record()
        try:
            verify_scenario(self.manifest['scenario'],record,self.manifest['source_fingerprint'],
                            require_real=self.manifest['evidence_mode']=='real')
        except Exception as exc:
            self.fail(exc);raise
        return record

    def fail(self, exc):
        self.manifest.update(status='blocked',finished_at=time.time(),error_type=type(exc).__name__,error=str(exc)[:1000])
        atomic_json(self.folder/'run.json',self.manifest)

    def record(self):
        return dict(schema_version=2,scenario=self.manifest['scenario'],run_id=self.manifest['run_id'],
            source_fingerprint=self.manifest['source_fingerprint'],git_revision=self.manifest['git_revision'],
            manifest_path=str(self.folder/'run.json'),manifest_sha256=file_sha256(self.folder/'run.json'),
            events_path=str(self.folder/'events.json'),events_sha256=file_sha256(self.folder/'events.json'),
            artifacts=dict(self.artifacts))
