import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services import narration_pipeline as N, production_v2 as P


def wav(path, samples=48000):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000)
        w.writeframes((np.sin(np.arange(samples)*.05)*2000).astype('<i2').tobytes())


def test_quality_numbers_and_stale_pass(tmp_path,monkeypatch):
    from app.services import asr_check as A
    p=tmp_path/'a.wav';wav(p)
    monkeypatch.setattr(A,'enabled',lambda:True)
    monkeypatch.setattr(A,'transcribe',lambda p:'一万大军走了3.5里。')
    result=A.check(p,'十万大军走了3.5里。')
    assert result.status=='failed' and any(f['kind']=='number' for f in result.findings)
    monkeypatch.setattr(A,'transcribe',lambda p:'11月，十万大军走了3.5里。')
    result=A.check(p,'十一月，十万大军走了3.5里。')
    assert result.status=='passed'
    assert result.valid_for(p,'十一月，十万大军走了3.5里。')
    wav(p,49000)
    assert not result.valid_for(p,'十一月，十万大军走了3.5里。')
    assert A.normalize_numbers('1.5万')=='15000'
    assert A.normalize_numbers('一亿二千万')=='120000000'


def test_conversion_unique_readable(tmp_path):
    from app.services.asr_check import _to_16k_wav
    a=_to_16k_wav(np.arange(24000),24000)
    b=_to_16k_wav(np.arange(24000),24000)
    try:
        assert a!=b
        for p in (a,b):
            with wave.open(str(p),'rb') as w:
                assert w.getframerate()==16000 and w.getnframes()==16000
    finally:
        a.unlink();b.unlink()


def test_transcription_calls_recognizer_and_cleans_temp(tmp_path,monkeypatch):
    from app.services import asr_check as A, cosyvoice
    import dashscope.audio.asr as sdk
    p=tmp_path/'a.wav';wav(p)
    paths=[]
    class Recognition:
        def __init__(self,**kw): pass
        def call(self,file):
            paths.append(Path(file));assert Path(file).exists()
            return SimpleNamespace(status_code=200,output={'sentence':[{'text':'原文。'}]})
    monkeypatch.setattr(sdk,'Recognition',Recognition)
    monkeypatch.setattr(cosyvoice,'_dashscope_key',lambda:'test-key')
    assert A.transcribe(p)=='原文。'
    assert len(paths)==1 and not paths[0].exists()
    monkeypatch.setattr(A,'_to_16k_wav',lambda *args:(_ for _ in ()).throw(ValueError('convert_failed')))
    result=A.check(p,'原文。')
    assert result.status=='unavailable' and 'convert_failed' in result.error


def test_document_preserves_source_and_delivery():
    from app.services.script_document import parse_document
    source='# 标题\n\n**整句正文！**\n\n> 天下太平？\n\n【语气：克制】行军3.5里。[停顿0.5秒]随后[停顿1.5秒]继续。\n\nHello world\nagain.\n\n> 作者：测试\n'
    d=parse_document(source)
    text=''.join(u['source_text'] for u in d['units'])
    assert '整句正文！' in text and '天下太平？' in text and '3.5里' in text
    assert 'Hello world again.' in text
    assert '作者' not in text and '停顿' not in text
    assert [c['pause_seconds'] for c in d['delivery_cues'] if c['intent']=='pause']==[.5,1.5]
    for b in d['blocks']:
        assert source[b['start']:b['end']]==b['raw_text']
        explained=set(b['source_map'])
        for e in b['excluded']:explained.update(range(e['start'],e['end']))
        assert explained==set(range(b['start'],b['end']))


def test_table_uses_same_body_contract():
    from app.services.script_document import parse_document
    d=parse_document('| 画面 | 旁白 |\n| --- | --- |\n| 城门 | **大唐靠什么？**[停顿0.5秒] |\n')
    assert d['units'][0]['source_text']=='大唐靠什么？'
    assert d['delivery_cues'][0]['pause_seconds']==.5


