"""Read a table screenshot locally and keep cell rectangles in original image pixels."""
import csv
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from app import paths
from app.services.studio_graphics import open_upload


def _run(command):
    try:
        result = subprocess.run(command,capture_output=True,timeout=70,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise ValueError('本地文字识别未完成，请检查 OCR 语言组件，或裁小截图再试') from exc
    if result.returncode:
        detail = result.stderr.decode('utf-8','replace').strip().splitlines()
        raise ValueError('本地文字识别失败：'+(detail[0][:190] if detail else str(result.returncode)))
    return result.stdout.decode('utf-8-sig')


def _recognize(path, language):
    if os.name=='nt':
        tag = {'简体中文':'zh-Hans','繁體中文':'zh-Hant','English':'en'}[language]
        return json.loads(_run(['powershell.exe','-NoProfile','-NonInteractive','-File',
            str(paths.ROOT/'scripts/read-graphic-ocr.ps1'),'-ImagePath',str(path),'-LanguageTag',tag]))
    executable = shutil.which('tesseract')
    if not executable:
        raise ValueError('此系统需要安装 Tesseract 及所选 OCR 语言包。也可以粘贴资料文字。')
    tag = {'简体中文':'chi_sim','繁體中文':'chi_tra','English':'eng'}[language]
    tsv = _run([executable,str(path),'stdout','-l',tag,'--psm','6','tsv'])
    words = []
    for row in csv.DictReader(io.StringIO(tsv),delimiter='\t',quoting=csv.QUOTE_NONE):
        if row['level']=='5' and row['text'].strip():
            x,y,w,h = (int(row[k]) for k in ('left','top','width','height'))
            words.append(dict(text=row['text'],box=[x,y,x+w,y+h]))
    return dict(engine='Tesseract',language=tag,words=words)


def read_image(data, language='简体中文'):
    with open_upload(data) as original:
        gray = ImageOps.grayscale(original)
    w,h = gray.size
    # Preserve screenshot coordinates across contrast correction and OCR scaling.
    if float(np.median(np.asarray(gray)))<110:
        gray = ImageOps.invert(gray)
    ink = np.asarray(gray)
    scale = min(2., 2400/max(w,h))
    with tempfile.TemporaryDirectory(prefix='hv-table-ocr-') as directory:
        path = Path(directory)/'table.png'
        size = (max(1,round(w*scale)),max(1,round(h*scale)))
        prepared = ImageOps.autocontrast(gray).resize(size,Image.Resampling.LANCZOS)
        prepared.save(path)
        prepared.close()
        scan = _recognize(path,language)
    for word in scan['words']:
        a,b,c,d = word['box']
        word['box'] = [max(0,round(a*w/size[0])),max(0,round(b*h/size[1])),
                       min(w,round(c*w/size[0])),min(h,round(d*h/size[1]))]
    if not scan['words']:
        raise ValueError('截图未识别到文字，可以放大原表或裁掉周围画面后再试')
    scan.update(size=[w,h],sections=table_sections(ink,scan['words']))
    return scan


def _runs(values,gap=0):
    positions = np.flatnonzero(values)
    if not len(positions):
        return []
    starts = np.r_[0,np.flatnonzero(np.diff(positions)>gap+1)+1]
    stops = np.r_[starts[1:]-1,len(positions)-1]
    return [(int(positions[i]),int(positions[j])+1) for i,j in zip(starts,stops)]


def _separators(gray):
    """Group ruling lines by their vertical extent, including stacked tables."""
    h,w = gray.shape
    dark = gray.astype(np.int16)
    offset = min(5,max(2,w//480))
    contrast = np.zeros_like(gray,dtype=bool)
    center = dark[:,offset:-offset]
    contrast[:,offset:-offset] = np.minimum(dark[:,:-2*offset]-center,dark[:,2*offset:]-center)>36
    height = max(44,h//13)
    segments = []
    for x in np.flatnonzero(contrast.sum(axis=0)>=height):
        if not offset<x<w-offset: continue
        for top,end in _runs(contrast[:,x],3):
            if end-top>=height:
                segments.append((x,top,end))
    groups = []
    for x,top,end in sorted(segments,key=lambda s:(s[1],s[0])):
        group = next((g for g in groups if abs(top-g[0])<=12 and abs(end-g[1])<=18),None)
        if group is None:
            groups.append([top,end,[x]])
        else:
            group[2].append(x)
    result = []
    for top,end,xx in groups:
        bars = []
        for x in sorted(xx):
            if not bars or x-bars[-1][-1]>8: bars.append([])
            bars[-1].append(x)
        cols = [round(statistics.mean(batch)) for batch in bars]
        if cols: result.append((top,end,cols))
    return result


def _lines(words):
    lines = []
    for word in sorted(words,key=lambda t:sum(t['box'][1::2])):
        _,a,_,b = word['box']
        mid = (a+b)/2
        if not lines or abs(mid-statistics.median((v['box'][1]+v['box'][3])/2 for v in lines[-1]))>max(4,(b-a)*.55):
            lines.append([])
        lines[-1].append(word)
    return [sorted(line,key=lambda w:w['box'][0]) for line in lines]


def _join(words):
    value = ''
    for word in words:
        text = word['text'].strip()
        if value and re.search(r'[A-Za-z]$',value) and re.match('[A-Za-z]',text):
            value += ' '
        value += text
    return value


def _bounds(boxes):
    return [min(b[0] for b in boxes),min(b[1] for b in boxes),
            max(b[2] for b in boxes),max(b[3] for b in boxes)]


def _infer_seams(lines,width):
    height = statistics.median(w['box'][3]-w['box'][1] for line in lines for w in line)
    coverage = np.zeros(width,dtype=int)
    for line in lines:
        for left,right in zip(line,line[1:]):
            x,y = left['box'][2]+3,right['box'][0]-3
            if y-x>max(14,height*.9): coverage[x:y] += 1
    return [(a+b)//2 for a,b in _runs(coverage>=max(2,round(len(lines)*.55))) if b-a>=8]


def _section(words,seams,region,ruled):
    lines = _lines(words)
    if not lines: return None
    if not ruled: seams = _infer_seams(lines,region[2])
    # Outer border lines are not columns.
    left = min(w['box'][0] for w in words)
    right = max(w['box'][2] for w in words)
    seams = [x for x in seams if left+3<x<right-3]
    n = len(seams)+1
    matrix = []
    for line in lines:
        cells = [[] for _ in range(n)]
        for word in line:
            mid = (word['box'][0]+word['box'][2])/2
            cells[sum(x<mid for x in seams)].append(word)
        if cells[0] or not matrix:
            matrix.append([[] for _ in range(n)])
        for i,cell in enumerate(cells):
            if cell: matrix[-1][i].append(cell)
    rows,boxes = [],[]
    for row in matrix:
        rows.append([''.join(_join(line) for line in cell) for cell in row])
        boxes.append([_bounds([w['box'] for line in cell for w in line]) if cell else None for cell in row])
    return dict(rows=rows,boxes=boxes,region=region,ruled=ruled)


def table_sections(gray,words):
    h,w = gray.shape
    sections = []
    for top,end,seams in _separators(gray):
        selected = [t for t in words if top-5<=(t['box'][1]+t['box'][3])/2<=end+5]
        if len(selected)<3: continue
        part = _section(selected,seams,[0,top,w,end],True)
        if part and len(part['rows'])>=2 and len(part['rows'][0])>=2:
            if not any(min(end,r['region'][3])-max(top,r['region'][1])>(end-top)*.6 for r in sections):
                sections.append(part)
    if sections:
        return sorted(sections,key=lambda r:r['region'][1])
    return [_section(words,[],[0,0,w,h],False)]
