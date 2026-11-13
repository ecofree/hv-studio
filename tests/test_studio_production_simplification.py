"""Regression checks for the simplified production workflow.
All external services, model work and encoding are disabled. Fixtures are temporary.
"""
import io
import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
import pytest
from PIL import Image
from test_production_entrypoint import production_episode
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services import narration_pipeline as N
from app.services import image as I
from app.models import ImageGenRequest

pytestmark=pytest.mark.production


def jobs_for(ep, directory, count=2):
    return [I.ImageJob('plate-'+str(i),ImageGenRequest(model='gpt-image-2',prompt='fixture '+str(i)),directory/(str(i)+'.png')) for i in range(count)]


def pixels():
    stream=io.BytesIO();Image.new('RGB',(512,768),'blue').save(stream,format='PNG');return stream.getvalue()


def test_partial_images_are_registered_before_batch_failure(production_episode,monkeypatch):
    from app.services import episode_production as E,production_inputs as P
    ep,directory,_=production_episode;jobs=jobs_for(ep,directory)
    scene=directory/'test-scene.json'
    from app.services.studio_workflow import document
    units=document(ep.id)['units']
    atomic_json(scene,dict(unit_scenes=[dict(unit_id=u['unit_id'],image=jobs[i%2].id) for i,u in enumerate(units)]))
    monkeypatch.setattr(P,'prepare',lambda *a:(jobs,scene))
    def generate(items,**kwargs):
        items[0].install(pixels())
        return {items[0].id:str(items[0].out),items[1].id:None,items[1].id+'__error':'simulated 503'}
    monkeypatch.setattr(I,'generate_many',generate)
    result=E.run(ep.id,stages=['images'],max_images=2)
    state=N.read(directory/'production/v2/run.json')
    assert jobs[0].is_done() and jobs[0].id in state['image_assets']
    assert result['status']=='BLOCKED' and 'budget' in result


def test_invalid_external_audio_stops_before_images(production_episode,monkeypatch,tmp_path):
    from app.services import episode_production as E,production_inputs as P,external_narration as EN
    ep,directory,_=production_episode;jobs=jobs_for(ep,directory,1)
    scene=directory/'test-scene.json';atomic_json(scene,{})
    monkeypatch.setattr(P,'prepare',lambda *a:(jobs,scene))
    events=[]
    def generate(items,**kwargs):
        events.append('image_dispatch');items[0].install(pixels());return {items[0].id:str(items[0].out)}
    monkeypatch.setattr(I,'generate_many',generate)
    def bad_audio(*a,**kw):
        events.append('audio_rejected');raise ValueError('simulated audio/script mismatch')
    monkeypatch.setattr(EN,'build',bad_audio)
    master=tmp_path/'recording.wav';master.write_bytes(b'fixture never decoded')
    result=E.run(ep.id,stages=['tts','images'],max_images=1,external_master=str(master))
    assert events==['audio_rejected'] and result['status']=='BLOCKED'


def test_unused_prompt_is_excluded_from_required_jobs(production_episode,monkeypatch):
    from app.services import episode_production as E,production_inputs as P
    import app.orchestrator as O
    ep,directory,_=production_episode
    plan=E.run(ep.id,stages=['script'])['plan_path'];units=N.read(plan)['units']
    atomic_json(directory/'scene_plan.json',dict(unit_scenes=[dict(unit_id=u['unit_id'],image='plate-0') for u in units]))
    atomic_json(directory/'image_prompts.json',{'plate-0':{'en':'used'},'plate-1':{'en':'unused old idea'}})
    jobs=jobs_for(ep,directory)
    monkeypatch.setattr(O,'build_image_jobs',lambda *a,**k:jobs)
    prepared,_=P.prepare(ep,plan,directory/'production/v2')
    assert [j.id for j in prepared]==['plate-0']


