"""Cross-entry exclusion uses the existing kernel lease, not a second scheduler."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app import paths
from app.services import production_storage as S
from app.services.process_lock import file_lock

pytestmark = pytest.mark.production


def test_nested_entries_share_one_lease_but_other_thread_cannot_enter():
    with S.episode_execution('execution-fixture'):
        from app.orchestrator import _episode_lock
        with _episode_lock('execution-fixture'):
            assert S.episode_available('execution-fixture')
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(S.episode_available, 'execution-fixture').result(timeout=5)
    assert S.episode_available('execution-fixture')


def test_other_episodes_have_independent_leases():
    with S.episode_execution('execution-one'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(S.episode_available, 'execution-two').result(timeout=5)


def test_body_runtime_error_is_not_misclassified_as_contention():
    with pytest.raises(RuntimeError, match='actual body failure') as exc:
        with S.episode_execution('execution-fixture'):
            raise RuntimeError('actual body failure')
    assert not isinstance(exc.value, S.ProductionBusy)
    assert S.episode_available('execution-fixture')


def test_existing_raw_episode_lock_still_excludes_new_entry_points():
    with file_lock(paths.episode_dir('execution-fixture')/'.produce.lock'):
        assert not S.episode_available('execution-fixture')
    assert S.episode_available('execution-fixture')


def test_busy_v2_stops_before_source_preparation(monkeypatch):
    monkeypatch.setattr('app.managers.load', lambda *a: pytest.fail('must not load/prepare source'))
    @S.production_storage
    def run(eid):
        pytest.fail('must not start production')
    with file_lock(paths.episode_dir('execution-fixture')/'.produce.lock'):
        with pytest.raises(S.ProductionBusy): run('execution-fixture')


def test_queue_skips_busy_episode_without_attempt_or_budget_and_checks_next(tmp_path, monkeypatch):
    from app.services import batch_worker as B, production_admission as A
    from app.services.narration_pipeline import read
    folder=tmp_path/'queue'
    queue=B.enqueue('execution-batch', ['busy','next'], {}, folder=folder)
    before=read(read(queue)['budget_path']); seen=[]
    def check(eid):
        seen.append(eid)
        return dict(eligible=False,blockers=['input review pending'],input_fingerprint='fixture')
    monkeypatch.setattr(A, 'check', check)
    with file_lock(paths.episode_dir('busy')/'.produce.lock'):
        B.run_once(folder, command_factory=lambda *a: pytest.fail('must not spawn'))
    result=read(queue)
    assert seen==['next']
    assert result['episodes']['busy']['attempts']==0
    assert result['episodes']['busy']['status']=='QUEUED'
    assert result['episodes']['next']['status']=='NEEDS_REVIEW'
    assert read(result['budget_path'])==before


def test_studio_job_records_contention_before_validation_or_budget(tmp_path, monkeypatch):
    import app.services.studio_jobs as J
    monkeypatch.setattr(J,'validate_request',lambda *a:pytest.fail('must not validate while owned'))
    spec=importlib.util.spec_from_file_location('isolated_studio_job', paths.ROOT/'bin/studio_job.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=tmp_path/'studio-job';root.mkdir()
    with file_lock(paths.episode_dir('execution-fixture')/'.produce.lock'):
        module.execute(root/'request.json',{'ep_id':'execution-fixture'})
    result=json.loads((root/'result.json').read_text('utf-8'))
    assert result['status']=='BLOCKED'
    assert result['error_type']=='ProductionBusy'
    assert not (root/'budget.json').exists()


def test_killed_process_releases_episode_lease_without_deleting_lock(tmp_path, monkeypatch):
    root=tmp_path/'episodes';root.mkdir(exist_ok=True)
    monkeypatch.setattr(paths,'episode_dir',lambda eid:root/eid)
    ready=tmp_path/'ready'
    script=tmp_path/'owner.py'
    script.write_text("""import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app import paths
paths.episode_dir=lambda eid:Path(sys.argv[2])/eid
from app.services.production_storage import episode_execution
with episode_execution('shared'):
    Path(sys.argv[3]).write_text('owned')
    time.sleep(60)
""",encoding='utf-8')
    child=subprocess.Popen([sys.executable,str(script),str(paths.ROOT),str(root),str(ready)],
                           stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+15
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert ready.exists(), 'child failed to acquire lease'
        assert not S.episode_available('shared')
        child.kill();child.wait(timeout=10)
        assert S.episode_available('shared')
        assert (root/'shared/.produce.lock').exists()
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=10)
        child.stderr.close()
