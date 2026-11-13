"""Explicit scene reuse of existing plates; no provider calls or asset edits."""
import wave
from functools import lru_cache
from pathlib import Path
from app import paths
from app.services import studio_workflow as W
from app.services.provenance import atomic_json,file_sha256,fingerprint


def assign(eid, target_keys, source_key, *, expected_identity):
    from app.orchestrator import _episode_lock
    from PIL import Image
    with _episode_lock(eid):
        if W.identity(eid)!=expected_identity:raise ValueError('方案已变化，请刷新')
        state=W.read(paths.episode_dir(eid)/'production/v2/run.json')
        source=state.get('image_assets',{}).get(source_key)
        if not source:raise ValueError('请先选择已登记的底图')
        with Image.open(source) as im:im.load()
        declared=set(state.get('declared_image_keys',[]))
        if not target_keys or not set(target_keys)<=declared:raise ValueError('请选择当前镜头')
        path=W.folder(eid)/'image-reuse.json'
        row=W.read(path) if path.exists() else {}
        sha=W.document(eid)['source_sha256']
        if row.get('source_sha256')!=sha:row=dict(source_sha256=sha,images={})
        for key in target_keys:row['images'][key]=dict(path=str(source),sha256=file_sha256(source))
        atomic_json(path,row)
        root=paths.episode_dir(eid)/'production/v2'
        atomic_json(root/'run-history'/(fingerprint(state)+'.json'),state)
        for key in ('timeline_path','receipt_path','full_episode_registration'):
            state.pop(key,None)
        state['last_result']=dict(status='PREPARED',final_mp4=None,approved=False,
                                  message='用图安排已更新；现有媒体保留，待重新构建时间轴')
        atomic_json(root/'run.json',state)


def apply(eid,jobs):
    from app.services.existing_images import ExistingImage
    path=W.folder(eid)/'image-reuse.json'
    if not path.exists():return jobs
    row=W.read(path)
    if row.get('source_sha256')!=W.document(eid)['source_sha256']:
        raise ValueError('正文已变化，请重新选择复用底图')
    result=[]
    for job in jobs:
        if job.id in row['images']:
            job=ExistingImage(job.id,row['images'][job.id],path,dict(source_sha256=row['source_sha256']))
            job.is_done()
        result.append(job)
    return result


@lru_cache(maxsize=512)
def _display_digest(path, size, modified, changed):
    return file_sha256(path)


def _matches_file(path, expected):
    path=Path(path)
    if not path.is_file():return False
    st=path.stat()
    return _display_digest(path.resolve(),st.st_size,st.st_mtime_ns,st.st_ctime_ns)==expected


def validate_scene_source(scene, document):
    bound = scene.get('source_sha256')
    if bound and bound != document['source_sha256']:
        raise ValueError('分镜属于旧版正文，请重新绑定')
    if 'unit_scenes' in scene:
        expected = [u['unit_id'] for u in document['units']]
        if [r['unit_id'] for r in scene['unit_scenes']] != expected:
            raise ValueError('分镜单元与当前正文的内容或顺序不符')


