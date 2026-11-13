import copy
from pathlib import Path
import streamlit as st
from app import paths
from app.services import studio_workflow as W
from app.webui.studio_components import current,persist
eid,v,doc=current();units={u['unit_id']:u['source_text'] for u in doc['units']}
st.subheader('让每个画面推进一个意思')
st.caption('叙事节点可以细于旁白段落。它记录新信息和观察焦点，不改变原文、人物或画风。')
if v.get('cards'):
    with st.expander('首帧与幕卡设计'):
        st.dataframe([{'标题':r['card'].get('title',''),'副标题':r['card'].get('subtitle',''),
                       '显示时长（毫秒）':r['duration_ms'],'定位':r['at']} for r in v['cards']],hide_index=True)
        st.caption('卡片文字摘自当前原稿；镜头与语句的实际时间在新音轨最终速度确定后对齐。')
design_path=W.folder(eid)/'act-design.json'
if design_path.exists():
    with st.expander('八幕设计与待补素材'):
        acts=W.read(design_path)
        st.dataframe([{'幕':a['title'],'推进':a['purpose'],'情绪':a['performance'],
                       '环境与视差':a['motion_intent'],'画面边界':a['visual_limits']} for a in acts['acts']],hide_index=True)
        for issue in acts.get('unresolved',[]):st.caption(issue)
with st.expander('查看正文与来源版本'):
    from app.managers import load
    from app.orchestrator import _load_script_md
    source=_load_script_md(load(eid));identity=W.identity(eid)
    from app.services.studio_sources import contract
    source_contract=contract(eid)
    if source_contract:
        st.caption('用户原稿已锁定。要更新正文，请修改原项目后从左侧导入新版本。')
        revision=Path(source_contract['revision'])
        st.text_area('原稿（只读）',value=(revision/'source.md').read_text('utf-8'),height=300,disabled=True)
        st.download_button('下载原稿原始文件',data=(revision/'source.md').read_bytes(),file_name='source.md')
        st.caption('版本：'+revision.name+' · 旁白尚未制作')
        authoring=W.read(revision/'authoring.json')
        st.write(f"已分离 {len(authoring['acts'])} 幕、{len(authoring['notes'])} 条逐幕标注及片尾字幕；史料备注保留在原稿。")
    else:
        with st.form('source-'+eid):
            edited=st.text_area('正文版本',value=source,height=300)
            if st.form_submit_button('保存为独立文案修订'):
                try:
                    W.replace_source(eid,edited,expected_identity=identity);st.success('旧正文保留；新的主张与分镜需重新核对。');st.rerun()
                except (ValueError,OSError) as exc:st.error(str(exc))
if v['binding']!=W.binding(eid):
    st.warning('正文或底图需求有更新，请重新绑定。正文变更需要复核史实；仅改画面不撤销正文确认。')
    if st.button('将方案重新绑定到当前正文和底图需求'):
        try:
            W.rebind_current(eid,expected_identity=W.identity(eid));st.rerun()
        except (ValueError,OSError) as exc:st.error(str(exc))
    st.caption('仍对应正文的记录保留；失去对应关系的旧节点和声音事件留在历史修订中，按实际制作需要重新安排。')
if v['beats']:
    st.dataframe([{'节点':b['id'],'作用':b['purpose'],'新信息':b['new_information'],'底图':b['image_id'],'动效':'局部动画' if b.get('animation') else b['motion']['type']} for b in v['beats']],hide_index=True)
chosen=st.selectbox('编辑叙事节点',['新增']+[b['id'] for b in v['beats']])
b=next((copy.deepcopy(b) for b in v['beats'] if b['id']==chosen),dict(id=f'beat-{len(v["beats"])+1:03}',unit_id=next(iter(units)),text_start=0,purpose='',new_information='',claim_ids=[],image_id='',motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5])))
unit=st.selectbox('对应正文',list(units),index=list(units).index(b['unit_id']) if b['unit_id'] in units else 0,format_func=lambda k:units[k][:65])
st.write(units[unit])
with st.form('beat-'+chosen+'-'+unit):
    b['unit_id']=unit;b['id']=st.text_input('节点编号',value=b['id'])
    b['text_start']=int(st.number_input('从本段第几个字开始（首字为 0）',min_value=0,max_value=len(units[unit])-1,value=min(b['text_start'],len(units[unit])-1)))
    b['purpose']=st.text_input('节点作用（选填）',value=b['purpose'])
    b['new_information']=st.text_area('新信息（选填）',value=b['new_information'])
    b['performance']=st.text_input('这一句怎么说：意图、重词、节奏与停顿',value=b.get('performance',''))
    b['claim_ids']=st.multiselect('关联史料（选填）',[c['id'] for c in v['claims']],default=[c for c in b['claim_ids'] if c in {c['id'] for c in v['claims']}])
    b['image_id']=st.text_input('使用的底图编号',value=b['image_id'])
    options={'保持观察':'static','缓慢靠近':'push','缓慢拉远':'pull'}
    motion=st.selectbox('镜头焦点变化',list(options),index=next((i for i,x in enumerate(options.values()) if x==b['motion']['type']),0))
    kind=options[motion];b['motion']=dict(type=kind,start_zoom=1.06 if kind=='pull' else 1.,end_zoom=1.06 if kind=='push' else 1.,start_xy=[.5,.5],end_xy=[.5,.5])
    if st.form_submit_button('保存叙事节点'):
        v['beats']=[old for old in v['beats'] if old['id']!=chosen]+[b]
        order={k:i for i,k in enumerate(units)};v['beats'].sort(key=lambda x:(order.get(x['unit_id'],999999),x['text_start']))
        persist(eid,v)

