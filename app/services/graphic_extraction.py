"""Propose editable graphics from source text or OCR. Keep the evidence behind each row."""
import hashlib
import re
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation

from app import paths
from app.services import studio_graphics as G, studio_workflow as workflow
from app.services.onscreen_tables import find_tables
from app.services.provenance import file_sha256, replace_file


DIGITS = dict(zip('零一二三四五六七八九',range(10)),〇=0,两=2,兩=2)
SMALL = {'十':10,'百':100,'千':1000}
BIG = {'万':10000,'萬':10000,'亿':100000000,'億':100000000}
CN = '零〇一二三四五六七八九十百千万萬亿億两兩'
NUM = rf'(?:\d+(?:,\d{{3}})*(?:\.\d+)?(?:[万萬亿億])?|[{CN}]+)'
UNIT = r'(?:平方公里|平方米|公里|厘米|千克|公斤|公顷|千米|斤|两|兩|贯|缗|钱|枚|匹|头|根|亩|石|斛|斗|户|戶|口|丁|人|座|名|个|艘|册|件|吨|米|元|[%％㎡]|km[2²]|m[2²])'
PRE = r'(?:(?:大约|约有|大約|接近|超过|将近|至少|至多|不足|近|约|約|逾|仅|负|[≈<>≤≥−-])\s*)?'
MEASURE = re.compile(rf'{PRE}{NUM}\s*(?:(?:至|到|[–—~～\-])\s*{NUM}\s*)?{UNIT}(?:\s*[/／]\s*(?:匹|头|根|人|户|口|枚|亩|年|座))?')
REF = re.compile(r'《[^》\n]{1,55}》')
SOURCE = re.compile(r'出处|出處|史料|考古来源|考古來源|来源|來源|source|reference',re.I)


def _plain(value):
    return unicodedata.normalize('NFKC',value).strip().replace('，',',')


def _number(text):
    if text[:1] in BIG: raise ValueError('missing numeral')
    for mark in BIG:
        if text.endswith(mark) and re.fullmatch(r'\d[\d,.]*',text[:-1]):
            return Decimal(text[:-1].replace(',',''))*BIG[mark]
    if re.fullmatch(r'\d[\d,.]*',text):
        return Decimal(text.replace(',',''))
    if all(c in DIGITS for c in text):
        return Decimal(''.join(str(DIGITS[c]) for c in text))
    total = section = digit = 0
    for c in text:
        if c in DIGITS: digit=DIGITS[c]
        elif c in SMALL: section+=(digit or 1)*SMALL[c]; digit=0
        elif c in ('万','萬'): total+=(section+digit)*10000; section=digit=0
        elif c in ('亿','億'): total=(total+section+digit)*100000000; section=digit=0
        else: raise ValueError('unknown numeral')
    return Decimal(total+section+digit)


def _header_unit(header):
    found=re.search(r'\(([^()]+)\)\s*$',_plain(header))
    return found[1].strip() if found else ''


def scalar(raw,header=''):
    """Only comparable, exact, single measurements qualify for bars."""
    text=_plain(raw).replace(' ','')
    match=re.fullmatch(rf'({NUM})({UNIT}(?:/[^\s/]+)?)',text)
    if not match and re.fullmatch(NUM,text):
        match=re.fullmatch(rf'({NUM})()',text)
    if not match: return None
    header_unit=_header_unit(header)
    unit=match[2] or header_unit
    if not unit: return None
    scale=1
    if unit[0] in BIG:
        scale=BIG[unit[0]]
        unit=unit[1:]
    if not re.fullmatch(rf'{UNIT}(?:/[\w千每]+)?',unit): return None
    if match[2] and header_unit and header_unit.lstrip('万萬亿億')!=unit: return None
    try: value=_number(match[1])*scale
    except (InvalidOperation,ValueError): return None
    return value,unit.replace('兩','两').replace('戶','户').replace('km2','平方公里').replace('m2','平方米')


