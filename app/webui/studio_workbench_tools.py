"""Focused local recovery, review navigation and explicit cache maintenance."""
from pathlib import Path
import streamlit as st
from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import file_sha256


def recovery(ep_id):
    report_path=paths.episode_dir(ep_id)/'production/v2/assemblies/alignment-recovery.json'
    if not report_path.is_file():return
    import json, hashlib
    payload=report_path.read_bytes();report=json.loads(payload)
    report_sha=hashlib.sha256(payload).hexdigest()
    from app.services.studio_workflow import document
    if document(ep_id)['source_sha256']!=report['source_sha256']:return
    if not any(r['needs_location'] or r['manual_location'] for r in report['rows']):return
    with st.expander('录音局部定位：先试听，再修正范围'):
        st.caption('自动定位失败不等于缺录。保存范围不修改母带，不批准声音；继续制作时仍检查切片与字幕。')
        rows=report['rows'];labels={r['unit_id']:r for r in rows}
        ordered=sorted(labels,key=lambda k:not labels[k]['needs_location'])
        uid=st.selectbox('定位哪段旁白',ordered,format_func=lambda k:labels[k]['text'][:65],key='locate-unit-'+ep_id)
        row=labels[uid];index=rows.index(row)
        st.write(row['text'])
        if index:st.caption('前句：'+rows[index-1]['text'])
        if index+1<len(rows):st.caption('后句：'+rows[index+1]['text'])
        lo=max(0.,row['window_start']);hi=min(report['duration_seconds'],row['window_end'])
        st.caption(f'邻近范围 {lo:.2f}–{hi:.2f} 秒；该范围仅供试听定位。')
        if hi>lo:st.audio(report['master_path'],start_time=lo,end_time=hi)
        with st.form('locate-range-'+ep_id+'-'+uid+'-'+report_sha):
            start=st.number_input('实际语音起点（秒）',min_value=0.,value=float(row['start'] if row['start'] is not None else lo),format='%.3f')
            end=st.number_input('实际语音终点（秒）',min_value=0.,value=float(row['end'] if row['end'] is not None else max(lo,hi)),format='%.3f')
            if st.form_submit_button('保存局部定位，不制作'):
                try:
                    from app.services.production_storage import episode_execution
                    from app.services.studio_jobs import require_no_active
                    from app.services.recording_alignment_recovery import save_range
                    with episode_execution(ep_id):
                        require_no_active(ep_id)
                        save_range(report_path,uid,start,end,expected_sha256=report_sha)
                    st.success('定位已保存。下一次本地旁白任务会复用原录音和识别缓存。')
                except (OSError,ValueError,RuntimeError) as exc:st.error(str(exc))
        if st.button('按已保存定位继续本地旁白检查',key='continue-location-'+ep_id):
            try:
                from app.services.studio_jobs import launch
                launch(ep_id,mode='voice',external_master=report['master_path'])
                st.rerun()
            except (OSError,ValueError,RuntimeError) as exc:st.error(str(exc))


def cleanup(ep_id):
    with st.expander('空间管理：保留成片和原始素材'):
        from app.services import v2_cleanup
        segments=st.checkbox('同时清理已完成版本的分段视频（再次修改时可能需要重编）',key='clean-segments-'+ep_id)
        key='v2-clean-preview-'+ep_id
        if st.button('生成清理清单',key='clean-plan-'+ep_id):
            try:st.session_state[key]=v2_cleanup.plan(ep_id,include_segments=segments)
            except (OSError,ValueError,RuntimeError) as exc:st.error(str(exc))
        preview=st.session_state.get(key)
        if preview and preview['include_segments']==segments:
            st.write(f"候选中间文件 {len(preview['items'])} 个，合计 {preview['total_bytes']/1024**3:.2f} GiB。")
            st.caption('只处理已完成渲染的中间文件，执行前重新核对任务锁、清单和成片内容。')
            st.dataframe([dict(文件=r['path'],大小MiB=round(r['size']/1024**2,2)) for r in preview['items']])
            for row in preview['skipped']:st.caption('保留：'+row['path']+'；'+row['reason'])
            if st.button('删除清单中的中间文件',disabled=not preview['items'],key='clean-apply-'+ep_id):
                try:
                    result=v2_cleanup.apply(ep_id,preview)
                    del st.session_state[key]
                    st.success(f"已清理 {result['deleted_bytes']/1024**3:.2f} GiB。成片和原始素材保留。")
                except (OSError,ValueError,RuntimeError) as exc:st.error(str(exc))


def review(ep_id, candidate_path=None):
    from app.services.production_metrics import accumulated
    with st.expander('累计制作耗时'):
        metrics=accumulated(paths.episode_dir(ep_id)/'production/v2')
        st.caption('累计记录本功能启用后的实际任务，包含失败和重做；不补造历史耗时、人工时间或费用。')
        st.write(f"已记录 {metrics['invocations']} 次，其中未完成 {metrics['failed_invocations']} 次；累计 {metrics['wall_seconds']/60:.1f} 分钟。")
        st.dataframe([dict(工序=k,次数=v['invocations'],分钟=round(v['seconds']/60,2),缓存与调用计数=v['counters']) for k,v in metrics['stages'].items()])
        for error in metrics['errors']:st.caption(error)
    with st.expander('看片定位与画面节奏'):
        from app.services.production_diagnostics import report, review_points
        key='review-diagnostics-'+ep_id
        st.caption('长时间同图和字幕密度只提醒看片，不要求补图，也不改变内容确认。修改制作输入后请重新载入。')
        if st.button('载入或刷新看片清单',key='review-refresh-'+ep_id):
            try:st.session_state[key]=report(ep_id)
            except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:st.error(str(exc))
        diagnostic=st.session_state.get(key)
        if not diagnostic:return
        for error in diagnostic.get('errors',[]):st.caption(error)
        points=review_points(diagnostic)
        if points:
            index=st.selectbox('看片位置',range(len(points)),format_func=lambda i:f"{points[i]['start_seconds']:.1f}s · {points[i]['label']}",key='review-point-'+ep_id)
            point=points[index];st.write(point['text'])
            if point.get('image_path'):st.image(point['image_path'],width=260)
            if point.get('audio_path'):st.audio(point['audio_path'])
            if candidate_path and st.button('跳到成片对应位置',key='review-seek-'+ep_id):
                st.session_state['video-start-'+ep_id]=int(point['start_seconds']);st.rerun()
        import json
        st.download_button('保存诊断 JSON',json.dumps(diagnostic,ensure_ascii=False,indent=2),
                           file_name=ep_id+'-diagnostics.json',mime='application/json')
