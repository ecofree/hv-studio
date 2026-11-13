"""Editorial decisions, source anchors, pilot reviews and measured feedback.

Uses the existing episode/source/directing/timeline authorities. No approval is
inferred from old files, an import, an ASR score, or a successful render.
"""
from __future__ import annotations

import copy
import json
import math
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import paths
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.process_lock import file_lock

BRIEF_FIELDS = ('audience', 'question', 'promise', 'title', 'opening', 'opening_visual', 'closing')
CHECKS = ('story', 'voice_identity', 'pronunciation', 'emotion', 'loudness', 'visual')


def now(): return datetime.now(timezone.utc).isoformat()


def folder(ep_id): return paths.episode_dir(ep_id) / 'studio'


def read(path): return json.loads(Path(path).read_text('utf-8'))


def document(ep_id):
    from app.managers import load
    from app.orchestrator import _load_script_md
    from app.services.script_document import parse_document
    text = _load_script_md(load(ep_id))
    if not text.strip(): raise ValueError('请先导入正文')
    return parse_document(text)


def binding(ep_id):
    d = document(ep_id)
    files = {}
    for name in ('scene_plan.json', 'image_prompts.json', 'editorial/source-binding.json', 'studio/playback-policy.json', 'studio/parent.json'):
        p = paths.episode_dir(ep_id) / name
        if p.exists(): files[name] = file_sha256(p)
    return dict(source_sha256=d['source_sha256'], visual_inputs=files)


def new(ep_id, *, scope='full'):
    if scope not in ('full', 'pilot'): raise ValueError('未知制作范围')
    return dict(version=1, episode_id=ep_id, scope=scope, revision=0, binding=binding(ep_id),
                brief={k: '' for k in BRIEF_FIELDS}, target_seconds=90 if scope=='pilot' else 240,
                voice=dict(identity='', model='', baseline='', master='', delivery='自然叙述，通过重音、节奏和停顿表达情绪'),
                claims=[], beats=[], hooks=[], sound={}, cards=[])


def load(ep_id):
    p = folder(ep_id) / 'workflow.json'
    return read(p) if p.exists() else new(ep_id)


def validate(ep_id, value):
    if value.get('version') != 1 or value.get('episode_id') != ep_id:
        raise ValueError('创作方案版本或剧集不匹配')
    if set(value) - set(new(ep_id)):
        raise ValueError('未知创作方案字段')
    if value['scope'] not in ('full', 'pilot'): raise ValueError('未知制作范围')
    seconds = value['target_seconds']
    if type(seconds) is not int or not 1 <= seconds <= 36000:
        raise ValueError('请填写有效的目标时长')
    if value['scope']=='pilot' and seconds>120:
        raise ValueError('短样片目标时长不能超过 120 秒')
    if set(value['brief']) != set(BRIEF_FIELDS) or any(not isinstance(v,str) for v in value['brief'].values()):
        raise ValueError('创作卡字段不完整')
    if len(value['brief']['title']) > 18: raise ValueError('首帧标题超过 18 字，请编辑缩写')
    if not isinstance(value['voice'],dict) or set(value['voice']) != {'identity','model','baseline','master','delivery'}:
        raise ValueError('声音基准字段不完整')
    units={u['unit_id']:u['source_text'] for u in document(ep_id)['units']}
    claim_ids=set()
    for c in value['claims']:
        required={'id','unit_id','quote','type','source','locator','excerpt','uncertainty','visual_limits'}
        if not required <= set(c) or set(c)-required-{'context_source'} or not c['id'] or c['id'] in claim_ids:
            raise ValueError('主张 ID 重复或字段不完整')
        if c.get('context_source'):
            parent=read(folder(ep_id)/'parent.json')['parent_episode']
            if c['context_source']!=parent or not any(all(old.get(k)==c[k] for k in required) for old in load(parent)['claims']):
                raise ValueError('共用史料不匹配原作品记录')
        elif c['unit_id'] not in units or not c['quote'] or c['quote'] not in units[c['unit_id']]:
            raise ValueError('主张引用必须逐字对应当前正文')
        if c['type'] not in ('史籍记载','史籍指控','现代研究','作者推论','艺术示意','未知'):
            raise ValueError('请区分事实、推论与示意')
        claim_ids.add(c['id'])
    seen=set()
    for b in value['beats']:
        required={'id','unit_id','text_start','purpose','new_information','claim_ids','image_id','motion'}
        if not required <= set(b) or set(b)-required-{'animation','performance'} or b['id'] in seen:
            raise ValueError('叙事节点 ID 重复或字段不完整')
        if b['unit_id'] not in units or type(b['text_start']) is not int or not 0<=b['text_start']<len(units[b['unit_id']]):
            raise ValueError('叙事节点必须锚定正文中的有效位置')
        if not set(b['claim_ids']) <= claim_ids: raise ValueError('叙事节点引用了不存在的主张')
        from app.models.shot import MotionSpec
        from app.services.motion import resolve_motion
        resolve_motion(MotionSpec(**b['motion']))
        if b.get('animation'):
            from app.services.scene_animation import validate as validate_animation
            validate_animation(b['animation'])
        if not isinstance(b.get('performance',''),str):raise ValueError('表演标注应为文字')
        seen.add(b['id'])
    if len({(b['unit_id'],b['text_start']) for b in value['beats']}) != len(value['beats']):
        raise ValueError('多个视觉事件不能使用同一个正文锚点')
    if not isinstance(value['hooks'],list) or len(value['hooks'])>5:
        raise ValueError('最多保留五个开场备选')
    for h in value['hooks']:
        if set(h)!={'title','opening','visual','hypothesis'} or len(h['title'])>18:
            raise ValueError('开场方案字段或字数不符合要求')
    if not isinstance(value['sound'],dict): raise ValueError('声音设计需要独立事件表')
    from app.services import overlay
    for event in value.get('cards',[]):
        if set(event)!={'at','duration_ms','card'}:raise ValueError('标题卡字段不完整')
        anchor=event['at']
        if anchor!={'at':'start'} and (set(anchor)!={'unit_id','text_start'} or anchor['unit_id'] not in units
                or type(anchor['text_start']) is not int or not 0<=anchor['text_start']<len(units[anchor['unit_id']])):
            raise ValueError('标题卡必须绑定当前正文')
        spec=overlay.Spec(start_ms=0,end_ms=event['duration_ms'],**event['card'])
        overlay.validate([spec],total_ms=event['duration_ms'])
    return value


