"""Per-take evidence and explicit issue disposition, without rewriting old QA."""
from pathlib import Path
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def build(manifest_path, destination):
    from app.services.asr_check import check, normalize_numbers, phonetic_equivalent, Transcript
    from app.services.local_asr import recognize, preflight
    manifest = validate_assembly(manifest_path); rows = []
    context = preflight()
    for unit, take in zip(manifest['alignment'], manifest['takes']):
        recognized, evidence = recognize(take['audio_path'], context=context)
        transcript = Transcript(recognized['text'])
        transcript.request_id = recognized['request_id']; transcript.checker_version = recognized['checker_version']
        transcript.recognition_path = str(evidence.resolve())
        quality = check(Path(take['audio_path']), unit['text'], prepared_transcript=transcript)
        quality = quality.to_dict() if hasattr(quality, 'to_dict') else quality
        if quality['audio_sha256'] != take['audio_sha256'] or quality['expected_sha256'] != fingerprint(unit['text']):
            raise ValueError('quality evidence not bound to current audio/source')
        transcript = quality.get('transcript', '')
        row = dict(unit_id=unit['unit_id'], audio_path=take['audio_path'], audio_sha256=take['audio_sha256'],
                   source_text=unit['text'], source_sha256=fingerprint(unit['text']),
                   sample_start=unit['sample_start'], sample_end=unit['sample_end'], previous_status=take['quality']['status'], quality=quality,
                   disposition='machine_transcript_passed' if quality['status']=='passed' else 'needs_listening_review',
                   interpretation='ASR recognition is not proof of a spoken error; compare this bounded take to source')
        if normalize_numbers(unit['text']) == normalize_numbers(transcript): row['explanation'] = 'equivalent number/traditional numeral notation'
        elif phonetic_equivalent(unit['text'], transcript): row['explanation'] = 'exact whole-utterance syllable and tone equality; ASR spelling variance'
        rows.append(row)
    record = dict(manifest_path=str(Path(manifest_path).resolve()), manifest_sha256=file_sha256(manifest_path),
                  checker_sha256=file_sha256(Path(__file__).with_name('asr_check.py')), takes=rows,
                  pending_units=[r['unit_id'] for r in rows if r['quality']['status']!='passed'], listening_approved=False)
    path = Path(destination)/(fingerprint(record)+'.json'); atomic_json(path, record)
    return path


def record_disposition(report_path, unit_id, reviewer, finding, decision):
    """Human-only API/CLI entry, requiring the exact listened take and finding."""
    from datetime import datetime, timezone
    report = read(report_path); manifest = validate_assembly(report['manifest_path'])
    if file_sha256(report['manifest_path']) != report['manifest_sha256']: raise ValueError('stale quality report')
    if decision not in ('asr_false_positive', 'spoken_error') or not reviewer.strip() or not finding.strip():
        raise ValueError('reviewer, finding and explicit disposition required')
    row = next(r for r in report['takes'] if r['unit_id']==unit_id)
    if file_sha256(row['audio_path']) != row['audio_sha256']: raise ValueError('reviewed audio changed')
    value = dict(unit_id=unit_id, audio_sha256=row['audio_sha256'], source_sha256=row['source_sha256'],
                 reviewer=reviewer, finding=finding, decision=decision, reviewed_at=datetime.now(timezone.utc).isoformat())
    path = Path(report_path).parent/'dispositions'/(fingerprint(value)+'.json'); atomic_json(path, value)
    if decision == 'spoken_error':
        from app.services.audio_disposition import reject_take
        take = next(t for t in manifest['takes'] if t['unit_id']==unit_id)
        reject_take(take, reviewer, finding, evidence_path=path)
    return path


def pending(report_path):
    report = read(report_path)
    if file_sha256(report['manifest_path']) != report['manifest_sha256'] or file_sha256(Path(__file__).with_name('asr_check.py')) != report['checker_sha256']:
        raise ValueError('quality report is stale')
    result = []
    records = [read(p) for p in (Path(report_path).parent/'dispositions').glob('*.json')]
    for row in report['takes']:
        if row['quality']['status']=='passed': continue
        reviewed = [r for r in records if r['audio_sha256']==row['audio_sha256'] and r['source_sha256']==row['source_sha256']]
        latest = max(reviewed, key=lambda r:r['reviewed_at'], default={})
        if latest.get('decision') != 'asr_false_positive': result.append(row['unit_id'])
    return result