with st.expander('底图需求：沿用现有画风'):
    p=paths.episode_dir(eid)/'image_prompts.json';prompts=W.read(p) if p.exists() else {}
    key=st.selectbox('选择底图需求',['新增']+list(prompts))
    with st.form('image-'+key):
        ident=st.text_input('底图编号',value='' if key=='新增' else key)
        prior=prompts.get(key,{})
        prior=prior if isinstance(prior,dict) else {'en':prior}
        prompt=st.text_area('主体、行动、环境、道具与光线',value=prior.get('en',''),height=150)
        shot_labels={'':'随分镜','wide':'全景','medium':'中景','close_up':'近景','insert':'道具插镜'}
        current_shot=prior.get('shot_type') or ''
        shot_options=list(dict.fromkeys([*shot_labels, current_shot]))
        shot_type=st.selectbox('构图用途',shot_options,index=shot_options.index(current_shot),
                               format_func=lambda key:shot_labels.get(key,key))
        from app.config.config import get_settings
        available=sorted({m for c in get_settings().channels if c.enabled for m in (c.models or [c.model])})
        current_model=prior.get('model') or ''
        options=list(dict.fromkeys(['',*available,current_model]))
        model=st.selectbox('生图模型',options,index=options.index(current_model),format_func=lambda m:m or '按分镜自动选择')
        if st.form_submit_button('保存底图需求'):
            try:W.save_image_brief(eid,ident,prompt,expected_identity=W.identity(eid),model=model,shot_type=shot_type);st.rerun()
            except (ValueError,OSError) as exc:st.error(str(exc))

    if prompts and available:
        with st.form('batch-models-'+eid):
            selected=st.multiselect('要指定型号的新底图',list(prompts))
            default=get_settings().image.primary.model
            batch_model=st.selectbox('批量指定型号',available,index=available.index(default) if default in available else 0)
            if st.form_submit_button('保存所选新底图的型号'):
                try:
                    assigned=W.assign_scene_models(eid,selected,expected_identity=W.identity(eid),model=batch_model)
                    st.write(assigned or '所选镜头沿用现有安排。')
                except (ValueError,OSError,RuntimeError) as exc:st.error(str(exc))

with st.expander('局部环境动画与受限视差'):
    st.caption('只移动原画可见的背景区域。人物保护区与作用区必须人工确认，不自动制造人物转身。')
    if v['beats']:
        animation_id=st.selectbox('应用到节点',[b['id'] for b in v['beats']])
        target=next(b for b in v['beats'] if b['id']==animation_id)
        mapping_path=paths.episode_dir(eid)/'production/existing-images.json'
        image_map=W.read(mapping_path).get('images',{}) if mapping_path.exists() else {}
        record=image_map.get(target['image_id'])
        if record:
            st.image(record['path'],width=300)
            with st.form('animation-'+animation_id):
                kind=st.selectbox('局部变化',['烛光起伏','窗外细雨','背景视差'])
                st.caption('区域以画面宽高比例填写：左、上、右、下（0–1）。')
                region=st.text_input('背景作用区域',value='0.05,0.05,0.25,0.25')
                protected=st.text_input('人物或重要道具保护区域',value='0.30,0.15,0.85,0.90')
                verified=st.checkbox('我已核对原画中的背景范围和人物保护范围')
                if st.form_submit_button('保存局部动效'):
                    try:
                        if not verified:raise ValueError('请先核对区域')
                        def rect(text):
                            a,c,d,e=[float(x.strip()) for x in text.split(',')]
                            if not 0<=a<d<=1 or not 0<=c<e<=1:raise ValueError('区域范围错误')
                            return [[a,c],[d,c],[d,e],[a,e]]
                        from app.services.provenance import file_sha256
                        layer=dict(kind={'烛光起伏':'glow','窗外细雨':'rain','背景视差':'parallax'}[kind],polygon=rect(region),feather=.01)
                        layer.update({'strength':.045} if kind=='烛光起伏' else {'density':40,'opacity':.12,'speed':.15} if kind=='窗外细雨' else {'travel':[.006,0]})
                        plan=dict(version=1,source_sha256=file_sha256(record['path']),seed=42,protected=[rect(protected)],layers=[layer])
                        from app.services.scene_animation import validate
                        validate(plan,plan['source_sha256']);target['animation']=plan;persist(eid,v)
                    except (ValueError,OSError) as exc:st.error(str(exc))
        else:st.info('先登记这个节点使用的实际底图，再设计局部保护区域。')

if st.button('更新正式分镜绑定'):
    try:W.update_scene_bindings(eid);st.success('分镜已更新；合成时检查画面，正文史实确认保持有效。');st.rerun()
    except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
if st.button('重新登记可复用底图'):
    try:
        from app.services.existing_images import register
        register(eid);st.success('已登记实际底图；未生成图片，未授予画面批准。')
    except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
if st.button('应用当前画面安排'):
    try:
        result=W.compile_direction(eid)
        st.success('已生成导演事件，配音对齐后执行。' if result else '使用正式分镜；没有额外导演事件。')
    except (ValueError,OSError) as exc:st.error(str(exc))
