"""Edit and share historical geography with an optional, source-attributed event sequence."""
import copy
import hashlib

import streamlit as st

from app.services import historical_atlas as A, studio_graphics as G


def edit(eid,old,prefix,grid):
    library=A.templates()
    names=['当前底图','唐末两京示意图','导入底图包'] if old.get('atlas') else ['唐末两京示意图','空白底图','导入底图包']
    options=names+[p.as_posix() for p in library]
    labels={p.as_posix():p.parent.name.removeprefix('atlas-')+' · '+p.stem[:8] for p in library}
    choice=st.selectbox('可复用底图',options,format_func=lambda v:labels.get(v,v),key=prefix+'-library')
    data=story=None
    if choice=='当前底图':
        atlas=old['atlas']
    elif choice=='唐末两京示意图':
        atlas=copy.deepcopy(A.TANG)
        story=dict(title='黄巢军由东都入长安',source='《资治通鉴》卷二五四 · 广明元年',rows=A.EVENTS)
    elif choice=='空白底图':
        atlas=dict(name='',period='',source='',places=[],features=[])
    else:
        if choice=='导入底图包':
            upload=st.file_uploader('导入历史底图 ZIP',type=['zip'],key=prefix+'-bundle')
            if upload is None:
                st.info('选择包含地标、地形和可选底图图片的地图包。')
                return None,None,'',None,None
            packet=upload.getvalue()
        else:
            packet=next(p for p in library if p.as_posix()==choice).read_bytes()
        atlas,data,story=A.unpack(packet)
    key=prefix+'-'+hashlib.sha256((choice+str(atlas)+str(story)).encode()+(data or b'')).hexdigest()[:12]
    name=st.text_input('底图名称',value=atlas['name'],key=key+'-name')
    period=st.text_input('地图年代',value=atlas['period'],key=key+'-period')
    source=st.text_input('底图资料出处',value=atlas['source'],key=key+'-atlas-source')
    places=st.text_area('地标（地名、横向位置、纵向位置、地理说明）',value=grid(atlas['places']),
        help='粘贴四列表格，位置为底图内 0 到 1 的比例。事件中直接使用地名。',key=key+'-places')
    features=st.text_area('地形线（名称、河流或山地、节点）',value=grid(atlas.get('features',[])),
        help='每条线一行，节点填 x,y，以空格分隔。使用图片自带地形时可留空。',key=key+'-features')
    medium=old.get('media') if choice=='当前底图' else None
    picture=st.file_uploader('历史底图图片（可选）',type=['png','jpg','jpeg','webp'],key=key+'-picture')
    if picture is not None:
        data=picture.getvalue()
    elif (medium or data) and st.checkbox('去掉底图图片',key=key+'-no-picture'):
        medium=data=None
    atlas=dict(name=name.strip(),period=period.strip(),source=source.strip(),
               places=G.parse_grid(places) if places.strip() else [],
               features=G.parse_grid(features) if features.strip() else [])
    return atlas,data,key,medium,story


def share(eid,atlas,medium,data,key,title,source,grid):
    with st.expander('跨集复用这张地图'):
        include=st.checkbox('包含本次阶段、路线与双方记载',value=True,key=key+'-with-events')
        keep=st.button('存入地图模板库',key=key+'-keep')
        export=st.checkbox('导出当前地图包',key=key+'-export')
        if not (keep or export): return
        story=dict(title=title,source=source,rows=G.parse_grid(grid)) if include else None
        with G.open_media(eid,dict(media=medium),data) as im:
            if keep:
                saved=A.keep(atlas,im,story)
                st.success('已保存地图版本 '+saved.stem[:8]+'，其他剧集可直接选用。')
            if export:
                packet=A.bundle(atlas,im,story)
                st.download_button('下载可复用地图',packet,file_name='historical-atlas.zip',mime='application/zip',key=key+'-download')