def assembly(tmp_path,monkeypatch=None,samples=48000):
    src=tmp_path/'script.md';src.write_text('第一段有十万人。\n\n第二段还有转折。',encoding='utf-8')
    plan=N.prepare(src,tmp_path/'plans')
    asset=tmp_path/'voice.json'
    atomic_json(asset,{'model':'cosyvoice-v3.5-flash','voice_id':'test-voice','version':'v1',
                      'capabilities':{'instruction':{'status':'unverified'}}})
    calls=[]
    def provider(req,path): calls.append(req);wav(path,samples);return {'source':'test_transport'}
    def checker(path,text):return {'status':'passed','audio_sha256':file_sha256(path),'expected_sha256':fingerprint(text)}
    manifest=N.synthesize(plan,asset,max_requests=2,max_characters=100,provider=provider,checker=checker)
    return manifest,plan,asset,calls,provider,checker


def test_audio_master_immutable_cache_and_budget(tmp_path):
    manifest,plan,asset,calls,provider,checker=assembly(tmp_path)
    m=N.validate_assembly(manifest)
    assert len(calls)==2 and m['samples']==96000
    raw=b''.join(N.wave_info(t['raw_path'])[1] for t in m['takes'])
    assert N.wave_info(m['master_path'])[1]==raw
    N.synthesize(plan,asset,max_requests=2,max_characters=100,provider=provider,checker=checker)
    assert len(calls)==2
    with pytest.raises(ValueError,match='budget'):
        N.synthesize(plan,asset,max_requests=1,max_characters=100,force=True,provider=provider,checker=checker)
    assert len(calls)==2
    before=file_sha256(m['master_path'])
    N.synthesize(plan,asset,max_requests=2,max_characters=100,force=True,provider=provider,checker=checker)
    assert len(calls)==4 and file_sha256(m['master_path'])==before


def test_full_request_invalidates_and_display_does_not():
    u={'source_text':'原文。','approved_spoken_text':'原文。','display_text':'原文。','delivery':{'instruction':''}}
    asset={'model':'m','voice_id':'v','version':'1','capabilities':{'instruction':{'status':'supported'}}}
    first=fingerprint(N.compile_request(u,asset))
    u['display_text']='改字幕换行'
    assert fingerprint(N.compile_request(u,asset))==first
    assert fingerprint(N.compile_request(u,asset,pitch=1.1))!=first
    u['delivery']['instruction']='a'*33+'b'
    before=fingerprint(N.compile_request(u,asset))
    u['delivery']['instruction']='a'*33+'c'
    assert fingerprint(N.compile_request(u,asset))!=before
    u['delivery']['instruction']='字'*51
    with pytest.raises(ValueError,match='100'):N.compile_request(u,asset)


def test_cumulative_boundaries_no_300_shot_drift():
    boundaries=[P.frame_boundary(i*72480,24000) for i in range(301)]
    assert boundaries[-1]==22650
    assert sum(b-a for a,b in zip(boundaries,boundaries[1:]))==22650


def test_v2_geometry_uses_full_cover_and_explicit_endpoints():
    from app.models.shot import MotionSpec
    from app.services.motion import frame_box,resolve_motion
    assert frame_box(1080,1920,1,.5,.5,'fit_cover_v2')==(0,0,1080,1920)
    assert frame_box(2160,3840,1,.5,.5,'fit_cover_v2')==(0,0,2160,3840)
    m=MotionSpec(geometry_version='fit_cover_v2',type='push',start_zoom=1,end_zoom=1.2,start_xy=(.2,.3),end_xy=(.7,.8))
    assert resolve_motion(m)==(1,1.2,(.2,.3),(.7,.8))


def test_batch_failure_persists_and_changed_stages_resume(tmp_path,monkeypatch):
    from app.services import hv_ops
    calls=[]
    monkeypatch.setattr(hv_ops.paths,'state_dir',lambda:tmp_path)
    def produce(eid,stages=None):calls.append(stages);return {'status':'FAILED'}
    monkeypatch.setattr('app.orchestrator.produce',produce)
    r=hv_ops.run_batch(['a','b'],batch_id='bound',stop_on_error=True,stages=['script'])
    assert N.read(r['state'])['failed'][0]['ep']=='a'
    artifact=tmp_path/'script.json';artifact.write_text('{}')
    monkeypatch.setattr('app.orchestrator.produce',lambda eid,stages=None:{'status':'SCRIPTED',
        'stages_done':['script'],'artifacts':[str(artifact)]})
    hv_ops.run_batch(['a'],batch_id='bound',stages=['script'])
    monkeypatch.setattr('app.orchestrator.produce',produce)
    hv_ops.run_batch(['a'],batch_id='bound',resume=True,stages=['verify'])
    assert calls==[['script'],['verify']]


