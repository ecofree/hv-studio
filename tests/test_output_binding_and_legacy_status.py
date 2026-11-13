"""Output binding checks own temporary media; never mutate delivered episodes."""
import pytest
from app.services import output_binding
from app.services.production_status import get_status

pytestmark = pytest.mark.shared


def test_output_binding_detects_replaced_and_missing_video(tmp_episodes, monkeypatch):
    path = output_binding.final_mp4('binding-fixture')
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original fixture bytes')
    monkeypatch.setattr(output_binding, 'probe', lambda p: dict(bytes=p.stat().st_size, frames=25, duration_s=1.))
    record = output_binding.bind('binding-fixture')
    assert record['frames'] == 25 and len(record['sha256']) == 64
    assert output_binding.verify('binding-fixture')['ok']
    path.write_bytes(b'replaced fixture bytes')
    assert not output_binding.verify('binding-fixture')['ok']
    path.unlink()
    assert not output_binding.verify('binding-fixture')['ok']


def test_missing_episode_cannot_acquire_output_approval(tmp_episodes):
    with pytest.raises(FileNotFoundError):
        output_binding.bind('missing-fixture')
    result = get_status('missing-fixture')
    assert result['status'] == 'NOT_STARTED' and not result['approved']
