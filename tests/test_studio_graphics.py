"""Offline graphic editing, image budgets and spoken-word cuts through the v2 entry points."""
import io
from pathlib import Path

import pytest
from PIL import Image

from app import paths
from app.managers import load
from app.services import graphics_timeline as T, studio_graphics as G
from app.services import studio_workspace as S, studio_workflow as W
from app.services.provenance import atomic_json
from app.services.spec import VideoFormat

pytestmark = pytest.mark.production


def episode(eid='graphics-fixture'):
    S.create_episode(eid,'资料测试','开皇每千枚重四斤二两，后来有二斤，也见一斤。\n\n回到都城。')
    return eid,W.document(eid)


def bars(doc):
    uid = doc['units'][0]['unit_id']
    return dict(kind='bars',title='每千枚钱重',source='用户示例 · 待核',unit='两 / 千枚',
                rows=[['开皇','66','四斤二两'],['私铸','32','二斤'],['更轻者','16','一斤']],
                first_unit=uid,last_unit=uid,quotes=['后来','也见'])


def test_save_both_formats_without_image_jobs_and_rebind(tmp_episodes,monkeypatch):
    from app.services.production_inputs import input_files,prepare
    eid,doc = episode()
    ep = load(eid)
    s = bars(doc)
    initial = input_files(eid)
    G.save(eid,'weights',s,doc,None)
    plan,rev = G.current(eid,doc)
    entry = plan['graphics']['weights']
    for fmt in VideoFormat:
        stages = entry['plates'][fmt.value]
        assert len(set(stages))==3
        for sha in stages:
            with Image.open(T.plate(eid,sha)) as im:
                assert im.size==fmt.size
    with monkeypatch.context() as patch:
        patch.setattr(G,'render_plate',lambda *a:pytest.fail('changing only word anchors must reuse the plates'))
        G.save(eid,'weights',dict(s,quotes=['后来有','也见一斤']),doc,rev)
    revised,rev = G.current(eid,doc)
    assert revised['graphics']['weights']['plates']==entry['plates']
    root = paths.episode_dir(eid)/'production/v2'
    jobs,scene = prepare(ep,doc,root,preview=True)
    assert [(j.id,getattr(j,'imported',False)) for j in jobs]==[('unit-000',True),('unit-001',False)]
    assert jobs[0].is_done() and not scene.exists()
    # Authored local scenes need neither an existing drawing nor a generation brief.
    directory = paths.episode_dir(eid)
    atomic_json(directory/'scene_plan.json',dict(unit_scenes=[dict(unit_id=u['unit_id'],image=k)
                for u,k in zip(doc['units'],['coin','city'])],images={'coin':{'local_only':True}}))
    atomic_json(directory/'image_prompts.json',dict(coin={'en':'intentionally unused'},
                city={'en':'A stone city gate rises above the quiet river outside the ancient capital.'}))
    jobs,_ = prepare(ep,doc,root,preview=True)
    assert {j.id for j in jobs if getattr(j,'imported',False)}=={'coin'}
    repeated = {'unit_scenes':[dict(image='same',unit_id=u['unit_id']) for u in doc['units']]}
    assert T.local_jobs(ep,doc,repeated,{})=={}
    directed = dict(source_identity=G.source_key(doc),visual_events=[
        dict(image_id='only-coins',at={'at':'start'}),
        dict(image_id='city',at={'unit_id':doc['units'][1]['unit_id'],'text_start':0})])
    assert set(T.local_jobs(ep,doc,repeated,directed))=={'only-coins','same'}
    with pytest.raises(ValueError,match='已更新'):
        G.save(eid,'weights',s,doc,None)
    with pytest.raises(ValueError,match='重叠'):
        G.save(eid,'overlap',s,doc,rev)
    assert G.snapshot(eid)[1]==rev
    T.check_current(eid,{'inputs':{str(G.path_for(eid).resolve()):rev}})
    with pytest.raises(ValueError,match='图文已变化'):
        T.check_current(eid,{'inputs':{}})
    G.save(eid,'weights',None,doc,rev)
    assert G.snapshot(eid)==(None,None)
    assert set(input_files(eid))==set(initial)


