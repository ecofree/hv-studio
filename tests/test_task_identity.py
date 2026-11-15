"""Shared identity is a safety boundary, never a second scheduler or approval."""
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from app import paths
from app.services import production_identity as I
from app.services import batch_worker as B, production_admission as A, studio_jobs as J
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read
from app.services.provider_budget import ProviderBudget
from _queue_fixture import result_command
from test_production_entrypoint import production_episode

pytestmark = pytest.mark.production


def contract(tmp_path, *, owner='one', request=None, stages=('script',), path=None):
    return I.make_task('task-fixture', origin='studio', owner=owner,
        request=request or {'work':'script'}, inputs={'source':'fixed'},
        budget_path=path or tmp_path/'budget.json',
        limits=dict(tts_requests=1,characters=100,image_requests=0), stages=stages)


def test_retry_keeps_task_and_changes_attempt_without_hashing_spending(tmp_path):
    task = contract(tmp_path)
    ledger = ProviderBudget(task['budget']['path'], **task['budget']['limits'])
    first = I.execution_ref(task, 'a'*32)
    ledger.reserve('tts', 'offline-fixture', 10)
    assert contract(tmp_path) == task
    second = I.execution_ref(task, 'b'*32)
    assert first['task_id'] == second['task_id'] and first['attempt_id'] != second['attempt_id']
    assert I.execution_ref(contract(tmp_path,owner='other'), 'a'*32)['task_id'] != first['task_id']


@pytest.mark.parametrize('change', ['request','inputs','implementation','budget','settings'])
def test_changed_task_binding_is_rejected(tmp_path, monkeypatch, change):
    task = contract(tmp_path)
    if change == 'implementation': monkeypatch.setattr(I,'implementation_identity',lambda:'e'*64)
    if change == 'settings': monkeypatch.setattr(I,'load_env',lambda:{'fixture_mode':'changed'})
    expected = contract(tmp_path,request={'work':'different'} if change=='request' else None,
                        path=tmp_path/'other.json' if change=='budget' else None)
    if change == 'inputs':
        expected['inputs_sha256'] = fingerprint({'source':'different'})
        expected['spec_sha256'] = fingerprint({k:v for k,v in expected.items() if k!='spec_sha256'})
    with pytest.raises(I.TaskMismatch,match='binding changed'):
        I.require_same_task(task, expected)


def test_schema_damage_cannot_be_reinterpreted_as_legacy(tmp_path):
    task = contract(tmp_path)
    for bad in (None, [], {}, dict(task,schema_version=2), dict(task,schema_version=True),
                dict(task,task_id='corrupt'), dict(task,unexpected='data')):
        with pytest.raises((I.TaskMismatch,ValueError)):
            I.validate_task(bad)


def test_scope_isolation_and_nested_execution_replacement(tmp_path):
    task = contract(tmp_path)
    assert I.current_execution() is None
    with pytest.raises(RuntimeError,match='body failed'):
        with I.task_scope(task, 'a'*32):
            original=I.current_execution()
            copy_ref=I.current_execution();copy_ref['attempt_id']='bad'
            assert I.current_execution()==original
            with I.task_scope(task, 'a'*32): assert I.current_execution()==original
            with pytest.raises(I.TaskMismatch,match='replace'):
                with I.task_scope(task,'b'*32): pytest.fail('different attempt entered')
            raise RuntimeError('body failed')
    assert I.current_execution() is None


def test_budget_swap_blocked_before_file_creation_or_reservation(tmp_path):
    task=contract(tmp_path)
    old=ProviderBudget(tmp_path/'old.json',tts_requests=1,characters=100)
    before=file_sha256(old.path)
    with I.task_scope(task,'a'*32):
        with pytest.raises(I.TaskMismatch,match='budget differs'):
            ProviderBudget(tmp_path/'replacement.json',tts_requests=1,characters=100)
        assert not (tmp_path/'replacement.json').exists()
        with pytest.raises(I.TaskMismatch,match='budget differs'):
            old.reserve('tts','offline',1)
    assert file_sha256(old.path)==before


