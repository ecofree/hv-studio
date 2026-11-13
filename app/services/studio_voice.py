"""One local-voice take cache: exact requests, bounded calls, recoverable receipts."""
from __future__ import annotations

import io
import wave
from contextvars import ContextVar
from pathlib import Path

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.process_lock import file_lock
from app.services.provider_budget import ProviderBudget, BudgetExceeded, tts_budget_scope
from app.services.narration_pipeline import read

_dispatch = ContextVar('studio_local_voice_dispatch', default=None)


def require_dispatch(text,speaker,mode,instruction,reference):
    r=_dispatch.get()
    if r is None:raise ValueError('local TTS requires a budgeted studio_voice request')
    expected=r['reference']['path'] if r['reference'] else None
    if (text,speaker,mode,instruction or '',str(reference) if reference else None)!=(r['text'],r['speaker'],r['mode'],r['instruction'],expected):
        raise ValueError('local TTS arguments differ from the charged request')


def request(text, *, speaker, mode, instruction, model, engine_revision, reference=None):
    if not text.strip() or not speaker or not model or not engine_revision:
        raise ValueError('正文、说话人、模型与引擎版本必须明确')
    if mode not in ('clone','custom'): raise ValueError('unknown voice mode')
    if mode=='clone' and instruction: raise ValueError('clone 接口不支持情绪指令，不能静默更换声音')
    ref=None
    if reference:
        p=Path(reference).resolve()
        ref=dict(path=str(p),sha256=file_sha256(p))
    if mode=='clone' and not ref: raise ValueError('clone requires the existing reference recording')
    return dict(version=1,text=text,speaker=speaker,mode=mode,instruction=instruction or '',
                model=model,engine_revision=engine_revision,language='chinese',reference=ref)


def wav_info(data):
    with wave.open(io.BytesIO(data),'rb') as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2 or w.getnframes()<=0 or w.getframerate()<=0:
            raise ValueError('provider returned invalid mono PCM audio')
        frames=w.getnframes();rate=w.getframerate();pcm=w.readframes(frames)
        if len(pcm)!=frames*2: raise ValueError('provider WAV is truncated')
    return frames,rate


def render_take(req,cache,provider,*,budget=None,retry_uncertain=False):
    from app.services.studio_voice_contract import validate
    locked=validate(req)
    cache=Path(cache);key=fingerprint(req);root=cache/key
    raw=root/'raw.wav';receipt=root/'receipt.json';attempt=root/'attempt.json'
    with file_lock(cache/(key+'.lock')):
        if receipt.exists():
            r=read(receipt)
            if r.get('request')!=req or not raw.exists() or r.get('audio_sha256')!=file_sha256(raw):
                raise ValueError('voice cache identity or bytes changed')
            wav_info(raw.read_bytes())
            # A crash between receipt publication and ledger completion is recoverable.
            if budget is not None and r.get('budget_path')==str(budget.path.resolve()):
                state=budget.snapshot();a=next((a for a in state['attempts'] if a['id']==r['attempt_id']),None)
                if a and a['status']=='reserved': budget.finish(a['id'],True)
            return raw,r
        if raw.exists(): raise ValueError('raw audio has no receipt; inspect before retrying')
        if not isinstance(budget,ProviderBudget): raise ValueError('new TTS requests require an explicit durable budget')
        budget.check_capacity(tts_requests=1,characters=len(req['text']))
        if attempt.exists() and not retry_uncertain:
            raise ValueError('previous voice outcome is uncertain; inspect receipt or explicitly retry with remaining budget')
        if req['reference'] and file_sha256(req['reference']['path'])!=req['reference']['sha256']:
            raise ValueError('voice reference changed before dispatch')
        validator=getattr(provider,'validate_request',None)
        if locked and not validator:raise ValueError('provider cannot enforce the confirmed voice contract')
        if validator:validator(req)
        token=budget.reserve('tts',key,characters=len(req['text']))
        parent_attempt=read(attempt)['attempt_id'] if attempt.exists() else None
        if attempt.exists(): atomic_json(root/'attempts'/(read(attempt)['attempt_id']+'.json'),read(attempt))
        ledger=dict(attempt_id=token,request_fingerprint=key,budget_path=str(budget.path.resolve()),recovery_of=parent_attempt)
        atomic_json(attempt,dict(ledger,status='reserved'))
        try:
            token_scope=_dispatch.set(req)
            try:
                with tts_budget_scope(budget):
                    data=provider(req['text'],speaker=req['speaker'],mode=req['mode'],instruct=req['instruction'] or None,
                                  ref_audio=req['reference']['path'] if req['reference'] else None)
            finally:_dispatch.reset(token_scope)
            frames,rate=wav_info(data)
            root.mkdir(parents=True,exist_ok=True)
            temp=raw.with_suffix('.pending');temp.write_bytes(data);temp.replace(raw)
            r=dict(request=req,request_fingerprint=key,audio_sha256=file_sha256(raw),samples=frames,sample_rate=rate,
                   attempt_id=token,budget_path=str(budget.path.resolve()),approved=False,listening_review='pending',content_qa='pending')
            atomic_json(receipt,r)
            budget.finish(token,True)
            atomic_json(attempt,dict(ledger,status='audio_received_content_unchecked'))
            return raw,r
        except Exception as exc:
            budget.finish(token,False)
            atomic_json(attempt,dict(ledger,status='failed_or_uncertain',error_type=type(exc).__name__,error=str(exc)[-1200:]))
            raise


