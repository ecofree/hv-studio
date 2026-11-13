"""Verified sample ranges across two revisions of the same episode recording."""
from dataclasses import dataclass
import json
from pathlib import Path
import wave

from app.services.provenance import file_sha256


def _pcm(path):
    with wave.open(str(path),'rb') as src:
        if src.getnchannels()!=1 or src.getsampwidth()!=2:
            raise ValueError('revision comparison requires mono PCM16')
        return src.getframerate(),src.readframes(src.getnframes())


def _equal_prefix(left, right):
    import numpy as np
    length=min(len(left),len(right))
    for start in range(0,length,262144):
        end=min(length,start+262144)
        mismatch=np.flatnonzero(left[start:end]!=right[start:end])
        if len(mismatch):return start+int(mismatch[0])
    return length


@dataclass(frozen=True)
class SampleEdit:
    first: int
    old_stop: int
    new_stop: int
    old_length: int
    new_length: int

    @classmethod
    def compare(cls,old,new):
        import numpy as np
        a=np.frombuffer(old,dtype='<i2');b=np.frombuffer(new,dtype='<i2')
        first=_equal_prefix(a,b)
        tail=_equal_prefix(a[first:][::-1],b[first:][::-1])
        return cls(first,len(a)-tail,len(b)-tail,len(a),len(b))

    def boundary(self, position):
        if position<=self.first:return position
        if position>=self.old_stop:return position+self.new_stop-self.old_stop
        return None

    def interval(self,start,end):
        if end<=self.first: return start,end
        if start>=self.old_stop:
            delta=self.new_stop-self.old_stop
            return start+delta,end+delta
        return None


def find_parent(destination, master, prior_path):
    root=Path(destination).resolve()
    edit_path=Path(master).parent/'edit.json'
    edit=None
    if edit_path.is_file():
        info=json.loads(edit_path.read_text(encoding='utf-8'))
        if info.get('derived_master_sha256')==file_sha256(master):edit=info
    candidates=[Path(prior_path)] if prior_path else []
    if edit: candidates.extend(sorted(root.glob('ext-*/manifest.json')))
    for p in dict.fromkeys(candidates):
        if not p.resolve().is_relative_to(root) or not p.is_file():continue
        doc=json.loads(p.read_text(encoding='utf-8'))
        if doc.get('provenance')!='external_recording':continue
        source=doc.get('source_recording',{})
        if edit and source.get('delivered_sha256')!=edit.get('source_master_sha256'):continue
        if not Path(doc['master_path']).is_file() or file_sha256(doc['master_path'])!=doc['master_sha256']:continue
        if not Path(doc['plan_path']).is_file() or file_sha256(doc['plan_path'])!=doc['plan_sha256']:continue
        if edit and edit.get('old_source_sha256') and json.loads(Path(doc['plan_path']).read_text(encoding='utf-8'))['source_sha256']!=edit['old_source_sha256']:
            continue
        return p,doc
    return None,None


def prior_chunk_bounds(parent, old16, new16, count):
    if not parent or not old16.is_file():return ()
    cache=Path(parent['asr']['cache_dir'])
    parts=sorted(cache.glob('part*.json'),key=lambda p:int(p.stem[4:]))
    if not parts:return ()
    rate,old=_pcm(old16);rate2,new=_pcm(new16)
    if rate!=rate2:return ()
    edit=SampleEdit.compare(old,new)
    if edit.first==0 and edit.old_stop==edit.old_length:return ()
    bounds=[0]
    for p in parts:
        row=json.loads(p.read_text(encoding='utf-8'))
        if row['start_sample']!=bounds[-1] or row['end_sample']<=bounds[-1]:return ()
        bounds.append(row['end_sample'])
    if bounds[-1]!=edit.old_length:return ()
    return tuple(sorted({n for v in bounds if (n:=edit.boundary(v)) is not None and 0<n<count}))


def reuse_cuts(parent, data, units, ranges, rows, rate):
    if not parent:return ranges,{}
    old_rate,old=_pcm(parent['master_path'])
    if old_rate!=rate:return ranges,{}
    edit=SampleEdit.compare(old,data)
    oldtakes={u['unit_id']:(u,t) for u,t in zip(parent['alignment'],parent['takes'])}
    script=json.loads(Path(parent['plan_path']).read_text(encoding='utf-8'))
    spoken={u['unit_id']:u['approved_spoken_text'] for u in script['units']}
    kept={}
    for i,u in enumerate(units):
        oldunit,take=oldtakes.get(u['unit_id'],(None,None))
        if not oldunit or oldunit['text']!=u['display_text'] or spoken.get(u['unit_id'])!=u['approved_spoken_text']:continue
        span=edit.interval(oldunit['sample_start'],oldunit['sample_end'])
        if not span:continue
        path=Path(take['audio_path'])
        if not path.is_file() or file_sha256(path)!=take['audio_sha256']:continue
        hz,pcm=_pcm(path);a,b=span
        if hz==rate and data[a*2:b*2]==pcm:
            kept[i]=(a,b,str(path.resolve()))
    # Drop an anchor if the next changed unit's observed speech would be clipped.
    for i,(a,b,_) in list(kept.items()):
        if ((i and i-1 not in kept and round(rows[i-1]['end']*rate)>a) or
            (i+1<len(units) and i+1 not in kept and round(rows[i+1]['start']*rate)<b)):
            del kept[i]
    output=list(ranges)
    for i,(a,b,_) in kept.items():
        output[i]=a,b
        if i and i-1 not in kept:output[i-1]=(output[i-1][0],min(output[i-1][1],a))
        if i+1<len(output) and i+1 not in kept:output[i+1]=(max(output[i+1][0],b),output[i+1][1])
    if any(a>=b or (i and a<output[i-1][1]) for i,(a,b) in enumerate(output)):
        return ranges,{}
    return output,{i:row[2] for i,row in kept.items()}
