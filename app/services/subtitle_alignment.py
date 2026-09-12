"""Conservative subtitle drafts from independently recognized word boundaries.

No word is split in time, and no timing is estimated from character counts.
Unmatched source text remains an explicit issue and cannot pass review validation.
"""
import difflib
import re
import uuid
from pathlib import Path
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read, validate_assembly


def symbols(text):
    from pypinyin import lazy_pinyin, Style
    from app.services.asr_check import normalize_numbers
    text = text.translate(str.maketrans('萬億兩', '万亿两'))
    positions=[];values=[]
    for match in re.finditer(r'[0-9零〇一二两三四五六七八九十百千万亿]+(?:[点.][0-9零〇一二两三四五六七八九]+)?[万亿]?|[^\W_]',text):
        value=match.group();positions.append(match.start())
        normalized=normalize_numbers(value)
        values.append('#'+normalized if re.fullmatch(r'\d+(?:\.\d+)?',normalized) else
                      lazy_pinyin(value,style=Style.TONE3,neutral_tone_with_five=True)[0].lower())
    return values,positions


def phrase_boundaries(source):
    """Use source punctuation, keeping book titles and closing marks intact."""
    ends=[];book_depth=0
    for i,c in enumerate(source):
        if c=='《':book_depth+=1
        elif c=='》':book_depth=max(0,book_depth-1)
        if not book_depth and c in '，。！？；：—…':
            end=i+1
            while end<len(source) and source[end] in '，。！？；：—…”’）》':end+=1
            if not ends or end>ends[-1]:ends.append(end)
    if not ends or ends[-1]!=len(source):ends.append(len(source))
    return ends


def phrase_cues(source,atoms,issues):
    # A word crossing punctuation keeps its complete time interval: merge the
    # neighboring clauses instead of inventing a split inside that word.
    ends=[end for end in phrase_boundaries(source)
          if not any(a['text_start']<end<a['text_end'] for a in atoms)]
    cues=[];start=0
    for end in ends:
        rows=[a for a in atoms if start<=a['text_start'] and a['text_end']<=end]
        ordered=all(a['sample_end']<=b['sample_start'] for a,b in zip(rows,rows[1:]))
        if rows and ordered and rows[0]['text_start']==start and rows[-1]['text_end']==end:
            cue=dict(rows[0]);cue.update(text_end=end,text=source[start:end],sample_end=rows[-1]['sample_end'])
            if any(a['text_end']!=b['text_start'] for a,b in zip(rows,rows[1:])):
                # The subtitle covers a whole audible phrase, not claimed word
                # times for the unmatched interior. Explicit listening review remains required.
                cue['alignment_source']='asr_phrase_span_draft'
                issues.append({'kind':'phrase_contains_unmatched_source','text':source[start:end]})
            if len(cue['text'])>32:issues.append({'kind':'long_phrase','text':cue['text']})
            cues.append(cue)
        else:
            # Missing phrase endpoints or invalid timing remain incomplete.
            for row in rows:
                if cues and cues[-1]['text_end']==row['text_start'] and cues[-1]['sample_end']<=row['sample_start'] and cues[-1]['text_start']>=start:
                    cues[-1].update(text_end=row['text_end'],text=source[cues[-1]['text_start']:row['text_end']],sample_end=row['sample_end'])
                else:cues.append(dict(row))
        start=end
    return cues


def align_unit(unit, recognition, sample_rate):
    source=unit['text']; reference,positions=symbols(source)
    words=[w for segment in recognition['segments'] for w in segment.get('words',[])]
    heard=[]; spans=[]
    for word in words:
        normalized,_=symbols(word['word']);a=len(heard);heard.extend(normalized)
        spans.append((a,len(heard),word))
    mapping={};issues=[]
    for tag,a,b,c,d in difflib.SequenceMatcher(None,reference,heard,autojunk=False).get_opcodes():
        if tag=='equal':mapping.update({c+i:a+i for i in range(b-a)})
        else:issues.append({'kind':'text_mismatch','source_symbol_range':[a,b],
                            'heard_symbol_range':[c,d],'operation':tag})
    atoms=[]
    for a,b,word in spans:
        if a==b:continue
        mapped=[mapping.get(i) for i in range(a,b)]
        if any(i is None for i in mapped) or mapped!=list(range(mapped[0],mapped[0]+len(mapped))):
            continue
        i=0 if mapped[0]==0 else positions[mapped[0]]
        j=positions[mapped[-1]+1] if mapped[-1]+1<len(positions) else len(source)
        start=unit['sample_start']+round(word['start']*sample_rate)
        end=unit['sample_start']+round(word['end']*sample_rate)
        if not unit['sample_start']<=start<end<=unit['sample_end']:
            issues.append({'kind':'invalid_word_time','text':word['word']});continue
        if word.get('probability',0)<.55:
            issues.append({'kind':'low_word_confidence','text':source[i:j],'probability':word.get('probability')})
        atoms.append({'unit_id':unit['unit_id'],'text_start':i,'text_end':j,'text':source[i:j],
            'sample_start':start,'sample_end':end,'alignment_source':'asr_word_timestamps_draft'})
    cues=phrase_cues(source,atoms,issues)
    cursor=0;previous_end=unit['sample_start']
    for cue in cues:
        if cue['text_start']!=cursor:issues.append({'kind':'unmapped_source','range':[cursor,cue['text_start']]})
        if cue['sample_start']<previous_end:issues.append({'kind':'overlapping_word_times'})
        cursor=cue['text_end'];previous_end=cue['sample_end']
    if cursor!=len(source):issues.append({'kind':'unmapped_source','range':[cursor,len(source)]})
    return cues,issues


