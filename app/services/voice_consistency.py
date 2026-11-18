"""Offline acoustic drift hints, never speaker certification or a release gate.

Compares pitch range, cepstral envelope and level against the existing selected
voice's hash-bound audition clips. No model, network, resynthesis or media write.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import wave

import numpy as np

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.qwen_voice import local_path

VERSION = 'voice-acoustic-drift-v1'
WINDOW_SECONDS = 8.0


def envelope_distance(values):
    """Upper-quartile RMS catches localized resonance changes without one-bin spikes."""
    squared=np.sort(np.asarray(values)**2,axis=-1)
    return np.sqrt(np.mean(squared[...,-max(1,squared.shape[-1]//4):],axis=-1))


def clock(path):
    with wave.open(str(path), 'rb') as f:
        if (f.getnchannels(), f.getsampwidth(), f.getcomptype()) != (1, 2, 'NONE'):
            raise ValueError('声音一致性检查需要单声道 PCM16 WAV')
        if f.getframerate() < 16000 or not f.getnframes():
            raise ValueError('声音一致性检查需要至少 16kHz 的有效录音')
        return f.getnframes(), f.getframerate()


def windows(start, end):
    """Cover the end too; do not sample only the beginning of a long take."""
    cursor = start
    while end-cursor > WINDOW_SECONDS+2:
        yield cursor, cursor+WINDOW_SECONDS
        cursor += WINDOW_SECONDS
    if end > cursor: yield cursor, end


def features(path, start, end):
    with wave.open(str(path), 'rb') as f:
        sr = f.getframerate()
        a, b = round(start*sr), min(f.getnframes(), round(end*sr))
        f.setpos(a)
        x = np.frombuffer(f.readframes(b-a), dtype='<i2').astype(float)/32768
    size, hop = round(.064*sr), round(.02*sr)
    base = dict(seconds=len(x)/sr, voiced_seconds=0., f0_hz=None, f0_range_hz=None,
                level_dbfs=None, envelope=None, clipped_fraction=float(np.mean(np.abs(x)>=.999)) if len(x) else 0.)
    if len(x) < size: return base
    frames = np.lib.stride_tricks.sliding_window_view(x, size)[::hop].copy()
    power = np.mean(frames*frames, axis=1)
    active = power > 1e-4  # silence is not a different speaker
    frames = frames[active]; power = power[active]
    if not len(frames): return base
    frames -= frames.mean(axis=1, keepdims=True)
    nfft = 1 << (2*size-1).bit_length()
    spectrum = np.abs(np.fft.rfft(frames*np.hanning(size), n=nfft))**2
    ac = np.fft.irfft(spectrum, n=nfft)[:, :size]
    ac /= np.maximum(ac[:, :1], 1e-12)
    lo, hi = round(sr/600), min(size-2, round(sr/55))
    pitches=[]; voiced=[]
    for index, row in enumerate(ac):
        peaks=np.flatnonzero((row[lo:hi]>row[lo-1:hi-1]) & (row[lo:hi]>=row[lo+1:hi+1]))+lo
        if not len(peaks): continue
        strongest=float(np.max(row[peaks]))
        if strongest < .45: continue
        lag=int(peaks[row[peaks]>=max(.45,strongest*.9)][0])
        pitches.append(sr/lag); voiced.append(index)
    if not pitches: return base
    # Fixed physical-frequency mel bands permit comparison across sample rates.
    mel=lambda hz:2595*np.log10(1+hz/700)
    hz=lambda m:700*(10**(m/2595)-1)
    edges=hz(np.linspace(mel(80),mel(7600),26))
    freq=np.fft.rfftfreq(nfft,1/sr)
    filters=np.maximum(0,np.minimum(
        (freq[None,:]-edges[:-2,None])/(edges[1:-1]-edges[:-2])[:,None],
        (edges[2:,None]-freq[None,:])/(edges[2:]-edges[1:-1])[:,None]))
    filters/=np.maximum(filters.sum(axis=1,keepdims=True),1e-12)
    log_energy=np.log(np.maximum(spectrum[voiced]@filters.T,1e-12))
    dct=np.cos(np.pi/24*(np.arange(24)[None,:]+.5)*np.arange(1,13)[:,None])
    envelope=np.median(log_energy@dct.T,axis=0)/24
    base.update(voiced_seconds=len(voiced)*hop/sr,f0_hz=float(np.median(pitches)),
        f0_range_hz=[float(v) for v in np.percentile(pitches,[10,90])],
        level_dbfs=float(10*np.log10(np.mean(power))),envelope=envelope.tolist())
    return base


def assess(audio, references, destination, *, segments=None):
    audio=local_path(audio); destination=Path(destination)
    samples,sr=clock(audio); duration=samples/sr
    segments=segments or [dict(id='track',start=0.,end=duration)]
    end=0.
    for row in segments:
        if (not all(math.isfinite(float(row[k])) for k in ('start','end'))
                or not 0<=row['start']<row['end']<=duration+1/sr or row['start']<end-1/sr):
            raise ValueError('声音检查分段时间无效')
        end=row['end']
    refs=[]; reference_issues=[]; inputs={str(audio):file_sha256(audio)}
    for item in references:
        path=local_path(item['path'])
        try:
            digest=file_sha256(path)
            if digest!=item['sha256']:raise ValueError('reference fingerprint changed')
            n,rate=clock(path)
            inputs[str(path)]=digest
            refs.append((path,n/rate))
        except (OSError,ValueError,wave.Error,EOFError) as exc:
            reference_issues.append(dict(path=str(path),reason=str(exc)))
    identity=fingerprint(dict(version=VERSION,implementation=file_sha256(__file__),inputs=inputs,
                              references=references,segments=segments,reference_issues=reference_issues))
    if destination.exists():
        old=json.loads(destination.read_text('utf-8'))
        if old.get('identity')==identity:return old
        raise ValueError('声音检查输入已变，请保留旧报告并使用新版本目录')
    reference_features=[]
    for path,seconds in refs:
        for a,b in windows(0.,seconds):
            f=features(path,a,b)
            if f['voiced_seconds']>=1. and f['seconds']>=2.:reference_features.append(f)
    adequate=len(reference_features)>=3 and not reference_issues
    baseline=None
    if adequate:
        envelopes=np.asarray([f['envelope'] for f in reference_features])
        center=np.median(envelopes,axis=0)
        scale=np.maximum(.12,1.4826*np.median(np.abs(envelopes-center),axis=0))
        distances=envelope_distance((envelopes-center)/scale)
        pitches=np.asarray([f['f0_hz'] for f in reference_features])
        pitch=float(np.median(pitches))
        pitch_spread=float(np.max(np.abs(12*np.log2(pitches/pitch))))
        levels=np.asarray([f['level_dbfs'] for f in reference_features])
        baseline=dict(f0_hz=pitch,pitch_tolerance_semitones=max(3.,pitch_spread+2.),
            level_dbfs=float(np.median(levels)),level_tolerance_db=max(6.,float(np.ptp(levels))+3.),
            envelope_center=center.tolist(),envelope_scale=scale.tolist(),
            envelope_distance_limit=max(3.,float(np.max(distances))+1.5),
            reference_windows=len(reference_features))
    findings=[]; rows=[]
    for segment in segments:
        for a,b in windows(segment['start'],min(duration,segment['end'])):
            f=features(audio,a,b); issues=[]; metrics={}
            if f['clipped_fraction']>.001:issues.append('clipping')
            if f['voiced_seconds']<1. or f['seconds']<2.:
                issues.append('insufficient_voiced_audio')
            elif baseline:
                shift=12*math.log2(f['f0_hz']/baseline['f0_hz'])
                distance=float(envelope_distance((np.asarray(f['envelope'])-center)/scale))
                level=f['level_dbfs']-baseline['level_dbfs']
                metrics=dict(pitch_shift_semitones=round(shift,2),envelope_distance=round(distance,3),level_delta_db=round(level,2))
                if abs(shift)>baseline['pitch_tolerance_semitones']:issues.append('pitch_drift')
                if distance>baseline['envelope_distance_limit']:issues.append('timbre_drift_hint')
                if abs(level)>baseline['level_tolerance_db']:issues.append('level_jump')
            row=dict(segment_id=segment['id'],start=round(a,3),end=round(b,3),features=f,metrics=metrics,issues=issues)
            rows.append(row)
            if issues:findings.append({k:row[k] for k in ('segment_id','start','end','metrics','issues')})
    result=dict(version=VERSION,identity=identity,audio_path=str(audio),audio_sha256=inputs[str(audio)],inputs=inputs,
        sample_rate=sr,samples=samples,baseline=baseline,reference_issues=reference_issues,windows=rows,findings=findings,
        status='reference_unavailable' if not adequate else ('review_hints' if findings else 'no_acoustic_outliers'),
        blocking=False,approved=False,speaker_identity_verified=False,
        scope='acoustic_drift_hints_only',
        limitations='声谱受字音和情绪影响；仅定位疑点，不证明说话人身份、发音正确或人工听审通过。')
    atomic_json(destination,result)
    return result


def load_report(path, audio):
    """UI readback must not display a stale report as a current result."""
    result=json.loads(Path(path).read_text('utf-8'))
    if result.get('version')!=VERSION or result.get('audio_sha256')!=file_sha256(local_path(audio)):
        raise ValueError('声音检查报告不对应当前音频')
    if any(file_sha256(local_path(p))!=sha for p,sha in result.get('inputs',{}).items()):
        raise ValueError('声音检查参考或音频已改变')
    return result
