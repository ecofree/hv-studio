"""One heavy local model worker at a time; bounded waits and owned-process cleanup."""
from contextlib import contextmanager
import os
import subprocess
import time
import wave
from pathlib import Path
from app.services.process_lock import file_lock


def timeout_for(audio,minimum=180):
    try:
        with wave.open(str(audio),'rb') as w:seconds=w.getnframes()/w.getframerate()
    except (OSError,wave.Error,EOFError):seconds=0
    return min(7200,max(int(minimum),int(seconds*4+120)))


def threads(value):return max(1,min(int(value),4,os.cpu_count() or 1))


@contextmanager
def lease(kind,*,wait_seconds=120,min_available_mb=2048):
    from app import paths
    import psutil
    deadline=time.monotonic()+wait_seconds
    while True:
        lock=file_lock(paths.state_dir()/'model-resources/heavy-model.lock')
        try:lock.__enter__();break
        except RuntimeError:
            if time.monotonic()>=deadline:raise RuntimeError('local model busy; resume later without new generation')
            time.sleep(.1)
    try:
        if psutil.virtual_memory().available<min_available_mb*1024**2:
            raise RuntimeError('insufficient available RAM for '+kind+'; no worker started')
        yield
    finally:lock.__exit__(None,None,None)


def stop_owned(process):
    """Only processes descended from the process object created by this caller."""
    import psutil
    if process.poll() is not None:return
    try:
        children=psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children=[]
    for child in reversed(children):
        try:child.kill()
        except psutil.NoSuchProcess:pass
    try:process.kill()
    except ProcessLookupError:pass
    process.wait(timeout=10)
    if children:psutil.wait_procs(children,timeout=10)


def run(args,*,timeout,**kwargs):
    # communicate drains both pipes; never kill unrelated TTS/server processes.
    kwargs.pop('capture_output',None)
    with subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs) as process:
        try:out,err=process.communicate(timeout=timeout)
        except BaseException:
            stop_owned(process)
            raise
        return subprocess.CompletedProcess(args,process.returncode,out,err)
