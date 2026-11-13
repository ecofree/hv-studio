"""Small shared UI helpers; mutations stay in domain services."""
import copy
import streamlit as st
from app.services import studio_workflow as W

def current():
    eid=st.session_state['studio_episode'];return eid,W.load(eid),W.document(eid)


def show_issues(issues):
    groups={}
    for issue in issues:
        label=issue.split('：',1)[0]
        groups.setdefault(label,[]).append(issue)
    for label,rows in groups.items():
        st.info(label+(f'（{len(rows)} 处）' if len(rows)>1 else ''))
    if len(issues)>len(groups):
        with st.expander(f'查看 {len(issues)} 条具体待办'):
            st.dataframe({'待办':issues},hide_index=True,height=250)

def persist(eid,value):
    try:
        W.save(eid,value,expected_revision=value['revision']);st.success('已保存；备注修改保留审核，实际内容变化需要复核。');st.rerun()
    except (ValueError,OSError,KeyError,TypeError) as exc:st.error(str(exc))

def review_form(eid,kind):
    try: identity=W.identity(eid) if kind=='editorial' else W.pilot_identity(eid)
    except (ValueError,OSError) as exc:st.info(str(exc));return
    labels={'story':'内容清楚，开场问题得到回应','voice_identity':'声线前后统一','pronunciation':'读音准确','emotion':'情绪符合内容','loudness':'音量自然、没有突增','visual':'画风、人物、字幕和动效符合要求'}
    with st.form('review-'+kind+'-'+identity):
        who=st.text_input('审核人');finding=st.text_area('实际发现与结论')
        if kind=='editorial':checks={'facts_and_story':st.checkbox('我已核对核心事件有可靠依据，人物、时序和结果准确，争议和非原文转述没有冒充定论或原话')}
        else:checks={k:st.checkbox(label) for k,label in labels.items()}
        decision=st.radio('审核结论',['仍需修改','确认当前版本'],index=None)
        if st.form_submit_button('记录审核结论'):
            try:
                if decision is None:raise ValueError('请明确选择结论')
                W.record_review(eid,kind,who,finding,'approved' if decision=='确认当前版本' else 'rejected',expected_identity=identity,checks=checks)
                st.success('审核已绑定当前材料。');st.rerun()
            except (ValueError,OSError) as exc:st.error(str(exc))