def test_v2_uses_worker_identity_for_result_stage_progress_and_run_state(production_episode):
    from app.services.episode_production import run
    ep,directory,_=production_episode
    path=directory/'shared-budget.json'
    task=I.make_task(ep.id,origin='batch',owner='fixture',request={'stages':['script']},
        inputs={'source':'fixture'},budget_path=path,limits=dict(tts_requests=0,characters=0,image_requests=0),
        stages=['script'])
    budget=ProviderBudget(path)
    events=[]
    with I.task_scope(task,'c'*32) as execution:
        result=run(ep.id,stages=['script'],budget=budget,progress=events.append)
    assert result['run_ok'] and result['stages_done']==['script']
    assert result['execution']==execution and result['task']==task
    assert read(directory/'production/v2/run.json')['execution']==execution
    assert events and all(row['execution']==execution for row in events)
    assert budget.snapshot()['used']==dict(tts_requests=0,characters=0,image_requests=0)


def test_wrong_v2_scope_blocks_before_loading_source(monkeypatch,tmp_path):
    from app.services.episode_production import run
    task=contract(tmp_path)
    with I.task_scope(task,'a'*32):
        with pytest.raises(I.TaskMismatch,match='episode or stages'):
            # Skip only the storage decorator: its source/disk preflight is independent.
            run.__wrapped__('task-fixture',stages=['images'])


def studio_submission(tmp_path):
    root=paths.state_dir()/'studio-jobs/task-fixture'
    request=dict(ep_id='task-fixture',expected_identity='fixture-design',job_mode='video',
                 budget_id='studio-task-fixture',max_requests=0,max_characters=0,max_images=0,
                 max_image_retries=0,task_schema=1)
    root.mkdir(parents=True)
    task=J.task_for_request(root,request,create=True)
    atomic_json(root/'request.json',request)
    atomic_json(root/'submission.json',dict(request=request,request_sha256=file_sha256(root/'request.json')))
    execution=I.execution_ref(task,'a'*32);atomic_json(root/'execution.json',execution)
    return root,request,task,execution


def test_studio_execution_validates_submission_and_budget(tmp_path):
    root,request,task,execution=studio_submission(tmp_path)
    assert J.execution_for_request(root,request)==(task,execution)
    changed=dict(request,max_images=1)
    with pytest.raises(I.TaskMismatch): J.execution_for_request(root,changed)
    (root/'task.json').unlink()
    with pytest.raises(FileNotFoundError): J.execution_for_request(root,request)


def test_studio_resume_rejects_implementation_drift_without_changing_ledger(tmp_path,monkeypatch):
    root,request,task,execution=studio_submission(tmp_path)
    budget=ProviderBudget(task['budget']['path']);before=file_sha256(budget.path)
    monkeypatch.setattr(J,'alive',lambda _:False)
    monkeypatch.setattr(J,'validate_request',lambda _:None)
    monkeypatch.setattr(I,'implementation_identity',lambda:'e'*64)
    with pytest.raises(I.TaskMismatch,match='implementation'):
        J.check_resume(root)
    assert file_sha256(budget.path)==before


def test_studio_resume_rejects_foreign_result(tmp_path,monkeypatch):
    root,request,task,execution=studio_submission(tmp_path)
    atomic_json(root/'result.json',dict(status='BLOCKED',execution=dict(execution,attempt_id='b'*32)))
    monkeypatch.setattr(J,'alive',lambda _:False)
    monkeypatch.setattr(J,'validate_request',lambda _:None)
    with pytest.raises(I.TaskMismatch,match='different task or execution'):
        J.check_resume(root)


def test_studio_attempt_rotation_retains_task_and_authorization(tmp_path,monkeypatch):
    from types import SimpleNamespace
    root,request,task,first=studio_submission(tmp_path)
    monkeypatch.setattr(J.subprocess,'Popen',lambda *a,**k:SimpleNamespace(pid=os.getpid()))
    budget=ProviderBudget(task['budget']['path']);before=file_sha256(budget.path)
    J._spawn(root,request)
    _,second=J.execution_for_request(root,request)
    assert second['task_id']==first['task_id'] and second['attempt_id']!=first['attempt_id']
    assert read(root/'checkpoint.json')['execution']==second
    assert file_sha256(budget.path)==before and read(root/'task.json')==task


