"""Per-invocation stage timings and acoustic cache counters."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_active = ContextVar('studio_stage_measurements', default=None)


class StageTimings:
    def __init__(self):
        self.rows = []
        self.started = None

    def begin(self, name):
        self.finish()
        self.started = time.monotonic()
        self.rows.append(dict(stage=name, started_at=time.time(), counters={}))

    def finish(self):
        if self.started is not None:
            self.rows[-1].update(completed_at=time.time(), elapsed_seconds=round(time.monotonic()-self.started, 3))
            self.started = None


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
