"""Offline counterexamples: stale approvals, missing pilots, unsafe imports and data."""
import copy
from pathlib import Path
import pytest
from app.services import studio_workflow as W,studio_workspace as S
from app.services.provenance import atomic_json

pytestmark=pytest.mark.production

@pytest.fixture
def draft(tmp_episodes):
    eid='studio-fixture'
    S.create_episode(eid,'离线流程夹具','### 【第一幕：测试】\n\n这是离线验证用的正文。',scope='full')
    v=W.load(eid);u=W.document(eid)['units'][0]
    v['brief']={k:'离线验证' for k in W.BRIEF_FIELDS}
    baseline=tmp_episodes/'fixture-audio.wav';baseline.write_bytes(b'offline identity fixture')
    v['voice']=dict(identity='fixture-only',model='fixture-only',baseline=str(baseline),master='',delivery='fixture-only')
    v['claims']=[dict(id='c1',unit_id=u['unit_id'],quote=u['source_text'],type='艺术示意',source='offline fixture',locator='test:1',excerpt='fixture only',uncertainty='',visual_limits='不是真实历史材料')]
    v['beats']=[dict(id='b1',unit_id=u['unit_id'],text_start=0,purpose='解释测试',new_information='说明流程',claim_ids=['c1'],image_id='plate1',motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5]))]
    W.save(eid,v,expected_revision=v['revision'])
    return eid

def approve_editorial(eid):
    W.record_review(eid,'editorial','offline fixture','isolated test only','approved',expected_identity=W.identity(eid),checks={'facts_and_story':True})

def test_editorial_approval_never_implies_pilot_approval(draft):
    approve_editorial(draft)
    s=W.status(draft)
    assert s['editorial_approved'] and not s['pilot_approved'] and s['ready_for_generation']

def test_text_or_direction_edit_invalidates_exact_review(draft):
    approve_editorial(draft);v=W.load(draft)
    v['beats'][0]['new_information']='不同的创作意图'
    W.save(draft,v,expected_revision=v['revision'])
    assert W.review_valid(draft,'editorial')
    approve_editorial(draft)
    from app.managers import load
    source=Path(load(draft).script_path);source.write_text('### 【第一幕：测试】\n\n正文已发生改变。',encoding='utf-8')
    assert not W.review_valid(draft,'editorial')
    assert W.editorial_issues(draft)

def test_conflicting_save_preserves_latest_revision(draft):
    old=W.load(draft);v=copy.deepcopy(old);v['brief']['question']='新的问题'
    W.save(draft,v,expected_revision=v['revision'])
    with pytest.raises(ValueError,match='更新'):W.save(draft,old,expected_revision=old['revision'])
    assert W.load(draft)['brief']['question']=='新的问题'


def test_replacing_voice_baseline_invalidates_review(draft):
    approve_editorial(draft)
    old_voice=W.narration_identity(draft)
    baseline=Path(W.load(draft)['voice']['baseline'])
    baseline.write_bytes(b'a different recording at the same path')
    assert W.review_valid(draft,'editorial')
    assert W.narration_identity(draft)!=old_voice


def test_source_revision_can_rebind_without_stale_anchor_deadlock(draft):
    approve_editorial(draft)
    W.replace_source(draft,'### 【第一幕：新正文】\n\n一段完全不同的新正文。',expected_identity=W.identity(draft))
    result=W.rebind_current(draft,expected_identity=W.identity(draft))
    v=W.load(draft)
    assert result=={'claims':1,'beats':1} and v['claims']==[] and v['beats']==[]
    assert not W.review_valid(draft,'editorial')
    v['brief']['question']='新版问题';W.save(draft,v,expected_revision=v['revision'])
    assert len(list((W.folder(draft)/'revisions').glob('*.json')))>=3


def test_pilot_copies_exact_passage_without_full_master_or_approval(draft):
    approve_editorial(draft)
    child=S.create_pilot(draft,'pilot-fixture',0,1)
    assert W.document(child)['units'][0]['source_text']==W.document(draft)['units'][0]['source_text']
    v=W.load(child)
    assert v['scope']=='pilot' and not v['voice']['master'] and v['claims']
    assert not W.review_valid(child,'editorial') and not W.review_valid(child,'pilot')
    assert W.read(W.folder(child)/'parent.json')['parent_identity']==W.identity(draft)

