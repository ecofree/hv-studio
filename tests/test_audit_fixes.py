# -*- coding: utf-8 -*-
"""Audit fixes: unbound `fid` in _legacy_status, and scoped --image-key on v2.

2026-10-01 full audit. The dual-state masking fix is covered behaviourally in
test_production_readiness.py against the project's own evidence fixture.
"""
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.production


@pytest.fixture
def no_baseline_episode(monkeypatch):
    """An approved, composed episode that has no frames to compare subtitles to.

    Everything except the fidelity branch is stubbed so the assertions isolate
    what `_legacy_status` does when the comparison has no baseline.
    """
    from app.services import production_status as PS

    class NoFrames:
        frames = []

    monkeypatch.setattr("app.services.production_admission.check",
                        lambda eid: dict(eligible=True, blockers=[]))
    monkeypatch.setattr("app.services.output_binding.verify",
                        lambda eid: dict(ok=True, skipped=True, sha256=None, frames=None))
    # final_mp4 must look present so _legacy_status does not return None early
    monkeypatch.setattr("app.services.output_binding.final_mp4",
                        lambda eid: ROOT / "synthetic.mp4")
    (ROOT / "episodes/synthetic-approval").mkdir(exist_ok=True)
    (ROOT / "episodes/synthetic-approval/production").mkdir(exist_ok=True)
    approval = ROOT / "episodes/synthetic-approval/production/input-approval.json"
    approval.write_text('{"decision":"approved_inputs","reviewer":"t","finding":"t"}',
                        encoding="utf-8")
    # `_narration_source` is imported inside the function body
    monkeypatch.setattr("app.services.subtitle_fidelity._narration_source", lambda ep: "")
    monkeypatch.setattr("app.managers.load", lambda eid: NoFrames())
    yield PS
    import shutil
    shutil.rmtree(ROOT / "episodes/synthetic-approval", ignore_errors=True)


def test_missing_baseline_is_not_a_defect(no_baseline_episode):
    """No frames to compare -> not_applicable, and it must not block approval.

    Regression: `fid` was bound only inside the fidelity branch, so this path left
    it unbound and the approval expression raised UnboundLocalError. That is a
    NameError subclass, which production_readiness.assess does not catch, so it
    escaped into check_project instead of being reported as a blocker.
    """
    out = no_baseline_episode._legacy_status("synthetic-approval")
    assert out is not None, "an approved episode must still produce a report"
    assert out["dimensions"]["subtitle_fidelity"] == "not_applicable"
    assert out["approved"] is True
    assert out["content_approved"] is True


def test_unverifiable_fidelity_fails_closed(no_baseline_episode, monkeypatch):
    """If the check itself cannot run, the episode must not read as approved."""
    def boom(eid):
        raise OSError("episode.json unreadable")
    monkeypatch.setattr("app.managers.load", boom)
    out = no_baseline_episode._legacy_status("synthetic-approval")
    assert out["dimensions"]["subtitle_fidelity"] == "stale_or_invalid"
    assert out["approved"] is False, "unverifiable fidelity must fail closed"
    assert any("subtitle fidelity" in b for b in out["blockers"])


def test_every_status_shape_exposes_content_approved():
    """get_status must always answer the delivery-level question.

    Consumers (readiness, operator_flow, WebUI) cannot special-case which branch
    produced the status.
    """
    from app.services.production_status import get_status
    for eid in sorted(p.stem for p in (ROOT / "episodes").glob("*.json"))[:8]:
        status = get_status(eid)
        assert "content_approved" in status, eid
        assert isinstance(status["content_approved"], bool), eid
        assert "delivered_approval" in status, eid
        if not status["content_approved"]:
            assert status["blockers"], f"{eid} unapproved without a blocker"


# ------------------------------------------------- scoped --image-key on the v2 path
class FakeJob:
    """Minimal stand-in for app.services.image.ImageJob as the v2 stage uses it."""

    def __init__(self, jid):
        self.id = jid
        self.imported = False
        self.out_path = None

    @property
    def out(self):
        return ROOT / f"{self.id}.png"

    @property
    def receipt_path(self):
        return ROOT / f"{self.id}.receipt.json"

    def request_fingerprint(self):
        return f"fp-{self.id}"

    def is_done(self):
        return True


def test_v2_run_accepts_image_keys():
    import app.services.episode_production as EP
    params = inspect.signature(EP.run).parameters
    assert "image_keys" in params, "run() must accept image_keys or the CLI cannot reach it"


