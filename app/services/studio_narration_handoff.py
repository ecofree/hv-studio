"""Select a source-bound local take and hand it to the maintained v2 assembler."""
from pathlib import Path
import io
from app.services import studio_workflow as W
from app.services.provenance import file_sha256, fingerprint, atomic_json
from app.services.production_revision import write_once
from app.services.process_lock import file_lock
from app.services.narration_pipeline import read


def selection_path(ep_id):
    return W.folder(ep_id)/'narration-selection.json'


def finish_receipt(ep_id, value, contract_path=None):
    """Complete provenance for newly produced audio; does not select or approve it."""
    from app.services.series_voice import source_binding
    files = [value['raw_master'], value['master'], value['speed_receipt'],
             str(Path(value['raw_master']).with_suffix('.chunks.json')),
             str(Path(value['master']).with_suffix('.level.json'))]
    chunks = read(files[3])
    for row in chunks['chunks']:
        files.extend([row['raw_path'], row['receipt_path']])
        files.extend(row['content_qa']['inputs'])
        take_receipt = read(row['receipt_path'])
        if take_receipt.get('kind') == 'compound_tts_take_v1':
            for take in take_receipt.get('subtakes', []):
                files.extend(take[k] for k in ('provider_raw_path','provider_receipt_path','processed_path','trim_receipt_path'))
                files.extend(take['content_qa']['inputs'])
    if contract_path: files.append(str(Path(contract_path).resolve()))
    # Preserve every node in the speed chain (including its original raw receipt).
    receipt = value['speed_receipt']
    while receipt:
        r = read(receipt); files.extend([receipt,r['path']])
        receipt = (r.get('parent') or {}).get('receipt')
    value.update(version=2, episode_id=ep_id, narration_identity=W.narration_identity(ep_id), source_binding=source_binding(ep_id),
                 voice_contract_path=str(Path(contract_path).resolve()) if contract_path else None,
                 inputs={str(Path(p).resolve()):file_sha256(p) for p in files})
    return value


