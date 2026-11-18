"""Read-only production/quality projection. No second state machine or approvals."""
from pathlib import Path
import html
import json
from app import paths
from app.services.provenance import file_sha256, fingerprint
from app.services.narration_pipeline import read


def execution_overview(ep_id):
    from app.services import studio_jobs, batch_worker
    rows=[];errors=[]
    try:
        for row in studio_jobs.jobs(ep_id, diagnostics=errors):
            rows.append(dict(origin='studio',path=row['path'],status=row['status'],stage=row.get('stage'),
                             running=row['running'],budget=row.get('budget'),reason=row.get('error') or row.get('recovery_issue')))
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        errors.append('studio: '+str(exc))
    for p in batch_worker.queue_dir().glob('*.json'):
        if p.name=='worker.json':continue
        try:
            job=read(p);row=job.get('episodes',{}).get(ep_id)
            if not row:continue
            live=row.get('status')=='RUNNING' and batch_worker.alive(row.get('child_pid'),row.get('child_created'))
            status='INTERRUPTED' if row.get('status')=='RUNNING' and not live else row['status']
            ledger=read(job['budget_path'])
            rows.append(dict(origin='batch',path=str(p),status=status,running=live,execution=row.get('execution'),
                reason=row.get('error') or row.get('last_contention'),attempts=row.get('attempts'),
                resource_waits=row.get('resource_waits',0),budget=dict(limits=ledger['limits'],used=ledger['used'])))
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append(p.name+': '+str(exc))
    media=None
    try:
        from app.services.production_status import get_status
        status=get_status(ep_id)
        media={k:status.get(k) for k in ('status','pipeline_version','execution','approved','content_approved','blockers','quality_pending_units','delivery_pending')}
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,ImportError) as exc:
        errors.append('V2/media: '+str(exc))
    return dict(entries=rows,media=media,errors=errors,authority='existing Studio/Batch/V2 records',production_ready=False)


def input_snapshot(ep_id,state):
    from app.managers import load
    from app.orchestrator import _load_script_md
    from app.services import studio_workflow as W, production_quality as Q
    from app.services.production_inputs import input_files
    from app.config.config import get_settings
    manifest=read(state['manifest_path']) if state.get('manifest_path') else {}
    return dict(source=fingerprint(_load_script_md(load(ep_id))),voice=W.narration_identity(ep_id),
        audio=manifest.get('master_sha256'),visual=fingerprint(input_files(ep_id)),qa=Q.decision_identity(),
        rendering=fingerprint(get_settings().compose.model_dump(mode='json')))


def impact(before,after):
    """Conservative *advice*, never a bypass of stage-specific validity gates."""
    from app.services.production_identity import V2_STAGES
    dependencies={
        'source':V2_STAGES,
        'voice':('tts','delivery','quality','alignment','timeline','compose','verify'),
        'audio':('quality','alignment','timeline','compose','verify'),
        'visual':('images','timeline','compose','verify'),
        'qa':('quality','verify'),
        'rendering':('compose','verify'),
    }
    changed=sorted(k for k in set(before)|set(after) if before.get(k)!=after.get(k))
    stages=set()
    for k in changed:stages.update(dependencies.get(k,V2_STAGES))
    return dict(changed=changed,suggested_stages=[s for s in V2_STAGES if s in stages],
                automatic_execution=False,requires_current_stage_validation=True)