@pytest.mark.NEEDS_FFMPEG
@pytest.mark.parametrize('samples',[48000,72480])
def test_real_v2_render_and_hash_bound_publication(tmp_path,samples):
    from PIL import Image
    manifest,*_=assembly(tmp_path,samples=samples)
    m=N.read(manifest)
    img=tmp_path/'image.png';Image.new('RGB',(540,960),(30,60,90)).save(img)
    subtitles=[dict(unit_id=u['unit_id'],sample_start=u['sample_start'],sample_end=u['sample_end'],
                    text=u['text'],text_start=0,text_end=len(u['text'])) for u in m['alignment']]
    timeline=tmp_path/'timeline.json'
    atomic_json(timeline,{'shots':[{'id':'one','sample_start':0,'sample_end':m['samples'],'image_path':str(img)}],
                          'subtitles':subtitles})
    receipt=P.render(manifest,timeline,tmp_path/'renders')
    review=P.review_candidate(receipt,'test-fixture','synthetic test only','approved')
    audio=N.record_listening_review(manifest,'test-fixture','synthetic test only','approved')
    pointer=tmp_path/'published.json'
    P.publish(receipt,review,audio,pointer)
    before=pointer.read_bytes()
    Image.new('RGB',(540,960),(90,60,30)).save(img)
    with pytest.raises(ValueError,match='input changed'):P.publish(receipt,review,audio,pointer)
    assert pointer.read_bytes()==before


def test_streaming_wav_header_finalize_keeps_pcm(tmp_path):
    import struct
    p=tmp_path/'raw.wav';wav(p)
    original=p.read_bytes();data=bytearray(original)
    struct.pack_into('<I',data,4,0x7fffffbf)
    struct.pack_into('<I',data,40,0x7fffff9b)
    p.write_bytes(data)
    before=file_sha256(p)
    canonical=N.canonicalize_provider_wav(p,{'status':'succeeded'})
    assert file_sha256(p)==before
    assert canonical.read_bytes()[44:]==original[44:]
    with pytest.raises(ValueError,match='truncated'):
        N.canonicalize_provider_wav(p,{'status':'failed'})


def test_installed_sdk_mock_transport_captures_effective_parameters(tmp_path,monkeypatch):
    from bin.probe_cosyvoice_contract import probe
    result=probe(tmp_path)
    params=[m['payload']['parameters'] for m in result['observed_messages'] if m['header']['action']=='run-task']
    assert [p['rate'] for p in params]==[1,1.2]
    assert all(p['enable_ssml'] is True for p in params)
    assert result['paid_calls']==0
    assert 'test-key' not in json.dumps(result)


