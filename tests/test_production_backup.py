"""Verify that paid assets and spent budgets survive an isolated restore."""
import pytest
from app import paths
from app.services.provenance import atomic_json,file_sha256
from app.services.narration_pipeline import read
from app.services.production_backup import create_snapshot,verify_snapshot,restore_snapshot
from app.services.provider_budget import ProviderBudget,BudgetExceeded

pytestmark=pytest.mark.production


def test_snapshot_restore_preserves_v2_assets_and_exhausted_budget(tmp_path,monkeypatch):
    root=tmp_path/'project';root.mkdir();monkeypatch.setattr(paths,'ROOT',root)
    monkeypatch.setattr(paths,'episodes_dir',lambda:root/'episodes')
    monkeypatch.setattr(paths,'state_dir',lambda:root/'state')
    atomic_json(paths.episode_json('one'),{'id':'one'})
    media=paths.episode_dir('one')/'production/v2/images/paid.png';media.parent.mkdir(parents=True);media.write_bytes(b'paid image bytes')
    atomic_json(media.with_suffix('.png.response.json'),{'response':'received'})
    budget=ProviderBudget(paths.state_dir()/'production-queue/budgets/paid.json',image_requests=1)
    budget.reserve('image','uncertain-request')
    pause=paths.state_dir()/'production-queue/PAUSE';pause.write_text('operator pause')
    (root/'.env').write_text('secret excluded')
    snapshot=create_snapshot(['one'],tmp_path/'archives')['snapshot']
    manifest=verify_snapshot(snapshot)
    assert '.env' not in manifest['files']
    restored=tmp_path/'restore'
    restore_snapshot(snapshot,restored)
    assert file_sha256(restored/media.relative_to(root))==file_sha256(media)
    resumed=ProviderBudget(restored/budget.path.relative_to(root),image_requests=1)
    with pytest.raises(BudgetExceeded):resumed.reserve('image','must-not-resend')
    assert read(restored/'RESTORE-INFO.json')['automatic_resume'] is False
    assert (restored/pause.relative_to(root)).read_text()=='operator pause'
    with pytest.raises(ValueError):restore_snapshot(snapshot,restored)
    (restored/'safe.txt').write_text('keep')
    manifest['files']['../../escape.txt']={'sha256':'x','bytes':1}
    atomic_json(__import__('pathlib').Path(snapshot)/'manifest.json',manifest)
    with pytest.raises(ValueError,match='escape'):verify_snapshot(snapshot)
