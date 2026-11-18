"""Known acoustic changes, no speech model and no production material."""
import json
from pathlib import Path
import wave

import numpy as np
import pytest

from app.services import voice_consistency as A
from app.services.provenance import file_sha256, atomic_json

pytestmark=pytest.mark.production


def tone(path,hz=120,seconds=3.,level=.15,bright=False):
    sr=24000;t=np.arange(round(sr*seconds))/sr
    signal=np.sin(2*np.pi*hz*t)+(.7 if bright else .12)*np.sin(2*np.pi*hz*6*t)
    pcm=(np.clip(signal*level,-1,1)*32767).astype('<i2')
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,sr,0,'NONE','not compressed'));w.writeframes(pcm.tobytes())
    return path


@pytest.fixture
def refs(tmp_path):
    result=[]
    for i,hz in enumerate((119,120,121)):
        p=tone(tmp_path/f'ref-{i}.wav',hz)
        result.append(dict(path=str(p),sha256=file_sha256(p)))
    return result


def test_matching_sound_is_advisory_and_never_approved(tmp_path,refs):
    p=tone(tmp_path/'audio.wav');before=p.read_bytes()
    r=A.assess(p,refs,tmp_path/'report.json')
    assert r['status']=='no_acoustic_outliers' and not r['blocking']
    assert not r['approved'] and not r['speaker_identity_verified']
    assert p.read_bytes()==before and r['baseline']['reference_windows']==3


@pytest.mark.parametrize('change,expected',[(dict(hz=220),'pitch_drift'),(dict(bright=True),'timbre_drift_hint'),(dict(level=.7),'level_jump')])
def test_known_drift_is_located_without_resynthesis(tmp_path,refs,change,expected):
    p=tone(tmp_path/'audio.wav',**change)
    r=A.assess(p,refs,tmp_path/'report.json',segments=[dict(id='pickup-7',start=0.,end=3.)])
    assert any(expected in x['issues'] for x in r['findings'])
    assert all(x['segment_id']=='pickup-7' for x in r['findings'])
    assert not r['blocking']


def test_tail_and_middle_windows_are_checked(tmp_path,refs):
    a=tone(tmp_path/'a.wav',seconds=8.);b=tone(tmp_path/'b.wav',hz=220,seconds=8.)
    p=tmp_path/'whole.wav'
    with wave.open(str(a),'rb') as left,wave.open(str(b),'rb') as right,wave.open(str(p),'wb') as out:
        out.setparams(left.getparams());out.writeframes(left.readframes(left.getnframes()));out.writeframes(right.readframes(right.getnframes()))
    r=A.assess(p,refs,tmp_path/'report.json')
    assert r['windows'][-1]['end']==16.
    assert any(x['start']>=8. and 'pitch_drift' in x['issues'] for x in r['findings'])


@pytest.mark.parametrize('kind',['missing','changed','none'])
def test_missing_or_changed_reference_never_becomes_fake_pass(tmp_path,refs,kind):
    p=tone(tmp_path/'audio.wav')
    if kind=='missing':Path(refs[0]['path']).unlink()
    elif kind=='changed':tone(Path(refs[0]['path']),hz=200)
    else:refs=[]
    r=A.assess(p,refs,tmp_path/'report.json')
    assert r['status']=='reference_unavailable' and r['baseline'] is None
    assert not r['blocking'] and not r['speaker_identity_verified']


def test_silence_cannot_pass_voice_comparison(tmp_path,refs):
    p=tone(tmp_path/'audio.wav',level=0.)
    r=A.assess(p,refs,tmp_path/'report.json')
    assert r['status']=='review_hints'
    assert 'insufficient_voiced_audio' in r['findings'][0]['issues']


def test_gain_change_is_not_mislabelled_as_timbre_change(tmp_path,refs):
    p=tone(tmp_path/'audio.wav',level=.4)
    r=A.assess(p,refs,tmp_path/'report.json')
    assert any('level_jump' in row['issues'] for row in r['findings'])
    assert all('timbre_drift_hint' not in row['issues'] for row in r['findings'])


def test_report_cache_and_stale_readback(tmp_path,refs,monkeypatch):
    p=tone(tmp_path/'audio.wav');out=tmp_path/'report.json'
    first=A.assess(p,refs,out)
    monkeypatch.setattr(A,'features',lambda *a:pytest.fail('identical acoustic work should be cached'))
    assert A.assess(p,refs,out)==first
    tone(p,hz=220)
    with pytest.raises(ValueError,match='当前音频'):A.load_report(out,p)
    with pytest.raises(ValueError,match='新版本目录'):A.assess(p,refs,out)


def test_reference_change_invalidates_report_display(tmp_path,refs):
    p=tone(tmp_path/'audio.wav');out=tmp_path/'report.json'
    A.assess(p,refs,out);tone(Path(refs[0]['path']),hz=220)
    with pytest.raises(ValueError,match='参考'):A.load_report(out,p)


def test_review_panel_only_locates_audio_without_new_approval(tmp_path,refs):
    from streamlit.testing.v1 import AppTest
    p=tone(tmp_path/'audio.wav',hz=220);report=tmp_path/'report.json'
    A.assess(p,refs,report)
    source=tmp_path/'source-map.json';atomic_json(source,dict(voice_consistency_path=str(report)))
    code=f"from app.webui.voice_consistency_panel import render\nrender({str(p)!r},{str(source)!r},key='fixture')"
    app=AppTest.from_string(code).run(timeout=30)
    assert not app.exception and len(app.selectbox)==1 and len(app.button)==0
    assert any('不自动重配音' in x.value for x in app.caption)
