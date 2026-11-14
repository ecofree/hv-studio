"""Shared episode ownership and conservative local storage admission.

Reuses the existing .produce.lock. Reentrancy is restricted to this process and
synchronous thread so nested Studio -> V2 calls retain the same kernel lease.
Nothing here allocates budgets, changes approvals, or declares stages complete.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import math
import os
import tempfile
import threading

_owned = threading.local()


class ProductionBusy(RuntimeError):
    """Another entry point owns this episode; no production was started."""


@contextmanager
def episode_execution(ep_id, *, timeout=0):
    from app.paths import episode_dir
    from app.services.process_lock import file_lock, wait_file_lock
    lock = episode_dir(ep_id) / '.produce.lock'
    # Including PID prevents an inherited thread-local entry from granting a
    # forked process ownership. Only the current thread may nest an entry.
    key = (os.getpid(), str(lock.resolve()).casefold())
    owned = getattr(_owned, 'locks', None)
    if owned is None:
        owned = _owned.locks = set()
    if key in owned:
        yield
        return
    handle = wait_file_lock(lock, timeout=timeout) if timeout else file_lock(lock)
    try:
        handle.__enter__()
    except RuntimeError as exc:
        raise ProductionBusy(f'episode {ep_id} already running') from exc
    owned.add(key)
    try:
        yield
    finally:
        owned.remove(key)
        handle.__exit__(None, None, None)


def episode_available(ep_id):
    """Advisory admission probe. Execution must still acquire its own lease."""
    try:
        with episode_execution(ep_id):
            return True
    except ProductionBusy:
        return False


def episode_task(fn):
    @wraps(fn)
    def guarded(ep_id, *args, **kwargs):
        with episode_execution(ep_id):
            return fn(ep_id, *args, **kwargs)
    return guarded


def production_storage(fn):
    @wraps(fn)
    def guarded(ep_id, *args, **kwargs):
        from app.paths import state_dir, episode_dir
        from app.managers import load
        from app.orchestrator import _load_script_md, _check_disk_space
        from app.services.process_lock import file_lock
        # Episode ownership precedes ALL source preparation/recording writes.
        # Keep the existing conservative machine-wide storage limit for now.
        with episode_execution(ep_id), file_lock(state_dir()/'production-storage.lock'):
            text = kwargs.get('script_md')
            if text is None: text = _load_script_md(load(ep_id))
            seconds = max(60, len(text)/3)
            need_mb = max(2500, math.ceil(seconds * 6 + 512))
            root = episode_dir(ep_id)
            while not root.exists(): root = root.parent
            _check_disk_space(root, need_mb=need_mb)
            _check_disk_space(Path(tempfile.gettempdir()), need_mb=need_mb)
            return fn(ep_id, *args, **kwargs)
    return guarded
