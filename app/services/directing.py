"""Compile opt-in direction against the existing source and acoustic clock.

No narration rewrite, episode save, provider call or alternate timing authority.
The compiled timeline is still validated and rendered by production_v2.
"""
import copy
from pathlib import Path
from app.services.provenance import fingerprint, file_sha256, atomic_json
from app.services.narration_pipeline import read


def source_identity(manifest):
    return fingerprint([(u['unit_id'], u['text']) for u in manifest['alignment']])


def anchor_sample(anchor, manifest, evidence):
    if anchor == {'at': 'start'}:
        return 0
    if anchor == {'at': 'end'}:
        return manifest['samples']
    unit = next((u for u in manifest['alignment'] if u['unit_id'] == anchor.get('unit_id')), None)
    if unit is None:
        raise ValueError('direction anchor has unknown unit')
    index = anchor.get('text_start', 0)
    if type(index) is not int:
        raise ValueError('direction text offset must be an integer')
    if index == len(unit['text']):
        return unit['sample_end']
    rows = [w for w in evidence['words'][unit['unit_id']] if w['text_start'] == index]
    if len(rows) != 1:
        raise ValueError('direction anchor must be an observed acoustic word boundary')
    return rows[0]['sample_start']


def apply(manifest, timeline, design_path, image_assets, destination):
    from app.services.forced_alignment import require_evidence
    from app.services import overlay
    from app.models.shot import MotionSpec
    from app.services.motion import resolve_motion
    require_evidence(manifest, timeline)
    design = read(design_path)
    if design.get('version') != 1 or design.get('source_identity') != source_identity(manifest):
        raise ValueError('direction plan is not bound to this narration')
    evidence = read(timeline['alignment_evidence_path'])
    resolve = lambda a: anchor_sample(a, manifest, evidence)
    result = copy.deepcopy(timeline)
    sr = manifest['sample_rate']
    visual = design.get('visual_events', [])
    if visual:
        starts = [resolve(v['at']) for v in visual]
        if starts[0] != 0 or starts != sorted(set(starts)) or starts[-1] >= manifest['samples']:
            raise ValueError('visual events must start at zero and increase within the narration')
        shots = []
        for i, (event, a, b) in enumerate(zip(visual, starts, starts[1:] + [manifest['samples']])):
            key = event['image_id']
            if key not in image_assets:
                raise ValueError(f'direction image is not declared: {key}')
            spec = MotionSpec(**dict(event['motion'], geometry_version='fit_cover_v2'))
            resolve_motion(spec)
            shots.append(dict(id=f'directed-{i:03}', image_id=key, image_path=image_assets[key],
                              sample_start=a, sample_end=b, motion=spec.model_dump(mode='json'),
                              source_anchor=event['at'], purpose=event.get('purpose', '')))
            if event.get('animation') is not None:
                from app.services.scene_animation import validate as validate_animation
                shots[-1]['animation'] = validate_animation(event['animation'], file_sha256(image_assets[key]))
        result['shots'] = shots
    cards = []
    for event in design.get('cards', []):
        start = round(resolve(event['at']) * 1000 / sr)
        cards.append(overlay.Spec(start_ms=start, end_ms=start + event['duration_ms'], **event['card']))
    if cards:
        overlay.validate(cards, total_ms=manifest['samples'] * 1000 / sr)
        payload = {'version': overlay.SCHEMA_VERSION, 'overlays': [c.payload() for c in cards]}
        path = Path(destination) / ('overlays-' + fingerprint(payload)[:24] + '.json')
        atomic_json(path, payload)
        result['overlay_path'] = str(path.resolve())
    sound = copy.deepcopy(design.get('sound', {}))
    for event in sound.get('voice_gains', []):
        event['sample_start'] = resolve(event.pop('start'))
        event['sample_end'] = resolve(event.pop('end'))
    for event in sound.get('beds', []):
        event['sample_start'] = resolve(event.pop('start'))
        event['sample_end'] = resolve(event.pop('end'))
    if sound:
        from app.services.sound_design import validate
        validate(sound, manifest['samples'])
        if result.get('bgm_path'):
            raise ValueError('choose directed sound or legacy bgm, not both')
        result['sound_design'] = sound
    result.setdefault('inputs', {})[str(Path(design_path).resolve())] = file_sha256(design_path)
    result['direction'] = {'source_identity': source_identity(manifest), 'status': 'compiled',
                           'performance': 'original_voice_edit_and_mix', 'listening_approved': False}
    return result