def test_cached_image_work_does_not_start_asr(production_episode,monkeypatch):
    from app.services import episode_production as E,production_inputs as P,local_asr
    from app.services import compose
    ep,directory,_=production_episode;jobs=jobs_for(ep,directory,1)
    root=directory/'production/v2'
    for job in jobs:
        job.out_path=root/'images'/job.request_fingerprint()/job.out.name;job.install(pixels())
    scene=directory/'test-scene.json';atomic_json(scene,{})
    monkeypatch.setattr(P,'prepare',lambda *a:(jobs,scene))
    monkeypatch.setattr(compose,'check_font_available',lambda:None)
    def fail():raise ValueError('ASR model unavailable fixture')
    monkeypatch.setattr(local_asr,'preflight',fail)
    monkeypatch.setattr(I,'generate_many',lambda items,**k:{j.id:str(j.out) for j in items})
    result=E.run(ep.id,stages=['images','compose'])
    assert 'ASR model unavailable' not in result.get('error','')


@pytest.mark.parametrize('failure',['policy','timeout'])
def test_policy_and_uncertain_results_are_not_dispatched_again(tmp_path,monkeypatch,failure):
    from app.services.provider_budget import ProviderBudget
    budget=ProviderBudget(tmp_path/'ledger.json',image_requests=20)
    monkeypatch.setattr(I,'_throttle',lambda:None);monkeypatch.setattr(I,'_backoff',lambda _:0)
    channels=[SimpleNamespace(name=name,supports=lambda _:True) for name in ['first','second']]
    job=jobs_for(None,tmp_path,1)[0];calls=[]
    def generate(req,channel):
        token=budget.reserve('image',job.request_fingerprint());calls.append(channel.name);budget.finish(token,False)
        if failure=='policy':raise I.ImageError('HTTP 400 content policy rejected')
        raise I.TransientImageError('ReadTimeout outcome unknown')
    queue=Queue();queue.put(job);result={}
    I._generate_many_worker(SimpleNamespace(generate=generate),queue,result,channels,False,budget=budget)
    assert len(calls)==1 and result[job.id] is None
    queue.put(job)
    I._generate_many_worker(SimpleNamespace(generate=generate),queue,result,channels,False,budget=budget)
    assert len(calls)==1


def test_all_image_attempts_share_one_durable_limit(tmp_path):
    from app.services.provider_budget import ProviderBudget,BudgetExceeded
    budget=ProviderBudget(tmp_path/'ledger.json',image_requests=1,image_retries=2)
    token=budget.reserve('image','first');budget.finish(token,False)
    budget.check_capacity(image_requests=1)
    budget.reserve('image','brand-new-second');budget.reserve('image','brand-new-third')
    assert budget.snapshot()['used']['image_requests']==3


@pytest.mark.parametrize('changed',['manifest','quality'])
def test_stale_audio_review_invalidates_content_approval(production_episode,monkeypatch,changed):
    from app.services import production_status as S,delivery as D
    from app.orchestrator import _load_script_md
    ep,directory,_=production_episode;root=directory/'production/v2'
    candidate=root/'candidate.mp4';candidate.parent.mkdir(parents=True);candidate.write_bytes(b'not encoded; status fixture')
    manifest=root/'manifest.json';atomic_json(manifest,dict(takes=[],delivery_dispositions=[],revision=1))
    old_sha=file_sha256(manifest)
    receipt=root/'receipt.json';atomic_json(receipt,dict(candidate_path=str(candidate),candidate_sha256=file_sha256(candidate),inputs={str(manifest.resolve()):old_sha},manifest_path=str(manifest)))
    visual=root/'visual.json';atomic_json(visual,dict(decision='approved',receipt_sha256=file_sha256(receipt)))
    audio=root/'audio.json';atomic_json(audio,dict(decision='approved',manifest_sha256=old_sha))
    atomic_json(root/'published.json',dict(approved=True,receipt=str(receipt),sha256=file_sha256(candidate),review=str(visual),listening_review=str(audio)))
    state=dict(source_sha256=fingerprint(_load_script_md(ep)),manifest_path=str(manifest),receipt_path=str(receipt),last_result={})
    if changed=='manifest':
        atomic_json(manifest,dict(takes=[],delivery_dispositions=[],revision=2))
    else:
        from app.services import production_quality as Q
        state['quality_path']=str(root/'quality.json')
        # Simulate the legitimate result of a newly recorded spoken-error finding.
        monkeypatch.setattr(Q,'pending',lambda _:['reported-unit'])
    atomic_json(root/'run.json',state)
    monkeypatch.setattr(D,'pending',lambda _:[])
    status=S.get_status(ep.id)
    assert status['approved'] is False and status['content_approved'] is False
    if changed=='quality':assert status['candidate_path']


