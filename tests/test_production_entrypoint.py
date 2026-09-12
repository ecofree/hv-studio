"""Readiness regressions through the shared entry point, using no provider network."""
import io
import json
import wave
from pathlib import Path
from unittest.mock import patch
import pytest
from PIL import Image
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services import narration_pipeline as N
from app.services.image import ImageJob
from app.models import ImageGenRequest, Episode
from app.config.config import get_settings


@pytest.fixture
def production_episode(tmp_path, tmp_episodes, monkeypatch):
    from app.managers import save
    ep = Episode(id="entry-audit", title="Audit", episode_number=1)
    save(ep)
    directory=tmp_episodes/ep.id; directory.mkdir(exist_ok=True)
    (directory/'script.md').write_text('**李世民率军出征。**\n\n> 兵贵神速。\n\n普通正文。',encoding='utf-8')
    cache=tmp_path/'voice-cache.json'
    atomic_json(cache,{'voice_id':'cosyvoice-fixture','model':'cosyvoice-v3.5-flash'})
    asset=tmp_path/'voice.json'; N.import_voice(cache,asset)
    config=get_settings().get_profile().voice
    monkeypatch.setattr(config,'voice_asset',str(asset))
    monkeypatch.setattr(config,'cosyvoice_model','cosyvoice-v3.5-flash')
    return ep, directory, asset


def test_public_prepare_preserves_source_without_migrating_old_frames(production_episode):
    from app.orchestrator import produce
    from app.managers.episode import episode_path
    ep,directory,_=production_episode
    before=episode_path(ep.id).read_bytes()
    r=produce(ep.id,stages=['script'])
    assert r['run_ok'] and r['pipeline_version']=='production_v2'
    assert [u['source_text'] for u in N.read(r['plan_path'])['units']]==['李世民率军出征。','兵贵神速。','普通正文。']
    assert episode_path(ep.id).read_bytes()==before
    assert not r['approved']


def test_voice_resolution_uses_registered_asset_without_design(production_episode,monkeypatch):
    from app.services import cosyvoice
    monkeypatch.setattr(cosyvoice,'design_voice',lambda **kw:pytest.fail('must not redesign voice'))
    assert cosyvoice.get_voice_id('cosyvoice-v3.5-flash')=='cosyvoice-fixture'
    with pytest.raises(ValueError,match='model differs'):cosyvoice.get_voice_id('wrong-model')
    asset=production_episode[2]; data=N.read(asset);data['voice_id']='tampered';atomic_json(asset,data)
    with pytest.raises(ValueError,match='version'):cosyvoice.get_voice_id('cosyvoice-v3.5-flash')


def test_public_default_budget_blocks_before_provider(production_episode,monkeypatch):
    from app.orchestrator import produce
    monkeypatch.setattr(N,'_provider',lambda *a:pytest.fail('must not call provider without budget'))
    r=produce(production_episode[0].id)
    assert r['status']=='BLOCKED' and not r['run_ok']
    assert r['stages_done']==['script'] and 'budget' in r['error']