def clauses(document):
    result=[]
    for u in document['units']:
        text=u['source_text']
        # Commas inside grouped Arabic digits are part of the number.
        for m in re.finditer(r'(?:[^。！？；;，,\n]|(?<=\d)[,，](?=\d{3}(?:\D|$)))+',text):
            line=m[0].strip()
            if line:
                i=m.start()+len(m[0])-len(m[0].lstrip())
                result.append(dict(quote=line,unit_id=u['unit_id'],text_start=i))
    return result


def _amounts(text):
    # Match in the original string so the displayed evidence retains its exact glyphs.
    found=list(MEASURE.finditer(text))
    return [m for m in found if not text[:m.start()].rstrip().endswith('每')]


def _source_col(headers):
    return next((i for i,h in enumerate(headers) if SOURCE.search(h)),None)


def _item(head,rows, *, boxes=None,where=None):
    return dict(columns=head,rows=rows,boxes=boxes or [],region=where or [])


def from_script(raw,document):
    """Read authored Markdown/TSV tables as well as quantities in spoken prose."""
    from app.services.studio_graphics import parse_grid
    sheets=[]
    body_start=raw.find(document.get('source',''))
    for span in find_tables(raw):
        data=parse_grid(raw[span['data_start']:span['end']])
        if len(data)<2 or any(len(row)!=len(data[0]) for row in data): continue
        sheet=_item(data[0],data[1:])
        preceding=[u for u in document['units'] if body_start>=0 and u.get('source_map') and body_start+u['source_map'][-1]<span['start']]
        sheet.update(title=span['title'],script_span=[span['start'],span['end']],
                     near_unit=preceding[-1]['unit_id'] if preceding else None)
        sheets.append(sheet)
    records=[]
    for c in clauses(document):
        text=c['quote']
        matches=_amounts(text)
        if not matches: continue
        first,last=matches[0],matches[-1]
        # Adjacent units, ranges and multiple measures retain the complete expression.
        amount=text[first.start():last.end()]
        label=text[:first.start()].strip(' ：:，,') or text
        refs=' / '.join(REF.findall(text))
        records.append((label,amount,refs,c))
    if records:
        sheet=_item(['旁白项目','记载数值','原句列明的出处'],[list(row[:3]) for row in records])
        sheet['matches']=[r[3] for r in records]
        sheet['prose']=True
        sheets.append(sheet)
    return sheets


def from_ocr(scan):
    sheets=[]
    for s in scan['sections']:
        data=s['rows']
        if len(data)>=2 and len(data[0])>=2:
            sheet=_item(data[0],data[1:],boxes=s['boxes'][1:],where=s['region'])
            sheet['header_boxes']=s['boxes'][0]
        else:
            sheet=_item(['识别行','原文'],[[str(i+1),' '.join(row)] for i,row in enumerate(data)],
                        boxes=[[None,next((b for b in cells if b),None)] for cells in s['boxes']],where=s['region'])
        sheets.append(sheet)
    return sheets


def _match(row,headers,spoken,source):
    label=row[0].strip()
    numeric=set()
    for i,cell in enumerate(row[1:],1):
        if i!=source:
            value=scalar(cell,headers[i])
            if value: numeric.add(value)
    scores=[]
    for c in spoken:
        line=c['quote']
        main=re.split(r'[（(]',label)[0].strip()
        has_name=len(main)>=2 and (main in line or label in line)
        seen={scalar(m[0]) for m in _amounts(line)}-{None}
        same=numeric & seen
        literal=any(len(v)>=2 and v in line and not re.search(r'\d',v) and not _amounts(v)
                    for i,v in enumerate(row[1:],1) if i!=source)
        if numeric and seen and not same:
            continue
        if not has_name and not literal: continue
        score=(4 if has_name else 0)+(3 if same else 0)+(1 if literal else 0)
        if score: scores.append((score,c))
    if not scores: return None
    best=max(n for n,c in scores)
    top=[c for n,c in scores if n==best]
    return top[0] if len(top)==1 else None


