"""The user-facing production command exposes its pipeline and paid-call limits."""
import pytest
from app import cli

pytestmark = pytest.mark.production


def test_episode_help_exposes_production_controls(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['hv', 'episode', '--help'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for option in ('--pipeline', '--max-requests', '--max-images', '--budget-id'):
        assert option in help_text