def test_process_lock_excludes_same_process_threads_and_releases(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.process_lock import file_lock
    def acquire():
        with file_lock(tmp_path/'operation.lock'):return True
    with file_lock(tmp_path/'operation.lock'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(RuntimeError,match='running'):pool.submit(acquire).result()
    assert acquire()


def test_bad_image_cache_and_scoped_segment_hashes(tmp_path):
    from PIL import Image
    from app.services.image import ImageJob
    from app.services.compose import _sync_segment_params
    from app.models.shot import MotionSpec
    bad=tmp_path/'bad.png';bad.write_bytes(b'not an image'*10000)
    assert not ImageJob('bad',SimpleNamespace(),bad).is_done()
    a=tmp_path/'a.png';b=tmp_path/'b.png'
    for p in (a,b):Image.new('RGB',(20,30),(1,2,3)).save(p)
    def shot(name,path):return SimpleNamespace(id=name,duration_ms=2000,image_path=str(path),
                     upscaled_path=None,subtitle_text='字幕',motion=MotionSpec())
    timeline=[(shot('a',a),0,2000),(shot('b',b),2000,4000)]
    segments=[[row] for row in timeline];directory=tmp_path/'segments';directory.mkdir()
    _sync_segment_params(directory,timeline,['libx264'],'segmented',segments)
    for i in range(2):(directory/f'seg-{i:03d}.mp4').write_bytes(b'cached')
    Image.new('RGB',(20,30),(3,2,1)).save(a)
    _sync_segment_params(directory,timeline,['libx264'],'segmented',segments)
    assert not (directory/'seg-000.mp4').exists()
    assert (directory/'seg-001.mp4').read_bytes()==b'cached'


def test_revised_delivery_only_resynthesizes_target_unit(tmp_path):
    manifest,plan,asset,calls,provider,checker=assembly(tmp_path)
    before=file_sha256(plan)
    old=N.read(manifest)
    revised=N.revise_delivery(plan,old['takes'][0]['unit_id'],.92)
    new=N.synthesize(revised,asset,max_requests=1,max_characters=100,provider=provider,checker=checker)
    assert len(calls)==3 and calls[-1]['speech_rate']==.92
    assert file_sha256(plan)==before
    assert N.read(new)['takes'][1]['audio_sha256']==old['takes'][1]['audio_sha256']
    assert N.read(new)['takes'][1]['raw_path']==old['takes'][1]['raw_path']


def test_v2_render_segments_resume_from_state(tmp_path, monkeypatch):
    """A long v2 render keeps completed segments and resumes the missing one."""
    manifest, plan, asset, calls, provider, checker = assembly(tmp_path, samples=48000)
    m = N.read(manifest)
    from PIL import Image
    image = tmp_path / "image.png"
    Image.new("RGB", (540, 960), (30, 60, 90)).save(image)
    subtitles = [dict(unit_id=u["unit_id"], sample_start=u["sample_start"],
                       sample_end=u["sample_end"], text=u["text"], text_start=0,
                       text_end=len(u["text"])) for u in m["alignment"]]
    timeline = tmp_path / "timeline.json"
    atomic_json(timeline, {"shots": [
        {"id": "one", "sample_start": 0, "sample_end": 48000,
         "image_path": str(image)},
        {"id": "two", "sample_start": 48000, "sample_end": 96000,
         "image_path": str(image)},
    ], "subtitles": subtitles})

    from app import config as config_module
    from app.services import compose as C, media_integrity as MI
    settings = SimpleNamespace(get_profile=config_module.config.get_settings().get_profile, compose=SimpleNamespace(
        segment_max_sec=2.0, video_encoder="libx264", preset="medium",
        cq=20, fallbacks=[{"preset": "ultrafast", "crf": 20}],
    ))
    monkeypatch.setattr(config_module.config, "get_settings", lambda: settings)
    monkeypatch.setattr(C, "check_font_available", lambda: None)
    monkeypatch.setattr(P, "_probe_segment_frames", lambda path: 50)
    encoded = []

    def encode(rows, specs, cues, sample_rate, out_path, vcodec, timeout):
        encoded.append(Path(out_path).name)
        Path(out_path).write_bytes(b"segment")
        return sum(last - first for _, first, last in rows)

    monkeypatch.setattr(P, "_encode_v2_segment", encode)
    monkeypatch.setattr(C, "_concat_segments", lambda paths, out: Path(out).write_bytes(b"concat"))
    monkeypatch.setattr(C, "_run_audio_mux", lambda video, audio, out, codec, bitrate, timeout: Path(out).write_bytes(b"candidate"))
    monkeypatch.setattr(MI, "verify_media", lambda *args, **kwargs: {"status": "passed"})

    receipt = P.render(manifest, timeline, tmp_path / "renders", timeout=10)
    assert len(encoded) == 2
    state = N.read(N.read(receipt)["render_state_path"])
    assert state["status"] == "completed"
    assert all(row["status"] == "complete" for row in state["segments"])

    Path(receipt).unlink()
    Path(state["segments"][1]["path"]).unlink()
    before = len(encoded)
    receipt2 = P.render(manifest, timeline, tmp_path / "renders", timeout=10)
    assert len(encoded) == before + 1
    assert Path(receipt2).exists()
    # A same-frame-count replacement must not become the new trusted SHA.
    second_state = N.read(N.read(receipt2)['render_state_path'])
    Path(receipt2).unlink()
    Path(second_state['segments'][0]['path']).write_bytes(b'replaced-content')
    before = len(encoded)
    receipt3 = P.render(manifest, timeline, tmp_path/'renders', timeout=10)
    assert encoded[before:] == ['seg-000.mp4']
    third_state = N.read(N.read(receipt3)['render_state_path'])
    assert third_state['segments'][1]['reused'] is True