def test_short_pilot_receipt_binds_exact_contiguous_parent_source(tmp_path,monkeypatch):
    eid='parent-fixture';studio=tmp_path/'studio';studio.mkdir()
    units=[dict(unit_id=f'u{i}',source_text=f'原稿段落{i}') for i in range(4)]
    parent='parent-identity';ids=['u1','u2'];passage='\n\n'.join(u['source_text'] for u in units[1:3])
    design=dict(parent_episode=eid,parent_identity=parent,source_sha256='source-hash',
                source_unit_ids=ids,source_text=passage,target_rate=1.2,target_seconds=84,
                acceptable_seconds=[60,90])
    atomic_json(studio/'pilot-design.json',design)
    scoped=tmp_path/'pilot'/'scoped-design.json';scoped.parent.mkdir()
    atomic_json(scoped,dict(parent_episode=eid,parent_identity=parent,source_units=ids,
        source_text=passage,target_rate=1.2,target_seconds=84,acceptable_seconds=[60,90]))
    monkeypatch.setattr(W,'folder',lambda _:studio)
    monkeypatch.setattr(W,'document',lambda _:{'source_sha256':'source-hash','units':units})
    monkeypatch.setattr(W,'identity',lambda _:parent)
    inputs={str(scoped.resolve()):__import__('hashlib').sha256(scoped.read_bytes()).hexdigest()}
    manifest={'alignment':[dict(unit_id=u['unit_id'],text=u['source_text']) for u in units[1:3]]}
    binding=W._pilot_subset_binding(eid,manifest,inputs)
    assert binding['pilot_parent_identity']==parent
    assert binding['pilot_design_sha256']==__import__('hashlib').sha256((studio/'pilot-design.json').read_bytes()).hexdigest()

    manifest['alignment'][1]['text']='未经授权的改写'
    with pytest.raises(ValueError,match='旁白与当前整集原稿选段不一致'):
        W._pilot_subset_binding(eid,manifest,inputs)

def test_short_pilot_rejects_noncontiguous_parent_units(tmp_path,monkeypatch):
    eid='parent-fixture';studio=tmp_path/'studio';studio.mkdir()
    units=[dict(unit_id=f'u{i}',source_text=f'原稿段落{i}') for i in range(4)]
    selected=['u0','u2'];passage='\n\n'.join(units[i]['source_text'] for i in (0,2))
    atomic_json(studio/'pilot-design.json',dict(parent_episode=eid,parent_identity='parent-identity',
        source_sha256='source-hash',source_unit_ids=selected,source_text=passage,
        target_rate=1.2,target_seconds=84,acceptable_seconds=[60,90]))
    monkeypatch.setattr(W,'folder',lambda _:studio)
    monkeypatch.setattr(W,'document',lambda _:{'source_sha256':'source-hash','units':units})
    monkeypatch.setattr(W,'identity',lambda _:'parent-identity')
    with pytest.raises(ValueError,match='短样片选择与当前整集原稿或方案不匹配'):
        W._pilot_subset_binding(eid,{'alignment':[]},{})

def test_claim_cannot_reference_other_text(draft):
    v=W.load(draft);v['claims'][0]['quote']='正文里没有这个说法'
    with pytest.raises(ValueError,match='逐字'):W.save(draft,v,expected_revision=v['revision'])

def test_direction_uses_existing_schema_and_stales_on_edit(draft):
    v=W.load(draft)
    v['cards']=[dict(at={'at':'start'},duration_ms=3000,card=dict(kind='title_card',title='测试',fade_in_ms=0))]
    W.save(draft,v,expected_revision=v['revision'])
    approve_editorial(draft);p=W.compile_direction(draft);d=W.read(p)
    assert d['visual_events'][0]['at']=={'at':'start'}
    assert d['cards'][0]['at']=={'at':'start'} and d['cards'][0]['card']['fade_in_ms']==0
    assert 'sample_start' not in d['visual_events'][0]
    W.require_direction_current(draft)
    v=W.load(draft);v['beats'][0]['image_id']='another-plate';W.save(draft,v,expected_revision=v['revision'])
    with pytest.raises(ValueError,match='导演事件'):W.require_direction_current(draft)

def test_unknown_metrics_stay_missing_and_invalid_percent_rejected(draft):
    row=dict(publication_id='fixture',version_sha256='a'*64,observed_at='2026-10-21T00:00:00+00:00',window_hours=24,duration_seconds=90,
             topic='fixture',hypothesis='fixture only',plays=20,avg_watch_seconds=None,likes=None,comments=0,shares=None,saves=None,follows=None,
             retention_3s=None,retention_5s=None,traffic_source='fixture',notes='')
    W.add_observation(draft,row)
    assert W.observations(draft)[0]['avg_watch_seconds'] is None
    row['retention_3s']=101
    with pytest.raises(ValueError,match='100'):W.add_observation(draft,row)

def test_library_import_preserves_frame_structure_without_old_approval(tmp_path,tmp_episodes,monkeypatch):
    from app import paths
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    atomic_json(tmp_path/'.studio-project.json',dict(original_root='D:/hv'))
    library=tmp_path/'episodes';library.mkdir(exist_ok=True)
    original=library/'old';original.mkdir();(original/'script.md').write_text('### 【第一幕：测试】\n\n测试正文。',encoding='utf-8')
    frames=[{'id':'frame-a','text':'保留手工剪辑结构'}]
    atomic_json(library/'old.json',dict(id='old',script_path=str(original/'script.md'),title='test',frames=frames,shots={},state={'approved':True}))
    before=(library/'old.json').read_bytes()
    S.import_episode('old','copy-fixture')
    out=W.read(paths.episode_json('copy-fixture'))
    assert out['frames']==frames and out['id']=='copy-fixture' and out['state']=={}
    assert (library/'old.json').read_bytes()==before
    assert not W.read(paths.episode_dir('copy-fixture')/'studio/import.json')['approvals_imported']
    with pytest.raises(ValueError,match='不同'):S.import_episode('old','old')

def test_title_is_rejected_without_silent_truncation():
    from app.services.overlay_author import _clip
    with pytest.raises(ValueError,match='不会自动截断'):_clip('长'*19,18)
