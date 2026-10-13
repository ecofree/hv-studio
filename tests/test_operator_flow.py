"""Operator next-step text stays specific and does not authorize spending or approval."""
import json
from types import SimpleNamespace

import pytest

from app import cli
from app.services.operator_flow import advise, format_detail, list_rows, operator_status

pytestmark = pytest.mark.production


def test_fresh_episode_starts_at_script_without_a_budget():
    advice = advise("caomin-01", {"status": "NOT_STARTED"}, script_exists=True)
    text = format_detail("caomin-01", advice)
    assert advice["command"] == ".\\cli.bat episode produce caomin-01 --stage script"
    assert "--budget-id" not in text
    assert "--max-images" not in text


def test_missing_script_does_not_offer_a_paid_command():
    advice = advise("new-ep", {"status": "NOT_STARTED"}, script_exists=False)
    assert advice["command"] is None
    assert "script.md" in "\n".join(advice["notes"])


def test_image_step_keeps_the_budget_explicit():
    advice = advise("caomin-01", {"status": "STAGES_COMPLETE", "stages_done": ["script"]})
    assert advice["command"].endswith("--max-images N --budget-id <本次授权>")
    assert any("没有授权就不要执行" in note for note in advice["notes"])


def test_voice_findings_come_before_a_stale_rebuild():
    advice = advise("caomin-01", {
        "status": "STALE",
        "blockers": ["source changed", "audio findings await disposition"],
        "quality_pending_units": ["u1"],
    })
    assert advice["command"] == ".\\webui.bat"
    assert "语音" in advice["headline"]
    assert "--decision" not in (advice["command"] or "")


def test_running_and_approved_do_not_start_another_produce():
    running = advise("caomin-01", {"status": "RUNNING", "active_stage": "tts"})
    approved = advise("caomin-01", {"status": "APPROVED"})
    assert running["command"] is None and "tts" in running["headline"]
    assert approved["command"] is None
    assert "produce" not in "\n".join(approved["notes"])


def test_interrupted_resume_does_not_raise_the_budget():
    advice = advise("caomin-01", {"status": "INTERRUPTED"})
    assert advice["command"] == ".\\cli.bat episode produce caomin-01"
    assert "--max-" not in advice["command"]


def test_known_blocker_is_translated_and_unknown_text_is_kept():
    advice = advise("caomin-01", {
        "status": "BLOCKED",
        "blockers": ["requested sample exceeds explicit request/character budget", "自定义故障"],
    })
    assert "额度不够" in advice["blockers"][0]
    assert advice["blockers"][1] == "自定义故障"
    assert "<新的授权标识>" in advice["command"]


def test_image_stage_resume_continues_to_voice(monkeypatch, tmp_path):
    root = tmp_path / "episodes"
    folder = root / "lishimin-02"
    plan = folder / "production" / "v2" / "plans" / "plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}", encoding="utf-8")
    (root / "lishimin-02.json").write_text("{}", encoding="utf-8")
    (folder / "production" / "v2" / "run.json").write_text(json.dumps({
        "plan_path": str(plan),
        "image_assets": {"S01": "plate.png"},
        "last_result": {"status": "STAGES_COMPLETE", "stages_done": ["images"], "requested_stages": ["images"]},
    }), encoding="utf-8")
    monkeypatch.setattr("app.paths.episodes_dir", lambda: root)
    monkeypatch.setattr("app.services.production_status.get_status", lambda _episode: {
        "status": "STAGES_COMPLETE",
        "stages_done": ["images"],
        "blockers": ["current candidate requires full listening, visual and fact review"],
    })
    status, _script = operator_status("lishimin-02")
    advice = advise("lishimin-02", status, script_exists=True)
    assert status["stages_done"] == ["script", "images"]
    assert advice["command"] == ".\\cli.bat episode produce lishimin-02 --stage tts --max-requests N --max-characters C --budget-id <本次授权>"
    assert "听审" not in advice["headline"]
    assert advice["blockers"] == []