def validate_receipt(ep_id, source_map, *, current=True, deep=True):
    """No model calls. Verify original takes, exact text, processing and final clock."""
    from app.services import studio_voice_contract as C
    from app.services.series_voice import source_binding, validate_parts
    from app.services.studio_media import speed_plan
    from app.services.narration_master import verify_repair
    r = read(source_map)
    if r.get('version') != 2 or r.get('episode_id') != ep_id:
        raise ValueError('音轨缺少当前入口的文案与声音交接记录')
    if current:
        if r['source_binding'] != source_binding(ep_id): raise ValueError('音轨不属于当前新稿或规则版本')
        if r.get('narration_identity'):
            if r['narration_identity']!=W.narration_identity(ep_id):raise ValueError('音轨对应的文案或声音方案已更新')
        elif r['voice_design_identity'] != W.voice_design_identity(ep_id) and not r.get('voice_contract_path'):
            raise ValueError('旧音轨没有声音合同，无法证明变更仅涉及画面')
        # Old contracted takes are checked below against the actual source,
        # contract, requests, samples and playback chain; visuals do not speak.
    for p, sha in r['inputs'].items():
        if file_sha256(p) != sha: raise ValueError('音轨来源材料已改变：'+p)
    required = {str(Path(p).resolve()) for p in (r['master'],r['raw_master'],r['speed_receipt'],
                Path(r['master']).with_suffix('.level.json'),Path(r['raw_master']).with_suffix('.chunks.json'))}
    if not required <= set(r['inputs']): raise ValueError('音轨缺少处理来源绑定')
    if r['sha256'] != file_sha256(r['master']): raise ValueError('音轨文件已改变')
    contract_path = r.get('voice_contract_path')
    contract = None
    if contract_path:
        if str(Path(contract_path).resolve()) not in r['inputs']: raise ValueError('缺少声音方案绑定')
        contract = C.load(contract_path,source_sha256=r['source_sha256'],
                          voice=W.load(ep_id)['voice'] if current else None)
    elif current and C.path_for(ep_id).exists(): raise ValueError('音轨未使用本集锁定声音方案')
    parts = r['parts']
    if current:
        doc = W.document(ep_id)
        if contract and contract['version'] == 1:
            units = {u['unit_id']:u['source_text'] for u in doc['units']}; seen = []
            for part in parts:
                if part['text'] != '\n\n'.join(units[u] for u in part['unit_ids']): raise ValueError('旧方案分段与原稿不一致')
                seen.extend(part['unit_ids'])
            if seen != list(units): raise ValueError('原稿覆盖不完整')
        else: validate_parts(parts,doc)
    chunks = read(Path(r['raw_master']).with_suffix('.chunks.json'))
    if chunks['source_sha256'] != file_sha256(r['raw_master']) or len(parts) != len(chunks['chunks']):
        raise ValueError('原始配音与分段不一致')
    request_ids = []
    import wave
    with wave.open(str(r['raw_master']),'rb') as master:
        if (master.getnchannels(),master.getsampwidth(),master.getframerate(),master.getnframes()) != (1,2,chunks['sample_rate'],chunks['samples']):
            raise ValueError('原始合并录音的采样时钟不一致')
        cursor=0
        for row in chunks['chunks']:
            with wave.open(str(row['raw_path']),'rb') as take:
                if (take.getnchannels(),take.getsampwidth(),take.getframerate()) != (1,2,chunks['sample_rate']):
                    raise ValueError('原始配音采样格式不一致')
                n=take.getnframes()
                if row['sample_start'] < cursor or row['sample_end']-row['sample_start'] != n:
                    raise ValueError('原始配音合并范围不一致')
                gap=master.readframes(row['sample_start']-cursor)
                if any(gap) or master.readframes(n) != take.readframes(n):
                    raise ValueError('合并音轨没有逐采样保留原始配音')
                cursor=row['sample_end']
        if cursor != chunks['samples']: raise ValueError('合并音轨末尾存在未登记内容')
    def qa_matches(qa, text, audio_sha):
        if (qa.get('status')!='passed_machine' or qa.get('findings')
                or qa.get('source_sha256')!=fingerprint(text) or qa.get('audio_sha256')!=audio_sha
                or not qa.get('inputs') or not set(qa['inputs']) <= set(r['inputs'])):
            return False
        if any(r['inputs'].get(p)!=sha for p,sha in qa['inputs'].items()):return False
        from app.services.audio_content_qa import transcript
        for p in qa['inputs']:
            recognition=read(p);observed=recognition['result']
            if (recognition['result_sha256']!=fingerprint(observed) or observed['audio_sha256']!=audio_sha
                    or transcript(text,observed['text'])['findings']):return False
        return True

    for part, row in zip(parts,chunks['chunks']):
        for name in ('raw_path','receipt_path'):
            if str(Path(row[name]).resolve()) not in r['inputs']: raise ValueError('缺少原始配音凭据')
        receipt = read(row['receipt_path'])
        if (row['receipt_sha256'] != file_sha256(row['receipt_path']) or row['text'] != part['text']
                or row['instruction'] != part.get('instruction', row['instruction'])
                or receipt.get('audio_sha256') != file_sha256(row['raw_path'])):
            raise ValueError('实际配音请求与文案或音轨不一致')
        if receipt.get('kind') == 'compound_tts_take_v1':
            subtakes=receipt.get('subtakes') or [];subrequests=[];spoken=[];cursor_bytes=[]
            if (receipt.get('source_text')!=part['text'] or len(subtakes)<2
                    or row['request_fingerprint']!=fingerprint([t['request'] for t in subtakes])):
                raise ValueError('复合配音段落缺少正确的正文或请求绑定')
            for i,take in enumerate(subtakes):
                req=take['request'];spoken.append(take['source_text'])
                if req.get('text','').find(take['source_text'])<0:
                    raise ValueError('复合配音子请求没有覆盖其正文片段')
                for field in ('provider_raw_path','provider_receipt_path','processed_path','trim_receipt_path'):
                    name=str(Path(take[field]).resolve())
                    if name not in r['inputs'] or r['inputs'][name]!=file_sha256(name):
                        raise ValueError('复合配音缺少原始生成或静音裁切凭据')
                provider_receipt=read(take['provider_receipt_path'])
                if (take['provider_receipt_sha256']!=file_sha256(take['provider_receipt_path'])
                        or provider_receipt.get('request')!=req
                        or provider_receipt.get('audio_sha256')!=file_sha256(take['provider_raw_path'])
                        or take['trim_receipt_sha256']!=file_sha256(take['trim_receipt_path'])
                        or take['processed_sha256']!=file_sha256(take['processed_path'])):
                    raise ValueError('复合配音子请求来源哈希不一致')
                trim=read(take['trim_receipt_path'])
                if (trim.get('source_sha256')!=file_sha256(take['provider_raw_path'])
                        or trim.get('output_sha256')!=file_sha256(take['processed_path'])):
                    raise ValueError('复合配音静音裁切没有对应原始音频')
                if not qa_matches(take['content_qa'],take['source_text'],take['processed_sha256']):
                    raise ValueError('复合配音子片段的漏读或重复检查未通过')
                if contract:
                    C.validate(req)
                    if req.get('voice_contract',{}).get('sha256')!=file_sha256(contract_path):
                        raise ValueError('实际配音没有使用已选声音方案')
                subrequests.append(req);cursor_bytes.append(Path(take['processed_path']).read_bytes())
            if ''.join(spoken)!=part['text']:
                raise ValueError('复合配音子片段未逐字覆盖完整段落')
            import wave
            import numpy as np
            sr=receipt.get('sample_rate');gap=receipt.get('gap_samples')
            if type(sr)!=int or type(gap)!=int or gap<0:raise ValueError('复合配音缺少采样时钟或段间距')
            expected=bytearray()
            for i,raw in enumerate(cursor_bytes):
                with wave.open(io.BytesIO(raw),'rb') as w:
                    if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,sr):
                        raise ValueError('复合配音子片段采样格式不一致')
                    pcm=w.readframes(w.getnframes())
                if i:expected.extend(np.zeros(gap,dtype='<i2').tobytes())
                expected.extend(pcm)
            with wave.open(str(row['raw_path']),'rb') as w:
                if (w.getnchannels(),w.getsampwidth(),w.getframerate())!=(1,2,sr):
                    raise ValueError('复合配音音轨采样格式不一致')
                assembled=w.readframes(w.getnframes())
            if assembled!=bytes(expected):raise ValueError('复合配音音轨与子片段无法逐采样复现')
            request_ids.extend(subrequests)
        else:
            req=receipt.get('request')
            if (not req or req['text']!=part['text'] or req['instruction']!=part['instruction']
                    or receipt.get('request_fingerprint')!=fingerprint(req)
                    or row['request_fingerprint']!=fingerprint(req)):
                raise ValueError('实际配音请求与文案或音轨不一致')
            if contract:
                C.validate(req)
                if req.get('voice_contract',{}).get('sha256') != file_sha256(contract_path):
                    raise ValueError('实际配音没有使用已选声音方案')
            request_ids.append(req)
        qa = row['content_qa']
        if not qa_matches(qa,part['text'],receipt['audio_sha256']):
            raise ValueError('漏读、重复或长静音检查尚未通过')
    if fingerprint(request_ids) != chunks['requests_identity']: raise ValueError('合并录音请求链不一致')
    level_path=Path(r['master']).with_suffix('.level.json')
    level = verify_repair(level_path) if deep else read(level_path)
    if level['output_sha256']!=r['sha256'] or level['source_sha256']!=file_sha256(level['source_path']):
        raise ValueError('音量处理依据与最终音轨不一致')
    speed = read(r['speed_receipt'])
    if speed['source_identity'] != r['source_sha256']: raise ValueError('倍速记录文案不一致')
    if level['source_sha256'] != file_sha256(speed['parent']['path']): raise ValueError('音量处理未接在最后倍速上')
    target = contract['target_rate'] if contract else speed['applied_rate']
    if current:
        policy = W.folder(ep_id)/'playback-policy.json'
        configured = read(policy)['target_rate'] if policy.exists() else 1.
        if configured != target: raise ValueError('所选音轨与本集倍速方案不一致')
    speed_plan(r['master'],r['speed_receipt'],target,subtitles_aligned=True)
    origin=speed
    while origin.get('parent'): origin=read(origin['parent']['receipt'])
    if Path(origin['path']).resolve()!=Path(r['raw_master']).resolve() or origin['applied_rate']!=1.:
        raise ValueError('倍速链未从原始合并配音开始')
    return r


