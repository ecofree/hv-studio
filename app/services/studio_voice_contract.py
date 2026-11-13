"""A source-bound, user-confirmed voice recipe. It does not authorize new calls."""
import json
from pathlib import Path
from app.services.provenance import file_sha256,fingerprint


def path_for(ep_id):
    from app import paths
    return paths.episode_dir(ep_id)/'studio/voice-contract.json'


def load(path,*,source_sha256=None,voice=None,rate=None):
    contract=json.loads(Path(path).read_text('utf-8'))
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
        if file_sha256(name)!=sha:raise ValueError('confirmed voice evidence changed: '+name)
    if not contract.get('confirmation_path'):raise ValueError('voice contract requires recorded user confirmation')
    if contract.get('confirmation_path'):
        review=json.loads(Path(contract['confirmation_path']).read_text('utf-8'))
        pilot=json.loads(Path(contract['pilot_receipt_path']).read_text('utf-8'))
        plan=json.loads(Path(contract['generation_plan_path']).read_text('utf-8'))
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


def bind(req,path):
    c=load(path)
    if c['version']==2 and not any(req['text'] in p['text'] for p in c['parts']):
        raise ValueError('voice request is not a contiguous passage of the bound new draft')
    for k in ('speaker','model','mode','instruction','engine_revision'):
        if req[k]!=c[k]:raise ValueError('confirmed voice '+k+' changed')
    if req.get('reference') is not None:raise ValueError('confirmed custom voice has no clone reference')
    return dict(req,voice_contract=dict(path=str(Path(path).resolve()),sha256=file_sha256(path)),
                generation=c['generation'],seed=c['seed'],source_sha256=c['source_sha256'])


def validate(req):
    binding=req.get('voice_contract')
    if not binding:return None
    if file_sha256(binding['path'])!=binding['sha256']:raise ValueError('voice contract changed')
    base={k:v for k,v in req.items() if k not in ('voice_contract','generation','seed','source_sha256')}
    if bind(base,binding['path'])!=req:raise ValueError('voice request differs from confirmed settings')
    return load(binding['path'])
