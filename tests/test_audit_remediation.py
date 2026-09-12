"""Regressions for the independent audit's reproducible failure modes."""

import pytest

pytestmark = pytest.mark.production

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
from app.services import production_v2 as P
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services.narration_pipeline import read


def test_hard_segment_limit_keeps_every_frame_including_long_shot():
    timeline = {'shots':[dict(id='long', sample_start=0, sample_end=120*24000),
                         dict(id='tail', sample_start=120*24000, sample_end=123*24000)]}
    chunks = P._segment_ranges(timeline, 24000, 45)
    frames = [f for chunk in chunks for _, a, b in chunk for f in range(a,b)]
    assert frames == list(range(123*25))
    assert [sum(b-a for _,a,b in c) for c in chunks] == [1125,1125,825]


def test_large_subtitle_and_duration_are_rejected_before_encoding():
    from app.services.subtitle_layout import measure, validate_cues
    assert measure('长字幕'*40)['errors']
    cue = dict(unit_id='u', text='正常字幕。', sample_start=0, sample_end=5*24000)
    with pytest.raises(ValueError, match='duration'): validate_cues([cue], 24000)
    cue['sample_end'] = 2*24000
    assert validate_cues([cue], 24000)[0]['line_count'] <= 2


def test_direct_render_lock_covers_parallel_entrypoints(tmp_path, monkeypatch):
    active = threading.Event(); release = threading.Event()
    def render(*a): active.set(); assert release.wait(5); return 'finished'
    monkeypatch.setattr(P, '_render', render)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(P.render, tmp_path/'m', tmp_path/'t', tmp_path/'out')
        assert active.wait(5)
        try:
            with pytest.raises(RuntimeError, match='running'):
                P.render(tmp_path/'m', tmp_path/'t', tmp_path/'out')
        finally: release.set()
        assert first.result() == 'finished'


def test_shared_budget_survives_failure_restart_and_concurrency(tmp_path):
    from app.services.provider_budget import ProviderBudget, BudgetExceeded
    path = tmp_path/'budget.json'
    budget = ProviderBudget(path, tts_requests=3, characters=6, image_requests=4)
    token = budget.reserve('tts','one',3); budget.finish(token,False)
    restored = ProviderBudget(path, tts_requests=3, characters=6, image_requests=4)
    restored.reserve('tts','two',3)
    with pytest.raises(BudgetExceeded): restored.reserve('tts','three',1)
    def attempt(i):
        try: restored.reserve('image',str(i)); return True
        except BudgetExceeded: return False
    with ThreadPoolExecutor(8) as pool: results = list(pool.map(attempt, range(10)))
    assert sum(results) == 4
    assert restored.snapshot()['used'] == dict(tts_requests=2,characters=6,image_requests=4)
    with pytest.raises(ValueError,match='limits changed'): ProviderBudget(path,tts_requests=9)


def test_forced_alignment_packs_source_exactly_at_acoustic_boundaries():
    from app.services.forced_alignment import pack
    source = '甲军抵达城门。乙军守住大桥。'
    unit = dict(unit_id='u',text=source,sample_start=0,sample_end=6*24000)
    rows = [dict(text_start=i,text_end=i+1,sample_start=i*10000,sample_end=(i+1)*10000) for i in range(len(source))]
    cues = pack(unit,rows,24000)
    assert ''.join(c['text'] for c in cues) == source
    boundaries = {r['sample_start'] for r in rows}|{unit['sample_end']}
    assert all(c['sample_start'] in boundaries and c['sample_end'] in boundaries for c in cues)
    assert all(.8 <= (c['sample_end']-c['sample_start'])/24000 <= 4 for c in cues)


def test_pause_execution_preserves_every_original_pcm_sample(tmp_path, monkeypatch):
    from app.services import narration_pipeline as N, delivery
    from test_production_v2 import wav
    source = tmp_path/'source.md'; source.write_text('第一段正文。[停顿0.5秒]\n\n第二段正文。',encoding='utf-8')
    plan = N.prepare(source,tmp_path/'plans')
    asset = tmp_path/'voice.json'; atomic_json(asset,dict(model='cosyvoice-v3.5-flash',voice_id='fixture',version='v1',capabilities={}))
    def provider(req,path): wav(path,48000);return dict(status='succeeded',audio_sha256=file_sha256(path))
    def checker(p,text): return dict(status='passed',audio_sha256=file_sha256(p),expected_sha256=fingerprint(text))
    old_path = N.synthesize(plan,asset,max_requests=2,max_characters=100,provider=provider,checker=checker)
    old = read(old_path); sha = file_sha256(old['master_path'])
    derived = delivery.apply(old_path,read(asset)); new = N.validate_assembly(derived)
    assert new['samples'] == old['samples']+12000
    pcm = N.wave_info(new['master_path'])[1]; raw = N.wave_info(old['master_path'])[1]
    assert pcm[:96000]+pcm[120000:] == raw
    assert pcm[96000:120000] == b'\0'*24000
    assert file_sha256(old['master_path']) == sha
    assert delivery.apply(derived,read(asset)) == derived


def test_director_instruction_uses_only_verified_capability():
    from app.services.delivery import effective_unit, dispositions
    from app.services.narration_pipeline import compile_request
    unit = dict(unit_id='u',source_text='甲军抵达。',approved_spoken_text='甲军抵达。',delivery={})
    cue = dict(cue_id='c',unit_id='u',intent='direction',text_offset=0,raw_text='【语气：平稳】')
    plan = dict(units=[unit],delivery_cues=[cue])
    asset = dict(version='v',model='m',voice_id='v',capabilities={'instruction':{'status':'unverified'}})
    assert not compile_request(effective_unit(unit,plan,asset),asset)['instruction']
    assert dispositions(plan,asset)[0]['status'] == 'unsupported_needs_disposition'
    asset['capabilities']['instruction']['status']='supported'
    assert compile_request(effective_unit(unit,plan,asset),asset)['instruction']=='语气：平稳'


