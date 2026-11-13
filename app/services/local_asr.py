"""Read-only audio recognition in an isolated worker; no provider fallback."""
import json
from pathlib import Path
import subprocess
from app.config.config import ROOT, get_settings
from app.services.provenance import atomic_json, fingerprint, file_sha256


def resolve(path):
    p=Path(path)
    return p if p.is_absolute() else ROOT/p


def runtime_versions(python,lock):
    expected=dict(line.strip().split('==',1) for line in lock.read_text(encoding='utf-8-sig').splitlines()
                  if line.strip() and not line.startswith('#'))
    code="import importlib.metadata as m,json,sys;print(json.dumps({n:m.version(n) for n in json.loads(sys.argv[1])}))"
    result=subprocess.run([str(python),'-c',code,json.dumps(list(expected))],capture_output=True,
                          text=True,encoding='utf-8',timeout=30,check=True,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    actual=json.loads(result.stdout)
    if actual!=expected:raise ValueError('local ASR environment differs from requirements.asr.lock.txt')
    return actual


def preflight(config=None):
    config=config or get_settings().get_profile().asr
    model=resolve(config.model_dir)
    model_manifest=json.loads((model/'model_manifest.json').read_text(encoding='utf-8'))
    if not {'model.bin','config.json','tokenizer.json','vocabulary.txt'}.issubset(model_manifest['files']):
        raise ValueError('local ASR model manifest is incomplete')
    from app.services.acoustic_session import current
    session = current()
    lock = ROOT/'requirements.asr.lock.txt'
    files = [model/'model_manifest.json',lock]+[model/n for n in model_manifest['files']]
    if any(Path(n).name != n for n in model_manifest['files']):
        raise ValueError('invalid model manifest path')
    signature = (str(model),str(resolve(config.python_executable)),
                 tuple((p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns) for p in files))
    if session and session.context and session.context[0] == signature:
        return session.context[1]
    if session: session.close_worker()
    for name,record in model_manifest['files'].items():
        if file_sha256(model/name)!=record['sha256']:
            raise ValueError('local ASR model file changed: '+name)
    result = model, runtime_versions(resolve(config.python_executable),lock)
    if session: session.context = (signature,result)
    return result


# Known transport-only migration: inference options and model output fields are unchanged.
OLD_ASR_WORKER = 'd4b66999dd2a3730ce226e1db47a249b9e3f4823ac33b4554a35b1e3748f3c14'
RESIDENT_ASR_WORKER = 'f8f2b8daff975db948990f1b097f257a3d85de5a32b8c2b5f18aaa5694348276'
OLD_ALIGNMENT_WORKER = '4aa0dfdd26b38c3db3e6b67e50d1af072ede49e90af547a762d3f2158f3c770b'
RESIDENT_ALIGNMENT_WORKER = 'a48e964feaafb0722a80538b27f1906724192cd51c482998fd5d6a1f30ddf629'
OLD_ALIGNMENT_ADAPTER = 'b2ff4bf0b84a28cdd08cabf6ea78afa28240d744cc347ddef0ef71b392d7bd62'


def recognize(audio, *, config=None, context=None):
    from app import paths
    config=config or get_settings().get_profile().asr
    model,runtime=context or preflight(config)
    worker=ROOT/'bin/local_asr_worker.py'
    runtime_lock=ROOT/'requirements.asr.lock.txt'
    request={'adapter':'local-asr-v1','audio_sha256':file_sha256(audio),
        'model_manifest_sha256':file_sha256(model/'model_manifest.json'),
        'worker_sha256':file_sha256(worker),'runtime_lock_sha256':file_sha256(runtime_lock),
        'options':{'language':'zh','beam_size':5,'temperature':0,'word_timestamps':True,
                   'vad_filter':True,'condition_on_previous_text':False,'device':'cpu','compute_type':'int8'},
        'threads':config.threads,'python':str(resolve(config.python_executable)),'runtime':runtime}
    from app.services import recognition_cache as C, model_resources as R
    key=fingerprint(request)
    cache=paths.state_dir()/'local_asr'/f'{key}.json'
    from app.services.acoustic_session import infer
    from app.services.production_metrics import count
    found=C.lookup(request,cache)
    if not found and request['worker_sha256']==RESIDENT_ASR_WORKER:
        prior=dict(request,worker_sha256=OLD_ASR_WORKER)
        found=C.lookup(prior,cache.parent/(fingerprint(prior)+'.json'))
    if found:
        count('asr_cache_hits')
        return found
    from app.services.acoustic_session import job_session
    with job_session() as session:
        session.acquire()
        found=C.lookup(request,cache)
        if found:
            count('asr_cache_hits')
            if not session.reusable:session.close_worker()
            return found
        result=infer(config,model,dict(operation='recognize',audio=str(Path(audio).resolve())),
                     R.timeout_for(audio,config.timeout_seconds))
        count('asr_inferences')
        if result['audio_sha256']!=request['audio_sha256'] or file_sha256(audio)!=request['audio_sha256']:
            raise ValueError('audio changed during local recognition')
        if any(runtime.get(k)!=v for k,v in result['runtime'].items()):
            raise ValueError('local ASR runtime changed during recognition')
        if not result.get('text','').strip():raise ValueError('local ASR returned no transcript')
        result['request_id']='local-'+key
        result['checker_version']='faster-whisper-'+result['runtime']['faster-whisper']+'-'+request['model_manifest_sha256'][:16]
        atomic_json(cache,{'request':request,'result':result,'result_sha256':fingerprint(result)})
        C.promote(cache)
        if not session.reusable:session.close_worker()
        return result,cache