def report(ep_id, *, max_hold_seconds=60., max_subtitle_cps=12.):
    paths.validate_episode_id(ep_id)
    import math
    if not all(math.isfinite(v) and v>0 for v in (max_hold_seconds,max_subtitle_cps)):
        raise ValueError('review thresholds must be finite and positive')
    folder=paths.episode_dir(ep_id);p=folder/'production/v2/run.json'
    state=read(p) if p.is_file() else {};errors=[]
    value=dict(schema_version=1,episode_id=ep_id,execution=state.get('execution'),
               operational=execution_overview(ep_id),listening=[],shots=[],subtitle_flags=[],
               review_required=True,approved=False,production_ready=False,errors=errors)
    try:value['inputs']=input_snapshot(ep_id,state)
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:errors.append('input identity: '+str(exc))
    if state.get('quality_path'):
        try:
            from app.services.production_quality import review_queue
            value['listening']=review_queue(state['quality_path'],all_units=True)
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append('audio QA: '+str(exc))
    manifest={};timeline={}
    if state.get('manifest_path'):
        try:
            from app.services.narration_pipeline import validate_assembly
            manifest=validate_assembly(state['manifest_path'])
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append('narration: '+str(exc))
    sr=manifest.get('sample_rate');samples=manifest.get('samples')
    if state.get('timeline_path') and sr:
        try:
            timeline=read(state['timeline_path'])
            from app.services.production_v2 import validate_timeline
            validate_timeline(manifest,timeline)
            from app.services.studio_attraction import rhythm_points
            value['attraction_points'] = rhythm_points(timeline, sr)
            prompts=read(folder/'image_prompts.json') if (folder/'image_prompts.json').is_file() else {}
            previous=None
            for shot in timeline['shots']:
                image=Path(shot['image_path']);sha=file_sha256(image)
                start,end=shot['sample_start']/sr,shot['sample_end']/sr
                # Adjacent shots using the same actual bytes count as one hold.
                if previous and previous['image_sha256']==sha and abs(previous['end_seconds']-start)<1/sr:
                    previous['end_seconds']=end
                else:
                    previous=dict(image_path=str(image),image_sha256=sha,start_seconds=start,end_seconds=end,references=[])
                    value['shots'].append(previous)
                brief=prompts.get(shot.get('image_id'),{})
                if isinstance(brief,dict):
                    refs=brief.get('reference_paths') or ([brief['reference_path']] if brief.get('reference_path') else [])
                    for ref in refs:
                        reference=Path(ref)
                        if not reference.is_absolute():reference=folder/reference
                        if reference.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):continue
                        record=dict(path=str(reference.resolve()),sha256=file_sha256(reference),review_required=True)
                        if record not in previous['references']:previous['references'].append(record)
            for hold in value['shots']:
                hold['duration_seconds']=hold['end_seconds']-hold['start_seconds']
                hold['review_long_hold']=hold['duration_seconds']>max_hold_seconds
                hold['narration']=[u['text'] for u in manifest['alignment']
                    if u['sample_end']/sr>hold['start_seconds'] and u['sample_start']/sr<hold['end_seconds']]
            for cue in timeline['subtitles']:
                seconds=(cue['sample_end']-cue['sample_start'])/sr
                cps=len(cue['text'])/seconds
                if cps>max_subtitle_cps:
                    value['subtitle_flags'].append(dict(unit_id=cue['unit_id'],text=cue['text'],
                        start_seconds=cue['sample_start']/sr,end_seconds=cue['sample_end']/sr,
                        characters_per_second=round(cps,2),advisory_only=True))
        except (OSError,ValueError,KeyError,TypeError,ZeroDivisionError) as exc:errors.append('timeline: '+str(exc))
    timings=state.get('last_result',{}).get('stage_timings',[])
    elapsed=sum(row.get('elapsed_seconds',0) for row in timings)
    seconds=samples/sr if sr and samples else None
    metrics=dict(scope='latest_invocation_not_lifetime',stage_timings=timings,
        output_seconds=seconds,elapsed_seconds=elapsed,
        compute_seconds_per_output_minute=elapsed/(seconds/60) if seconds else None,
        human_review_minutes=None,approved_cost_per_minute=None)
    if state.get('budget_path'):
        try:
            ledger=read(state['budget_path']);execution=state.get('execution') or {}
            rows=[a for a in ledger['attempts'] if a.get('execution',{}).get('task_id')==execution.get('task_id') and a.get('execution')]
            metrics.update(attributed_provider_attempts=len(rows),attributed_tts_characters=sum(a.get('characters',0) for a in rows),
                unattributed_legacy_attempts=sum(1 for a in ledger['attempts'] if not a.get('execution')),
                shared_budget_limits=ledger['limits'],shared_budget_used=ledger['used'])
        except (OSError,ValueError,KeyError,TypeError) as exc:errors.append('budget: '+str(exc))
    value['metrics']=metrics
    from app.services.production_metrics import accumulated
    value['metrics']['accumulated']=accumulated(folder/'production/v2')
    value['interpretation']='Technical flags and reference comparison only; no artistic or identity approval.'
    return value


