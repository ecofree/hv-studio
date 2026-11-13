"""Product regressions: simple creation, immutable source, explicit approvals."""
import pytest
from app import paths
from app.services import studio_workflow as W, studio_workspace as S
from app.services.provenance import atomic_json
pytestmark=pytest.mark.production


def test_candidate_requires_current_key_fact_review_but_not_a_pilot(tmp_episodes):
    S.create_episode('simple','测试','甲入城。',scope='full')
    assert not W.editorial_issues('simple')
    assert not W.status('simple')['ready_for_generation']
    assert not W.status('simple')['editorial_approved']
    with pytest.raises(ValueError,match='当前正文的关键事件'):
        W.compile_direction('simple')
    W.record_review('simple','editorial','测试人员','已核对核心事件（仅夹具）','approved',expected_identity=W.identity('simple'),checks={'facts_and_story':True})
    assert W.status('simple')['ready_for_generation']
    assert not W.review_valid('simple','pilot')


def test_note_edit_preserves_review_but_audio_replacement_does_not(tmp_episodes):
    S.create_episode('notes','测试','甲入城。')
    audio=tmp_episodes/'baseline.wav';audio.write_bytes(b'identity fixture')
    v=W.load('notes');v['voice']['baseline']=str(audio);W.save('notes',v,expected_revision=v['revision'])
    W.record_review('notes','editorial','测试人员','仅夹具','approved',expected_identity=W.identity('notes'),checks={'facts_and_story':True})
    original=W.identity('notes');v=W.load('notes');v['brief']['audience']='普通观众'
    W.save('notes',v,expected_revision=v['revision'])
    assert W.identity('notes')==original and W.review_valid('notes','editorial')
    voice=W.narration_identity('notes')
    audio.write_bytes(b'changed voice')
    assert W.review_valid('notes','editorial')
    assert W.narration_identity('notes')!=voice


def test_pilot_keeps_shared_source_without_fabricating_quote(tmp_episodes):
    S.create_episode('parent','测试','甲入城。\n\n乙出城。',scope='full')
    units=W.document('parent')['units'];v=W.load('parent')
    c=dict(id='event',unit_id=units[0]['unit_id'],quote=units[0]['source_text'],type='史籍记载',source='夹具',locator='卷一',excerpt='夹具',uncertainty='',visual_limits='')
    v['claims']=[c]
    v['beats']=[dict(id='second',unit_id=units[1]['unit_id'],text_start=0,purpose='',new_information='',claim_ids=['event'],image_id='plate',motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5]))]
    W.save('parent',v,expected_revision=v['revision'])
    S.create_pilot('parent','child',1,2)
    child=W.load('child'); copied=child['claims'][0]
    assert copied['quote']==c['quote'] and copied['context_source']=='parent'
    assert child['beats'][0]['claim_ids']==['event'] and not W.editorial_issues('child')
    W.rebind_current('child',expected_identity=W.identity('child'))
    assert W.load('child')['claims'][0]['quote']==c['quote']


def test_frozen_snapshot_corruption_still_blocks(tmp_episodes):
    S.create_episode('snapshot','测试','甲入城。')
    from app.services import studio_sources as Sources
    # Existing revision verifier must still be called for source-bound versions.
    atomic_json(paths.episode_dir('snapshot')/'editorial/source-binding.json',dict(revision=str(tmp_episodes/'missing'),manifest_sha256='x',inputs=[]))
    assert Sources.issues('snapshot')


def test_final_confirmation_refuses_stale_receipt_before_review(tmp_episodes,monkeypatch):
    from app.services import studio_completion as C
    root=paths.episode_dir('finish')/'production/v2';root.mkdir(parents=True)
    receipt=root/'candidate.json';atomic_json(receipt,{'fixture':True})
    atomic_json(root/'run.json',{'receipt_path':str(receipt)})
    with pytest.raises(ValueError,match='变化'):C.confirm('finish','old','tester','fixture')
    assert not (root/'published.json').exists()