def test_partial_scene_preserves_camera_clock_and_uses_observed_words(tmp_episodes):
    eid,doc = episode()
    s = bars(doc)
    s.update(rows=s['rows'][:2],start_quote='每千枚',stop_before='也见',quotes=['后来'])
    G.save(eid,'weights',s,doc,None)
    assert T.local_jobs(load(eid),doc,{},{})=={}
    u = doc['units'][0]
    text = u['source_text']
    ids = [x['unit_id'] for x in doc['units']]
    m = dict(samples=10000,sample_rate=1000,alignment=[dict(unit_id=v['unit_id'],text=v['source_text'],
             sample_start=a,sample_end=b) for v,a,b in zip(doc['units'],[0,8000],[8000,10000])])
    times = [(0,0),(text.index('每千枚'),1200),(text.index('，'),2900),
             (text.index('后来'),3400),(text.index('也见'),6100)]
    evidence = {'words':{ids[0]:[dict(text_start=i,sample_start=t) for i,t in times],
                         ids[1]:[dict(text_start=0,sample_start=8000)]}}
    original = dict(id='scene',image_id='background',image_path='art.png',sample_start=0,sample_end=10000)
    def new():
        return dict(shots=[original],subtitles=[],inputs={str(G.path_for(eid).resolve()):G.snapshot(eid)[1]})
    t = new()
    T.apply(eid,m,evidence,t)
    assert [(r['sample_start'],r['sample_end']) for r in t['shots']]==[(0,1200),(1200,3400),(3400,6100),(6100,10000)]
    assert t['shots'][0]['camera_samples']==t['shots'][-1]['camera_samples']==[0,10000]
    assert [q['graphic']['step'] for q in t['shots'][1:3]]==[1,2]
    del evidence['words'][ids[0]][3]
    with pytest.raises(ValueError,match='实测词首'):
        T.apply(eid,m,evidence,new())


def test_dense_table_import_profile_portrait_and_route_geometry(tmp_episodes):
    table = G.parse_grid('|阶段|规格|出处|\n|:---|---:|---|\n|开皇|每千枚重四斤二两|隋书·食货志|')
    assert table[1][1]=='每千枚重四斤二两'
    assert G.parse_grid('说明,原文\n数量,"1,000"')[1][1]=='1,000'
    with pytest.raises(ValueError,match='非负数'):
        G.validate_spec(dict(kind='bars',title='重量',unit='两',source='示例',
                             rows=[['甲','NaN']],quotes=[]))
    eid,doc = episode()
    profile = dict(bars(doc),kind='person',title='人物示例',rows=[['身份','资料整理员']],quotes=[])
    blob = io.BytesIO()
    with Image.new('RGB',(320,460),'#e91a7c') as portrait:
        portrait.save(blob,format='PNG')
    G.save(eid,'person',profile,doc,None,upload=blob.getvalue())
    entry = G.snapshot(eid)[0]['graphics']['person']
    assert G.media_path(eid,entry['media']).is_file()
    for fmt in VideoFormat:
        with Image.open(T.plate(eid,entry['plates'][fmt.value][0])) as image:
            from app.services.information_design import HEADER_END
            assert image.getpixel((212,HEADER_END[fmt]+180))==(233,26,124)
    route = dict(title='历史路线',kind='route',rows=[['甲','.2','.5','起点'],['乙','.8','.5','第二站']],source='示意')
    with G.render_plate(route,0,VideoFormat.LANDSCAPE) as before,G.render_plate(route,1,VideoFormat.LANDSCAPE) as after:
        # The second segment crosses this point only after the corresponding quote.
        assert before.getpixel((627,521))!=after.getpixel((627,521))