def save(ep_id, value, *, expected_revision):
    validate(ep_id, value)
    root=folder(ep_id)
    with file_lock(root/'workflow.lock'):
        current=load(ep_id)
        if current['revision']!=expected_revision:
            raise ValueError('方案已被更新，请重新载入后保存')
        if value['binding']!=binding(ep_id): raise ValueError('正文或分镜已改变，请先重新绑定方案')
        if material(current)==material(value) and current['binding']==binding(ep_id):
            voice_value=copy.deepcopy(value);voice_value['voice']['master']=''
            atomic_json(root/'identity-anchor.json',dict(material=material(value),identity=identity(ep_id),
                voice_material=material(voice_value),voice_identity=voice_design_identity(ep_id),
                audio={k:(file_sha256(Path(value['voice'][k])) if Path(value['voice'].get(k,'')).is_file() else None) for k in ('baseline','master')}))
        v=copy.deepcopy(value);v['revision']=current['revision']+1
        atomic_json(root/'revisions'/f"{v['revision']:05}-{fingerprint(v)[:12]}.json",v)
        atomic_json(root/'workflow.json',v)
    return v


def material(value):
    v=copy.deepcopy(value)
    for key in ('revision','brief','claims','hooks','target_seconds'):
        v.pop(key,None)
    for beat in v['beats']:
        for key in ('purpose','new_information','claim_ids'): beat.pop(key,None)
    return fingerprint(v)


def identity(ep_id, value=None):
    v=copy.deepcopy(value or load(ep_id));v.pop('revision',None)
    audio={};hashed={}
    for key in ('baseline','master'):
        p=Path(v['voice'].get(key,''))
        if p.is_file():
            path=str(p.resolve())
            if path not in hashed:hashed[path]=file_sha256(p)
            audio[key]=hashed[path]
        else:audio[key]=None
    anchor=folder(ep_id)/'identity-anchor.json'
    if anchor.exists():
        row=read(anchor)
        if row.get('material')==material(v) and v['binding']==binding(ep_id):
            # File bytes remain part of identity even for editorial-only changes.
            if row.get('audio',audio)==audio: return row['identity']
        if row.get('voice_material')==material(v) and v['binding']==binding(ep_id):
            if audio==dict(baseline=row['audio']['baseline'],master=None):return row['voice_identity']
    return fingerprint([v,binding(ep_id),audio])


def voice_design_identity(ep_id):
    """Selecting the completed take does not change the design that produced it."""
    value=copy.deepcopy(load(ep_id));value['voice']['master']=''
    return identity(ep_id,value)


def narration_identity(ep_id):
    """Only inputs capable of changing spoken audio, never visual planning."""
    value=load(ep_id);voice=copy.deepcopy(value['voice']);voice.pop('master',None)
    contract=folder(ep_id)/'voice-contract.json'
    baseline=Path(voice.get('baseline',''))
    policy=folder(ep_id)/'playback-policy.json'
    return fingerprint(dict(source=document(ep_id)['source_sha256'],voice=voice,
        baseline=file_sha256(baseline) if baseline.is_file() else None,
        contract=file_sha256(contract) if contract.exists() else None,
        playback=file_sha256(policy) if policy.exists() else None,
        performance=[] if contract.exists() else [(b['unit_id'],b['text_start'],b.get('performance','')) for b in value['beats'] if b.get('performance')]))


