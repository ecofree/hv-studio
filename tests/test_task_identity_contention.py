"""A pre-execution lock conflict remains bound to a resumable Studio attempt."""
import importlib.util
from pathlib import Path
import pytest
from app import paths
from app.services import studio_jobs as J
from app.services.narration_pipeline import read
from test_task_identity import studio_submission

pytestmark = pytest.mark.production


def test_versioned_studio_contention_remains_identified_and_resumable(tmp_path,monkeypatch):
    from app.services.process_lock import file_lock
    root,request,task,execution=studio_submission(tmp_path)
    spec=importlib.util.spec_from_file_location('task_contention_studio_worker',paths.ROOT/'bin/studio_job.py')
    worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    monkeypatch.setattr(J,'validate_request',lambda _:pytest.fail('must acquire episode before source checks'))
    with file_lock(paths.episode_dir('task-fixture')/'.produce.lock'):
        worker.execute(root/'request.json',request)
    result=read(root/'result.json')
    assert result['status']=='BLOCKED' and result['error_type']=='ProductionBusy'
    assert result['execution']==execution
    assert not Path(task['budget']['path']).exists()
    monkeypatch.setattr(J,'validate_request',lambda _:None)
    assert J.check_resume(root)[0]==request
