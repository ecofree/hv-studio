"""Opt-in resource overlap and read-only quality/efficiency diagnostics."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import sys
import time
import pytest
from app import paths
from app.services import production_storage as S, production_diagnostics as D
from app.services.provenance import atomic_json,file_sha256
pytestmark=pytest.mark.production


@pytest.fixture
def resources(monkeypatch):
    monkeypatch.setattr('psutil.virtual_memory',lambda:SimpleNamespace(available=64*1024**3))
    checks=[]
    monkeypatch.setattr('app.orchestrator._check_disk_space',lambda p,*,need_mb:checks.append((p,need_mb)))
    return checks


def test_default_capacity_is_one_not_unbounded(resources):
    with S.resource_reservation('one','fixed script'):
        with pytest.raises(S.ResourceBusy,match='episode-capacity'):
            with S.resource_reservation('two','fixed script'):pass
    with S.machine_storage():assert S.active_reservations()==[]
    assert list((paths.state_dir()/'resource-leases').glob('*.lock'))


def test_opt_in_two_episodes_reserve_combined_disk_and_serialize_render(resources):
    atomic_json(paths.state_dir()/'resource-policy.json',dict(max_active_episodes=2))
    with S.resource_reservation('one','script'),S.resource_reservation('two','script'):
        with S.machine_storage():assert len(S.active_reservations())==2
        with pytest.raises(S.ResourceBusy):
            with S.resource_reservation('three','script'):pass
        with S.render_lease():
            with S.render_lease():pass  # same-thread reentrancy only
            def render():
                with pytest.raises(S.ResourceBusy,match='video-encoder'):
                    with S.render_lease():pass
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(render).result(timeout=5)
    # Existing free-space tests remain authoritative. We check reservation
    # demand grows rather than counting the same free bytes twice.
    assert max(n for _,n in resources)>=min(n for _,n in resources)*2


def test_policy_changes_require_existing_production_to_drain(resources):
    atomic_json(paths.state_dir()/'resource-policy.json',dict(max_active_episodes=2))
    with S.resource_reservation('one','script'):
        atomic_json(paths.state_dir()/'resource-policy.json',dict(max_active_episodes=3))
        with pytest.raises(S.ResourceBusy,match='policy-change'):
            with S.resource_reservation('two','script'):pass


def test_memory_wait_is_not_a_supplier_failure(resources,monkeypatch):
    monkeypatch.setattr('psutil.virtual_memory',lambda:SimpleNamespace(available=512*1024**2))
    with pytest.raises(S.ResourceBusy,match='available-memory'):
        with S.resource_reservation('one','script'):pytest.fail('started without memory')


def test_live_resource_reservation_blocks_snapshot(resources,tmp_path,monkeypatch):
    from app.services import production_backup as B
    atomic_json(paths.episode_json('one'),dict(id='one'))
    with S.resource_reservation('one','script'):
        with pytest.raises(S.ResourceBusy,match='snapshot'):
            B.create_snapshot(['one'],tmp_path/'snapshot',dry_run=True)


def test_process_death_reclaims_capacity_without_deleting_kernel_lock(tmp_path):
    root=tmp_path/'worker';ready=tmp_path/'ready';state=paths.state_dir()
    script=tmp_path/'resource-owner.py'
    script.write_text('''import sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from app import paths
paths.state_dir=lambda:Path(sys.argv[2])
paths.episode_dir=lambda eid:Path(sys.argv[3])/eid
import psutil
psutil.virtual_memory=lambda:SimpleNamespace(available=64*1024**3)
from app import orchestrator
orchestrator._check_disk_space=lambda *a,**k:None
from app.services.production_storage import resource_reservation
with resource_reservation('one','script'):
    Path(sys.argv[4]).write_text('ready')
    time.sleep(30)
''',encoding='utf-8')
    child=subprocess.Popen([sys.executable,str(script),str(paths.ROOT),str(state),str(root),str(ready)],
        stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+15
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert ready.exists(),child.stderr.read().decode() if child.poll() is not None else 'child timeout'
        with S.machine_storage():assert len(S.active_reservations())==1
        child.kill();child.wait(timeout=10)
        with S.machine_storage():assert S.active_reservations()==[]
        assert list((state/'resource-leases').glob('*.lock'))
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        child.stderr.close()


def test_selective_rework_is_advice_not_new_approval():
    value=D.impact({'source':'same','visual':'old'},{'source':'same','visual':'new'})
    assert 'images' in value['suggested_stages'] and 'tts' not in value['suggested_stages']
    assert not value['automatic_execution'] and value['requires_current_stage_validation']
    assert 'tts' in D.impact({'source':'old'},{'source':'new'})['suggested_stages']
    assert D.compare({'inputs':{'x':1}},{'inputs':{'x':2}})['quality_improvement_proven'] is False


def test_missing_media_is_not_a_green_diagnostic(tmp_path):
    value=D.report('no-records')
    assert value['errors']
    assert value['approved'] is False and value['production_ready'] is False
    assert value['metrics']['human_review_minutes'] is None
    assert value['metrics']['approved_cost_per_minute'] is None


def test_same_pixels_group_contiguous_holds_and_escape_review_text(tmp_path,monkeypatch):
    from app.services import narration_pipeline as N, production_v2 as V, delivery
    image=tmp_path/'one.png';image.write_bytes(b'pixels')
    other=tmp_path/'two.png';other.write_bytes(b'pixels')
    timeline=tmp_path/'timeline.json'
    atomic_json(timeline,dict(shots=[dict(image_path=str(image),sample_start=0,sample_end=70),
        dict(image_path=str(other),sample_start=70,sample_end=100)],
        subtitles=[dict(unit_id='one',sample_start=0,sample_end=1,text='too many letters')]))
    manifest=dict(sample_rate=1,samples=100,alignment=[dict(unit_id='one',text='<script>alert(1)</script>',sample_start=0,sample_end=100)])
    monkeypatch.setattr(N,'validate_assembly',lambda *a:manifest)
    monkeypatch.setattr(V,'validate_timeline',lambda *a:None)
    monkeypatch.setattr(D,'input_snapshot',lambda *a:{'source':'fixed'})
    atomic_json(paths.episode_dir('diagnostic')/'production/v2/run.json',dict(manifest_path='fixture',timeline_path=str(timeline)))
    value=D.report('diagnostic')
    assert len(value['shots'])==1 and value['shots'][0]['review_long_hold']
    assert value['shots'][0]['duration_seconds']==100
    assert value['subtitle_flags'][0]['advisory_only'] is True
    page=D.write_report(value,tmp_path/'report')
    assert '<script>' not in page.read_text('utf-8')
    assert '&lt;script&gt;' in page.read_text('utf-8')