def editorial_issues(ep_id, value=None):
    v=value or load(ep_id); issues=[]
    from app.services.studio_sources import issues as source_issues
    issues.extend(source_issues(ep_id))
    try: validate(ep_id,v)
    except (ValueError,KeyError,TypeError) as exc: return [str(exc)]
    if v['binding']['source_sha256']!=document(ep_id)['source_sha256']: issues.append('正文已改变，需重新绑定并复审')
    # Facts are confirmed once for the current script; detailed evidence is optional.
    for c in v['claims']:
        if c['type'] in ('未知','作者推论','史籍指控') and not c['uncertainty'].strip():
            issues.append('需要明确争议或推论边界：'+c['id'])
    if v['scope']=='pilot' and sum(len(u['source_text']) for u in document(ep_id)['units'])>800:
        issues.append('短样片正文过长，请单独选取一个完整段落')
    return issues


def _review_path(ep_id, kind): return folder(ep_id)/('review-'+kind+'.json')


def samples(ep_id):
    p=folder(ep_id)/'samples.json'
    return read(p) if p.exists() else []


def pilot_identity(ep_id):
    rows=[r for r in samples(ep_id) if r['design_identity']==identity(ep_id)]
    if not rows: raise ValueError('请先登记可试听、可观看的短样片')
    if not any(r['has_video'] for r in rows): raise ValueError('整集制作前还需要视频样片')
    for r in rows:
        if r['design_identity']!=identity(ep_id): raise ValueError('样片对应旧方案，请重新制作或明确重新登记并审核')
        if file_sha256(r['path'])!=r['sha256']: raise ValueError('样片文件已改变')
        proof=r.get('source_receipt')
        if proof and file_sha256(proof['path'])!=proof['sha256']:raise ValueError('样片来源记录已改变')
        if proof and proof.get('pilot_design_path'):
            if proof.get('pilot_parent_identity')!=identity(ep_id):
                raise ValueError('短样片绑定的整集方案已更新，请重新审核')
            if file_sha256(proof['pilot_design_path'])!=proof.get('pilot_design_sha256'):
                raise ValueError('短样片对应的逐幕设计已更新，请重新审核')
    return fingerprint([identity(ep_id),rows])


def _pilot_subset_binding(ep_id, manifest, candidate_inputs):
    """Bind a short sample to an exact contiguous passage in the full source."""
    pilot_path=folder(ep_id)/'pilot-design.json'
    pilot=read(pilot_path);doc=document(ep_id);current=identity(ep_id)
    ids=pilot.get('source_unit_ids',[])
    by_id={u['unit_id']:u['source_text'] for u in doc['units']}
    order=[u['unit_id'] for u in doc['units']]
    positions=[order.index(uid) for uid in ids if uid in order]
    expected_text='\n\n'.join(by_id[uid] for uid in ids if uid in by_id)
    if (pilot.get('parent_episode')!=ep_id or pilot.get('parent_identity')!=current
            or pilot.get('source_sha256')!=doc['source_sha256'] or not ids
            or len(set(ids))!=len(ids) or len(positions)!=len(ids)
            or positions!=list(range(positions[0],positions[0]+len(positions)))
            or pilot.get('source_text')!=expected_text):
        raise ValueError('短样片选择与当前整集原稿或方案不匹配')

    scoped_paths=[Path(name) for name in candidate_inputs if Path(name).name=='scoped-design.json']
    if len(scoped_paths)!=1:raise ValueError('样片缺少唯一的整集来源绑定')
    scoped_path=scoped_paths[0].resolve()
    if candidate_inputs.get(str(scoped_path))!=file_sha256(scoped_path):
        raise ValueError('样片逐幕来源记录无效')
    scoped=read(scoped_path)
    if (scoped.get('parent_episode')!=ep_id or scoped.get('parent_identity')!=current
            or scoped.get('source_units')!=ids or scoped.get('source_text')!=expected_text
            or scoped.get('target_rate')!=pilot.get('target_rate')
            or scoped.get('target_seconds')!=pilot.get('target_seconds')
            or scoped.get('acceptable_seconds')!=pilot.get('acceptable_seconds')):
        raise ValueError('样片设计与当前整集选段不一致')
    actual=[(u.get('unit_id'),u.get('text')) for u in manifest.get('alignment',[])]
    if actual!=[(uid,by_id[uid]) for uid in ids]:
        raise ValueError('样片旁白与当前整集原稿选段不一致')
    return dict(pilot_design_path=str(pilot_path.resolve()),
                pilot_design_sha256=file_sha256(pilot_path),pilot_parent_identity=current)


