"""Regression boundaries from the 8430d470 read-only audit. No providers."""
from pathlib import Path
import copy
import pytest
from app import paths
from app.services import batch_worker as B, production_backup as BK, production_quality as Q
from app.services.provider_budget import ProviderBudget, BudgetExceeded
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read
from test_production_readiness import evidence

pytestmark = pytest.mark.production


def test_active_studio_snapshot_restores_pending_queue_budget_and_pause(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    monkeypatch.setattr(paths,'ROOT',root)
    monkeypatch.setattr(paths,'episodes_dir',lambda:root/'workspace/episodes')
    monkeypatch.setattr(paths,'state_dir',lambda:root/'workspace/state')
    atomic_json(root/'.studio-project.json', {'version':1})
    atomic_json(paths.episode_json('pending'), {'id':'pending'})
    job=B.enqueue('pending', ['pending'], {'max_images':1})
    budget=ProviderBudget(read(job)['budget_path'],image_requests=1)
    budget.reserve('image','uncertain')
    pause=paths.state_dir()/'production-queue/PAUSE';pause.write_text('operator paused')
    rejection=paths.state_dir()/'audio_rejections/rejected.json'
    atomic_json(rejection,{'decision':'rejected'})
    (root/'.env').write_text('secret excluded')
    snapshot=BK.create_snapshot(['pending'],tmp_path/'backups')['snapshot']
    manifest=BK.verify_snapshot(snapshot)
    for p in (job,budget.path,pause,rejection,root/'.studio-project.json'):
        assert p.relative_to(root).as_posix() in manifest['files']
    assert '.env' not in manifest['files']
    target=tmp_path/'restore'
    BK.restore_snapshot(snapshot,target)
    assert (target/'workspace/state/production-queue/STOP').is_file()
    assert (target/pause.relative_to(root)).read_text()=='operator paused'
    restored=ProviderBudget(target/budget.path.relative_to(root),image_requests=1)
    with pytest.raises(BudgetExceeded): restored.reserve('image','again')


@pytest.mark.parametrize('base,extra',[(0,0),(0,2),(1,1),(2,0)])
def test_effective_image_total_is_identical_in_queue_and_dispatch(tmp_path,base,extra):
    from app.services.queue_records import validate_job
    job=B.enqueue('cap',['episode'],dict(max_images=base,max_image_retries=extra),folder=tmp_path/'queue')
    value=read(job);budget=ProviderBudget(value['budget_path'],image_requests=base,image_retries=extra)
    for i in range(base+extra): budget.reserve('image',str(i))
    validate_job(read(job),job)
    with pytest.raises(BudgetExceeded): budget.reserve('image','over')
    forged=read(budget.path);forged['used']['image_requests']+=1;atomic_json(budget.path,forged)
    with pytest.raises(ValueError,match='budget usage'): validate_job(read(job),job)


def test_changed_qa_policy_rejudges_cached_recognition(monkeypatch,tmp_path):
    from app.services import local_asr,asr_check,audio_content_qa
    audio=tmp_path/'audio.wav';audio.write_bytes(b'isolated-audio')
    evidence_file=tmp_path/'recognition.json';atomic_json(evidence_file,{'transcript':'same'})
    unit=dict(text='same');sha=file_sha256(audio);fp=fingerprint(unit['text']);calls=[]
    prior=dict(status='passed',audio_sha256=sha,expected_sha256=fp,transcript='same',
               decision_sha256='old',recognition_sha256=file_sha256(evidence_file))
    take=dict(audio_path=str(audio),audio_sha256=sha,quality=prior)
    monkeypatch.setattr(local_asr,'recognize',lambda *a,**kw:(dict(text='same',request_id='cached',checker_version='model-v1'),evidence_file))
    def check(*a,**kw):
        calls.append(kw['prepared_transcript']);return dict(prior,status='failed')
    monkeypatch.setattr(asr_check,'check',check)
    monkeypatch.setattr(audio_content_qa,'evaluate',lambda *a,**kw:{'status':'passed_machine','findings':[]})
    result=Q._check_take((unit,take,None,fp))[2]
    assert calls==['same'] and result['status']=='failed'
    assert result['decision_sha256']==Q.decision_identity()
    take['quality']=dict(result,status='passed');calls.clear()
    assert Q._check_take((unit,take,None,fp))[2]['status']=='passed'
    assert calls==[]


@pytest.mark.parametrize('damage',['reverse','outside','episode','task','attempt','pid','budget','offline'])
def test_causal_acceptance_rejects_integral_but_wrong_evidence(evidence,damage):
    from app.services.production_acceptance import verify_scenario
    _, _, validation=evidence
    record=validation['scenario_evidence']['kill_resume']
    events=read(record['events_path']);manifest=read(record['manifest_path'])
    if damage=='reverse': events.reverse()
    if damage=='outside': events[-1]['at']=manifest['finished_at']+1
    if damage=='episode': events[-1]['episode_id']='different'
    if damage=='task': events[-1]['task_id']='d'*64
    if damage=='attempt': events[-1]['attempt_id']='b'*32
    if damage=='pid': events[1]['pid']=456
    if damage=='budget': events[-1]['budget_path']='other-ledger'
    if damage=='offline': manifest['evidence_mode']='offline'
    atomic_json(record['events_path'],events);record['events_sha256']=file_sha256(record['events_path'])
    atomic_json(record['manifest_path'],manifest);record['manifest_sha256']=file_sha256(record['manifest_path'])
    with pytest.raises(ValueError): verify_scenario('kill_resume',record,'current')