def _recommend(sheet,indices):
    headers=sheet['columns']
    if SOURCE.search(headers[0]) or sum(not SOURCE.search(h) for h in headers)!=2: return None,[]
    candidates=[]
    for i,h in enumerate(headers[1:],1):
        if SOURCE.search(h) or re.search(r'约数|估计|估算|上限|范围|区间|下限',h): continue
        values=[scalar(sheet['rows'][n][i],h) for n in indices]
        if all(values) and len({v[1] for v in values})==1:
            contexts=[' '.join(val for j,val in enumerate(sheet['rows'][n]) if not SOURCE.search(headers[j])) for n in indices]
            bases=[re.findall(r'每(?:[\d一二三四五六七八九十百千,]+)?[枚户口斤亩匹头丁人]',s) for s in contexts]
            if any(b!=bases[0] for b in bases) or any(re.search(r'估算|估计|约数|推测|上限|下限|不详',s) for s in contexts):
                continue
            if sheet.get('prose'):
                criteria=[re.findall(r'每[一二三四五六七八九十百千\d]+[枚户口斤]|重|面积|仓窖|户数|人口',sheet['rows'][n][0]) for n in indices]
                if not criteria[0] or any(c!=criteria[0] for c in criteria): continue
            candidates.append((i,values))
    return candidates[0] if len(candidates)==1 and len(indices)>1 else (None,[])


def correct_sheet(sheet,grid):
    values=G.parse_grid(grid)
    if len(values)!=1+len(sheet['rows']) or any(len(row)!=len(sheet['columns']) for row in values):
        raise ValueError('校订时请保持行列数量与原图一致，以便追溯单元格的位置')
    return dict(sheet,raw_columns=sheet['columns'],raw_rows=sheet['rows'],columns=values[0],rows=values[1:])


def _row_check(row,headers,link,spoken,source_col):
    cells=[v for i,v in enumerate(row) if i!=source_col]
    notes=[]
    values={scalar(v,headers[i]) for i,v in enumerate(row) if i!=source_col}-{None}
    if any(re.search(r'约|約|近|至少|至多|不足|超过|[\d一二两三四五六七八九十][%％\s]*[–—~～至→-]',v) for v in cells):
        notes.append('有范围、约数或上下限，保留原文')
    if link and values:
        heard={scalar(m[0]) for m in _amounts(link['quote'])}-{None}
        notes.append('数值及单位在原句中对应' if values<=heard else '已定位原句，数值或单位需比对')
    elif not link:
        related=[c['quote'] for c in spoken if len(row[0])>=2 and row[0] in c['quote']]
        if len(related)==1 and values:
            notes.append('请与这句对读：'+related[0])
        else:
            notes.append('旁白中未找到唯一对应，本行可只上屏')
    else:
        notes.append('按原句核对复合表达')
    if source_col is None or not row[source_col].strip():
        notes.append('该行未列史料出处')
    elif link:
        cited=set(REF.findall(link['quote']))
        listed=set(REF.findall(row[source_col]))
        if cited and listed: notes.append('原句与本行书名一致' if cited==listed else '旁白与本行所列书名有差异')
    return '。'.join(notes)