def offline_provider(request,raw):
    with wave.open(str(raw),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes(b'\0\0'*24000)
    return {'status':'succeeded','audio_sha256':file_sha256(raw)}


def test_v2_tts_reuses_exact_master_and_never_falls_back(production_episode,monkeypatch):
    from app.orchestrator import produce
    from app.services import asr_check,voice
    calls=[]
    def provider(req,path):calls.append(req);return offline_provider(req,path)
    monkeypatch.setattr(N,'_provider',provider)
    monkeypatch.setattr(asr_check,'check',lambda audio,text:{'status':'unavailable',
        'audio_sha256':file_sha256(audio),'expected_sha256':fingerprint(text)})
    monkeypatch.setattr(voice,'_one_edge',lambda *a:pytest.fail('no voice fallback'))
    ep,directory,_=production_episode
    first=produce(ep.id,stages=['tts'],max_requests=3,max_characters=100)
    assert first['run_ok'] and len(calls)==3 and first['technical_quality']=='pending'
    manifest=N.read(directory/'production/v2/run.json')['manifest_path']
    again=produce(ep.id,stages=['tts'])
    assert again['run_ok'] and len(calls)==3
    assert N.read(directory/'production/v2/run.json')['manifest_path']==manifest
    compose=produce(ep.id,stages=['compose'])
    assert compose['status']=='BLOCKED' and 'timeline' in compose['error']


def test_cosyvoice_failure_cannot_call_edge(production_episode,monkeypatch):
    from app.orchestrator import produce
    from app.services import voice
    monkeypatch.setattr(N,'_provider',lambda *a:(_ for _ in ()).throw(RuntimeError('provider failed')))
    monkeypatch.setattr(voice,'_one_edge',lambda *a:pytest.fail('unexpected Edge call'))
    r=produce(production_episode[0].id,stages=['tts'],max_requests=3,max_characters=100)
    assert not r['run_ok'] and 'provider failed' in r['error']


@pytest.mark.NEEDS_FFMPEG
def test_public_entry_renders_and_verifies_without_touching_legacy(production_episode,monkeypatch):
    from app.orchestrator import produce
    from app.services import asr_check
    ep,directory,_=production_episode
    monkeypatch.setattr(N,'_provider',offline_provider)
    monkeypatch.setattr(asr_check,'check',lambda audio,text:{'status':'passed',
        'audio_sha256':file_sha256(audio),'expected_sha256':fingerprint(text)})
    assert produce(ep.id,stages=['tts'],max_requests=3,max_characters=100)['run_ok']
    state_path=directory/'production/v2/run.json'
    m=N.read(N.read(state_path)['manifest_path'])
    image=directory/'image.png';image.write_bytes(png())
    timeline=directory/'timeline.json'
    atomic_json(timeline,{'shots':[{'id':'s1','sample_start':0,'sample_end':m['samples'],'image_path':str(image)}],
        'subtitles':[dict(unit_id=u['unit_id'],sample_start=u['sample_start'],sample_end=u['sample_end'],
                          text_start=0,text_end=len(u['text']),text=u['text']) for u in m['alignment']]})
    r=produce(ep.id,stages=['compose','verify'],timeline=str(timeline))
    assert r['run_ok'] and r['status']=='CANDIDATE' and not r['approved']
    before=state_path.read_bytes()
    assert produce(ep.id,stages=['verify'])['run_ok']
    assert state_path.read_bytes()==before
    Path(r['final_mp4']).unlink()
    r=produce(ep.id,stages=['verify'])
    assert not r['run_ok']


def png(size=(1080,1920),color='red'):
    b=io.BytesIO();Image.new('RGB',size,color).save(b,format='PNG');return b.getvalue()


def test_image_identity_dimensions_and_bad_response_preserve_previous(tmp_path):
    request=ImageGenRequest(model='gpt-image-2',prompt='first')
    job=ImageJob('a',request,tmp_path/'a.png')
    job.out.write_bytes(png((2,2)))
    assert not job.is_done()
    with pytest.raises(ValueError,match='dimensions'):job.install(png((2,2)))
    job.install(png());before=job.out.read_bytes();receipt=job.receipt_path.read_bytes()
    assert job.is_done()
    request.prompt='changed'
    assert not job.is_done()
    with pytest.raises(Exception):job.install(b'corrupt response')
    assert job.out.read_bytes()==before and job.receipt_path.read_bytes()==receipt
    job.install(png(color='blue'));assert job.is_done() and job.out.read_bytes()!=before
    assert list((tmp_path/'.candidates').rglob('previous.png'))


def test_image_reference_content_invalidates_cache(tmp_path):
    ref=tmp_path/'ref.png';ref.write_bytes(png())
    job=ImageJob('a',ImageGenRequest(model='gpt-image-2',prompt='ref'),tmp_path/'a.png',(str(ref),))
    job.install(png());assert job.is_done()
    ref.write_bytes(png(color='blue'));assert not job.is_done()


def test_v2_images_preserve_historical_assets(production_episode,monkeypatch):
    from app.orchestrator import produce
    from app.services import image as image_service
    ep,directory,_=production_episode
    old=directory/'old.png';old.write_bytes(png(color='blue'));before=old.read_bytes()
    job=ImageJob('one',ImageGenRequest(model='gpt-image-2',prompt='new scene'),old)
    scene=directory/'scene.json';atomic_json(scene, {})
    monkeypatch.setattr('app.services.production_inputs.prepare',lambda *a,**kw:([job],scene))
    def generate(jobs, **kwargs):
        for j in jobs:j.install(png())
        return {j.id:str(j.out) for j in jobs}
    monkeypatch.setattr(image_service,'generate_many',generate)
    r=produce(ep.id,stages=['images'],max_images=1)
    assert r['run_ok'] and old.read_bytes()==before
    assert job.out!=old and job.is_done()


def test_batch_resume_rechecks_output_config_and_input(tmp_path,tmp_episodes,monkeypatch):
    from app.services import hv_ops
    monkeypatch.setattr(hv_ops.paths,'state_dir',lambda:tmp_path)
    episode=tmp_episodes/'batch.json';episode.write_text('{}')
    resultfile=tmp_path/'film.mp4';calls=[]
    def produce(eid,stages=None):
        calls.append(eid);resultfile.write_bytes(b'current candidate')
        return {'status':'CANDIDATE','run_ok':True,'final_mp4':str(resultfile)}
    monkeypatch.setattr('app.orchestrator.produce',produce)
    def batch():return hv_ops.run_batch(['batch'],batch_id='test',resume=True)
    assert batch()['done']==['batch'];assert batch()['done']==['batch'];assert len(calls)==1
    resultfile.unlink();batch();assert len(calls)==2
    monkeypatch.setattr(get_settings().get_profile().voice,'cosyvoice_model','changed-model')
    batch();assert len(calls)==3
    episode.write_text('{"changed":true}');batch();assert len(calls)==4
    resultfile.write_bytes(b'changed video');batch();assert len(calls)==5


def test_batch_rejects_partial_success_for_full_run(tmp_path,monkeypatch):
    from app.services import hv_ops
    monkeypatch.setattr(hv_ops.paths,'state_dir',lambda:tmp_path)
    monkeypatch.setattr('app.orchestrator.produce',lambda *a,**k:{'status':'SCRIPTED','run_ok':True})
    r=hv_ops.run_batch(['a'],batch_id='partial')
    assert not r['done'] and r['failed']


def test_api_uses_v2_and_returns_blocked_status(production_episode):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.controllers.v1.auth import require_token
    app.dependency_overrides[require_token]=lambda:None
    try:
        with TestClient(app) as client:
            r=client.post('/v1/episode/entry-audit/produce',params={'stage':'script'})
            assert r.status_code==200 and r.json()['pipeline_version']=='production_v2'
            r=client.post('/v1/episode/entry-audit/produce')
            assert r.status_code==409 and r.json()['status']=='BLOCKED'
    finally:app.dependency_overrides.pop(require_token,None)
