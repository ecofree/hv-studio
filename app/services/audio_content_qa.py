"""Read-only speech checks. Findings are review requests, never listening approval."""
from __future__ import annotations
import difflib
import wave
from pathlib import Path
import numpy as np
from app.services.provenance import atomic_json, file_sha256, fingerprint


def acoustic(audio):
    with wave.open(str(audio),'rb') as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2:raise ValueError('QA requires mono PCM16')
        sr,n=w.getframerate(),w.getnframes();data=w.readframes(n)
    if not n or len(data)!=n*2:raise ValueError('empty or truncated audio')
    x=np.frombuffer(data,dtype='<i2').astype(np.float64)
    step=max(1,round(sr*.02))
    energy=np.array([np.sqrt(np.mean(x[i:i+step]**2)) for i in range(0,n,step)])
    quiet=energy<32768*10**(-50/20)
    spans=[];start=None
    for i,value in enumerate(list(quiet)+[False]):
        if value and start is None:start=i
        if not value and start is not None:
            a,b=start*step,min(i*step,n);start=None
            kind='leading' if a==0 else 'trailing' if b==n else 'internal'
            threshold=3 if kind=='internal' else 2
            if (b-a)/sr>=threshold:spans.append(dict(kind='long_silence',position=kind,start_sample=a,end_sample=b,seconds=(b-a)/sr))
    if quiet.all():spans.append(dict(kind='no_audible_speech'))
    return dict(samples=n,sample_rate=sr,duration_seconds=n/sr,findings=spans)


def transcript(source, heard):
    from app.services.subtitle_alignment import symbols
    a,positions=symbols(source);b,_=symbols(heard)
    if not a:raise ValueError('empty narration source')
    issues=[];changed=0
    for op,i,j,k,l in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes():
        if op=='equal':continue
        changed+=max(j-i,l-k)
        if max(j-i,l-k)<4:continue
        start=positions[i] if i<len(positions) else len(source)
        end=positions[j] if j<len(positions) else len(source)
        if op=='insert':
            fragment=b[k:l]
            repeated=any(a[n:n+len(fragment)]==fragment for n in range(len(a)-len(fragment)+1))
            kind='possible_repetition' if repeated else 'unexpected_speech'
        else:kind='possible_omission' if op=='delete' else 'content_mismatch'
        issues.append(dict(kind=kind,text_start=start,text_end=end,source_excerpt=source[start:end],
                           expected_symbols=j-i,heard_symbols=l-k))
    if not b:issues.append(dict(kind='empty_transcript'))
    elif changed/len(a)>.18 and not issues:issues.append(dict(kind='content_mismatch',symbol_error_ratio=changed/len(a)))
    return dict(findings=issues,symbol_error_ratio=changed/len(a))


def evaluate(audio, text, *, recognition=None, recognition_path=None, destination=None):
    """No recognition is launched here. A WAV alone can never pass content QA."""
    sha=file_sha256(audio);evidence={}
    if recognition_path:
        import json
        p=Path(recognition_path);record=json.loads(p.read_text('utf-8'))
        if record.get('result_sha256')!=fingerprint(record['result']):raise ValueError('ASR cache result changed')
        if record['request']['audio_sha256']!=sha:raise ValueError('ASR cache belongs to different audio')
        if recognition is not None and recognition!=record['result']:raise ValueError('ASR result differs from receipt')
        recognition=record['result'];evidence[str(p.resolve())]=file_sha256(p)
    if recognition is not None and recognition.get('audio_sha256')!=sha:raise ValueError('ASR audio binding differs')
    result=acoustic(audio);findings=list(result.pop('findings'))
    compared=transcript(text,recognition['text']) if recognition else None
    findings.extend(compared['findings'] if compared else [dict(kind='transcript_check_pending')])
    if file_sha256(audio)!=sha:raise ValueError('audio changed during QA')
    record=dict(version=1,audio_path=str(Path(audio).resolve()),audio_sha256=sha,source_sha256=fingerprint(text),
                checker_sha256=file_sha256(__file__),recognition_sha256=fingerprint(recognition) if recognition else None,
                inputs=evidence,**result,findings=findings,status='needs_review' if findings else 'passed_machine',
                listening_approved=False,approved=False)
    if destination:
        atomic_json(Path(destination)/(fingerprint(record)+'.json'),record)
    return record


def require_content(audio,text,destination,*,recognizer=None):
    from app.services.local_asr import recognize
    # Gross silence is actionable before spending time loading a recognition model.
    first=evaluate(audio,text,destination=destination)
    if any(f['kind']!='transcript_check_pending' for f in first['findings']):
        raise ValueError('audio QA needs review: long silence / no speech; raw take preserved')
    result,path=(recognizer or recognize)(audio)
    record=evaluate(audio,text,recognition=result,recognition_path=path,destination=destination)
    if record['status']!='passed_machine':raise ValueError('audio QA needs review: missing/repeated/content mismatch; no automatic re-TTS')
    return record