def propose(sheet,document,indices,columns=None,*,origin,sha,engine='',as_table=False):
    if not indices or len(indices)>48 or len(set(indices))!=len(indices):
        raise ValueError('请选择 1–48 行资料')
    cols=list(range(len(sheet['columns']))) if columns is None else list(columns)
    if len(cols)<2 or len(cols)>5 or len(set(cols))!=len(cols): raise ValueError('请选择 2–5 个上屏列')
    headers=[sheet['columns'][i] for i in cols]
    headers=[h.strip() or f'列 {i+1}' for i,h in zip(cols,headers)]
    original=[sheet['rows'][n] for n in indices]
    source_col=sheet.get('source_column',_source_col(sheet['columns']))
    compare_headers=['出处' if c==source_col else h for c,h in zip(cols,headers)]
    visible=dict(columns=compare_headers,rows=[[row[i] for i in cols] for row in original],prose=sheet.get('prose'))
    num,values=_recommend(visible,range(len(original)))
    bars=num is not None and not as_table
    rows=([[row[0],format(q[0],'f'),row[num]] for row,q in zip(visible['rows'],values)]
          if bars else visible['rows'])
    references=list(dict.fromkeys(row[source_col] for row in original if source_col is not None and row[source_col].strip()))
    if len(references)==1: source=references[0]
    elif references and not bars and source_col in cols: source='资料出处见表内「'+sheet['columns'][source_col]+'」列'
    else: source=' / '.join(references)
    spoken=clauses(document)
    matches=[sheet['matches'][i] for i in indices] if 'matches' in sheet else [_match(row,sheet['columns'],spoken,source_col) for row in original]
    order={u['unit_id']:i for i,u in enumerate(document['units'])}
    previous=(-1,-1)
    warnings=[]
    linked=[]
    for n,m in enumerate(matches):
        if m:
            try:
                G.locate(document,m['quote'])
                pos=(order[m['unit_id']],m['text_start'])
                if pos<=previous: m=None
                else: previous=pos
            except ValueError: m=None
        linked.append(m)
    if not source: warnings.append('资料没有明确出处，请填写可追溯的来源')
    evidence=dict(origin=origin,sha256=sha,engine=engine,columns=sheet['columns'],rows=original,
                  header_boxes=sheet.get('header_boxes',[]),boxes=[sheet['boxes'][i] for i in indices] if sheet['boxes'] else [],
                  region=sheet['region'],source_column=source_col,links=linked,source_identity=G.source_key(document),
                  checks=[_row_check(row,sheet['columns'],link,spoken,source_col) for row,link in zip(original,linked)])
    if 'raw_rows' in sheet:
        evidence.update(raw_columns=sheet['raw_columns'],raw_rows=[sheet['raw_rows'][n] for n in indices])
    if 'script_span' in sheet: evidence['script_span']=sheet['script_span']
    assigned=[m for m in linked if m]
    first=sheet.get('near_unit') or (assigned[0]['unit_id'] if assigned else document['units'][0]['unit_id'])
    last=assigned[-1]['unit_id'] if assigned and not sheet.get('near_unit') else first
    return dict(kind='bars' if bars else 'table',title=sheet.get('title') or (headers[num] if bars else ' · '.join(headers[:2])),
                columns=[] if bars else headers,rows=rows,unit=values[0][1] if bars else '',source=source,
                first_unit=first,last_unit=last,start_quote='',stop_before='',timing='span',quotes=[],evidence=evidence,
                warnings=warnings,recommendation='各行数值精确且口径一致' if bars else '按原列保留数值、范围与单位')


def script_input(eid):
    from app.managers import load
    from app.orchestrator import _load_script_md
    from app.services.studio_sources import contract
    from app.services.studio_workspace import asset_path
    bound=contract(eid)
    if bound:
        path=asset_path(bound['revision'])/'source.md'
        expected=next(row['sha256'] for row in bound['inputs'] if row['role']=='authoring')
        if file_sha256(path)!=expected: raise ValueError('所选原稿快照已变化，请核对绑定的版本')
        return path.read_text(encoding='utf-8')
    return _load_script_md(load(eid))


def original_path(eid,digest):
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest): raise ValueError('截图指纹无效')
    return paths.contained_path(workflow.folder(eid),'graphic-sources/'+digest+'.image')


def bind_original(eid,evidence,data):
    if evidence.get('origin')!='截图': return
    digest=evidence['sha256']
    path=original_path(eid,digest)
    if data is None:
        if not path.is_file() or file_sha256(path)!=digest: raise ValueError('原始截图缺失或已变化，请重新提取')
        return
    if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('截图已更换，请重新提取')
    if path.is_file() and file_sha256(path)==digest: return
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(uuid.uuid4().hex+'.tmp')
    try:
        temp.write_bytes(data)
        replace_file(temp,path)
    finally:
        temp.unlink(missing_ok=True)
