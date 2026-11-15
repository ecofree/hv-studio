"""Owned-process acceptance smoke is real execution, never paid/production proof."""
from pathlib import Path
import pytest
from app.services import acceptance_runner as R
from app.services import production_acceptance as A
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, file_sha256
pytestmark=pytest.mark.production


def plan(tmp_path):
    p=tmp_path/'plan.json'
    R.make_plan(['acceptance-one','acceptance-two'],{},p,mode='offline',timeout=30)
    return p


def test_explicit_authorization_required_and_cannot_refill(tmp_path):
    p=plan(tmp_path)
    with pytest.raises(FileNotFoundError):R.run(p,scenarios=['kill_resume'])
    with pytest.raises(ValueError,match='fault injection'):R.authorize(p,'operator','smoke')
    auth=R.authorize(p,'operator','offline smoke',allow_faults=True)
    before=file_sha256(auth)
    with pytest.raises(ValueError,match='already exists'):R.authorize(p,'operator','refill',allow_faults=True)
    assert file_sha256(auth)==before


def test_offline_rejects_paid_and_real_rejects_historical_episode(tmp_path):
    with pytest.raises(ValueError,match='offline smoke'):
        R.make_plan(['acceptance-one'],{'max_images':1},tmp_path/'paid.json',mode='offline')
    with pytest.raises(ValueError,match='isolated acceptance'):
        R.make_plan(['historical-work'],{},tmp_path/'history.json')


def test_actual_children_kill_timeout_resume_and_reuse_budget(tmp_path):
    p=plan(tmp_path);R.authorize(p,'offline tester','owned processes only',allow_faults=True)
    result=R.run(p,scenarios=['multi_episode_current_rebuild','kill_resume','timeout_recovery'])
    assert all(v['status']=='PASS' for v in result['scenarios'].values()), result
    assert result['production_ready'] is False
    assert not any(result['budget']['used'].values())
    assert result['budget']['attempts']==[]
    budgets=list((tmp_path/'runs/queue/budgets').glob('*.json'))
    assert len(budgets)==1
    before=file_sha256(budgets[0])
    again=R.run(p,scenarios=['kill_resume','timeout_recovery'])
    assert all(v.get('reused_evidence') for v in again['scenarios'].values())
    assert file_sha256(budgets[0])==before
    for name in ('kill_resume','timeout_recovery'):
        pointer=read(tmp_path/f'runs/{name}/current.json');record=pointer['record']
        events=read(record['events_path'])
        assert events[0]['pid']>0
        assert events[-1]['attempt_id']!=events[0]['attempt_id']
        assert events[-1]['budget_path']==events[0]['budget_path']
        with pytest.raises(ValueError,match='offline'):
            A.verify_scenario(name,record,read(p)['source_fingerprint'])


def test_toy_worker_cannot_certify_real_providers_or_unattended_queue(tmp_path):
    p=plan(tmp_path);R.authorize(p,'offline tester','no provider allowed',allow_faults=True)
    result=R.run(p,scenarios=['unattended_worker_full_batch','real_provider_models'])
    assert all(v['status']=='BLOCKED' for v in result['scenarios'].values())
    assert result['budget']['attempts']==[]
    with pytest.raises(ValueError,match='explicit --resume'):
        R.run(p,scenarios=['unattended_worker_full_batch'])


def test_edited_plan_is_not_implicitly_reauthorized(tmp_path):
    p=plan(tmp_path);R.authorize(p,'operator','offline',allow_faults=True)
    changed=read(p);changed['timeout']=10;atomic_json(p,changed)
    with pytest.raises(ValueError,match='authorization differs'):R.run(p,scenarios=['kill_resume'])


def test_completed_candidate_tampering_invalidates_capture(tmp_path):
    from app.services.acceptance_capture import Capture
    from app.services import production_identity as I
    from app.services.provider_budget import ProviderBudget
    b=ProviderBudget(tmp_path/'budget.json')
    task=I.make_task('acceptance-one',origin='direct',owner='fixture',request={},inputs={},
        budget_path=b.path,limits=b.limits,stages=I.V2_STAGES)
    capture=Capture(tmp_path/'evidence','multi_episode_current_rebuild','fp','a'*40,mode='offline')
    p=tmp_path/'candidate';p.write_bytes(b'before');sha=file_sha256(p);p.write_bytes(b'after')
    with pytest.raises(ValueError,match='candidate changed'):
        capture.completed('episode_rebuild_completed',dict(run_ok=True,final_mp4=str(p),candidate_sha256=sha,
            execution=I.execution_ref(task,'b'*32)),task=task,attempt='b'*32)
