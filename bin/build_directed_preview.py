"""Build an isolated prefix preview using existing media and the v2 renderer.

No providers, episode writes, source rewrites, or human approval records.
"""
import argparse
import copy
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def prepare(episode, destination, seconds):
    from app.paths import validate_episode_id
    from app.services.audio_disposition import require_usable
    from app.services.external_narration import validate_external
    validate_episode_id(episode)
    out = Path(destination).resolve()
    if out.is_relative_to(ROOT / 'episodes'):
        raise ValueError('preview must live outside production episodes')
    if not 60 <= seconds <= 90:
        raise ValueError('preview duration must be 60-90 seconds')
    state = read(ROOT / 'episodes' / episode / 'production/v2/run.json')
    source = validate_assembly(state['manifest_path'])
    require_usable(source)
    sr = source['sample_rate']; count = round(seconds * sr)
    if count > source['samples']:
        raise ValueError('preview exceeds source duration')
    if any(u['sample_start'] < count < u['sample_end'] for u in source['alignment']):
        raise ValueError('preview end must fall between complete narration units')
    out.mkdir(parents=True, exist_ok=True)
    units = [u for u in source['alignment'] if u['sample_end'] <= count]
    ids = {u['unit_id'] for u in units}
    original_plan = read(source['plan_path'])
    plan = copy.deepcopy(original_plan)
    plan.update(units=[u for u in plan['units'] if u['unit_id'] in ids], status='preview_draft',
                delivery_cues=[c for c in plan.get('delivery_cues', []) if c.get('unit_id') in ids],
                excerpt={'source_plan': source['plan_path'], 'sha256': file_sha256(source['plan_path']),
                         'sample_start': 0, 'sample_end': count})
    atomic_json(out / 'plan.json', plan)
    master = out / 'narration.wav'
    with wave.open(source['master_path'], 'rb') as inp:
        data = inp.readframes(count)
        if len(data) != count * 2:
            raise ValueError('source prefix sample count mismatch')
        with wave.open(str(master), 'wb') as dest:
            dest.setparams((1, 2, sr, 0, 'NONE', 'not compressed')); dest.writeframes(data)
    manifest = copy.deepcopy(source)
    manifest.update(plan_path=str((out/'plan.json').resolve()), plan_sha256=file_sha256(out/'plan.json'),
                    master_path=str(master), master_sha256=file_sha256(master), samples=count,
                    alignment=units, takes=[t for t in source['takes'] if t['unit_id'] in ids],
                    listening_review=None, alignment_review=None, approved=False,
                    base_manifest_path=state['manifest_path'], base_manifest_sha256=file_sha256(state['manifest_path']))
    manifest['source_recording'] = dict(path=str(master), sha256=file_sha256(master),
        delivered_path=source['master_path'], delivered_sha256=source['master_sha256'],
        operation='exact_pcm_prefix', source_sample_start=0, source_sample_end=count,
        source_samples=source['samples'], sample_rate=sr, total_samples=count)
    atomic_json(out/'manifest.json', manifest)
    validate_assembly(out/'manifest.json'); validate_external(manifest)
    atomic_json(out/'source.json', {'episode': episode, 'run': state, 'seconds': seconds,
                                 'source_manifest_sha256': file_sha256(state['manifest_path'])})
    print(f'Prepared {len(units)} complete source units; {count} original samples', flush=True)


def align(destination):
    from app.services.forced_alignment import build
    out=Path(destination).resolve()
    path=build(out/'manifest.json', out/'alignment')
    atomic_json(out/'alignment-pointer.json', {'path':str(path.resolve())})
    print('Alignment verified against current implementation',flush=True)


def render(destination):
    from app.services.directing import apply
    from app.services.production_v2 import validate_timeline, render as render_v2
    out=Path(destination).resolve(); manifest=validate_assembly(out/'manifest.json')
    info=read(out/'source.json'); source_timeline=read(info['run']['timeline_path'])
    evidence_path=read(out/'alignment-pointer.json')['path']; evidence=read(evidence_path)
    timeline=copy.deepcopy(source_timeline)
    timeline['shots']=[dict(s,sample_end=min(s['sample_end'],manifest['samples']))
                       for s in timeline['shots'] if s['sample_start'] < manifest['samples']]
    timeline.update(subtitles=evidence['subtitles'],alignment_evidence_path=evidence_path,
                    inputs={info['run']['timeline_path']:file_sha256(info['run']['timeline_path'])})
    timeline.pop('quality_report_path',None)
    images={s['image_id']:s['image_path'] for s in source_timeline['shots']}
    timeline=apply(manifest,timeline,out/'directing.json',images,out/'compiled')
    validate_timeline(manifest,timeline); atomic_json(out/'timeline.json',timeline)
    receipt=render_v2(out/'manifest.json',out/'timeline.json',out/'renders',timeout=900)
    atomic_json(out/'result.json',{'receipt_path':str(receipt),'status':'candidate','approved':False})
    print('DONE '+str(receipt),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('stage',choices=['prepare','align','render'])
    ap.add_argument('--episode',default='xlc-01')
    ap.add_argument('--destination',required=True)
    ap.add_argument('--seconds',type=float,default=84.)
    args=ap.parse_args()
    if args.stage=='prepare': prepare(args.episode,args.destination,args.seconds)
    elif args.stage=='align': align(args.destination)
    else: render(args.destination)
