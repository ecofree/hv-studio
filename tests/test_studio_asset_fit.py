"""Offline fixtures only: source fitting, paid response reuse, lossless excerpts."""
import base64
import io
from pathlib import Path
import wave
import pytest
from PIL import Image
from app import paths
from app.models import ImageGenRequest
from app.services import studio_workflow as W, studio_workspace as S
from app.services import studio_audio_excerpt as E
from app.services.image import ImageJob, recover_saved_response, source_fit
from app.services.provenance import atomic_json, file_sha256

pytestmark=pytest.mark.production


def test_recover_quarantined_portrait_offline_and_fit_without_stretch(tmp_path):
    data=io.BytesIO();Image.new('RGB',(1024,1536),(45,90,70)).save(data,format='PNG')
    job=ImageJob('plate',ImageGenRequest(model='gpt-image-2',prompt='test',size='1080x1920'),tmp_path/'output/plate.png')
    receipt=tmp_path/'_quarantine/response.json'
    atomic_json(receipt,dict(request_fingerprint=job.request_fingerprint(),channel='offline-fixture',
        response=dict(created=1,data=[dict(b64_json=base64.b64encode(data.getvalue()).decode())])))
    before=file_sha256(receipt)
    assert recover_saved_response(job,receipt)==job.out
    assert job.is_done() and file_sha256(receipt)==before
    assert job.out.read_bytes()==data.getvalue()
    fit=source_fit((1024,1536));assert fit['scale']==1.25
    assert fit['retained_area']==pytest.approx(.84375)
    from app.services.motion import frame_box
    x0,y0,x1,y1=frame_box(1024,1536,1,.5,.5,'fit_cover_v2')
    assert (x1-x0,y1-y0)==(864,1536)
    assert 0<=x0<x1<=1024 and 0<=y0<y1<=1536
    with Image.open(job.out) as im:
        assert im.crop((x0,y0,x1,y1)).resize((1080,1920),Image.Resampling.LANCZOS).size==(1080,1920)
    bad=ImageJob('other',ImageGenRequest(model='gpt-image-2',prompt='changed',size='1080x1920'),tmp_path/'other.png')
    with pytest.raises(RuntimeError,match='another request'):recover_saved_response(bad,receipt)


def test_bad_image_still_rejected(tmp_path):
    job=ImageJob('bad',ImageGenRequest(model='gpt-image-2',prompt='test'),tmp_path/'bad.png')
    data=io.BytesIO();Image.new('RGB',(12,12)).save(data,format='PNG')
    with pytest.raises(ValueError,match='too small'):job.install(data.getvalue())
    assert not job.is_done()
    with pytest.raises(OSError):job.install(b'not an image')


