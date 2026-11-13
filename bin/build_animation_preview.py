"""Apply authored animation to an existing complete timeline, then render via v2.

The design binds the current baseline, narration and source timeline by hash.
Only animation fields are added; subtitle, audio, camera, cuts and cards remain
the baseline's. No episode writes, network calls or approval records.
"""
import argparse
import copy
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['PATH']=str(ROOT/'tools/ffmpeg/bin')+os.pathsep+os.environ.get('PATH','')

from app.services.provenance import atomic_json,file_sha256
from app.services.narration_pipeline import read,validate_assembly
from app.services.production_v2 import validate_timeline,frame_boundary,render
from app.services.scene_animation import validate as validate_animation,Scene


def prepare(design_path,destination):
    from PIL import Image
    from app.services.forced_alignment import require_evidence
    design=read(design_path);out=Path(destination).resolve()
    if out.is_relative_to(ROOT/'episodes'):
        raise ValueError('preview must be isolated from production episodes')
    if design.get('version')!=1:
        raise ValueError('unsupported animation design')
    for item in design['sources'].values():
        if file_sha256(item['path'])!=item['sha256']:
            raise ValueError('animation baseline changed')
    manifest_path=design['sources']['manifest']['path']
    source_path=design['sources']['timeline']['path']
    manifest=validate_assembly(manifest_path);original=read(source_path)
    validate_timeline(manifest,original);require_evidence(manifest,original)
    timeline=copy.deepcopy(original)
    plans=design['shots'];identifiers=[row['shot_id'] for row in plans]
    if len(set(identifiers))!=len(identifiers) or set(identifiers)!={s['id'] for s in original['shots']}:
        raise ValueError('animation design must cover each existing shot exactly once')
    plans={row['shot_id']:row for row in plans}
    for shot in timeline['shots']:
        entry=plans[shot['id']]
        if entry['image_id']!=shot['image_id']:
            raise ValueError('animation design changes the source image')
        animation=validate_animation(entry['animation'],file_sha256(shot['image_path']))
        first=frame_boundary(shot['sample_start'],manifest['sample_rate'])
        last=frame_boundary(shot['sample_end'],manifest['sample_rate'])
        with Image.open(shot['image_path']) as opened:
            scene=Scene(opened.convert('RGB'),animation,first,last)
            # Detect invalid source regions before beginning the full render.
            scene.frame(first).close();scene.frame(last-1).close()
        shot['animation']=animation
    stripped=copy.deepcopy(timeline)
    for shot in stripped['shots']:shot.pop('animation',None)
    if stripped!=original:
        raise ValueError('animation compilation changed the baseline timeline')
    inputs=timeline.setdefault('inputs',{})
    for item in design['sources'].values():inputs[str(Path(item['path']).resolve())]=item['sha256']
    inputs[str(Path(design_path).resolve())]=file_sha256(design_path)
    inputs[str(Path(__file__).resolve())]=file_sha256(__file__)
    out.mkdir(parents=True,exist_ok=True)
    validate_timeline(manifest,timeline)
    atomic_json(out/'timeline.json',timeline)
    print(f'Prepared {len(timeline["shots"])} source-preserving animated shots',flush=True)
    return manifest_path,out/'timeline.json'


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--design',required=True);ap.add_argument('--output',required=True)
    args=ap.parse_args();manifest,timeline=prepare(args.design,args.output)
    receipt=render(manifest,timeline,Path(args.output)/'renders',timeout=900)
    atomic_json(Path(args.output)/'result.json',{'receipt_path':str(receipt),'status':'candidate','approved':False})
    print('DONE '+str(receipt),flush=True)
