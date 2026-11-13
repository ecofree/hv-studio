import importlib.util
import io
import json
from pathlib import Path
import wave
import numpy as np
import pytest
from app.services import narration_leveling as L, narration_master as M
from app.services.provenance import atomic_json,file_sha256
from app.services.provider_budget import ProviderBudget,BudgetExceeded

pytestmark=pytest.mark.production


def recording(path):
    sr=24000;t=np.arange(sr*20)/sr
    x=np.sin(2*np.pi*220*t)*2400
    x[sr*10:]*=3
    x[int(sr*9.8):int(sr*10.2)]=0
    L.save(path,np.rint(x).astype('<i2'),sr)
    return path


@pytest.mark.NEEDS_FFMPEG
def test_gain_repair_preserves_clock_silence_source_and_reduces_step(tmp_path):
    src=recording(tmp_path/'raw.wav');before=file_sha256(src)
    r=L.repair(src,tmp_path/'balanced.wav',boundaries=[240000],target_lufs=-25)
    x,sr=L.load(src);y,ysr=L.load(r['output_path'])
    assert len(x)==len(y)==r['samples'] and sr==ysr
    assert np.all(y[x==0]==0) and file_sha256(src)==before
    assert abs(r['after_seams'][0]['change_db'])<=2
    assert abs(r['before_seams'][0]['change_db'])>8
    assert r['after']['true_peak_dbtp']<=-1.8
    assert M.verify_repair(Path(r['output_path']).with_suffix('.level.json'))['samples']==len(x)


def test_overwrite_is_rejected_before_touching_audio(tmp_path):
    src=recording(tmp_path/'raw.wav');before=file_sha256(src)
    with pytest.raises(ValueError,match='overwrite'):L.repair(src,src)
    assert file_sha256(src)==before


@pytest.mark.parametrize('bounds',[[100,100],[-2],[480001],[1.5]])
def test_invalid_boundary_clocks_fail(bounds):
    with pytest.raises(ValueError):L.gain_curve(np.ones(480000,dtype='<i2'),24000,bounds)


@pytest.mark.NEEDS_FFMPEG
def test_selected_master_is_bound_to_plan_audio_and_transform(tmp_path):
    src=recording(tmp_path/'raw.wav');out=tmp_path/'balanced.wav'
    L.repair(src,out,boundaries=[240000],target_lufs=-25)
    plan=tmp_path/'plan.json';atomic_json(plan,dict(source_sha256='test-source'))
    root=tmp_path/'production';selected=M.select(root,plan,out.with_suffix('.level.json'))
    assert M.resolve(root,plan)==selected['master_path']
    audio=Path(selected['master_path']);audio.write_bytes(audio.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='changed'):M.resolve(root,plan)
    atomic_json(plan,dict(source_sha256='new-source'))
    with pytest.raises(ValueError,match='another plan'):M.resolve(root,plan)


def generator():
    path=Path(__file__).resolve().parents[1]/'resource/风趣历史.writing-DNA/scripts/generate_chutang_emotional.py'
    spec=importlib.util.spec_from_file_location('emotional_generation_fixture',path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_generation_caches_raw_chunks_and_charges_only_actual_attempts(tmp_path,tmp_episodes,monkeypatch):
    g=generator();src=recording(tmp_path/'fixture.wav');calls=[]
    def fake(*args,**kwargs):calls.append((args,kwargs));return src.read_bytes()
    monkeypatch.setattr(g,'call_tts_api',fake)
    from app.services import local_asr
    from app.services.provenance import fingerprint
    def recognize_fixture(p):
        r=dict(audio_sha256=file_sha256(p),text='旁白。',segments=[])
        receipt=tmp_path/'cached-fixture.json'
        atomic_json(receipt,dict(request=dict(audio_sha256=file_sha256(p)),result=r,result_sha256=fingerprint(r)))
        return r,receipt
    monkeypatch.setattr(local_asr,'recognize',recognize_fixture)
    with pytest.raises(ValueError,match='budget'):g.render_act('test','旁白。',tmp_path/'act.wav')
    assert not calls
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    g.render_act('test','旁白。',tmp_path/'act.wav',budget=budget)
    g.render_act('test','旁白。',tmp_path/'act.wav')
    assert len(calls)==1 and budget.snapshot()['used']['tts_requests']==1
    receipt=json.loads((tmp_path/'act.chunks.json').read_text(encoding='utf-8'))
    assert receipt['chunks'][0]['text']=='旁白。' and receipt['chunks'][0]['active_rms_dbfs'] is not None
    assert calls[0][1]['speaker']=='dylan'


def test_uncertain_generation_consumes_attempt_and_does_not_retry(tmp_path,tmp_episodes,monkeypatch):
    g=generator();budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100);calls=[]
    def fail(*a,**k):calls.append(1);raise TimeoutError('unknown result')
    monkeypatch.setattr(g,'call_tts_api',fail)
    with pytest.raises(TimeoutError):g.render_act('test','旁白。',tmp_path/'act.wav',budget=budget)
    assert len(calls)==1 and budget.snapshot()['attempts'][0]['status']=='failed_or_uncertain'
    with pytest.raises(BudgetExceeded):g.render_act('test','旁白。',tmp_path/'act.wav',budget=budget)
    assert len(calls)==1


def test_emphasis_is_scoped_to_relevant_text():
    g=generator()
    assert '只在誓言句' not in g.chunk_direction(3,'马匹走向西方。',0,2)
    assert '只在誓言句' in g.chunk_direction(3,'他发誓绝不后退。',1,2)
    assert '开篇从容' not in g.chunk_direction(0,'后面的叙事。',1,3)