def test_unknown_image_key_is_rejected_before_any_call(monkeypatch):
    """A typo'd key must fail loudly; silently regenerating everything is the
    failure mode --image-key exists to prevent (caomin-01, 2026-09-29: a 3-slot
    budget split across 4 plates, none of them the wanted one)."""
    import app.services.episode_production as EP

    dispatched = []
    monkeypatch.setattr("app.services.production_inputs.prepare",
                        lambda ep, plan, root: ([FakeJob("A"), FakeJob("B")], ROOT / "scene.json"))
    monkeypatch.setattr("app.services.image.generate_many",
                        lambda jobs, budget=None: dispatched.extend(j.id for j in jobs))

    result = EP.run("synthetic-scoped", stages=["images"], script_md="旁白。",
                    image_keys=["A", "NOPE"], max_images=5)
    assert result["status"] == "BLOCKED"
    assert "NOPE" in result["error"]
    assert "not defined for this episode" in result["error"]
    assert dispatched == [], "no image call may be dispatched for an unknown key"


def test_scoped_run_dispatches_only_requested_keys(monkeypatch, tmp_path):
    import app.services.episode_production as EP

    dispatched = []
    def fake_generate(jobs, budget=None):
        dispatched.extend(j.id for j in jobs)
        return {j.id: str(j.out) for j in jobs}

    monkeypatch.setattr("app.services.production_inputs.prepare",
                        lambda ep, plan, root: ([FakeJob("A"), FakeJob("B"), FakeJob("C")],
                                                ROOT / "scene.json"))
    monkeypatch.setattr("app.services.image.generate_many", fake_generate)

    result = EP.run("synthetic-scoped2", stages=["images"], script_md="旁白。",
                    image_keys=["B"], max_images=5)
    assert result["run_ok"] is True, result.get("error")
    assert result["scoped_image_keys"] == ["B"]
    assert dispatched == ["B"], f"only the requested plate may be generated, got {dispatched}"
    # The other plates are untouched, so the episode is honestly incomplete...
    assert set(result["image_assets_incomplete"]) == {"A", "C"}
    # ...and a scoped run must not erase the plates it did not touch.
    from app.services.narration_pipeline import read
    state = read(ROOT / "episodes/synthetic-scoped2/production/v2/run.json")
    assert set(state["image_assets"]) == {"B"}
    assert state["declared_image_keys"] == ["A", "B", "C"], \
        "timeline needs to know what the episode declares without re-running prepare()"
    import shutil
    shutil.rmtree(ROOT / "episodes/synthetic-scoped2", ignore_errors=True)
    (ROOT / "episodes/synthetic-scoped2.json").unlink(missing_ok=True)


def test_timeline_refuses_a_partial_plate_set(monkeypatch):
    """A scoped image set must stop the timeline with an actionable message."""
    import app.services.episode_production as EP

    monkeypatch.setattr("app.services.production_inputs.prepare",
                        lambda ep, plan, root: ([FakeJob("A"), FakeJob("B")], ROOT / "scene.json"))
    monkeypatch.setattr("app.services.image.generate_many",
                        lambda jobs, budget=None: {j.id: str(j.out) for j in jobs})
    EP.run("synthetic-partial", stages=["images"], script_md="旁白。",
           image_keys=["A"], max_images=5)

    # Now ask for the timeline. It cannot succeed (no narration manifest exists),
    # but it must never render a hole, and the failure must be a BLOCKED report.
    result = EP.run("synthetic-partial", stages=["timeline"], script_md="旁白。")
    assert result["status"] == "BLOCKED"
    assert result["run_ok"] is False
    import shutil
    shutil.rmtree(ROOT / "episodes/synthetic-partial", ignore_errors=True)
    (ROOT / "episodes/synthetic-partial.json").unlink(missing_ok=True)


def test_unscoped_run_is_unchanged(monkeypatch):
    """No --image-key must keep the old behaviour: every plate, no incompleteness."""
    import app.services.episode_production as EP

    dispatched = []
    def fake_generate(jobs, budget=None):
        dispatched.extend(j.id for j in jobs)
        return {j.id: str(j.out) for j in jobs}

    monkeypatch.setattr("app.services.production_inputs.prepare",
                        lambda ep, plan, root: ([FakeJob("A"), FakeJob("B")], ROOT / "scene.json"))
    monkeypatch.setattr("app.services.image.generate_many", fake_generate)

    result = EP.run("synthetic-full", stages=["images"], script_md="旁白。", max_images=5)
    assert result["run_ok"] is True, result.get("error")
    assert dispatched == ["A", "B"]
    assert "scoped_image_keys" not in result
    assert "image_assets_incomplete" not in result
    import shutil
    shutil.rmtree(ROOT / "episodes/synthetic-full", ignore_errors=True)
    (ROOT / "episodes/synthetic-full.json").unlink(missing_ok=True)