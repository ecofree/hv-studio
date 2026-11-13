from pathlib import Path
import streamlit as st
from app import paths
from app.services import studio_workflow as W,studio_jobs as J
from app.services import studio_narration_handoff as H
from app.webui.studio_components import current
eid,v,doc=current();s=W.status(eid)
st.subheader('把已经选定的方案做成完整作品')
state_path=paths.episode_dir(eid)/'production/v2/run.json'
full_candidate=None
if state_path.is_file():
    from app.services.narration_pipeline import read
    if read(state_path).get('receipt_path'):
        from app.services.production_status import get_status
        full_candidate=get_status(eid)
        if full_candidate.get('candidate_path'):
            st.info('整集已登记，可在这里观看。声音方案和短样片的确认继续保留；整集仍需完整听看。')
            st.video(full_candidate['candidate_path'],start_time=st.session_state.get('video-start-'+eid,0))
            if full_candidate.get('audio_master_path'):
                with st.expander('完整旁白'):
                    st.audio(full_candidate['audio_master_path'])
            st.caption('本次复用现有视频与声音，未重新配音或渲染。' if full_candidate.get('full_episode_registration') else '当前整集候选。')
        else:
            st.warning('已有整集记录需要核对，暂不展示为有效候选。')
            for issue in full_candidate.get('blockers',[]):st.caption(issue)
if s['blockers']:
    from app.webui.studio_components import show_issues
    show_issues(s['blockers'])
else:st.success('当前正文已确认，可按现有授权制作候选；成片仍需完整听看。')
selected=None
external=v['voice'].get('master','')
try:
    selected=H.selected(eid)
    if selected:
        external=selected['master']
        st.info('已选用当前新稿的旁白；整集将复用这条音轨，在最终倍速上对齐字幕。')
        st.caption(selected['master'])
    elif not external:
        from app.services.studio_voice_contract import path_for
        if not path_for(eid).exists(): external=J.imported_master(eid) or ''
except (ValueError,OSError,KeyError) as exc:st.error('旁白交接待处理：'+str(exc))
if v['scope']=='pilot' and not selected:
    from app.services.studio_voice_contract import path_for
    if not path_for(eid).exists():
        with st.expander('从现有外部母带截取样片旁白'):
            from app.services import studio_audio_excerpt as excerpt
            st.caption('先试听母带确定这段正文的起止时间。另存无损片段并保留母带关系，不调用配音服务；字幕对齐和最终试听仍需检查。')
            try:
                source=excerpt.selected_source(eid)
                st.audio(str(source));st.caption(str(source))
                with st.form('excerpt-'+eid):
                    first=st.number_input('起点（秒）',min_value=0.0,value=0.0)
                    last=st.number_input('终点（秒）',min_value=0.0,value=0.0)
                    if st.form_submit_button('另存并选用这一段'):
                        excerpt.create(eid,first,last,expected_identity=W.identity(eid));st.rerun()
            except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
if state_path.exists():
    state=W.read(state_path)
    assets=state.get('image_assets',{})
    if assets:
        with st.expander('复用已有底图，不补画相同场景'):
            from app.services.studio_image_reuse import assign
            with st.form('reuse-images-'+eid):
                source_key=st.selectbox('使用哪张已有底图',list(assets))
                targets=st.multiselect('用在这些镜头',state.get('declared_image_keys',list(assets)))
                if st.form_submit_button('保存镜头用图安排'):
                    try:assign(eid,targets,source_key,expected_identity=W.identity(eid));st.rerun()
                    except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
            st.caption('只调整用图安排，不改原图，不生成图片；最终看片确认构图。')
    if state.get('manifest_path'):
        if not selected and st.checkbox('局部补录与试听',key='pickup-'+eid):
            from app.services import recording_patch
            manifest_path=state['manifest_path']
            manifest=W.read(manifest_path)
            if manifest.get('provenance')=='external_recording':
                units={u['unit_id']:u for u in manifest['alignment']}
                uid=st.selectbox('需要修音的旁白段',list(units),format_func=lambda k:units[k]['text'][:65])
                unit=units[uid]
                sr=manifest['sample_rate']
                st.write(unit['text'])
                st.audio(next(t['audio_path'] for t in manifest['takes'] if t['unit_id']==uid))
                with st.form('pickup-range-'+uid):
                    start=st.number_input('替换起点（全片秒）',value=unit['sample_start']/sr,format='%.5f')
                    end=st.number_input('替换终点（全片秒）',value=unit['sample_end']/sr,format='%.5f')
                    pickup=st.text_input('同声线补录 WAV 路径')
                    if st.form_submit_button('试听补录前后接缝'):
                        try:
                            output=recording_patch.preview(eid,manifest_path,uid,round(start*sr),round(end*sr),pickup,expected_identity=W.identity(eid))
                            st.session_state['pickup-preview-'+eid]=str(output)
                        except (ValueError,RuntimeError,OSError) as exc:st.error(str(exc))
                if output:=st.session_state.get('pickup-preview-'+eid):
                    st.audio(str(Path(output).with_name('preview.wav')))
                    if st.button('选用试听过的补录版本'):
                        try:
                            recording_patch.select(eid,output)
                            del st.session_state['pickup-preview-'+eid]
                            st.rerun()
                        except (ValueError,RuntimeError,OSError) as exc:st.error(str(exc))
        with st.expander('字幕对齐与切片边界'):
            st.caption('范围不对时，先在上方重新选取母带片段并制作旁白候选；范围正确时可只重跑对齐，不配音、不补图、不渲染。')
            if st.button('仅重新检查字幕对齐'):
                try:J.launch(eid,mode='alignment');st.rerun()
                except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
