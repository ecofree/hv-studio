"""Source-backed maps use the same portable package and spoken clock as other graphics."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image
import pytest

from app.services import atlas_terrain as terrain
from app.services import graphics_timeline as T
from app.services import historical_atlas as A
from app.services import studio_graphics as G, studio_workflow as W, studio_workspace as S
from app.services.spec import VideoFormat

pytestmark = pytest.mark.production
BOUNDS = [108.50,33.90,109.25,34.65]
SITES = [
    ['西城','108.58','34.04','城址','测试年代','西城遗址定位',''],
    ['东关','109.16','34.56','关隘','测试年代','东关文献定位',''],
    ['横山','108.76','34.02','山地','现代','测试高程',''],
]
STORY = dict(title='源地形示例',source='史料举例',
    rows=[['东关','东关','','甲方','','先标东关。','第一日','史料卷一'],
          ['西城','西城','东关','甲方','乙方','位置切换至西城。','第二日','史料卷二']])
WORDING = ['先标东关。','位置切换至西城。']


@pytest.fixture
def raw_terrain():
    keys = terrain.tile_keys(BOUNDS,9)
    assert len(keys)>1
    altitude = 200+np.arange(256)[None,:]//2+np.arange(256)[:,None]*3
    raw = altitude+32768
    rgb = np.stack((raw//256,raw%256,np.zeros_like(raw)),axis=-1).astype('uint8')
    with Image.fromarray(rgb) as tile, io.BytesIO() as png, io.BytesIO() as packet:
        tile.save(png,format='PNG')
        with zipfile.ZipFile(packet,'w') as z:
            for key in keys: z.writestr('%d/%d/%d.png'%key,png.getvalue())
        rivers = json.dumps(dict(type='FeatureCollection',features=[
            dict(type='Feature',properties={'scalerank':3},
                 geometry=dict(type='LineString',coordinates=[[108.49,34.3],[108.92,34.31],[109.26,34.33]])),
            dict(type='Feature',properties={},geometry=dict(type='MultiLineString',coordinates=[[[112,32],[115,34]]]))])).encode()
        return packet.getvalue(),rivers


def atlas_from(raw_terrain):
    png,geo = terrain.build(BOUNDS,*raw_terrain)
    atlas = dict(name='校验地图',period='测试年代',source=terrain.CREDITS,features=[],geography=geo,
                 sites=SITES,places=terrain.map_sites(SITES,BOUNDS))
    return png,atlas


def test_reference_package_rejects_offscreen_sites_and_swapped_raster(raw_terrain,tmp_episodes):
    png,atlas=atlas_from(raw_terrain)
    assert atlas['geography']['river_lines']==1
    assert atlas['geography']['terrain_sha256']==hashlib.sha256(raw_terrain[0]).hexdigest()
    assert float(atlas['places'][0][1])<float(atlas['places'][1][1])
    assert float(atlas['places'][0][2])>float(atlas['places'][1][2])
    with G.open_upload(png) as base:
        assert len(set(base.getdata()))>100 and (131,169,173) in base.getdata()
        first=A.keep(atlas,base,STORY)
        assert A.keep(atlas,base,STORY)==first and A.templates()==[first]
    base,data,story=A.unpack(first.read_bytes())
    assert base==atlas and story==STORY and data.startswith(b'\x89PNG')
    with pytest.raises(ValueError,match='范围外'):
        terrain.map_sites([['错位','34','108','城址','唐代','例','']],BOUNDS)
    with pytest.raises(ValueError,match='史料出处'):
        A.validate(atlas,[STORY['rows'][0][0:6]])
    with pytest.raises(ValueError,match='没有的地点'):
        A.validate(atlas,[[*STORY['rows'][0][:1],'横山',*STORY['rows'][0][2:]]])
    with zipfile.ZipFile(first) as original,io.BytesIO() as modified:
        assert json.loads(original.read('atlas.json'))['version']==2
        with zipfile.ZipFile(modified,'w') as z,Image.new('RGB',(800,600),'green') as fake,io.BytesIO() as b:
            fake.save(b,format='PNG')
            z.writestr('atlas.json',original.read('atlas.json'))
            z.writestr('base.png',b.getvalue())
        with pytest.raises(ValueError,match='校验记录不一致'):
            A.unpack(modified.getvalue())


def test_source_atlas_cuts_to_evidence_stages_from_words(raw_terrain,tmp_episodes):
    eid='source-atlas-clock'
    S.create_episode(eid,'源地形时钟','\n\n'.join(WORDING))
    doc=W.document(eid)
    first,last=[u['unit_id'] for u in doc['units']]
    raw,atlas=atlas_from(raw_terrain)
    spec=dict(STORY,kind='campaign',atlas=atlas,first_unit=first,last_unit=last,quotes=WORDING[1:])
    G.save(eid,'map',spec,doc,None,upload=raw)
    state,rev=G.snapshot(eid)
    assert state['graphics']['map']['atlas']==atlas
    cues=[dict(unit_id=u['unit_id'],text=u['source_text'],text_start=0,text_end=len(u['source_text']),
               sample_start=i*96000,sample_end=(i+1)*96000) for i,u in enumerate(doc['units'])]
    clock=dict(samples=192000,sample_rate=24000,alignment=cues)
    for fmt in VideoFormat:
        timeline=dict(video_format=fmt.value,inputs={str(G.path_for(eid).resolve()):rev},subtitles=cues,
                      shots=[dict(id='base',image_id='base',sample_start=0,sample_end=192000)])
        T.apply(eid,clock,dict(words={r['unit_id']:[r] for r in cues}),timeline)
        shots=timeline['shots']
        assert [row['sample_start'] for row in shots]==[0,96000]
        assert timeline['subtitles']==cues and [s['graphic']['step'] for s in shots]==[1,2]
        assert shots[0]['image_path']!=shots[1]['image_path']
        with Image.open(shots[1]['image_path']) as frame: assert frame.size==fmt.size


def test_invalid_or_missing_tiles_and_public_response_contract(raw_terrain,monkeypatch):
    import requests
    pack,rivers=raw_terrain
    with zipfile.ZipFile(io.BytesIO(pack)) as original,io.BytesIO() as output:
        items={name:original.read(name) for name in original.namelist()}
        with zipfile.ZipFile(output,'w') as z:
            z.writestr('0/0/0.png',next(iter(items.values())))
        with pytest.raises(ValueError,match='缩放级别'):
            terrain.build(BOUNDS,output.getvalue(),rivers)
    with io.BytesIO() as output,zipfile.ZipFile(output,'w') as z:
        key,content=next(iter(items.items()))
        z.writestr(key,content)
        z.close()
        with pytest.raises(ValueError,match='数据缺少'):
            terrain.build(BOUNDS,output.getvalue(),rivers)
    with pytest.raises(ValueError,match='GeoJSON'):
        terrain.build(BOUNDS,pack,b'{"type":"Polygon"}')

    class Response:
        def __init__(self,payload): self.payload=payload
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def raise_for_status(self): pass
        def iter_content(self,_): yield self.payload

    def get(url,*,stream,timeout):
        assert stream and timeout==(5,10)
        return Response(rivers if url==terrain.RIVERS_URL else items[url.removeprefix(terrain.TERRAIN_URL+'/')])

    monkeypatch.setattr(requests,'get',get)
    fetched,water=terrain.fetch_public(BOUNDS,9)
    assert water==rivers and terrain.build(BOUNDS,fetched,water)[0]==terrain.build(BOUNDS,pack,rivers)[0]


def test_studio_creates_georeferenced_campaign(raw_terrain,tmp_episodes,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app.webui.graphics_panel import _grid
    monkeypatch.setattr(terrain,'fetch_public',lambda bounds,zoom:raw_terrain)
    eid='source-atlas-editor'
    S.create_episode(eid,'地图编辑','\n\n'.join(WORDING))
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=35).run()
    app.switch_page('app_pages/production.py').run()
    app.checkbox(key='graphics-open-'+eid).check().run()
    def widget(kind,label):
        return next(w for w in getattr(app,kind) if w.label==label)
    widget('selectbox','画面类型').select('campaign').run()
    widget('selectbox','可复用底图').select('按经纬度生成地形底图').run()
    widget('text_input','底图经纬度范围（西、南、东、北）').set_value(','.join(map(str,BOUNDS)))
    widget('radio','地形和河网数据来源').set_value('下载公开数据').run()
    widget('number_input','高程瓦片缩放级别').set_value(9)
    widget('button','生成地形与河网底图').click().run()
    widget('text_input','底图名称').set_value('校验地图')
    widget('text_input','地图年代').set_value('测试年代')
    widget('text_area','有出处的地标（名称、经度、纬度、类型、年代、出处、说明）').set_value(_grid(SITES)).run()
    widget('text_input','上屏标题或人物姓名').set_value(STORY['title'])
    widget('text_input','上屏资料出处').set_value(STORY['source'])
    widget('text_area','数据行').set_value(_grid(STORY['rows']))
    last=W.document(eid)['units'][-1]['unit_id']
    widget('selectbox','结束旁白段').select(last)
    widget('text_area','从第 2 条开始，每条对应一句旁白原文，按出现顺序每行一句').set_value(WORDING[1])
    widget('button','预览这一条').click().run()
    assert not app.exception and app.get('image'), [v.value for v in app.error]
    widget('button','保存并用于本集').click().run()
    assert not app.exception and G.path_for(eid).is_file(), [v.value for v in app.error]
    entry=next(iter(G.snapshot(eid)[0]['graphics'].values()))
    assert entry['atlas']['sites']==SITES and entry['rows']==STORY['rows']
