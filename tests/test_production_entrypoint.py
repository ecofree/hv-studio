"""Readiness regressions through the shared entry point, using no provider network."""

import pytest

pytestmark = pytest.mark.production

import io
import json
import wave
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services import narration_pipeline as N
from app.services.image import ImageJob
from app.models import ImageGenRequest, Episode
from app.config.config import get_settings


@pytest.fixture
def production_episode(tmp_path, tmp_episodes, monkeypatch):
    from app.managers import save
    monkeypatch.setattr("app.services.local_asr.preflight", lambda: None)
    ep = Episode(id="entry-audit", title="Audit", episode_number=1)
    save(ep)
    directory=tmp_episodes/ep.id; directory.mkdir(exist_ok=True)
    (directory/'script.md').write_text(
        '> 作品类型：历史解说\n\n**李世民率军出征。**\n\n普通正文。',encoding='utf-8')
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
    # 只有正文进 narration。头部引用块是「给人看的」元数据（`作品类型：…`），
    # 按约定与 script.strip_script_markdown 口径一致地整行排除 —— 见
    # test_quote_looking_like_narration_is_flagged_not_silently_dropped。
    assert [u['source_text'] for u in N.read(r['plan_path'])['units']]==['李世民率军出征。','普通正文。']
    assert episode_path(ep.id).read_bytes()==before
    assert not r['approved']


def test_voice_resolution_uses_registered_asset_without_design(production_episode,monkeypatch):
    from app.services.voice_assets import resolve_voice_asset
    assert resolve_voice_asset('cosyvoice-v3.5-flash')[1]['voice_id']=='cosyvoice-fixture'
    with pytest.raises(ValueError,match='model differs'):resolve_voice_asset('wrong-model')
    asset=production_episode[2]; data=N.read(asset);data['voice_id']='tampered';atomic_json(asset,data)
    with pytest.raises(ValueError,match='version'):resolve_voice_asset('cosyvoice-v3.5-flash')[1]['voice_id']


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
    from app.services import asr_check
    calls=[]
    def provider(req,path):calls.append(req);return offline_provider(req,path)
    monkeypatch.setattr(N,'_provider',provider)
    monkeypatch.setattr(asr_check,'check',lambda audio,text:{'status':'unavailable',
        'audio_sha256':file_sha256(audio),'expected_sha256':fingerprint(text)})
    ep,directory,_=production_episode
    # 2 个旁白单元（头部引用块是元数据，不产生 TTS 调用）→ 恰好 2 次供应商调用。
    # 断言精确次数是本测试的重点：多一次就意味着发生了 fallback 或重复合成。
    expected_units=2
    first=produce(ep.id,stages=['tts'],max_requests=3,max_characters=100)
    assert first['run_ok'] and len(calls)==expected_units and first['technical_quality']=='pending'
    manifest=N.read(directory/'production/v2/run.json')['manifest_path']
    again=produce(ep.id,stages=['tts'])
    assert again['run_ok'] and len(calls)==expected_units
    assert N.read(directory/'production/v2/run.json')['manifest_path']==manifest
    compose=produce(ep.id,stages=['compose'])
    assert compose['status']=='BLOCKED' and 'timeline' in compose['error']


def test_cosyvoice_failure_cannot_call_edge(production_episode,monkeypatch):
    from app.orchestrator import produce
    monkeypatch.setattr(N,'_provider',lambda *a:(_ for _ in ()).throw(RuntimeError('provider failed')))
    r=produce(production_episode[0].id,stages=['tts'],max_requests=3,max_characters=100)
    assert not r['run_ok'] and 'provider failed' in r['error']