def test_traditional_number_notation_does_not_hide_actual_numeric_errors():
    from app.services.asr_check import normalize_numbers, phonetic_equivalent
    assert normalize_numbers('兩萬三千') == normalize_numbers('23000')
    assert normalize_numbers('三十万') != normalize_numbers('三百万')
    assert phonetic_equivalent('武德年间。','五德年间。')
    assert not phonetic_equivalent('三十万人。','三百万人。')
    assert normalize_numbers('陆军与伍员')=='陆军与伍员'


def test_reference_changed_after_preparation_is_rejected(tmp_path):
    import base64
    from app.models import ImageEditRequest
    from app.services.image import ImageJob
    from test_production_entrypoint import png
    ref=tmp_path/'ref.png';ref.write_bytes(png())
    req=ImageEditRequest(model='gpt-image-2',prompt='same person',image=base64.b64encode(ref.read_bytes()).decode())
    job=ImageJob('portrait',req,tmp_path/'out.png',(str(ref),))
    job.request_fingerprint()
    ref.write_bytes(png(color='blue'))
    with pytest.raises(ValueError,match='reference changed'):job.request_fingerprint()


def test_unknown_scene_source_is_rejected_instead_of_retimed():
    from app.services.production_timeline import scene_bindings
    ep = SimpleNamespace(frames=[SimpleNamespace(text='旧文本。')])
    with pytest.raises(ValueError,match='source differs'):
        scene_bindings(ep,{'alignment':[{'text':'新文本。'}]},dict(scenes=[dict(frames=[0,0],image='a')]))


@pytest.mark.NEEDS_FFMPEG
def test_new_episode_full_entrypoint_builds_timeline_and_multiple_episodes_resume(tmp_path, tmp_episodes, monkeypatch):
    """Real orchestration + files + encoder; synthetic providers/timings only."""
    from app.models import Episode
    from app.managers import save
    from app.config.config import get_settings
    from app.services import narration_pipeline as N, image as I, local_asr as L, forced_alignment as F, hv_ops
    from app.orchestrator import produce
    from test_production_v2 import wav
    from test_production_entrypoint import png
    from app.services.asr_check import Transcript, QualityResult
    cache=tmp_path/'voice-cache.json'; atomic_json(cache,dict(voice_id='cosyvoice-fixture',model='cosyvoice-v3.5-flash'))
    asset=tmp_path/'voice.json'; N.import_voice(cache,asset)
    monkeypatch.setattr(get_settings().get_profile().voice,'voice_asset',str(asset))
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    monkeypatch.setattr('app.services.production_inputs.preflight_media',lambda:None)
    monkeypatch.setattr(L,'preflight',lambda *a:('synthetic',{}))
    spoken = {}
    def provider(request,path):
        wav(path,48000); spoken[str(Path(path).parent.resolve())]=request['text']
        return dict(status='succeeded',audio_sha256=file_sha256(path))
    monkeypatch.setattr(N,'_provider',provider)
    def recognize(path, **kwargs):
        text = spoken[str(Path(path).parent.resolve())]
        result = dict(text=text,request_id='synthetic',checker_version='fixture',segments=[])
        receipt=tmp_path/'recognition'/f'{fingerprint(str(path))}.json'; atomic_json(receipt,result)
        return result, receipt
    monkeypatch.setattr(L,'recognize',recognize)
    def check(path,text, **kw):return QualityResult('passed',file_sha256(path),fingerprint(text),transcript=text)
    monkeypatch.setattr('app.services.asr_check.check',check)
    def align(path,source,recognition,duration,context=None):
        words=[dict(word=c,start=i*duration/len(source),end=(i+1)*duration/len(source),probability=1) for i,c in enumerate(source)]
        result=dict(words=words); receipt=tmp_path/'timing'/f'{fingerprint(str(path))}.json';atomic_json(receipt,result)
        return result,receipt
    monkeypatch.setattr(F,'align',align)
    calls=[]
    def generate(jobs, budget=None, **kw):
        for job in jobs:
            if job.is_done():continue
            token=budget.reserve('image',job.request_fingerprint());calls.append(job.id)
            job.install(png());budget.finish(token,True)
        return {j.id:str(j.out) for j in jobs}
    monkeypatch.setattr(I,'generate_many',generate)
    ids=['smoke-new-a','smoke-new-b']
    for eid in ids:
        save(Episode(id=eid,title='Synthetic acceptance',episode_number=1))
        directory=tmp_episodes/eid;directory.mkdir(exist_ok=True)
        (directory/'script.md').write_text('甲军抵达城门。\n\n乙军守住大桥。',encoding='utf-8')
    result=hv_ops.run_batch(ids,batch_id='new-entry',production_options=dict(max_requests=4,max_characters=100,max_images=4))
    assert result['done']==ids and not result['failed'] and len(calls)==4
    for eid in ids:
        state=read(tmp_episodes/eid/'production/v2/run.json')
        timeline=read(state['timeline_path']); assert len(timeline['shots'])==2
        assert state['last_result']['status']=='CANDIDATE' and not state['last_result']['approved']
        assert not (tmp_episodes/eid/'scene_plan.json').exists()
    resumed=hv_ops.run_batch(ids,batch_id='new-entry',resume=True,production_options=dict(max_requests=4,max_characters=100,max_images=4))
    assert resumed['done']==ids and not resumed['failed'] and len(calls)==4
