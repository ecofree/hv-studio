"""Executable delivery cues and explicit unresolved artistic directions."""
import copy
from pathlib import Path
import wave
from app.services.narration_pipeline import read, validate_assembly, wave_info
from app.services.provenance import atomic_json, fingerprint, file_sha256


def effective_unit(unit, plan, asset):
    """Consume supported voice instructions without silently truncating them."""
    result = copy.deepcopy(unit)
    cues = [c for c in plan.get('delivery_cues', []) if c.get('unit_id') == unit['unit_id']
            and c['intent'] != 'pause' and c.get('text_offset') == 0
            and '语气' in c['raw_text'] and '配乐' not in c['raw_text']]
    if cues and not result.get('delivery', {}).get('instruction') and asset.get('capabilities', {}).get('instruction', {}).get('status') == 'supported':
        result.setdefault('delivery', {})['instruction'] = '；'.join(c['raw_text'].strip('【】') for c in cues)
    return result


def dispositions(plan, asset, insertions=None):
    rows = []
    for cue in plan.get('delivery_cues', []):
        row = dict(cue_id=cue['cue_id'], unit_id=cue.get('unit_id'), text=cue['raw_text'])
        if cue['intent'] == 'pause':
            done = next((p for values in (insertions or {}).values() for p in values if p['cue_id'] == cue['cue_id']), None)
            row.update(status='executed' if done else 'pending', action='insert_exact_pcm_silence', evidence=done)
        elif cue.get('unit_id'):
            unit = next(u for u in plan['units'] if u['unit_id'] == cue['unit_id'])
            effective = effective_unit(unit, plan, asset)
            instruction = effective.get('delivery', {}).get('instruction')
            row.update(status='request_compiled_audio_review_pending' if instruction else 'unsupported_needs_disposition',
                       action='listening_review_or_explicit_direction_revision', instruction=instruction or None)
        else:
            row.update(status='visual_direction_needs_review', action='review_final_scene_and_title')
        rows.append(row)
    return rows