external=st.text_input('已有旁白文件或文件夹',value=external,disabled=bool(selected))
audio_mode=st.selectbox('音频输入',('auto','master','parts'),disabled=bool(selected),
                        format_func={'auto':'有完整母带时优先，否则使用编号分段','master':'只用完整母带','parts':'使用编号分段'}.__getitem__)
from app.services.studio_image_reuse import coverage_report
visual = coverage_report(eid, master=external or None, audio_mode=audio_mode)
if visual['status'] == 'available':
    basis = {'timeline':'成片时间线','aligned':'已对齐的分镜','estimate':'按录音估算'}[visual['spans'][0]['timing']]
    st.write(f"{basis} · 正片 {visual['duration_seconds']/60:.1f} 分钟 · {visual['asset_count']} 张底图 · 同图最长 {visual['longest_seconds']:.1f} 秒")
    if visual['longest_seconds'] >= 60:
        st.warning('同一底图连续超过一分钟。请对照这段旁白中的人物、地点与动作安排换景。')
    if visual.get('pending_design') and basis=='成片时间线':
        next_plan=coverage_report(eid,master=external or None,audio_mode=audio_mode,planned=True)
        if next_plan['status']=='available':
            st.caption(f"当前导演草案与这份成片不同。下次提交预计使用 {next_plan['asset_count']} 张底图，同图最长 {next_plan['longest_seconds']:.1f} 秒。")
        else:st.info(next_plan['reason'])
    with st.expander('整集用图与持续时间'):
        st.dataframe(visual['review_rows'],hide_index=True)
else:
    st.info('画面时长暂不可用：'+visual['reason'])
if visual['missing_local']:st.caption('需要本地提供：'+'、'.join(visual['missing_local']))
if visual['missing_briefs']:
    st.caption('尚缺场景描述：'+'、'.join(visual['missing_briefs']))
if visual['missing_assets']:
    st.caption(f"当前尚缺 {len(visual['missing_assets'])} 份对应画面。")
    try:
        from app.managers import load
        from app.managers.relay import get_relay
        from app.services.image import _available
        from app.services.production_inputs import prepare
        from collections import Counter
        jobs, _ = prepare(load(eid), doc, state_path.parent, preview=True)
        missing = set(visual['missing_assets']) - set(visual['missing_local'])
        counts = Counter(job.req.model for job in jobs if job.id in missing
                         and not getattr(job, 'imported', False) and not getattr(job, 'source_brief_missing', False))
        if counts:
            relay = get_relay()
            routes = [c for c in relay.all() if any(_available(relay, c, m) for m in counts)]
            slots = '、'.join(f'{c.name} × {c.max_concurrency}' for c in routes)
            st.caption(f"本批共享并发位 · {slots or '当前无可用渠道'}")
            st.caption(' · '.join(f'{model} {count} 张' for model, count in sorted(counts.items())))
    except (OSError,ValueError,RuntimeError) as exc:st.caption('渠道信息暂不可用：'+str(exc))
if full_candidate:
    alignment=full_candidate.get('alignment',{})
    if alignment.get('status')=='machine_validated':
        st.caption(f"字幕声学对齐已完成，共 {alignment.get('subtitle_count',0)} 条字幕；语音识别疑点在听审区单独处理。")
    elif alignment.get('status')=='stale_or_invalid':st.warning('字幕对齐记录需要更新：'+alignment.get('error',''))
