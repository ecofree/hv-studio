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


def test_input_review_cannot_approve_unseen_changed_material(tmp_episodes,monkeypatch):
    from app.paths import episode_dir
    from app.services.provenance import fingerprint
    value=dict(source_sha256='shown source',unsupported_cues=[])
    monkeypatch.setattr(admission,'inputs',lambda _:value)
    shown=fingerprint(value)
    value['source_sha256']='changed while form was open'
    with pytest.raises(ValueError,match='reload'):
        admission.record_approval('one','test reviewer','reviewed old input',expected_fingerprint=shown)
    assert not (episode_dir('one')/'production/input-approval.json').exists()
    admission.record_approval('one','test reviewer','reviewed current input',expected_fingerprint=fingerprint(value))
    assert read(episode_dir('one')/'production/input-approval.json')['input_fingerprint']==fingerprint(value)


def test_pause_finishes_active_child_and_resume_keeps_budget(tmp_path,tmp_episodes,monkeypatch):
    folder=tmp_path/'queue';path=worker.enqueue('pause',['first','next'],{},folder=folder)
    budget_before=read(read(path)['budget_path'])
    monkeypatch.setattr(admission,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='x'))
    def command(spec,eid,token):
        result=folder/'results'/(token+'.json')
        script=('import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(exist_ok=True); '
                'pathlib.Path(sys.argv[2]).write_text("pause requested while running"); '
                'time.sleep(0.1); p.write_text(\'{"status":"NEEDS_REVIEW"}\')')
        return [sys.executable,'-c',script,str(result),str(folder/'PAUSE')]
    worker.run_once(folder,command_factory=command)
    job=read(path)
    assert job['episodes']['first']['status']=='NEEDS_REVIEW'
    assert job['episodes']['next']['status']=='QUEUED'
    assert read(folder/'worker.json')['status']=='paused'
    worker.run_once(folder,command_factory=lambda *a:pytest.fail('paused worker must not dispatch'))
    assert read(read(path)['budget_path'])==budget_before
    worker.set_paused(False,'test operator','resume after local repair',folder)
    monkeypatch.setattr(admission,'check',lambda _:dict(eligible=False,blockers=['input review'],input_fingerprint='x'))
    worker.run_once(folder)
    assert read(path)['episodes']['next']['status']=='NEEDS_REVIEW'
    assert read(folder/'worker.json')['status']=='idle'
    assert read(read(path)['budget_path'])==budget_before
    assert len(list((folder/'controls').glob('*.json')))==1
