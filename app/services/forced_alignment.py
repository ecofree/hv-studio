"""Source-complete acoustic subtitle timing, separate from transcript QA."""
import difflib
from pathlib import Path

from app.services.acoustic_session import job_session
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


def align(audio, source, recognition, duration, context=None, timeout_seconds=None, prior_audio=None):
    from app.config.config import ROOT,get_settings
    from app.services import local_asr as asr,model_resources as resources
    from app.services.acoustic_session import job_session,infer
    from app.services.production_metrics import count
    from app import paths
    config=get_settings().get_profile().asr
    timeout=resources.timeout_for(audio,config.timeout_seconds) if timeout_seconds is None else timeout_seconds
    if type(timeout) is not int or timeout<1:raise ValueError('alignment timeout must be a positive integer')
    model,runtime=context or asr.preflight(config)
    worker=file_sha256(ROOT/'bin/local_alignment_worker.py')
    loader=file_sha256(ROOT/'bin/local_asr_worker.py')
    request=dict(audio_sha256=file_sha256(audio),model_sha256=file_sha256(model/'model_manifest.json'),
                 windows=windows(source,recognition,duration),runtime=runtime,threads=resources.threads(config.threads),
                 worker_sha256=worker,loader_sha256=loader,device='cpu',compute_type='int8')
    folder=paths.state_dir()/'forced_alignment'
    path=folder/(fingerprint(request)+'.json')

    def cached(target,expected):
        if not target.is_file():return None
        row=read(target)
        result=row['result']
        if (row.get('request')!=expected or fingerprint(result)!=row.get('result_sha256')
                or result.get('audio_sha256')!=request['audio_sha256'] or result.get('windows')!=request['windows']):
            raise ValueError('invalid acoustic alignment cache')
        return result

    result=cached(path,request)
    if result is not None:
        count('alignment_cache_hits')
        return result,path
    if (worker,loader)==(asr.RESIDENT_ALIGNMENT_WORKER,asr.RESIDENT_ASR_WORKER):
        for old_audio in dict.fromkeys(str(Path(p).resolve()) for p in (audio,prior_audio) if p):
            previous=dict(audio=old_audio,audio_sha256=request['audio_sha256'],model=str(model),
                          model_sha256=request['model_sha256'],threads=request['threads'],windows=request['windows'],
                          runtime=runtime,worker_sha256=asr.OLD_ALIGNMENT_WORKER,adapter_sha256=asr.OLD_ALIGNMENT_ADAPTER)
            origin=folder/(fingerprint(previous)+'.json')
            result=cached(origin,previous)
            if result is not None:
                count('alignment_legacy_hits')
                atomic_json(path,dict(request=request,result=result,result_sha256=fingerprint(result),
                                      prior_receipt_sha256=file_sha256(origin)))
                return result,path
    with job_session() as session:
        session.acquire()
        result=cached(path,request)
        if result is not None:
            count('alignment_cache_hits')
            return result,path
        result=infer(config,model,dict(operation='align',audio=str(Path(audio).resolve()),windows=request['windows']),timeout)
        if (result['audio_sha256']!=request['audio_sha256'] or file_sha256(audio)!=request['audio_sha256']
                or result['windows']!=request['windows']):
            raise ValueError('audio or windows changed during alignment')
        atomic_json(path,dict(request=request,result=result,result_sha256=fingerprint(result)))
        count('alignment_inferences')
        return result,path


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
    # A take can retain silence or an independently spoken heading after the
    # last source word. Do not force the preceding subtitle across that tail.
    terminal = unit['sample_end']
    if rows and terminal-rows[-1]['sample_end'] > high:
        terminal = min(terminal, rows[-1]['sample_end']+round(.2*sr))
    times = [r['sample_start'] for r in rows]+[terminal]
    if any(a>b for a,b in zip(times,times[1:])): raise ValueError('acoustic alignment is not monotone')
    # Consider candidates in the same cost order as the exhaustive DP. Only
    # measure glyphs until the cheapest feasible edge is found; j breaks ties.
    best = {n:(0.,n)}
    limit = cfg.max_chars_per_line*2
    for i in range(n-1,-1,-1):
        options=[]
        for j in range(i+1,n+1):
            start,end=rows[i]['text_start'],rows[j-1]['text_end']
            duration=times[j]-times[i]
            if end-start>limit or duration>high:break
            if j not in best or duration<low:continue
            text=unit['text'][start:end]
            if not any(c.isalnum() for c in text):continue
            punctuation=text[-1] in '，。！？；：—…、”’）》'
            cost=best[j][0]+1+(duration/sr-2.5)**2*.1+(0 if punctuation else 1.5)
            options.append((cost,j,text))
        for cost,j,text in sorted(options):
            if not measure(text)['errors']:
                best[i]=(cost,j)
                break
    if 0 not in best: raise ValueError('no readable subtitle grouping fits observed timing for '+unit['unit_id'])
    cues=[];i=0
    while i<n:
        j=best[i][1]
        start,end=rows[i]['text_start'],rows[j-1]['text_end']
        cues.append(dict(unit_id=unit['unit_id'],text_start=start,text_end=end,text=unit['text'][start:end],
                         sample_start=times[i],sample_end=times[j],alignment_source='acoustic_source_alignment',
                         timing_review='pending'))
        i=j
    validate_cues(cues,sr)
    return cues


