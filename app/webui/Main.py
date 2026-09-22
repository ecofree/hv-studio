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
from app.services.production_status import get_status
from app.services.narration_pipeline import read
episode_ids = sorted(p.stem for p in paths.episodes_dir().glob('*.json'))
if episode_ids:
    selected = st.selectbox('选择剧集', episode_ids)
    current = get_status(selected)
    labels = {'NOT_STARTED':'尚未开始', 'RUNNING':'生产中', 'INTERRUPTED':'生产中断，可续跑',
              'BLOCKED':'需要处理问题', 'STALE':'输入已变化，需要更新候选', 'CANDIDATE':'候选片待审核',
              'STAGES_COMPLETE':'本次阶段已完成'}
    st.subheader(labels.get(current['status'], current['status']))
    if current.get('blockers'):
        with st.expander('当前阻断与待处理事项', expanded=True):
            for blocker in current['blockers']: st.write('• '+blocker)
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
        from app.services.production_admission import inputs as admission_inputs
        try: directions=admission_inputs(selected).get('direction_cues',[])
        except (ValueError,RuntimeError,OSError,KeyError,TypeError): directions=[]
        for cue in directions: st.write('待执行/核查：'+cue['text'])
        with st.form('input-review-'+selected):
            reviewer=st.text_input('输入审核人')
            finding=st.text_area('原稿与分镜核查结论')
            confirmed=st.checkbox('我已核对当前原稿、分镜与图片提示词')
            deferred=st.checkbox('允许先按现有控制生成候选；未支持的表演和画面要求在成片后逐项审核，不视为已实现',value=False) if directions else False
            if st.form_submit_button('保存输入审核'):
                if not confirmed or not reviewer.strip() or not finding.strip(): st.error('请完成核查并填写结论。')
                else:
                    try: record_approval(selected,reviewer,finding,'candidate_review_required' if deferred else None); st.success('已保存输入审核。')
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

s = get_settings()
r = get_relay()

# ---- 渠道健康（ok_rate 真实成功率；err_rate = 1 - ok_rate） ----
st.subheader("subrouter 渠道池")
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
                delta_color="inverse",  # err 越低越好
            )
            st.caption(f"ok {ok_rate*100:.0f}% · {h['n_history']} 次 · "
                       f"quota ${h['last_quota_usd'] or '—'}")

# ---- 关键参数 ----
st.subheader("图像配置")
c1, c2, c3 = st.columns(3)
c1.metric("请求 size", s.image_size)
c2.metric("预测实际", f"{s.predict_actual_image_dims()[0]}×{s.predict_actual_image_dims()[1]}")
c3.metric("超分后", f"{s.predict_upscaled_dims()[0]}×{s.predict_upscaled_dims()[1]}")

# ---- 多音字测试 ----
st.subheader("多音字预处理")
text = st.text_area("原文", value="张骞被匈奴扣押十年，单于冒顿派阏氏看守")
if text:
    out = preprocess(text)
    st.code(out, language="text")

# ---- 使用指引 ----
st.subheader("生产入口")
st.info(
    "真实出片不在此页。用：\n"
    "- `python -m app.cli episode produce <ep> --stage script`：准备 v2 旁白计划；"
    "生成需显式设置预算，时间轴确认后合成候选。旧片重建需 `--pipeline legacy-preview`。"
    "长片中断后重跑即断点续跑；\n"
    "- `... episode produce <ep> --stage verify`：只读校验产物真值；\n"
    "- `... episode --all audit-wav --apply`：清孤儿音频；\n"
    "- API：`POST /v1/episode/{id}/produce`。"
)
