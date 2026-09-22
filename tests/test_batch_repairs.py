"""Regress real queue isolation and fresh-input admission failure modes."""
import sys
import pytest
from app.services import batch_worker as worker, production_admission as admission
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json
from app.services.provider_budget import ProviderBudget

pytestmark=pytest.mark.production


@pytest.mark.parametrize('badrow',[{},None,{'status':'RUNNING','attempts':1,'token':'../../escape'},
    {'status':'QUEUED','attempts':'1'}])
def test_damaged_row_does_not_block_next_episode(tmp_path,tmp_episodes,monkeypatch,badrow):
    folder=tmp_path/'queue'
    path=worker.enqueue('batch',['bad','good'],{'max_images':1},folder=folder)
    job=read(path);budget=ProviderBudget(job['budget_path'],image_requests=1)
    budget.reserve('image','spent-before-recovery')
    job['episodes']['bad']=badrow;atomic_json(path,job)
    monkeypatch.setattr(admission,'check',lambda _:dict(eligible=False,blockers=['needs input'],input_fingerprint='x'))
    worker.run_once(folder,command_factory=lambda *a:pytest.fail('no provider or child'))
    result=read(path)
    assert result['episodes']['bad']==badrow
    assert result['episodes']['good']['status']=='NEEDS_REVIEW'
    assert list((folder/'errors').glob('*.json'))
    assert read(job['budget_path'])['used']['image_requests']==1


def test_damaged_result_and_batch_spec_are_isolated(tmp_path,tmp_episodes,monkeypatch):
    folder=tmp_path/'queue'
    broken=worker.enqueue('00-broken',['one'],{},folder=folder)
    bad=read(broken);bad['budget_path']=str(tmp_path/'outside.json');atomic_json(broken,bad)
    path=worker.enqueue('01-valid',['bad-result','good'],{},folder=folder)
    monkeypatch.setattr(admission,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='x'))
    def command(spec,eid,token):
        result=folder/'results'/(token+'.json')
        text='[]' if eid=='bad-result' else '{"status":"NEEDS_REVIEW","candidate_path":"offline"}'
        return [sys.executable,'-c','import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(exist_ok=True);p.write_text(sys.argv[2])',str(result),text]
    worker.run_once(folder,command_factory=command)
    assert read(path)['episodes']['good']['status']=='NEEDS_REVIEW'
    assert len(list((folder/'errors').glob('*.json')))==2
    assert not (tmp_path/'outside.json').exists()


def test_explicit_direction_plan_allows_first_candidate_but_never_approves_it(tmp_path,tmp_episodes,monkeypatch):
    from app import paths
    value=dict(source_sha256='source',files={},image_requests={},voice_asset_version='voice-v1',
               unsupported_cues=['cue-1'],direction_cues=[dict(cue_id='cue-1',text='【冷笑】')])
    monkeypatch.setattr(admission,'inputs',lambda _:value)
    assert not admission.check('fresh')['eligible']
    admission.record_approval('fresh','test reviewer','test-only input findings')
    assert not admission.check('fresh')['eligible']
    admission.record_approval('fresh','test reviewer','test-only input findings','candidate_review_required')
    assert admission.check('fresh')['eligible']
    assert not (paths.episode_dir('fresh')/'production/v2').exists()
    value['voice_asset_version']='voice-v2'
    assert not admission.check('fresh')['eligible']
    value['voice_asset_version']='voice-v1'
    from app.services import production_quality
    atomic_json(paths.episode_dir('fresh')/'production/v2/run.json',dict(source_sha256='source',quality_path='offline'))
    monkeypatch.setattr(production_quality,'pending',lambda _:['spoken-error'])
    assert not admission.check('fresh')['eligible']


def test_operator_retry_keeps_spending_and_attempts(tmp_path,tmp_episodes):
    folder=tmp_path/'queue';path=worker.enqueue('spent',['one'],dict(max_images=1),folder=folder)
    job=read(path);ledger=ProviderBudget(job['budget_path'],image_requests=1);ledger.reserve('image','spent')
    before=read(ledger.path)
    job['episodes']['one'].update(status='FAILED',attempts=3);atomic_json(path,job)
    worker.retry_episode('spent','one','test operator','local test recovery',folder=folder)
    row=read(path)['episodes']['one']
    assert row['attempts']==3 and row['authorized_attempt_limit']==4
    assert row['status']=='QUEUED' and read(ledger.path)==before