def fixture_audio(tmp_episodes):
    S.create_episode('parent','fixture','第一句。\n\n第二句。',scope='full')
    master=tmp_episodes/'parent/full.wav'
    with wave.open(str(master),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(1000)
        out.writeframes(b'\x01\x00'*1000+b'\x02\x00'*1000+b'\x03\x00'*1000)
    value=W.load('parent');value['voice'].update(identity='Charon',model='fixture',master=str(master))
    W.save('parent',value,expected_revision=value['revision'])
    S.create_pilot('parent','child',0,1)
    return master


def test_excerpt_preserves_master_voice_and_has_own_source_binding(tmp_episodes,monkeypatch):
    root=tmp_episodes.parent
    episodes=root/'workspace/episodes';episodes.mkdir(parents=True)
    monkeypatch.setattr(paths,'ROOT',root)
    monkeypatch.setattr(paths,'episodes_dir',lambda:episodes)
    master=fixture_audio(episodes);before=file_sha256(master)
    with pytest.raises(ValueError,match='整集母带'):E.reject_uncut_parent('child',master)
    out=E.create('child',1,2,expected_identity=W.identity('child'))
    assert file_sha256(master)==before
    assert E._pcm(out)[1]==b'\x02\x00'*1000
    assert E.selected_source('child')==master.resolve()
    assert E.validate('child',out)['listening_review']=='pending'
    assert W.load('child')['voice']['identity']=='Charon'
    assert not W.review_valid('child','pilot')
    # Exercise the studio entry gate with a simulated workspace root.
    monkeypatch.setattr(S,'config',lambda _:dict(original_root=str(root/'original')))
    W.require_voice_binding('child',out)
    from app.services import studio_jobs as J
    W.record_review('child','editorial','fixture','offline test only','approved',expected_identity=W.identity('child'),checks={'facts_and_story':True})
    submitted=[]
    monkeypatch.setattr(J,'_spawn',lambda root,request:submitted.append(request))
    J.launch('child',mode='voice',external_master=str(out),provider_attempts_per_image=1)
    assert len(submitted)==1 and submitted[0]['max_requests']==0
    assert submitted[0]['external_master']==str(out.resolve())
    assert submitted[0]['max_image_retries']==0
    assert submitted[0]['provider_attempts_per_image']==1
    out.write_bytes(out.read_bytes()+b'tampered')
    with pytest.raises(ValueError,match='内容已改变'):E.validate('child',out)


def test_excerpt_rejects_invalid_ranges_and_stale_source(tmp_episodes,monkeypatch):
    monkeypatch.setattr(paths,'ROOT',tmp_episodes.parent)
    master=fixture_audio(tmp_episodes)
    for lo,hi in [(0,0),(-1,1),(0,4),(0,float('nan'))]:
        with pytest.raises(ValueError):E.create('child',lo,hi,expected_identity=W.identity('child'))
    out=E.create('child',0,1,expected_identity=W.identity('child'))
    out=E.create('child',2,3,expected_identity=W.identity('child'))
    assert E._pcm(out)[1]==b'\x03\x00'*1000
    # Simulate a source file changed outside the UI; bound drafts are not editable there.
    from app.managers import load
    from pathlib import Path
    Path(load('child').script_path).write_text('新正文。',encoding='utf-8')
    with pytest.raises(ValueError,match='正文或声音方案'):E.validate('child',out)


def test_external_voice_worker_only_requests_audio_stages(tmp_episodes,tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from app.services import studio_jobs as J,episode_production as P
    spec=importlib.util.spec_from_file_location('excerpt_worker',Path(__file__).resolve().parents[1]/'bin/studio_job.py')
    worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    monkeypatch.setattr(J,'validate_request',lambda _:None)
    monkeypatch.setattr(W,'identity',lambda _: 'fixture')
    captured={}
    master=tmp_path/'chosen.wav';master.write_bytes(b'synthetic routing fixture')
    manifest=tmp_path/'manifest.json';atomic_json(manifest,dict(master_path=str(master)))
    atomic_json(paths.episode_dir('child')/'production/v2/run.json',dict(manifest_path=str(manifest)))
    def run(**kwargs):
        captured.update(kwargs)
        return dict(run_ok=True,status='STAGES_COMPLETE',approved=False,production_ready=False)
    monkeypatch.setattr(P,'run',run)
    job=tmp_path/'job/request.json';job.parent.mkdir()
    worker.execute(job,dict(ep_id='child',expected_identity='fixture',job_mode='voice',external_master=str(master),max_requests=0,max_characters=0,max_images=0))
    assert captured['stages']==['tts','quality','alignment']
    assert captured['max_requests']==captured['max_characters']==captured['max_images']==0
    assert captured['external_master']==str(master)
    result=W.read(job.parent/'result.json')
    assert result['voice_reused_without_generation'] and not result['approved']


def test_preproduction_coverage_groups_aliases_by_effective_plate(tmp_episodes,tmp_path):
    from app.services.studio_image_reuse import coverage_report
    S.create_episode('exposure','实测底图时长','第一段。\n\n第二段。')
    eid='exposure';ep=paths.episode_dir(eid);sha=W.document(eid)['source_sha256']
    a,b=[u['unit_id'] for u in W.document(eid)['units']]
    manifest=tmp_path/'timing.json';atomic_json(manifest,dict(sample_rate=10,samples=310,alignment=[
        dict(unit_id=a,sample_start=0,sample_end=115),dict(unit_id=b,sample_start=120,sample_end=305)]))
    left=str(tmp_path/'left.png');right=str(tmp_path/'right.png')
    atomic_json(ep/'scene_plan.json',dict(source_sha256=sha,unit_scenes=[dict(unit_id=a,image='left'),dict(unit_id=b,image='right')]))
    Path(left).write_bytes(b'fixture plate')
    atomic_json(ep/'production/v2/run.json',dict(source_sha256=sha,manifest_path=str(manifest),image_assets=dict(left=left,right=right)))
    atomic_json(W.folder(eid)/'image-reuse.json',dict(source_sha256=sha,images=dict(right=dict(path=left,sha256=file_sha256(left)))))
    assert coverage_report(eid)['spans']==[dict(image='left',start_seconds=0.,duration_seconds=31.,units=2,timing='estimate')]


def test_reopen_import_uses_same_delivered_recording_only(tmp_episodes,tmp_path):
    from app.services.studio_jobs import imported_master
    eid='reopen';S.create_episode(eid,'苏裴','已经录好的正文。')
    sha=W.document(eid)['source_sha256'];root=paths.episode_dir(eid)/'production/v2'
    original=tmp_path/'delivery.wav';original.write_bytes(b'original PCM')
    plan=tmp_path/'plan.json';atomic_json(plan,dict(source_sha256=sha))
    manifest=tmp_path/'manifest.json';atomic_json(manifest,dict(provenance='external_recording',
        plan_path=str(plan),plan_sha256=file_sha256(plan),source_recording=dict(delivered_path=str(original),delivered_sha256=file_sha256(original))))
    state=dict(source_sha256=sha,manifest_path=str(manifest));atomic_json(root/'run.json',state)
    assert imported_master(eid)==str(original.resolve())
    original.write_bytes(b'another take')
    with pytest.raises(ValueError,match='已改变'):imported_master(eid)
    atomic_json(root/'run.json',dict(state,source_sha256='old source'))
    assert imported_master(eid) is None


def test_adopt_staged_source_preserves_episode_audio_and_all_original_bytes(tmp_episodes,monkeypatch):
    from app.services import studio_workspace as workspace
    monkeypatch.setattr(workspace,'library_path',lambda p:Path(p))
    monkeypatch.setattr(workspace,'config',lambda root=None:None)
    import json
    from app.models import Episode
    from app.services import studio_sources as sources
    from app.services.provenance import atomic_json, file_sha256
    eid='staged-fifth';root=paths.episode_dir(eid);ed=root/'editorial';ed.mkdir(parents=True)
    script=ed/'narration.md';source='### 【第一幕：离线接管测试】\n\n正文逐字保留。\n'
    script.write_text(source,encoding='utf8')
    audio=ed/'voice.wav';audio.write_bytes(b'original delivery bytes')
    untouched=ed/'notes.txt';untouched.write_bytes(b'untouched')
    original=Episode(id=eid,title='第五集',episode_number=5,script_path=str(script))
    row=original.model_dump(mode='json');row.update(status='audio_ready',audio_path=str(audio),tts_voice='Charon')
    atomic_json(paths.episode_json(eid),row);before={str(p):file_sha256(p) for p in (script,audio,untouched)}
    revision=sources.import_authoring(eid,'第五集',script,adopt_existing=True)
    saved=json.loads(paths.episode_json(eid).read_text('utf8'))
    assert saved['audio_path']==str(audio) and saved['status']=='scripted'
    assert saved['script_path']==str(revision/'narration.md')
    assert all(file_sha256(k)==v for k,v in before.items()) and untouched.read_bytes()==b'untouched'
    from app.services import studio_workflow as W
    from app.services.studio_jobs import imported_master
    assert sources.disposition(eid)['status']=='current' and not W.review_valid(eid,'editorial')
    assert imported_master(eid)==str(audio.resolve())
    import_record=json.loads((root/'studio/import.json').read_text('utf8'))
    assert import_record['legacy_status']=='audio_ready' and import_record['studio_status']=='scripted'


def test_saved_and_revised_scene_keep_source_and_legacy_missing_binding_is_checked(tmp_episodes):
    from app.services.studio_image_reuse import coverage_report
    eid='scene-edit';S.create_episode(eid,'分镜改稿','开场。\n\n结尾。',scope='full')
    root=paths.episode_dir(eid);doc=W.document(eid);ids=[u['unit_id'] for u in doc['units']]
    scene=root/'scene_plan.json'
    atomic_json(scene,dict(unit_scenes=[dict(unit_id=u,image='room') for u in ids]))
    atomic_json(root/'image_prompts.json',{'room':{'en':'Tang court'}})
    W.update_scene_bindings(eid)
    assert W.read(scene)['source_sha256']==doc['source_sha256']
    script=Path(W.read(paths.episode_json(eid))['script_path'])
    script.write_text('开场。\n\n新的结尾。',encoding='utf-8')
    assert coverage_report(eid)['status']=='invalid'
    W.rebind_current(eid,expected_identity=W.identity(eid));W.update_scene_bindings(eid)
    new=W.document(eid)
    saved=W.read(scene);assert saved['source_sha256']==new['source_sha256']
    assert [r['unit_id'] for r in saved['unit_scenes']]==[u['unit_id'] for u in new['units']]
    saved.pop('source_sha256');atomic_json(scene,saved)
    assert coverage_report(eid)['status']=='waiting'
    saved['unit_scenes'][1]['unit_id']=ids[1];atomic_json(scene,saved)
    assert coverage_report(eid)['status']=='invalid'
