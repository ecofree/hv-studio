"""Per-take evidence and explicit issue disposition, without rewriting old QA."""
from pathlib import Path
from app.services.acoustic_session import job_session
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def _check_take(args):
    """Single take: recognize + check. Pure per-take work, safe to parallelize."""
    from app.services.asr_check import check, normalize_numbers, phonetic_equivalent, Transcript
    from app.services.local_asr import recognize
    from app.services.provenance import file_sha256, fingerprint

    unit, take, context, unit_text_fp = args
    # Short-circuit: a passed take whose QA is still bound to this exact audio
    # and text needs no re-recognition. recognize+check are deterministic, so
    # skipping them changes nothing except wall time.
    prior = take.get('quality') or {}
    if (prior.get('status') == 'passed'
            and prior.get('expected_sha256') == unit_text_fp
            and prior.get('audio_sha256') == take.get('audio_sha256')
            and file_sha256(take['audio_path']) == take.get('audio_sha256')):
        quality = dict(prior)
    else:
        recognized, evidence = recognize(take['audio_path'], context=context)
        transcript = Transcript(recognized['text'])
        transcript.request_id = recognized['request_id']; transcript.checker_version = recognized['checker_version']
        transcript.recognition_path = str(evidence.resolve())
        quality = check(Path(take['audio_path']), unit['text'], prepared_transcript=transcript)
        quality = quality.to_dict() if hasattr(quality, 'to_dict') else quality
        if quality['audio_sha256'] != take['audio_sha256'] or quality['expected_sha256'] != unit_text_fp:
            raise ValueError('quality evidence not bound to current audio/source')
    from app.services.audio_content_qa import evaluate
    observed=dict(audio_sha256=take['audio_sha256'],text=quality.get('transcript',''))
    content=evaluate(take['audio_path'],unit['text'],recognition=observed)
    quality=dict(quality,content_checks=content)
    return unit, take, quality


@job_session()
def build(manifest_path, destination, *, progress=None):
    from app.services.asr_check import normalize_numbers, phonetic_equivalent
    from app.services.local_asr import preflight
    manifest = validate_assembly(manifest_path); rows = []
    context = preflight()
    pairs = [(u, t, context, fingerprint(u['text']))
             for u, t in zip(manifest['alignment'], manifest['takes'])]
    for i, args in enumerate(pairs):
        unit, take, quality = _check_take(args)
        if progress: progress(dict(done=i+1, total=len(pairs), message='音频质量检查'))
        transcript = quality.get('transcript', '')
        row = dict(unit_id=unit['unit_id'], audio_path=take['audio_path'], audio_sha256=take['audio_sha256'],
                   source_text=unit['text'], source_sha256=fingerprint(unit['text']),
                   sample_start=unit['sample_start'], sample_end=unit['sample_end'], previous_status=take['quality']['status'], quality=quality,
                   disposition='machine_transcript_passed' if quality['status']=='passed' and quality['content_checks']['status']=='passed_machine' else 'needs_listening_review',
                   interpretation='ASR recognition is not proof of a spoken error; compare this bounded take to source')
        if normalize_numbers(unit['text']) == normalize_numbers(transcript): row['explanation'] = 'equivalent number/traditional numeral notation'
        elif phonetic_equivalent(unit['text'], transcript): row['explanation'] = 'exact whole-utterance syllable and tone equality; ASR spelling variance'
        rows.append(row)
    record = dict(manifest_path=str(Path(manifest_path).resolve()), manifest_sha256=file_sha256(manifest_path),
                  checker_sha256=file_sha256(Path(__file__).with_name('asr_check.py')), takes=rows,
                  content_checker_sha256=file_sha256(Path(__file__).with_name('audio_content_qa.py')),
                  pending_units=[r['unit_id'] for r in rows if r['disposition']=='needs_listening_review'], listening_approved=False)
    path = Path(destination)/(fingerprint(record)+'.json'); atomic_json(path, record)
    return path


def record_disposition(report_path, unit_id, reviewer, finding, decision):
    """Single-unit callers use the same validation and storage as batch review."""
    return record_dispositions(report_path, [unit_id], reviewer, finding, decision)[0]