def replace_source(ep_id,source,*,expected_identity):
    from app.services.studio_sources import contract, require_usable
    require_usable(ep_id)
    if contract(ep_id) or (folder(ep_id)/'parent.json').exists():
        raise ValueError('此版本绑定用户原稿，请在原项目修改后导入新的独立版本；这里不改写正文')
    from app.managers import load as load_episode,save as save_episode
    from app.services.script_handoff import write_revision
    with file_lock(folder(ep_id)/'workflow.lock'):
        if identity(ep_id)!=expected_identity:raise ValueError('正文或方案已改变，请重新载入')
        target=write_revision(source,paths.episode_dir(ep_id)/'editorial/revisions',{'episode':ep_id},allow_plain=True)
        ep=load_episode(ep_id);ep.script_path=str(target/'narration.md');save_episode(ep)
    return target


def rebind_current(ep_id,*,expected_identity):
    """Keep exact anchors; archive obsolete links through immutable revisions."""
    if identity(ep_id)!=expected_identity:raise ValueError('方案已改变，请重新载入')
    v=load(ep_id);units={u['unit_id']:u['source_text'] for u in document(ep_id)['units']}
    claims=[]
    for c in v['claims']:
        if c.get('context_source'):claims.append(c)
        elif c['unit_id'] in units and c['quote'] in units[c['unit_id']]:claims.append(c)
        else:
            matches=[key for key,text in units.items() if c['quote'] in text]
            if len(matches)==1:c['unit_id']=matches[0];claims.append(c)
    ids={c['id'] for c in claims}
    beats=[b for b in v['beats'] if b['unit_id'] in units and b['text_start']<len(units[b['unit_id']])]
    for b in beats:b['claim_ids']=[c for c in b['claim_ids'] if c in ids]
    removed=dict(claims=len(v['claims'])-len(claims),beats=len(v['beats'])-len(beats))
    source_changed=v['binding']['source_sha256']!=document(ep_id)['source_sha256']
    v.update(claims=claims,beats=beats,binding=binding(ep_id),sound={} if source_changed else v['sound'])
    save(ep_id,v,expected_revision=v['revision'])
    return removed


def save_image_brief(ep_id,key,prompt,*,expected_identity,model=None,shot_type=None):
    paths.validate_episode_id(key)
    if not prompt.strip():raise ValueError('请填写主体、行动、环境与道具')
    from app.orchestrator import _word_hit,_STYLE_BANNED_LATIN,_STYLE_BANNED_CJK
    if _word_hit(prompt.lower(),_STYLE_BANNED_LATIN) or any(x in prompt for x in _STYLE_BANNED_CJK):
        raise ValueError('底图需求只描述内容，现有画风由项目配置统一提供')
    with file_lock(folder(ep_id)/'workflow.lock'):
        if identity(ep_id)!=expected_identity:raise ValueError('方案已变化，请刷新')
        p=paths.episode_dir(ep_id)/'image_prompts.json'
        values=read(p) if p.exists() else {}
        previous=values.get(key,{})
        entry=dict(previous) if isinstance(previous,dict) else {}
        entry['en']=prompt
        if shot_type is not None:
            if shot_type:
                entry['shot_type'] = shot_type
            else:
                entry.pop('shot_type', None)
        if model is not None:
            if model:
                entry['model']=model
            else:
                entry.pop('model',None)
        if entry==previous or (isinstance(previous,str) and entry=={'en':previous}):
            return
        if p.exists():atomic_json(folder(ep_id)/'visual-history'/(file_sha256(p)+'.json'),values)
        values[key]=entry
        atomic_json(p,values)


def assign_scene_models(ep_id, keys, *, expected_identity, model=None):
    """Set a model for selected ungenerated plates, preserving previously bound requests."""
    from app.config.config import get_settings
    from app.orchestrator import _episode_lock, build_image_jobs
    from app.services.production_admission import approved_image_requests
    from app.services.scene_models import plan_scene_models
    from app.services.existing_images import include_registered
    from app.services.studio_image_reuse import apply
    with _episode_lock(ep_id), file_lock(folder(ep_id)/'workflow.lock'):
        if identity(ep_id) != expected_identity:
            raise ValueError('分镜已更新，请刷新')
        if model and not any(c.supports(model) for c in get_settings().channels):
            raise ValueError('当前启用的渠道没有该型号')
        if not keys:
            return {}
        for key in keys:
            paths.validate_episode_id(key)
        directory = paths.episode_dir(ep_id)
        target = directory/'image_prompts.json'
        values = read(target)
        jobs = build_image_jobs(ep_id, only=set(keys), create_output_dir=False)
        jobs = apply(ep_id, include_registered(ep_id, jobs))
        scene = directory/'scene_plan.json'
        directing = directory/'directing.json'
        root = directory/'production/v2'
        plan_scene_models(root, jobs, values, scene=read(scene) if scene.is_file() else {},
                          directing=read(directing) if directing.is_file() else {},
                          approved=approved_image_requests(ep_id), persist=False)
        state = root/'run.json'
        existing = read(state).get('image_assets', {}) if state.is_file() else {}
        assignments = {}
        for job in jobs:
            if job.id not in keys or getattr(job, 'imported', False) or job.model_bound or job.id in existing or job.out.is_file():
                continue
            value = values[job.id]
            entry = dict(value) if isinstance(value, dict) else {'en': value}
            if entry.get('model'):
                continue
            entry['model'] = assignments[job.id] = model or job.req.model
            values[job.id] = entry
        if assignments:
            atomic_json(folder(ep_id)/'visual-history'/(file_sha256(target)+'.json'), read(target))
            atomic_json(target, values)
        return assignments


