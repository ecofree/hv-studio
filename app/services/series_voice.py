"""Reusable voice settings, with episode text and approvals kept separate."""
import copy
import json
from pathlib import Path

from app import paths
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.production_revision import write_once
from app.services.process_lock import file_lock

RECIPE_KEYS = ('speaker', 'model', 'mode', 'instruction', 'engine_revision',
               'generation', 'seed', 'max_chunk_characters', 'target_rate',
               'selected_variant', 'worker_path', 'engine_manifest_path', 'model_path')


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def catalog():
    return sorted((paths.state_dir()/'series-voices').glob('*/scheme.json'))


def publish(name, confirmed_contract):
    """Archive the existing confirmed recipe. No inference, new approval or calls."""
    from app.services.studio_voice_contract import load
    original = load(confirmed_contract)
    if original['version'] != 1 or not name.strip():
        raise ValueError('需要已有确认记录的声音方案和名称')
    root = paths.state_dir()/'series-voices'/fingerprint([name, file_sha256(confirmed_contract)])[:24]
    with file_lock(root/'publish.lock'):
        archived = copy.deepcopy(original)
        replacements = {}
        for filename, sha in original['evidence'].items():
            source = Path(filename)
            target = root/'evidence'/(sha+source.suffix)
            if target.exists():
                if file_sha256(target) != sha: raise ValueError('声音证据副本已改变')
            else:
                payload = source.read_bytes()
                if fingerprint_bytes(payload) != sha: raise ValueError('声音证据在复制时已改变')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            replacements[filename] = str(target.resolve())
        archived['evidence'] = {replacements[p]:sha for p,sha in original['evidence'].items()}
        for key in ('confirmation_path','pilot_receipt_path','generation_plan_path','engine_manifest_path','worker_path'):
            archived[key] = replacements[original[key]]
        archived['request_paths'] = [replacements[p] for p in original['request_paths']]
        origin = write_once(root/'confirmed-origin.json', archived)
        scheme = dict(version=1, name=name.strip(), origin_path=str(origin.resolve()),
                      origin_sha256=file_sha256(origin),
                      adopted_from_sha256=file_sha256(confirmed_contract),
                      recipe={k:archived[k] for k in RECIPE_KEYS},
                      approval_scope='voice_settings_only', authorizes_generation=False,
                      new_episode_review='not_inherited', stop_micro_tuning=True)
        result = write_once(root/'scheme.json', scheme)
        load_scheme(result)
        return result


def fingerprint_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def load_scheme(path):
    from app.services.studio_voice_contract import load
    scheme = read(path)
    if scheme.get('version') != 1 or scheme.get('approval_scope') != 'voice_settings_only':
        raise ValueError('未知系列声音方案')
    if file_sha256(scheme['origin_path']) != scheme['origin_sha256']:
        raise ValueError('系列声音确认依据已改变')
    origin = load(scheme['origin_path'])
    if origin['version'] != 1 or scheme['recipe'] != {k:origin[k] for k in RECIPE_KEYS}:
        raise ValueError('系列声音参数不再对应已确认方案')
    return scheme


def scheme_for_contract(contract_path):
    """Pilots inherit only the confirmed recipe, with a fresh exact source binding."""
    from app.services.studio_voice_contract import load
    c=load(contract_path)
    if c['version']==2:return Path(c['scheme_path'])
    for path in catalog():
        if read(path).get('adopted_from_sha256')==file_sha256(contract_path):
            load_scheme(path);return path
    return publish(f"{c['speaker']} · {c['target_rate']} 倍速已确认方案",contract_path)


def source_binding(ep_id):
    from app.services import studio_workflow as W
    from app.services.studio_sources import require_usable
    from app.managers import load
    require_usable(ep_id)
    p = Path(load(ep_id).script_path).resolve()
    source = paths.episode_dir(ep_id)/'editorial/source-binding.json'
    return dict(source_sha256=W.document(ep_id)['source_sha256'], narration_path=str(p),
                narration_sha256=file_sha256(p),
                import_sha256=file_sha256(source) if source.exists() else None)


def split_units(doc, limit):
    """Pack adjacent paragraphs within each act; split oversized units losslessly."""
    if type(limit) is not int or limit < 1: raise ValueError('无效配音段落上限')
    acts={};act=0
    for block in doc.get('blocks',[]):
        if block.get('kind')=='heading':act+=1
        acts[block['block_id']]=act
    parts=[];current=None;previous_act=None
    for unit in doc['units']:
        act=acts.get((unit.get('block_ids') or [None])[0],0)
        if current and act!=previous_act:
            parts.append(current);current=None
        previous_act=act
        text = unit['source_text']; start = 0
        while start < len(text):
            end = min(start+limit, len(text))
            if end < len(text):
                stops = [i+1 for i in range(start, end) if text[i] in '。！？；\n']
                if stops: end = stops[-1]
            fragment=text[start:end]
            if current and len(current['text'])+2+len(fragment)>limit:
                parts.append(current);current=None
            span=dict(unit_id=unit['unit_id'],text_start=start,text_end=end)
            if current:
                current['text']+='\n\n'+fragment;current['spans'].append(span)
            else:current=dict(text=fragment,spans=[span])
            start = end
            if start<len(text):parts.append(current);current=None
    if current:parts.append(current)
    return parts


