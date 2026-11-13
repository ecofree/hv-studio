"""Content identities exclude scheduling knobs, but retain model/options/runtime."""
import json
import wave
from pathlib import Path
from app.services.provenance import atomic_json,file_sha256,fingerprint


def identity(request):return fingerprint({k:v for k,v in request.items() if k not in ('threads','python')})


def validated(path,request=None):
    r=json.loads(Path(path).read_text('utf-8'))
    if r.get('result_sha256')!=fingerprint(r['result']):raise ValueError('recognition cache corrupted')
    if request is not None and identity(r['request'])!=identity(request):raise ValueError('recognition cache identity differs')
    if r['result'].get('audio_sha256')!=r['request']['audio_sha256']:raise ValueError('recognition cache audio differs')
    for p,h in r.get('inputs',{}).items():
        if file_sha256(p)!=h:raise ValueError('derived recognition input changed')
    return r


def promote(path):
    from app import paths
    r=validated(path)
    alias=paths.state_dir()/'local_asr/by-content'/(identity(r['request'])+'.json')
    # Preserve the original cache bytes and request id.
    atomic_json(alias,dict(path=str(Path(path).resolve()),sha256=file_sha256(path)))
    return alias


def lookup(request,exact):
    from app import paths
    alias=paths.state_dir()/'local_asr/by-content'/(identity(request)+'.json')
    if Path(exact).is_file():
        r=validated(exact,request)
        if not alias.is_file():promote(exact)
        return r['result'],Path(exact)
    if not alias.exists():return None
    ref=json.loads(alias.read_text('utf-8'))
    if file_sha256(ref['path'])!=ref['sha256']:raise ValueError('recognition cache receipt changed')
    r=validated(ref['path'],request)
    return r['result'],Path(ref['path'])


def slice_cached(master,clip,start_sample,end_sample,parent_path,destination):
    """Reuse independent ASR only for an exact PCM cut, with no crossed words."""
    parent=validated(parent_path);master_sha=file_sha256(master)
    if parent['request']['audio_sha256']!=master_sha:raise ValueError('parent recognition belongs to another master')
    with wave.open(str(master),'rb') as m, wave.open(str(clip),'rb') as c:
        if (m.getnchannels(),m.getsampwidth(),m.getframerate())!=(c.getnchannels(),c.getsampwidth(),c.getframerate()):
            raise ValueError('clip clock/format changed')
        sr=m.getframerate()
        if not 0<=start_sample<end_sample<=m.getnframes():raise ValueError('clip bounds invalid')
        m.setpos(start_sample);expected=m.readframes(end_sample-start_sample)
        if c.getnframes()!=end_sample-start_sample or c.readframes(c.getnframes())!=expected:
            raise ValueError('clip is not exact master PCM')
    words=[]
    for s in parent['result']['segments']:
        for w in s.get('words',[]):
            a,b=round(w['start']*sr),round(w['end']*sr)
            if b<=start_sample or a>=end_sample:continue
            if a<start_sample or b>end_sample:raise ValueError('word crosses clip boundary; use separate recognition')
            words.append(dict(w,start=(a-start_sample)/sr,end=(b-start_sample)/sr))
    if not words:raise ValueError('no observed words in clip')
    sha=file_sha256(clip)
    result=dict(parent['result'],audio_sha256=sha,text=''.join(w['word'] for w in words),
                segments=[dict(start=0,end=(end_sample-start_sample)/sr,words=words)])
    request=dict(parent['request'],audio_sha256=sha,derivation=dict(parent_sha256=file_sha256(parent_path),
                 start_sample=start_sample,end_sample=end_sample,master_sha256=master_sha))
    result['request_id']='slice-'+fingerprint(request)
    record=dict(request=request,result=result,result_sha256=fingerprint(result),
                inputs={str(Path(master).resolve()):master_sha,str(Path(clip).resolve()):sha,
                        str(Path(parent_path).resolve()):file_sha256(parent_path)})
    path=Path(destination)/(fingerprint(request)+'.json');atomic_json(path,record)
    return result,path


