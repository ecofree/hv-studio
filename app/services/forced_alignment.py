"""Source-complete acoustic subtitle timing, separate from transcript QA."""
import difflib
import json
from pathlib import Path
import subprocess
import tempfile

from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read, validate_assembly


def windows(source, recognition, duration):
    """Split only at independently observed, matching word boundaries."""
    if duration <= 30: return [dict(text=source, start=0., end=duration)]
    from app.services.subtitle_alignment import symbols
    reference, positions = symbols(source)
    heard, spans = [], []
    for segment in recognition['segments']:
        for word in segment.get('words', []):
            values, _ = symbols(word['word'])
            spans.append((len(heard), word)); heard.extend(values)
    mapping = {}
    for a, b, size in difflib.SequenceMatcher(None, reference, heard, autojunk=False).get_matching_blocks():
        if size >= 4:
            for n in range(size): mapping[b+n] = a+n
    anchors = [(float(w['start']), positions[mapping[h]]) for h, w in spans
               if h in mapping and h+1 in mapping and mapping[h] > 0]
    start, char_start, result = 0., 0, []
    while duration-start > 30:
        options = [(t, i) for t, i in anchors if start+10 <= t <= start+25 and i > char_start]
        if not options: raise ValueError('long utterance has no reliable observed alignment split')
        stop, char_end = min(options, key=lambda row: abs(row[0]-start-22))
        result.append(dict(text=source[char_start:char_end], start=start, end=stop))
        start, char_start = stop, char_end
    result.append(dict(text=source[char_start:], start=start, end=duration))
    return result


