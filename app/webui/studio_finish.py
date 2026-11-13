"""One place for explicit human dispositions and final local delivery."""
from pathlib import Path
import streamlit as st
from app import paths
from app.services.provenance import file_sha256
from app.services.narration_pipeline import read


def render_finish(eid, current):
    if not current or not current.get('candidate_path'): return
    state=read(paths.episode_dir(eid)/'production/v2/run.json')
    receipt_path=state['receipt_path']
    if not Path(receipt_path).is_file():
        st.warning('成片审核记录缺失，暂不能确认导出。');return
    receipt_hash=file_sha256(receipt_path)
    st.subheader('完整听看与导出')
    st.caption('先在上方观看当前成片，再处理具体问题。确认只登记本地成片，不上传平台。')
    render_findings(eid,current)
    manifest_path=state.get('manifest_path')
    if manifest_path:
        manifest=read(manifest_path)
        if manifest.get('provenance')=='external_recording':
            with st.expander('本次完整听审的分幕提示'):
                for cue in read(manifest['plan_path']).get('delivery_cues',[]):
                    st.write(cue['raw_text'])
    with st.form('finish-review-'+receipt_hash):
        who=st.text_input('确认人');finding=st.text_area('完整观看后的发现与结论')
        heard=st.checkbox('已听完整旁白，读音、声线与音量可接受')
        seen=st.checkbox('已看完整成片，关键史实、画面和字幕可接受')
        if st.form_submit_button('确认当前成片供导出'):
            try:
                if not heard or not seen:raise ValueError('请完成完整听看后分别确认')
                from app.services.studio_completion import confirm
                confirm(eid,receipt_hash,who,finding)
                st.success('已确认当前成片。');st.rerun()
            except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    if current.get('content_approved'):
        st.success('当前已确认的视频文件可用于手工发布。')
        if st.button('打开成片所在文件夹'):
            import os
            os.startfile(str(Path(current['candidate_path']).resolve().parent))


def render_findings(eid, current):
    review_key=eid
    quality=current.get('quality_report_path')
    if quality:
        from app.services.production_quality import review_queue, record_dispositions
        report_hash = file_sha256(quality)
        all_units=st.checkbox('从全部旁白段定位试听',key='all-listening-'+eid)
        queue = review_queue(quality, all_units=True) if all_units else review_queue(quality)
        if queue:
            st.info(f'全部旁白共 {len(queue)} 段，可定位并登记听到的问题。' if all_units else f'{len(queue)} 段语音识别疑点待听审，不代表这些位置已经确认读错或字幕错位。')
            kinds = sorted({kind for row in queue for kind in row['kinds']})
            labels = {'meaning_change':'可能改变原意的单字','pronunciation':'读音差异','number':'数字差异','omission':'疑似漏读',
                      'insertion':'疑似多读','unit':'单位差异','content_mismatch':'内容差异'}
            chosen_kind = st.selectbox('筛选疑点', ['全部']+kinds,
                                       format_func=lambda x: labels.get(x,x), key='review-kind-'+eid)
            shown = [row for row in queue if chosen_kind=='全部' or chosen_kind in row['kinds']]
            by_id = {row['unit_id']:row for row in shown}
            def label(key):
                row=by_id[key]; seconds=int(row['start_seconds'])
                return f"{seconds//60:02d}:{seconds%60:02d} · {row['text'][:45]}"
            listening = st.selectbox('定位试听', list(by_id), format_func=label, key='listen-unit-'+eid)
            row=by_id[listening]
            st.write(row['text']);st.audio(row['audio_path'])
            st.caption('识别文本：'+row['transcript'])
            if current.get('candidate_path'):
                if st.button('从该位置查看成片',key='seek-'+eid):
                    st.session_state['video-start-'+eid]=int(row['start_seconds']);st.rerun()
            with st.form('finish-quality-'+report_hash):
                selected = st.multiselect('已实际听过且结论相同的片段', list(by_id), format_func=label)
                who=st.text_input('核对人');finding=st.text_input('实际听到的情况')
                choice=st.selectbox('处理',['请选择','音频正确，识别有误','确有口误'])
                listened=st.checkbox('我已听过所选片段，并确认上述结论适用于每一段')
                if st.form_submit_button('保存所选片段的核对结论'):
                    try:
                        if not selected or not listened or choice=='请选择':
                            raise ValueError('请听审、明确选择片段并确认结论')
                        record_dispositions(quality, selected, who, finding,
                            'asr_false_positive' if choice=='音频正确，识别有误' else 'spoken_error',
                            expected_report_sha256=report_hash)
                        st.rerun()
                    except (ValueError,OSError) as exc:st.error(str(exc))
    for row in current.get('delivery_pending',[]):
        with st.expander('表演或画面提示：'+row['text']):
            with st.form('finish-direction-'+row['cue_id']+'-'+review_key):
                who=st.text_input('核对人');finding=st.text_input('实际效果')
                choice=st.selectbox('处理',['请选择','接受当前效果','仍需调整'])
                if st.form_submit_button('记录效果'):
                    try:
                        if choice=='请选择':raise ValueError('请选择实际结论')
                        from app.services.delivery import record_review
                        record_review(current['manifest_path'],row['cue_id'],who,finding,'accepted_existing' if choice=='接受当前效果' else 'needs_revision');st.rerun()
                    except (ValueError,OSError) as exc:st.error(str(exc))