def compare(before,after):
    same=bool(before.get('inputs') and before.get('inputs')==after.get('inputs')
              and not before.get('errors') and not after.get('errors'))
    return dict(comparable_inputs=same,
        impact=impact(before.get('inputs',{}),after.get('inputs',{})),
        before_metrics=before.get('metrics'),after_metrics=after.get('metrics'),
        quality_improvement_proven=False)


def review_points(value):
    """One ordered navigation list, not a new review gate."""
    points=[]
    attraction = {p['start_seconds']: p for p in value.get('attraction_points', [])}
    for shot in value.get('shots',[]):
        hint = attraction.pop(shot['start_seconds'], None)
        if hint:
            points.append(dict(hint, image_path=shot['image_path'],
                               text=hint['text']+' '+' '.join(shot['narration'])))
            continue
        points.append(dict(start_seconds=shot['start_seconds'],label=(
            '连续同图 '+str(round(shot['duration_seconds'],1))+' 秒' if shot['review_long_hold'] else '画面切换'),
            text=' '.join(shot['narration']),image_path=shot['image_path']))
    points.extend(attraction.values())
    for row in value.get('listening',[]):
        if row.get('kinds'):
            points.append(dict(start_seconds=row['start_seconds'],label='语音疑点 '+', '.join(row['kinds']),
                               text=row['text'],audio_path=row['audio_path']))
    for row in value.get('subtitle_flags',[]):
        points.append(dict(start_seconds=row['start_seconds'],label='字幕阅读密度提醒',text=row['text']))
    return sorted(points,key=lambda p:p['start_seconds'])


def write_report(value,destination):
    """A local review page with no external scripts, URLs or write controls."""
    from app.services.provenance import atomic_json
    destination=Path(destination).resolve()
    if any((destination/name).exists() for name in ('index.html','diagnostics.json')):
        raise ValueError('choose a new report destination; existing reviews are not overwritten')
    destination.mkdir(parents=True,exist_ok=True)
    atomic_json(destination/'diagnostics.json',value)
    esc=lambda x:html.escape(str(x),quote=True)
    rows=[]
    for s in value['shots']:
        uri=Path(s['image_path']).resolve().as_uri()
        refs=''.join('<img loading="lazy" alt="原始身份/场景参考，未自动批准" src="'+esc(Path(r['path']).resolve().as_uri())+'">' for r in s.get('references',[]))
        rows.append('<article><img loading="lazy" src="'+esc(uri)+'"><p>'+esc(f"{s['start_seconds']:.2f}–{s['end_seconds']:.2f}s")+
                    '</p><p>'+esc(' '.join(s['narration']))+'</p>'+refs+'</article>')
    body='''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; style-src 'unsafe-inline'">
<title>HV Studio 只读复核</title><style>body{font-family:system-ui;max-width:1100px;margin:40px auto;padding:16px}article{display:inline-block;width:280px;vertical-align:top;margin:12px}img{max-width:260px;max-height:320px}pre{white-space:pre-wrap}</style>
<h1>当前候选只读复核</h1><p>技术检查不等于内容批准；长镜头及字幕速度仅提醒复核。</p>'''
    body+=''.join(rows)+'<h2>诊断与指标</h2><pre>'+esc(json.dumps(value,ensure_ascii=False,indent=2))+'</pre></html>'
    (destination/'index.html').write_text(body,encoding='utf-8')
    return destination/'index.html'
