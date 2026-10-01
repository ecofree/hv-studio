"""
Streamlit 生产进度与内容核查控制台。

⚠️ 定位：**只读监控 + 小工具**，不是出片驱动。真实生产链走
`python app/cli.py episode produce <ep>`（orchestrator 全链）或 API /produce。
本页展示同一 v2 状态，并保存用户明确提交的逐项核查结论。

启动：python -m app.cli webui
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from app.config.config import get_settings
from app.managers import get_relay
from app.services.polyphone import preprocess

st.set_page_config(page_title="hv 控制台", page_icon="🎬", layout="wide")

st.title("hv — 历史解说短视频流水线")
st.caption("查看当前生产进度、候选成片和待处理问题")

from app import paths
from app.services.operator_flow import advise, explain_blocker, operator_status, split_ids
from app.services.narration_pipeline import read
all_ids = sorted(p.stem for p in paths.episodes_dir().glob('*.json'))
real_ids, smoke_ids = split_ids(all_ids)
show_smoke = st.checkbox(f'显示 {len(smoke_ids)} 个冒烟残留', value=False) if smoke_ids else False
episode_ids = (real_ids + smoke_ids) if show_smoke else (real_ids or smoke_ids)
if episode_ids:
    selected = st.selectbox('选择剧集', episode_ids)
    current, script_ready = operator_status(selected)
    advice = advise(selected, current, script_exists=script_ready)
    st.subheader(advice['label'])
    st.info(advice['headline'])
    if advice.get('command'):
        st.caption('下一步')
        st.code(advice['command'], language='text')
    for note in advice.get('notes') or []:
        st.write(note)
    if current.get('blockers'):
        with st.expander('当前阻断与待处理事项', expanded=True):
            for blocker in current['blockers']:
                text = explain_blocker(blocker)
                st.write('• ' + text)
                if text != blocker:
                    st.caption(blocker)
    if current.get('stages_done'): st.caption('已完成：'+' → '.join(current['stages_done']))
    if current.get('candidate_path'): st.video(current['candidate_path'])
    if current.get('quality_report_path'):
        report = read(current['quality_report_path'])
        for row in report['takes']:
            if row['unit_id'] in current['quality_pending_units']:
                with st.expander('语音待核查：'+row['source_text'][:25]):
                    st.write('原文：'+row['source_text']); st.write('识别文字：'+row['quality'].get('transcript',''))
                    st.audio(row['audio_path'])
                    with st.form('quality-'+row['unit_id']):
                        who=st.text_input('审核人')
                        finding=st.text_area('试听发现')
                        choice=st.radio('处置结论',['音频正确，识别有误','确有口误，需要重做'],index=None)
                        if st.form_submit_button('保存本段核查结论'):
                            if not who.strip() or not finding.strip() or choice is None: st.error('请填写审核人、发现和结论。')
                            else:
                                from app.services.production_quality import record_disposition
                                record_disposition(current['quality_report_path'],row['unit_id'],who,finding,
                                    'asr_false_positive' if choice=='音频正确，识别有误' else 'spoken_error')
                                st.success('已保存；刷新后更新待处理列表。')
    if current.get('delivery_pending'):
        st.warning(f"还有 {len(current['delivery_pending'])} 项导演提示待核查。")
        for row in current['delivery_pending']:
            with st.expander(row['text']):
                manifest=read(current['manifest_path'])
                take=next((t for t in manifest['takes'] if t['unit_id']==row.get('unit_id')),None)
                if take: st.audio(take['audio_path'])
                with st.form('direction-'+row['cue_id']):
                    who=st.text_input('审核人'); finding=st.text_area('实际效果与处理说明')
                    choice=st.radio('导演提示处置',['接受当前效果','仍需要调整'],index=None)
                    if st.form_submit_button('保存本项处置'):
                        if not who.strip() or not finding.strip() or choice is None: st.error('请填写完整的核查结论。')
                        else:
                            from app.services.delivery import record_review
                            record_review(current['manifest_path'],row['cue_id'],who,finding,
                                          'accepted_existing' if choice=='接受当前效果' else 'needs_revision')
                            st.success('已保存；刷新后更新待处理列表。')
    if current.get('budget'):
        st.caption('累计实际调用：'+str(current['budget']['used']))
    if st.button('刷新进度'): st.rerun()

    with st.expander('无人生产输入审核'):
        from app.services.production_admission import check as admission_check, record_approval
        admission=admission_check(selected)
        if admission['eligible']: st.success('当前输入已具备进入无人生产队列的条件。成片仍需审核。')
        else:
            for blocker in admission['blockers']: st.write('• '+blocker)
        st.caption('仅在已核对原稿史实、分镜和图片提示词后提交；此操作不批准音频或成片。')
        from app.services.production_admission import review_materials
        material_key='input-material-'+selected
        if st.button('重新载入当前审核材料',key='reload-input-'+selected):
            st.session_state.pop(material_key,None)
        try:
            if material_key not in st.session_state:
                st.session_state[material_key]=review_materials(selected)
            material=st.session_state[material_key]
        except (ValueError,RuntimeError,OSError,KeyError,TypeError) as exc:
            st.error('无法准备当前审核材料：'+str(exc));material=None
        directions=material['directions'] if material else []
        stale=not material or material['input_fingerprint']!=admission.get('input_fingerprint')
        if stale: st.warning('材料缺失或已变化，请重新载入并核查后再提交。')
        if material:
            st.caption('本次审核版本：'+material['input_fingerprint'][:16])
            st.text_area('完整原稿',value=material['source'],height=300,disabled=True,
                         key=material_key+'-'+material['input_fingerprint'])
            with st.expander('显式分镜与图片提示词'):
                st.json(material['storyboard']);st.json(material['prompts'])
            previews=material['previews']
            if previews and st.checkbox('展示已有图片供核对',key='show-input-images-'+selected):
                page=int(st.number_input('图片页码（每页 12 张）',min_value=1,max_value=max(1,(len(previews)+11)//12),
                                         value=1,key='input-image-page-'+selected))
                cols=st.columns(4)
                for i,preview in enumerate(previews[(page-1)*12:page*12]):
                    cols[i%4].image(preview['path'],caption=preview['id'],width=180)
        for cue in directions: st.write('待执行/核查：'+cue['text'])
        with st.form('input-review-'+selected+'-'+(material['input_fingerprint'] if material else 'missing')):
            reviewer=st.text_input('输入审核人')
            finding=st.text_area('原稿与分镜核查结论')
            confirmed=st.checkbox('我已核对当前原稿、分镜与图片提示词')
            deferred=st.checkbox('允许先按现有控制生成候选；未支持的表演和画面要求在成片后逐项审核，不视为已实现',value=False) if directions else False
            if st.form_submit_button('保存输入审核',disabled=stale):
                if not confirmed or not reviewer.strip() or not finding.strip(): st.error('请完成核查并填写结论。')
                else:
                    try:
                        record_approval(selected,reviewer,finding,'candidate_review_required' if deferred else None,
                                        expected_fingerprint=material['input_fingerprint'])
                        st.success('已保存输入审核。')
                    except (ValueError,OSError) as exc: st.error(str(exc))

st.subheader('无人生产队列')
from app.services.batch_worker import queue_dir, alive
import time
worker_file=queue_dir()/'worker.json'
worker_state=read(worker_file) if worker_file.exists() else {}
if worker_state and alive(worker_state.get('pid'),worker_state.get('created_at')) and time.time()-worker_state.get('heartbeat',0)<120:
    st.caption('生产服务在线；已收到近期心跳。')
else: st.warning('生产服务未运行或心跳过期。')
queue_labels={'QUEUED':'排队中','RUNNING':'生产中','NEEDS_REVIEW':'等待审核','BLOCKED_INPUT':'输入需修复',
              'BUDGET_EXHAUSTED':'预算已用尽','RETRY_WAIT':'等待有限重试','FAILED':'重试已停止','APPROVED':'审核通过'}
rows=[]
for queue_path in sorted(queue_dir().glob('*.json')):
    if queue_path.name=='worker.json': continue
    try:
        queued=read(queue_path)
        for eid,item in queued.get('episodes',{}).items():
            rows.append({'批次':queued['spec']['batch_id'],'剧集':eid,'状态':queue_labels.get(item['status'],item['status']),
                         '已尝试':item.get('attempts',0),'待处理':'; '.join(item.get('blockers',[])) or item.get('error','')})
    except (ValueError,OSError,KeyError,TypeError,AttributeError): st.error('无法读取队列记录：'+queue_path.name)
for error_path in sorted((queue_dir()/'errors').glob('*.json')):
    try:
        issue=read(error_path)
        if not issue.get('resolved_at'):
            st.error('已隔离队列异常：'+Path(issue['path']).name+' / '+str(issue.get('episode') or '批次')+'：'+issue.get('message',issue['error']))
    except (ValueError,OSError,KeyError,TypeError,AttributeError): st.error('无法读取队列异常记录：'+error_path.name)
if rows: st.dataframe(rows,hide_index=True)
else: st.caption('暂无生产任务。')

with st.expander('队列暂停与恢复'):
    from app.services.batch_worker import set_paused
    is_paused=(queue_dir()/'PAUSE').exists()
    st.caption('已请求暂停；当前剧集完成后停止取新任务，服务保持在线。' if is_paused else '队列允许处理已获准的任务。')
    with st.form('queue-pause-control'):
        operator=st.text_input('操作人')
        reason=st.text_input('暂停或恢复原因')
        if st.form_submit_button('恢复队列' if is_paused else '完成当前剧集后暂停'):
            try:
                set_paused(not is_paused,operator,reason)
                st.success('已恢复队列。' if is_paused else '已请求在剧集之间暂停。')
            except (ValueError,RuntimeError,OSError) as exc: st.error(str(exc))

retryable=[r for r in rows if r['状态'] in ('输入需修复','预算已用尽','重试已停止')]
if retryable:
    with st.expander('修复后受控续跑'):
        st.caption('保留累计尝试和原预算。增加运行尝试次数不会增加生图或配音额度。')
        with st.form('queue-retry-control'):
            selection=st.selectbox('停止的任务',range(len(retryable)),
                format_func=lambda i:retryable[i]['批次']+' / '+retryable[i]['剧集'])
            operator=st.text_input('续跑操作人'); reason=st.text_area('已完成的修复与续跑原因')
            attempts=st.number_input('额外运行尝试次数',min_value=1,max_value=3,value=1,step=1)
            if st.form_submit_button('使用原预算续跑'):
                try:
                    from app.services.batch_worker import retry_episode
                    row=retryable[selection]
                    retry_episode(row['批次'],row['剧集'],operator,reason,int(attempts))
                    st.success('已重新排队，准入和原预算限制仍然生效。')
                except (ValueError,RuntimeError,OSError,KeyError) as exc: st.error(str(exc))

s = get_settings()
r = get_relay()

with st.expander("生图渠道、图像参数和多音字"):
    snap = r.snapshot()
    if not snap:
        st.warning("无可生图渠道（.env 未配或未同步）")
    else:
        cols = st.columns(len(snap))
        for col, (name, h) in zip(cols, snap.items()):
            ok_rate = h.get("ok_rate", 1.0)
            err_rate = 1 - ok_rate
            with col:
                st.metric(
                    name,
                    f"{h['score']:.0f}",
                    delta=f"err {err_rate*100:.0f}%" if h.get("n_history", 0) else "无历史",
                    delta_color="inverse",
                )
                st.caption(f"ok {ok_rate*100:.0f}% · {h['n_history']} 次 · "
                           f"quota ${h['last_quota_usd'] or '—'}")
    c1, c2, c3 = st.columns(3)
    c1.metric("请求 size", s.image_size)
    c2.metric("预测实际", f"{s.predict_actual_image_dims()[0]}×{s.predict_actual_image_dims()[1]}")
    c3.metric("超分后", f"{s.predict_upscaled_dims()[0]}×{s.predict_upscaled_dims()[1]}")
    text = st.text_area("多音字原文", value="张骞被匈奴扣押十年，单于冒顿派阏氏看守")
    if text:
        st.code(preprocess(text), language="text")

st.subheader("生产入口")
st.info(
    "这一页负责查看进度、试听和保存你提交的核查结论。出片命令：\n"
    "- `.\\cli.bat status`：看每集进度；`.\\cli.bat status <id>`：看这一集的下一步\n"
    "- `.\\cli.bat episode produce <id> --stage script`：只做旁白计划，不调用付费接口\n"
    "- `.\\cli.bat episode produce <id>`：额度为 0 时只复用缓存，需要新调用会在花钱前停住\n"
    "- 孤儿音频：`.\\cli.bat episode audit-wav --all`，确认后再加 `--apply`"
)
