import streamlit as st
from app.services import studio_workflow as W
from app.webui.studio_components import current,persist,review_form
eid,v,doc=current()
st.subheader('先确定观众为什么要看')
labels={'audience':'给谁看','question':'这一集回答什么问题','promise':'观众看完会明白什么','title':'首帧标题（18 字以内）','opening':'第一句话','opening_visual':'第一眼看到什么','closing':'结尾如何兑现开场'}
with st.expander('创作卡（可选）'):
    with st.form('brief-'+eid):
        for key,label in labels.items():v['brief'][key]=st.text_input(label,value=v['brief'][key])
        v['target_seconds']=int(st.number_input('目标时长（秒）',min_value=1,max_value=120 if v['scope']=='pilot' else 36000,value=v['target_seconds']))
        if st.form_submit_button('保存创作卡'):persist(eid,v)

st.subheader('史实备注（可选，按事件记录即可）')
st.caption('核对关键事件、人物、时序和结果；争议要说明。不要求逐段填写来源，详细备注供需要时使用。')
units={u['unit_id']:u['source_text'] for u in doc['units']}
if v['claims']:
    st.dataframe([{'编号':c['id'],'正文':c['quote'],'性质':c['type'],'来源':c['source'],'争议':c['uncertainty']} for c in v['claims']],hide_index=True)
with st.expander('新增或修改主张',expanded=False):
    selected=st.selectbox('编辑主张',['新增']+[c['id'] for c in v['claims'] if not c.get('context_source')])
    if any(c.get('context_source') for c in v['claims']):st.caption('样片共用史料保留原作品引用；需要修订时在原作品中处理。')
    c=next((dict(c) for c in v['claims'] if c['id']==selected),dict(id=f'claim-{len(v["claims"])+1:03}',unit_id=next(iter(units)),quote='',type='史籍记载',source='',locator='',excerpt='',uncertainty='',visual_limits=''))
    with st.form('claim-'+selected):
        c['id']=st.text_input('主张编号',value=c['id'])
        c['unit_id']=st.selectbox('对应正文段落',list(units),index=list(units).index(c['unit_id']) if c['unit_id'] in units else 0,format_func=lambda k:units[k][:55])
        c['quote']=st.text_area('正文中的原句或片段',value=c['quote'])
        kinds=['史籍记载','史籍指控','现代研究','作者推论','艺术示意','未知']
        c['type']=st.selectbox('主张性质',kinds,index=kinds.index(c['type']))
        for key,label in [('source','史料或研究名称'),('locator','卷页、条目、版本或链接'),('excerpt','支持该判断的必要短摘'),('uncertainty','争议、反证或不确定性'),('visual_limits','画面可以表达什么，哪些细节没有依据')]:
            c[key]=st.text_area(label,value=c[key],height=75)
        if st.form_submit_button('保存主张'):
            v['claims']=[old for old in v['claims'] if old['id']!=selected]+[c];persist(eid,v)

st.subheader('当前方案核查')
issues=W.editorial_issues(eid)
if issues:
    from app.webui.studio_components import show_issues
    show_issues(issues)
else:st.info('请记录本稿关键史实的实际核查结论。详细史料和创作卡按需填写。')
review_form(eid,'editorial')
