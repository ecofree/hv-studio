"""End-to-end authoring of reusable historical maps and source-bound table drafts."""
import copy
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.services import graphic_extraction as E, graphic_ocr as O, historical_atlas as A
from app.services import studio_graphics as G, studio_workflow as W, studio_workspace as S
from app.services.spec import VideoFormat

pytestmark=pytest.mark.production


def document(*texts):
    return dict(units=[dict(unit_id=f'unit-{i:03}',source_text=t) for i,t in enumerate(texts)])


def recommendation(table,doc,**kw):
    return E.propose(table,doc,list(range(len(table['rows']))),origin='截图',sha='a'*64,**kw)


def test_extract_tables_prose_and_original_quotes():
    text='|仓名|仓窖（座）|史料出处|\n|---|---|---|\n|甲仓|1,000|《甲录》|\n|乙仓|1.2万|《乙录》|'
    doc=document('《甲录》记甲仓有一千座。','《乙录》记乙仓有12,000座。')
    table,prose=E.from_script(text,doc)
    draft=recommendation(table,doc)
    assert draft['kind']=='bars' and draft['unit']=='座'
    assert [r[1] for r in draft['rows']]==['1000','12000.0']
    assert [r[-1] for r in draft['evidence']['rows']]==['《甲录》','《乙录》']
    assert all('书名一致' in c and '数值及单位在原句中对应' in c for c in draft['evidence']['checks'])
    assert G.intervals({'one':draft},doc)[0][3][0]==0
    assert prose['matches'][1]['quote']=='《乙录》记乙仓有12,000座'
    assert E.scalar('二两')==(2,'两') and E.scalar('万平方米') is None
    assert E.scalar('2','面积（万平方米）')==(20000,'平方米')


def test_mixed_meanings_conflicts_and_repeated_quotes_stay_for_review():
    doc=document('甲仓有1200座。乙仓有240座。乙仓有240座。')
    base=E._item(['名称','数量（座）','出处'],[['甲仓','120','《甲录》'],['乙仓','240','《乙录》']])
    proposed=recommendation(base,doc)
    assert proposed['evidence']['links']==[None,None]
    assert '1200座' in proposed['evidence']['checks'][0]
    for a,b in [('50–55万斤','4亿斤'),('约700座','160座'),('44.3万丁','164.15万口'),
                ('4斤2两','2斤'),('10万钱/匹','1万钱/头'),('50%–75%','25%')]:
        sheet=E._item(['项目','规格','出处'],[['甲项',a,'《甲录》'],['乙项',b,'《乙录》']])
        assert recommendation(sheet,doc)['kind']=='table'
    extra=E._item(['项目','重量','社会表现','考古来源'],[['甲','4斤','物价平稳','文献'],['乙','2斤','价格波动','考古']])
    assert recommendation(extra,doc)['kind']=='table'
    denominator=E._item(['项目','重量'],[['每千枚','4斤'],['每百枚','2斤']])
    assert recommendation(denominator,doc)['kind']=='table'
    different=recommendation(base,document('《丙录》记甲仓有120座。乙仓有240座。'))
    assert '书名有差异' in different['evidence']['checks'][0]


def test_script_designed_table_never_enters_spoken_text_or_caption_clock(tmp_path,tmp_episodes):
    from app.managers import load
    from app.services import graphics_timeline as T, narration_pipeline as N, script_handoff as H
    from app.services.script import strip_script_markdown
    first='钱的分量变了。'
    last='百姓感受最深。'
    source=first+'\n\n【上屏表格：钱币与流通】\n项目\t重量\t史料出处\n开皇五铢\t4斤2两\t《甲录》\n私铸钱\t2斤\t《乙录》\n\n'+last
    revision=H.write_revision(source,tmp_path/'revisions',allow_plain=True)
    assert H.verify_revision(revision)['schema_version']==H.PLAIN_TABLE_VERSION
    plan=N.read(N.prepare(revision/'narration.md',tmp_path/'plans'))
    assert [u['approved_spoken_text'] for u in plan['units']]==[first,last]
    assert strip_script_markdown(source)==first+'\n\n'+last
    eid='visual-only-table'
    S.create_episode(eid,'钱的分量',source)
    doc=W.document(eid)
    material=E.script_input(eid)
    sheet,=E.from_script(material,doc)
    spec=E.propose(sheet,doc,[0,1],origin='剧本',sha=hashlib.sha256(material.encode()).hexdigest())
    assert spec['first_unit']==spec['last_unit']==doc['units'][0]['unit_id']
    assert (spec['timing'],spec['quotes'],spec['start_quote'],spec['evidence']['links'])==('span',[],'',[None,None])
    G.save(eid,'currency',spec,doc,None)
    p,rev=G.snapshot(eid)
    plates=p['graphics']['currency']['plates']
    assert len(plates['landscape'])==1 and len(plates['portrait'])==2
    assert set(T.local_jobs(load(eid),doc,{},{}))=={'unit-000'}
    cues=[dict(unit_id=u['unit_id'],text=u['source_text'],sample_start=i*96000,sample_end=(i+1)*96000,
              text_start=0,text_end=len(u['source_text'])) for i,u in enumerate(doc['units'])]
    manifest=dict(samples=192000,sample_rate=24000,alignment=cues)
    clock={'words':{c['unit_id']:[c] for c in cues}}
    for fmt,starts in [('landscape',[0,96000]),('portrait',[0,48000,96000])]:
        t=dict(inputs={str(G.path_for(eid).resolve()):rev},video_format=fmt,subtitles=copy.deepcopy(cues),
               shots=[dict(id='base',image_id='other',sample_start=0,sample_end=192000)])
        T.apply(eid,manifest,clock,t)
        assert [s['sample_start'] for s in t['shots']]==starts and t['subtitles']==cues
    # Alter just the authored table. Narration stays identical, but old numbers cannot ship.
    Path(load(eid).script_path).write_text(material.replace('4斤2两','3斤'),encoding='utf-8')
    assert G.source_key(W.document(eid))==G.source_key(doc)
    with pytest.raises(ValueError,match='剧本资料已变化'): T.check_current(eid,t)
    with pytest.raises(ValueError,match='剧本资料已变化'): G.current(eid,doc)
    # A previously imported TSV can be verified under the rules used when it was authored.
    prior=H.derive('旁白。\n\n地点\t数量\n甲地\t120座',version=H.PLAIN_VERSION)
    assert '120座' in prior['spoken.txt'].decode()


