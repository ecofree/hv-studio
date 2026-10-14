"""Audio-first v2 pipeline. All generation is explicit, bounded and immutable.

The existing episode CLI exposes this module through `narration`. v1 episodes are
not migrated by opening them. Approval records identify exact bytes and inputs.
"""
from __future__ import annotations

import json
import shutil
import uuid
import wave
from pathlib import Path

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.script_document import parse_document
from app.services.audio_disposition import take_rejected, rejection, require_usable

VERSION = "narration-v2.0"
SAMPLE_RATE = 24000


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare(source, destination, max_units=None):
    source, destination = Path(source), Path(destination)
    document = parse_document(source.read_text(encoding="utf-8"), str(source.resolve()))
    units = document["units"][:max_units] if max_units else document["units"]
    plan = {"schema_version":"2.0", "source_sha256":document["source_sha256"],
            "units":units, "rewrites":[], "delivery_cues":document["delivery_cues"],
            "source_path":str(source.resolve()), "status":"prepared"}
    plan["plan_id"] = fingerprint(plan)
    target = destination / plan["plan_id"]
    atomic_json(target/"document.json",document)
    atomic_json(target/"plan.json",plan)
    return target/"plan.json"


def import_voice(cache_path, destination):
    cache=read(cache_path)
    asset={"schema_version":"2.0", "voice_id":cache["voice_id"],"model":cache["model"],
           "design_prompt":cache.get("voice_prompt"),"source_cache_sha256":file_sha256(cache_path),
           "preview_text":cache.get("preview_text"),"preview_audio_sha256":None,
           "status":"unverified", "capabilities":{k:{"status":"unverified","request_id":None}
           for k in ("instruction","ssml","hot_fix","timestamp","wav")}}
    asset["version"]=fingerprint(asset)
    atomic_json(destination,asset)
    return asset


def compile_request(unit, asset, *, pitch=None, rate=None, volume=None):
    controls=unit.get("voice_parameters",{})
    pitch=controls.get("pitch_rate",1.0) if pitch is None else pitch
    rate=controls.get("speech_rate",1.0) if rate is None else rate
    volume=controls.get("volume",50) if volume is None else volume
    # The adapter starts from approved source; legacy narration_text is never read.
    if unit["approved_spoken_text"] != unit["source_text"]:
        raise ValueError("spoken rewrite needs a separately reviewed source revision")
    instruction=unit.get("delivery",{}).get("instruction", "")
    if sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in instruction)>100:
        raise ValueError("instruction exceeds 100 counting characters; edit rather than truncate")
    if instruction and asset["capabilities"]["instruction"]["status"]!="supported":
        raise ValueError("instruction capability is unverified for this VoiceAsset")
    if not (0.5<=pitch<=2 and 0.5<=rate<=2 and 0<=volume<=100):
        raise ValueError("invalid voice parameters")
    pronunciation=unit.get("pronunciation",{})
    if pronunciation and asset["capabilities"].get("hot_fix",{}).get("status")!="supported":
        raise ValueError("pronunciation hot_fix capability is unverified for this VoiceAsset")
    if pronunciation.get("replace"):
        raise ValueError("surface text replacement requires a separately reviewed source revision")
    from app.services.spoken_text import compile_text
    text,transform=compile_text(unit["approved_spoken_text"])
    request={"adapter":VERSION,"model":asset["model"],"voice_id":asset["voice_id"],
            "voice_asset_version":asset["version"],"text":text,
            "instruction":instruction or None,"pitch_rate":pitch,"speech_rate":rate,
            "volume":volume,"format":"wav","sample_rate":SAMPLE_RATE,
            "pronunciation":pronunciation,"seed":unit.get("seed",0)}
    if transform['changes']:
        request.update(source_text=unit['approved_spoken_text'],text_transform=transform)
    return request