def test_changed_external_master_cannot_reuse_old_manifest(production_episode,monkeypatch,tmp_path):
    from app.orchestrator import produce
    from app.services import episode_production as EP, external_narration as EN
    ep,directory,_=production_episode
    assert produce(ep.id,stages=['script'])['run_ok']
    old=tmp_path/'old.wav';new=tmp_path/'new.wav'
    offline_provider({},old);offline_provider({},new)
    # Different immutable WAV bytes are enough to identify a changed recording.
    new.write_bytes(new.read_bytes()+b'new-version')
    state_path=directory/'production/v2/run.json';state=N.read(state_path)
    old_manifest=tmp_path/'old.json';new_manifest=tmp_path/'new.json'
    base={'plan_sha256':file_sha256(state['plan_path']),'technical_quality':'pending',
          'provenance':'external_recording','takes':[],'master_path':str(old),
          'source_recording':{'delivered_sha256':file_sha256(old)}}
    atomic_json(old_manifest,base);state['manifest_path']=str(old_manifest);atomic_json(state_path,state)
    monkeypatch.setattr(N,'validate_assembly',lambda p:N.read(p))
    monkeypatch.setattr(N,'require_usable',lambda m:None)
    monkeypatch.setattr(EP,'check_narration_binding',lambda *a,**k:None)
    monkeypatch.setattr(EP,'resolve_voice_asset',lambda **k:pytest.fail('external audio must not resolve another voice'))
    calls=[]
    def build(plan,master,destination,**kwargs):
        calls.append((str(master),kwargs['cache_dir']))
        atomic_json(new_manifest,dict(base,master_path=str(new),source_recording={'delivered_sha256':file_sha256(new)}))
        return new_manifest
    monkeypatch.setattr(EN,'build',build)
    result=produce(ep.id,stages=['tts'],external_master=str(new))
    assert result['run_ok'] and N.read(state_path)['manifest_path']==str(new_manifest.resolve())
    assert calls[0][0]==str(new) and calls[0][1].name==file_sha256(new)


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
    # Title-card authoring now requires the real quality timing report.
    r=produce(ep.id,stages=['quality','compose','verify'],timeline=str(timeline))
    assert r['run_ok'] and r['status']=='CANDIDATE' and not r['approved'], r
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


@pytest.mark.parametrize('stage', ['images', 'tts', 'assets'])
def test_retired_paid_legacy_stages_cannot_be_called_directly(stage):
    from app.orchestrator import produce_legacy
    with pytest.raises(ValueError, match='retired'):
        produce_legacy('retired-fixture', stages=[stage])


def test_failed_new_brief_cannot_reuse_same_key_old_plate(production_episode, monkeypatch):
    from app.services import episode_production as E, image as I
    ep, directory, _ = production_episode
    root = directory/'production/v2'
    source = '将士整顿队伍。'
    brief = dict(source_sha256=fingerprint(source), images={
        'unit-000': {'en': 'Soldiers regroup beside a camp at dawn.'}})
    atomic_json(root/'source_image_briefs.json', brief)
    def generated(jobs, **kwargs):
        for job in jobs: job.install(png())
        return {j.id:str(j.out) for j in jobs}
    monkeypatch.setattr(I, 'generate_many', generated)
    first = E.run(ep.id, stages=['images'], script_md=source, max_images=1)
    assert first['run_ok'], first
    old = N.read(root/'run.json')['image_assets']['unit-000']
    brief['images']['unit-000']['en'] = 'Soldiers cross a bridge at sunset.'
    atomic_json(root/'source_image_briefs.json', brief)
    monkeypatch.setattr(I, 'generate_many', lambda jobs, **kw: {j.id:None for j in jobs})
    second = E.run(ep.id, stages=['images'], script_md=source, max_images=1)
    assert not second['run_ok'] and second['image_assets_incomplete'] == ['unit-000']
    assert N.read(root/'run.json')['image_assets'] == {}
    assert Path(old).is_file()


def test_raw_narration_never_dispatches_a_new_image(production_episode, monkeypatch):
    from app.services import episode_production as E
    monkeypatch.setattr('app.services.image.generate_many', lambda *a, **kw: pytest.fail('raw narration dispatched'))
    result = E.run(production_episode[0].id, stages=['images'], max_images=3)
    assert not result['run_ok'] and 'missing visual briefs' in result['error']


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