def record_dispositions(report_path, unit_ids, reviewer, finding, decision, *, expected_report_sha256=None):
    """Record explicitly selected, actually listened units. Validate all before writing."""
    from datetime import datetime, timezone
    from app.services.process_lock import file_lock
    if decision not in ('asr_false_positive', 'spoken_error') or not reviewer.strip() or not finding.strip():
        raise ValueError('reviewer, finding and explicit disposition required')
    if not unit_ids or len(set(unit_ids)) != len(unit_ids):
        raise ValueError('select distinct units that were actually listened to')
    with file_lock(Path(report_path).with_suffix('.review.lock')):
        if expected_report_sha256 and file_sha256(report_path) != expected_report_sha256:
            raise ValueError('quality report changed; reload before reviewing')
        report = read(report_path); manifest = validate_assembly(report['manifest_path'])
        if file_sha256(report['manifest_path']) != report['manifest_sha256']:
            raise ValueError('stale quality report')
        rows = {r['unit_id']: r for r in report['takes']}
        if not set(unit_ids) <= rows.keys(): raise ValueError('unknown review unit')
        for key in unit_ids:
            if file_sha256(rows[key]['audio_path']) != rows[key]['audio_sha256']:
                raise ValueError('reviewed audio changed')
        written = []
        for key in unit_ids:
            row = rows[key]
            value = dict(unit_id=key, audio_sha256=row['audio_sha256'], source_sha256=row['source_sha256'],
                         reviewer=reviewer, finding=finding, decision=decision,
                         reviewed_at=datetime.now(timezone.utc).isoformat())
            path = Path(report_path).parent/'dispositions'/(fingerprint(value)+'.json')
            atomic_json(path, value); written.append(path)
            if decision == 'spoken_error':
                from app.services.audio_disposition import reject_take
                take = next(t for t in manifest['takes'] if t['unit_id']==key)
                reject_take(take, reviewer, finding, evidence_path=path)
        return written


def review_queue(report_path, *, all_units=False):
    """Time-ordered listening queue; ASR observations are not timing failures."""
    report = read(report_path)
    wanted = set(pending(report_path))
    manifest = read(report['manifest_path'])
    sr = manifest['sample_rate']
    rows = []
    for row in report['takes']:
        if not all_units and row['unit_id'] not in wanted: continue
        findings = row['quality'].get('findings', []) + row['quality'].get('content_checks', {}).get('findings', [])
        kinds={f.get('kind','unclassified') for f in findings}
        if all_units:
            import difflib
            text=row['source_text']
            heard=row['quality'].get('transcript') or ''
            pairs={'南北','东西','東西','左右','进退','進退','胜败','勝敗','生死','攻守','买卖','買賣'}
            for op,l,r,i,j in difflib.SequenceMatcher(None,text,heard,autojunk=False).get_opcodes():
                if op=='replace' and r-l==j-i==1 and any({text[l],heard[i]}==set(pair) for pair in pairs):
                    kinds.add('meaning_change')
        rows.append(dict(unit_id=row['unit_id'], text=row['source_text'], audio_path=row['audio_path'],
                         start_seconds=row['sample_start']/sr, end_seconds=row['sample_end']/sr,
                         kinds=sorted(kinds),
                         transcript=row['quality'].get('transcript','')))
    return sorted(rows, key=lambda r:r['start_seconds'])



def pending(report_path):
    report = read(report_path)
    if file_sha256(report['manifest_path']) != report['manifest_sha256'] or file_sha256(Path(__file__).with_name('asr_check.py')) != report['checker_sha256']:
        raise ValueError('quality report is stale')
    if report.get('content_checker_sha256') and report['content_checker_sha256']!=file_sha256(Path(__file__).with_name('audio_content_qa.py')):
        raise ValueError('content quality report is stale')
    result = []
    records = [read(p) for p in (Path(report_path).parent/'dispositions').glob('*.json')]
    for row in report['takes']:
        if row['quality']['status']=='passed' and not row['quality'].get('content_checks',{}).get('findings'): continue
        reviewed = [r for r in records if r['audio_sha256']==row['audio_sha256'] and r['source_sha256']==row['source_sha256']]
        latest = max(reviewed, key=lambda r:r['reviewed_at'], default={})
        if latest.get('decision') != 'asr_false_positive': result.append(row['unit_id'])
    return result
