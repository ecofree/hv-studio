"""Build image motion and short subtitles from source + acoustic evidence."""
from pathlib import Path
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def normalized(text): return ''.join(c for c in text if c.isalnum())


def scene_bindings(ep, manifest, scene_plan):
    """Use legacy frame ranges only when their full source is identical."""
    if normalized(''.join(f.text for f in ep.frames)) != normalized(''.join(u['text'] for u in manifest['alignment'])):
        raise ValueError('scene frame source differs from current narration; provide a source-bound scene plan')
    offsets, cursor = [], 0
    for frame in ep.frames:
        offsets.append(cursor); cursor += len(normalized(frame.text))
    offsets.append(cursor)
    result, last = [], 0
    for scene in scene_plan['scenes']:
        a, b = scene['frames']
        if a != last or not a <= b < len(ep.frames): raise ValueError('scene frame ranges have gaps, overlap or invalid bounds')
        result.append(dict(image=scene['image'], source_start=offsets[a], source_end=offsets[b+1],
                           motion=scene.get('motion'), grades=scene.get('grades', [])))
        last = b+1
    if last != len(ep.frames): raise ValueError('scene plan does not cover all frames')
    return result


def timed_scenes(ep, manifest, plan, evidence):
    if 'scenes' in plan:
        bindings = scene_bindings(ep, manifest, plan)
    else:
        # A new episode can provide explicit unit ranges without legacy frames.
        units = {u['unit_id']: u for u in manifest['alignment']}
        offsets, cursor = {}, 0
        for uid, u in units.items(): offsets[uid] = cursor; cursor += len(normalized(u['text']))
        bindings = [dict(image=s['image'], source_start=offsets[s['unit_id']],
                         source_end=offsets[s['unit_id']]+len(normalized(units[s['unit_id']]['text'])),
                         motion=s.get('motion'), grades=s.get('grades', [])) for s in plan['unit_scenes']]
    # Source character starts bind to acoustic word starts, not char-proportional timing.
    anchors, cursor = {}, 0
    for unit in manifest['alignment']:
        for word in evidence['words'][unit['unit_id']]:
            key = cursor+len(normalized(unit['text'][:word['text_start']]))
            anchors.setdefault(key, word['sample_start'])
        cursor += len(normalized(unit['text']))
    anchors[0], anchors[cursor] = 0, manifest['samples']
    result, end = [], 0
    for scene in bindings:
        a, b = scene['source_start'], scene['source_end']
        if a != end or b <= a: raise ValueError('scene source ranges must cover source exactly once')
        if a not in anchors or b not in anchors:
            raise ValueError('scene change falls inside an acoustic word; adjust the source scene boundary')
        result.append((scene,anchors[a],anchors[b]))
        end=b
    if end!=cursor:raise ValueError('scene source coverage incomplete')
    return result


def build(ep, manifest_path, alignment_path, image_assets, scene_plan_path, destination, quality_path=None):
    from app.services.studio_workflow import require_direction_current
    require_direction_current(ep.id)
    from app.services.production_v2 import validate_timeline, frame_boundary
    from app.services.forced_alignment import require_evidence
    from app.config.config import get_settings
    from app.services.bgm import pick_bgm
    manifest, evidence = validate_assembly(manifest_path), read(alignment_path)
    plan = read(scene_plan_path)
    shots = []
    for i,(scene,start,stop) in enumerate(timed_scenes(ep,manifest,plan,evidence)):
        motion = scene.get('motion') or dict(type='push' if i % 2 == 0 else 'pull',
                        start_zoom=1. if i % 2 == 0 else 1.06, end_zoom=1.06 if i % 2 == 0 else 1.,
                        start_xy=[.5,.5], end_xy=[.5,.5], geometry_version='fit_cover_v2')
        if scene.get('grades') and not motion.get('grade'): motion = dict(motion, grade=scene['grades'][0])
        shots.append(dict(id=f'scene-{i:03}', image_id=scene['image'], image_path=image_assets[scene['image']],
                          source_start=scene['source_start'], source_end=scene['source_end'], sample_start=start, sample_end=stop, motion=motion))
    timeline = dict(schema_version='2.1', shots=shots, subtitles=evidence['subtitles'],
                    alignment_evidence_path=str(Path(alignment_path).resolve()), status='candidate',
                    inputs={str(Path(scene_plan_path).resolve()): file_sha256(scene_plan_path)},
                    provenance='source_ranges_to_observed_acoustic_word_boundaries',
                    # Declarative only: no gate reads review_required. The real
                    # publication bars are pending()/require_evidence upstream
                    # and the dual human reviews at publish. Renaming it would
                    # orphan every timeline on disk, so it stays with this note.
                    review_required=True)
    bgm = pick_bgm(get_settings().profile_name)
    from app.services.production_inputs import input_files
    timeline['inputs'].update({p:sha for p,sha in input_files(ep.id).items() if sha is not None})
    if bgm: timeline['bgm_path'] = str(bgm.resolve())
    if quality_path:
        timeline['quality_report_path'] = str(Path(quality_path).resolve())
        timeline['inputs'][str(Path(quality_path).resolve())] = file_sha256(quality_path)
    from app.paths import episode_dir
    design_path = episode_dir(ep.id) / 'directing.json'
    if design_path.is_file():
        from app.services.directing import apply
        timeline = apply(manifest, timeline, design_path, image_assets, destination)
    validate_timeline(manifest, timeline)
    path = Path(destination)/(fingerprint(timeline)+'.json'); atomic_json(path, timeline)
    return path