def test_alignment_without_evidence_keeps_weak_units_pending(tmp_path):
    from app.services.production_status import alignment_review_pending
    manifest=tmp_path/'manifest.json';atomic_json(manifest,dict(alignment_review=['weak-unit']))
    pending=alignment_review_pending(dict(manifest_path=str(manifest)))
    assert pending==['weak-unit']


def test_budget_counts_distinct_dispatched_requests(production_episode,monkeypatch):
    from app.services import episode_production as E,production_inputs as P
    ep,directory,_=production_episode;jobs=jobs_for(ep,directory)
    jobs[1].req=jobs[0].req.model_copy()
    assert jobs[0].request_fingerprint()==jobs[1].request_fingerprint()
    scene=directory/'test-scene.json';atomic_json(scene,{})
    monkeypatch.setattr(P,'prepare',lambda *a:(jobs,scene))
    monkeypatch.setattr(I,'generate_many',lambda *a,**k:{})
    result=E.run(ep.id,stages=['images'],max_images=1)
    assert '2 jobs need generation' not in result.get('error','')


def test_explicit_reuse_needs_no_new_images(production_episode):
    from app.services import studio_image_reuse as R,studio_workflow as W
    ep,directory,_=production_episode
    image=directory/'kept.png';image.write_bytes(pixels());before=file_sha256(image)
    state_path=directory/'production/v2/run.json'
    atomic_json(state_path,dict(image_assets={'plate-0':str(image)},declared_image_keys=['plate-0','plate-1'],receipt_path='previous-receipt',timeline_path='previous-timeline'))
    R.assign(ep.id,['plate-1'],'plate-0',expected_identity=W.identity(ep.id))
    jobs=R.apply(ep.id,jobs_for(ep,directory))
    assert jobs[1].imported and jobs[1].is_done() and jobs[1].out==image
    assert file_sha256(image)==before and 'receipt_path' not in N.read(state_path)
    assert list((directory/'production/v2/run-history').glob('*.json'))


def test_503_cools_down_channel_without_retrying_batch(tmp_path,monkeypatch):
    from app.services.provider_budget import ProviderBudget
    b=ProviderBudget(tmp_path/'budget.json',image_requests=20)
    monkeypatch.setattr(I,'_throttle',lambda:None)
    calls=[]
    def generate(req,channel):
        calls.append(channel.name);raise I.TransientImageError('HTTP 503 no available channel',http_status=503)
    gen=SimpleNamespace(generate=generate);q=Queue()
    for j in jobs_for(None,tmp_path):q.put(j)
    result={}
    I._generate_many_worker(gen,q,result,[SimpleNamespace(name='down',supports=lambda _:True)],False,budget=b)
    assert calls==['down']


def test_prepare_recovers_quarantine_before_budget(production_episode):
    import base64
    from app.services import episode_production as E,production_inputs as P
    ep,directory,_=production_episode
    plan=E.run(ep.id,stages=['script'])['plan_path'];root=directory/'production/v2'
    jobs,_=P.prepare(ep,plan,root);job=jobs[0]
    receipt=root/'images/_quarantine'/job.request_fingerprint()/(job.out.name+'.response.json')
    atomic_json(receipt,dict(request_fingerprint=job.request_fingerprint(),channel='fixture',response=dict(data=[dict(b64_json=base64.b64encode(pixels()).decode())])))
    before=file_sha256(receipt)
    recovered,_=P.prepare(ep,plan,root)
    assert recovered[0].is_done() and file_sha256(receipt)==before


def test_invalid_existing_title_stops_before_image_dispatch(production_episode,monkeypatch):
    from app.services import episode_production as production,production_inputs as inputs
    ep,directory,_=production_episode
    scene=directory/'scene.json'
    atomic_json(scene,{})
    jobs=jobs_for(ep,directory,1)
    monkeypatch.setattr(inputs,'prepare',lambda *a:(jobs,scene))
    atomic_json(directory/'overlays.json',dict(version=1,overlays=[]))
    monkeypatch.setattr(I,'generate_many',lambda *a,**kw:pytest.fail('paid dispatch before title validation'))
    result=production.run(ep.id,stages=['images'],max_images=1)
    assert result['status']=='BLOCKED' and '片头标题' in result['error']
    assert result['budget']['used']['image_requests']==0
