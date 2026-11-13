"""One lazy, bounded offline Whisper process per audio job."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import json
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
import tempfile
from threading import Thread, Timer
import time

from app.services import model_resources as resources
from app.services.production_metrics import count

_current = ContextVar('studio_acoustic_session', default=None)


def _start_worker(python, script, model, threads, errors):
    return subprocess.Popen([str(python),'-u',str(script),'--serve','--model',str(model),
                             '--threads',str(threads)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=errors, text=True, encoding='utf-8', errors='strict',
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


class Session:
    def __init__(self):
        self.resources = ExitStack()
        self.process = self.signature = self.context = None
        self.reusable = True
        self.leased = False

    def close_worker(self):
        try:
            self.resources.close()
        finally:
            self.process = self.signature = None
            self.leased = False

    def acquire(self):
        if not self.leased:
            started = time.monotonic()
            self.resources.enter_context(resources.lease('local acoustic worker'))
            self.leased = True
            count('model_queue_seconds', round(time.monotonic()-started,3))

    def call(self, python, script, model, threads, payload, timeout):
        signature = (python, script, model, threads)
        if self.process is None or self.signature != signature or self.process.poll() is not None:
            if self.process is not None: self.close_worker()
            self.acquire()
            try:
                self.errors = self.resources.enter_context(tempfile.TemporaryFile())
                self.process = _start_worker(python,script,model,threads,self.errors)
                self.resources.enter_context(self.process)
                self.resources.callback(resources.stop_owned, self.process)
                self.signature = signature
                self.responses = Queue()
                # Only this reader touches stdout. EOF also wakes a waiting request.
                def collect(stream, queue):
                    try:
                        for line in stream: queue.put(line)
                    except (OSError,ValueError):pass
                    finally:
                        queue.put(None)
                Thread(target=collect,args=(self.process.stdout,self.responses),daemon=True).start()
                count('model_process_starts')
            except BaseException:
                self.close_worker()
                raise
        deadline = time.monotonic()+timeout
        watchdog = Timer(timeout,resources.stop_owned,args=(self.process,))
        watchdog.daemon=True
        watchdog.start()
        try:
            self.process.stdin.write(json.dumps(payload,ensure_ascii=True)+'\n')
            self.process.stdin.flush()
            reply = self.responses.get(timeout=max(0.,deadline-time.monotonic()))
            if reply is None:
                if time.monotonic()>=deadline:raise TimeoutError('acoustic worker timed out')
                self.errors.seek(0,os.SEEK_END)
                self.errors.seek(max(0,self.errors.tell()-1400))
                raise RuntimeError('acoustic worker stopped: '+self.errors.read().decode('utf-8','replace'))
            row = json.loads(reply)
            if 'error' in row: raise ValueError(row['error'])
            return row['result']
        except Empty as exc:
            self.close_worker()
            raise TimeoutError(f'local acoustic worker exceeded {timeout} seconds') from exc
        except BaseException:
            self.close_worker()
            raise
        finally:
            watchdog.cancel()


@contextmanager
def job_session():
    current = _current.get()
    if current is not None:
        yield current
        return
    session = Session()
    token = _current.set(session)
    try:
        yield session
    finally:
        session.close_worker()
        _current.reset(token)


def current():
    return _current.get()


def infer(config, model, payload, timeout):
    from app.services.local_asr import ROOT, resolve
    with job_session() as session:
        return session.call(resolve(config.python_executable),ROOT/'bin/local_asr_worker.py',model,
                            resources.threads(config.threads),payload,timeout)