def update_scene_bindings(ep_id):
    v=load(ep_id);doc=document(ep_id)
    p=paths.episode_dir(ep_id)/'image_prompts.json'
    prompts=read(p) if p.exists() else {}
    rows=[];previous=None
    old_path=paths.episode_dir(ep_id)/'scene_plan.json'
    existing={r['unit_id']:r for r in read(old_path).get('unit_scenes',[])} if old_path.exists() else {}
    for u in doc['units']:
        beats=[b for b in v['beats'] if b['unit_id']==u['unit_id']]
        if not beats:
            previous=existing.get(u['unit_id'],previous)
            if previous is None:raise ValueError('请为开场选择底图；后续无新节点的段落沿用前一场景')
            rows.append(dict(previous,unit_id=u['unit_id']));continue
        b=min(beats,key=lambda b:b['text_start'])
        if any(b['image_id'] not in prompts for b in beats):raise ValueError('分镜引用的底图需求尚未填写')
        previous=dict(unit_id=u['unit_id'],image=b['image_id'],motion=b['motion'])
        rows.append(previous)
    target=paths.episode_dir(ep_id)/'scene_plan.json'
    old=read(target) if target.exists() else {}
    if target.exists():atomic_json(folder(ep_id)/'visual-history'/(file_sha256(target)+'.json'),old)
    atomic_json(target,dict(episode=ep_id,source_sha256=doc['source_sha256'],authored=True,
                            review_required=True,unit_scenes=rows,images=old.get('images',{})))
    v['binding']=binding(ep_id)
    return save(ep_id,v,expected_revision=v['revision'])


def review_valid(ep_id, kind):
    p=_review_path(ep_id,kind)
    if not p.exists(): return False
    r=read(p)
    if kind=='editorial' and r.get('scope')=='key_facts_v1':
        return (r.get('source_sha256')==document(ep_id)['source_sha256']
                and r.get('decision')=='approved' and bool(r.get('reviewer')) and bool(r.get('finding'))
                and r.get('checks',{}).get('facts_and_story') is True)
    try: expected=identity(ep_id) if kind=='editorial' else pilot_identity(ep_id)
    except (OSError,ValueError): return False
    return (r.get('identity')==expected and r.get('decision')=='approved'
            and bool(r.get('reviewer')) and bool(r.get('finding')))


def record_review(ep_id,kind,reviewer,finding,decision,*,expected_identity,checks):
    if kind not in ('editorial','pilot') or decision not in ('approved','rejected'):
        raise ValueError('无效审核类型或结论')
    if not reviewer.strip() or not finding.strip(): raise ValueError('请填写审核人及实际发现')
    with file_lock(folder(ep_id)/'workflow.lock'):
        current=identity(ep_id) if kind=='editorial' else pilot_identity(ep_id)
        if current!=expected_identity: raise ValueError('审核材料已改变，请重新载入')
        if decision=='approved':
            if kind=='editorial':
                issues=editorial_issues(ep_id)
                if issues: raise ValueError('；'.join(issues))
                if checks.get('facts_and_story') is not True: raise ValueError('需要确认关键事件、人物时序和争议边界')
            elif not all(checks.get(k) is True for k in CHECKS):
                raise ValueError('请分别确认内容、声线、读音、情绪、响度和画面')
        row=dict(kind=kind,identity=current,reviewer=reviewer.strip(),finding=finding.strip(),
                 decision=decision,checks=checks,reviewed_at=now())
        if kind=='editorial':row.update(scope='key_facts_v1',source_sha256=document(ep_id)['source_sha256'])
        atomic_json(folder(ep_id)/'reviews'/(uuid.uuid4().hex+'.json'),row)
        atomic_json(_review_path(ep_id,kind),row)
    return row


