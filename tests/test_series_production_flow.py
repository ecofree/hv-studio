"""Offline regressions for series reuse, source-bound handoff and UI recovery."""
import importlib.util
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest

from app import paths
from app.services import series_voice as SV, studio_workflow as W, studio_workspace as S
from app.services import studio_narration_handoff as H, studio_voice_contract as C
from app.services.provenance import atomic_json, file_sha256, fingerprint

pytestmark=pytest.mark.production
ROOT=Path(__file__).resolve().parents[1]


def recipe(tmp_path):
    # Recorded approval here belongs only to a synthetic, isolated test fixture.
    c=dict(version=1,speaker='dylan',model='Qwen3-TTS-12Hz-1.7B-CustomVoice',mode='custom',
           instruction='同一位旁白自然讲故事，不变声，不喊叫。',engine_revision='fixture-engine',
           source_sha256='old-fixture-source',generation={'temperature':.65},seed=42,
           target_rate=1.2,max_chunk_characters=347,selected_variant='fixture-C',model_path='fixture-model',request_paths=[])
    evidence={
        'confirmation_path':dict(decision='accepted_for_use',selected_variant='fixture-C',source_sha256=c['source_sha256'],target_rate=1.2),
        'pilot_receipt_path':dict(request={k:c[k] for k in ('speaker','model','mode','instruction','engine_revision')}),
        'generation_plan_path':{k:c[k] for k in ('generation','seed','source_sha256')},
        'engine_manifest_path':dict(model_files={},runtime={}), 'worker_path':dict(fixture=True)}
    c['evidence']={}
    for key,value in evidence.items():
        p=tmp_path/(key+'.json');atomic_json(p,value);c[key]=str(p);c['evidence'][str(p)]=file_sha256(p)
    p=tmp_path/'old-contract.json';atomic_json(p,c)
    return SV.publish('离线测试声音',p),p


def episode(eid='new-story',text='玄奘走出城门。\n\n他继续向西。'):
    S.create_episode(eid,'测试故事','### 【第一幕：出发】\n\n'+text,scope='pilot')
    return eid


