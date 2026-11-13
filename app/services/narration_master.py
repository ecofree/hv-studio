"""Explicit, hash-bound selection of repaired external narration for production."""
from pathlib import Path
import json
import shutil
import numpy as np
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_leveling import load, apply_curve


def verify_repair(path):
    r=json.loads(Path(path).read_text(encoding='utf-8'))
    for key in ('source','output'):
        if file_sha256(r[key+'_path'])!=r[key+'_sha256']:
            raise ValueError('repair '+key+' fingerprint changed')
    x,sr=load(r['source_path']);y,ysr=load(r['output_path'])
    if sr!=ysr or sr!=r['sample_rate'] or len(x)!=len(y) or len(y)!=r['samples']:
        raise ValueError('repair changed sample clock')
    if r['method']!='positive_scalar_gain_only_no_resynthesis_no_resampling':
        raise ValueError('unsupported timing preservation method')
    times=np.asarray(r['curve_samples'],dtype=float);gain=np.asarray(r['curve_db'],dtype=float)
    if len(times)!=len(gain) or not len(times) or not np.isfinite(gain).all() or not np.isfinite(times).all():
        raise ValueError('invalid gain evidence')
    if np.any(np.diff(times)<=0) or times[0]<0 or times[-1]>=len(x):raise ValueError('invalid gain clock')
    expected=apply_curve(x,times,gain,r['global_db'])
    # Serialized gains are rounded to six decimals, giving <=1 LSB rounding error.
    if np.max(np.abs(expected.astype(np.int32)-y.astype(np.int32)))>1:
        raise ValueError('audio is not the declared gain-only transform')
    if np.any(y[x==0]!=0):raise ValueError('source silence changed')
    return r


def select(root,plan_path,repair_path):
    root,plan_path,repair_path=Path(root),Path(plan_path),Path(repair_path)
    r=verify_repair(repair_path);plan=json.loads(plan_path.read_text(encoding='utf-8'))
    identity=fingerprint([r['output_sha256'],file_sha256(plan_path)])[:24]
    dest=root/'narration-revisions'/identity;dest.mkdir(parents=True,exist_ok=True)
    master=dest/'master.wav';receipt=dest/'repair.json'
    for src,target in [(Path(r['output_path']),master),(repair_path,receipt)]:
        if target.exists():
            if file_sha256(src)!=file_sha256(target):raise ValueError('immutable narration revision collision')
        else:shutil.copy2(src,target)
    value=dict(version=1,plan_sha256=file_sha256(plan_path),source_sha256=plan['source_sha256'],
        master_path=str(master.resolve()),master_sha256=file_sha256(master),samples=r['samples'],
        sample_rate=r['sample_rate'],repair_path=str(receipt.resolve()),repair_sha256=file_sha256(receipt),
        original_master_path=r['source_path'],original_master_sha256=r['source_sha256'],
        sample_clock='unchanged',listening_review='pending',approved=False)
    atomic_json(root/'external-master.json',value)
    return value


def resolve(root,plan_path):
    pointer=Path(root)/'external-master.json'
    if not pointer.exists():return None
    value=json.loads(pointer.read_text(encoding='utf-8'))
    if value.get('version')!=1 or value['plan_sha256']!=file_sha256(plan_path):
        raise ValueError('selected narration is bound to another plan')
    if file_sha256(value['master_path'])!=value['master_sha256']:
        raise ValueError('selected narration changed')
    if file_sha256(value['repair_path'])!=value['repair_sha256']:
        raise ValueError('selected narration repair evidence changed')
    r=verify_repair(value['repair_path'])
    if r['output_sha256']!=value['master_sha256']:raise ValueError('selected narration differs from repaired audio')
    return value['master_path']
