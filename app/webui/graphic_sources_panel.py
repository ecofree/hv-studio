"""Review a local extraction before filling the existing, editable graphics form."""
import hashlib

import streamlit as st
from PIL import ImageDraw

from app.services import graphic_extraction as E, studio_graphics as G


def show_evidence(eid,evidence,raw,key):
    if not evidence: return
    src=evidence.get('source_column')
    lines=[]
    for i,(row,link) in enumerate(zip(evidence['rows'],evidence['links'])):
        line={'资料内容':' ｜ '.join(row),'行内出处':row[src] if src is not None else '',
              '原有旁白中的对应句':link['quote'] if link else '无唯一对应','核对提示':evidence['checks'][i]}
        if 'raw_rows' in evidence: line['校订前识别文字']=' ｜ '.join(evidence['raw_rows'][i])
        lines.append(line)
    st.dataframe(lines,hide_index=True)
    if evidence['origin']!='截图': return
    if raw is None:
        path=E.original_path(eid,evidence['sha256'])
        if not path.is_file():
            st.warning('原始截图缺失，请重新选择截图')
            return
        raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=evidence['sha256']: raise ValueError('截图与提取记录不一致')
    row=st.number_input('在原图查看第几行',min_value=1,max_value=len(lines),step=1,key=key+'-line')
    with G.open_upload(raw) as im:
        d=ImageDraw.Draw(im)
        for box in evidence['boxes'][row-1]:
            if box: d.rectangle(box,outline='#ffb84d',width=max(2,im.width//500))
        bbox=evidence.get('region')
        if bbox:
            with im.crop(bbox) as crop:
                crop.thumbnail((1100,600))
                st.image(crop,caption='橙框标出这一行识别到的单元格')
        else: st.image(im)


def extract(eid,document,key,previous=None):
    selected_key=key+'-selected'
    if st.checkbox('从剧本或截图提取资料',key=key+'-open'):
        mode=st.radio('提取来源',['当前剧本','表格截图'],horizontal=True,key=key+'-origin')
        raw=None
        engine=''
        origin='剧本' if mode=='当前剧本' else '截图'
        if origin=='剧本':
            st.caption('剧本设计时可在讲述段后写「【上屏表格：标题】」并粘贴 Markdown 或制表符表格。表内文字只上屏。')
            material=E.script_input(eid)
            digest=hashlib.sha256(material.encode()).hexdigest()
        else:
            upload=st.file_uploader('上传表格截图',type=['png','jpg','jpeg','webp'],key=key+'-source-upload')
            raw=upload.getvalue() if upload is not None else None
            if raw is None and previous and previous['origin']=='截图':
                path=E.original_path(eid,previous['sha256'])
                if path.is_file():
                    raw=path.read_bytes()
                    st.caption('已选用这则图文保存的原始截图，可上传新图替换。')
            digest=hashlib.sha256(raw).hexdigest() if raw else ''
            language=st.selectbox('截图文字',['简体中文','繁體中文','English'],key=key+'-lang')
        signature=(origin,digest,language if raw else '')
        cached=st.session_state.get(key+'-result')
        if st.button('提取资料',key=key+'-run',disabled=not digest):
            if not cached or cached[0]!=signature:
                if origin=='剧本':
                    sheets=E.from_script(material,document)
                else:
                    from app.services.graphic_ocr import read_image
                    scan=read_image(raw,language)
                    sheets=E.from_ocr(scan)
                    engine=scan['engine']+' · '+scan['language']
                cached=(signature,sheets,engine)
                st.session_state[key+'-result']=cached
        if cached and cached[0]==signature:
            _,sheets,engine=cached
            if not sheets:
                st.info('这份资料中还没有识别到可整理的数值或表格。')
            else:
                sub=key+'-'+digest[:16]+signature[2]
                ix=st.selectbox('识别出的表格或数字段',range(len(sheets)),
                    format_func=lambda i:f'{i+1} · '+ ' / '.join(sheets[i]['columns'][:4]),key=sub+'-sheet')
                sheet=sheets[ix]
                sub+='-'+str(ix)
                if origin=='截图' and st.checkbox('对照原图校订识别文字',key=sub+'-correct'):
                    from app.webui.graphics_panel import _grid
                    st.image(raw,caption='上传的原图')
                    corrected=st.text_area('逐格校订（保留行数与列数）',
                        value=_grid([sheet['columns'],*sheet['rows']]),height=220,key=sub+'-corrected')
                    sheet=E.correct_sheet(sheet,corrected)
                total=len(sheet['rows'])
                options=[None,*range(len(sheet['columns']))]
                ref=st.selectbox('史料出处在哪一列',options,index=options.index(E._source_col(sheet['columns'])),
                    format_func=lambda i:'未列出处' if i is None else sheet['columns'][i] or f'列 {i+1}',key=sub+'-ref')
                sheet=dict(sheet,source_column=ref)
                lo=st.number_input('首行',min_value=1,max_value=total,value=1,key=sub+'-lo')
                hi=st.number_input('末行',min_value=lo,max_value=total,value=min(total,lo+47),key=sub+'-hi-'+str(lo))
                cols=st.multiselect('上屏列',list(range(len(sheet['columns']))),
                    default=list(range(min(5,len(sheet['columns'])))),format_func=lambda i:sheet['columns'][i] or f'列 {i+1}',key=sub+'-columns')
                table=st.checkbox('保留为对照表',key=sub+'-table')
                if 2<=len(cols)<=5 and hi-lo<48:
                    draft=E.propose(sheet,document,list(range(lo-1,hi)),cols,origin=origin,sha=digest,engine=engine,as_table=table)
                    st.write('建议使用'+G.KINDS[draft['kind']]+'。'+draft['recommendation']+'。')
                    if draft['warnings']: st.caption('。'.join(draft['warnings']))
                    show_evidence(eid,draft['evidence'],raw,sub+f'-{lo}-{hi}')
                    if st.button('将所选资料填入图文编辑器',key=sub+'-apply'):
                        st.session_state[selected_key]=(draft,raw)
                        st.rerun()
                else:
                    st.caption('每则图文可选 1–48 行及 2–5 列。')
    if selected_key in st.session_state:
        if st.button('清除本次提取草稿',key=key+'-clear'):
            del st.session_state[selected_key]
            st.rerun()
        return st.session_state[selected_key]
    return {},None