def test_studio_worker_propagates_same_execution_without_new_budget(tmp_path,monkeypatch):
    from app.services import episode_production as P
    root,request,task,execution=studio_submission(tmp_path)
    spec=importlib.util.spec_from_file_location('task_bound_studio_worker',paths.ROOT/'bin/studio_job.py')
    worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    monkeypatch.setattr(J,'validate_request',lambda _:None)
    def run(**kwargs):
        assert 'task_schema' not in kwargs
        assert I.current_execution()==execution and I.current_task()==task
        return dict(status='BLOCKED',run_ok=False,error='offline fixture',approved=False)
    monkeypatch.setattr(P,'run',run)
    worker.execute(root/'request.json',request)
    assert read(root/'result.json')['execution']==execution
    assert I.current_execution() is None
    assert not Path(task['budget']['path']).exists()


def test_batch_retry_keeps_task_and_original_budget(tmp_path,monkeypatch):
    folder=tmp_path/'queue';path=B.enqueue('task-retry',['task-fixture'],{},folder=folder)
    monkeypatch.setattr(A,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='fixed'))
    def command(spec,eid,token):
        return result_command(spec,eid,token,dict(status='RETRY_WAIT',error='offline timeout'))
    B.run_once(folder,command_factory=command)
    first=read(path);task=first['episodes']['task-fixture']['task']
    execution=first['episodes']['task-fixture']['execution'];ledger=read(first['budget_path'])
    first['episodes']['task-fixture']['retry_at']=0;atomic_json(path,first)
    B.run_once(folder,command_factory=command)
    second=read(path);row=second['episodes']['task-fixture']
    assert row['attempts']==2 and row['task']==task
    assert row['execution']['attempt_id']!=execution['attempt_id']
    assert read(second['budget_path'])==ledger


def test_batch_changed_inputs_do_not_silently_rebind_or_spend(tmp_path,monkeypatch):
    folder=tmp_path/'queue';path=B.enqueue('task-inputs',['task-fixture'],{},folder=folder)
    current=['first']
    monkeypatch.setattr(A,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint=current[0]))
    B.run_once(folder,command_factory=lambda s,e,t:result_command(s,e,t,dict(status='RETRY_WAIT',error='offline timeout')))
    first=read(path);oldtask=copy.deepcopy(first['episodes']['task-fixture']['task'])
    first['episodes']['task-fixture']['retry_at']=0;atomic_json(path,first);current[0]='revised'
    B.run_once(folder,command_factory=lambda *a:pytest.fail('changed task must not spawn'))
    row=read(path)['episodes']['task-fixture']
    assert row['status']=='BLOCKED_INPUT' and row['error_type']=='TaskMismatch'
    assert row['task']==oldtask and row['attempts']==1


@pytest.mark.parametrize('bad_execution',[None,dict(task_id='x',task_sha256='y',attempt_id='z')])
def test_batch_replayed_result_is_isolated_and_other_episode_continues(tmp_path,monkeypatch,bad_execution):
    folder=tmp_path/'queue';path=B.enqueue('task-replay',['wrong','right'],{},folder=folder)
    monkeypatch.setattr(A,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='fixed'))
    def command(spec,eid,token):
        value=dict(status='NEEDS_REVIEW',candidate_path='offline-candidate')
        if eid=='wrong':value['execution']=bad_execution
        return result_command(spec,eid,token,value)
    B.run_once(folder,command_factory=command)
    state=read(path)
    assert state['episodes']['wrong']['status']=='RUNNING'
    assert state['episodes']['right']['status']=='NEEDS_REVIEW'
    assert list((folder/'errors').glob('*.json'))