def sample_source_proof(ep_id,path,receipt=None):
    """A new script cannot acquire old media merely by registering its pathname."""
    from app.services.studio_sources import contract, require_usable
    require_usable(ep_id)
    if not contract(ep_id) and not (folder(ep_id)/'parent.json').exists():return None
    path=Path(path).resolve()
    proof=Path(receipt) if receipt else path.parent/('source-map.json' if path.suffix.lower()=='.wav' else 'candidate.json')
    if not proof.is_file():raise ValueError('当前原稿的样片必须提供制作来源记录，不能把旧素材直接登记为新版')
    r=read(proof)
    if path.suffix.lower()=='.wav':
        design_matches=(r.get('design_identity')==identity(ep_id)
                        or r.get('voice_design_identity')==voice_design_identity(ep_id))
        if (Path(r.get('master','')).resolve()!=path or r.get('sha256')!=file_sha256(path)
                or not design_matches or r.get('source_sha256')!=document(ep_id)['source_sha256']):
            raise ValueError('音频来源记录不对应当前文案和设计')
        from app.services.studio_media import speed_plan
        policy=folder(ep_id)/'playback-policy.json'
        target=read(policy)['target_rate'] if policy.exists() else 1.
        speed_plan(path,r.get('speed_receipt'),target,subtitles_aligned=True)
    else:
        if Path(r.get('candidate_path','')).resolve()!=path or r.get('candidate_sha256')!=file_sha256(path):
            raise ValueError('视频来源记录与实际文件不一致')
        manifest_path=Path(r['manifest_path'])
        if r.get('inputs',{}).get(str(manifest_path.resolve()))!=file_sha256(manifest_path):
            raise ValueError('视频旁白清单没有有效版本绑定')
        manifest=read(manifest_path)
        actual=[(u['unit_id'],u['text']) for u in manifest['alignment']]
        pilot_path=folder(ep_id)/'pilot-design.json'
        scoped_paths=[Path(name) for name in r.get('inputs',{})
                      if Path(name).name=='scoped-design.json']
        if pilot_path.is_file() and len(scoped_paths)==1:
            # A short pilot is expected to cover a selected, exact source range,
            # not every unit in the full episode. Bind that range to the current
            # parent design and require the child receipt to carry the same range.
            pilot_binding=_pilot_subset_binding(ep_id,manifest,r['inputs'])
        else:
            expected=[(u['unit_id'],u['source_text']) for u in document(ep_id)['units']]
            direction=paths.episode_dir(ep_id)/'directing.json'
            if not (folder(ep_id)/'direction-binding.json').is_file():raise ValueError('视频缺少当前导演方案的版本记录')
            require_direction_current(ep_id)
            if actual!=expected or not direction.is_file() or r.get('inputs',{}).get(str(direction.resolve()))!=file_sha256(direction):
                raise ValueError('视频文案或导演安排不对应当前版本')
        changed=[]
        workflow_file=str(Path(__file__).resolve())
        for filename,sha in r['inputs'].items():
            actual_sha=file_sha256(filename)
            if actual_sha!=sha:
                if str(Path(filename).resolve())==workflow_file:
                    # This module owns approval/source registration only; it is
                    # not imported by the renderer or audio pipeline. Keep the
                    # reviewed output usable while recording this narrow drift.
                    changed.append(dict(path=workflow_file,expected_sha256=sha,actual_sha256=actual_sha,
                                        effect='workflow validation only'))
                else:raise ValueError('视频输入已改变，需重新核对样片来源')
    result=dict(path=str(proof.resolve()),sha256=file_sha256(proof))
    if path.suffix.lower()!='.wav' and 'pilot_binding' in locals():
        result.update(pilot_binding)
        if changed:result['review_only_input_drift']=changed
    return result


def register_sample(ep_id,path,label,*,expected_identity,source_receipt=None):
    from app.services.studio_workspace import library_path
    path=library_path(path)
    if not path.is_file() or not label.strip(): raise ValueError('请选择实际文件并填写名称')
    proof=sample_source_proof(ep_id,path,source_receipt)
    before=file_sha256(path)
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type','-of','json',str(path)],capture_output=True,text=True,timeout=20)
        if result.returncode:raise ValueError('文件无法读取为音频或视频，请检查所选文件')
        probe=json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:raise ValueError('媒体检查超时，请检查文件是否可读取') from exc
    duration=float(probe['format']['duration'])
    if not math.isfinite(duration) or not 1<=duration<=120.1: raise ValueError('请使用 1–120 秒的短样片')
    types={s['codec_type'] for s in probe['streams']}
    if 'audio' not in types: raise ValueError('样片必须包含可试听的声音')
    with file_lock(folder(ep_id)/'workflow.lock'):
        if identity(ep_id)!=expected_identity or file_sha256(path)!=before:
            raise ValueError('方案或样片在登记时发生改变')
        row=dict(id=uuid.uuid4().hex,label=label.strip(),path=str(path),sha256=before,
                 duration=duration,has_video='video' in types,design_identity=expected_identity,registered_at=now())
        if proof:
            if file_sha256(proof['path'])!=proof['sha256']:raise ValueError('登记时样片来源记录发生改变')
            row['source_receipt']=proof
        rows=samples(ep_id);rows.append(row);atomic_json(folder(ep_id)/'samples.json',rows)
    return row


