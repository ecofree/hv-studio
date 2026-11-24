"""Edit and share historical geography with an optional, source-attributed event sequence."""
import copy
import hashlib
import re

import streamlit as st

from app.services import atlas_terrain as terrain, historical_atlas as A, studio_graphics as G


def _new_terrain(prefix):
    bounds = st.text_input('底图经纬度范围（西、南、东、北）',key=prefix+'-bounds',
                           placeholder='108.32, 33.66, 113.05, 35.61')
    mode = st.radio('地形和河网数据来源',['上传本地资料','下载公开数据'],horizontal=True,key=prefix+'-data-mode')
    tile_data = river_data = None
    zoom = None
    if mode=='上传本地资料':
        tiles = st.file_uploader('Mapzen Terrarium 高程瓦片 ZIP',type=['zip'],key=prefix+'-tiles',
                                 help='压缩包内保留 z/x/y.png，使用同一缩放级别。')
        rivers = st.file_uploader('Natural Earth 河网 GeoJSON',type=['geojson','json'],key=prefix+'-rivers')
        tile_data = tiles.getvalue() if tiles is not None else None
        river_data = rivers.getvalue() if rivers is not None else None
    else:
        zoom = int(st.number_input('高程瓦片缩放级别',min_value=1,max_value=14,value=8,key=prefix+'-zoom'))
        st.caption('使用 Mapzen Terrain Tiles 和 Natural Earth。范围较大时可降低缩放级别。')
    signature=(bounds,mode,zoom,hashlib.sha256(tile_data).hexdigest() if tile_data else None,
               hashlib.sha256(river_data).hexdigest() if river_data else None)
    receipt_key=prefix+'-terrain-result'
    if st.button('生成地形与河网底图',key=prefix+'-build-terrain',
                 disabled=not bounds.strip() or (zoom is None and not (tile_data and river_data))):
        box=terrain.parse_bounds(re.split(r'[,，\s]+',bounds.strip()))
        if zoom is not None:
            tile_data,river_data=terrain.fetch_public(box,zoom)
        raw,geo=terrain.build(box,tile_data,river_data)
        st.session_state[receipt_key]=(signature,raw,geo)
    current=st.session_state.get(receipt_key)
    if not current or current[0]!=signature:
        return None,None
    _,raw,geo=current
    st.caption(f"底图已生成。高程瓦片 {geo['tile_count']} 张，河网线条 {geo['river_lines']} 条。")
    return raw,geo


def edit(eid,old,prefix,grid):
    library=A.templates()
    names=['当前底图','唐末两京示意图','导入底图包'] if old.get('atlas') else ['唐末两京示意图','空白底图','导入底图包']
    options=names+['按经纬度生成地形底图']+[p.as_posix() for p in library]
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
    elif choice=='按经纬度生成地形底图':
        data,geo=_new_terrain(prefix+'-geography')
        if geo is None:
            return None,None,'',None,None
        atlas=dict(name='',period='',source=terrain.CREDITS,features=[],sites=[],geography=geo)
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
    medium=old.get('media') if choice=='当前底图' else None
    atlas=dict(atlas,name=name.strip(),period=period.strip(),source=source.strip())
    if 'geography' in atlas:
        box=atlas['geography']['bounds']
        st.caption('已绑定经纬度范围 '+', '.join(map(str,box))+'。可在同一底图上编辑或增加地点。')
        sites=st.text_area('有出处的地标（名称、经度、纬度、类型、年代、出处、说明）',value=grid(atlas['sites']),
            help='粘贴七列表格，坐标为 WGS84 十进制度。城址、关隘和地点可绑定事件，河流和山地只标名称。',key=key+'-sites')
        if not sites.strip():
            st.info('填入至少一个带有年代与出处的地标，随后可编排历史事件。')
            return None,None,'',None,None
        atlas['sites']=G.parse_grid(sites)
        atlas['places']=terrain.map_sites(atlas['sites'],box)
    else:
        places=st.text_area('地标（地名、横向位置、纵向位置、地理说明）',value=grid(atlas['places']),
            help='粘贴四列表格，位置为底图内 0 到 1 的比例。事件中直接使用地名。',key=key+'-places')
        features=st.text_area('地形线（名称、河流或山地、节点）',value=grid(atlas.get('features',[])),
            help='每条线一行，节点填 x,y，以空格分隔。使用图片自带地形时可留空。',key=key+'-features')
        picture=st.file_uploader('历史底图图片（可选）',type=['png','jpg','jpeg','webp'],key=key+'-picture')
        if picture is not None:
            data=picture.getvalue()
        elif (medium or data) and st.checkbox('去掉底图图片',key=key+'-no-picture'):
            medium=data=None
        atlas.update(places=G.parse_grid(places) if places.strip() else [],
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
