"""User-facing edits stay drafts; navigation never grants approval or spends."""
from pathlib import Path
import pytest
from app.services import studio_workspace as S,studio_workflow as W
pytestmark=pytest.mark.production


def test_registered_full_episode_is_visible_without_launching_a_job(tmp_episodes,tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app import paths
    from app.services import production_status as P,studio_jobs as J
    from app.services.provenance import atomic_json
    eid='registered-ui-fixture'
    S.create_episode(eid,'整集登记夹具','### 【第一幕：测试】\n\n保留用户原文。',scope='full')
    media=tmp_path/'fixture.mp4';media.write_bytes(b'UI-only fixture; no encoding')
    atomic_json(paths.episode_dir(eid)/'production/v2/run.json',{'receipt_path':'fixture'})
    status={'candidate_path':str(media),'full_episode_registration':{'approved':False},'blockers':[]}
    def browse_status(episode_id, *, display):
        assert episode_id == eid and display
        return status
    monkeypatch.setattr(P,'get_status',browse_status)
    monkeypatch.setattr(J,'jobs',lambda _:[])
    monkeypatch.setattr(J,'launch',lambda *a,**k:pytest.fail('page navigation must never generate'))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=30).run()
    app.switch_page('app_pages/production.py').run()
    assert not app.exception and len(app.get('video'))==1
    assert any('整集已登记' in x.value for x in app.info)
    assert not W.review_valid(eid,'pilot')
    status.update(candidate_path=None,blockers=['fixture evidence changed'])
    app.run()
    assert not app.exception and not app.get('video') and app.warning

def test_five_pages_render_and_brief_edit_does_not_approve(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    eid='ui-studio-fixture'
    S.create_episode(eid,'离线界面夹具','### 【第一幕：测试】\n\n这是离线验证用的正文。',scope='pilot')
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=30).run()
    assert not app.exception
    next(w for w in app.text_input if w.label=='给谁看').set_value('测试读者')
    next(b for b in app.button if b.label=='保存创作卡').click().run()
    assert not app.exception and W.load(eid)['brief']['audience']=='测试读者'
    assert not W.review_valid(eid,'editorial')
    for page in ('story','pilot','production','feedback'):
        app.switch_page(f'app_pages/{page}.py').run()
        assert not app.exception, (page,[e.message for e in app.exception])

def test_narration_request_reuses_selected_voice_and_preserves_editorial_directions(tmp_episodes,tmp_path):
    import importlib.util
    script=Path(__file__).resolve().parents[1]/'bin/studio_narrate.py'
    spec=importlib.util.spec_from_file_location('studio_narrate_fixture',script)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    eid='directed-voice-fixture';S.create_episode(eid,'离线标注','### 【第一幕：测试】\n\n第一段正文。\n\n第二段正文。',scope='pilot')
    v=W.load(eid);u=W.document(eid)['units'];v['voice']['model']='Qwen3-TTS-12Hz-1.7B-CustomVoice';v['voice']['identity']='dylan'
    v['beats']=[dict(id='b1',unit_id=u[0]['unit_id'],text_start=0,purpose='测试',new_information='测试',claim_ids=[],image_id='plate1',
                    performance='只重读第一段',motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5]))]
    W.save(eid,v,expected_revision=v['revision'])
    from test_series_production_flow import recipe
    from app.services import series_voice as SV
    scheme,_=recipe(tmp_path);SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    _,parts=mod.requests(eid)
    assert '\n\n'.join(p['text'] for p in parts)=='\n\n'.join(x['source_text'] for x in u)
    assert all(p['instruction']==SV.load_scheme(scheme)['recipe']['instruction'] for p in parts)
    assert W.load(eid)['beats'][0]['performance']=='只重读第一段'


@pytest.mark.NEEDS_FFMPEG
def test_local_narration_keeps_raw_audio_and_returns_clock_preserving_derivative(tmp_episodes,tmp_path,monkeypatch):
    import importlib.util,json,numpy as np
    from app.services import narration_leveling as L
    from app.services.narration_master import verify_repair
    script=Path(__file__).resolve().parents[1]/'bin/studio_narrate.py'
    spec=importlib.util.spec_from_file_location('studio_render_fixture',script)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    eid='local-render-fixture';S.create_episode(eid,'离线配音','### 【第一幕：测试】\n\n这是一次离线配音检查。',scope='pilot')
    v=W.load(eid);v['voice'].update(identity='dylan',model='Qwen3-TTS-12Hz-1.7B-CustomVoice')
    W.save(eid,v,expected_revision=v['revision'])
    raw=tmp_path/'fixture.wav';sr=24000
    L.save(raw,np.rint(2600*np.sin(2*np.pi*220*np.arange(sr*5)/sr)).astype('<i2'),sr)
    calls=[]
    def provider(*a,**k):calls.append(a[0]);return raw.read_bytes()
    from test_series_production_flow import recipe
    from app.services import series_voice as SV
    scheme,_=recipe(tmp_path);SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    class FakeProvider:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def validate_request(self,r):
            from app.services.studio_voice_contract import validate
            validate(r)
        def __call__(self,*a,**k):return provider(*a,**k)
    monkeypatch.setattr('app.services.locked_voice_provider.LockedVoiceProvider',FakeProvider)
    # This fixture checks clock/leveling, not a real model's transcript of a tone.
    from app.services import local_asr
    from app.services.provenance import atomic_json,file_sha256,fingerprint
    def recognize_fixture(p):
        r=dict(audio_sha256=file_sha256(p),text='这是一次离线配音检查。',segments=[])
        receipt=tmp_path/'asr-fixture.json'
        atomic_json(receipt,dict(request=dict(audio_sha256=file_sha256(p)),result=r,result_sha256=fingerprint(r)))
        return r,receipt
    monkeypatch.setattr(local_asr,'recognize',recognize_fixture)
    result=mod.render(eid,tmp_path/'budget.json',1,100)
    receipt=verify_repair(result.with_suffix('.level.json'))
    source_map=json.loads((result.parent/'source-map.json').read_text('utf-8'))
    assert calls==['这是一次离线配音检查。']
    assert abs(receipt['samples']-sr*5/1.2)<sr*.02 and receipt['exact_sample_clock']
    assert Path(source_map['raw_master']).is_file() and not source_map['approved']
    assert source_map['parts'][0]['text']==calls[0]
