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


def joined_recording_path(selection, destination):
    key = fingerprint(dict(clock='mono-pcm16-24000-no-added-gap-v1', selection=selection))
    return Path(destination)/key/'master.wav'


def selected_master_sha(selection, destination):
    if not selection:
        return None
    if selection['kind'] == 'master':
        return selection['files'][0]['sha256']
    path = joined_recording_path(selection, destination)
    receipt = parts_receipt(path) if path.is_file() else None
    return receipt['track_sha256'] if receipt else None


def materialize(selection, destination):
    """Keep a provided master in place. Join parts only once, on a sample clock."""
    validate(selection)
    if selection['kind']=='master':return Path(selection['files'][0]['path'])
    from app.services.external_narration import canonical_master
    from app.services.process_lock import wait_file_lock
    target=joined_recording_path(selection, destination)
    folder=target.parent
    folder.mkdir(parents=True,exist_ok=True)
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


class StaleRecordingSource(ValueError):
    """A previously selected recording belongs to a different source text."""


def episode_source(ep_id, *, explicit=None, mode='auto', source_text=None):
    """Read-only audio authority shared by admission, Studio and V2.

    Resolve recordings without synthesizing, assembling numbered parts, changing
    approvals or treating an existing file alone as a valid V2 binding.
    """
    from app import paths
    from app.managers import load
    from app.orchestrator import _load_script_md
    from app.services import studio_narration_handoff as H
    folder = paths.episode_dir(ep_id); root = folder/'production/v2'
    selected = H.selected(ep_id) if H.selection_path(ep_id).is_file() else None
    def recording(value, origin, evidence=None):
        parts = parts_receipt(value) if Path(value).is_file() else None
        selection = parts['selection'] if parts else bind(value, mode if explicit is not None else 'auto')
        validate(selection)
        return dict(kind='external_recording', origin=origin, requires_tts=False,
                    selection=selection, evidence=evidence or {},
                    source_sha256=fingerprint(source_text if source_text is not None
                                              else _load_script_md(load(ep_id))))
    if explicit is not None:
        value = recording(explicit, 'explicit')
        if selected and (value['selection']['kind'] != 'master'
                        or value['selection']['files'][0]['sha256'] != selected['master_sha256']):
            raise ValueError('explicit recording differs from selected narration')
        return value
    if selected:
        return recording(selected['master'], 'studio_selection',
                         {str(H.selection_path(ep_id)):file_sha256(H.selection_path(ep_id))})
    state_path = root/'run.json'
    state = read(state_path) if state_path.is_file() else {}
    text = source_text if source_text is not None else _load_script_md(load(ep_id))
    plan_path = root/'external-master.json'
    if plan_path.is_file():
        from app.services.narration_master import resolve
        plan = Path(state['plan_path'])
        if read(plan)['source_sha256'] != fingerprint(text):
            raise ValueError('selected recording belongs to another source')
        return recording(resolve(root, plan), 'repaired_master', {str(plan_path):file_sha256(plan_path)})
    if state.get('manifest_path'):
        path = Path(state['manifest_path'])
        manifest = read(path)
        from app.services.external_narration import PROVENANCE
        if manifest.get('provenance') == PROVENANCE:
            # Resolve the original delivery, not the generated assembly. A
            # damaged/missing intermediate take may be safely rebuilt locally;
            # downstream assembly/QA still validates every produced sample.
            plan = read(manifest['plan_path'])
            if state.get('source_sha256') != fingerprint(text) or plan['source_sha256'] != fingerprint(text):
                raise StaleRecordingSource('external narration belongs to another source; explicitly select the new recording')
            if file_sha256(manifest['plan_path']) != manifest['plan_sha256']:
                raise ValueError('external narration plan changed')
            src = manifest['source_recording']
            if file_sha256(src['delivered_path']) != src['delivered_sha256']:
                raise ValueError('delivered recording changed：已导入的原始母带已改变，请明确选择录音')
            return recording(src['delivered_path'], 'v2_manifest', {str(path):file_sha256(path)})
    legacy = folder/'production/external-audio-binding.json'
    if legacy.is_file():
        from app.services.external_audio import verify_binding
        episode = load(ep_id)
        check = verify_binding(episode)
        if not check['ok']: raise ValueError('external audio binding invalid: '+'; '.join(check['fails']))
        registered=read(paths.episode_json(ep_id)).get('audio_path')
        if registered:
            value=recording(registered,'legacy_binding',{str(legacy):file_sha256(legacy)})
            if value['selection']['kind']!='master' or value['selection']['files'][0]['sha256']!=read(legacy)['source_sha256']:
                raise ValueError('legacy original recording differs from verified binding')
            return value
        return dict(kind='legacy_cut_audio',origin='legacy_binding',requires_tts=False,
                    selection=None,source_sha256=fingerprint(text),evidence={str(legacy):file_sha256(legacy)})
    from app.services import studio_workflow as W
    workflow = W.folder(ep_id)/'workflow.json'
    if workflow.is_file() and W.load(ep_id)['voice'].get('master'):
        return recording(W.load(ep_id)['voice']['master'], 'workflow_master')
    # Imported raw recordings are registered by source import, not arbitrary
    # media found in a directory. Their explicit source binding is checked.
    from app.services.studio_sources import contract, require_usable
    registered = read(paths.episode_json(ep_id)).get('audio_path')
    if contract(ep_id) and registered:
        require_usable(ep_id)
        from app.services.studio_workspace import library_path
        return recording(library_path(registered), 'imported_source')
    return dict(kind='tts', origin='voice_asset', requires_tts=True,
                source_sha256=fingerprint(text), selection=None, evidence={})