def validate_parts(parts, doc):
    cursor=0;pos=0;units=doc['units']
    for part in parts:
        spans=part.get('spans') or [{k:part[k] for k in ('unit_id','text_start','text_end')}]
        pieces=[]
        for span in spans:
            if cursor>=len(units):raise ValueError('配音分段包含多余内容')
            unit=units[cursor];end=span['text_end']
            if (span['unit_id']!=unit['unit_id'] or span['text_start'] != pos or not pos < end <= len(unit['source_text'])):
                raise ValueError('配音分段与用户原文不一致，存在漏字、重复或改写')
            pieces.append(unit['source_text'][pos:end]);pos=end
            if pos==len(unit['source_text']):cursor+=1;pos=0
        if part['text']!='\n\n'.join(pieces):raise ValueError('配音分段与用户原文不一致，存在漏字、重复或改写')
    if cursor!=len(units) or pos:raise ValueError('配音分段未覆盖全部用户原文')


def bind_episode(ep_id, scheme_path, *, expected_identity):
    """Explicit metadata binding; never replaces an existing locked episode."""
    from app.services import studio_workflow as W
    from app.services.studio_voice_contract import path_for
    scheme_path = Path(scheme_path).resolve(); recipe = load_scheme(scheme_path)['recipe']
    with file_lock(W.folder(ep_id)/'workflow.lock'):
        if W.identity(ep_id) != expected_identity: raise ValueError('文案或方案已更新，请重新载入')
        target = path_for(ep_id)
        if target.exists():
            c = read(target)
            if c.get('version') == 2 and c.get('scheme_sha256') == file_sha256(scheme_path):
                load_binding(target); return target
            raise ValueError('本集已有锁定声音方案，保留旧绑定；新稿请建立独立版本')
        voice = W.load(ep_id)['voice']
        if (voice['identity'], voice['model']) != (recipe['speaker'], recipe['model']):
            raise ValueError('请先在声音基准中选择相同的旁白身份和模型，再绑定共用方案')
        policy = W.folder(ep_id)/'playback-policy.json'
        if not policy.exists() or read(policy).get('target_rate') != recipe['target_rate']:
            raise ValueError('请先为新稿登记与声音方案一致的目标倍速')
        binding = dict(version=2, episode_id=ep_id, scheme_path=str(scheme_path),
                       scheme_sha256=file_sha256(scheme_path), source=source_binding(ep_id),
                       parts=split_units(W.document(ep_id), recipe['max_chunk_characters']),
                       approved=False, listening_review='pending', authorizes_generation=False)
        write_once(target, binding)
        return target


def prepare_episode(ep_id, scheme_path, *, expected_identity):
    """User-selected setup for a new draft; text and existing voice bindings stay put."""
    from app.services import studio_workflow as W
    from app.services.studio_voice_contract import path_for
    if path_for(ep_id).exists():
        return bind_episode(ep_id, scheme_path, expected_identity=expected_identity)
    if W.identity(ep_id) != expected_identity: raise ValueError('文案或方案已更新，请重新载入')
    recipe = load_scheme(scheme_path)['recipe']
    value = W.load(ep_id)
    for key, expected in [('identity',recipe['speaker']),('model',recipe['model'])]:
        if value['voice'][key] and value['voice'][key] != expected:
            raise ValueError('已有其他声音基准，不能静默替换')
    if value['voice'].get('master'): raise ValueError('已有选定音轨，新稿请建立独立版本')
    policy = W.folder(ep_id)/'playback-policy.json'
    if policy.exists() and read(policy).get('target_rate') != recipe['target_rate']:
        raise ValueError('已有不同倍速方案，不能静默替换')
    if not policy.exists():
        write_once(policy,dict(version=1,target_rate=recipe['target_rate'],source='selected_series_voice',approved=False))
    value['voice'].update(identity=recipe['speaker'],model=recipe['model'],delivery=recipe['instruction'])
    value['binding'] = W.binding(ep_id)
    W.save(ep_id,value,expected_revision=value['revision'])
    return bind_episode(ep_id,scheme_path,expected_identity=W.identity(ep_id))


def load_binding(path):
    from app.services import studio_workflow as W
    binding = read(path)
    if binding.get('version') != 2: raise ValueError('未知单集声音绑定')
    if file_sha256(binding['scheme_path']) != binding['scheme_sha256']:
        raise ValueError('本集绑定的系列声音方案已改变')
    recipe = load_scheme(binding['scheme_path'])['recipe']
    if binding['source'] != source_binding(binding['episode_id']):
        raise ValueError('文案或创作规则已改变，请建立新稿绑定')
    validate_parts(binding['parts'], W.document(binding['episode_id']))
    if any(len(p['text']) > recipe['max_chunk_characters'] for p in binding['parts']):
        raise ValueError('新稿配音分段超过已确认输入上限')
    return dict(binding, **recipe, source_sha256=binding['source']['source_sha256'])
