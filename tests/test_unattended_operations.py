"""Current audit failure scenarios, through public boundaries and real processes."""
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.services.provenance import atomic_json
from app.services.narration_pipeline import read
from app.services.provider_budget import ProviderBudget, BudgetExceeded
from app.services import image as images, batch_worker as worker
from app.models import ImageGenResponse, ImageGenItem
from test_image_routing import generator, job, response

pytestmark = pytest.mark.production


def test_api_budget_and_path_guards_precede_any_dispatch(generator, monkeypatch, tmp_path, tmp_episodes):
    from app import paths
    from app.main import app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(paths,'state_dir',lambda:tmp_path/'state')
    calls=[]
    monkeypatch.setattr(generator,'_post_unlocked',lambda *a:calls.append(1) or response())
    with TestClient(app) as client:
        assert client.post('/v1/image/test',json={}).status_code==422
        assert client.post('/v1/image/test',json={'budget_id':'explicit-zero'}).status_code==409
        assert not calls
        for eid in ('..%5Caudit-escape','C%3Aescape','CON','%252e%252e%255cx'):
            assert client.post(f'/v1/episode/{eid}/init').status_code in (404,422)
        assert not list(tmp_episodes.glob('*.json'))
        for _ in range(2):
            result=client.post('/v1/image/test',json={'budget_id':'one','max_images':1})
            assert result.status_code==200, result.text
        assert calls==[1]
    with pytest.raises(BudgetExceeded): generator.generate(job(tmp_path,'unbudgeted').req)


def test_paid_response_survives_download_failure_without_channel_or_new_budget(generator, monkeypatch, tmp_path):
    calls=[]; downloads=[]
    monkeypatch.setattr(generator,'_post_unlocked',lambda *a:calls.append(1) or
                        ImageGenResponse(data=[ImageGenItem(url='https://offline.invalid/image.png')]))
    def unavailable(*a):
        downloads.append(1)
        raise images.TransientImageError('download timeout')
    monkeypatch.setattr(images,'_download',unavailable)
    monkeypatch.setattr(images.time,'sleep',lambda _:None)
    budget=ProviderBudget(tmp_path/'budget.json',image_requests=1)
    task=job(tmp_path,'resume')
    assert images.generate_many([task],budget=budget,progress=False)[task.id] is None
    assert len(calls)==1 and len(downloads)==3
    monkeypatch.setattr(images,'get_image',lambda:pytest.fail('recovery must not require provider configuration'))
    monkeypatch.setattr(images,'_download',lambda *a:base64.b64decode(response().data[0].b64_json))
    assert images.generate_many([job(tmp_path,'resume')],progress=False)[task.id]
    assert task.is_done() and budget.snapshot()['used']['image_requests']==1


def test_episode_paths_reject_aliases_and_escape_junctions(tmp_episodes,tmp_path):
    from app.paths import episode_dir, episode_json, InvalidEpisodeId
    from app.managers.episode import checkpoint_path
    for eid in ('../x',r'..\x','C:x',r'\\host\share','x.','x ','nul','LPT1','%2e%2e'):
        for call in (episode_dir,episode_json):
            with pytest.raises(InvalidEpisodeId): call(eid)
    with pytest.raises(InvalidEpisodeId): checkpoint_path('valid','../label')
    assert episode_dir('李世民-01').parent==tmp_episodes.resolve()
    external=tmp_path/'external';external.mkdir()
    link=tmp_episodes/'linked'
    if sys.platform=='win32':
        quoted_link="'"+str(link).replace("'","''")+"'"
        quoted_target="'"+str(external).replace("'","''")+"'"
        run=subprocess.run(['powershell','-NoProfile','-Command',
                            f'New-Item -ItemType Junction -Path {quoted_link} -Target {quoted_target} -ErrorAction Stop | Out-Null'],
                           capture_output=True)
        assert run.returncode==0,run.stderr.decode(errors='replace')[:200]
    else: link.symlink_to(external,target_is_directory=True)
    try:
        with pytest.raises(InvalidEpisodeId): episode_dir('linked')
    finally:
        link.rmdir() if sys.platform=='win32' else link.unlink()


