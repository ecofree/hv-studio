"""Versioned punctuation-only transport text; the approved source stays intact."""
import re


def compile_text(source):
    pieces=[];mapping=[];changes=[];cursor=0
    for match in re.finditer(r'—+|…+|\.{3,}',source):
        pieces.append(source[cursor:match.start()])
        mapping.extend([i,i+1] for i in range(cursor,match.start()))
        replacement='，' if match.group().startswith('—') else '。'
        pieces.append(replacement);mapping.append([match.start(),match.end()])
        changes.append({'source_range':[match.start(),match.end()],
                        'original':match.group(),'replacement':replacement})
        cursor=match.end()
    pieces.append(source[cursor:]);mapping.extend([i,i+1] for i in range(cursor,len(source)))
    text=''.join(pieces)
    if ''.join(c for c in text if c.isalnum())!=''.join(c for c in source if c.isalnum()):
        raise ValueError('punctuation compilation changed spoken content')
    return text,{'version':'punctuation-v1','changes':changes,'source_map':mapping}
