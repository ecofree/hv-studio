
import pytest

pytestmark = pytest.mark.production

import json
from pathlib import Path
from app.services import subtitle_alignment as A, asr_check
from app.services.provenance import atomic_json,file_sha256,fingerprint


def unit(text='兵贵神速。'):
    return {'unit_id':'one','text':text,'sample_start':0,'sample_end':48000}


def recognition():
    return {'segments':[{'words':[{'word':'兵贵','start':.1,'end':.8,'probability':.9},
                                  {'word':'神速。','start':.9,'end':1.7,'probability':.9}]}]}


def test_word_boundaries_are_preserved_not_divided_by_characters():
    cues,issues=A.align_unit(unit(),recognition(),24000)
    assert not issues and len(cues)==1
    assert cues[0]['text']=='兵贵神速。'
    assert (cues[0]['sample_start'],cues[0]['sample_end'])==(2400,40800)
    assert cues[0]['alignment_source']=='asr_word_timestamps_draft'


def test_misrecognized_text_is_not_silently_rewritten():
    r=recognition();r['segments'][0]['words'][1]['word']='失败。'
    cues,issues=A.align_unit(unit(),r,24000)
    assert any(i['kind']=='text_mismatch' for i in issues)
    with pytest.raises(ValueError,match='incomplete'):
        A.validate_cues({'alignment':[unit()]},cues)


def test_numeric_change_and_overlapping_times_cannot_pass():
    r={'segments':[{'words':[{'word':'十万','start':.1,'end':.8,'probability':.9}]}]}
    _,issues=A.align_unit(unit('一万。'),r,24000)
    assert any(i['kind']=='text_mismatch' for i in issues)
    cues=[dict(unit_id='one',text_start=0,text_end=2,text='兵贵',sample_start=0,sample_end=24000),
          dict(unit_id='one',text_start=2,text_end=5,text='神速。',sample_start=23000,sample_end=48000)]
    with pytest.raises(ValueError,match='overlaps'):A.validate_cues({'alignment':[unit()]},cues)


def test_phrase_timing_needs_real_review_record_not_reviewer_string():
    from app.services.production_v2 import validate_timeline
    cues,_=A.align_unit(unit(),recognition(),24000)
    cues[0].update(alignment_source='human_reviewed',reviewer='claimed-reviewer')
    timeline={'shots':[{'id':'shot','sample_start':0,'sample_end':48000}],'subtitles':cues}
    with pytest.raises(ValueError,match='hash-bound'):
        validate_timeline({'samples':48000,'sample_rate':24000,'alignment':[unit()]},timeline)


def test_review_binds_exact_subtitles_and_evidence(tmp_path,monkeypatch):
    manifest={'alignment':[unit()],'takes':[],'master_sha256':'fixture','sample_rate':24000,'samples':48000}
    manifest_file=tmp_path/'manifest.json';atomic_json(manifest_file,manifest)
    monkeypatch.setattr(A,'validate_assembly',lambda p:manifest)
    cues,_=A.align_unit(unit(),recognition(),24000)
    draft=tmp_path/'draft.json'
    atomic_json(draft,{'manifest_path':str(manifest_file),'manifest_content_sha256':fingerprint(manifest),
        'inputs':{str(manifest_file):file_sha256(manifest_file)},'subtitles':cues})
    record=A.review(draft,'offline-test','synthetic boundary test','approved')
    timeline={'subtitles':cues,'alignment_review_path':str(record)}
    assert A.require_review(manifest,timeline)
    cues[0]['sample_start']+=1
    with pytest.raises(ValueError,match='changed after review'):A.require_review(manifest,timeline)


def test_local_asr_backend_never_falls_back_to_cloud(tmp_path,monkeypatch):
    from app.config.config import get_settings
    from app.services import local_asr,cosyvoice
    monkeypatch.setattr(get_settings().get_profile().asr,'backend','local-whisper')
    monkeypatch.setattr(local_asr,'recognize',lambda *a:(_ for _ in ()).throw(RuntimeError('local unavailable')))
    monkeypatch.setattr(cosyvoice,'_dashscope_key',lambda:pytest.fail('cloud fallback'))
    audio=tmp_path/'audio.wav';audio.write_bytes(b'fixture')
    q=asr_check.check(audio,'兵贵神速。')
    assert q.status=='unavailable' and 'local unavailable' in q.error


