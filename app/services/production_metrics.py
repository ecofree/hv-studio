"""Per-invocation stage timings and acoustic cache counters."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid
from pathlib import Path

_active = ContextVar('studio_stage_measurements', default=None)


class StageTimings:
    def __init__(self):
        self.rows = []
        self.started = None
        self.invocation_id = uuid.uuid4().hex
        self.opened_at = time.time()
        self.clock_started = time.monotonic()

    def begin(self, name):
        self.finish()
        self.started = time.monotonic()
        self.rows.append(dict(stage=name, started_at=time.time(), counters={}))
        from app.services.production_identity import current_execution
        execution = current_execution()
        if execution is not None:
            self.rows[-1]['execution'] = execution

    def finish(self):
        if self.started is not None:
            self.rows[-1].update(completed_at=time.time(), elapsed_seconds=round(time.monotonic()-self.started, 3))
            self.started = None

    def persist(self, root, result):
        from app.services.provenance import atomic_json
        self.finish()
        atomic_json(Path(root)/'measurements'/(self.invocation_id+'.json'),dict(
            schema_version=1,invocation_id=self.invocation_id,started_at=self.opened_at,
            finished_at=time.time(),wall_seconds=round(time.monotonic()-self.clock_started,3),
            status=result.get('status'),run_ok=result.get('run_ok'),execution=result.get('execution'),
            stage_timings=self.rows,final_mp4=result.get('final_mp4')))


def accumulated(root):
    """Measured invocations since instrumentation; never fabricate old timings."""
    from app.services.narration_pipeline import read
    stages={};total=0.;runs=0;failed=0;errors=[]
    for p in sorted((Path(root)/'measurements').glob('*.json')):
        try:
            row=read(p)
            if row['invocation_id']!=p.stem or row['schema_version']!=1:raise ValueError('invalid measurement')
            total+=row['wall_seconds'];runs+=1;failed+=int(row['run_ok'] is not True)
            for item in row['stage_timings']:
                stage=stages.setdefault(item['stage'],dict(invocations=0,seconds=0.,counters={}))
                stage['invocations']+=1;stage['seconds']+=item.get('elapsed_seconds',0)
                for k,v in item.get('counters',{}).items():stage['counters'][k]=stage['counters'].get(k,0)+v
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append(p.name+': '+str(exc))
    return dict(scope='recorded_invocations_since_instrumentation',invocations=runs,failed_invocations=failed,
                wall_seconds=round(total,3),stages=stages,errors=errors,
                historical_time_reconstructed=False,human_minutes=None,currency_cost=None)


def count(key, value=1):
    timer = _active.get()
    if timer is not None and timer.started is not None:
        counters = timer.rows[-1]['counters']
        counters[key] = counters.get(key, 0) + value


@contextmanager
def measure():
    timer = StageTimings()
    token = _active.set(timer)
    try:
        yield timer
    finally:
        timer.finish()
        _active.reset(token)