def _coverage(eid, state, doc, master, scene_path, audio_mode, prepared, planned):
    from app.managers import load
    from app.services.production_timeline import scene_bindings
    root = paths.episode_dir(eid)
    plan = W.read(scene_path) if scene_path and Path(scene_path).is_file() else {}
    state_current = state.get('source_sha256') == doc['source_sha256']
    if plan:
        validate_scene_source(plan, doc)
    audio_path = state.get('manifest_path')
    manifest = W.read(audio_path) if state_current and audio_path and Path(audio_path).is_file() else {}
    files = []
    if master:
        from app.services.recording_input import select
        kind, files = select(master,audio_mode)
        master = files[0] if kind=='master' else None
        if kind=='parts' and manifest:
            parts = manifest.get('source_recording',{}).get('numbered_parts',{})
            records=parts.get('selection',{}).get('files',[])
            if [r['path'] for r in records]!=[str(p) for p in files] or any(not _matches_file(r['path'],r['sha256']) for r in records):
                manifest={}
    if manifest and master:
        selected = manifest.get('source_recording', {}).get('delivered_path') or manifest.get('master_path')
        recorded_sha=manifest.get('source_recording',{}).get('delivered_sha256') or manifest.get('master_sha256')
        if not selected or Path(selected).resolve()!=Path(master).resolve() or not _matches_file(master,recorded_sha):
            manifest = {}
    if manifest.get('plan_path'):
        active_plan=Path(manifest['plan_path'])
        if (not active_plan.is_file() or file_sha256(active_plan)!=manifest.get('plan_sha256')
                or W.read(active_plan).get('source_sha256')!=doc['source_sha256']):
            raise ValueError('分镜时长所用的旁白清单已过期')
    clock = manifest.get('sample_rate', 1)
    total = manifest.get('samples', 0)
    if not total and files:
        from app.services.recording_input import duration
        total, clock = duration(files), 1
    duration = total / clock
    generated = bool(scene_path and Path(scene_path).parent.parent.name == 'storyboards')
    declared = Path(scene_path).parent/'image_prompts.json' if scene_path else root/'image_prompts.json'
    if generated or not plan:
        brief = root/'production/v2/source_image_briefs.json'
        payload = W.read(brief) if brief.is_file() else {}
        prompts = payload.get('images', {}) if payload.get('source_sha256') == doc['source_sha256'] else {}
    else:
        prompts = W.read(declared) if declared.is_file() else {}
    units = doc['units']
    rows = plan.get('unit_scenes', [])
    from app.services.production_inputs import input_files
    current_inputs=input_files(eid)
    same_inputs=state.get('image_inputs',current_inputs)==current_inputs
    effective = dict(state.get('image_assets', {})) if state_current and same_inputs else {}
    if prepared is not None:
        for job in prepared:
            if job.is_done():effective[job.id]=str(job.out)
            else:effective.pop(job.id,None)
    local = {k for k,v in plan.get('images',{}).items() if v.get('local_only')}
    for key,spec in plan.get('images',{}).items():
        if spec.get('path') and spec.get('sha256'):
            p=Path(spec['path']).resolve()
            if not p.is_relative_to(paths.ROOT.resolve()) or not _matches_file(p,spec['sha256']):
                raise ValueError('声明的已有底图缺失或已改变：'+key)
            effective[key]=str(p)
    reuse = W.folder(eid)/'image-reuse.json'
    if reuse.is_file():
        selection = W.read(reuse)
        if selection.get('source_sha256') != doc['source_sha256']:
            raise ValueError('复用图片记录属于旧版正文')
        for key, asset in selection['images'].items():
            path = Path(asset['path'])
            if not path.is_file() or file_sha256(path) != asset['sha256']:
                raise ValueError('复用底图已改变：'+key)
            effective[key] = str(path)
    old_timeline = state.get('timeline_path')
    timeline = W.read(old_timeline) if manifest and old_timeline and Path(old_timeline).is_file() else {}
    inputs = timeline.get('inputs', {})
    exact = bool(plan and timeline and scene_path and same_inputs
                 and str(Path(scene_path).resolve()) in inputs
                 and timeline.get('alignment_evidence_path') == state.get('alignment_path')
                 and all(Path(p).is_file() and file_sha256(p)==sha for p,sha in inputs.items()))
    preview = bool(scene_path and not Path(scene_path).resolve().is_relative_to(root.resolve()))
    direction = (Path(scene_path).parent if preview else root)/'directing.json'
    design = W.read(direction) if direction.is_file() else {}
    workflow_path=W.folder(eid)/'workflow.json'
    workflow=W.read(workflow_path) if workflow_path.is_file() else {}
    pending_design=False
    if not preview and workflow.get('beats') and workflow['binding']['source_sha256']==doc['source_sha256']:
        draft=W.direction_design(workflow,doc)
        pending_design=draft!=design
        if planned or not exact:
            if pending_design:exact=False
            design=draft
    if direction.is_file() and str(direction.resolve()) not in inputs:exact=False
    if exact:
        spans = [(s['image_id'], s['sample_start'], s['sample_end']) for s in timeline['shots']]
        timing = 'timeline'
    else:
        if 'scenes' in plan:
            narration = {'alignment':[{'text':u['source_text']} for u in units]}
            bindings = scene_bindings(load(eid), narration, plan)
            spans = [(b['image'], b['source_start'], b['source_end']) for b in bindings]
            denom = spans[-1][2] if spans else 0
            spans = [(k,total*a/denom,total*b/denom) for k,a,b in spans] if denom else []
        else:
            if not rows:
                rows = [dict(unit_id=u['unit_id'],image=f'unit-{i:03}') for i,u in enumerate(units)]
            spans = []
            if manifest:
                if [u['unit_id'] for u in manifest['alignment']] != [u['unit_id'] for u in rows]:
                    raise ValueError('所选音轨的单元与分镜不符')
                bounds = [0]+[u['sample_start'] for u in manifest['alignment'][1:]]+[total]
            else:
                lengths = [len(u['source_text'].strip()) for u in units]
                from itertools import accumulate
                length = max(1,sum(lengths))
                bounds = [0]+[total*x/length for x in accumulate(lengths)]
            spans = [(s['image'],a,b) for s,a,b in zip(rows,bounds,bounds[1:])]
        timing = 'estimate'
        evidence_path=state.get('alignment_path')
        evidence=W.read(evidence_path) if manifest and evidence_path and Path(evidence_path).is_file() else {}
        if evidence and evidence.get('manifest_content_sha256')!=fingerprint(manifest):
            raise ValueError('声学报告与所选录音不符')
        if evidence and plan:
            from app.services.production_timeline import timed_scenes
            spans=[(s['image'],a,b) for s,a,b in timed_scenes(load(eid),manifest,plan,evidence)]
            timing='aligned'
        events=design.get('visual_events',[])
        if events:
            if design.get('source_identity')!=fingerprint([(u['unit_id'],u['source_text']) for u in units]):
                raise ValueError('导演节点属于旧版旁白')
            if evidence:
                from app.services.directing import anchor_sample
                starts=[anchor_sample(e['at'],manifest,evidence) for e in events]
                timing='aligned'
            else:
                from itertools import accumulate
                weights=[len(u['source_text']) for u in units]
                offsets=dict(zip([u['unit_id'] for u in units],accumulate([0]+weights)))
                starts=[0 if e['at']=={'at':'start'} else total*(offsets[e['at']['unit_id']]+e['at']['text_start'])/max(1,sum(weights)) for e in events]
            if total and (starts[0]!=0 or starts!=sorted(set(starts)) or starts[-1]>=total):
                raise ValueError('导演节点时间范围无效')
            spans=[(e['image_id'],a,b) for e,a,b in zip(events,starts,starts[1:]+[total])]
    keys = {k for k,_,_ in spans}
    local_missing = sorted(k for k in local & keys if k not in effective or not Path(effective[k]).is_file())
    present = {k for k in keys if k in effective and Path(effective[k]).is_file()}
    names={};aliases={}
    for key in sorted(present):
        p=Path(effective[key]).resolve();stat=p.stat()
        digest=_display_digest(p,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        aliases[key]=names.setdefault(digest,key)
    grouped = []
    for key,begin,stop in spans:
        name=aliases.get(key,key)
        if not grouped or grouped[-1]['image'] != name:
            grouped.append(dict(image=name,start_seconds=begin/clock,duration_seconds=0.,units=0,timing=timing))
        grouped[-1]['duration_seconds'] += (stop-begin)/clock
        grouped[-1]['units'] += 1
    contexts=[]
    align=manifest.get('alignment',[])
    for g in grouped:
        begin=g['start_seconds']*clock;end=begin+g['duration_seconds']*clock
        lines=[u.get('text','') for u in align if u.get('sample_end',0)>begin and u['sample_start']<end]
        subject=prompts.get(g['image'],{})
        contexts.append({'底图':g['image'],'开始（秒）':round(g['start_seconds'],2),
                         '连续（秒）':round(g['duration_seconds'],2),'涉及片段':g['units'],
                         '开头旁白':lines[0][:42] if lines else '', '结尾旁白':lines[-1][-42:] if lines else '',
                         '画面描述':subject.get('en',subject.get('prompt',''))})
    return dict(status='available' if total and grouped else 'waiting', duration_seconds=duration,review_rows=contexts,pending_design=pending_design,
                spans=grouped, asset_count=len({s['image'] for s in grouped}),
                missing_briefs=sorted(keys-set(prompts)-present-local), missing_assets=sorted(keys-present), missing_local=local_missing,
                reason='' if total and grouped else '尚缺已选旁白的实际时长或分镜',
                longest_seconds=max((s['duration_seconds'] for s in grouped),default=0.))


def coverage_report(eid, *, state=None, document=None, master=None, scene_path=None,audio_mode='auto', prepared=None, planned=False):
    """One read-only estimate for the planning screen and the production worker."""
    root = paths.episode_dir(eid)
    try:
        if state is None:
            p = root/'production/v2/run.json'
            state = W.read(p) if p.is_file() else {}
        doc = document if document is not None else W.document(eid)
        if scene_path is None:
            p = root/'scene_plan.json'
            scene_path = p if p.is_file() else state.get('scene_plan_path')
        return _coverage(eid,state,doc,master,scene_path,audio_mode,prepared,planned)
    except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error) as exc:
        return dict(status='invalid', reason=str(exc), spans=[], review_rows=[], duration_seconds=0.,
                    asset_count=0, longest_seconds=0., missing_briefs=[], missing_assets=[], missing_local=[])