def test_production_page_preview_save_and_remove(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    eid,doc = episode()
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=30).run()
    app.switch_page('app_pages/production.py').run()
    app.checkbox(key='graphics-open-'+eid).check().run()
    def widget(group,label):
        return next(w for w in getattr(app,group) if w.label==label)
    widget('text_input','上屏标题或人物姓名').set_value('钱重')
    widget('text_input','上屏资料出处').set_value('用户表一')
    widget('text_area','数据行').set_value('阶段\t每千枚重量\n开皇\t四斤二两\n私铸\t二斤')
    widget('text_area','从第 2 条开始，每条对应一句旁白原文，按出现顺序每行一句').set_value('后来')
    widget('button','预览这一条').click().run()
    assert not app.exception and not G.path_for(eid).exists()
    assert app.get('image'), [x.value for x in app.error]
    widget('button','保存并用于本集').click().run()
    assert not app.exception
    assert G.path_for(eid).is_file(), [x.value for x in app.error]
    key, = G.snapshot(eid)[0]['graphics']
    widget('selectbox','本集图文').select(key).run()
    widget('button','移除此图文').click().run()
    assert not app.exception and G.snapshot(eid)==(None,None)


@pytest.mark.NEEDS_FFMPEG
def test_saved_graphics_reach_both_decoded_videos(tmp_path,tmp_episodes,monkeypatch):
    import subprocess
    from app.services import production_inputs,production_timeline,production_v2
    from app.services.narration_pipeline import read
    from app.services.provenance import file_sha256,fingerprint
    from test_production_v2 import assembly
    eid = 'graphics-encoded'
    manifest,plan,*_ = assembly(tmp_path,samples=24000)
    S.create_episode(eid,'资料片段',(tmp_path/'script.md').read_text('utf-8'))
    doc = W.document(eid)
    s = dict(bars(doc),first_unit=doc['units'][0]['unit_id'],last_unit=doc['units'][1]['unit_id'],
             rows=[['第一段','66'],['第二段','32']],quotes=[doc['units'][1]['source_text']])
    G.save(eid,'series',s,doc,None)
    m = read(manifest)
    units = m['alignment']
    cues = [dict(unit_id=u['unit_id'],sample_start=u['sample_start'],sample_end=u['sample_end'],
                 text=u['text'],text_start=0,text_end=len(u['text'])) for u in units]
    evidence = tmp_path/'test-clock.json'
    atomic_json(evidence,dict(method='acoustic_source_alignment_v1',status='machine_validated',
                manifest_content_sha256=fingerprint(m),inputs={str(manifest):file_sha256(manifest)},
                subtitles=cues,words={r['unit_id']:[r] for r in cues}))
    monkeypatch.setattr('app.services.bgm.pick_bgm',lambda _:None)
    for fmt in VideoFormat:
        ep = load(eid).model_copy(update={'video_format':fmt})
        jobs,scene = production_inputs.prepare(ep,plan,tmp_path/'slate')
        assert all(isinstance(j,T.GraphicPlate) and j.is_done() for j in jobs)
        path = production_timeline.build(ep,manifest,evidence,{j.id:str(j.out) for j in jobs},scene,tmp_path/'timelines')
        timeline = read(path)
        assert timeline['subtitles']==cues and [s['sample_start'] for s in timeline['shots']]==[0,24000]
        receipt = production_v2.render(manifest,path,tmp_path/'renders')
        result = read(receipt)
        v = next(s for s in result['integrity']['streams'] if s['codec_type']=='video')
        assert (v['width'],v['height'],int(v['nb_read_packets']))==(*fmt.size,50)
        # Decode a focus-colored chart bar in the second stage, beyond its dissolve.
        raw = subprocess.check_output(['ffmpeg','-v','error','-ss','1.56','-i',result['candidate_path'],
                                      '-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        y = (632 if fmt is VideoFormat.LANDSCAPE else 1000)
        with Image.frombytes('RGB',fmt.size,raw) as im,Image.open(timeline['shots'][1]['image_path']) as plate:
            x = 600 if fmt is VideoFormat.LANDSCAPE else 110
            # Search the expected bar in its column using the source PNG's known accent.
            px = next((x,k) for k in range(y-100,y+180) if plate.getpixel((x,k))==(244,186,113))
            assert max(abs(a-b) for a,b in zip(im.getpixel((px[0],px[1]+5)),(244,186,113)))<18
        assert production_v2.render(manifest,path,tmp_path/'renders')==receipt