with st.form('produce-'+eid):
    st.caption('任务在后台运行；默认只复用缓存。生成的新视频仍需现有成片与完整试听审核。')
    images=int(st.number_input('图片调用总次数（含失败）',min_value=0,value=0,step=1))
    requests=int(st.number_input('允许新配音请求次数',min_value=0,value=0,step=1))
    chars=int(st.number_input('允许新配音字符数',min_value=0,value=0,step=100))
    mode=st.selectbox('本次制作内容',['制作视频候选','先制作旁白候选'])
    if st.form_submit_button('开始后台制作',disabled=not s['ready_for_generation']):
        try:
            result=J.launch(eid,max_images=images,max_requests=requests,max_characters=chars,external_master=external.strip() or None,
                            mode='voice' if mode=='先制作旁白候选' else 'video',audio_mode=audio_mode)
            st.success('任务已启动。');st.rerun()
        except (ValueError,OSError,RuntimeError) as exc:st.error(str(exc))
if st.button('刷新制作状态'):st.rerun()
for job in J.jobs(eid):
    result=job['result']
    st.write('任务：'+Path(job['path']).name[:12])
    labels={'QUEUED':'等待启动','RUNNING':'正在处理','INTERRUPTED':'任务中断',
            'BLOCKED':'需要处理运行问题','CANDIDATE':'视频候选已生成，待完整听看',
            'VOICE_CANDIDATE':'旁白候选已生成，待听审','STAGES_COMPLETE':'本次工序已完成'}
    st.write('状态：'+labels.get(job.get('status'),job.get('status','未知')))
    stages={'tts':('导入与切分已有旁白' if job.get('external_master') else '准备旁白'),'quality':'音频质量检查','alignment':'字幕声学对齐',
            'images':'准备画面','timeline':'建立时间轴','compose':'渲染与拼接','verify':'完整性校验','admission':'检查输入'}
    if job.get('stage'):st.caption(('当前工序：' if job.get('running') else '最后工序：')+stages.get(job['stage'],job['stage']))
    if job.get('stage_timings'):
        with st.expander('工序耗时与缓存复用'):
            st.dataframe(job['stage_timings'],hide_index=True)
    progress=job.get('progress') or {}
    if progress.get('total'):
        st.progress(min(1.,progress.get('done',0)/progress['total']),
                    text=f"{progress.get('message','处理进度')}：{progress.get('done',0)} / {progress['total']}")
    elif progress.get('message'):st.caption(progress['message'])
    if job.get('heartbeat_at') and job.get('running'):
        import time
        st.caption(f"最近心跳：{max(0,int(time.time()-job['heartbeat_at']))} 秒前")
    if job.get('exit_reason'):st.warning(job['exit_reason'])
    if job.get('budget'):
        b=job['budget'];st.caption('图片调用已用 '+str(b['used'].get('image_requests',0))+' / 总上限 '+str(b['limits'].get('image_requests',0)+b['limits'].get('image_retries',0)))
    if result:
        if result.get('error'):st.error(result.get('error_phase','运行')+'：'+result['error'])
        if result.get('final_mp4'):st.caption('本次任务的候选文件：'+result['final_mp4'])
        if result.get('audio_path'):
            st.audio(result['audio_path']);st.caption('候选音轨：'+result['audio_path'])
            if result.get('source_map_path'):
                st.caption('选用只登记整集输入，不启动配音或渲染，也不代表已完成整集试听审核。')
                if st.button('选用这条旁白制作整集',key='select-voice-'+job['path']):
                    try:
                        H.select(eid,result['source_map_path'],expected_identity=W.identity(eid));st.rerun()
                    except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
    else:st.caption('等待任务结果；可查看日志确认进展。')
    if job.get('running'):st.caption('任务运行中，关闭或刷新页面不会重复提交。')
    elif job.get('recoverable'):
        limits=job.get('budget_limits',{})
        st.caption(f"可继续原任务：配音上限 {limits.get('max_requests',0)} 次 / {limits.get('max_characters',0)} 字，图片 {limits.get('max_images',0)} 次。已消耗额度保留，结果不明的请求不会自动重发。")
        if st.button('按原预算继续此任务',key='resume-'+job['path']):
            try:J.resume(job['path']);st.rerun()
            except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
    elif job.get('recovery_issue'):st.warning('不能直接恢复：'+job['recovery_issue'])
    with st.expander('运行记录'):st.text(job.get('log','尚无日志'))
from app.webui.studio_finish import render_finish
render_finish(eid,full_candidate)