def align(audio, source, recognition, duration, context=None):
    from app.config.config import ROOT, get_settings
    from app.services.local_asr import preflight, resolve
    from app import paths
    config = get_settings().get_profile().asr
    model, runtime = context or preflight(config)
    worker = ROOT/'bin/local_alignment_worker.py'
    request = {'audio': str(Path(audio).resolve()), 'audio_sha256': file_sha256(audio),
               'model': str(model), 'model_sha256': file_sha256(model/'model_manifest.json'),
               'threads': config.threads, 'windows': windows(source, recognition, duration),
               'runtime': runtime, 'worker_sha256': file_sha256(worker),
               'adapter_sha256': file_sha256(__file__)}
    path = paths.state_dir()/'forced_alignment'/(fingerprint(request)+'.json')
    if path.is_file():
        cached = read(path)
        if cached.get('request') == request and fingerprint(cached['result']) == cached.get('result_sha256'):
            return cached['result'], path
    from app.services.process_lock import file_lock
    with file_lock(path.with_suffix('.lock')):
        with tempfile.TemporaryDirectory(prefix='hv-align-') as tmp:
            inp, out = Path(tmp)/'request.json', Path(tmp)/'result.json'
            atomic_json(inp, request)
            run = subprocess.run([str(resolve(config.python_executable)), str(worker), '--request', str(inp), '--output', str(out)],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=config.timeout_seconds,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if run.returncode: raise RuntimeError('local alignment failed: '+run.stderr[-1600:])
            result = read(out)
        if result['audio_sha256'] != request['audio_sha256'] or file_sha256(audio) != request['audio_sha256']:
            raise ValueError('audio changed during alignment')
        atomic_json(path, dict(request=request, result=result, result_sha256=fingerprint(result)))
    return result, path


def atoms(unit, result, sr):
    source, cursor, rows = unit['text'], 0, []
    for word in result['words']:
        text = word['word']
        if not source.startswith(text, cursor):
            raise ValueError('forced alignment token text differs from source')
        end = cursor+len(text)
        a, b = round(word['start']*sr), round(word['end']*sr)
        if a < 0 or b < a or b > unit['sample_end']-unit['sample_start']+round(.04*sr):
            raise ValueError('forced alignment word time outside audio')
        rows.append(dict(text_start=cursor, text_end=end, sample_start=unit['sample_start']+a,
                         sample_end=min(unit['sample_end'], unit['sample_start']+b), probability=word['probability']))
        cursor = end
    if cursor != len(source): raise ValueError('forced alignment source coverage incomplete')
    return rows


def pack(unit, rows, sr):
    """Optimize line-safe, 0.8–4s groups at acoustic word boundaries.

    Zero-duration punctuation stays with its neighboring word. Display holds
    may occupy existing silence; no estimated word timings are manufactured.
    """
    from app.services.subtitle_layout import measure, validate_cues
    from app.config.config import get_settings
    cfg = get_settings().get_profile().subtitle
    low, high = round(cfg.min_duration_ms*sr/1000), round(cfg.max_duration_ms*sr/1000)
    n = len(rows)
    # Shared display boundaries avoid overlaps. They remain observed word starts.
    times = [r['sample_start'] for r in rows]+[unit['sample_end']]
    if any(a>b for a,b in zip(times,times[1:])): raise ValueError('acoustic alignment is not monotone')
    best = {n: (0., [])}
    for i in range(n-1, -1, -1):
        for j in range(i+1, n+1):
            if j not in best: continue
            text = unit['text'][rows[i]['text_start']:rows[j-1]['text_end']]
            if len(text) > cfg.max_chars_per_line*2: break
            duration = times[j]-times[i]
            if not low <= duration <= high or measure(text)['errors']: continue
            # Prefer clause breaks and ~2.5 seconds; avoid isolated punctuation.
            if not any(c.isalnum() for c in text): continue
            punctuation = text[-1] in '，。！？；：—…、”’）》'
            cost = best[j][0]+1+(duration/sr-2.5)**2*.1+(0 if punctuation else 1.5)
            if i not in best or cost < best[i][0]:
                cue = dict(unit_id=unit['unit_id'], text_start=rows[i]['text_start'], text_end=rows[j-1]['text_end'],
                           text=text, sample_start=times[i], sample_end=times[j],
                           alignment_source='acoustic_source_alignment', timing_review='pending')
                best[i] = (cost, [cue]+best[j][1])
    if 0 not in best: raise ValueError('no readable subtitle grouping fits observed timing for '+unit['unit_id'])
    validate_cues(best[0][1], sr)
    return best[0][1]


def _align_take(args):
    """Single take: recognize + forced align. Pure per-take work; the caller
    merges results in unit order, so output is byte-identical to serial."""
    from app.services.local_asr import recognize

    unit, take, manifest, context = args
    recognized, asr_path = recognize(take['audio_path'], context=context)
    result, result_path = align(take['audio_path'], unit['text'], recognized,
                                take['samples'] / manifest['sample_rate'], context)
    return unit, take, recognized, asr_path, result, result_path


def build(manifest_path, destination):
    from app.services.local_asr import recognize, preflight
    from app.services.subtitle_alignment import validate_cues
    from app.services.audio_disposition import require_usable
    manifest = validate_assembly(manifest_path); require_usable(manifest)
    context = preflight()
    inputs = {str(Path(manifest_path).resolve()): file_sha256(manifest_path), str(Path(__file__).resolve()): file_sha256(__file__)}
    subtitles, words, issues = [], {}, []
    import concurrent.futures
    import os
    try:
        workers = int((os.environ.get('HV_ALIGNMENT_WORKERS') or '').strip() or 0)
    except ValueError:
        workers = 0
    if workers <= 0:
        workers = max(1, min(4, os.cpu_count() or 2))
    pairs = [(u, t, manifest, context)
             for u, t in zip(manifest['alignment'], manifest['takes'])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        aligned = list(pool.map(_align_take, pairs))
    for unit, take, recognized, asr_path, result, result_path in aligned:
        for path in (asr_path, result_path): inputs[str(path.resolve())] = file_sha256(path)
        rows = atoms(dict(unit, sample_end=unit['sample_start']+take['samples']), result, manifest['sample_rate'])
        # The delivered master can include exact, separately recorded pauses.
        insertions = manifest.get('pause_insertions', {}).get(unit['unit_id'], [])
        for row in rows:
            for key in ('sample_start', 'sample_end'):
                original = row[key]-unit['sample_start']
                row[key] += sum(p['samples'] for p in insertions if original >= p['at_sample'])
        words[unit['unit_id']] = rows
        subtitles.extend(pack(unit, rows, manifest['sample_rate']))
        if take['quality']['status'] != 'passed': issues.append(dict(kind='transcript_quality_pending', unit_id=unit['unit_id']))
    validate_cues(manifest, subtitles)
    record = {'manifest_path': str(Path(manifest_path).resolve()), 'manifest_content_sha256': fingerprint(manifest),
              'master_sha256': manifest['master_sha256'], 'sample_rate': manifest['sample_rate'],
              'method': 'acoustic_source_alignment_v1', 'status': 'machine_validated',
              'source_coverage_complete': True, 'listening_approved': False,
              'inputs': inputs, 'subtitles': subtitles, 'words': words, 'issues': issues}
    path = Path(destination)/(fingerprint(record)+'.json'); atomic_json(path, record)
    return path


def require_evidence(manifest, timeline):
    """Machine timing may create a review candidate; it never grants publication."""
    path = timeline.get('alignment_evidence_path')
    if not path: raise ValueError('missing acoustic alignment evidence')
    record = read(path)
    if record.get('method') != 'acoustic_source_alignment_v1' or record.get('status') != 'machine_validated':
        raise ValueError('alignment is not machine validated')
    if record['manifest_content_sha256'] != fingerprint(manifest) or record['subtitles'] != timeline['subtitles']:
        raise ValueError('alignment evidence does not bind this timeline')
    if any(file_sha256(p) != sha for p, sha in record['inputs'].items()): raise ValueError('alignment evidence changed')
    from app.services.subtitle_alignment import validate_cues
    from app.services.subtitle_layout import validate_cues as validate_layout
    validate_cues(manifest, record['subtitles']); validate_layout(record['subtitles'], manifest['sample_rate'])
    return {str(Path(path).resolve()): file_sha256(path), **record['inputs']}