def status(ep_id):
    v=load(ep_id);issues=editorial_issues(ep_id,v)
    editorial=not issues and review_valid(ep_id,'editorial')
    pilot=review_valid(ep_id,'pilot')
    blockers=list(issues)
    if not editorial: blockers.append('当前正文的关键事件待确认')
    # Pilots are optional experiments, never mandatory for a mature series.
    return dict(scope=v['scope'],editorial_approved=editorial,pilot_approved=pilot,
                ready_for_generation=not blockers,blockers=blockers,identity=identity(ep_id),
                production_ready=False)


def require_production(ep_id,stages,*,external_master=None):
    from app.services.studio_sources import require_usable
    require_usable(ep_id)
    from app.services.studio_workspace import config
    # Standalone original projects and isolated fixtures retain their own contracts.
    active=paths.episode_dir(ep_id).resolve()
    if not config(paths.ROOT) or not active.is_relative_to((paths.ROOT/'workspace').resolve()): return
    if not set(stages)&{'images','tts','timeline','compose'}: return
    s=status(ep_id)
    if not s['ready_for_generation']: raise ValueError('制作前检查：'+'；'.join(s['blockers']))
    if 'tts' in stages:
        voice=load(ep_id)['voice']
        if not external_master and (not voice.get('identity') or not voice.get('model')):
            raise ValueError('合成新旁白需要选择声音方案；已有录音可直接传入完整母带')
        for key in ('baseline','master'):
            if key=='master' and external_master:continue  # Explicit input is checked by require_voice_binding.
            if voice.get(key) and not (Path(voice[key]).is_file() or (key=='master' and Path(voice[key]).is_dir())):
                raise ValueError('声音输入不存在：'+key)


def direction_design(v,doc):
    if any(not b['image_id'] for b in v['beats']): raise ValueError('视觉节点缺少图片')
    order={u['unit_id']:i for i,u in enumerate(doc['units'])}
    beats=sorted(v['beats'],key=lambda b:(order[b['unit_id']],b['text_start']))
    if beats[0]['unit_id']!=doc['units'][0]['unit_id'] or beats[0]['text_start']!=0:
        raise ValueError('第一个视觉事件必须从首段正文起点开始')
    return dict(version=1,source_identity=fingerprint([(u['unit_id'],u['source_text']) for u in doc['units']]),
                visual_events=[dict(at=({'at':'start'} if i==0 else dict(unit_id=b['unit_id'],text_start=b['text_start'])),
                                    image_id=b['image_id'],motion=b['motion'],purpose=b['purpose'],
                                    **({'animation':b['animation']} if b.get('animation') else {})) for i,b in enumerate(beats)],
                cards=copy.deepcopy(v.get('cards',[])),sound=v['sound'])


def compile_direction(ep_id):
    """Write the existing directing.json protocol; acoustic compilation stays in v2."""
    from app.services.studio_sources import require_usable
    require_usable(ep_id)
    v=load(ep_id)
    issues=editorial_issues(ep_id,v)
    if issues: raise ValueError('；'.join(issues))
    if not review_valid(ep_id,'editorial'): raise ValueError('请先确认当前正文的关键事件')
    if not v['beats']:
        # Removing all studio events deliberately returns to the scene plan.
        p=paths.episode_dir(ep_id)/'directing.json';bound=folder(ep_id)/'direction-binding.json'
        if bound.exists():
            if p.exists():
                atomic_json(folder(ep_id)/'direction-history'/(file_sha256(p)+'.json'),read(p));p.unlink()
            atomic_json(folder(ep_id)/'direction-history'/(file_sha256(bound)+'.json'),read(bound));bound.unlink()
        return None
    design=direction_design(v,document(ep_id))
    p=paths.episode_dir(ep_id)/'directing.json'
    if p.exists(): atomic_json(folder(ep_id)/'direction-history'/(file_sha256(p)+'.json'),read(p))
    atomic_json(p,design)
    atomic_json(folder(ep_id)/'direction-binding.json',dict(design_identity=identity(ep_id),sha256=file_sha256(p)))
    return p


