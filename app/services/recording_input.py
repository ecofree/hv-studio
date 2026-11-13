"""Resolve one delivery: a full recording or an ordered sequence of WAV files."""
import re
import tempfile
import wave
from pathlib import Path

from app.services.provenance import atomic_json, file_sha256, fingerprint, replace_file
from app.services.narration_pipeline import read

_NUMBER = re.compile(r'^(?:(?:act|part|track|chapter|scene)[ _.-]*|第)?(\d+)(?=$|[ _.:：-]|幕|段|章)', re.I)
_MASTER = re.compile(r'完整.*(?:母带|解说|旁白|录音)|(?:解说|旁白).*母带|母带|(?:^|[ _.-])(?:full|master)(?:$|[ _.-])',re.I)


def select(value, mode='auto'):
    path = Path(value).resolve()
    if mode not in ('auto','master','parts'):
        raise ValueError('未知旁白输入方式')
    if path.is_file() and mode != 'parts':
        return 'master', [path]
    directory = path if path.is_dir() else path.parent if path.is_file() else None
    if directory is None:
        raise ValueError('所选旁白文件或目录不存在')
    def chosen(kind, items):
        if any(not p.resolve().is_relative_to(directory) for p in items):
            raise ValueError('选中的旁白指向目录外的 WAV')
        return kind, items
    files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower()=='.wav']
    masters = [p for p in files if _MASTER.search(p.stem)]
    if mode != 'parts' and masters:
        if len(masters)!=1: raise ValueError('目录中有多份完整母带，请指定要使用的文件')
        return chosen('master',masters)
    if mode=='master':
        raise ValueError('目录中缺少可识别的完整母带，请指定文件')
    numbered = sorted((int(hit[1]),p) for p in files if p not in masters and (hit:=_NUMBER.match(p.stem)))
    if not numbered:
        if mode=='auto' and len(files)==1: return chosen('master',files)
        raise ValueError('找不到完整母带或编号 WAV，请指定输入')
    numbers = [n for n,_ in numbered]
    if numbers[0] not in (0,1) or numbers!=list(range(numbers[0],numbers[0]+len(numbers))):
        raise ValueError('音频分段编号重复或不连续，请先确认顺序')
    return chosen('parts',[p for _,p in numbered])


def duration(files):
    seconds = 0.
    for path in files:
        with wave.open(str(path),'rb') as src: seconds += src.getnframes()/src.getframerate()
    return seconds


def bind(value, mode='auto'):
    kind, files = select(value,mode)
    return dict(kind=kind, source=str(files[0] if kind=='master' else files[0].parent),
                files=[dict(path=str(p),sha256=file_sha256(p)) for p in files])


def validate(selection):
    kind, files = select(selection['source'],selection['kind'])
    if kind!=selection['kind'] or [str(p) for p in files]!=[r['path'] for r in selection['files']]:
        raise ValueError('旁白输入清单或编号顺序已变化')
    for p,row in zip(files,selection['files']):
        if file_sha256(p)!=row['sha256']: raise ValueError('旁白源文件已变化：'+p.name)


def parts_receipt(master):
    path=Path(master).resolve()
    receipt=path.parent/'parts.json'
    if path.name!='master.wav' or not receipt.is_file():return None
    value=read(receipt)
    if value.get('kind')!='numbered_recording' or value['track_sha256']!=file_sha256(path):
        raise ValueError('分段工作音轨回执与文件不符')
    selection=value['selection']
    identity=fingerprint(dict(clock='mono-pcm16-24000-no-added-gap-v1',selection=selection))
    if path.parent.name!=identity or len(selection['files'])!=len(value['spans']):
        raise ValueError('分段清单与工作音轨版本不符')
    previous=0
    for span in value['spans']:
        if span['start_sample']!=previous or span['end_sample']<=previous:raise ValueError('分段采样范围无效')
        previous=span['end_sample']
    with wave.open(str(path),'rb') as src:
        if (src.getnchannels(),src.getsampwidth(),src.getframerate(),src.getnframes())!=(1,2,24000,previous):
            raise ValueError('分段采样范围与工作音轨不符')
    if value['samples']!=previous or value['gap_samples_added']!=0:raise ValueError('分段音轨时钟不符')
    validate(selection)
    return value


def materialize(selection, destination):
    """Keep a provided master in place. Join parts only once, on a sample clock."""
    validate(selection)
    if selection['kind']=='master':return Path(selection['files'][0]['path'])
    from app.services.external_narration import canonical_master
    from app.services.process_lock import wait_file_lock
    key=fingerprint(dict(clock='mono-pcm16-24000-no-added-gap-v1',selection=selection))
    folder=Path(destination)/key
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/'master.wav'
    with wait_file_lock(folder/'parts.lock',timeout=600):
        if (folder/'parts.json').is_file():
            rec=parts_receipt(target)
            if rec['selection']!=selection:raise ValueError('分段登记不符')
            return target
        with tempfile.TemporaryDirectory(dir=folder) as stage:
            tmp=Path(stage)/'master.wav'; spans=[]; cursor=0
            with wave.open(str(tmp),'wb') as dst:
                dst.setparams((1,2,24000,0,'NONE','not compressed'))
                for i,row in enumerate(selection['files']):
                    src=Path(row['path'])
                    with wave.open(str(src),'rb') as h:
                        native=(h.getnchannels(),h.getsampwidth(),h.getframerate())==(1,2,24000)
                    if not native:
                        norm=Path(stage)/f'part-{i}.wav'
                        canonical_master(src,norm)
                        src=norm
                    with wave.open(str(src),'rb') as h:
                        n=h.getnframes(); written=0
                        while data:=h.readframes(65536):
                            dst.writeframesraw(data); written+=len(data)
                        if n<=0 or written!=2*n:raise ValueError('音频分段为空或被截断')
                    spans.append(dict(start_sample=cursor,end_sample=cursor+n));cursor+=n
            validate(selection)
            rec=dict(kind='numbered_recording',selection=selection,track_sha256=file_sha256(tmp),
                     sample_rate=24000,samples=cursor,spans=spans,gap_samples_added=0)
            replace_file(tmp,target)
            atomic_json(folder/'parts.json',rec)
        return target
