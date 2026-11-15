"""Bounded acceptance supervisor, separate from normal production scheduling.

Real runs require an immutable plan + named explicit authorization and isolated
acceptance-* episodes. Offline smoke uses toy files, never real media/providers.
No function here approves content or writes a production-ready certificate.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import uuid
import psutil
from app import paths
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read
from app.services.acceptance_capture import Capture, budget_snapshot
from app.services import production_identity as I
from app.services.provider_budget import ProviderBudget, budget_limits


def revision():
    return subprocess.check_output(['git','rev-parse','HEAD'],cwd=paths.ROOT,text=True).strip()


def make_plan(episodes, options, output, *, mode='real', timeout=3600, fault_stage='compose'):
    if mode not in ('real','offline') or not episodes or len(set(episodes))!=len(episodes):
        raise ValueError('explicit unique acceptance episodes and mode required')
    if type(timeout) is not int or not 1<=timeout<=14400 or fault_stage not in I.V2_STAGES:
        raise ValueError('invalid bounded acceptance timeout/stage')
    if not isinstance(options,dict) or set(options)-{'max_requests','max_characters','max_images','max_image_retries'}:
        raise ValueError('unknown acceptance budget option')
    limits=budget_limits(options)
    if mode=='offline' and any(limits.values()): raise ValueError('offline smoke cannot authorize provider spending')
    cases=[]
    from app.services.production_acceptance import source_fingerprint
    for eid in episodes:
        paths.validate_episode_id(eid)
        if not eid.startswith('acceptance-'): raise ValueError('use isolated acceptance-* episodes, never historical work')
        if mode=='real':
            if not paths.episode_dir(eid).resolve().is_relative_to((paths.ROOT/'workspace/episodes').resolve()):
                raise ValueError('real acceptance requires the standalone workspace')
            if (paths.episode_dir(eid)/'production/v2/published.json').exists():
                raise ValueError('do not run fault injection on a published episode')
            from app.services.production_admission import check
            from app.services.recording_input import episode_source
            admission=check(eid)
            if not admission['eligible']: raise ValueError(eid+': '+'; '.join(admission['blockers']))
            audio=episode_source(eid)
            kind='tts' if audio['requires_tts'] else audio['selection']['kind']
            cases.append(dict(episode_id=eid,input_fingerprint=admission['input_fingerprint'],audio_mode=kind))
        else: cases.append(dict(episode_id=eid,input_fingerprint='offline-fixture',audio_mode='offline'))
    value=dict(schema_version=1,plan_id=uuid.uuid4().hex,evidence_mode=mode,git_revision=revision(),
               source_fingerprint=source_fingerprint(),settings_sha256=I.settings_identity(),
               root=str(paths.ROOT.resolve()),cases=cases,options=options,limits=limits,
               timeout=timeout,fault_stage=fault_stage,production_ready=False)
    output=Path(output).resolve()
    if output.exists(): raise ValueError('acceptance plan is immutable; choose a new path')
    atomic_json(output,value);return value


def authorize(plan_path, operator, reason, *, allow_paid=False, allow_faults=False):
    plan=read(plan_path)
    if not operator.strip() or not reason.strip(): raise ValueError('named operator and reason required')
    if any(plan['limits'].values()) and not allow_paid: raise ValueError('explicit paid-budget authorization required')
    if not allow_faults: raise ValueError('explicit owned-worker fault injection authorization required')
    target=Path(plan_path).parent/'authorization.json'
    if target.exists(): raise ValueError('authorization already exists; never replace or refill it')
    value=dict(plan_sha256=file_sha256(plan_path),operator=operator.strip(),reason=reason.strip(),
               limits=plan['limits'],allow_faults=True,allow_paid=allow_paid,at=time.time())
    atomic_json(target,value);return target


def _check_plan(plan_path):
    plan=read(plan_path);auth=read(Path(plan_path).parent/'authorization.json')
    from app.services.production_acceptance import source_fingerprint
    if (auth['plan_sha256']!=file_sha256(plan_path) or auth['limits']!=plan['limits']
            or auth.get('allow_faults') is not True or not auth.get('operator')
            or any(plan['limits'].values()) and auth.get('allow_paid') is not True):
        raise ValueError('acceptance authorization differs from plan')
    if (plan['git_revision']!=revision() or plan['source_fingerprint']!=source_fingerprint()
            or plan['settings_sha256']!=I.settings_identity() or plan['root']!=str(paths.ROOT.resolve())):
        raise ValueError('acceptance code/settings/root changed; do not silently repin')
    return plan


def _task(plan, case, budget, owner):
    return I.make_task(case['episode_id'],origin='direct',owner=owner,request={'plan_id':plan['plan_id']},
            inputs={'admission':case['input_fingerprint']},budget_path=budget.path,limits=budget.limits,
            stages=I.V2_STAGES)


def invoke(plan_path, task, budget, root, capture, *, start_event=None, fault=None, previous=None):
    """Launch only our fixed worker; wait for a real stage barrier before killing."""
    plan=read(plan_path);attempt=uuid.uuid4().hex;folder=Path(root)/attempt;folder.mkdir(parents=True)
    owner=psutil.Process()
    spec=dict(plan_path=str(Path(plan_path).resolve()),task=task,attempt_id=attempt,
              budget_path=str(budget.path.resolve()),mode=plan['evidence_mode'],
              fault=fault,fault_stage=plan['fault_stage'],owner_pid=owner.pid,owner_created=owner.create_time(),
              work_root=str((Path(plan_path).parent/'offline-work'/task['episode_id']).resolve()))
    spec_path=folder/'request.json';atomic_json(spec_path,spec)
    # Snapshot before spawn: fast children must not spend ahead of the baseline.
    before=budget_snapshot(budget.path,folder)
    with (folder/'worker.log').open('wb') as log:
        child=subprocess.Popen([sys.executable,str(paths.ROOT/'bin/acceptance_worker.py'),str(spec_path)],
            cwd=paths.ROOT,stdout=log,stderr=log,env=dict(os.environ,PYTHONUTF8='1',PYTHONUNBUFFERED='1'))
        created=psutil.Process(child.pid).create_time()
        if start_event:
            capture.emit(start_event,task=task,attempt=attempt,pid=child.pid,process_created_at=created,
                         previous_attempt_id=previous,**before)
        deadline=time.monotonic()+plan['timeout'];barrier_seen=None;terminated=False
        try:
            while child.poll() is None:
                barrier=folder/'fault-ready.json'
                if fault and barrier.exists():
                    proof=read(barrier)
                    I.require_execution(proof.get('execution'),I.execution_ref(task,attempt))
                    if proof.get('stage')!=plan['fault_stage']: raise ValueError('fault stage differs from plan')
                    if barrier_seen is None: barrier_seen=time.monotonic()
                    if fault=='kill' or time.monotonic()-barrier_seen>=1:
                        if fault=='timeout':
                            capture.emit('timeout_detected',task=task,attempt=attempt,pid=child.pid,
                                         process_created_at=created,timeout_seconds=1)
                        from app.services.batch_worker import stop_tree
                        stop_tree(child.pid,created);child.wait(timeout=10);terminated=True
                        capture.emit('worker_terminated',task=task,attempt=attempt,pid=child.pid,
                                     process_created_at=created,exit_code=child.returncode)
                        break
                if time.monotonic()>=deadline: raise TimeoutError('acceptance worker deadline exceeded before completion')
                time.sleep(.05)
            if fault:
                if not terminated: raise RuntimeError('worker never reached the requested fault barrier')
                return None,attempt
            child.wait(timeout=10)
            result=read(folder/'result.json')
            I.require_execution(result.get('execution'),I.execution_ref(task,attempt))
            if child.returncode or not result.get('run_ok'):
                raise RuntimeError(result.get('error') or 'acceptance child failed')
            return result,attempt
        finally:
            if child.poll() is None:
                from app.services.batch_worker import stop_tree
                stop_tree(child.pid,created);child.wait(timeout=10)


def run(plan_path, *, scenarios=None, resume=False):
    """No automatic reauthorization: all scenarios share one durable ledger."""
    plan_path=Path(plan_path).resolve();plan=_check_plan(plan_path)
    selected=scenarios or ['multi_episode_current_rebuild','kill_resume','timeout_recovery','unattended_worker_full_batch','real_provider_models']
    allowed={'multi_episode_current_rebuild','kill_resume','timeout_recovery','unattended_worker_full_batch','real_provider_models'}
    if not set(selected)<=allowed or len(set(selected))!=len(selected): raise ValueError('invalid acceptance scenarios')
    from app.services import batch_worker as B
    from app.services.process_lock import file_lock
    root=plan_path.parent/'runs';root.mkdir(exist_ok=True)
    with file_lock(root/'supervisor.lock'):
        # Use the actual queue authorization as the only ledger, also for the
        # direct fault scenarios. No per-scenario refill or shadow budgets.
        queue=B.enqueue('acceptance-'+plan['plan_id'],[c['episode_id'] for c in plan['cases']],
                        plan['options'],timeout=plan['timeout'],folder=root/'queue')
        budget=ProviderBudget(read(queue)['budget_path'],**plan['limits'])
        outcomes={}; provider_capture=None
        # Begin provider observation BEFORE production, excluding old caches.
        ordered=sorted(selected,key=lambda s:s!='real_provider_models')
        for scenario in ordered:
            parent=root/scenario; parent.mkdir(exist_ok=True)
            pointer=parent/'current.json'
            if pointer.exists():
                from app.services.production_acceptance import verify_scenario
                prior=read(pointer)
                try:
                    verify_scenario(scenario,prior['record'],plan['source_fingerprint'],require_real=plan['evidence_mode']=='real')
                    outcomes[scenario]=dict(status='PASS',evidence_mode=plan['evidence_mode'],reused_evidence=True)
                    continue
                except (ValueError,OSError,KeyError,TypeError):
                    if not resume: raise ValueError('existing incomplete evidence requires explicit --resume')
            trials=parent/'trials';trials.mkdir(exist_ok=True)
            if len(list(trials.iterdir()))>=3: raise ValueError('bounded scenario trial limit reached; review evidence')
            destination=trials/uuid.uuid4().hex
            capture=Capture(destination,scenario,plan['source_fingerprint'],plan['git_revision'],mode=plan['evidence_mode'])
            atomic_json(pointer,dict(run_directory=str(destination),run_sha256=file_sha256(destination/'run.json'),record={}))
            if scenario=='real_provider_models':
                provider_capture=capture
                continue
            try:
                if scenario in ('kill_resume','timeout_recovery'):
                    case=plan['cases'][0];task=_task(plan,case,budget,plan['plan_id']+':'+scenario)
                    _,old=invoke(plan_path,task,budget,destination,capture,start_event='worker_started',
                                 fault='kill' if scenario=='kill_resume' else 'timeout')
                    begin='resume_started' if scenario=='kill_resume' else 'recovery_started'
                    end='resume_completed' if scenario=='kill_resume' else 'recovery_completed'
                    result,attempt=invoke(plan_path,task,budget,destination,capture,start_event=begin,previous=old)
                    capture.completed(end,result,task=task,attempt=attempt,**budget_snapshot(budget.path,destination))
                elif scenario=='multi_episode_current_rebuild':
                    for case in plan['cases']:
                        task=_task(plan,case,budget,plan['plan_id']+':'+scenario)
                        result,attempt=invoke(plan_path,task,budget,destination,capture)
                        capture.completed('episode_rebuild_completed',result,task=task,attempt=attempt)
                else:
                    if plan['evidence_mode']!='real':
                        raise ValueError('offline toy worker does not certify the unattended production queue')
                    capture.emit('batch_started',batch_id=read(queue)['spec']['batch_id'])
                    B.run_once(root/'queue')
                    job=read(queue)
                    for eid,row in job['episodes'].items():
                        if row['status'] not in ('NEEDS_REVIEW','APPROVED') or not row.get('candidate_path'):
                            raise ValueError('actual batch did not complete: '+eid+' '+row['status'])
                        result=dict(run_ok=True,execution=row['execution'],final_mp4=row['candidate_path'],
                                    candidate_sha256=file_sha256(row['candidate_path']))
                        capture.completed('episode_candidate_completed',result,task=row['task'],attempt=row['token'],batch_id=job['spec']['batch_id'])
                    capture.emit('batch_completed',batch_id=job['spec']['batch_id'])
                record=capture.close();outcomes[scenario]=dict(status='PASS',evidence_mode=plan['evidence_mode'])
                atomic_json(pointer,dict(run_directory=str(destination),run_sha256=file_sha256(destination/'run.json'),record=record))
            except Exception as exc:
                capture.fail(exc);outcomes[scenario]=dict(status='BLOCKED',error=str(exc))
        if provider_capture is not None:
            try:
                from app.services.acceptance_providers import capture as collect_providers
                collect_providers(provider_capture,plan['cases'],budget)
                record=provider_capture.close()
                p=root/'real_provider_models/current.json'
                atomic_json(p,dict(run_directory=str(provider_capture.folder),run_sha256=file_sha256(provider_capture.folder/'run.json'),record=record))
                outcomes['real_provider_models']=dict(status='PASS',evidence_mode=plan['evidence_mode'])
            except Exception as exc:
                provider_capture.fail(exc);outcomes['real_provider_models']=dict(status='BLOCKED',error=str(exc))
        report=dict(plan_id=plan['plan_id'],scenarios=outcomes,budget=budget.snapshot(),
                    production_ready=False,content_review='required',evidence_mode=plan['evidence_mode'])
        atomic_json(root/'summary.json',report)
        return report


def child(spec_path):
    """Only called in the owned subprocess. No arbitrary command execution."""
    import threading
    spec_path=Path(spec_path);spec=read(spec_path);plan=_check_plan(spec['plan_path'])
    task=spec['task'];attempt=spec['attempt_id'];execution=I.execution_ref(task,attempt)
    def watch_owner():
        from app.services.batch_worker import alive
        while alive(spec['owner_pid'],spec['owner_created']):time.sleep(.5)
        for process in psutil.Process().children(recursive=True):
            try:process.kill()
            except psutil.NoSuchProcess:pass
        os._exit(75)
    threading.Thread(target=watch_owner,daemon=True).start()
    def progress(value):
        atomic_json(spec_path.parent/'progress.json',dict(value,execution=execution,observed_at=time.time()))
        if spec['fault'] and value.get('stage')==spec['fault_stage']:
            atomic_json(spec_path.parent/'fault-ready.json',dict(execution=execution,stage=value['stage']))
            # This deliberate barrier is killed by our supervisor. It does not
            # pretend to simulate a supplier-side timeout.
            while True:time.sleep(.05)
    try:
        with I.task_scope(task,attempt):
            budget=ProviderBudget(spec['budget_path'],**task['budget']['limits'])
            if spec['mode']=='offline':
                root=Path(spec['work_root']);root.mkdir(parents=True,exist_ok=True)
                for stage in I.V2_STAGES:progress(dict(stage=stage))
                candidate=root/'offline-candidate.json'
                if not candidate.exists():atomic_json(candidate,dict(kind='offline-only',episode_id=task['episode_id']))
                result=dict(run_ok=True,final_mp4=str(candidate),execution=execution,approved=False)
            else:
                from app.services.production_admission import check
                admission=check(task['episode_id'])
                case=next(c for c in plan['cases'] if c['episode_id']==task['episode_id'])
                if not admission['eligible'] or admission['input_fingerprint']!=case['input_fingerprint']:
                    raise ValueError('acceptance input changed or no longer eligible')
                from app.services.episode_production import run as produce
                result=produce(task['episode_id'],budget=budget,progress=progress,**plan['options'])
            if result.get('run_ok') and result.get('final_mp4'):
                result['candidate_sha256']=file_sha256(result['final_mp4'])
    except Exception as exc:
        result=dict(run_ok=False,execution=execution,error_type=type(exc).__name__,error=str(exc))
    atomic_json(spec_path.parent/'result.json',result)
    return 0 if result.get('run_ok') else 1
