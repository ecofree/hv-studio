from pathlib import Path
import streamlit as st
from app.services import studio_workflow as W
from app.webui.studio_components import current,persist,review_form
eid,v,doc=current()
st.subheader('选择声音，按需试听样片')
st.caption('建议先比较开场与关键段落。所有记录都是候选；技术通过不会自动批准声线或表演。')
from app.services import series_voice as SV,studio_voice_contract as VC
with st.expander('系列共用声音方案',expanded=not VC.path_for(eid).exists()):
    schemes=SV.catalog()
    if VC.path_for(eid).exists():
        try:
            locked=VC.load(VC.path_for(eid),source_sha256=doc['source_sha256'],voice=v['voice'])
            st.info(f"本集已锁定 {locked['speaker']} · {locked['target_rate']} 倍速。")
            st.caption('保留原声音绑定。新稿用独立版本绑定共用方案，不继承旧音轨、旧字幕或审核。')
        except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    elif schemes:
        with st.form('series-voice-'+eid):
            scheme=st.selectbox('选择已确认的系列声音方案',schemes,format_func=lambda p:SV.read(p)['name'])
            st.caption('只绑定当前原文、声音参数与目标倍速。保留逐幕表演备注，配音请求沿用统一声线指令。不会开始配音。')
            if st.form_submit_button('将共用声音方案绑定到当前新稿'):
                try:
                    SV.prepare_episode(eid,scheme,expected_identity=W.identity(eid));st.rerun()
                except (ValueError,OSError,KeyError,RuntimeError) as exc:st.error(str(exc))
    else:st.caption('尚未登记系列共用声音方案。')
if v['scope']=='full':
    with st.expander('从当前正文建立独立短样片'):
        with st.form('extract-pilot-'+eid):
            start=int(st.number_input('从第几段开始',min_value=1,max_value=len(doc['units']),value=1))
            end=int(st.number_input('到第几段结束',min_value=1,max_value=len(doc['units']),value=min(2,len(doc['units']))))
            new_id=st.text_input('新样片版本名称',value=eid+'-pilot-01')
            st.caption('复制所选正文、证据、底图和表演意图；沿用声音基准，单独制作短音轨，不继承旧审核。')
            if st.form_submit_button('建立短样片草稿'):
                try:
                    from app.services.studio_workspace import create_pilot
                    create_pilot(eid,new_id,start-1,end);st.success('已建立 '+new_id+'，可在左侧选择它继续编辑。')
                except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
with st.expander('高级声音设置'):
    with st.form('voice-'+eid):
        for key,label in [('identity','固定旁白身份 / speaker'),('model','现有模型'),('baseline','原声试听基准文件路径'),('master','选定的完整旁白文件路径（已有录音直接复用）'),('delivery','表演原则：重音、语速、停顿与情绪边界')]:
            v['voice'][key]=st.text_input(label,value=v['voice'].get(key,''))
        if st.form_submit_button('保存声音基准'):persist(eid,v)
if Path(v['voice'].get('baseline','')).is_file() and st.checkbox('试听现有声音基准'):
    st.audio(v['voice']['baseline'])
with st.expander('记录开场备选'):
    if v['hooks']:st.dataframe(v['hooks'],hide_index=True)
    with st.form('hook-'+eid):
        h={k:st.text_input(label) for k,label in [('title','标题'),('opening','首句'),('visual','主视觉'),('hypothesis','这版要验证什么') ]}
        if st.form_submit_button('增加开场方案'):v['hooks'].append(h);persist(eid,v)
identity=W.identity(eid)
with st.expander('速度与声音对照检查（只检查文件）'):
    from app.services.studio_media import speed_plan,inspect_video,compare_video_probes
    policy=W.folder(eid)/'playback-policy.json'
    if policy.exists():
        st.caption(f"本版本目标为 {W.read(policy)['target_rate']} 倍速；先确定最终速度，再对齐字幕。")
    with st.form('speed-check-'+eid):
        audio_path=st.text_input('待检查音轨路径')
        receipt_path=st.text_input('音轨速度记录路径')
        target_rate=st.number_input('目标累计速度',min_value=.25,max_value=4.,value=1.2,step=.05)
        locked_clock=st.checkbox('此音轨的字幕已经对齐')
        if st.form_submit_button('检查是否需要变速'):
            try:
                p=speed_plan(audio_path,receipt_path,target_rate,subtitles_aligned=locked_clock)
                st.success(f"已应用 {p['applied_rate']} 倍；还需乘 {p['factor']:.4f}。"+('直接复用，不再加速。' if p['operation']=='reuse' else '须在变速后重新对齐字幕。'))
            except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    with st.form('ab-check-'+eid):
        before_video=st.text_input('声音修改前视频路径')
        after_video=st.text_input('声音修改后视频路径')
        if st.form_submit_button('核对是否为同画面的声音对照'):
            try:
                check=compare_video_probes(inspect_video(before_video),inspect_video(after_video))
                if check['ok']:st.success('视频内容、帧数与逐包时间一致；还需核对音轨采样、速度和字幕。')
                else:st.error('；'.join(check['issues']))
            except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    st.caption('新版文案与旧版内容不同，只能作历史参照。声音修复的严格对照需保持文案、字幕、累计速度和画面一致。')
with st.form('sample-'+eid):
    label=st.text_input('样片名称',placeholder='开场 A / 紧张段 / 跨幕声线对比')
    file=st.text_input('副本中的音频或视频文件路径')
    proof=st.text_input('制作来源记录路径（可留空，默认读取同目录记录）')
    st.caption('登记 1–120 秒的实际文件，保留原文、音色与旧素材；登记不授予批准。')
    if st.form_submit_button('登记短样片'):
        try:W.register_sample(eid,file,label,expected_identity=identity,source_receipt=proof or None);st.rerun()
        except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
rows=W.samples(eid)
if rows:
    choice=st.selectbox('选择试听版本',range(len(rows)),format_func=lambda i:rows[i]['label'])
    row=rows[choice]
    st.caption(f"{row['duration']:.2f} 秒 · 对应方案 {row['design_identity'][:12]}")
    if row['has_video']:st.video(row['path'])
    else:st.audio(row['path'])
    st.caption('一次展示一个播放器，避免对比时多段声音叠播。')
review_form(eid,'pilot')
