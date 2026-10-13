"""Durable local queue with one owner, bounded child processes and shared budgets.

No automatic budget increases or content approvals. STOP pauses at any point;
restarting uses the same immutable job specification and ledger.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import psutil
from app import paths
from app.services.provenance import atomic_json, fingerprint
from app.services.narration_pipeline import read
from app.services.process_lock import file_lock
from app.services.queue_records import load_result


def queue_dir(): return paths.state_dir()/'production-queue'


def enqueue(batch_id, episodes, options, *, timeout=3600, retries=2, folder=None):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', batch_id): raise ValueError('invalid batch ID')
    if not episodes or len(set(episodes)) != len(episodes): raise ValueError('episodes must be nonempty and unique')
    for eid in episodes:
        paths.episode_dir(eid); paths.episode_json(eid)
    if set(options)-{'max_requests','max_characters','max_images','max_image_retries'}:
        raise ValueError('unsupported unattended options')
    limits = {key:options.get(key, 0) for key in ('max_requests','max_characters','max_images','max_image_retries')}
    if any(type(v) is not int or v < 0 for v in limits.values()): raise ValueError('invalid budget')
    if not 1 <= timeout <= 14400 or not 0 <= retries <= 3: raise ValueError('invalid worker bounds')
    spec = dict(batch_id=batch_id, episodes=list(episodes), options=limits, timeout=timeout, retries=retries)
    folder = Path(folder or queue_dir()); path = folder/(batch_id+'.json')
    with file_lock(folder/(batch_id+'.enqueue.lock')):
        if path.exists():
            if read(path)['spec'] != spec: raise ValueError('batch ID already bound to another specification')
            return path
        from app.services.provider_budget import for_run
        budget = for_run(folder, ['unattended', batch_id], limits)
        atomic_json(path, dict(spec=spec, budget_path=str(budget.path.resolve()), status='QUEUED',
                              episodes={eid:dict(status='QUEUED', attempts=0) for eid in episodes},
                              events=[], created_at=time.time()))
    return path


def alive(pid, created):
    try: return psutil.Process(pid).create_time() == created
    except (psutil.Error, ValueError, TypeError): return False


def stop_tree(pid, created):
    if not alive(pid, created): return
    parent = psutil.Process(pid)
    owned = parent.children(recursive=True)+[parent]
    for proc in reversed(owned):
        try: proc.terminate()
        except psutil.NoSuchProcess: pass
    _, remaining = psutil.wait_procs(owned, timeout=5)
    for proc in remaining:
        try: proc.kill()
        except psutil.NoSuchProcess: pass
    psutil.wait_procs(remaining, timeout=5)


def classify(error):
    low = error.lower()
    if 'budget' in low: return 'BUDGET_EXHAUSTED'
    if any(s in low for s in ('review', 'disposition', 'unsupported artistic')): return 'NEEDS_REVIEW'
    if any(s in low for s in ('timeout', 'timed out', 'transientimageerror', 'interrupted', 'window-close event', 'already running', '429', '503', '502', 'connection')):
        return 'RETRY_WAIT'
    return 'BLOCKED_INPUT'


def event(job, eid, status, reason=None):
    job['events'].append(dict(at=time.time(), episode=eid, status=status, reason=reason))
    job['events'] = job['events'][-200:]


def execute(spec_path, eid, token, owner_pid, owner_created):
    """Private child entry: durable RUNNING record must exist before work starts."""
    import threading
    spec_path = Path(spec_path)
    deadline = time.monotonic()+15
    while True:
        job = read(spec_path); row = job['episodes'][eid]
        if row.get('token') == token and row.get('child_pid'): break
        if time.monotonic()>deadline or not alive(owner_pid, owner_created): return 1
        time.sleep(.1)

    def watch_owner():
        while alive(owner_pid, owner_created): time.sleep(.5)
        for child in psutil.Process().children(recursive=True):
            try: child.kill()
            except psutil.NoSuchProcess: pass
        os._exit(75)
    threading.Thread(target=watch_owner, daemon=True).start()
    from app.services.production_admission import check
    from app.services.provider_budget import ProviderBudget
    from app.orchestrator import produce
    from app.services.production_identity import artifact_receipt, input_identity
    result_path = spec_path.parent/'results'/(token+'.json')
    try:
        admission = check(eid)
        if not admission['eligible']: raise ValueError('review required: '+'; '.join(admission['blockers']))
        options = job['spec']['options']
        ledger = read(job['budget_path'])
        budget = ProviderBudget(job['budget_path'], **ledger['limits'])
        result = produce(eid, budget=budget, **options)
        if not result.get('run_ok') or not result.get('final_mp4'):
            raise RuntimeError(result.get('error') or 'full production has no candidate')
        from app.services.production_status import get_status
        status = get_status(eid)
        record = dict(status='APPROVED' if status['approved'] else 'NEEDS_REVIEW',
                      candidate_path=result['final_mp4'], artifacts=artifact_receipt(result),
                      identity=input_identity(eid, None, options), blockers=status['blockers'])
    except Exception as exc:
        error = f'{type(exc).__name__}: {str(exc)[:1000]}'
        record = dict(status=classify(error), error=error)
    atomic_json(result_path, record)
    return 0


def run_once(folder=None, *, command_factory=None):
    """Isolate invalid batches and episodes, preserving their original evidence."""
    import threading
    from app.services.queue_records import validate_job, validate_row
    folder=Path(folder or queue_dir());folder.mkdir(parents=True,exist_ok=True)
    with file_lock(folder/'worker.lock'):
        owner=psutil.Process()
        if (folder/'PAUSE').exists():
            atomic_json(folder/'worker.json',dict(pid=owner.pid,created_at=owner.create_time(),
                        heartbeat=time.time(),status='paused'))
            return
        done=threading.Event()
        def heartbeat():
            while not done.is_set():
                atomic_json(folder/'worker.json',dict(pid=owner.pid,created_at=owner.create_time(),heartbeat=time.time(),status='scanning'))
                done.wait(5)
        ticker=threading.Thread(target=heartbeat,daemon=True);ticker.start()
        try:
            for path in sorted(folder.glob('*.json')):
                if path.name=='worker.json': continue
                if (folder/'STOP').exists() or (folder/'PAUSE').exists(): break
                try:
                    job=read(path);validate_job(job,path)
                except Exception as exc:
                    _record_error(folder,path,None,exc);continue
                _resolve_error(folder,path,None)
                for eid,row in job['episodes'].items():
                    if (folder/'STOP').exists() or (folder/'PAUSE').exists(): break
                    try:
                        validate_row(row)
                        _run_episode(folder,path,job,eid,row,owner,command_factory)
                    except Exception as exc:
                        _record_error(folder,path,eid,exc)
                        # Keep damaged rows intact for repair; never manufacture
                        # ownership, attempts or an empty replacement budget.
                        continue
                    _resolve_error(folder,path,eid)
                job['status']='APPROVED' if all(isinstance(r,dict) and r.get('status')=='APPROVED' for r in job['episodes'].values()) else 'PENDING'
                atomic_json(path,job)
        finally:
            done.set();ticker.join(timeout=10)
            atomic_json(folder/'worker.json',dict(pid=owner.pid,created_at=owner.create_time(),heartbeat=time.time(),
                        status='paused' if (folder/'PAUSE').exists() else 'idle'))


def _error_path(folder,path,eid):
    return folder/'errors'/((path.stem if eid is None else path.stem+'-'+fingerprint(eid)[:16])+'.json')


def _record_error(folder,path,eid,exc):
    atomic_json(_error_path(folder,path,eid),dict(error=type(exc).__name__,message=str(exc)[:500],path=str(path),episode=eid,at=time.time()))


def _resolve_error(folder,path,eid):
    target=_error_path(folder,path,eid)
    if target.exists():
        try: prior=read(target)
        except (ValueError,OSError):return
        if isinstance(prior,dict) and not prior.get('resolved_at'):
            prior.update(resolved_at=time.time());atomic_json(target,prior)


def set_paused(paused,operator,reason,folder=None):
    """Pause between episodes while keeping the supervised worker alive."""
    if type(paused) is not bool or not operator.strip() or not reason.strip():
        raise ValueError('explicit pause state, operator and reason required')
    folder=Path(folder or queue_dir())
    with file_lock(folder/'control.lock'):
        record=dict(paused=paused,operator=operator.strip(),reason=reason.strip(),at=time.time())
        if paused: atomic_json(folder/'PAUSE',record)
        else: (folder/'PAUSE').unlink(missing_ok=True)
        atomic_json(folder/'controls'/(uuid.uuid4().hex+'.json'),record)
    return record


def retry_episode(batch_id,eid,operator,reason,extra_attempts=1,folder=None):
    """Explicitly retry a stopped job without erasing spending or attempt history."""
    from app.services.queue_records import validate_job,validate_row,integer
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',batch_id):raise ValueError('invalid batch ID')
    paths.validate_episode_id(eid);integer(extra_attempts,1)
    if extra_attempts>3 or not operator.strip() or not reason.strip():raise ValueError('operator, reason and 1..3 attempts required')
    folder=Path(folder or queue_dir());path=folder/(batch_id+'.json')
    with file_lock(folder/'worker.lock'):
        job=read(path);validate_job(job,path);row=job['episodes'][eid];validate_row(row)
        if row['status'] not in ('FAILED','BLOCKED_INPUT','BUDGET_EXHAUSTED','RUNNING'):
            raise ValueError('only stopped or interrupted jobs can be retried')
        if row['status']=='RUNNING' and alive(row.get('child_pid'),row.get('child_created')):
            raise ValueError('production child is still alive')
        prior_status=row['status']
        row['authorized_attempt_limit']=row['attempts']+extra_attempts
        row.setdefault('retry_authorizations',[]).append(dict(operator=operator.strip(),reason=reason.strip(),
            prior_status=prior_status,attempts_before=row['attempts'],extra_attempts=extra_attempts,at=time.time()))
        row.update(status='QUEUED');row.pop('retry_at',None)
        event(job,eid,'QUEUED','explicit retry: '+reason.strip());job['status']='PENDING';atomic_json(path,job)
    return dict(batch_id=batch_id,episode=eid,status='QUEUED',attempts=row['attempts'],budget_path=job['budget_path'])

def _run_episode(folder, path, job, eid, row, owner, command_factory):
    if (folder/'STOP').exists(): return
    if row['status'] == 'RUNNING':
        stop_tree(row.get('child_pid'), row.get('child_created'))
        result_path = folder/'results'/(row['token']+'.json')
        if result_path.exists(): row.update(load_result(result_path))
        else: row.update(status='RETRY_WAIT', error='interrupted worker', retry_at=time.time())
        event(job, eid, row['status'], 'recovered previous owner')
        atomic_json(path, job)
    if row['status'] == 'APPROVED':
        from app.services.production_status import get_status
        if get_status(eid).get('approved'): return
        row['status'] = 'NEEDS_REVIEW'
    if row['status'] == 'NEEDS_REVIEW' and row.get('candidate_path'):
        from app.services.production_status import get_status
        status = get_status(eid)
        if status['approved']:
            for key in ('blockers', 'error', 'retry_at'):
                row.pop(key, None)
            row['status']='APPROVED'; event(job,eid,'APPROVED'); atomic_json(path,job)
            return
        elif status['status'] == 'STALE' and not status['quality_pending_units'] and not status['delivery_pending']:
            row['status']='QUEUED'
        else: return
    if row['status'] in ('BUDGET_EXHAUSTED','FAILED'): return
    if row.get('retry_at', 0)>time.time(): return
    from app.services.production_admission import check
    admission = check(eid)
    if row['status']=='BLOCKED_INPUT' and row.get('admission_fingerprint')==admission.get('input_fingerprint'):
        return
    if not admission['eligible']:
        if row['status'] != 'NEEDS_REVIEW' or row.get('blockers') != admission['blockers']:
            row.update(status='NEEDS_REVIEW', blockers=admission['blockers'])
            event(job,eid,'NEEDS_REVIEW','input admission'); atomic_json(path,job)
        return
    if row['attempts'] >= row.get('authorized_attempt_limit',1+job['spec']['retries']):
        row['status']='FAILED'; event(job,eid,'FAILED','retry limit'); atomic_json(path,job); return
    token = uuid.uuid4().hex
    # Previous failure details remain in events; they are not current blockers.
    for key in ('blockers', 'error', 'retry_at'):
        row.pop(key, None)
    row.update(status='RUNNING', attempts=row['attempts']+1, token=token,
               admission_fingerprint=admission.get('input_fingerprint'),
               started_at=time.time(), owner_pid=owner.pid, owner_created=owner.create_time(),
               child_pid=None, child_created=None)
    event(job,eid,'RUNNING'); atomic_json(path,job)
    command = ([sys.executable, str(paths.ROOT/'bin/production_worker.py'), '--execute', str(path.resolve()),
                '--episode', eid, '--token', token, '--owner-pid', str(owner.pid),
                '--owner-created', str(owner.create_time())] if command_factory is None else command_factory(path,eid,token))
    log_path = folder/'logs'/(token+'.log'); log_path.parent.mkdir(exist_ok=True)
    with log_path.open('ab') as log:
        child = subprocess.Popen(command, cwd=paths.ROOT, stdout=log, stderr=log,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            row.update(child_pid=child.pid, child_created=psutil.Process(child.pid).create_time())
            atomic_json(path,job)
            while child.poll() is None and not (folder/'STOP').exists():
                if time.time()-row['started_at'] > job['spec']['timeout']: break
                atomic_json(folder/'worker.json', dict(pid=owner.pid, created_at=owner.create_time(),
                    heartbeat=time.time(), batch=job['spec']['batch_id'], episode=eid, deadline=row['started_at']+job['spec']['timeout']))
                time.sleep(.5)
        finally:
            if child.poll() is None:
                if row.get('child_created') is not None: stop_tree(child.pid,row['child_created'])
                else: child.terminate()
            child.wait(timeout=10)
    result_path = folder/'results'/(token+'.json')
    if result_path.exists(): row.update(load_result(result_path))
    else: row.update(status='RETRY_WAIT', error='interrupted or timed out child')
    if row['status']=='RETRY_WAIT': row['retry_at']=time.time()+min(2**row['attempts'],60)
    event(job,eid,row['status'],row.get('error')); atomic_json(path,job)
