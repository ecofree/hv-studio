"""A source-bound, user-confirmed voice recipe. It does not authorize new calls."""
import json
from pathlib import Path
from app.services.provenance import file_sha256,fingerprint
from app.services.qwen_voice import local_path, read


def path_for(ep_id):
    from app import paths
    return paths.episode_dir(ep_id)/'studio/voice-contract.json'


def load(path,*,source_sha256=None,voice=None,rate=None):
    contract=read(path)
    if contract.get('version')==2:
        from app.services.series_voice import load_binding
        contract=load_binding(path)
        if source_sha256 is not None and source_sha256!=contract['source_sha256']:raise ValueError('voice contract source changed')
        if voice is not None and (voice['identity'],voice['model'])!=(contract['speaker'],contract['model']):
            raise ValueError('confirmed voice identity/model changed')
        if rate is not None and rate!=contract['target_rate']:raise ValueError('confirmed playback rate changed')
        return contract
    if contract.get('version')!=1:raise ValueError('unknown voice contract')
    for name,sha in contract['evidence'].items():
        if file_sha256(local_path(name))!=sha:raise ValueError('confirmed voice evidence changed: '+name)
    if not contract.get('confirmation_path'):raise ValueError('voice contract requires recorded user confirmation')
    if contract.get('confirmation_path'):
        review=read(contract['confirmation_path'])
        pilot=read(contract['pilot_receipt_path'])
        plan=read(contract['generation_plan_path'])
        for key in ('confirmation_path','pilot_receipt_path','generation_plan_path','engine_manifest_path','worker_path'):
            if contract[key] not in contract['evidence']:raise ValueError('unbound voice confirmation evidence')
        if review.get('decision')!='accepted_for_use' or review['selected_variant']!=contract['selected_variant']:
            raise ValueError('voice scheme lacks the recorded user confirmation')
        for key in ('speaker','model','mode','instruction','engine_revision'):
            if contract[key]!=pilot['request'][key]:raise ValueError('voice contract differs from confirmed pilot: '+key)
        if any(contract[k]!=plan[k] for k in ('generation','seed','source_sha256')):
            raise ValueError('voice contract differs from recorded generation plan')
        if contract['source_sha256']!=review['source_sha256'] or contract['target_rate']!=review['target_rate']:
            raise ValueError('voice contract source/rate differs from confirmation')
        if not set(contract['request_paths']).issubset(contract['evidence']):raise ValueError('unbound voice request')
    if source_sha256 is not None and source_sha256!=contract['source_sha256']:raise ValueError('voice contract source changed')
    if voice is not None and (voice['identity'],voice['model'])!=(contract['speaker'],contract['model']):
        raise ValueError('confirmed voice identity/model changed')
    if rate is not None and rate!=contract['target_rate']:raise ValueError('confirmed playback rate changed')
    return contract


def bind(req,path,*,_legacy=False,_policy=None):
    from app.services import qwen_voice as Q
    c=load(path)
    if c['version']==2 and not any(req['text'] in p['text'] for p in c['parts']):
        raise ValueError('voice request is not a contiguous passage of the bound new draft')
    for k in ('speaker','model','mode','instruction','engine_revision'):
        permitted=(c[k],)
        if k=='instruction' and not _legacy:permitted+=(Q.effective(c)[k],)
        if req[k] not in permitted:raise ValueError('confirmed voice '+k+' changed')
    if req.get('reference') is not None:raise ValueError('confirmed custom voice has no clone reference')
    result=dict(req,voice_contract=dict(path=str(local_path(path)),sha256=file_sha256(local_path(path))),
                generation=c['generation'],seed=c['seed'],source_sha256=c['source_sha256'])
    if _legacy:return result
    if c['mode']!='custom' or not c['model'].startswith('Qwen3-TTS-') or not c['model'].endswith('-CustomVoice'):
        raise ValueError('此入口只执行已选 Qwen CustomVoice，不替换其他音色')
    if req.get('language')!='chinese':raise ValueError('confirmed Qwen language changed')
    result=Q.bind_request(result,c)
    if _policy is not None:
        # Historical current-policy receipts retain their exact implementation
        # fingerprints. Dispatch separately requires the installed policy.
        import re
        expected=Q.policy()
        if set(_policy)!=set(expected) or any(_policy[k]!=v for k,v in expected.items()
                if k not in ('worker_sha256','policy_sha256')):
            raise ValueError('unknown Qwen execution policy')
        if any(not re.fullmatch('[0-9a-f]{64}',str(_policy[k])) for k in ('worker_sha256','policy_sha256')):
            raise ValueError('invalid Qwen implementation fingerprint')
        result['qwen_policy']=_policy
    return result


def validate(req):
    binding=req.get('voice_contract')
    if not binding:return None
    if file_sha256(local_path(binding['path']))!=binding['sha256']:raise ValueError('voice contract changed')
    base={k:v for k,v in req.items() if k not in ('voice_contract','generation','seed','source_sha256','series_seed','qwen_policy')}
    expected=bind(base,binding['path'],_legacy='qwen_policy' not in req,_policy=req.get('qwen_policy'))
    # Stored path spelling is provenance; an old alias can resolve locally.
    expected['voice_contract']=dict(expected['voice_contract'],path=binding['path'])
    if expected!=req:raise ValueError('voice request differs from confirmed settings')
    return load(binding['path'])