def _align_take(args):
    """Single take: recognize + forced align. Pure per-take work; the caller
    merges results in unit order, so output is byte-identical to serial."""
    from app.services.local_asr import recognize

    unit, take, manifest, context, parent = args
    if parent:
        from app.services.recognition_cache import slice_cached
        recognized,asr_path=slice_cached(manifest['master_path'],take['audio_path'],unit['sample_start'],
            unit['sample_start']+take['samples'],parent,Path(parent).parent/'slices')
    else:
        recognized, asr_path = recognize(take['audio_path'], context=context)
    result, result_path = align(take['audio_path'], unit['text'], recognized,
                                take['samples'] / manifest['sample_rate'], context,
                                **({'prior_audio':take['prior_audio_path']} if take.get('prior_audio_path') else {}))
    return unit, take, recognized, asr_path, result, result_path


def _word_clock(unit,take,manifest,result):
    rows = atoms(dict(unit, sample_end=unit['sample_start']+take['samples']), result, manifest['sample_rate'])
    # The delivered master can include exact, separately recorded pauses.
    insertions = manifest.get('pause_insertions', {}).get(unit['unit_id'], [])
    for row in rows:
        for key in ('sample_start', 'sample_end'):
            original = row[key]-unit['sample_start']
            row[key] += sum(p['samples'] for p in insertions if original >= p['at_sample'])
    return rows


@job_session()
def build(manifest_path, destination, *, recognition_parent=None, progress=None):
    from app.services.local_asr import recognize, preflight
    from app.services.subtitle_alignment import validate_cues
    from app.services.audio_disposition import require_usable
    manifest = validate_assembly(manifest_path); require_usable(manifest)
    context = preflight()
    inputs = {str(Path(manifest_path).resolve()): file_sha256(manifest_path), str(Path(__file__).resolve()): file_sha256(__file__)}
    subtitles, words, transcript_review = [], {}, []
    # One model lease at a time. Do not expose a worker count we silently ignore.
    pairs = [(u, t, manifest, context, recognition_parent)
             for u, t in zip(manifest['alignment'], manifest['takes'])]
    flagged=set(manifest.get('alignment_review',()))
    pairs.sort(key=lambda pair: pair[0]['unit_id'] not in flagged)
    for i, args in enumerate(pairs):
        unit, take, recognized, asr_path, result, result_path = _align_take(args)
        if progress: progress(dict(done=i+1, total=len(pairs), message='字幕声学对齐'))
        for path in (asr_path, result_path): inputs[str(path.resolve())] = file_sha256(path)
        rows=_word_clock(unit,take,manifest,result)
        cues=pack(unit,rows,manifest['sample_rate'])
        words[unit['unit_id']]=rows
        subtitles.extend(cues)
        if take['quality']['status'] != 'passed': transcript_review.append(unit['unit_id'])
    subtitles.sort(key=lambda cue:cue['sample_start'])
    order={u['unit_id']:i for i,u in enumerate(manifest['alignment'])}
    transcript_review.sort(key=order.__getitem__)
    validate_cues(manifest, subtitles)
    record = {'manifest_path': str(Path(manifest_path).resolve()), 'manifest_content_sha256': fingerprint(manifest),
              'master_sha256': manifest['master_sha256'], 'sample_rate': manifest['sample_rate'],
              'method': 'acoustic_source_alignment_v1', 'status': 'machine_validated',
              'source_coverage_complete': True, 'listening_approved': False,
              'inputs': inputs, 'subtitles': subtitles, 'words': words, 'issues': [],
              'transcript_review_units': transcript_review}
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
    inputs=dict(record['inputs'])
    adapter=str(Path(__file__).resolve())
    mismatches=[p for p,h in inputs.items() if file_sha256(p)!=h]
    if mismatches:
        from app.services.local_asr import OLD_ALIGNMENT_ADAPTER
        if mismatches!=[adapter] or inputs[adapter] not in (OLD_ALIGNMENT_ADAPTER, 'd02684db3325b2c6f39335127e9798500d2de13ebd12552062520d15c11c352a'):
            raise ValueError('alignment evidence changed')
        # An old adapter receipt remains verifiable from its checked raw acoustic
        # responses. Require identical reconstructed words and cues under this code.
        by_content={}
        for p in inputs:
            if Path(p).parent.name=='forced_alignment':
                receipt=read(p); result=receipt['result']
                if fingerprint(result)!=receipt['result_sha256']:raise ValueError('alignment response changed')
                text=''.join(w['text'] for w in result['windows'])
                by_content[(result['audio_sha256'],text)]=result
        saved={}
        for cue in record['subtitles']:saved.setdefault(cue['unit_id'],[]).append(cue)
        for unit,take in zip(manifest['alignment'],manifest['takes']):
            result=by_content.get((take['audio_sha256'],unit['text']))
            if result is None:raise ValueError('missing archived word evidence')
            words=_word_clock(unit,take,manifest,result)
            if words!=record['words'][unit['unit_id']]:raise ValueError('archived word clock changed')
            times={w['text_start']:w['sample_start'] for w in words}
            times[len(unit['text'])]=unit['sample_end']
            for cue in saved.get(unit['unit_id'],[]):
                if (times.get(cue['text_start'])!=cue['sample_start'] or times.get(cue['text_end'])!=cue['sample_end']):
                    raise ValueError('archived subtitle clock changed')
        inputs[adapter]=file_sha256(adapter)
    from app.services.subtitle_alignment import validate_cues
    from app.services.subtitle_layout import validate_cues as validate_layout
    validate_cues(manifest, record['subtitles']); validate_layout(record['subtitles'], manifest['sample_rate'])
    return {str(Path(path).resolve()): file_sha256(path), **inputs}