def test_voice_design_survives_notes_with_selected_master(tmp_episodes):
    S.create_episode('voice-notes','测试','甲入城。')
    audio=tmp_episodes/'master.wav';audio.write_bytes(b'fixture')
    v=W.load('voice-notes');v['voice']['master']=str(audio);W.save('voice-notes',v,expected_revision=v['revision'])
    original=W.voice_design_identity('voice-notes')
    v=W.load('voice-notes');v['brief']['question']='修改备注'
    W.save('voice-notes',v,expected_revision=v['revision'])
    assert W.voice_design_identity('voice-notes')==original


@pytest.mark.parametrize('pending',[False,True])
def test_final_confirmation_preserves_explicit_reviews_and_pending_gate(tmp_episodes,monkeypatch,pending):
    from app.services import studio_completion as C, production_status as PS, narration_pipeline as N,production_v2 as V
    from app.services.provenance import file_sha256
    root=paths.episode_dir('finish')/'production/v2';root.mkdir(parents=True)
    receipt=root/'candidate.json';atomic_json(receipt,{'fixture':True})
    atomic_json(root/'run.json',{'receipt_path':str(receipt),'manifest_path':'fixture-manifest'})
    monkeypatch.setattr(PS,'get_status',lambda _:dict(candidate_path='fixture-video',quality_pending_units=['unit'] if pending else []))
    calls=[]
    def listen(*args):calls.append(('audio',args));return 'audio-review'
    def visual(*args):calls.append(('video',args));return 'video-review'
    def publish(*args):calls.append(('publish',args));return args[-1]
    monkeypatch.setattr(N,'record_listening_review',listen);monkeypatch.setattr(V,'review_candidate',visual);monkeypatch.setattr(V,'publish',publish)
    if pending:
        with pytest.raises(ValueError,match='具体问题'):C.confirm('finish',file_sha256(receipt),'tester','实际完整看审')
        assert not calls
    else:
        C.confirm('finish',file_sha256(receipt),'tester','实际完整看审')
        assert [c[0] for c in calls]==['audio','video','publish']
        assert calls[-1][1][1:3]==('video-review','audio-review')


def test_explicit_recording_reaches_local_import_after_source_confirmation(tmp_episodes,monkeypatch):
    from app.services import studio_jobs as J, episode_production as P, external_narration as EN, local_asr
    root=tmp_episodes.parent
    episodes=root/'workspace/episodes';episodes.mkdir(parents=True)
    monkeypatch.setattr(paths,'ROOT',root)
    monkeypatch.setattr(paths,'episodes_dir',lambda:episodes)
    atomic_json(root/'.studio-project.json',{'original_root':str(root/'original')})
    S.create_episode('recording','用户定稿','这是需要对应录音的正文。')
    master=episodes/'recording/master.wav';master.write_bytes(b'offline recording identity')
    W.record_review('recording','editorial','offline fixture','仅确认测试夹具事实',
                    'approved',expected_identity=W.identity('recording'),checks={'facts_and_story':True})
    spawned=[]
    monkeypatch.setattr(J,'_spawn',lambda *a:spawned.append(a))
    job=J.launch('recording',mode='voice',external_master=str(master))
    req=W.read(job/'request.json');J.validate_request(req)
    assert spawned and req['external_master']==str(master.resolve())
    assert req['max_requests']==0
    assert W.status('recording')['ready_for_generation']
    assert W.status('recording')['editorial_approved']
    assert not W.status('recording')['pilot_approved']
    assert W.review_valid('recording','editorial')
    calls=[]
    def local_import(plan,audio,*args,**kwargs):
        calls.append(str(audio))
        raise ValueError('fixture local audio mismatch')
    monkeypatch.setattr(local_asr,'preflight',lambda:None)
    monkeypatch.setattr(EN,'build',local_import)
    result=P.run('recording',stages=['tts'],external_master=str(master))
    assert calls==[str(master)]
    assert result['status']=='BLOCKED' and 'fixture local audio mismatch' in result['error']
    with pytest.raises(ValueError,match='合成新旁白'):
        W.require_production('recording',['tts'])
    v=W.load('recording');v['voice']['master']=str(master)
    W.save('recording',v,expected_revision=v['revision'])
    other=master.with_name('other.wav');other.write_bytes(b'other recording')
    with pytest.raises(ValueError,match='已选母带不同'):
        W.require_voice_binding('recording',other)
    master.write_bytes(b'changed since submission')
    with pytest.raises(ValueError):J.validate_request(req)