def test_existing_legacy_film_is_not_a_fresh_start(monkeypatch, tmp_path):
    root = tmp_path / "episodes"
    episode = root / "hw-002"
    (episode / "out").mkdir(parents=True)
    (episode / "out" / "hw-002_m1.mp4").write_bytes(b"film")
    (root / "hw-002.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("app.paths.episodes_dir", lambda: root)

    def fail_if_called(_episode_id):
        raise AssertionError("legacy output must not open the full production status")

    monkeypatch.setattr("app.services.production_status.get_status", fail_if_called)
    status, _script = operator_status("hw-002")
    advice = advise("hw-002", status, script_exists=True)
    assert status["status"] == "LEGACY_OUTPUT"
    assert advice["command"] is None
    assert "旧流程成片" in advice["headline"]
    assert any("--stage script" in note for note in advice["notes"])
    assert not any(flag in "\n".join(advice["notes"]) for flag in ("--max-images", "--max-requests"))


def test_status_list_hides_smoke_residue(monkeypatch, tmp_path):
    root = tmp_path / "episodes"
    root.mkdir()
    (root / "caomin-01.json").write_text("{}", encoding="utf-8")
    (root / "smoke-1.json").write_text("{}", encoding="utf-8")
    folder = root / "caomin-01" / "production" / "v2"
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps({
        "last_result": {"status": "CANDIDATE", "stages_done": ["script"]},
    }), encoding="utf-8")
    monkeypatch.setattr("app.paths.episodes_dir", lambda: root)
    rows, hidden = list_rows(False)
    assert [row["episode"] for row in rows] == ["caomin-01"]
    assert rows[0]["label"] == "候选片待审核"
    assert hidden == 1
    shown, none_hidden = list_rows(True)
    assert [row["episode"] for row in shown] == ["caomin-01", "smoke-1"]
    assert none_hidden == 0


def test_gc_command_lists_until_apply(monkeypatch, capsys):
    captured = {}

    def fake(older_than_days, dry_run):
        captured["dry_run"] = dry_run
        return {"removed": 2, "skipped_recent": 1, "human": "1KB", "dry_run": dry_run}

    monkeypatch.setattr("app.services.hv_ops.gc_state", fake)
    cli.cmd_gc(SimpleNamespace(older_than=7, apply=False, dry_run=False))
    assert captured["dry_run"] is True
    assert "--apply" in capsys.readouterr().out
    cli.cmd_gc(SimpleNamespace(older_than=7, apply=True, dry_run=False))
    assert captured["dry_run"] is False
    with pytest.raises(SystemExit):
        cli.cmd_gc(SimpleNamespace(older_than=7, apply=True, dry_run=True))


def test_candidate_advice_names_the_review_commands():
    advice = advise("xlc-01", {
        "status": "CANDIDATE",
        "candidate_path": "episodes/xlc-01/production/v2/renders/x/candidate.mp4",
    })
    text = "\n".join(advice["notes"])
    assert "hv narration review-quality" in text
    assert "hv narration review-audio" in text
    assert "hv narration review-video" in text
    assert "hv narration publish" in text
    assert advice["command"] == ".\\webui.bat"


def test_quality_advice_names_the_cli_alternative():
    advice = advise("xlc-01", {
        "status": "CANDIDATE",
        "candidate_path": "episodes/xlc-01/out.mp4",
        "quality_pending_units": ["narr-a"],
    })
    assert advice["command"] == ".\\webui.bat"
    assert "hv narration review-quality" in "\n".join(advice["notes"])


def test_retry_allowance_is_documented_next_to_the_budget_error():
    advice = advise("xlc-01", {
        "status": "BLOCKED",
        "blockers": ["provider budget exhausted: image_requests (27/27)"],
    })
    assert "--max-image-retries" in "\n".join(advice["notes"])
