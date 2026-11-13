"""User-authored imports and explicit version disposition; no media work."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import paths
from app.services.provenance import atomic_json, file_sha256
from app.services.process_lock import file_lock


def registry_path():
    return paths.state_dir() / 'source-versions.json'


def versions():
    p = registry_path()
    return json.loads(p.read_text('utf-8')) if p.exists() else {'version': 1, 'episodes': {}}


def disposition(ep_id):
    return versions()['episodes'].get(ep_id, {'status': 'draft', 'reason': '尚未登记来源版本'})


def set_disposition(ep_id, status, reason):
    if status not in ('current', 'historical', 'excluded') or not reason.strip():
        raise ValueError('版本状态或原因无效')
    if not paths.episode_json(ep_id).is_file():
        raise ValueError('剧集不存在')
    if status == 'current' and not (paths.episode_dir(ep_id) / 'editorial/source-binding.json').is_file():
        raise ValueError('当前版本必须先导入可核对的原稿')
    with file_lock(registry_path().with_suffix('.lock')):
        data = versions()
        data['episodes'][ep_id] = dict(status=status, reason=reason)
        atomic_json(registry_path(), data)


def selectable(include_history=False):
    records = versions()['episodes']
    ids = [p.stem for p in paths.episodes_dir().glob('*.json')]
    if not include_history:
        ids = [i for i in ids if records.get(i, {}).get('status') not in ('historical', 'excluded')]
    return sorted(ids, key=lambda i: (records.get(i, {}).get('status') != 'current', i))


def contract(ep_id):
    p = paths.episode_dir(ep_id) / 'editorial/source-binding.json'
    return json.loads(p.read_text('utf-8')) if p.exists() else None


def issues(ep_id, _seen=None):
    _seen=set() if _seen is None else _seen
    if ep_id in _seen:return ['样片来源链出现循环']
    _seen.add(ep_id)
    state = disposition(ep_id)
    result = []
    if state['status'] in ('historical', 'excluded'):
        result.append('此版本仅供追溯，禁止作为新制作输入：' + state['reason'])
    value = contract(ep_id)
    parent_path=paths.episode_dir(ep_id)/'studio/parent.json'
    if parent_path.is_file():
        parent=json.loads(parent_path.read_text('utf-8'))
        result.extend(issues(parent['parent_episode'],_seen))
        from app.services import studio_workflow as W
        if W.identity(parent['parent_episode'])!=parent['parent_identity']:
            result.append('样片的原稿或逐幕设计已更新，请从当前版本重新选取')
    if not value:
        return result
    from app.managers import load
    from app.services.script_handoff import verify_revision
    try:
        root = Path(value['revision'])
        verify_revision(root)
        if file_sha256(root / 'manifest.json') != value['manifest_sha256']:
            result.append('文案版本清单已改变')
        if Path(load(ep_id).script_path).resolve() != (root / 'narration.md').resolve():
            result.append('当前剧集没有绑定导入的用户原稿')
        for item in value['inputs']:
            for key in ('snapshot',):
                if item.get(key) and file_sha256(item[key]) != item['sha256']:
                    result.append('导入快照已改变，请恢复或建立新版本：' + item[key])
    except (OSError, ValueError, KeyError) as exc:
        result.append('文案版本核验失败：' + str(exc))
    return result


def require_usable(ep_id):
    found = issues(ep_id)
    if found:
        raise ValueError('；'.join(found))


def external_updates(ep_id):
    """Informational only: the imported snapshot remains the selected version."""
    result=[]
    for item in (contract(ep_id) or {}).get('inputs',[]):
        for key in ('path','original_path'):
            p=item.get(key)
            if p and (not Path(p).is_file() or file_sha256(p)!=item['sha256']):result.append(p)
    return result


def import_authoring(ep_id, title, source_file, *, tts_file=None, rule_files=(), scope='full',
                     adopt_existing=False):
    """Create a fresh episode from identical user inputs. Never retarget old media."""
    from app.services.script_handoff import derive, write_revision
    from app.services.studio_workspace import library_path, config
    from app.services import studio_workflow as W
    paths.validate_episode_id(ep_id)
    if not title.strip() or scope not in ('full','pilot'):raise ValueError('标题或制作范围无效')
    existing_json=paths.episode_json(ep_id)
    existing_root=paths.episode_dir(ep_id)
    if not adopt_existing and (existing_json.exists() or existing_root.exists()):
        raise ValueError('请使用新的版本名称；旧版本不覆盖')
    existing_episode=None
    if adopt_existing:
        if not existing_json.is_file() or not existing_root.is_dir():
            raise ValueError('接管素材需要已有剧集登记和素材目录')
        if disposition(ep_id)['status'] in ('historical','excluded'):
            raise ValueError('历史或排除版本不能接管为当前制作')
        source_path=library_path(source_file)
        existing_episode=json.loads(existing_json.read_text('utf-8'))
        if (existing_episode.get('id')!=ep_id
                or Path(existing_episode.get('script_path','')).resolve()!=source_path.resolve()):
            raise ValueError('只允许接管剧集登记中指向的原稿')
        if (existing_root/'editorial/source-binding.json').exists() or (existing_root/'studio/workflow.json').exists() or (existing_root/'production/v2/run.json').exists():
            raise ValueError('该剧集已有制作绑定；请走版本更新流程，不能重新接管')
        from app.services.studio_jobs import jobs
        if any(row.get('running') for row in jobs(ep_id)):
            raise ValueError('该剧集有运行任务，不能接管素材')
    supplied = [('authoring', source_file)]
    if tts_file: supplied.append(('tts_reference', tts_file))
    supplied += [('writing_rule', p) for p in rule_files]
    cfg = config(paths.ROOT)
    inputs, contents = [], {}
    for role, filename in supplied:
        p = library_path(filename)
        data = p.read_bytes()
        original = None if adopt_existing else (Path(cfg['original_root']) / p.relative_to(paths.ROOT) if cfg else None)
        if original is not None and (not original.is_file() or original.read_bytes() != data):
            raise ValueError('副本文案或规则与原项目不一致，请先逐字同步：' + str(p))
        contents[str(p)] = data
        inputs.append(dict(role=role, path=str(p), sha256=file_sha256(p),
                           original_path=str(original) if original else None))
    source = contents[inputs[0]['path']].decode('utf-8')
    payloads = derive(source)
    if tts_file:
        tts = next(i for i in inputs if i['role'] == 'tts_reference')
        other = derive(contents[tts['path']].decode('utf-8'))
        clean = lambda b: re.sub(r'\s+', '', b.decode('utf-8'))
        if clean(payloads['spoken.txt']) != clean(other['spoken.txt']):
            raise ValueError('原稿与 TTS 专版正文不一致；保留两份原文，禁止自动选择或改写')
    root = paths.episode_dir(ep_id)
    target = write_revision(source, root/'editorial/revisions', dict(episode=ep_id, inputs=inputs))
    # The handoff directory exists, so construct only this new episode directly.
    if adopt_existing:
        status_before=existing_episode.get('status')
        existing_episode['script_path']=str(target/'narration.md')
        if status_before=='audio_ready':
            # The prior episode registry used a status absent from this studio's
            # EpisodeStatus enum. Preserve that evidence and migrate its state.
            existing_episode['status']='scripted'
        from app.models import Episode
        Episode.model_validate(existing_episode)
        atomic_json(paths.episode_json(ep_id),existing_episode)
        if status_before=='audio_ready':
            atomic_json(root/'studio/import.json',dict(version=1,legacy_status=status_before,
                studio_status='scripted',source_sha256=file_sha256(target/'source.md'),
                original_audio_path=existing_episode.get('audio_path'),
                narration_revision=str(target),approvals_imported=False))
    else:
        from app.models import Episode
        ep = Episode(id=ep_id, title=title, episode_number=0, script_path=str(target/'narration.md'))
        atomic_json(paths.episode_json(ep_id), ep.model_dump(mode='json'))
    for item in inputs:
        snapshot = root/'editorial/inputs'/item['sha256']/Path(item['path']).name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(contents[item['path']])
        item['snapshot'] = str(snapshot)
    atomic_json(root/'editorial/source-binding.json', dict(version=1, revision=str(target),
        manifest_sha256=file_sha256(target/'manifest.json'), inputs=inputs,
        text_policy='verbatim_user_source', old_media_binding='historical_only',
        audio_binding='new_recording_required', approved=False))
    W.save(ep_id, W.new(ep_id, scope=scope), expected_revision=0)
    set_disposition(ep_id, 'current', '用户最新原稿；正文逐字导入，未配音、未渲染')
    return target
