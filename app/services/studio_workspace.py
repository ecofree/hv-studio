"""Independent copy: archived episodes are a library, never active run state."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path


def config(root=None):
    if root is None:
        root = Path(__file__).resolve().parents[2]
    file = Path(root) / '.studio-project.json'
    return json.loads(file.read_text('utf-8')) if file.is_file() else None


def install_original_write_guard():
    """Refuse Python writes to the source checkout, including stale absolute paths."""
    import sys
    value = config()
    if not value or getattr(sys, '_hv_studio_guard', False):
        return
    original = Path(value['original_root']).resolve()

    def check(name):
        if isinstance(name, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(name)).resolve()
            if path.is_relative_to(original):
                raise PermissionError('副本禁止写入原项目：' + str(path))

    def audit(event, args):
        if event == 'open':
            name, mode, flags = args
            if (mode and any(x in mode for x in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                check(name)
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.truncate'}:
            check(args[0])
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            check(args[0]); check(args[1])
    sys.addaudithook(audit)
    sys._hv_studio_guard = True


def library_path(value, root=None):
    from app import paths
    root = Path(root or paths.ROOT).resolve()
    cfg = config(root)
    path = Path(value)
    if cfg:
        old = Path(cfg['original_root']).resolve()
        if path.is_absolute() and path.resolve().is_relative_to(old):
            path = root / path.resolve().relative_to(old)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('素材必须在当前项目副本内')
    return path


def import_episode(old_id, new_id):
    """Copy frame structure; no splitter, old receipt rewrite or approval promotion."""
    from app import paths
    from app.services.provenance import atomic_json, file_sha256
    paths.validate_episode_id(old_id); paths.validate_episode_id(new_id)
    if old_id == new_id:
        raise ValueError('新版本必须使用不同的剧集 ID')
    source = paths.ROOT / 'episodes' / (old_id + '.json')
    target = paths.episode_json(new_id)
    if target.exists() or paths.episode_dir(new_id).exists():
        raise ValueError('新版本已存在，不覆盖')
    raw = json.loads(source.read_text('utf-8'))
    script = library_path(raw['script_path'])
    if not script.is_file():
        raise ValueError('历史版本正文缺失')
    cfg = config(paths.ROOT)
    old_root = Path(cfg['original_root']).resolve() if cfg else paths.ROOT
    def rebase(item):
        if isinstance(item, dict): return {k: rebase(v) for k,v in item.items()}
        if isinstance(item, list): return [rebase(v) for v in item]
        if isinstance(item, str) and (item.startswith(str(old_root)) or item.startswith(old_root.as_posix())):
            return str(library_path(item))
        return item
    data = rebase(copy.deepcopy(raw))
    folder = paths.episode_dir(new_id)
    folder.mkdir(parents=True)
    local_script = folder / 'script_narration.md'
    local_script.write_bytes(script.read_bytes())
    data.update(id=new_id, script_path=str(local_script), status='scripted', state={})
    old_folder = paths.ROOT / 'episodes' / old_id
    for name in ('scene_plan.json', 'image_prompts.json'):
        p = old_folder / name
        if p.is_file():
            atomic_json(folder / name, rebase(json.loads(p.read_text('utf-8'))))
    atomic_json(target, data)
    run_file=old_folder/'production/v2/run.json'
    scene_file=folder/'scene_plan.json'
    if run_file.exists() and scene_file.exists():
        old_run=json.loads(run_file.read_text('utf-8'))
        scene=json.loads(scene_file.read_text('utf-8'))
        for key,value in old_run.get('image_assets',{}).items():
            image=library_path(value)
            if image.is_file():scene.setdefault('images',{})[key]={'path':str(image)}
        atomic_json(scene_file,scene)
        from app.services.existing_images import register
        register(new_id)
    receipt = dict(version=1, source_episode=str(source), source_sha256=file_sha256(source),
                   source_script_sha256=file_sha256(script), new_episode=new_id,
                   frames_preserved=len(data.get('frames', [])), approvals_imported=False)
    atomic_json(folder/'studio'/'import.json', receipt)
    return receipt


def create_episode(ep_id, title, source, *, scope='pilot'):
    from app import paths
    from app.models import Episode
    from app.services.provenance import atomic_json
    from app.services.script_document import parse_document
    from app.services import studio_workflow as W
    paths.validate_episode_id(ep_id)
    if paths.episode_json(ep_id).exists() or paths.episode_dir(ep_id).exists(): raise ValueError('剧集已存在')
    if not title.strip() or not parse_document(source)['units']: raise ValueError('请填写标题与可朗读正文')
    root=paths.episode_dir(ep_id);root.mkdir(parents=True)
    script=root/'script_narration.md';script.write_text(source,encoding='utf-8')
    ep=Episode(id=ep_id,title=title,episode_number=0,script_path=str(script))
    atomic_json(paths.episode_json(ep_id),ep.model_dump(mode='json'))
    W.save(ep_id,W.new(ep_id,scope=scope),expected_revision=0)
    return ep_id


def create_pilot(parent_id,new_id,start,end):
    """Copy an exact contiguous passage into a new, unapproved pilot draft."""
    from app import paths
    from app.services import studio_workflow as W
    from app.services.provenance import atomic_json
    from app.services.studio_sources import require_usable
    require_usable(parent_id)
    parent=W.load(parent_id);document=W.document(parent_id)
    if type(start) is not int or type(end) is not int or not 0<=start<end<=len(document['units']):
        raise ValueError('请选择连续、有效的正文范围')
    units=document['units'][start:end]
    if sum(len(u['source_text']) for u in units)>800:raise ValueError('样片最多选取 800 字，请缩短范围')
    identity=W.identity(parent_id)
    source='### 【第一幕：样片验证】\n\n'+'\n\n'.join(u['source_text'] for u in units)
    create_episode(new_id,parent['brief']['title'] or '短样片',source,scope='pilot')
    child=W.load(new_id);new_units=W.document(new_id)['units']
    if [u['source_text'] for u in new_units]!=[u['source_text'] for u in units]:
        raise ValueError('所选内容不能无损建立样片，已保留独立草稿供检查')
    mapping={a['unit_id']:b['unit_id'] for a,b in zip(units,new_units)}
    child['brief']=copy.deepcopy(parent['brief']);child['voice']=copy.deepcopy(parent['voice'])
    child['voice']['master']=''
    used={c for b in parent['beats'] if b['unit_id'] in mapping for c in b['claim_ids']}
    child['claims']=[copy.deepcopy(c) for c in parent['claims'] if c['unit_id'] in mapping or c['id'] in used]
    for c in child['claims']:
        if c['unit_id'] in mapping: c['unit_id']=mapping[c['unit_id']]
        else:
            # Keep the original claim/quote as context, never pretend it quotes this passage.
            c['context_source']=parent_id
    ids={c['id'] for c in child['claims']}
    child['beats']=[copy.deepcopy(b) for b in parent['beats'] if b['unit_id'] in mapping]
    for b in child['beats']:
        b['unit_id']=mapping[b['unit_id']];b['claim_ids']=[c for c in b['claim_ids'] if c in ids]
    child['cards']=[]
    for event in parent.get('cards',[]):
        at=event['at']
        if at=={'at':'start'} and start==0:child['cards'].append(copy.deepcopy(event))
        elif at.get('unit_id') in mapping:
            copied=copy.deepcopy(event);copied['at']['unit_id']=mapping[at['unit_id']];child['cards'].append(copied)
    keys={b['image_id'] for b in child['beats'] if b['image_id']}
    p=paths.episode_dir(parent_id);out=paths.episode_dir(new_id)
    if (p/'image_prompts.json').exists():
        prompts=W.read(p/'image_prompts.json');atomic_json(out/'image_prompts.json',{k:prompts[k] for k in keys if k in prompts})
    if (p/'scene_plan.json').exists():
        scene=W.read(p/'scene_plan.json')
        rows=[dict(copy.deepcopy(r),unit_id=mapping[r['unit_id']]) for r in scene.get('unit_scenes',[]) if r['unit_id'] in mapping]
        atomic_json(out/'scene_plan.json',dict(episode=new_id,authored=True,review_required=True,unit_scenes=rows,
                    images={k:v for k,v in scene.get('images',{}).items() if k in keys}))
    policy=p/'studio/playback-policy.json'
    if policy.exists():atomic_json(out/'studio/playback-policy.json',W.read(policy))
    atomic_json(out/'studio/parent.json',dict(parent_episode=parent_id,parent_identity=identity,
                source_units=[u['unit_id'] for u in units],unit_mapping=mapping,approvals_imported=False,
                original_cues=[c for c in document['delivery_cues'] if c.get('unit_id') in mapping]))
    child['binding']=W.binding(new_id);W.save(new_id,child,expected_revision=child['revision'])
    from app.services.studio_voice_contract import path_for as voice_contract_path
    if voice_contract_path(parent_id).exists():
        from app.services.series_voice import scheme_for_contract,prepare_episode
        scheme=scheme_for_contract(voice_contract_path(parent_id))
        prepare_episode(new_id,scheme,expected_identity=W.identity(new_id))
    if (out/'scene_plan.json').exists() and (out/'image_prompts.json').exists():
        from app.services.existing_images import register
        register(new_id)
    return new_id