def test_supplier_lane_is_shared_by_separate_processes(tmp_path):
    script=tmp_path/'lane.py'
    script.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app.managers import relay
from app.config.config import Channel
from app.services.provenance import atomic_json
folder=Path(sys.argv[2]);relay.STATE_DIR=folder;relay.HEALTH_PATH=folder/'health.json'
manager=relay.RelayManager([Channel(name='same-supplier',api_key='offline',base_url='https://offline.invalid',model='gpt-image-2')])
intervals=[]
for i in range(12):
    with manager.request_slot('same-supplier'):
        start=time.time();time.sleep(.005);end=time.time()
        intervals.append(dict(start=start,end=end))
atomic_json(folder/(sys.argv[3]+'.json'),intervals)
''',encoding='utf-8')
    from app.paths import ROOT
    children=[subprocess.Popen([sys.executable,str(script),str(ROOT),str(tmp_path),str(i)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE) for i in range(6)]
    for child in children:
        _,err=child.communicate(timeout=20)
        assert child.returncode==0,err.decode(errors='replace')[:1500]
    intervals=sorted([r for i in range(6) for r in read(tmp_path/f'{i}.json')],key=lambda v:v['start'])
    assert len(intervals)==72
    assert all(b['start']>=a['end'] for a,b in zip(intervals,intervals[1:]))


def test_queue_is_durable_isolates_timeout_and_reuses_budget(tmp_path,monkeypatch,tmp_episodes):
    from app.services import production_admission
    monkeypatch.setattr(production_admission,'check',lambda eid:dict(eligible=True,blockers=[],input_fingerprint='input'))
    folder=tmp_path/'queue'
    path=worker.enqueue('bounded',['slow','next'],dict(max_images=1),timeout=1,retries=0,folder=folder)
    budget_path=read(path)['budget_path']
    budget=ProviderBudget(budget_path,image_requests=1)
    budget.reserve('image','already-consumed')
    def command(path,eid,token):
        if eid=='slow': return [sys.executable,'-c','import time;time.sleep(20)']
        target=folder/'results'/(token+'.json')
        return [sys.executable,'-c',
                'import sys,json,pathlib; p=pathlib.Path(sys.argv[1]);p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({"status":"NEEDS_REVIEW","candidate_path":"offline-candidate"}))',str(target)]
    worker.run_once(folder,command_factory=command)
    state=read(path)
    assert state['episodes']['slow']['status']=='RETRY_WAIT'
    assert state['episodes']['next']['status']=='NEEDS_REVIEW'
    assert not worker.alive(state['episodes']['slow']['child_pid'],state['episodes']['slow']['child_created'])
    assert read(budget_path)['used']['image_requests']==1
    assert worker.enqueue('bounded',['slow','next'],dict(max_images=1),timeout=1,retries=0,folder=folder)==path
    with pytest.raises(ValueError,match='another specification'):
        worker.enqueue('bounded',['slow','next'],dict(max_images=2),timeout=1,retries=0,folder=folder)


def test_input_review_blocks_generation_and_pending_status_survives_staleness(tmp_path,monkeypatch,tmp_episodes):
    from app.services import production_admission, production_status
    from app import paths
    folder=tmp_path/'queue'
    path=worker.enqueue('review',['draft'],{},folder=folder)
    worker.run_once(folder,command_factory=lambda *a:pytest.fail('draft must not dispatch'))
    assert read(path)['episodes']['draft']['status']=='NEEDS_REVIEW'
    root=paths.episode_dir('draft')/'production/v2'
    atomic_json(root/'run.json',dict(source_sha256='old',quality_path='quality',manifest_path='manifest',last_result={}))
    monkeypatch.setattr(production_status,'_quality_pending',lambda p:['audio-1'])
    monkeypatch.setattr(production_status,'_delivery_pending',lambda p:[{'cue_id':'cue-1'}])
    result=production_status.get_status('draft')
    assert result['status']=='STALE'
    assert result['quality_pending_units']==['audio-1'] and result['delivery_pending']==[{'cue_id':'cue-1'}]
    assert result['quality_report_path']=='quality' and result['manifest_path']=='manifest'


def test_invalid_prompt_is_an_input_blocker_not_a_worker_crash(monkeypatch):
    from app.services import production_admission
    monkeypatch.setattr(production_admission,'inputs',lambda _:(_ for _ in ()).throw(RuntimeError('prompt prescreen rejected input')))
    result=production_admission.check('bad-prompt')
    assert result['eligible'] is False
    assert 'prescreen' in result['blockers'][0]


def test_disk_uncertainty_and_insufficient_space_block_before_work(monkeypatch,tmp_path):
    import shutil
    from app.orchestrator import _check_disk_space
    monkeypatch.setattr(shutil,'disk_usage',lambda _:SimpleNamespace(free=900*1024**2))
    with pytest.raises(RuntimeError): _check_disk_space(tmp_path,need_mb=2500)
    monkeypatch.setattr(shutil,'disk_usage',lambda _:(_ for _ in ()).throw(OSError('unreadable')))
    with pytest.raises(RuntimeError,match='cannot verify'): _check_disk_space(tmp_path)


def test_killed_worker_recovers_owned_child_and_preserves_consumed_budget(tmp_path,monkeypatch,tmp_episodes):
    from app.services import production_admission
    from app.paths import ROOT
    monkeypatch.setattr(production_admission,'check',lambda _:dict(eligible=True,blockers=[],input_fingerprint='stable'))
    folder=tmp_path/'recover-queue'
    path=worker.enqueue('recover',['one','two'],{'max_images':1},timeout=30,retries=1,folder=folder)
    budget_path=read(path)['budget_path']
    harness=tmp_path/'interrupted.py'
    harness.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app.services import batch_worker as worker,production_admission
from app.services.provider_budget import ProviderBudget
from app.services.narration_pipeline import read
production_admission.check=lambda _:dict(eligible=True,blockers=[],input_fingerprint='stable')
folder=Path(sys.argv[2]);job=read(folder/'recover.json')
ProviderBudget(job['budget_path'],image_requests=1).reserve('image','response-already-consumed')
worker.run_once(folder,command_factory=lambda *a:[sys.executable,'-c','import time;time.sleep(25)'])
''',encoding='utf-8')
    # Kill the actual interpreter, not the Windows venv launcher, so the owned
    # child is deliberately left for the next worker to reconcile.
    errors=tmp_path/'worker-stderr.log'
    error_stream=errors.open('wb')
    parent=subprocess.Popen([sys.executable,str(harness),str(ROOT),str(folder)],stdout=subprocess.DEVNULL,stderr=error_stream)
    row=None
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            row=read(path)['episodes']['one']
            if row.get('child_pid') or parent.poll() is not None: break
            time.sleep(.05)
        assert row and row.get('child_pid'),errors.read_text(errors='replace')[-1600:]
        import psutil
        psutil.Process(row['owner_pid']).kill()
        parent.communicate(timeout=10)
        def complete(spec,eid,token):
            target=folder/'results'/(token+'.json')
            return [sys.executable,'-c',
                    'import sys,json,pathlib;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(exist_ok=True);p.write_text(json.dumps({"status":"NEEDS_REVIEW","candidate_path":"offline"}))',str(target)]
        worker.run_once(folder,command_factory=complete)
        result=read(path)
        assert all(r['status']=='NEEDS_REVIEW' for r in result['episodes'].values())
        assert result['episodes']['one']['attempts']==2
        assert not worker.alive(row['child_pid'],row['child_created'])
        assert read(budget_path)['used']['image_requests']==1
    finally:
        if row and row.get('owner_pid'): worker.stop_tree(row['owner_pid'],row.get('owner_created'))
        if parent.poll() is None: parent.kill();parent.communicate(timeout=10)
        if row and row.get('child_pid'): worker.stop_tree(row['child_pid'],row['child_created'])
        error_stream.close()
