"""Kernel-backed locks, including same-process thread exclusion.

The lock inode is intentionally retained; unlinking it would let a third writer
lock a new inode while another writer still holds the old one.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

_registry={}
_guard=threading.Lock()


@contextmanager
def file_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    key=str(path.resolve()).casefold()
    with _guard: mutex=_registry.setdefault(key,threading.Lock())
    if not mutex.acquire(blocking=False): raise RuntimeError(f"operation already running: {path}")
    stream=None
    held=False
    try:
        # Respect a live legacy PID file before switching to kernel locking.
        if path.exists():
            old=path.read_text(encoding='utf-8').strip()
            if old.isdigit() and int(old)!=os.getpid():
                import psutil
                if psutil.pid_exists(int(old)):
                    raise RuntimeError(f"legacy writer still running: PID {old}")
        stream=path.open('a+b')
        if stream.seek(0,2)==0: stream.write(b'#');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        held=True
        stream.seek(0);stream.truncate()
        stream.write(json.dumps({'schema_version':2,'pid':os.getpid()}).encode());stream.flush()
        yield
    except OSError as exc:
        if not held: raise RuntimeError(f"operation already running: {path}") from exc
        raise
    finally:
        if stream:
            if held:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
            stream.close()
        mutex.release()