def wave_info(path):
    with wave.open(str(path),"rb") as wav:
        if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getcomptype()!="NONE":
            raise ValueError("requires mono PCM16 WAV")
        if wav.getframerate()!=SAMPLE_RATE or wav.getnframes()<=0:
            raise ValueError("invalid sample rate or empty WAV")
        n=wav.getnframes()
        data=wav.readframes(n)
        if len(data)!=n*2: raise ValueError("truncated WAV")
    return n,data


def canonicalize_provider_wav(raw, effective):
    """Keep provider bytes, finalize only the known streaming RIFF size placeholders.

    Completion comes from SDK task-finished, not from a guessed file duration.
    Unknown truncated headers remain a hard failure.
    """
    import struct
    raw=Path(raw)
    content=raw.read_bytes()
    with wave.open(str(raw),"rb") as w:
        declared=w.getnframes()
        data=w.readframes(declared)
        channels,width,sr=w.getnchannels(),w.getsampwidth(),w.getframerate()
    if (channels,width,sr)!=(1,2,SAMPLE_RATE): raise ValueError("provider WAV format mismatch")
    if len(data)!=declared*2:
        if effective.get("status")!="succeeded" or len(content)<44 or content[:4]!=b"RIFF" or content[36:40]!=b"data" or struct.unpack_from('<I',content,4)[0]!=0x7fffffbf or struct.unpack_from('<I',content,40)[0]!=0x7fffff9b:
            raise ValueError("truncated WAV without verified streaming-header provenance")
        if not data or len(data)%2: raise ValueError("partial PCM sample")
    canonical=raw.with_name("audio.wav")
    with wave.open(str(canonical),"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(SAMPLE_RATE);out.writeframes(data)
    if wave_info(canonical)[1]!=data: raise ValueError("PCM changed while finalizing WAV")
    return canonical


def _provider(request, raw):
    from app.services.cosyvoice import DashScopeCosyVoiceSynthesizer
    svc=DashScopeCosyVoiceSynthesizer(model=request["model"],voice=request["voice_id"],
        sample_rate=request["sample_rate"],response_format="wav",
        pitch_rate=request["pitch_rate"],speech_rate=request["speech_rate"],
        volume=request["volume"],instruction=request["instruction"],
        hot_fix=request["pronunciation"] or None,seed=request["seed"])
    svc.voice_asset_version=request["voice_asset_version"]
    svc.synthesize(request["text"],raw)
    return read(svc.last_request_path)


def generation_quote(plan_path, asset_path):
    """Check every missing actual request before a multi-stage paid run."""
    from app.services.delivery import effective_unit
    plan, asset = read(plan_path), read(asset_path)
    base = Path(plan_path).parent
    document = read(base/'document.json')
    if fingerprint(document['source']) != plan['source_sha256']: raise ValueError('plan/source mismatch')
    source_units = {u['unit_id']:u for u in document['units']}
    for unit in plan['units']:
        if unit['unit_id'] not in source_units or any(unit[k]!=source_units[unit['unit_id']][k] for k in ('source_text','source_map','approved_spoken_text','display_text')):
            raise ValueError('plan text or mapping differs from source document')
    pending = []
    for unit in plan['units']:
        request = compile_request(effective_unit(unit, plan, asset), asset); key = fingerprint(request)
        cached = base/'cache'/(key+'.json')
        if cached.exists():
            take = read(cached)
            if not take_rejected(take) and all(Path(take[k+'_path']).is_file() and file_sha256(take[k+'_path'])==take[k+'_sha256'] for k in ('raw','audio')):
                continue
        reused = False
        for folder in (base/'takes').glob(unit['unit_id']+'-*'):
            if not all((folder/name).is_file() for name in ('effective_request.json','compiled_request.json','raw.wav')): continue
            effective = read(folder/'effective_request.json')
            if effective.get('status')=='succeeded' and not rejection(effective.get('audio_sha256')) and file_sha256(folder/'raw.wav')==effective.get('audio_sha256') and fingerprint(read(folder/'compiled_request.json'))==key:
                reused = True; break
        if not reused: pending.append(request)
    return dict(requests=len(pending), characters=sum(len(r['text']) for r in pending))


def _synthesize(plan_path, asset_path, *, max_requests, max_characters, force=False,
               provider=None, checker=None, budget=None):
    from app.services.asr_check import check
    plan,asset=read(plan_path),read(asset_path)
    document=read(Path(plan_path).with_name("document.json"))
    if fingerprint(document["source"])!=plan["source_sha256"]:
        raise ValueError("plan/source mismatch")
    actual={u["unit_id"]:u for u in document["units"]}
    for u in plan["units"]:
        if u["unit_id"] not in actual or any(u[k]!=actual[u["unit_id"]][k] for k in ("source_text","source_map","approved_spoken_text","display_text")):
            raise ValueError("plan text or mapping differs from source document")
    from app.services.delivery import effective_unit
    requests=[compile_request(effective_unit(u,plan,asset),asset) for u in plan["units"]]
    # Refuse before the first paid request, never truncate source to fit a budget.
    base=Path(plan_path).parent
    def needs_provider(unit,request):
        if force:return True
        key=fingerprint(request)
        cache=base/"cache"/(key+".json")
        if cache.exists():
            saved=read(cache)
            if not take_rejected(saved) and Path(saved["raw_path"]).exists() and Path(saved["audio_path"]).exists() and file_sha256(saved["raw_path"])==saved.get("raw_sha256") and file_sha256(saved["audio_path"])==saved.get("audio_sha256"):
                return False
        for previous in (base/"takes").glob(unit["unit_id"]+"-*"):
            if all((previous/name).exists() for name in ("effective_request.json","compiled_request.json","raw.wav")):
                effective=read(previous/"effective_request.json")
                if not rejection(effective.get('audio_sha256')) and fingerprint(read(previous/"compiled_request.json"))==key and effective.get("status")=="succeeded" and file_sha256(previous/"raw.wav")==effective.get("audio_sha256"):
                    return False
        return True
    pending=[r for u,r in zip(plan["units"],requests) if needs_provider(u,r)]
    if len(pending)>max_requests or sum(len(r["text"]) for r in pending)>max_characters:
        raise ValueError("requested sample exceeds explicit request/character budget")
    selected=[]
    for unit,request in zip(plan["units"],requests):
        key=fingerprint(request)
        cache=base/"cache"/(key+".json")
        take=None
        if cache.exists() and not force:
            saved=read(cache)
            raw=Path(saved["raw_path"])
            if not take_rejected(saved) and raw.exists() and file_sha256(raw)==saved.get("raw_sha256") and Path(saved["audio_path"]).exists() and file_sha256(saved["audio_path"])==saved["audio_sha256"]:
                take=saved
        cached_take=take is not None
        if take is None:
            # Resume postprocessing of a completed provider response, without re-synthesis.
            take_dir=None
            if not force:
                for previous in sorted((base/"takes").glob(unit["unit_id"]+"-*")):
                    if (previous/"effective_request.json").exists() and (previous/"raw.wav").exists() and (previous/"compiled_request.json").exists():
                        effective=read(previous/"effective_request.json")
                        if not rejection(effective.get('audio_sha256')) and fingerprint(read(previous/"compiled_request.json"))==key and effective.get("status")=="succeeded" and file_sha256(previous/"raw.wav")==effective.get("audio_sha256"):
                            take_dir=previous
                            break
            reused_provider=take_dir is not None
            if take_dir is None:
                take_dir=base/"takes"/(unit["unit_id"]+"-"+uuid.uuid4().hex)
                take_dir.mkdir(parents=True)
            raw=take_dir/"raw.wav"
            atomic_json(take_dir/"compiled_request.json",request)
            atomic_json(take_dir/"attempt.json",{"status":"started","characters":len(request["text"]),"request_fp":key})
            try:
                if reused_provider:
                    effective=read(take_dir/"effective_request.json")
                else:
                    # Real SDK attempts charge at the transport boundary. Injected
                    # offline adapters retain the same ledger contract for tests.
                    reservation = budget.reserve('tts', key, len(request['text'])) if budget and provider else None
                    try:
                        from app.services.provider_budget import tts_budget_scope
                        with tts_budget_scope(budget):
                            effective=(provider or _provider)(request,raw)
                    except BaseException:
                        if reservation: budget.finish(reservation, False)
                        raise
                    if reservation: budget.finish(reservation, True)
                atomic_json(take_dir/"effective_request.json",effective or {})
                audio=canonicalize_provider_wav(raw,effective or {})
                if rejection(file_sha256(audio)) or rejection(file_sha256(raw)):
                    raise ValueError('provider returned previously rejected audio')
                samples,_=wave_info(audio)
                expected=unit['approved_spoken_text']
                quality=(checker or check)(audio,expected)
                quality=quality.to_dict() if hasattr(quality,"to_dict") else quality
                # Never accept a stale/misbound result from a checker adapter.
                if quality.get("audio_sha256")!=file_sha256(audio) or quality.get("expected_sha256")!=fingerprint(expected):
                    raise ValueError("checker result is not bound to this audio and transcript")
                atomic_json(take_dir/"quality.json",quality)
                take={"unit_id":unit["unit_id"],"request_fp":key,"raw_path":str(raw.resolve()),
                      "audio_sha256":file_sha256(audio),"audio_path":str(audio.resolve()),
                      "raw_sha256":file_sha256(raw),"samples":samples,"quality":quality,
                      "voice_asset_version":asset["version"],"listening_review":"pending"}
                atomic_json(take_dir/"take.json",take)
                atomic_json(cache,take)
                atomic_json(take_dir/"attempt.json",{"status":"succeeded","characters":len(request["text"]),"request_fp":key})
            except Exception as exc:
                atomic_json(take_dir/"attempt.json",{"status":"failed","error_type":type(exc).__name__,
                            "characters":len(request["text"]),"request_fp":key})
                raise
        if cached_take and checker is None:
            from app.config.config import get_settings
            if get_settings().get_profile().asr.backend=='local-whisper':
                # New receipt and assembly reference; old immutable QA is retained.
                quality=check(Path(take['audio_path']),unit['approved_spoken_text']).to_dict()
                if quality.get('audio_sha256')!=take['audio_sha256'] or quality.get('expected_sha256')!=fingerprint(unit['approved_spoken_text']):
                    raise ValueError('cached audio recheck binding mismatch')
                from app import paths
                receipt=paths.state_dir()/'audio_quality'/(fingerprint(quality)+'.json')
                atomic_json(receipt,quality)
                take=dict(take,quality=quality,quality_receipt_path=str(receipt.resolve()))
        selected.append(take)
    require_usable({'takes':selected})
    # A new assembly references old immutable takes. --force never deletes them.
    assembly=base/"assemblies"/uuid.uuid4().hex
    assembly.mkdir(parents=True)
    master=assembly/"master.wav"
    alignment=[]
    cursor=0
    with wave.open(str(master),"wb") as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(SAMPLE_RATE)
        for u,take in zip(plan["units"],selected):
            if file_sha256(take["raw_path"])!=take["raw_sha256"] or file_sha256(take["audio_path"])!=take["audio_sha256"]: raise ValueError("take changed")
            samples,data=wave_info(take["audio_path"])
            out.writeframes(data)
            alignment.append({"unit_id":u["unit_id"],"sample_start":cursor,"sample_end":cursor+samples,
                              "text":u["display_text"],"source_map":u["source_map"],
                              "source":"whole_utterance_bounds", "word_alignment":"unavailable",
                              "audio_sha256":take["audio_sha256"]})
            cursor+=samples
    manifest={"schema_version":"2.0","plan_sha256":file_sha256(plan_path),"plan_path":str(Path(plan_path).resolve()),
              "master_path":str(master.resolve()),"master_sha256":file_sha256(master),"samples":cursor,
              "sample_rate":SAMPLE_RATE,"takes":selected,"alignment":alignment,
              "technical_quality":"passed" if all(t["quality"]["status"]=="passed" for t in selected) else "pending",
              "listening_review":"pending", "approved":False}
    atomic_json(assembly/"manifest.json",manifest)
    return assembly/"manifest.json"


def validate_assembly(manifest_path):
    manifest=read(manifest_path)
    if file_sha256(manifest["master_path"])!=manifest["master_sha256"]:
        raise ValueError("master audio changed after assembly")
    if file_sha256(manifest["plan_path"])!=manifest["plan_sha256"]:
        raise ValueError("plan changed after synthesis")
    samples,_=wave_info(manifest["master_path"])
    if samples!=manifest["samples"]: raise ValueError("master sample count mismatch")
    for take in manifest["takes"]:
        if file_sha256(take["raw_path"])!=take["raw_sha256"] or file_sha256(take["audio_path"])!=take["audio_sha256"]: raise ValueError("raw audio changed")
    if manifest.get('base_manifest_path'):
        if file_sha256(manifest['base_manifest_path']) != manifest['base_manifest_sha256']:
            raise ValueError('base narration manifest changed')
        if any(file_sha256(p)!=sha for p,sha in manifest.get('delivery_evidence',{}).items()):
            raise ValueError('pause alignment evidence changed')
    return manifest


def record_listening_review(manifest_path, reviewer, findings, decision):
    if not reviewer.strip() or not findings.strip() or decision not in ("approved","rejected"):
        raise ValueError("review requires reviewer, findings and explicit decision")
    manifest=validate_assembly(manifest_path)
    if decision=='approved':require_usable(manifest)
    record={"manifest_sha256":file_sha256(manifest_path),"master_sha256":manifest["master_sha256"],
            "reviewer":reviewer,"findings":findings,"decision":decision,
            "scope":"transcript, pronunciation, naturalness, continuity, emotion, pauses"}
    from datetime import datetime, timezone
    record["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    target=Path(manifest_path).parent/"reviews"/(uuid.uuid4().hex+".json")
    atomic_json(target,record)
    if decision=='rejected':
        from app.services.audio_disposition import reject_take
        for take in manifest['takes']:
            reject_take(take,reviewer,findings,evidence_path=target)
    return target


def synthesize(plan_path, asset_path, *, max_requests, max_characters, force=False, provider=None, checker=None, budget=None):
    from app.services.process_lock import file_lock
    if budget is None and provider is None:
        from app.services.provider_budget import for_run
        budget = for_run(Path(plan_path).parent, ['narration',file_sha256(plan_path),file_sha256(asset_path)],
                         dict(max_requests=max_requests,max_characters=max_characters))
    with file_lock(Path(plan_path).parent/".synthesize.lock"):
        return _synthesize(plan_path,asset_path,max_requests=max_requests,max_characters=max_characters,
                           force=force,provider=provider,checker=checker,budget=budget)


def revise_delivery(plan_path, unit_id, rate):
    """New plan revision, preserving old approvals and reusing unrelated audio takes."""
    if not 0.5<=rate<=2:raise ValueError("invalid speech rate")
    original=Path(plan_path)
    plan=read(original)
    unit=next((u for u in plan["units"] if u["unit_id"]==unit_id),None)
    if unit is None:raise ValueError("unknown narration unit")
    unit.setdefault("voice_parameters",{})["speech_rate"]=rate
    plan.pop("plan_id",None)
    plan["parent_plan_sha256"]=file_sha256(original)
    plan["plan_id"]=fingerprint(plan)
    target=original.parent.parent/plan["plan_id"]
    target.mkdir(parents=True,exist_ok=True)
    atomic_json(target/"plan.json",plan)
    shutil.copy2(original.with_name("document.json"),target/"document.json")
    if (original.parent/"cache").exists():
        shutil.copytree(original.parent/"cache",target/"cache",dirs_exist_ok=True)
    return target/"plan.json"
