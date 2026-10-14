"""Read-only audio recognition in an isolated worker; no provider fallback."""
import json
from pathlib import Path
import subprocess
import tempfile
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
    for name,record in model_manifest['files'].items():
        if Path(name).name!=name:raise ValueError('invalid model manifest path')
        if file_sha256(model/name)!=record['sha256']:
            raise ValueError('local ASR model file changed: '+name)
    runtime_lock=ROOT/'requirements.asr.lock.txt'
    runtime=runtime_versions(resolve(config.python_executable),runtime_lock)
    return model,runtime


_BUST_HINTED = False


def _hint_cache_miss_reason(request):
    """One-line hint when perf params (not audio/model) busted the cache.

    `threads`/`python` are part of the cache key, so retuning them silently
    re-recognizes everything. Bounded scan (64 most-recent records), once per
    process, read-only. Never blocks or alters the miss path.
    """
    global _BUST_HINTED
    if _BUST_HINTED:
        return
    try:
        from app import paths
        cached = sorted(paths.state_dir().glob('local_asr/*.json'),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:64]
        for path in cached:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            other = record.get('request') or {}
            if (other.get('audio_sha256') == request.get('audio_sha256')
                    and other.get('model_manifest_sha256') == request.get('model_manifest_sha256')
                    and other.get('threads') != request.get('threads')):
                print(f"  [asr] 缓存未命中：同音频已有识别结果，但 threads "
                      f"{other.get('threads')}->{request.get('threads')} 改变了缓存键；"
                      f"改回即命中，无需重识别", flush=True)
                break
    except OSError:
        pass
    finally:
        _BUST_HINTED = True


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
    key=fingerprint(request)
    cache=paths.state_dir()/'local_asr'/f'{key}.json'
    if cache.exists():
        record=json.loads(cache.read_text(encoding='utf-8'))
        if record.get('request')==request and record.get('result_sha256')==fingerprint(record['result']):
            return record['result'],cache
    _hint_cache_miss_reason(request)
    with tempfile.TemporaryDirectory(prefix='hv-asr-') as tmp:
        output=Path(tmp)/'result.json'
        run=subprocess.run([str(resolve(config.python_executable)),str(worker),'--audio',str(Path(audio).resolve()),
            '--model',str(model.resolve()),'--output',str(output),'--threads',str(config.threads)],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=config.timeout_seconds,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if run.returncode:
            raise RuntimeError('local ASR failed: '+run.stderr[-1200:])
        result=json.loads(output.read_text(encoding='utf-8'))
    if result['audio_sha256']!=request['audio_sha256'] or file_sha256(audio)!=request['audio_sha256']:
        raise ValueError('audio changed during local recognition')
    if any(runtime.get(k)!=v for k,v in result['runtime'].items()):
        raise ValueError('local ASR runtime changed during recognition')
    if not result.get('text','').strip():raise ValueError('local ASR returned no transcript')
    result['request_id']='local-'+key
    result['checker_version']='faster-whisper-'+result['runtime']['faster-whisper']+'-'+request['model_manifest_sha256'][:16]
    atomic_json(cache,{'request':request,'result':result,'result_sha256':fingerprint(result)})
    return result,cache
