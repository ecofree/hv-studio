from datetime import datetime,timezone
import streamlit as st
from app.services import studio_workflow as W
from app.webui.studio_components import current
eid,v,doc=current()
st.subheader('用真实反馈检验创作判断')
st.caption('比较相近题材、时长、观测窗口和流量来源。没有的指标留空，不把单条作品的表现当作规律。')
from app.services.production_status import get_status
from app.services.provenance import file_sha256
current_video=get_status(eid).get('candidate_path')
duration=float(v['target_seconds'])
if current_video:
    from app import paths
    run=W.read(paths.episode_dir(eid)/'production/v2/run.json')
    if run.get('receipt_path'):
        receipt=W.read(run['receipt_path'])
        if receipt.get('candidate_path')==current_video and receipt.get('expected_frames'):
            duration=receipt['expected_frames']/25
with st.form('observation-'+eid):
    row={}
    for k,label in [('publication_id','平台作品标识'),('topic','题材'),('hypothesis','本次只想验证的主要变化'),('traffic_source','流量来源或统计口径')]:row[k]=st.text_input(label)
    row['version_sha256']=file_sha256(current_video) if current_video else ''
    version_confirmed=st.checkbox('本次数据对应当前显示的成片版本')
    row['observed_at']=st.text_input('观测时间',value=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    row['window_hours']=st.number_input('发布后经过小时数',min_value=0.1,value=24.0)
    row['duration_seconds']=st.number_input('作品时长（秒）',min_value=0.1,value=duration)
    metrics={}
    for k,label in [('plays','播放数'),('avg_watch_seconds','平均观看秒数'),('likes','点赞'),('comments','评论'),('shares','分享'),('saves','收藏'),('follows','新增关注'),('retention_3s','3 秒留存百分比（若可取得）'),('retention_5s','5 秒留存百分比（若可取得）')]:metrics[k]=st.text_input(label)
    row['notes']=st.text_area('评论中的理解、疑问与下一次要验证的解释')
    if st.form_submit_button('保存本次真实观测'):
        try:
            if not current_video or not version_confirmed: raise ValueError('请先选择有效成片并确认本次数据对应此版本')
            row.update({k:float(x) if x.strip() else None for k,x in metrics.items()})
            W.add_observation(eid,row);st.success('已保存，缺失指标保持为空。');st.rerun()
        except (ValueError,OSError) as exc:st.error(str(exc))
rows=W.observations(eid)
if rows:st.dataframe(rows,hide_index=True)
