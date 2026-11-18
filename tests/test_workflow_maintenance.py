"""Production workflow regressions using isolated files, no providers or codecs."""
from pathlib import Path
import copy
import json
import pytest
from app import paths
from app.services.provenance import atomic_json, file_sha256
from app.services.narration_pipeline import read

pytestmark=pytest.mark.production


@pytest.fixture
def studio(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir()
    monkeypatch.setattr(paths,'ROOT',root)
    monkeypatch.setattr(paths,'episodes_dir',lambda:root/'workspace/episodes')
    monkeypatch.setattr(paths,'state_dir',lambda:root/'workspace/state')
    atomic_json(root/'.studio-project.json',{'legacy_path_roots':['D:/hv']})
    atomic_json(paths.episode_json('one'),{'id':'one'})
    paths.episode_dir('one').mkdir()
    return root


def test_backup_rebases_old_media_without_accessing_original(studio,monkeypatch,tmp_path):
    from app.services import production_backup as B
    local=studio/'resource/paid.png';local.parent.mkdir();local.write_bytes(b'paid bytes')
    atomic_json(paths.episode_json('one'),{'id':'one','image_path':'D:/hv/resource/paid.png',
        'original_path':'D:/hv/missing-provenance.md'})
    native=Path.is_file
    def check(p):
        assert not str(p).replace('\\','/').lower().startswith('d:/hv/')
        return native(p)
    monkeypatch.setattr(Path,'is_file',check)
    result=B.create_snapshot(['one'],tmp_path/'backups')
    manifest=B.verify_snapshot(result['snapshot'])
    assert 'resource/paid.png' in manifest['files']
    assert not manifest['external_dependencies']


def test_backup_missing_local_dependency_is_visible_and_blocks_incomplete_copy(studio,tmp_path):
    from app.services import production_backup as B
    atomic_json(paths.episode_json('one'),{'image_path':'D:/hv/resource/missing.png'})
    preview=B.create_snapshot(['one'],tmp_path/'backups',dry_run=True)
    assert preview['missing_dependencies'][0]['path']==str(studio/'resource/missing.png')
    assert not preview['complete_dependencies']
    with pytest.raises(ValueError,match='dependencies missing'):B.create_snapshot(['one'],tmp_path/'backups')
    assert not (tmp_path/'backups').exists()


@pytest.mark.parametrize('approved',[True,False])
def test_batch_uses_existing_source_confirmation(studio,monkeypatch,approved):
    from app.services import production_admission as A,studio_workflow as W,studio_sources as S
    monkeypatch.setattr(A,'inputs',lambda e:dict(unsupported_cues=[],source_sha256='source'))
    monkeypatch.setattr(S,'require_usable',lambda e:None)
    monkeypatch.setattr(W,'status',lambda e:dict(ready_for_generation=approved,blockers=[] if approved else ['当前正文的关键事件待确认']))
    result=A.check('one')
    assert result['eligible'] is approved
    assert not (paths.episode_dir('one')/'production/input-approval.json').exists()


def test_batch_source_confirmation_does_not_bypass_visual_validation(studio,monkeypatch):
    from app.services import production_admission as A
    def invalid(e):raise ValueError('scene references missing image jobs')
    monkeypatch.setattr(A,'inputs',invalid)
    assert not A.check('one')['eligible']


def test_batch_accepts_studio_source_briefs_and_external_master_without_second_review(studio):
    from app.services import studio_workspace as S,studio_workflow as W,production_admission as A
    S.create_episode('story','测试','军队进入城中。')
    master=paths.episode_dir('story')/'master.wav';master.write_bytes(b'input identity fixture')
    value=W.load('story');value['voice']['master']=str(master);W.save('story',value,expected_revision=value['revision'])
    doc=W.document('story')
    briefs=paths.episode_dir('story')/'production/v2/source_image_briefs.json'
    atomic_json(briefs,dict(source_sha256=doc['source_sha256'],images={'unit-000':{'en':'Soldiers enter the city gates at dawn.'}}))
    assert not A.check('story')['eligible']
    W.record_review('story','editorial','tester','fixture only','approved',expected_identity=W.identity('story'),checks={'facts_and_story':True})
    assert A.check('story')['eligible'], A.check('story')['blockers']
    content=read(briefs);content['images']['unit-000']['en']='Soldiers gather outside the city gates at dusk.';atomic_json(briefs,content)
    assert A.check('story')['eligible']
    assert not (paths.episode_dir('story')/'production/input-approval.json').exists()


def render_fixture():
    folder=paths.episode_dir('one')/'production/v2/renders'/('a'*32)
    (folder/'segments').mkdir(parents=True)
    candidate=folder/'candidate.mp4';candidate.write_bytes(b'isolated candidate fixture')
    concat=folder/'concat.video.mp4';concat.write_bytes(b'concat')
    segment=folder/'segments/seg-000.mp4';segment.write_bytes(b'segment')
    sha=file_sha256(candidate)
    atomic_json(folder/'candidate.json',{'candidate_path':str(candidate),'candidate_sha256':sha})
    atomic_json(folder/'render_state.json',{'status':'completed','candidate_sha256':sha})
    return folder,candidate,concat,segment


def test_v2_cleanup_preview_and_apply_keep_sources_and_segment_cache_by_default(studio):
    from app.services import v2_cleanup as C
    folder,candidate,concat,segment=render_fixture()
    image=folder/'paid.png';image.write_bytes(b'paid')
    preview=C.plan('one');assert len(preview['items'])==1
    assert concat.exists()
    result=C.apply('one',preview)
    assert result['deleted_bytes']==6 and not concat.exists()
    assert candidate.exists() and image.exists() and segment.exists()
    C.apply('one',C.plan('one',include_segments=True))
    assert not segment.exists() and candidate.exists()


@pytest.mark.parametrize('change',['candidate','intermediate','receipt','running'])
def test_cleanup_refuses_changed_or_busy_render(studio,change):
    from app.services import v2_cleanup as C
    from app.services.process_lock import file_lock
    folder,candidate,concat,segment=render_fixture();preview=C.plan('one')
    if change=='candidate':candidate.write_bytes(b'changed candidate')
    elif change=='intermediate':concat.write_bytes(b'changed intermediate')
    elif change=='receipt':atomic_json(folder/'candidate.json',{})
    if change=='running':
        with file_lock(paths.episode_dir('one')/'.produce.lock'):
            with pytest.raises(RuntimeError):C.apply('one',preview)
    else:
        with pytest.raises(ValueError):C.apply('one',preview)
    assert concat.exists() and segment.exists()


def test_cleanup_ignores_incomplete_and_escaped_candidate(studio,tmp_path):
    from app.services import v2_cleanup as C
    folder,candidate,concat,segment=render_fixture()
    atomic_json(folder/'render_state.json',{'status':'incomplete'})
    assert not C.plan('one',include_segments=True)['items']
    outside=tmp_path/'candidate.mp4';outside.write_bytes(b'outside')
    atomic_json(folder/'render_state.json',{'status':'completed','candidate_sha256':file_sha256(outside)})
    atomic_json(folder/'candidate.json',{'candidate_path':str(outside),'candidate_sha256':file_sha256(outside)})
    assert not C.plan('one')['items']


def recovery_fixture(tmp_path):
    from app.services import recording_alignment_recovery as R
    master=tmp_path/'master.wav';master.write_bytes(b'fixture master')
    plan={'source_sha256':'source','units':[{'unit_id':str(i),'display_text':str(i)} for i in range(3)]}
    recording=dict(delivered_path=str(master),delivered_sha256=file_sha256(master),duration_s=10.)
    rows=[dict(frame='0',start=0.,end=2.,coverage=1.),dict(frame='1',start=None,end=None,coverage=0.),dict(frame='2',start=7.,end=10.,coverage=1.)]
    R.prepare(plan,recording,rows,tmp_path)
    return plan,recording,rows,tmp_path/'alignment-recovery.json'


def test_local_alignment_recovers_only_selected_range_and_keeps_checks(tmp_path):
    from app.services import recording_alignment_recovery as R
    plan,recording,rows,report=recovery_fixture(tmp_path)
    original=Path(recording['delivered_path']).read_bytes()
    R.save_range(report,'1',3.,6.,expected_sha256=file_sha256(report))
    patched=R.prepare(plan,recording,rows,tmp_path)
    assert patched[0]==rows[0] and patched[2]==rows[2]
    assert patched[1]['start']==3. and patched[1]['coverage']==0.
    assert patched[1]['manual_location'] is True
    assert read(report)['approved'] is False
    assert Path(recording['delivered_path']).read_bytes()==original
    changed=dict(recording,delivered_sha256='changed')
    assert R.prepare(plan,changed,rows,tmp_path)[1]['start'] is None


@pytest.mark.parametrize('a,b',[(1.,6.),(3.,8.),(-1.,2.),(5.,4.),(0.,float('nan'))])
def test_local_alignment_refuses_overlap_and_invalid_bounds(tmp_path,a,b):
    from app.services import recording_alignment_recovery as R
    _,_,_,report=recovery_fixture(tmp_path)
    with pytest.raises(ValueError):R.save_range(report,'1',a,b,expected_sha256=file_sha256(report))


def test_local_alignment_refuses_changed_master_or_report(tmp_path):
    from app.services import recording_alignment_recovery as R
    _,rec,_,report=recovery_fixture(tmp_path)
    with pytest.raises(ValueError):R.save_range(report,'1',3.,6.,expected_sha256='stale')
    Path(rec['delivered_path']).write_bytes(b'changed')
    with pytest.raises(ValueError):R.save_range(report,'1',3.,6.,expected_sha256=file_sha256(report))


def test_changed_local_range_invalidates_old_cut_manifest(tmp_path):
    from app.services.external_narration import validate_external
    atomic_json(tmp_path/'alignment-ranges/current.json',{'range':[2,3]})
    with pytest.raises(ValueError,match='局部语音定位已变化'):
        validate_external({'master_path':str(tmp_path/'ext-one/master.wav')})


def test_cleanup_ui_requires_explicit_preview_and_apply(studio):
    from streamlit.testing.v1 import AppTest
    _,candidate,concat,segment=render_fixture()
    app=AppTest.from_string("from app.webui.studio_workbench_tools import cleanup\ncleanup('one')").run()
    assert not app.exception and concat.exists()
    app.button(key='clean-plan-one').click().run()
    assert not app.exception and concat.exists()
    app.button(key='clean-apply-one').click().run()
    assert not app.exception and not concat.exists() and candidate.exists() and segment.exists()


def test_measured_history_keeps_failures_and_does_not_double_count(tmp_path):
    from app.services.production_metrics import StageTimings,accumulated
    timer=StageTimings();timer.begin('alignment');timer.finish()
    timer.persist(tmp_path,dict(status='BLOCKED',run_ok=False));timer.persist(tmp_path,dict(status='BLOCKED',run_ok=False))
    second=StageTimings();second.begin('images');second.persist(tmp_path,dict(status='CANDIDATE',run_ok=True))
    result=accumulated(tmp_path)
    assert result['invocations']==2 and result['failed_invocations']==1
    assert set(result['stages'])=={'alignment','images'}
    assert result['currency_cost'] is None and not result['historical_time_reconstructed']


def test_review_navigation_is_chronological_and_advisory():
    from app.services.production_diagnostics import review_points
    data=dict(shots=[dict(start_seconds=5,duration_seconds=120,review_long_hold=True,narration=['text'],image_path='one.png')],
        listening=[dict(start_seconds=2,kinds=['meaning_change'],text='audio',audio_path='one.wav')],
        subtitle_flags=[dict(start_seconds=4,text='subtitle')])
    original=copy.deepcopy(data);points=review_points(data)
    assert [p['start_seconds'] for p in points]==[2,4,5]
    assert data==original and '连续同图' in points[-1]['label']