def validate_cues(manifest,cues):
    units={u['unit_id']:u for u in manifest['alignment']}
    cursors={k:0 for k in units};end=0
    for cue in cues:
        unit=units[cue['unit_id']]
        i,j=cue['text_start'],cue['text_end'];a,b=cue['sample_start'],cue['sample_end']
        if any(type(x)is not int for x in (i,j,a,b)):raise ValueError('alignment boundaries must be integers')
        if i!=cursors[cue['unit_id']] or not i<j<=len(unit['text']) or cue['text']!=unit['text'][i:j]:
            raise ValueError('alignment source omission, duplication or rewrite')
        if not unit['sample_start']<=a<b<=unit['sample_end'] or a<end:
            raise ValueError('alignment time overlaps or lies outside the utterance')
        cursors[cue['unit_id']]=j;end=b
    if any(cursors[k]!=len(u['text']) for k,u in units.items()):raise ValueError('alignment source coverage incomplete')


def build(manifest_path,destination):
    from app.services.local_asr import recognize
    manifest=validate_assembly(manifest_path)
    cues=[];issues=[];inputs={str(Path(manifest_path).resolve()):file_sha256(manifest_path),
                            str(Path(__file__).resolve()):file_sha256(__file__)}
    for unit,take in zip(manifest['alignment'],manifest['takes']):
        from app.services.audio_disposition import take_rejected
        if take_rejected(take):issues.append({'kind':'audio_rejected','unit_id':unit['unit_id']})
        result,path=recognize(take['audio_path'])
        inputs[str(path.resolve())]=file_sha256(path)
        rows,findings=align_unit(unit,result,manifest['sample_rate'])
        cues.extend(rows);issues.extend(dict(f,unit_id=unit['unit_id']) for f in findings)
    try:validate_cues(manifest,cues);complete=True
    except (ValueError,KeyError):complete=False
    folder=Path(destination)/uuid.uuid4().hex
    draft={'manifest_path':str(Path(manifest_path).resolve()),'manifest_content_sha256':fingerprint(manifest),
        'master_sha256':manifest['master_sha256'],'sample_rate':manifest['sample_rate'],
        'status':'needs_review','source_coverage_complete':complete,'issues':issues,'inputs':inputs,'subtitles':cues}
    atomic_json(folder/'alignment.draft.json',draft)
    def stamp(sample):
        millis=round(sample*1000/manifest['sample_rate']);sec,ms=divmod(millis,1000);mins,sec=divmod(sec,60);hours,mins=divmod(mins,60)
        return f'{hours:02}:{mins:02}:{sec:02},{ms:03}'
    (folder/'subtitles.draft.srt').write_text('\n\n'.join(f"{i}\n{stamp(c['sample_start'])} --> {stamp(c['sample_end'])}\n{c['text']}"
        for i,c in enumerate(cues,1))+'\n',encoding='utf-8')
    from app.services.alignment_review_page import create
    create(folder/'alignment.draft.json')
    return folder/'alignment.draft.json'


def review(draft_path,reviewer,findings,decision):
    from datetime import datetime,timezone
    if not reviewer.strip() or not findings.strip() or decision not in ('approved','rejected'):
        raise ValueError('explicit reviewer, findings and decision required')
    draft=read(draft_path);manifest=validate_assembly(draft['manifest_path'])
    if fingerprint(manifest)!=draft['manifest_content_sha256']:raise ValueError('alignment manifest changed')
    if any(file_sha256(p)!=sha for p,sha in draft['inputs'].items()):raise ValueError('alignment input changed')
    if decision=='approved':
        from app.services.audio_disposition import require_usable
        require_usable(manifest)
        validate_cues(manifest,draft['subtitles'])
    record={'reviewer':reviewer,'findings':findings,'decision':decision,
        'reviewed_at':datetime.now(timezone.utc).isoformat(),'scope':'subtitle text coverage and audible phrase timing',
        'draft_path':str(Path(draft_path).resolve()),'draft_sha256':file_sha256(draft_path),
        'manifest_content_sha256':fingerprint(manifest),'subtitles_sha256':fingerprint(draft['subtitles'])}
    path=Path(draft_path).parent/('alignment-review-'+uuid.uuid4().hex+'.json');atomic_json(path,record)
    return path


def require_review(manifest,timeline):
    path=timeline.get('alignment_review_path')
    if not path:raise ValueError('phrase timing needs a hash-bound alignment review record')
    from app.services.audio_disposition import require_usable
    require_usable(manifest)
    record=read(path)
    if record.get('decision')!='approved' or not record.get('reviewer') or not record.get('reviewed_at'):
        raise ValueError('alignment has no explicit human approval')
    if record.get('manifest_content_sha256')!=fingerprint(manifest):raise ValueError('alignment approval is stale')
    if file_sha256(record['draft_path'])!=record['draft_sha256']:raise ValueError('alignment draft changed after review')
    draft=read(record['draft_path'])
    if fingerprint(timeline['subtitles'])!=record['subtitles_sha256']:
        raise ValueError('subtitle times or text changed after review')
    if any(file_sha256(p)!=sha for p,sha in draft['inputs'].items()):raise ValueError('alignment evidence changed')
    return {str(Path(path).resolve()):file_sha256(path),record['draft_path']:record['draft_sha256'],**draft['inputs']}
