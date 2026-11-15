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


class ResourceBusy(RuntimeError):
    """Expected resource contention, not a failed supplier request."""
    def __init__(self, resource):
        self.resource = resource
        super().__init__('waiting for resource: '+resource)


@contextmanager
def machine_storage():
    from app.paths import state_dir
    from app.services.process_lock import file_lock
    lease = file_lock(state_dir()/'production-storage.lock')
    try: lease.__enter__()
    except RuntimeError as exc: raise ResourceBusy('machine-storage') from exc
    try: yield
    finally: lease.__exit__(None, None, None)


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


def resource_policy():
    """Opt-in overlap only. Defaults preserve single-production capacity."""
    from app.paths import state_dir
    from app.services.narration_pipeline import read
    p=state_dir()/'resource-policy.json'
    value=read(p) if p.is_file() else {}
    if not isinstance(value,dict) or set(value)-{'max_active_episodes','min_available_ram_mb'}:
        raise ValueError('unsupported resource policy')
    policy=dict(max_active_episodes=value.get('max_active_episodes',1),
                min_available_ram_mb=value.get('min_available_ram_mb',2048))
    if (type(policy['max_active_episodes']) is not int or not 1<=policy['max_active_episodes']<=4
            or type(policy['min_available_ram_mb']) is not int or policy['min_available_ram_mb']<2048):
        raise ValueError('invalid bounded resource policy')
    return policy


def active_reservations():
    """Called under machine_storage. Kernel leases, not PID guesses, own capacity."""
    from app.paths import state_dir
    from app.services.narration_pipeline import read
    from app.services.process_lock import file_lock
    import re
    folder=state_dir()/'resource-leases';active=[]
    for path in folder.glob('*.json'):
        if not re.fullmatch('[0-9a-f]{32}',path.stem):
            raise ValueError('invalid resource reservation filename')
        lease=file_lock(path.with_suffix('.lock'))
        try:lease.__enter__()
        except RuntimeError:
            active.append(read(path))
        else:
            try:path.unlink()  # Only this service's stale reservation, never media.
            finally:lease.__exit__(None,None,None)
    return active


def require_idle_resources():
    if active_reservations():raise ResourceBusy('active-production-snapshot')


@contextmanager
def resource_reservation(ep_id, script_md=None):
    import psutil, uuid
    from app.paths import state_dir,episode_dir
    from app.managers import load
    from app.orchestrator import _load_script_md,_check_disk_space
    from app.services.process_lock import file_lock,wait_file_lock
    from app.services.provenance import atomic_json,fingerprint
    lease=None;target=None
    with machine_storage():
        policy=resource_policy();active=active_reservations()
        if len(active)>=policy['max_active_episodes']:raise ResourceBusy('episode-capacity')
        if any(a['policy_sha256']!=fingerprint(policy) for a in active):
            raise ResourceBusy('resource-policy-change')
        required_ram=policy['min_available_ram_mb']*(len(active)+1)
        if psutil.virtual_memory().available<required_ram*1024**2:raise ResourceBusy('available-memory')
        text=script_md if script_md is not None else _load_script_md(load(ep_id))
        need_mb=max(2500,math.ceil(max(60,len(text)/3)*6+512))
        volumes={};roots={}
        for p in (episode_dir(ep_id),Path(tempfile.gettempdir())):
            while not p.exists():p=p.parent
            device=str(p.stat().st_dev)
            roots[device]=p;volumes[device]=volumes.get(device,0)+need_mb
        # Same-device project and temporary space are summed, not counted as
        # independent free-space pools. Outstanding reservations are subtracted.
        for device,demand in volumes.items():
            reserved=sum(a['volumes_mb'].get(device,0) for a in active)
            _check_disk_space(roots[device],need_mb=demand+reserved)
        token=uuid.uuid4().hex;target=state_dir()/'resource-leases'/(token+'.json')
        lease=file_lock(target.with_suffix('.lock'));lease.__enter__()
        try:
            atomic_json(target,dict(episode_id=ep_id,token=token,volumes_mb=volumes,
                        pid=os.getpid(),policy_sha256=fingerprint(policy)))
        except BaseException:
            lease.__exit__(None,None,None);raise
    try:yield
    finally:
        try:
            with wait_file_lock(state_dir()/'production-storage.lock',timeout=10):
                target.unlink(missing_ok=True)
        finally:lease.__exit__(None,None,None)


def production_storage(fn):
    @wraps(fn)
    def guarded(ep_id,*args,**kwargs):
        with episode_execution(ep_id),resource_reservation(ep_id,kwargs.get('script_md')):
            return fn(ep_id,*args,**kwargs)
    return guarded


@contextmanager
def render_lease():
    """Both renderers share one bounded encoder lane; never race GPU encoders."""
    from app.paths import state_dir
    from app.services.process_lock import file_lock
    key=(os.getpid(),threading.get_ident())
    if getattr(_owned,'render',None)==key:
        yield;return
    lease=file_lock(state_dir()/'resource-leases/render.lock')
    try:lease.__enter__()
    except RuntimeError as exc:raise ResourceBusy('video-encoder') from exc
    _owned.render=key
    try:yield
    finally:
        _owned.render=None;lease.__exit__(None,None,None)


def render_resource(fn):
    @wraps(fn)
    def guarded(*args,**kwargs):
        with render_lease():return fn(*args,**kwargs)
    return guarded
