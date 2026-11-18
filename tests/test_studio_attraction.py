import copy
import json
import pytest
from app.services import studio_attraction as A
from app.services.script_document import parse_document

pytestmark = pytest.mark.production


def fixture_plan():
    doc = parse_document('城门紧闭。\n\n将军等待。')
    plan = A.empty(doc)
    plan['title'] = '为什么迟迟不出战？'
    plan['cover']['text'] = '为何不战'
    plan['visual_beats'] = [dict(quote=u['source_text'], image_id='old', purpose='观察局势') for u in doc['units']]
    manifest = dict(sample_rate=24000, samples=240000, alignment=[
        dict(unit_id=u['unit_id'], text=u['source_text'], sample_start=i*120000, sample_end=(i+1)*120000)
        for i,u in enumerate(doc['units'])])
    evidence = {'words':{u['unit_id']:[dict(text_start=0, sample_start=i*120000)] for i,u in enumerate(doc['units'])}}
    return doc, plan, manifest, evidence


def test_save_does_not_change_production_inputs_or_approvals(tmp_episodes):
    from app.services import studio_workspace as S, studio_workflow as W
    from app.services.production_inputs import input_files
    eid = 'attraction-isolated'
    S.create_episode(eid, '测试', '城门紧闭。\n\n将军等待。', scope='full')
    doc = W.document(eid); plan = A.empty(doc)
    original = W.load(eid); inputs = input_files(eid); identity = W.identity(eid)
    p = A.save(eid, plan, doc, None)
    plan['title'] = '为何不出战'
    A.save(eid, plan, doc, A.file_sha256(p))
    assert W.load(eid) == original and input_files(eid) == inputs and W.identity(eid) == identity
    assert len(list((p.parent/'attraction-history').glob('*.json'))) == 1
    with pytest.raises(ValueError, match='刷新'):
        A.save(eid, plan, doc, None)


def test_export_reuses_images_clock_and_existing_directing_path(tmp_path, monkeypatch):
    from app.services import directing
    doc, plan, manifest, evidence = fixture_plan()
    plate=tmp_path/'old.png'; plate.write_bytes(b'preserved asset')
    original = copy.deepcopy((plan, manifest, evidence))
    design = A.compile_direction(plan, doc, manifest, evidence, {'old':str(plate)})
    assert design['visual_events'][0]['at'] == {'at':'start'}
    assert design['cards'][0]['card']['fade_in_ms'] == 0
    design_path=tmp_path/'directing.json';design_path.write_text(json.dumps(design),encoding='utf-8')
    ev=tmp_path/'evidence.json';ev.write_text(json.dumps(evidence),encoding='utf-8')
    monkeypatch.setattr('app.services.forced_alignment.require_evidence', lambda *args:None)
    timeline=directing.apply(manifest, {'alignment_evidence_path':str(ev)}, design_path, {'old':str(plate)}, tmp_path/'compiled')
    assert [(s['sample_start'],s['sample_end']) for s in timeline['shots']] == [(0,120000),(120000,240000)]
    assert (plan,manifest,evidence) == original and plate.read_bytes()==b'preserved asset'


@pytest.mark.parametrize('fault', ['stale','missing','unobserved','reorder','ambiguous'])
def test_unsafe_export_does_not_invent_timing(tmp_path, fault):
    doc, plan, manifest, evidence = fixture_plan()
    plate=tmp_path/'old.png';plate.write_bytes(b'asset');assets={'old':str(plate)}
    if fault=='stale':plan['source_identity']='old'
    if fault=='missing':assets={}
    if fault=='unobserved':evidence['words'][doc['units'][1]['unit_id']]=[]
    if fault=='reorder':plan['visual_beats'].reverse()
    if fault=='ambiguous':plan['visual_beats'][1]['quote']='。'
    with pytest.raises(ValueError):A.compile_direction(plan,doc,manifest,evidence,assets)


def test_rhythm_reports_reuse_and_never_blocks():
    timeline={'shots':[dict(sample_start=0,sample_end=6,image_path='A'),dict(sample_start=6,sample_end=18,image_path='A'),
                       dict(sample_start=18,sample_end=99,image_path='B')]}
    before=copy.deepcopy(timeline)
    points=A.rhythm_points(timeline,1)
    assert len(points)==2 and '2 个镜头' in points[0]['text']
    assert all(p['advisory_only'] for p in points) and timeline==before
    from app.services.production_diagnostics import review_points
    merged=review_points(dict(attraction_points=points, shots=[dict(start_seconds=0,
        duration_seconds=18,review_long_hold=False,narration=['原文'],image_path='A')]))
    assert sum(p['start_seconds']==0 for p in merged)==1


def test_zero_frame_check_rejects_transparent_title(tmp_path):
    from app.services import overlay as O, overlay_author
    p=tmp_path/'cards.json'
    spec=O.Spec(kind=O.KIND_TITLE,start_ms=0,end_ms=3000,title='测试')
    p.write_text(json.dumps({'version':1,'overlays':[spec.payload()]}),encoding='utf-8')
    with pytest.raises(ValueError,match='完整可见'):overlay_author.check_sidecar(p)
    from dataclasses import replace
    spec=replace(spec,fade_in_ms=0)
    p.write_text(json.dumps({'version':1,'overlays':[spec.payload()]}),encoding='utf-8')
    overlay_author.check_sidecar(p)


def test_panel_offline_save_and_download(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    from app.services import studio_workspace as S, studio_workflow as W
    eid='attraction-ui';S.create_episode(eid,'测试','城门紧闭。',scope='full')
    before=W.load(eid)
    doc=W.document(eid);plan=A.empty(doc)
    plan['visual_beats']=[dict(quote='城门紧闭',visual='营门与人物',image_id='',purpose='建立疑问',framing='全景')]
    A.save(eid,plan,doc,None)
    code="from app.services.studio_workflow import document\nfrom app.webui.attraction_panel import render\nrender('attraction-ui',document('attraction-ui'))"
    app=AppTest.from_string(code,default_timeout=20).run()
    assert not app.exception
    next(x for x in app.text_input if x.label=='发布标题').set_value('为何不战？')
    next(x for x in app.button if x.label=='保存吸引力方案').click().run()
    assert not app.exception and A.load(eid)['title']=='为何不战？' and W.load(eid)==before
