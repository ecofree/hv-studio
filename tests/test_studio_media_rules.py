import copy
import json

import pytest

from app.services import studio_media as M
from app.services.provenance import atomic_json,file_sha256

pytestmark=pytest.mark.production


@pytest.fixture
def audio(tmp_path,monkeypatch):
    # No audio generation/codec in these tests; clocks are independently supplied.
    wav=tmp_path/'already-1.2.wav';wav.write_bytes(b'opaque-file-fixture')
    monkeypatch.setattr(M,'wav_clock',lambda _:dict(samples=240000,sample_rate=24000,channels=1,sample_width=2))
    receipt=wav.with_suffix('.speed.json')
    M.record_speed(wav,receipt,applied_rate=1.2,evidence='isolated fixture',source_identity='source-fixture')
    return wav,receipt


def test_existing_1p2_is_reused_without_processing(audio,monkeypatch):
    p=M.speed_plan(*audio,1.2,subtitles_aligned=True)
    assert p['factor']==1 and p['operation']=='reuse' and p['audio_filter'] is None
    def forbidden(*a,**k):raise AssertionError('No codec call for identical speed')
    monkeypatch.setattr(M.subprocess,'run',forbidden)
    assert M.apply_speed(p,audio[0].with_name('unused.wav'))==audio


def test_only_remaining_ratio_is_applied(audio):
    p=M.speed_plan(*audio,1.44)
    assert p['factor']==pytest.approx(1.2)
    assert p['audio_filter']=='atempo=1.2'
    with pytest.raises(ValueError,match='字幕已对齐'):M.speed_plan(*audio,1.44,subtitles_aligned=True)


def test_unknown_speed_cannot_be_guessed_from_filename(audio):
    with pytest.raises(ValueError,match='速度来源不明'):M.speed_plan(audio[0],None)


@pytest.mark.parametrize('bad',[0,-1,True,float('nan'),float('inf'),5])
def test_invalid_rate_cannot_form_a_plan(audio,bad):
    with pytest.raises(ValueError):M.speed_plan(*audio,bad)


def test_replacing_audio_or_receipt_invalidates_plan(audio):
    wav,receipt=audio;plan=M.speed_plan(*audio,1.44)
    wav.write_bytes(b'replaced')
    with pytest.raises(ValueError):M.apply_speed(plan,wav.with_name('output.wav'))


def test_four_lost_frames_and_changed_clock_fail():
    before=dict(geometry={'width':720,'height':1280},frames=2480,packets=2480,content_sha256='a',timing_sha256='t',start='0',end='248/3',extradata_hash='x')
    after=dict(before,frames=2476,packets=2476,end='1238/15')
    assert not M.compare_video_probes(before,after)['ok']
    assert M.compare_video_probes(before,before)['ok']
    changed=dict(before,timing_sha256='different')
    assert not M.compare_video_probes(before,changed)['ok']


def test_audio_pair_requires_same_source_speed_and_subtitles(audio,tmp_path):
    wav,receipt=audio;a=tmp_path/'a.srt';b=tmp_path/'b.srt';a.write_text('1');b.write_text('1')
    assert M.validate_audio_pair(wav,wav,receipt,receipt,a,b)['ok']
    b.write_text('2')
    assert not M.validate_audio_pair(wav,wav,receipt,receipt,a,b)['ok']
    second=tmp_path/'other.speed.json';r=json.loads(receipt.read_text());r['source_identity']='new-script';atomic_json(second,r)
    assert any('历史参照' in x for x in M.validate_audio_pair(wav,wav,receipt,second,a,a)['issues'])


def test_remux_never_truncates_video_or_overwrites_input(audio,tmp_path,monkeypatch):
    video=tmp_path/'video.mp4';video.write_bytes(b'opaque-video-fixture')
    monkeypatch.setattr(M,'inspect_video',lambda _:dict(start='0',end='10',sha256='video-fixture'))
    p=M.remux_plan(video,audio[0],tmp_path/'comparison.mp4')
    assert '-shortest' not in p['argv'] and '-t' not in p['argv']
    assert p['argv'][p['argv'].index('-c:v')+1]=='copy' and p['pad_silence_samples']==0
    with pytest.raises(ValueError,match='覆盖'):M.remux_plan(video,audio[0],video)