def apply(manifest_path, asset):
    """Create a derived master with exact pauses; raw takes stay immutable."""
    from app.services.forced_alignment import align, atoms
    from app.services.local_asr import recognize, preflight
    manifest = validate_assembly(manifest_path)
    policy = file_sha256(__file__)
    if manifest.get('delivery_policy_sha256') == policy: return Path(manifest_path)
    if manifest.get('base_manifest_path'):
        manifest_path = manifest['base_manifest_path']; manifest = validate_assembly(manifest_path)
    plan = read(manifest['plan_path'])
    cues = [c for c in plan.get('delivery_cues', []) if c['intent'] == 'pause']
    insertions, evidence = {}, {}
    context = preflight() if any(c.get('text_offset', 0) > 0 for c in cues) else None
    for unit, take in zip(manifest['alignment'], manifest['takes']):
        pauses = [c for c in cues if c.get('unit_id') == unit['unit_id']]
        if not pauses: continue
        relative = dict(unit, sample_start=0, sample_end=take['samples'])
        rows = None
        for cue in pauses:
            offset = cue['text_offset']
            if not 0 < cue['pause_seconds'] <= 30: raise ValueError('pause duration must be in (0, 30] seconds')
            if offset == 0: at = 0
            elif offset == len(unit['text']): at = take['samples']
            else:
                if rows is None:
                    recognized, recognized_path = recognize(take['audio_path'], context=context)
                    aligned, aligned_path = align(take['audio_path'], unit['text'], recognized, take['samples']/manifest['sample_rate'], context)
                    rows = atoms(relative, aligned, manifest['sample_rate'])
                    for p in (recognized_path, aligned_path): evidence[str(p.resolve())] = file_sha256(p)
                matches = [r for r in rows if r['text_start'] == offset]
                if not matches: raise ValueError('pause falls inside an acoustic word; explicit alignment review required')
                at = matches[0]['sample_start']
            insertions.setdefault(unit['unit_id'], []).append(dict(cue_id=cue['cue_id'], text_offset=offset,
                        at_sample=at, samples=round(cue['pause_seconds']*manifest['sample_rate'])))
    # End-of-document pauses have no next utterance, but still execute.
    for cue in cues:
        if not cue.get('unit_id'):
            unit, take = manifest['alignment'][-1], manifest['takes'][-1]
            if not 0 < cue['pause_seconds'] <= 30: raise ValueError('invalid end pause')
            insertions.setdefault(unit['unit_id'], []).append(dict(cue_id=cue['cue_id'], text_offset=len(unit['text']),
                        at_sample=take['samples'], samples=round(cue['pause_seconds']*manifest['sample_rate'])))
    identity = dict(base_sha256=file_sha256(manifest_path), policy=policy, insertions=insertions, asset_version=asset['version'])
    target = Path(manifest_path).parent.parent/('delivery-'+fingerprint(identity)[:32])
    path = target/'manifest.json'
    if path.exists(): validate_assembly(path); return path
    target.mkdir(parents=True, exist_ok=True)
    master = target/'master.wav'; result = copy.deepcopy(manifest); cursor = 0
    with wave.open(str(master), 'wb') as out:
        out.setparams((1, 2, manifest['sample_rate'], 0, 'NONE', 'not compressed'))
        for unit, take in zip(result['alignment'], result['takes']):
            count, pcm = wave_info(take['audio_path']); last = 0
            pauses = sorted(insertions.get(unit['unit_id'], []), key=lambda p: p['at_sample'])
            for pause in pauses:
                out.writeframes(pcm[last*2:pause['at_sample']*2]); out.writeframes(b'\0\0'*pause['samples']); last=pause['at_sample']
            out.writeframes(pcm[last*2:])
            count += sum(p['samples'] for p in pauses)
            unit.update(sample_start=cursor, sample_end=cursor+count); cursor += count
    result.update(base_manifest_path=str(Path(manifest_path).resolve()), base_manifest_sha256=file_sha256(manifest_path),
                  master_path=str(master.resolve()), master_sha256=file_sha256(master), samples=cursor,
                  pause_insertions=insertions, delivery_policy_sha256=policy, delivery_evidence=evidence,
                  delivery_dispositions=dispositions(plan, asset, insertions))
    atomic_json(path, result)
    return path


def record_review(manifest_path, cue_id, reviewer, finding, decision):
    """Record an explicit listening/visual decision for one artistic direction."""
    from datetime import datetime, timezone
    manifest = validate_assembly(manifest_path)
    row = next(r for r in manifest.get('delivery_dispositions',[]) if r['cue_id']==cue_id)
    if decision not in ('accepted_existing', 'needs_revision') or not reviewer.strip() or not finding.strip():
        raise ValueError('reviewer, finding and explicit direction decision required')
    record = dict(cue_id=cue_id, master_sha256=manifest['master_sha256'], plan_sha256=manifest['plan_sha256'],
                  direction_sha256=fingerprint(row), reviewer=reviewer, finding=finding, decision=decision,
                  reviewed_at=datetime.now(timezone.utc).isoformat())
    path = Path(manifest_path).parent/'direction_reviews'/(fingerprint(record)+'.json'); atomic_json(path,record)
    return path


def pending(manifest_path):
    manifest = validate_assembly(manifest_path)
    reviews = [read(p) for p in (Path(manifest_path).parent/'direction_reviews').glob('*.json')]
    result = []
    for row in manifest.get('delivery_dispositions',[]):
        if row['status']=='executed': continue
        current = [r for r in reviews if r['cue_id']==row['cue_id'] and r['master_sha256']==manifest['master_sha256']
                   and r['plan_sha256']==manifest['plan_sha256'] and r['direction_sha256']==fingerprint(row)]
        latest = max(current,key=lambda r:r['reviewed_at'],default={})
        if latest.get('decision')!='accepted_existing':result.append(row)
    return result
