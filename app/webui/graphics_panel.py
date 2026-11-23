"""Edit one graphic against the current episode's spoken source."""
import csv
import io
import uuid

import streamlit as st

from app.services import studio_graphics as G
from app.services.spec import VideoFormat


_EXAMPLES = {
    'table': ('隋代钱币与流通','资料节选自《隋书》卷二十四《食货志》',
              '阶段\t形制或重量\t流通记录\t出处\n开皇五铢\t每千枚四斤二两\t关口用样钱核验\t《隋书·食货志》\n大业私铸钱\t先见每千枚二斤，后渐至一斤\t私铸增多，钱体变薄\t《隋书·食货志》\n代用钱\t铁片、皮、纸\t与钱混用\t《隋书·食货志》'),
    'bars': ('每千枚钱有多重','用户提供的表一节选 · 按一斤十六两换算，史料待核',
             '开皇五铢\t66\t四斤二两\n私铸钱\t32\t二斤\n更轻者\t16\t一斤'),
    'person': ('人物姓名','人物传记出处','身份\t填写与本段相关的身份\n经历\t填写旁白提到的一件事\n影响\t填写这件事与本集的关系'),
    # Reuse the node order and its normalized positions from bin/draw_sandtable.py (940×1672).
    'route': ('黄巢路线沙盘','本项目潼关沙盘 · 节点为叙事示意',
              '曹州\t0.681\t0.868\t起点\n汴州\t0.396\t0.736\t路线推进至汴州\n洛阳\t0.602\t0.579\t洛阳节点\n潼关\t0.315\t0.386\t潼关节点\n长安\t0.502\t0.190\t路线终点'),
}
_WAREHOUSES = ('官仓与户籍的数字','用户提供的第二张样表节选 · 数值及口径待编审核对',
               '项目\t样表数字\t上屏口径\t待核出处\n回洛仓\t约七百座仓窖\t遗址总量估算\t洛阳回洛仓考古材料\n均田授田\t露田八十亩，永业田二十亩\t法令标准\t《隋书·食货志》\n大索貌阅\t隐丁四十四万三千\t丁数与口数分列\t《隋书·食货志》')
_HINTS = {
    'table':'首行填列名，第一列填每行主题。横屏并排对照，竖屏每页一行。表格文字只用于画面。',
    'bars':'每行填项目、非负数值、可选的显示文字。所有数值共用同一单位。',
    'person':'每行填一个字段名及相应内容，例如“身份”和“隋朝大臣”。',
    'route':'每行填地名、横向位置、纵向位置、事件说明。左上角为 0、0，右下角为 1、1。',
    'campaign':'每行填阶段、地点、来处、行动方、守方、事件。地点取自底图。来处留空时仅标所在地点，阵营未知可留空。',
}


def _grid(rows):
    out = io.StringIO()
    csv.writer(out,delimiter='\t',lineterminator='\n').writerows(rows)
    return out.getvalue().rstrip('\n')


def render(eid,document):
    with st.expander('数据表、图表、人物与地图'):
        if not st.checkbox('编辑或预览图文',key='graphics-open-'+eid):
            return
        try:
            _editor(eid,document)
        except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
            st.error(str(exc))