def render_act(chunks,output,*,speaker,mode,model,engine_revision,provider,budget=None,reference=None,retry_uncertain=False,cache=None,voice_contract=None,recognizer=None):
    """Shared assembly for uniform and emotional directions. Old takes stay intact."""
    from app.services.narration_leveling import load,active_db
    output=Path(output);records=[];parts=[];cursor=0;sample_rate=None
    requests=[request(text,speaker=speaker,mode=mode,instruction=direction,model=model,
                      engine_revision=engine_revision,reference=reference) for text,direction in chunks]
    if voice_contract:
        from app.services.studio_voice_contract import bind
        requests=[bind(req,voice_contract) for req in requests]
    identity=fingerprint(requests)
    sidecar=output.with_suffix('.chunks.json')
    # Refuse old same-name caches before any new call; callers choose a new revision path.
    if output.exists() or sidecar.exists():
        if not output.exists() or not sidecar.exists(): raise ValueError('existing act lacks exact request receipt; choose a new output revision')
        old=read(sidecar)
        if old.get('requests_identity')!=identity or old.get('source_sha256')!=file_sha256(output):
            raise ValueError('raw act overwrite refused; choose a new output revision')
    for req in requests:
        path,receipt=render_take(req,Path(cache) if cache else output.parent/'takes',provider,budget=budget,retry_uncertain=retry_uncertain)
        from app.services.audio_content_qa import require_content
        qa=require_content(path,req['text'],path.parent/'content-qa',recognizer=recognizer)
        pcm,sr=load(path)
        if sample_rate is not None and sr!=sample_rate: raise ValueError('句块采样率不同，不能保持同一时钟')
        sample_rate=sr
        if parts: cursor+=round(sr*.35)
        records.append(dict(request_fingerprint=receipt['request_fingerprint'],receipt_path=str(path.with_name('receipt.json').resolve()),
                            receipt_sha256=file_sha256(path.with_name('receipt.json')),raw_path=str(path.resolve()),raw_sha256=receipt['audio_sha256'],
                            text=req['text'],instruction=req['instruction'],content_qa=qa,sample_start=cursor,sample_end=cursor+len(pcm),active_rms_dbfs=active_db(pcm,sr)))
        parts.append(pcm.tobytes());cursor+=len(pcm)
    if not parts: raise ValueError('没有可朗读正文')
    buf=io.BytesIO()
    with wave.open(buf,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(sample_rate)
        for i,pcm in enumerate(parts):
            if i:w.writeframes(b'\0\0'*round(sample_rate*.35))
            w.writeframes(pcm)
    payload=buf.getvalue()
    if output.exists() and output.read_bytes()!=payload: raise ValueError('raw act overwrite refused')
    output.parent.mkdir(parents=True,exist_ok=True)
    if not output.exists(): output.write_bytes(payload)
    atomic_json(sidecar,dict(requests_identity=identity,source_sha256=file_sha256(output),sample_rate=sample_rate,samples=cursor,
                            chunks=records,boundaries=[r['sample_start']-round(sample_rate*.35/2) for r in records[1:]],approved=False))
    return output
