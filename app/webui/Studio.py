"""Content-led workbench; existing operator console remains available separately."""
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ['PATH']=str(ROOT/'tools/ffmpeg/bin')+os.pathsep+os.environ['PATH']
import streamlit as st
from app import paths
from app.services import studio_workspace as S, studio_workflow as W
from app.services import studio_sources as Sources

st.set_page_config(page_title='历史创作工作台',page_icon=':material/movie:',layout='wide')
st.title('历史创作工作台')
st.caption('从一个值得追问的问题，到一部讲得清楚的历史视频。')
with st.sidebar:
    st.subheader('当前作品')
    include_history=st.checkbox('查看历史与停用版本',value=False)
    ids=Sources.selectable(include_history)
    if st.session_state.get('studio_episode') not in ids:
        st.session_state.pop('studio_episode',None)
    if ids:
        chosen=st.selectbox('选择创作版本',ids,key='studio_episode')
    else:
        chosen=None;st.info('先从旧项目建立新版本，或导入一段正文。')
    with st.expander('从同步原稿建立新版本'):
        st.caption('读取副本中的文件，并核对原项目。正文与配套 TTS 文本不一致时停止导入。')
        with st.form('import-authoring'):
            author_id=st.text_input('新原稿版本名称',placeholder='history-user-02')
            author_title=st.text_input('原稿作品标题')
            author_file=st.text_input('原稿文件路径')
            tts_file=st.text_input('配套 TTS 文本路径（可留空）')
            rules=st.text_area('当前规则文件路径（每行一个）')
            if st.form_submit_button('逐字导入并绑定新版本'):
                try:
                    Sources.import_authoring(author_id,author_title,author_file,tts_file=tts_file or None,
                        rule_files=[p.strip() for p in rules.splitlines() if p.strip()])
                    st.success('原文已完整保留；旧音轨和旧审核未继承。');st.rerun()
                except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    with st.expander('从历史素材建立新版本'):
        old=sorted(p.stem for p in (paths.ROOT/'episodes').glob('*.json'))
        if old:
            with st.form('import-history'):
                old_id=st.selectbox('历史剧集',old)
                new_id=st.text_input('新版本名称',value='history-studio-01')
                if st.form_submit_button('复制为独立创作版本'):
                    try:
                        S.import_episode(old_id,new_id);W.save(new_id,W.new(new_id),expected_revision=0)
                        st.success('已复制正文与帧结构；历史审批未继承。');st.rerun()
                    except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    with st.expander('新建作品或短样片'):
        with st.form('new-work'):
            ident=st.text_input('版本名称',placeholder='history-pilot-01')
            title=st.text_input('作品标题')
            source=st.text_area('正文',height=160,help='保留唯一正文。可使用“### 【第一幕：标题】”组织内容。')
            scope=st.selectbox('制作范围',['短样片','完整作品'])
            if st.form_submit_button('建立创作草稿'):
                try:
                    S.create_episode(ident,title,source,scope='pilot' if scope=='短样片' else 'full');st.rerun()
                except (ValueError,OSError) as exc:st.error(str(exc))
    st.caption('原项目与旧素材保留。新任务使用独立工作区；模型调用默认零预算。')

if chosen:
    if Sources.external_updates(chosen):
        st.info('外部原稿或规则有更新；当前仍使用已导入版本。需要采用新稿时，请建立新版本。')
    state_record=Sources.disposition(chosen)
    st.caption({'current':'当前用户原稿','draft':'普通草稿','historical':'历史版本','excluded':'停用修订稿'}[state_record['status']]+' · '+state_record['reason'])
    if state_record['status'] in ('historical','excluded'):
        st.warning('此版本仅供追溯，不能用于新制作。')
        st.write('\n\n'.join(u['source_text'] for u in W.document(chosen)['units']))
        st.stop()
    state=W.status(chosen)
    with st.container(horizontal=True):
        st.metric('创作方案','已审核' if state['editorial_approved'] else '待完善')
        st.metric('短样片（可选）','已确认' if state['pilot_approved'] else '按需制作')
        st.metric('制作范围','短样片' if state['scope']=='pilot' else '完整作品')
    pages=[
        st.Page('app_pages/brief.py',title='写稿与史实',icon=':material/search:'),
        st.Page('app_pages/story.py',title='文案与分镜',icon=':material/edit:'),
        st.Page('app_pages/pilot.py',title='声音与可选样片',icon=':material/play_circle:'),
        st.Page('app_pages/production.py',title='成片制作',icon=':material/movie:'),
        st.Page('app_pages/feedback.py',title='发布复盘',icon=':material/bar_chart:'),
    ]
    st.navigation(pages,position='top').run()
else:
    st.info('从左侧建立一个新版本，确认正文与关键史实后即可准备声音。')
    st.write('写稿与关键史实 → 配音 → 配图字幕 → 完整听看与导出 → 复盘')
