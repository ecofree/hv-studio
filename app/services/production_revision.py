"""Immutable production evidence and metadata-only amendments of existing media.

Nothing in this module invokes a provider, ASR, encoder, or approval action.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read
from app.services.process_lock import file_lock


def write_once(path, value):
    path = Path(path)
    with file_lock(path.with_name('.' + path.name + '.lock')):
        if path.exists():
            if read(path) != value:
                raise ValueError('immutable revision collision: ' + str(path))
        else:
            atomic_json(path, value)
    return path


def archive_candidate(receipt_path, destination, *, recovered=None):
    """Preserve exact receipt bytes and inputs, accepting recovered bytes by SHA only."""
    receipt_path, destination = Path(receipt_path).resolve(), Path(destination).resolve()
    receipt = read(receipt_path)
    if file_sha256(receipt['candidate_path']) != receipt['candidate_sha256']:
        raise ValueError('candidate changed before archive')
    files = dict(receipt['inputs'])
    files[str(receipt_path)] = file_sha256(receipt_path)
    rows = {}
    recovered = recovered or {}
    for name, expected in files.items():
        source = Path(name)
        target = destination/'objects'/(expected + source.suffix)
        if target.exists():
            if file_sha256(target) != expected:
                raise ValueError('archive object changed')
        else:
            data = recovered.get(name)
            if data is None:
                data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('cannot recover exact input: ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Content-addressed objects are never overwritten.
            with target.open('xb') as stream:
                stream.write(data)
        rows[name] = dict(path=str(target), sha256=expected)
    record = dict(version=1, receipt_path=str(receipt_path), receipt_sha256=files[str(receipt_path)],
                  candidate_path=receipt['candidate_path'], candidate_sha256=receipt['candidate_sha256'],
                  inputs=rows, recovered_inputs=sorted(recovered), approved=False)
    path = write_once(destination/'versions'/(files[str(receipt_path)]+'.json'), record)
    verify_archive(path)
    return path


def verify_archive(path):
    record = read(path)
    for item in record['inputs'].values():
        if file_sha256(item['path']) != item['sha256']:
            raise ValueError('archived input changed')
    receipt = read(record['inputs'][record['receipt_path']]['path'])
    if record['receipt_sha256'] != file_sha256(record['inputs'][record['receipt_path']]['path']):
        raise ValueError('archived receipt changed')
    if any(record['inputs'].get(p, {}).get('sha256') != h for p,h in receipt['inputs'].items()):
        raise ValueError('archive does not cover original receipt')
    if (record['candidate_path']!=receipt['candidate_path'] or
            record['candidate_sha256']!=receipt['candidate_sha256']):
        raise ValueError('archive candidate differs from original receipt')
    if file_sha256(record['candidate_path']) != record['candidate_sha256']:
        raise ValueError('archived candidate bytes changed')
    return record


def archived_json(archive, original):
    return read(archive['inputs'][str(original)]['path'])


def _timeline_path(archive, receipt):
    if receipt.get('timeline_path'): return receipt['timeline_path']
    candidates=[]
    for name in receipt['inputs']:
        if Path(name).suffix=='.json' and 'timeline' in Path(name).name:
            value=archived_json(archive,name)
            if 'shots' in value and 'subtitles' in value:candidates.append(name)
    if len(candidates)!=1:raise ValueError('receipt needs one bound timeline')
    return candidates[0]


def fork_visual_revision(timeline_path, overlays, destination):
    """Editing title/act copy always creates new inputs; the caller switches later."""
    original = read(timeline_path)
    version = Path(destination)/fingerprint([file_sha256(timeline_path), overlays])[:24]
    overlay_path = write_once(version/'overlays.json',overlays)
    timeline = copy.deepcopy(original)
    timeline['inputs'].pop(timeline['overlay_path'],None)
    timeline['overlay_path'] = str(overlay_path.resolve())
    timeline['inputs'][str(overlay_path.resolve())] = file_sha256(overlay_path)
    return write_once(version/'timeline.json',timeline)


def amend_metadata(archive_path, manifest, destination):
    """Rebind unchanged timing/media to corrected narration provenance, without rendering."""
    from app.services.narration_pipeline import validate_assembly
    from app.services.production_v2 import validate_timeline
    from app.services.production_identity import _is_renderer
    archive_path, destination = Path(archive_path).resolve(), Path(destination).resolve()
    archive = verify_archive(archive_path)
    origin = archived_json(archive, archive['receipt_path'])
    origin_timeline_path = _timeline_path(archive, origin)
    original_manifest = archived_json(archive, origin['manifest_path'])
    original_timeline = archived_json(archive, origin_timeline_path)
    for key in set(original_manifest) | set(manifest):
        if key != 'continuous_tts' and manifest.get(key) != original_manifest.get(key):
            raise ValueError('metadata amendment changes narration or clock: ' + key)
    manifest_path = write_once(destination/'manifest.json', manifest)
    validate_assembly(manifest_path)
    old_alignment_path = original_timeline['alignment_evidence_path']
    alignment = archived_json(archive, old_alignment_path)
    alignment['manifest_path'] = str(manifest_path)
    alignment['manifest_content_sha256'] = fingerprint(manifest)
    alignment['inputs'].pop(origin['manifest_path'])
    alignment['inputs'][str(manifest_path)] = file_sha256(manifest_path)
    # Timing belongs to the implementation used at production time. Freeze its
    # exact bytes before upgrading the checker; never claim timing was rerun.
    snapshots={item['sha256']:item['path'] for item in archive['inputs'].values()}
    for p,h in list(alignment['inputs'].items()):
        if Path(p).suffix=='.py' and h in snapshots:
            alignment['inputs'].pop(p)
            alignment['inputs'][snapshots[h]]=h
    alignment['metadata_parent'] = dict(path=archive['inputs'][old_alignment_path]['path'],
                                      sha256=archive['inputs'][old_alignment_path]['sha256'])
    alignment_path = write_once(destination/'alignment.json', alignment)
    overlay_path = write_once(destination/'overlays.json', archived_json(archive, original_timeline['overlay_path']))
    timeline = copy.deepcopy(original_timeline)
    timeline['alignment_evidence_path'] = str(alignment_path)
    timeline['overlay_path'] = str(overlay_path)
    timeline['inputs'].pop(original_timeline['overlay_path'], None)
    timeline['inputs'][str(overlay_path)] = file_sha256(overlay_path)
    timeline_path = write_once(destination/'timeline.json', timeline)
    validate_timeline(manifest, timeline)
    replaced = {origin['manifest_path'], origin_timeline_path, old_alignment_path, original_timeline['overlay_path']}
    inputs = {}
    for path, value in origin['inputs'].items():
        if path in replaced: continue
        # Preserve the implementation that actually rendered these bytes.
        bound = archive['inputs'][path]['path'] if _is_renderer(path) else path
        if file_sha256(bound) != value: raise ValueError('unchanged render input drifted')
        inputs[bound] = value
    for path in [archive_path,manifest_path,alignment_path,timeline_path,overlay_path,
                 Path(manifest['continuous_tts']['repair_receipt_path'])]:
        inputs[str(path)] = file_sha256(path)
    receipt = copy.deepcopy(origin)
    receipt.update(kind='metadata_amendment_v1',render_performed=False,approved=False,
                   origin_archive_path=str(archive_path),origin_archive_sha256=file_sha256(archive_path),
                   manifest_path=str(manifest_path),timeline_path=str(timeline_path),inputs=inputs)
    path = write_once(destination/'candidate.json', receipt)
    verify_amendment(path)
    return path


def verify_amendment(path):
    """Reject media/timing/visual edits disguised as metadata corrections."""
    from app.services.narration_pipeline import validate_assembly
    from app.services.production_v2 import validate_timeline
    r = read(path)
    if r.get('kind') != 'metadata_amendment_v1' or r.get('render_performed') is not False or r.get('approved') is not False:
        raise ValueError('invalid metadata amendment status')
    if file_sha256(r['origin_archive_path']) != r['origin_archive_sha256']:
        raise ValueError('origin archive changed')
    archive = verify_archive(r['origin_archive_path'])
    old = archived_json(archive, archive['receipt_path'])
    for key in ('candidate_path','candidate_sha256','samples','sample_rate','expected_frames'):
        if r[key] != old[key]: raise ValueError('metadata amendment changed media or clock')
    if any(file_sha256(p) != h for p,h in r['inputs'].items()):
        raise ValueError('metadata amendment input changed')
    m, t = validate_assembly(r['manifest_path']), read(r['timeline_path'])
    for name in ('manifest_path','timeline_path'):
        if r['inputs'].get(r[name]) != file_sha256(r[name]): raise ValueError('unbound amendment metadata')
    om, ot = archived_json(archive, old['manifest_path']), archived_json(archive, _timeline_path(archive,old))
    if {k:v for k,v in m.items() if k!='continuous_tts'} != {k:v for k,v in om.items() if k!='continuous_tts'}:
        raise ValueError('narration changed in metadata amendment')
    editable = {'raw_sha256','lineage_version','original_raw','repaired_raw','repair_receipt_path','repair_receipt_sha256'}
    if {k:v for k,v in m['continuous_tts'].items() if k not in editable} != {k:v for k,v in om['continuous_tts'].items() if k not in editable}:
        raise ValueError('voice configuration changed in metadata amendment')
    allowed = {'inputs','alignment_evidence_path','overlay_path'}
    if {k:v for k,v in t.items() if k not in allowed} != {k:v for k,v in ot.items() if k not in allowed}:
        raise ValueError('visuals or subtitle times changed in metadata amendment')
    if read(t['overlay_path']) != archived_json(archive, ot['overlay_path']):
        raise ValueError('overlay changed in metadata amendment')
    a, oa = read(t['alignment_evidence_path']), archived_json(archive, ot['alignment_evidence_path'])
    allowed = {'manifest_path','manifest_content_sha256','inputs','metadata_parent'}
    if {k:v for k,v in a.items() if k not in allowed} != {k:v for k,v in oa.items() if k not in allowed}:
        raise ValueError('acoustic timing changed in metadata amendment')
    validate_timeline(m,t)
    return r


def register_existing_full(ep_id, receipt_path, *, expected_identity):
    """Use the existing v2 run state as the sole current full-episode pointer."""
    from app import paths
    from app.services import studio_workflow as W
    from app.services.studio_sources import require_usable
    require_usable(ep_id)
    root = paths.episode_dir(ep_id)/'production/v2'
    receipt_path = Path(receipt_path).resolve()
    r = verify_amendment(receipt_path)
    media_probe = probe_existing_media(r)
    m, t = read(r['manifest_path']), read(r['timeline_path'])
    expected = [(u['unit_id'],u['source_text']) for u in W.document(ep_id)['units']]
    if [(u['unit_id'],u['text']) for u in m['alignment']] != expected:
        raise ValueError('full episode does not match current source')
    W.require_direction_current(ep_id)
    direction = str((paths.episode_dir(ep_id)/'directing.json').resolve())
    if r['inputs'].get(direction) != file_sha256(direction): raise ValueError('full episode directing changed')
    before_receipt = file_sha256(receipt_path)
    with file_lock(paths.episode_dir(ep_id)/'.produce.lock'):
        with file_lock(W.folder(ep_id)/'workflow.lock'):
            if W.identity(ep_id) != expected_identity or file_sha256(receipt_path) != before_receipt:
                raise ValueError('episode changed before registration')
            state_path = root/'run.json'
            state = read(state_path) if state_path.exists() else {}
            if state.get('running'): raise ValueError('production is still running')
            if state.get('receipt_path') == str(receipt_path): return state
            write_once(root/'run-history'/(fingerprint(state)+'.json'),state)
            state.update(source_sha256=W.document(ep_id)['source_sha256'],plan_path=m['plan_path'],
                         manifest_path=r['manifest_path'],timeline_path=r['timeline_path'],
                         alignment_path=t['alignment_evidence_path'],receipt_path=str(receipt_path),
                         audio_master_path=m['master_path'],active_stage='review',running=False,
                         full_episode_registration=dict(kind='existing_media_metadata_amendment',
                             receipt_path=str(receipt_path),receipt_sha256=before_receipt,
                             source_identity=expected_identity,render_performed=False,approved=False,
                             media_probe=media_probe))
            state.setdefault('image_assets',{}).update({s['image_id']:s['image_path'] for s in t['shots']})
            state['last_result'] = dict(pipeline_version='production_v2',status='NEEDS_REVIEW',
                stages_done=['script','images','tts','alignment','timeline','compose'],
                final_mp4=r['candidate_path'],audio_path=m['master_path'],approved=False,production_ready=False,
                provenance='registered_existing_full_episode_no_generation')
            atomic_json(state_path,state)  # Only mutable pointer; all evidence is already durable.
    return state


def probe_existing_media(receipt):
    """Count existing packets and inspect clocks, never encode/decode new media."""
    result=subprocess.run(['ffprobe','-v','error','-count_packets','-show_streams','-of','json',receipt['candidate_path']],
                          check=True,capture_output=True,text=True,encoding='utf-8',timeout=60)
    streams=json.loads(result.stdout)['streams']
    video=[s for s in streams if s['codec_type']=='video'];audio=[s for s in streams if s['codec_type']=='audio']
    if len(video)!=1 or len(audio)!=1:raise ValueError('registered media needs one video and one audio stream')
    v,a=video[0],audio[0];seconds=receipt['samples']/receipt['sample_rate']
    if (v['width'],v['height'])!=(1080,1920) or int(v['nb_read_packets'])!=receipt['expected_frames']:
        raise ValueError('registered media frame geometry/count mismatch')
    if (v['avg_frame_rate']!='25/1' or int(a['sample_rate'])!=receipt['sample_rate'] or
        abs(float(v['duration'])-seconds)>.045 or abs(float(a['duration'])-seconds)>.08):
        raise ValueError('registered media sample clock mismatch')
    return dict(frames=int(v['nb_read_packets']),sample_rate=int(a['sample_rate']),
                video_seconds=float(v['duration']),audio_seconds=float(a['duration']),status='passed')