def test_frozen_authoring_includes_visual_appendix_and_ignores_external_updates(tmp_episodes,tmp_path):
    from app.services import studio_sources as sources
    source=tmp_path/'authoring.md'
    source.write_text('### 【第一幕：钱的分量】\n\n百姓先感到钱变轻了。\n\n### 本集完成度与质量审查指标\n\n'
                      '【上屏表格：钱币】\n|币种|重量|史料出处|\n|---|---|---|\n|甲币|四斤|《甲录》|\n|乙币|二斤|《乙录》|',encoding='utf-8')
    # The fixture uses an explicit source directory in place of the project library.
    from unittest.mock import patch
    with patch('app.services.studio_workspace.library_path',side_effect=lambda p:Path(p)):
        sources.import_authoring('frozen-display','钱币',source)
    doc=W.document('frozen-display')
    material=E.script_input('frozen-display')
    sheet,=E.from_script(material,doc)
    assert sheet['title']=='钱币' and len(doc['units'])==1 and '四斤' not in doc['units'][0]['source_text']
    source.write_text('作者外部草稿的新版本',encoding='utf-8')
    assert E.script_input('frozen-display')==material
    assert all(json.loads((Path(sources.contract('frozen-display')['revision'])/'document.json').read_bytes())['units'])


def test_stacked_screenshot_wrap_and_editorial_correction():
    gray=np.full((440,860),250,dtype=np.uint8)
    words=[]
    for offset in (0,215):
        for x in (210,520): gray[offset+10:offset+210,x:x+2]=15
        for y,row in [(26,['项目','数量（座）','史料出处']),(85,['甲仓','12O','第一本']),
                      (115,['','','续行']),(159,['乙仓','240','第二本'])]:
            for x,value in zip([25,245,555],row):
                if value: words.append(dict(text=value,box=[x,y+offset,x+len(value)*15,y+offset+19]))
    sections=O.table_sections(gray,words)
    assert len(sections)==2
    first,second=E.from_ocr(dict(sections=sections))
    assert first['rows'][0]==['甲仓','12O','第一本续行']
    assert second['boxes'][0][1]==[245,300,290,319]
    assert recommendation(first,document('甲仓120座，乙仓240座。'))['kind']=='table'
    grid='项目\t数量（座）\t史料出处\n甲仓\t120\t第一本续行\n乙仓\t240\t第二本'
    revised=E.correct_sheet(first,grid)
    result=recommendation(revised,document('甲仓120座，乙仓240座。'))
    assert result['kind']=='bars' and result['evidence']['raw_rows'][0][1]=='12O'
    assert result['evidence']['boxes'][0][2][3]==134
    with pytest.raises(ValueError,match='行列数量'): E.correct_sheet(first,grid.splitlines()[0])


