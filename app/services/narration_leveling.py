"""Reproducible gain-only narration repair. Never changes the sample clock.

Chunk baseline matching and slow RMS compression retain the source waveform;
quiet blocks are excluded from measurement, not replaced with generated audio.
"""
from pathlib import Path
import json
import math
import re
import shutil
import subprocess
import wave
import numpy as np
from app.services.provenance import atomic_json, file_sha256, fingerprint

VERSION = 'narration-gain-v1'


def load(path):
    with wave.open(str(path), 'rb') as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getcomptype() != 'NONE':
            raise ValueError('leveling requires mono PCM16; convert explicitly before repair')
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').copy()
    if not len(x): raise ValueError('empty audio')
    return x, sr


def save(path, pcm, sr):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, sr, 0, 'NONE', 'not compressed'))
        w.writeframes(np.asarray(pcm, dtype='<i2').tobytes())


def active_db(x, sr, gate_db=-40):
    block = max(1, round(sr * .02)); n = len(x) // block
    if not n: return None
    z = np.asarray(x[:n*block], dtype=np.float64) / 32768.
    power = np.mean(z.reshape(n, block) ** 2, axis=1)
    power = power[power > 10 ** (gate_db / 10)]
    return float(10 * np.log10(np.mean(power))) if len(power) else None


def boundaries_report(x, sr, boundaries, window=10/3):
    rows=[]
    for b in boundaries:
        a=max(0,b-round(window*sr)); e=min(len(x),b+round(window*sr))
        left,right=active_db(x[a:b],sr),active_db(x[b:e],sr)
        def fraction(values):
            block=max(1,round(sr*.02)); n=len(values)//block
            if not n:return 0.
            z=values[:n*block].astype(float).reshape(n,block)/32768
            return float(np.mean(np.mean(z*z,axis=1)>1e-4))
        fractions=[fraction(x[a:b]),fraction(x[b:e])]
        rows.append(dict(sample=b,second=b/sr,before_db=left,after_db=right,
                         active_fractions=fractions,comparable=min(fractions)>=.4,
                         change_db=None if left is None or right is None else round(right-left,3)))
    return rows


def measure(path):
    ff=shutil.which('ffmpeg')
    if not ff: raise ValueError('ffmpeg is required on PATH')
    r=subprocess.run([ff,'-nostdin','-hide_banner','-i',str(path),'-af','ebur128=peak=true',
                      '-f','null','-'],capture_output=True,text=True,encoding='utf-8',errors='replace',check=True,timeout=240)
    tail=r.stderr.rsplit('Summary:',1)[-1]
    return dict(lufs=float(re.search(r'I:\s*([-\d.]+) LUFS',tail)[1]),
                lra_lu=float(re.search(r'LRA:\s*([-\d.]+) LU',tail)[1]),
                true_peak_dbtp=float(re.search(r'Peak:\s*([-\d.]+) dBFS',tail)[1]))


