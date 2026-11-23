"""Find an author's display tables without changing their original text or offsets."""
import re


CAPTION=re.compile(r'^\s*(?:#{1,6}\s*)?(?:📊\s*)?(?:\*\*)?【?上屏(?:数据表|表格|表)(?:\s*\d+)?\s*(?:[：:]\s*(.*?))?】?(?:\*\*)?\s*$')
NARRATION={'旁白','解说','口播','旁白/台词','narration'}


def find_tables(source):
    lines=source.splitlines(keepends=True)
    offsets=[0]
    for line in lines: offsets.append(offsets[-1]+len(line))
    def delimiter(line):
        if '\t' in line.strip('\r\n'): return '\t'
        if '|' in line: return '|'
        return None
    tables=[]
    i=0
    while i<len(lines):
        start=i
        title=CAPTION.fullmatch(lines[i].strip())
        if title:
            i+=1
            while i<len(lines) and not lines[i].strip(): i+=1
        if i>=len(lines): break
        sep=delimiter(lines[i])
        if not sep:
            if not title: i+=1
            continue
        header=i
        cells=[c.strip().casefold() for c in lines[i].strip().strip('|').split(sep)]
        if any(c in NARRATION for c in cells):
            i+=1
            while i<len(lines) and delimiter(lines[i])==sep: i+=1
            continue
        i+=1
        while i<len(lines) and delimiter(lines[i])==sep and not CAPTION.fullmatch(lines[i].strip()): i+=1
        rule=header+1<i and all(re.fullmatch(r':?-{2,}:?',c.strip()) for c in lines[header+1].strip().strip('|').split('|'))
        if len(cells)<2 or i-header<2+int(rule) or not (title or rule or sep=='\t'): continue
        tables.append(dict(start=offsets[start],data_start=offsets[header],end=offsets[i],
                           title=(title[1] or '').strip() if title else ''))
    return tables