def narrate_module():
    spec=importlib.util.spec_from_file_location('series_narrate_fixture',ROOT/'bin/studio_narrate.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod


def test_series_reuse_does_not_copy_old_text_or_approvals(tmp_path,tmp_episodes):
    scheme,old=recipe(tmp_path);before=file_sha256(old)
    for eid,text in [('story-a','他不肯回头。'),('story-b','她偏要试试。')]:
        episode(eid,text);identity=W.identity(eid)
        SV.prepare_episode(eid,scheme,expected_identity=identity)
        c=C.load(C.path_for(eid));_,parts=narrate_module().requests(eid)
        assert ''.join(p['text'] for p in parts)==text
        assert c['instruction']==SV.load_scheme(scheme)['recipe']['instruction']
        assert c['source_sha256']!='old-fixture-source'
        assert not W.review_valid(eid,'editorial') and not W.review_valid(eid,'pilot')
        assert W.load(eid)['voice']['master']==''
        assert not list((W.folder(eid)/'narration').glob('*'))
    assert file_sha256(old)==before
    assert SV.publish('离线测试声音',old)==scheme
    # Published worker and confirmation evidence are independent of the old run folder.
    origin=SV.read(scheme)['origin_path'];old_record=SV.read(old)
    Path(old_record['worker_path']).write_text('changed old fixture',encoding='utf-8')
    SV.load_scheme(scheme)
    assert SV.read(origin)['worker_path']!=old_record['worker_path']


def test_exact_split_handles_long_units_whitespace_and_punctuation():
    text=('长句不能漏字也不能重复，'*40)+'\n末尾  空格。'
    doc={'units':[{'unit_id':'u','source_text':text}]}
    parts=SV.split_units(doc,347);SV.validate_parts(parts,doc)
    assert ''.join(p['text'] for p in parts)==text and max(map(lambda p:len(p['text']),parts))<=347
    parts[1]['spans'][0]['text_start']-=1
    with pytest.raises(ValueError,match='漏字'):SV.validate_parts(parts,doc)


def test_short_paragraphs_share_continuous_chunks_but_acts_stay_separate():
    from app.services.script_document import parse_document
    doc=parse_document('### 【第一幕：出发】\n\n第一段。\n\n第二段。\n\n### 【第二幕：转折】\n\n第三段。')
    parts=SV.split_units(doc,347);SV.validate_parts(parts,doc)
    assert [p['text'] for p in parts]==['第一段。\n\n第二段。','第三段。']


def test_new_script_and_changed_voice_cannot_reuse_binding(tmp_path,tmp_episodes):
    scheme,_=recipe(tmp_path);eid=episode()
    SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    p=C.path_for(eid);value=SV.read(p);value['parts'][0]['text']='擅自改稿';atomic_json(p,value)
    with pytest.raises(ValueError,match='原文'):C.load(p)
    value['parts']=SV.split_units(W.document(eid),347);atomic_json(p,value)
    from app.managers import load
    script=Path(load(eid).script_path);script.write_text(script.read_text('utf-8')+'\n另一个结尾。',encoding='utf-8')
    with pytest.raises(ValueError,match='文案'):C.load(p)


def test_existing_voice_never_silently_replaced(tmp_path,tmp_episodes):
    scheme,_=recipe(tmp_path);eid=episode()
    v=W.load(eid);v['voice']['identity']='another-person';W.save(eid,v,expected_revision=v['revision'])
    before=W.identity(eid)
    with pytest.raises(ValueError,match='静默替换'):SV.prepare_episode(eid,scheme,expected_identity=before)
    assert W.identity(eid)==before and not C.path_for(eid).exists()


def test_pilot_inherits_recipe_but_rebinds_its_own_words(tmp_path,tmp_episodes):
    scheme,_=recipe(tmp_path);eid=episode()
    SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    S.create_pilot(eid,'new-pilot',1,2)
    c=C.load(C.path_for('new-pilot'))
    assert [p['text'] for p in c['parts']]==['他继续向西。']
    assert c['scheme_sha256']==file_sha256(scheme)
    assert not W.review_valid('new-pilot','pilot') and not W.review_valid('new-pilot','editorial')


def test_wrong_words_are_refused_before_budget_is_consumed(tmp_path,tmp_episodes):
    from app.services import studio_voice as V
    from app.services.provider_budget import ProviderBudget
    scheme,_=recipe(tmp_path);eid=episode()
    SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    c=C.load(C.path_for(eid))
    req=V.request(c['parts'][0]['text'],**{k:c[k] for k in ('speaker','model','mode','instruction','engine_revision')})
    req=C.bind(req,C.path_for(eid));req['text']='混入另一集的文案。'
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    with pytest.raises(ValueError,match='contiguous passage'):
        V.render_take(req,tmp_path/'takes',lambda *a,**k:pytest.fail('wrong script dispatched'),budget=budget)
    assert budget.snapshot()['used']['tts_requests']==0


def test_imported_rules_and_source_are_both_bound(tmp_path,tmp_episodes,monkeypatch):
    from app.services import studio_sources as Sources
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    original=tmp_path/'original';original.mkdir()
    atomic_json(tmp_path/'.studio-project.json',dict(original_root=str(original)))
    for filename,text in [('author.md','### 【第一幕：出发】\n\n【语气：松弛】\n玄奘走出城门。'),('rules.md','只用用户原稿。')]:
        (tmp_path/filename).write_text(text,encoding='utf-8');(original/filename).write_text(text,encoding='utf-8')
    Sources.import_authoring('imported-new','新原稿',tmp_path/'author.md',rule_files=[tmp_path/'rules.md'])
    scheme,_=recipe(tmp_path)
    SV.prepare_episode('imported-new',scheme,expected_identity=W.identity('imported-new'))
    assert [p['text'] for p in C.load(C.path_for('imported-new'))['parts']]==['玄奘走出城门。']
    assert not W.review_valid('imported-new','editorial')
    (original/'rules.md').write_text('更新后的规则。',encoding='utf-8')
    assert Sources.external_updates('imported-new')
    C.load(C.path_for('imported-new'))


@pytest.fixture
def generated_fixture(tmp_path,tmp_episodes,monkeypatch):
    scheme,_=recipe(tmp_path);eid=episode(text='玄奘走出城门。')
    SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid))
    calls=[];heard={}
    class FakeProvider:
        def __init__(self,*args):pass
        def validate_request(self,req):self.req=req;C.validate(req)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def __call__(self,text,**kwargs):
            calls.append(text)
            pcm=(2500*np.sin(2*np.pi*220*np.arange(24000*3)/24000)).astype('<i2')
            buf=io.BytesIO()
            with wave.open(buf,'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(pcm.tobytes())
            import hashlib
            data=buf.getvalue();heard[hashlib.sha256(data).hexdigest()]=text;return data
    def recognize(p):
        sha=file_sha256(p);r=dict(text=heard[sha],audio_sha256=sha,segments=[])
        path=tmp_path/(sha+'.json');atomic_json(path,dict(request=dict(audio_sha256=sha),result=r,result_sha256=fingerprint(r)))
        return r,path
    monkeypatch.setattr('app.services.locked_voice_provider.LockedVoiceProvider',FakeProvider)
    monkeypatch.setattr('app.services.local_asr.recognize',recognize)
    audio=narrate_module().render(eid,tmp_path/'budget.json',1,100)
    return eid,audio,calls


@pytest.mark.NEEDS_FFMPEG
def test_audio_selection_is_metadata_only_and_rejects_changed_input(generated_fixture,monkeypatch):
    eid,audio,calls=generated_fixture
    before=audio.read_bytes();identity=W.identity(eid);source_map=audio.parent/'source-map.json'
    H.validate_receipt(eid,source_map)
    monkeypatch.setattr('app.services.external_narration.build',lambda *a,**k:pytest.fail('selection must not assemble'))
    revision=H.select(eid,source_map,expected_identity=identity)
    assert H.selected(eid)['selection_path']==str(revision.resolve())
    assert audio.read_bytes()==before and W.identity(eid)==identity and len(calls)==1
    assert not W.review_valid(eid,'pilot')
    payload=SV.read(source_map);payload['parts'][0]['text']='被换的文案';atomic_json(source_map,payload)
    with pytest.raises(ValueError,match='来源'):H.selected(eid)


@pytest.mark.NEEDS_FFMPEG
def test_selected_local_take_reaches_v2_without_second_tts_or_speed(generated_fixture,monkeypatch):
    from app.services import episode_production as EP, external_audio as EA, asr_check, narration_pipeline as N
    eid,audio,calls=generated_fixture;source_map=audio.parent/'source-map.json'
    H.select(eid,source_map,expected_identity=W.identity(eid))
    before=file_sha256(audio)
    monkeypatch.setattr(N,'synthesize',lambda *a,**k:pytest.fail('must not synthesize'))
    # Model output is mocked; actual plan, WAV assembly, provenance and stage handoff run.
    monkeypatch.setattr(EA,'recognize_track',lambda *a,**k:dict(words=[dict(word='玄奘走出城门',start=.1,end=2.3)],
        speech_end=2.3,sample_rate=16000,duration=2.5))
    monkeypatch.setattr(asr_check,'check',lambda p,t:SimpleNamespace(to_dict=lambda:dict(status='passed',audio_sha256=file_sha256(p))))
    result=EP.run(eid,stages=['tts','delivery'])
    assert result['run_ok'],result
    state=SV.read(paths.episode_dir(eid)/'production/v2/run.json');manifest=N.validate_assembly(state['manifest_path'])
    assert manifest['source_recording']['delivered_sha256']==before
    assert manifest['studio_narration']['episode_id']==eid and not manifest['approved']
    assert manifest['samples']==SV.read(audio.with_suffix('.speed.json'))['clock']['samples']
    assert [u['text'] for u in manifest['alignment']]==[u['source_text'] for u in W.document(eid)['units']]
    again=EP.run(eid,stages=['tts','delivery'])
    assert again['run_ok'] and len(calls)==1 and file_sha256(audio)==before
    assert SV.read(paths.episode_dir(eid)/'production/v2/run.json')['manifest_path']==state['manifest_path']
    from app.services import forced_alignment as F
    monkeypatch.setattr('app.services.local_asr.preflight',lambda:None)
    def fixture_alignment(args):
        unit,take,manifest,_,_=args
        seconds=take['samples']/manifest['sample_rate']
        words=[dict(word=c,start=i*seconds/len(unit['text']),end=(i+1)*seconds/len(unit['text']),probability=1.)
               for i,c in enumerate(unit['text'])]
        evidence=Path(take['audio_path']).with_suffix('.fixture-alignment.json')
        result=dict(words=words);atomic_json(evidence,result)
        return unit,take,{},evidence,result,evidence
    monkeypatch.setattr(F,'_align_take',fixture_alignment)
    aligned=EP.run(eid,stages=['alignment'])
    assert aligned['run_ok'],aligned
    state=SV.read(paths.episode_dir(eid)/'production/v2/run.json');evidence=SV.read(state['alignment_path'])
    assert ''.join(c['text'] for c in evidence['subtitles'])=='玄奘走出城门。'
    assert evidence['master_sha256']==manifest['master_sha256']
    assert max(c['sample_end'] for c in evidence['subtitles'])<=manifest['samples']
    F.require_evidence(manifest,dict(alignment_evidence_path=state['alignment_path'],subtitles=evidence['subtitles']))


def test_recovery_page_does_not_start_until_clicked(tmp_path,tmp_episodes,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app.services import studio_jobs as J
    eid=episode();root=paths.state_dir()/'studio-jobs/job-fixture';root.mkdir(parents=True)
    req=dict(ep_id=eid,expected_identity=W.identity(eid),max_requests=2,max_characters=100,max_images=0)
    atomic_json(root/'request.json',req)
    atomic_json(root/'submission.json',dict(request=req,request_sha256=file_sha256(root/'request.json')))
    atomic_json(root/'result.json',dict(status='BLOCKED',error='fixture interruption'))
    calls=[]
    monkeypatch.setattr(J,'_spawn',lambda r,q:calls.append((r,q)))
    monkeypatch.setattr(J,'launch',lambda *a,**k:pytest.fail('navigation started production'))
    app=AppTest.from_file(str(ROOT/'app/webui/Studio.py'),default_timeout=30).run()
    app.switch_page('app_pages/production.py').run()
    assert not app.exception and not calls
    next(b for b in app.button if b.label=='按原预算继续此任务').click().run()
    assert not app.exception and len(calls)==1 and calls[0][1]==req
    req['max_requests']=3;atomic_json(root/'request.json',req)
    app.run()
    assert not app.exception and not any(b.label=='按原预算继续此任务' for b in app.button)
    assert any('不能直接恢复' in x.value for x in app.warning)


def test_series_binding_page_uses_current_draft_without_generation(tmp_path,tmp_episodes,monkeypatch):
    from streamlit.testing.v1 import AppTest
    scheme,_=recipe(tmp_path);eid=episode()
    from app.services import studio_jobs as J
    monkeypatch.setattr(J,'launch',lambda *a,**k:pytest.fail('binding started production'))
    app=AppTest.from_file(str(ROOT/'app/webui/Studio.py'),default_timeout=30).run()
    app.switch_page('app_pages/pilot.py').run()
    assert not app.exception
    next(b for b in app.button if b.label=='将共用声音方案绑定到当前新稿').click().run()
    assert not app.exception and C.load(C.path_for(eid))['scheme_path']==str(scheme.resolve())
    assert not W.review_valid(eid,'pilot')


@pytest.mark.NEEDS_FFMPEG
def test_selection_page_selects_without_starting_a_job(generated_fixture,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app.services import studio_jobs as J
    eid,audio,calls=generated_fixture
    monkeypatch.setattr(J,'launch',lambda *a,**k:pytest.fail('selection started production'))
    monkeypatch.setattr(J,'jobs',lambda e:[dict(path='fixture-job',result=dict(status='VOICE_CANDIDATE',audio_path=str(audio),
        source_map_path=str(audio.parent/'source-map.json')))])
    app=AppTest.from_file(str(ROOT/'app/webui/Studio.py'),default_timeout=30).run()
    app.switch_page('app_pages/production.py').run()
    assert not app.exception
    next(b for b in app.button if b.label=='选用这条旁白制作整集').click().run()
    assert not app.exception and H.selected(eid)['master_sha256']==file_sha256(audio) and len(calls)==1


def test_running_job_and_changed_selected_track_cannot_resume(tmp_path,tmp_episodes,monkeypatch):
    from app.services import studio_jobs as J
    eid=episode();root=paths.state_dir()/'studio-jobs/recover';root.mkdir(parents=True)
    req=dict(ep_id=eid,expected_identity=W.identity(eid),narration_selection_sha256='old')
    atomic_json(root/'request.json',req)
    atomic_json(root/'submission.json',dict(request=req,request_sha256=file_sha256(root/'request.json')))
    monkeypatch.setattr(J,'alive',lambda r:True)
    with pytest.raises(ValueError,match='still running'):J.check_resume(root)
    with pytest.raises(ValueError,match='已有运行中的任务'):J.launch(eid)
    assert list(root.parent.glob('*/request.json'))==[root/'request.json']
    monkeypatch.setattr(J,'alive',lambda r:False)
    monkeypatch.setattr(H,'selected',lambda e:dict(selection_sha256='new'))
    with pytest.raises(ValueError,match='选用旁白已改变'):J.check_resume(root)


def test_job_status_recovers_last_checkpoint_when_process_disappears(tmp_episodes,monkeypatch):
    from app import paths
    from app.services import studio_jobs as J
    from app.services.provenance import atomic_json
    root=paths.state_dir()/'studio-jobs'/'checkpoint-fixture';root.mkdir(parents=True)
    atomic_json(root/'request.json',{'ep_id':'progress','job_mode':'video','max_images':0})
    atomic_json(root/'result.json',{'status':'QUEUED'})  # readable old job format
    atomic_json(root/'checkpoint.json',{'status':'RUNNING','stage':'alignment',
                'progress':{'done':8,'total':12},'heartbeat_at':123})
    monkeypatch.setattr(J,'check_resume',lambda _:None)
    monkeypatch.setattr(J,'alive',lambda _:True)
    row=J.jobs('progress')[0]
    assert row['status']=='RUNNING' and row['progress']['done']==8
    monkeypatch.setattr(J,'alive',lambda _:False)
    row=J.jobs('progress')[0]
    assert row['status']=='INTERRUPTED' and row['stage']=='alignment' and row['recoverable']
    assert row['progress']['total']==12 and row['exit_reason']