def _smooth(v, width):
    k=max(1,int(width)); return np.convolve(np.pad(v,(k//2,(k-1)//2),mode='edge'),np.ones(k)/k,mode='valid')


def gain_curve(pcm, sr, boundaries=()):
    if any(type(b) is not int for b in boundaries) or list(boundaries)!=sorted(set(boundaries)):
        raise ValueError('chunk boundaries must be sorted unique integer samples')
    if any(not 0 < b < len(pcm) for b in boundaries): raise ValueError('boundary outside audio')
    block=max(1,round(sr*.02)); n=math.ceil(len(pcm)/block)
    z=np.pad(pcm.astype(np.float32)/32768.,(0,n*block-len(pcm)))
    power=np.mean(z.reshape(n,block)**2,axis=1,dtype=np.float64)
    voiced=power>10**(-40/10)
    if not voiced.any(): return np.arange(n)*block, np.zeros(n), {'silent':True}
    times=np.minimum(np.arange(n)*block+block/2,len(pcm)-1)
    edges=[0,*boundaries,len(pcm)]; levels=[]
    for a,b in zip(edges,edges[1:]):
        ids=(times>=a)&(times<b)&voiced
        levels.append(float(10*np.log10(power[ids].mean())) if ids.any() else None)
    ref=float(np.median([v for v in levels if v is not None]))
    base=np.zeros(n)
    for a,b,v in zip(edges,edges[1:],levels):
        base[(times>=a)&(times<b)]=np.clip(.70*(ref-v),-4,4) if v is not None else 0
    base=_smooth(base,round(.4*sr/block))
    corrected_power=power*10**(base/10)
    # A 2.4-second measurement window with only voiced blocks contributing.
    width=max(1,round(2.4*sr/block))
    count=_smooth(voiced.astype(float),width)
    average=_smooth(corrected_power*voiced,width)/np.maximum(count,1e-12)
    good=count>.1
    local=10*np.log10(np.maximum(average,1e-12))
    local=np.interp(np.arange(n),np.flatnonzero(good),local[good])
    target=float(np.median(local[voiced]))
    residual=_smooth(np.clip(.55*(target-local),-3,3),round(1.2*sr/block))
    gain=np.clip(base+residual,-6,6)
    # Balance only well-observed speech on either side of a splice. Do not
    # amplify whispered/breathy onsets merely because a fixed window is quiet.
    repairs=[]
    for iteration in range(3):
        for b in boundaries:
            lo=max(0,b-round(10/3*sr)); hi=min(len(pcm),b+round(10/3*sr))
            source=pcm[lo:hi].astype(float)
            weights=10**(np.interp(np.arange(lo,hi),times,gain)/20)
            part=np.rint(source*weights)
            row=boundaries_report(part,sr,[b-lo])[0]
            change=row['change_db']
            if not row['comparable'] or change is None or abs(change)<=1.8:continue
            attenuation=min(3.,abs(change)-1.5)
            # Smoothly attenuate the louder side, hold across the measurement
            # window, then return to its existing emotional contour over 5s.
            t=(times-b)/sr
            if change<0:t=-t
            shape=np.interp(t,[-.35,.35,4.,9.],[0,1,1,0],left=0,right=0)
            shape=.5-.5*np.cos(np.pi*shape)
            gain-=attenuation*shape
            repairs.append(dict(iteration=iteration,boundary=b,observed_db=change,attenuation_db=attenuation))
    return times,gain,dict(chunk_levels_db=levels,chunk_reference_db=ref,
                           local_target_db=target,voiced_blocks=int(voiced.sum()),seam_repairs=repairs)


def apply_curve(pcm, times, gain_db, global_db=0):
    out=np.empty_like(pcm)
    peak=0.
    # Bound memory and never clip silently. All gains are positive scalars.
    for a in range(0,len(pcm),1048576):
        b=min(len(pcm),a+1048576)
        g=10**((np.interp(np.arange(a,b),times,gain_db)+global_db)/20)
        y=pcm[a:b].astype(np.float64)*g
        peak=max(peak,float(np.abs(y).max()))
        if peak>32767: raise ValueError('gain exceeds PCM headroom')
        out[a:b]=np.rint(y).astype('<i2')
    return out


def repair(source, destination, *, boundaries=(), target_lufs=-23., peak_db=-2.):
    source,destination=Path(source),Path(destination)
    if source.resolve()==destination.resolve() or destination.exists():
        raise ValueError('repair must create a new version; source/output overwrite refused')
    if not math.isfinite(target_lufs) or not math.isfinite(peak_db) or peak_db> -1:
        raise ValueError('invalid loudness target')
    source_sha=file_sha256(source); pcm,sr=load(source)
    boundaries=list(boundaries)
    times,gain,stats=gain_curve(pcm,sr,boundaries)
    # Headroom is a constant gain, not hard clipping or a pitch/timing change.
    peak=0.
    for a in range(0,len(pcm),1048576):
        b=min(len(pcm),a+1048576)
        g=10**(np.interp(np.arange(a,b),times,gain)/20)
        peak=max(peak,float(np.max(np.abs(pcm[a:b].astype(float)*g)))/32768)
    safe_db=peak_db-20*np.log10(max(peak,1e-12))
    destination.parent.mkdir(parents=True,exist_ok=True)
    draft=destination.with_name(destination.stem+'.stage.wav')
    if draft.exists(): raise ValueError('staged output already exists')
    initial_db=min(0,float(safe_db))
    save(draft,apply_curve(pcm,times,gain,initial_db),sr)
    measured=measure(draft)
    global_db=min(initial_db+target_lufs-measured['lufs'],float(safe_db))
    output=apply_curve(pcm,times,gain,global_db)
    save(destination,output,sr)
    after=measure(destination); before=measure(source)
    if after['true_peak_dbtp']>peak_db+.2: raise ValueError('true peak exceeds requested ceiling')
    if file_sha256(source)!=source_sha: raise ValueError('source changed during repair')
    after_pcm,after_sr=load(destination)
    assert after_sr==sr and len(after_pcm)==len(pcm)
    assert np.all(after_pcm[pcm==0]==0)
    receipt=dict(version=VERSION,source_path=str(source.resolve()),source_sha256=source_sha,
        output_path=str(destination.resolve()),output_sha256=file_sha256(destination),
        engine_sha256=file_sha256(__file__),samples=len(pcm),sample_rate=sr,
        method='positive_scalar_gain_only_no_resynthesis_no_resampling',global_db=global_db,
        curve_samples=times.tolist(),curve_db=np.round(gain,6).tolist(),boundaries=boundaries,
        parameters=dict(target_lufs=target_lufs,peak_db=peak_db),stats=stats,before=before,after=after,
        loudness_target_reached=abs(after['lufs']-target_lufs)<=.3,
        before_seams=boundaries_report(pcm,sr,boundaries),after_seams=boundaries_report(output,sr,boundaries),
        exact_sample_clock=True,exact_digital_silence=True,approved=False,listening_review='pending')
    receipt['processing_fingerprint']=fingerprint({k:receipt[k] for k in ['source_sha256','engine_sha256','parameters','boundaries']})
    atomic_json(destination.with_suffix('.level.json'),receipt)
    return receipt