def select(ep_id, source_map, *, expected_identity):
    source_map = Path(source_map).resolve()
    with file_lock(W.folder(ep_id)/'workflow.lock'):
        if W.identity(ep_id) != expected_identity: raise ValueError('方案已改变，请重新载入')
        r = validate_receipt(ep_id,source_map)
        value = dict(version=1,episode_id=ep_id,source_map=str(source_map),source_map_sha256=file_sha256(source_map),
                     master=r['master'],master_sha256=r['sha256'],source_sha256=r['source_sha256'],
                     selected_for='full_episode_assembly',approved=False,listening_review='pending')
        revision = write_once(W.folder(ep_id)/'narration-selections'/(fingerprint(value)+'.json'),value)
        # The pointer can change; receipts and old full-episode registration cannot.
        atomic_json(selection_path(ep_id),dict(path=str(revision.resolve()),sha256=file_sha256(revision)))
        return revision


def selected(ep_id, *, deep=False):
    pointer = selection_path(ep_id)
    if not pointer.exists(): return None
    ref = read(pointer)
    if file_sha256(ref['path']) != ref['sha256']: raise ValueError('选定音轨记录已改变')
    value = read(ref['path'])
    if value['episode_id'] != ep_id or file_sha256(value['source_map']) != value['source_map_sha256']:
        raise ValueError('选定音轨来源已改变')
    r = validate_receipt(ep_id,value['source_map'],deep=deep)
    if (value['master'],value['master_sha256']) != (r['master'],r['sha256']): raise ValueError('选定音轨文件不一致')
    return dict(value,selection_path=ref['path'],selection_sha256=ref['sha256'])


