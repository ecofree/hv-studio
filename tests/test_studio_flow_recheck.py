"""No generation: exercise the intended order and publication boundaries."""
import copy
import pytest
from app import paths
from app.services import studio_workflow as W,studio_workspace as S
from app.services.provenance import atomic_json,file_sha256
pytestmark=pytest.mark.production


def story():
    S.create_episode('ordered','夹具','甲入城。\n\n乙出城。',scope='full')
    W.record_review('ordered','editorial','离线核查','仅测试夹具','approved',expected_identity=W.identity('ordered'),checks={'facts_and_story':True})
    return 'ordered'


def test_facts_then_voice_then_visuals_do_not_force_fact_review(tmp_episodes):
    eid=story();v=W.load(eid);v['voice'].update(identity='fixture',model='fixture')
    W.save(eid,v,expected_revision=v['revision'])
    assert W.review_valid(eid,'editorial')
    narration=W.narration_identity(eid)
    W.save_image_brief(eid,'plate','A traveller outside an old city gate',expected_identity=W.identity(eid))
    assert not W.editorial_issues(eid) and W.review_valid(eid,'editorial')
    W.rebind_current(eid,expected_identity=W.identity(eid))
    assert W.narration_identity(eid)==narration
    W.replace_source(eid,'不同的事件。',expected_identity=W.identity(eid))
    from app.managers import load
    from pathlib import Path
    from app.services.script_handoff import verify_revision
    revised=Path(load(eid).script_path)
    assert revised.read_text('utf-8')=='不同的事件。'
    verify_revision(revised.parent)
    assert not W.review_valid(eid,'editorial')
    assert W.narration_identity(eid)!=narration


def test_one_scene_can_cover_multiple_paragraphs_and_sound_survives_rebinding(tmp_episodes):
    eid=story();v=W.load(eid);unit=W.document(eid)['units'][0]['unit_id']
    atomic_json(paths.episode_dir(eid)/'image_prompts.json',{'plate':{'en':'A city gate'}})
    v['binding']=W.binding(eid)
    v['beats']=[dict(id='open',unit_id=unit,text_start=0,purpose='',new_information='',claim_ids=[],image_id='plate',motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5]))]
    v['sound']={'fixture_note':'keep existing design'}
    W.save(eid,v,expected_revision=v['revision']);W.update_scene_bindings(eid)
    rows=W.read(paths.episode_dir(eid)/'scene_plan.json')['unit_scenes']
    assert len(rows)==2 and [r['image'] for r in rows]==['plate','plate']
    W.rebind_current(eid,expected_identity=W.identity(eid))
    assert W.load(eid)['sound']==v['sound']
    W.compile_direction(eid)
    v=W.load(eid);v['beats']=[];W.save(eid,v,expected_revision=v['revision'])
    assert W.compile_direction(eid) is None
    assert not (paths.episode_dir(eid)/'directing.json').exists()
    assert list((W.folder(eid)/'direction-history').glob('*.json'))


def test_old_full_scope_review_is_not_automatically_promoted(tmp_episodes):
    eid=story();p=W.folder(eid)/'review-editorial.json';r=W.read(p)
    r.pop('scope');r.pop('source_sha256');atomic_json(p,r)
    assert W.review_valid(eid,'editorial')
    v=W.load(eid);v['voice']['model']='new';W.save(eid,v,expected_revision=v['revision'])
    assert not W.review_valid(eid,'editorial')


@pytest.mark.parametrize('changed',['renderer','content'])
def test_publish_uses_same_drift_policy_as_candidate_status(tmp_path,monkeypatch,changed):
    from app.services import production_v2 as V,production_identity as I,media_integrity as M,audio_disposition as A
    monkeypatch.setattr(I,'ROOT',tmp_path)
    code=tmp_path/'app/render.py';code.parent.mkdir();code.write_text('before')
    content=tmp_path/'narration.txt';content.write_text('source')
    media=tmp_path/'film.mp4';media.write_bytes(b'not a real film: isolated validation stub')
    manifest=tmp_path/'manifest.json';atomic_json(manifest,{'fixture':True})
    receipt=tmp_path/'receipt.json';atomic_json(receipt,dict(manifest_path=str(manifest),candidate_path=str(media),candidate_sha256=file_sha256(media),inputs={str(code):file_sha256(code),str(content):file_sha256(content)},expected_frames=1,samples=10,sample_rate=10))
    visual=tmp_path/'visual.json';atomic_json(visual,dict(decision='approved',receipt_sha256=file_sha256(receipt),candidate_sha256=file_sha256(media)))
    audio=tmp_path/'audio.json';atomic_json(audio,dict(decision='approved',manifest_sha256=file_sha256(manifest),master_sha256='fixture'))
    monkeypatch.setattr(V,'validate_assembly',lambda _:dict(master_sha256='fixture'))
    monkeypatch.setattr(A,'require_usable',lambda _:None)
    monkeypatch.setattr(M,'verify_media',lambda *a,**k:dict(sha256=file_sha256(media)))
    (code if changed=='renderer' else content).write_text('after')
    target=tmp_path/'published.json'
    if changed=='content':
        with pytest.raises(ValueError,match='input changed'):V.publish(receipt,visual,audio,target)
        assert not target.exists()
    else:
        V.publish(receipt,visual,audio,target)
        assert W.read(target)['renderer_drift']==[str(code)]
