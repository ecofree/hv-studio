"""Listen to acoustic outliers in the existing narration review flow."""
from pathlib import Path


def render(audio, source_map, *, key):
    import streamlit as st
    from app.services.qwen_voice import read, local_path
    from app.services.voice_consistency import load_report
    record=read(source_map)
    report_path=record.get('voice_consistency_path')
    if not report_path:
        st.caption('这条历史音轨未做声学一致性比较；原音轨保留。')
        return
    with st.expander('声音一致性与异常片段'):
        try:
            report=load_report(local_path(report_path),audio)
        except (OSError,ValueError,KeyError) as exc:
            st.warning('声音检查暂不可用：'+str(exc));return
        if report['status']=='reference_unavailable':
            st.info('已选声音的参考片段不足或不可用，暂不能比较跨集音色；继续使用现有听看流程。')
        elif report['status']=='no_acoustic_outliers':
            st.caption('未发现明显声学偏离。此结果不等于发音、音色或人工听审已通过。')
        findings=report.get('findings',[])
        if findings:
            labels={'pitch_drift':'音高变化较大','timbre_drift_hint':'共鸣或音色可能偏移',
                    'level_jump':'音量差异较大','clipping':'可能爆音',
                    'insufficient_voiced_audio':'有效人声不足'}
            selected=st.selectbox('定位需要试听的位置',list(range(len(findings))),
                format_func=lambda i:f"{findings[i]['start']:.1f}–{findings[i]['end']:.1f} 秒："+
                    '、'.join(labels.get(x,x) for x in findings[i]['issues']),key='voice-consistency-'+key)
            row=findings[selected]
            st.audio(str(local_path(audio)),start_time=max(0.,row['start']-1),end_time=row['end']+1)
        st.caption('这里只提示疑点；不自动重配音、不增加确认步骤。')