def require_direction_current(ep_id):
    p=folder(ep_id)/'direction-binding.json'
    if p.exists():
        r=read(p);d=paths.episode_dir(ep_id)/'directing.json'
        if r['design_identity']!=identity(ep_id) or not d.exists() or r['sha256']!=file_sha256(d):
            raise ValueError('导演事件与当前方案不一致，请重新编译')


def require_voice_binding(ep_id,external_master,voice_asset=None):
    from app.services.studio_workspace import config
    active=paths.episode_dir(ep_id).resolve()
    if not config(paths.ROOT) or not active.is_relative_to((paths.ROOT/'workspace').resolve()):return
    if external_master:
        from app.services.studio_workspace import library_path
        selected=load(ep_id)['voice'].get('master')
        master=library_path(external_master)
        if not master.is_file(): raise ValueError('所选旁白文件不存在')
        if selected and master!=library_path(selected):
            from app.services.recording_input import select, parts_receipt
            original = library_path(selected)
            parts = parts_receipt(master)
            if parts:
                source = Path(parts['selection']['source'])
                matches = source==original or (source.is_dir() and original.parent==source)
            else:
                kind, files = select(original)
                matches = kind=='master' and files[0]==master
            if not matches:raise ValueError('传入旁白与已选母带不同，请明确更换选定输入')
        from app.services import studio_audio_excerpt as excerpt
        if excerpt.receipt_path(master).exists():
            excerpt.validate(ep_id,master)
            return
        edit_path=master.parent/'edit.json'
        if master.is_relative_to(active/'production/v2/audio-edits') and edit_path.is_file():
            edit=read(edit_path)
            if edit.get('kind')=='external_pickup_v1':
                if (edit.get('episode')!=ep_id or edit['derived_master_sha256']!=file_sha256(master)
                        or edit['narration_identity']!=narration_identity(ep_id)):
                    raise ValueError('补录版本与当前正文、声音或文件不一致')
                return
        excerpt.reject_uncut_parent(ep_id,master)
        from app.services.studio_sources import contract
        if contract(ep_id):
            from app.services.studio_media import speed_plan
            policy=folder(ep_id)/'playback-policy.json'
            target=read(policy)['target_rate'] if policy.exists() else 1.0
            receipt=master.with_suffix('.speed.json')
            if not receipt.exists() and target==1.0:
                return  # Source/audio agreement is checked by the local alignment stage.
            plan=speed_plan(master,receipt,target,subtitles_aligned=True)
            if plan['source_identity']!=document(ep_id)['source_sha256']:
                raise ValueError('选定旁白对应旧文案，不能绑定为当前原稿配音')
        return
    from app.services.voice_assets import resolve_voice_asset
    _,asset=resolve_voice_asset(path=voice_asset)
    voice=load(ep_id)['voice']
    if voice.get('master'):raise ValueError('已选定外部旁白，请明确使用该文件，不能改走重新合成')
    if voice['identity']!=asset['voice_id'] or voice['model']!=asset['model']:
        raise ValueError('当前引擎音色与创作方案不一致。请使用既有旁白入口准备音轨并明确导入，禁止静默换声音。')


def add_observation(ep_id,row):
    """Append measured data; unknown values stay null, never manufacture retention."""
    required={'publication_id','version_sha256','observed_at','window_hours','duration_seconds','topic','hypothesis','plays','avg_watch_seconds','likes','comments','shares','saves','follows','retention_3s','retention_5s','traffic_source','notes'}
    if set(row)!=required: raise ValueError('复盘记录字段不完整')
    if not all(str(row[k]).strip() for k in ('publication_id','version_sha256','observed_at','topic','hypothesis','traffic_source')):
        raise ValueError('请记录作品版本、观测窗口、题材和测试假设')
    import re
    if not re.fullmatch('[0-9a-f]{64}',row['version_sha256']): raise ValueError('作品版本应使用成片 SHA256')
    datetime.fromisoformat(row['observed_at'])
    for k in ('window_hours','duration_seconds'):
        if isinstance(row[k],bool) or not isinstance(row[k],(int,float)) or not math.isfinite(row[k]) or row[k]<=0:
            raise ValueError(k+' 必须大于零')
    for k in ('plays','avg_watch_seconds','likes','comments','shares','saves','follows','retention_3s','retention_5s'):
        x=row[k]
        if x is not None and (isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0):
            raise ValueError(k+' 必须是非负数或留空')
        if x is not None and k.startswith('retention_') and x>100: raise ValueError('留存百分比不能超过 100')
        if x is not None and k in ('plays','likes','comments','shares','saves','follows') and int(x)!=x:
            raise ValueError('次数应为整数')
    p=folder(ep_id)/'observations'/(uuid.uuid4().hex+'.json')
    atomic_json(p,dict(row,recorded_at=now()))
    return p


def observations(ep_id):
    return [read(p) for p in sorted((folder(ep_id)/'observations').glob('*.json'))]
