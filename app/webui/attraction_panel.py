"""Presentation planning inside the existing production page, with no dispatch."""
import copy
import json
import streamlit as st
from app.services import studio_attraction as A
from app.services.provenance import file_sha256, fingerprint


def render(eid, document):
    with st.expander('标题、封面与全片吸引力方案'):
        st.caption('同一份方案串起观看理由、开场和全片转折。保存与下载只处理方案，不改旁白、不生成素材，也不增加审批。')
        try:
            saved = A.load(eid)
            if saved and saved.get('source_identity') != A.source_key(document):
                st.info('正文已有新版本。可在下方建立当前版本的方案，保存时将归档旧方案。')
                plan = A.empty(document)
            else:
                plan = saved or A.empty(document)
            A.validate(plan, document)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            st.warning(str(exc))
            return
        expected = file_sha256(A.path_for(eid)) if saved else None
        from app import paths
        run_path = paths.episode_dir(eid)/'production/v2/run.json'
        try:
            run = json.loads(run_path.read_text('utf-8')) if run_path.exists() else {}
        except (ValueError, OSError):
            run = {}
        image_keys = sorted(run.get('image_assets', {}))
        if not image_keys:
            st.caption('尚未登记本集画面。可先完成分镜意图，素材编号留空；不会自动生成图片。')
        for option in plan.get('title_options', []):
            st.write(option)
        with st.form('attraction-'+eid+'-'+plan['source_identity']):
            title = st.text_input('发布标题', value=plan['title'])
            cover = st.text_input('封面短句', value=plan['cover']['text'])
            composition = st.text_area('封面构图', value=plan['cover']['composition'])
            description = st.text_area('发布简介', value=plan.get('description', ''))
            rows = [dict(原句=b['quote'],画面=b.get('visual',''),作用=b.get('purpose',''),底图=b.get('image_id',''),构图=b.get('framing','保留当前安排'))
                    for b in plan.get('visual_beats', [])]
            if rows:
                rows = st.data_editor(rows, hide_index=True, disabled=['原句'],
                    column_config={'底图':st.column_config.SelectboxColumn('已有底图',options=['']+sorted(set(image_keys+[b.get('image_id','') for b in plan['visual_beats']]))),
                                   '构图':st.column_config.SelectboxColumn('构图',options=list(A.FRAMINGS))},
                    key='attraction-beats-'+eid+'-'+str(expected))
            choices = [''] + [u['source_text'] for u in document['units']]
            selected = st.selectbox('添加镜头所用的旁白', choices, key='beat-source-'+eid+'-'+plan['source_identity']+'-'+str(expected))
            excerpt = st.text_input('镜头起点原句（可从所选旁白截取）', key='beat-excerpt-'+eid+'-'+str(expected))
            if st.form_submit_button('保存吸引力方案'):
                try:
                    edited = copy.deepcopy(plan)
                    edited.update(title=title, description=description)
                    for beat, row in zip(edited['visual_beats'], rows):
                        beat.update(visual=row['画面'], purpose=row['作用'], image_id=row['底图'] or '')
                        beat['framing'] = row['构图']
                        if row['构图'] != '保留当前安排':
                            beat['motion'] = A.framing_motion(row['构图'])
                    if selected or excerpt.strip():
                        quote = excerpt.strip() or selected
                        if selected and quote not in selected:
                            raise ValueError('镜头原句须出自选中的旁白')
                        A.locate(document, quote)
                        if any(b['quote']==quote for b in edited['visual_beats']):
                            raise ValueError('该原句已有镜头节点')
                        edited['visual_beats'].append(dict(quote=quote, visual='', purpose='', image_id='', framing='保留当前安排'))
                    edited['cover'].update(text=cover, composition=composition)
                    A.save(eid, edited, document, expected)
                    st.rerun()
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    st.error(str(exc))
        for note in A.notes(plan):
            st.caption(note)
        st.write('开场安排（目标节奏，具体切点沿用录音对齐结果）')
        for row in plan.get('opening', []):
            st.write(row)
        if plan.get('acts'):
            st.dataframe(plan['acts'], hide_index=True)
        st.caption(plan.get('ai_disclosure', ''))
        st.download_button('下载标题、封面和镜头方案', json.dumps(plan, ensure_ascii=False, indent=2),
                           file_name=eid+'-attraction.json', mime='application/json', key='attraction-download-'+eid)
        if st.button('准备已有素材的导演文件', key='attraction-compile-'+eid):
            try:
                from app import paths
                read = lambda p: json.loads(p.read_text('utf-8'))
                from pathlib import Path
                run = read(paths.episode_dir(eid)/'production/v2/run.json')
                manifest = read(Path(run['manifest_path']))
                evidence = read(Path(run['alignment_path']))
                result = A.compile_direction(plan, document, manifest, evidence, run.get('image_assets', {}))
                st.session_state['attraction-export-'+eid] = (fingerprint(plan),result)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                st.info('暂时保留画面方案。已有素材或对齐信息尚不完整：'+str(exc))
        cached = st.session_state.get('attraction-export-'+eid)
        if cached and cached[0] == fingerprint(plan):
            st.download_button('下载 directing.json 草稿', json.dumps(cached[1], ensure_ascii=False, indent=2),
                               file_name='directing.json', mime='application/json')
            st.caption('兼容现有导演入口；没有覆盖当前导演文件或启动制作。')