def test_recovered_contention_uses_same_identity_and_attempt_accounting(tmp_path,monkeypatch):
    folder=tmp_path/'queue';path=B.enqueue('task-recover',['task-fixture'],{},retries=0,folder=folder)
    admission=dict(eligible=True,blockers=[],input_fingerprint='fixed')
    job=read(path);row=job['episodes']['task-fixture'];task=B.task_for_episode(path,job,'task-fixture',admission)
    row.update(status='RUNNING',attempts=1,token='a'*32,task=task,execution=I.execution_ref(task,'a'*32),
               child_pid=None,child_created=None)
    result=folder/'results'/('a'*32+'.json')
    atomic_json(result,dict(status='RETRY_WAIT',error='ProductionBusy: contention',
        error_type='ProductionBusy',lease_acquired=False,execution=row['execution']))
    atomic_json(path,job)
    monkeypatch.setattr('app.services.production_storage.episode_available',lambda _:False)
    B.run_once(folder,command_factory=lambda *a:pytest.fail('busy'))
    row=read(path)['episodes']['task-fixture']
    assert row['status']=='QUEUED' and row['attempts']==0
    B.run_once(folder,command_factory=lambda *a:pytest.fail('busy'))
    assert read(path)['episodes']['task-fixture']['attempts']==0


def test_actual_batch_child_rejects_changed_inputs_before_production(tmp_path,monkeypatch):
    # A real interpreter enters execute(), not a fabricated successful result.
    folder=tmp_path/'queue';path=B.enqueue('actual-child',['task-fixture'],{},folder=folder)
    monkeypatch.setattr(A,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='submitted'))
    harness=tmp_path/'child.py'
    harness.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app import paths
from app.services import batch_worker as B,production_admission as A
from app.services.narration_pipeline import read
from app import orchestrator
p=Path(sys.argv[2]);eid=sys.argv[3];token=sys.argv[4]
paths.episodes_dir=lambda:p.parent/'episodes'
paths.state_dir=lambda:p.parent/'state'
A.check=lambda _:dict(eligible=True,blockers=[],input_fingerprint='changed-after-spawn')
def forbidden(*a,**k): raise AssertionError('production must not begin')
orchestrator.produce=forbidden
row=read(p)['episodes'][eid]
raise SystemExit(B.execute(p,eid,token,row['owner_pid'],row['owner_created']))
''',encoding='utf-8')
    B.run_once(folder,command_factory=lambda s,e,t:[sys.executable,str(harness),str(paths.ROOT),str(s),e,t])
    row=read(path)['episodes']['task-fixture']
    assert row['status']=='BLOCKED_INPUT' and row['error_type']=='TaskMismatch'
    assert 'inputs_sha256' in row['error']
    assert read(read(path)['budget_path'])['attempts']==[]


@pytest.mark.parametrize('changed', ['none','attempt','path','bytes'])
def test_approved_candidate_must_belong_to_this_task(tmp_path,monkeypatch,changed):
    from app.services import production_status as P
    folder=tmp_path/'queue';path=B.enqueue('task-approval',['task-fixture'],{},folder=folder)
    job=read(path);row=job['episodes']['task-fixture']
    task=B.task_for_episode(path,job,'task-fixture',dict(input_fingerprint='fixed'))
    ref=I.execution_ref(task,'a'*32)
    candidate=tmp_path/'candidate.mp4';candidate.write_bytes(b'offline-media-fixture')
    row.update(task=task,execution=ref,token='a'*32,status='NEEDS_REVIEW',attempts=1,
               candidate_path=str(candidate),artifacts={str(candidate):file_sha256(candidate)})
    atomic_json(path,job)
    status=dict(approved=True,candidate_path=str(candidate),execution=copy.deepcopy(ref))
    if changed=='attempt':status['execution']['attempt_id']='b'*32
    elif changed=='path':status['candidate_path']=str(tmp_path/'another.mp4')
    elif changed=='bytes':candidate.write_bytes(b'replaced')
    monkeypatch.setattr(P,'get_status',lambda _:status)
    B.run_once(folder,command_factory=lambda *a:pytest.fail('approval must not start production'))
    result=read(path)['episodes']['task-fixture']
    assert result['status']==('APPROVED' if changed=='none' else 'BLOCKED_INPUT')
    assert result['attempts']==1