def test_local_asr_provenance_is_kept_in_quality_result(tmp_path,monkeypatch):
    from app.config.config import get_settings
    from app.services import local_asr
    monkeypatch.setattr(get_settings().get_profile().asr,'backend','local-whisper')
    receipt=tmp_path/'recognition.json'
    monkeypatch.setattr(local_asr,'recognize',lambda *a:({'text':'兵贵神速。',
        'request_id':'local-fixture','checker_version':'test-version'},receipt))
    audio=tmp_path/'audio.wav';audio.write_bytes(b'fixture')
    q=asr_check.check(audio,'兵贵神速。')
    assert q.status=='passed' and q.request_id=='local-fixture'
    assert q.recognition_path==str(receipt.resolve()) and q.checker_version=='test-version'


def test_missing_local_model_blocks_tts_before_generation(tmp_path,tmp_episodes,monkeypatch):
    from app.config.config import get_settings
    from app.managers import save
    from app.models import Episode
    from app.orchestrator import produce
    from app.services import narration_pipeline as N
    ep=Episode(id='asr-preflight',title='test',episode_number=1);save(ep)
    directory=tmp_episodes/ep.id;directory.mkdir(exist_ok=True)
    (directory/'script.md').write_text('兵贵神速。',encoding='utf-8')
    monkeypatch.setattr(get_settings().get_profile().asr,'backend','local-whisper')
    monkeypatch.setattr(get_settings().get_profile().asr,'model_dir',str(tmp_path/'missing-model'))
    cache=tmp_path/'voice-cache.json';atomic_json(cache,dict(voice_id='cosyvoice-fixture',model='cosyvoice-v3.5-flash'))
    asset=tmp_path/'voice.json';N.import_voice(cache,asset)
    monkeypatch.setattr(get_settings().get_profile().voice,'voice_asset',str(asset))
    monkeypatch.setattr(N,'synthesize',lambda *a,**kw:pytest.fail('TTS called before ASR preflight'))
    r=produce(ep.id,stages=['tts'],max_requests=1,max_characters=10)
    assert r['status']=='BLOCKED' and 'model_manifest' in r['error']


def test_local_model_and_audio_identity_control_cache(tmp_path,monkeypatch):
    from app.services import local_asr as L
    from app.config.config import ProfileASR
    from types import SimpleNamespace
    monkeypatch.setattr(L,'ROOT',tmp_path)
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    model=tmp_path/'model';model.mkdir()
    names=['model.bin','config.json','tokenizer.json','vocabulary.txt']
    for name in names:(model/name).write_bytes(name.encode())
    atomic_json(model/'model_manifest.json',{'files':{n:{'sha256':file_sha256(model/n)} for n in names}})
    (tmp_path/'bin').mkdir();(tmp_path/'bin/local_asr_worker.py').write_text('fixture')
    (tmp_path/'requirements.asr.lock.txt').write_text('faster-whisper==test')
    runtime={'faster-whisper':'test'};monkeypatch.setattr(L,'runtime_versions',lambda *a:runtime)
    config=ProfileASR(model_dir=str(model),python_executable='fixture-python')
    audio=tmp_path/'audio.wav';audio.write_bytes(b'first-audio');calls=[]
    def execute(config,model,payload,timeout):
        calls.append(payload)
        return {'text':'兵贵神速。','audio_sha256':file_sha256(audio),'segments':[],'runtime':runtime}
    from app.services import acoustic_session
    monkeypatch.setattr(acoustic_session,'infer',execute)
    first,_=L.recognize(audio,config=config);again,_=L.recognize(audio,config=config)
    assert first==again and len(calls)==1
    audio.write_bytes(b'changed-audio');L.recognize(audio,config=config);assert len(calls)==2
    (model/'model.bin').write_bytes(b'changed-model')
    with pytest.raises(ValueError,match='model file changed'):L.recognize(audio,config=config)
    assert len(calls)==2