def test_shared_map_roundtrip_staging_and_frozen_episode_copy(tmp_episodes):
    story=dict(title='唐末两京',source='《资治通鉴》卷二五四',rows=copy.deepcopy(A.EVENTS))
    with Image.new('RGB',(80,50),'#825c43') as image:
        template=A.keep(A.TANG,image,dict(story,first_unit='other',approved=True))
        atlas,png,restored=A.unpack(template.read_bytes())
        assert restored==story and png.startswith(b'\x89PNG') and A.templates()==[template]
        assert A.keep(atlas,image,story)==template
        assert A.keep(atlas,image)!=template
    assert A.unpack(A.bundle(atlas,None))==(atlas,None,None)
    broken=copy.deepcopy(story)
    broken['rows'][0][1]='未知地点'
    with pytest.raises(ValueError,match='底图里没有'): A.bundle(atlas,None,broken)
    with io.BytesIO() as buf:
        with zipfile.ZipFile(buf,'w') as z: z.writestr('../atlas.json','{}')
        with pytest.raises(ValueError): A.unpack(buf.getvalue())
    for eid in ('atlas-fixture-first','atlas-fixture-second'):
        S.create_episode(eid,'地图测试','黄巢军进入东都。双方在潼关交战。黄巢军进入长安。')
        doc=W.document(eid)
        spec=dict(restored,kind='campaign',atlas=atlas,quotes=['双方在潼关','黄巢军进入长安'],
                  first_unit=doc['units'][0]['unit_id'],last_unit=doc['units'][-1]['unit_id'])
        G.save(eid,'map',spec,doc,None,upload=png)
        entry=G.current(eid,doc)[0]['graphics']['map']
        assert entry['rows']==story['rows'] and entry['bound_to']==G.source_key(doc)
        for fmt in VideoFormat:
            hashes=entry['plates'][fmt.value]
            assert len(set(hashes))==3
            with Image.open(G.media_path(eid,hashes[1],'graphics-plates')) as plate: assert plate.size==fmt.size
    assert all(p.is_file() for p in A.templates())


def test_native_ocr_and_exact_saved_screenshot(tmp_episodes):
    if os.name!='nt': pytest.skip('Windows OCR provider integration')
    from app.services.compose import _get_font
    with Image.new('RGB',(1050,455),'#181818') as image:
        d=ImageDraw.Draw(image)
        for x in [300,640]: d.line((x,38,x,417),fill='#ddd',width=2)
        for y,row in zip([54,146,238,330],[['地点','仓窖数量（座）','来源'],['甲仓','120','示例记录甲'],
                                           ['乙仓','240','示例记录乙'],['丙仓','360','示例记录丙']]):
            for x,value in zip([25,335,675],row): d.text((x,y),value,font=_get_font(33),fill='white',anchor='lt')
        buf=io.BytesIO()
        image.save(buf,format='PNG')
    raw=buf.getvalue()
    try: scan=O.read_image(raw)
    except ValueError as exc:
        if 'language unavailable' in str(exc): pytest.skip(str(exc))
        raise
    sheet,=E.from_ocr(scan)
    assert [r[1] for r in sheet['rows']]==['120','240','360']
    assert sheet['boxes'][1][1][1]>=230
    eid='original-ocr-fixture'
    S.create_episode(eid,'仓窖测试','甲仓有120座，乙仓有240座，丙仓有360座。')
    doc=W.document(eid)
    sha=hashlib.sha256(raw).hexdigest()
    draft=E.propose(sheet,doc,[0,1,2],origin='截图',sha=sha,engine=scan['engine'])
    assert draft['kind']=='bars' and all(draft['evidence']['links'])
    with pytest.raises(ValueError,match='已更换'): G.save(eid,'test',draft,doc,None,evidence_image=raw+b'x')
    assert G.snapshot(eid)==(None,None)
    G.save(eid,'test',draft,doc,None,evidence_image=raw)
    assert E.original_path(eid,sha).read_bytes()==raw
    E.original_path(eid,sha).write_bytes(b'edited')
    with pytest.raises(ValueError,match='原始截图'): G.save(eid,'test',draft,doc,G.snapshot(eid)[1])


def test_production_ui_extract_and_reuse_map(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    eid='historical-ui'
    S.create_episode(eid,'资料编辑','据《仓录》甲仓仓窖有120座，据《仓录》乙仓仓窖有240座。')
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=35).run()
    app.switch_page('app_pages/production.py').run()
    app.checkbox(key='graphics-open-'+eid).check().run()
    def widget(kind,label): return next(w for w in getattr(app,kind) if w.label==label)
    widget('checkbox','从剧本或截图提取资料').check().run()
    widget('button','提取资料').click().run()
    widget('button','将所选资料填入图文编辑器').click().run()
    assert widget('selectbox','画面类型').value=='bars'
    widget('button','保存并用于本集').click().run()
    assert not app.exception and G.snapshot(eid)[0], [w.value for w in app.error]
    saved=list(G.snapshot(eid)[0]['graphics'].values())
    assert len(saved)==1 and saved[0]['evidence']['origin']=='剧本'
    widget('selectbox','画面类型').select('campaign').run()
    assert '潼关' in widget('text_area','数据行').value
    widget('button','存入地图模板库').click().run()
    assert len(A.templates())==1 and not app.exception
    widget('button','预览这一条').click().run()
    assert app.get('image') and not app.exception, [w.value for w in app.error]