def _editor(eid,document):
    saved,revision = G.snapshot(eid)
    entries = (saved or {}).get('graphics',{})
    stale = [s['title'] for s in entries.values() if s['bound_to']!=G.source_key(document)]
    if stale:
        st.info('正文已有新版本，需要重新绑定：'+'、'.join(stale))
    ids = [u['unit_id'] for u in document['units']]
    if not ids:
        st.info('导入正文后可以绑定图文。')
        return
    texts = {u['unit_id']:u['source_text'] for u in document['units']}
    scope = 'graphics-'+eid+'-'+G.source_key(document)[:12]+'-'+str(revision)[:12]
    key = st.selectbox('本集图文',['',*entries],format_func=lambda k:entries[k]['title'] if k else '新增图文',key=scope)
    old = entries.get(key,{})
    evidence_image=None
    if not key or (old.get('kind') in ('table','bars') and st.checkbox('重新提取这则图文的资料',key=scope+'-replace-'+key)):
        from app.webui.graphic_sources_panel import extract
        draft,evidence_image=extract(eid,document,scope+'-extract-'+key,old.get('evidence'))
        if draft:
            from app.services.provenance import fingerprint
            old=draft
            scope+='-'+fingerprint(old)[:16]
    elif old.get('evidence') and st.checkbox('查看这则图文的原始资料',key=scope+'-evidence-'+key):
        from app.webui.graphic_sources_panel import show_evidence
        show_evidence(eid,old['evidence'],None,scope+'-original-'+key)
    kind = st.selectbox('画面类型',list(G.KINDS),index=list(G.KINDS).index(old.get('kind','table')),
                        format_func=G.KINDS.get,key=scope+'-kind-'+key)
    prefix = scope+'-'+key+'-'+kind
    existing = old.get('kind')==kind
    example = st.checkbox('填入示例数据',key=prefix+'-example') if not existing and kind!='campaign' else False
    prefix += '-example' if example else '-edit'
    title,source,grid = _EXAMPLES[kind] if example else ('','','')
    if kind=='table' and example:
        group = st.radio('样表',['钱币与流通','官仓与户籍'],horizontal=True,key=prefix+'-sample')
        if group=='官仓与户籍':
            title,source,grid = _WAREHOUSES
            prefix += '-warehouse'
    if existing:
        rows = old['rows']
        grid = _grid([old['columns'],*rows] if kind=='table' else rows)
        title,source=old['title'],old['source']
    unit = detail = ''
    upload = None
    media = old.get('media') if existing else None
    atlas=None
    if kind=='campaign':
        from app.webui.atlas_panel import edit,share
        atlas,upload,atlas_key,media,story=edit(eid,old if existing else {},prefix,_grid)
        if atlas is None: return
        prefix=atlas_key
        if story:
            title,source,grid=story['title'],story['source'],_grid(story['rows'])
            old={}
    title = st.text_input('上屏标题或人物姓名',value=title,key=prefix+'-title')
    source = st.text_input('上屏资料出处',value=source,key=prefix+'-source')
    if kind=='bars':
        unit = st.text_input('统一单位',value=old.get('unit','两 / 每千枚' if example else ''),key=prefix+'-unit')
    if kind=='person':
        detail = st.text_input('年代或简要身份',value=old.get('detail',''),key=prefix+'-detail')
    if kind in ('person','route'):
        blob = st.file_uploader('人物像' if kind=='person' else '地图底图（可选）',
                                type=['png','jpg','jpeg','webp'],key=prefix+'-media')
        if blob is not None:
            upload = blob.getvalue()
        elif media and st.checkbox('移除当前配图',key=prefix+'-remove-media'):
            media = None
    grid = st.text_area('数据行',value=grid,height=150,help=_HINTS[kind]+' 支持从表格粘贴、CSV 和 Markdown 表格。',
                        key=prefix+'-data')
    if atlas is not None: share(eid,atlas,media,upload,prefix,title,source,grid)
    first = st.selectbox('开始旁白段',ids,index=ids.index(old['first_unit']) if old.get('first_unit') in ids else 0,
                         format_func=lambda u:texts[u][:75],key=prefix+'-first')
    lasts = ids[ids.index(first):]
    last = st.selectbox('结束旁白段',lasts,index=lasts.index(old['last_unit']) if old.get('last_unit') in lasts else 0,
                        format_func=lambda u:texts[u][:75],key=prefix+'-last')
    st.text('\n'.join(texts[u] for u in ids[ids.index(first):ids.index(last)+1]))
    start = st.text_input('入场原句（留空从段首开始）',value=old.get('start_quote',''),key=prefix+'-start')
    stop = st.text_input('在哪句之前退出（留空用到段末）',value=old.get('stop_before',''),key=prefix+'-stop')
    timing='quotes'
    if kind in ('table','bars'):
        timing=st.radio('换页方式',['span','quotes'],horizontal=True,
            format_func=lambda v:'按展示时长自动分页' if v=='span' else '按指定原句切换',
            index=1 if existing and old.get('timing','quotes')=='quotes' else 0,key=prefix+'-timing')
    if timing=='quotes' and st.button('按数据行名称匹配原句',key=prefix+'-suggest'):
        data = G.parse_grid(grid)[1:] if kind=='table' else G.parse_grid(grid)
        scope_units = {'units':document['units'][ids.index(first):ids.index(last)+1]}
        suggestions = []
        for row in data[1:]:
            try:
                G.locate(scope_units,row[0])
            except ValueError:
                st.info('未能逐条匹配到唯一原句，已保留手填内容。')
                break
            suggestions.append(row[0])
        else:
            st.session_state[prefix+'-quotes'] = '\n'.join(suggestions)
    quotes=''
    if timing=='quotes':
        suggestions=old.get('quotes') or [m['quote'] if m else '' for m in old.get('evidence',{}).get('links',[])[1:]]
        quotes=st.text_area('从第 2 条开始，每条对应一句旁白原文，按出现顺序每行一句',
                            value='\n'.join(suggestions),key=prefix+'-quotes')
    fmt = st.radio('预览画幅',list(VideoFormat),format_func=lambda f:'横屏 16:9' if f==VideoFormat.LANDSCAPE else '竖屏 9:16',
                   horizontal=True,key=prefix+'-format')
    step = st.number_input('预览第几条',min_value=1,step=1,key=prefix+'-step')
    preview = st.button('预览这一条',key=prefix+'-preview')
    commit = st.button('保存并用于本集',key=prefix+'-save')
    if key and st.button('移除此图文',key=prefix+'-delete'):
        G.save(eid,key,None,document,revision)
        st.rerun()
    if not preview and not commit:
        return
    rows = G.parse_grid(grid)
    columns = rows.pop(0) if kind=='table' and rows else []
    entry = dict(kind=kind,title=title.strip(),source=source.strip(),unit=unit,detail=detail,
                 rows=rows,columns=columns,media=media,first_unit=first,last_unit=last,timing=timing,
                 start_quote=start.strip(),stop_before=stop.strip(),quotes=[s.strip() for s in quotes.splitlines() if s.strip()])
    if atlas is not None: entry['atlas']=atlas
    if old.get('evidence') and kind in ('table','bars'): entry['evidence']=old['evidence']
    if preview:
        # Preview the draft artwork before the editor has chosen all the word anchors.
        G.validate_spec(dict(entry,quotes=['']*max(0,len(rows)-1) if timing=='quotes' else []))
        if step>len(rows):
            raise ValueError('预览条目超出数据行数')
        with G.open_media(eid,entry,upload) as medium, G.render_plate(entry,step-1,fmt,medium) as im:
            im.thumbnail((940,700))
            st.image(im)
    if commit:
        G.save(eid,key or uuid.uuid4().hex[:12],entry,document,revision,upload=upload,evidence_image=evidence_image)
        st.rerun()