def build(ep_id, plan_path, destination, *, progress=None):
    """Production-only assembly; called only by an explicitly submitted video job."""
    from app.services.external_narration import build as build_recording
    value = selected(ep_id,deep=True)
    if not value: raise ValueError('请先选用当前新稿的旁白候选')
    plan = read(plan_path)
    if plan['source_sha256'] != value['source_sha256']: raise ValueError('整集计划与选定旁白文案不一致')
    key = fingerprint([file_sha256(plan_path),value['selection_sha256']])
    target = Path(destination)/('studio-'+key)/'manifest.json'
    if target.exists():
        from app.services.narration_pipeline import validate_assembly
        validate_assembly(target); return target
    base = build_recording(plan_path,value['master'],Path(destination)/('studio-'+key)/'recording',
                           cache_dir=Path(destination)/'asr-cache'/value['master_sha256'],progress=progress)
    manifest = read(base)
    manifest['studio_narration'] = dict(episode_id=ep_id,selection_path=value['selection_path'],
                                       selection_sha256=value['selection_sha256'])
    validate_manifest(manifest)
    return write_once(target,manifest)


def validate_manifest(manifest):
    binding = manifest.get('studio_narration')
    if not binding: return
    if file_sha256(binding['selection_path']) != binding['selection_sha256']: raise ValueError('整集旁白选择依据已改变')
    value = read(binding['selection_path'])
    if file_sha256(value['source_map']) != value['source_map_sha256']: raise ValueError('整集旁白制作依据已改变')
    # Immutable historical versions remain auditable after selecting another take.
    r = validate_receipt(binding['episode_id'],value['source_map'],current=False,deep=False)
    if (value['master_sha256'] != r['sha256'] or read(manifest['plan_path'])['source_sha256'] != r['source_sha256']
            or manifest['source_recording']['delivered_sha256'] != r['sha256']):
        raise ValueError('整集配音交接的来源或文案不一致')
