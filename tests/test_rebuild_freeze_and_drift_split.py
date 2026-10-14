# -*- coding: utf-8 -*-
"""Pin Q2/Q3: missing-plate detection and the content/renderer drift split.

Why: lishimin-01b carried approved=True while its candidate.mp4 was gone;
the audit first misread 22 plates as lost (they were under v2/images/, found
via image_assets). And three approved v2 candidates read STALE for months
because renderer source edits invalidated byte-bound approvals (2026-10-14).
"""
import json
import sys

import pytest

sys.path.insert(0, r"D:\hv")

pytestmark = pytest.mark.production


def test_missing_scene_images_supports_both_plan_shapes(tmp_path, monkeypatch):
    from app.services import production_status as PS

    ep = tmp_path / "ep"
    ep.mkdir()
    (ep / "x.png").write_bytes(b"x")
    (ep / "scene_plan.json").write_text(json.dumps({
        "images": {"good": {"path": str(ep / "x.png")}},
        "scenes": [{"frames": [0, 1], "image": "good"},
                   {"frames": [2, 3], "image": "gone"}],
    }), encoding="utf-8")
    # _episode_dir is imported function-level from app.orchestrator,
    # so the seam is the orchestrator attribute.
    monkeypatch.setattr("app.orchestrator._episode_dir", lambda _eid: ep)
    assert PS.missing_scene_images("any", only_when_built=False) == ["gone"]


def test_missing_scene_images_supports_unit_scenes(tmp_path, monkeypatch):
    from app.services.production_status import missing_scene_images

    ep = tmp_path / "ep"
    ep.mkdir()
    (ep / "x.png").write_bytes(b"x")
    (ep / "scene_plan.json").write_text(json.dumps({
        "images": {"good": {"path": str(ep / "x.png")}},
        "unit_scenes": [{"unit_id": "u1", "image": "good"},
                        {"unit_id": "u2", "image": "gone"}],
    }), encoding="utf-8")
    monkeypatch.setattr("app.orchestrator._episode_dir", lambda _eid: ep)
    assert missing_scene_images("any", only_when_built=False) == ["gone"]


def test_missing_scene_images_without_a_plan_is_empty(tmp_path, monkeypatch):
    from app.services.production_status import missing_scene_images

    monkeypatch.setattr("app.orchestrator._episode_dir",
                        lambda _eid: tmp_path / "empty")
    assert missing_scene_images("any") == []


def test_split_receipt_drift_separates_content_from_renderer():
    from app.services.production_identity import split_receipt_drift

    receipt = {
        r"D:\hv\app\services\compose.py": "0" * 64,          # renderer, drifted
        r"D:\hv\app\config\config.yaml": "0" * 64,          # renderer, drifted
        r"D:\hv\episodes\caomin-02\script.md": "0" * 64,     # content, drifted
        r"D:\hv\requirements.core.lock.txt": "0" * 64,      # renderer pin
    }
    content, renderer = split_receipt_drift(receipt)
    assert content == [r"D:\hv\episodes\caomin-02\script.md"]
    assert sorted(renderer) == sorted([
        r"D:\hv\app\services\compose.py",
        r"D:\hv\app\config\config.yaml",
        r"D:\hv\requirements.core.lock.txt",
    ])


def test_split_receipt_drift_empty_receipt_is_both_empty():
    from app.services.production_identity import split_receipt_drift

    assert split_receipt_drift({}) == ([], [])
    assert split_receipt_drift(None) == ([], [])


def test_renderer_drift_is_surfaced_by_advise_but_blocks_nothing():
    from app.services.operator_flow import advise

    advice = advise("laogongtuan", {
        "status": "APPROVED",
        "candidate_path": "episodes/laogongtuan/out.mp4",
        "renderer_changed": [r"D:\hv\app\services\compose.py"],
    })
    assert advice["status"] == "APPROVED"
    assert any("渲染器源码" in n for n in advice["notes"])


def test_apply_scene_freeze_revokes_approval_but_keeps_evidence():
    from app.services.production_status import apply_scene_freeze

    result = {"status": "APPROVED", "approved": True, "delivery_ready": True,
              "blockers": [], "scene_images_missing": ["LA01", "LA02"]}
    out = apply_scene_freeze(result)
    assert out["approved"] is False
    assert out["delivery_ready"] is False
    assert out["status"] == "STALE"
    assert any("LA01" in b and "LA02" in b for b in out["blockers"])
    # The missing list itself survives for the operator to act on.
    assert out["scene_images_missing"] == ["LA01", "LA02"]


def test_apply_scene_freeze_leaves_healthy_status_untouched():
    from app.services.production_status import apply_scene_freeze

    result = {"status": "APPROVED", "approved": True, "delivery_ready": True,
              "blockers": [], "scene_images_missing": []}
    out = apply_scene_freeze(result)
    assert out["approved"] is True
    assert out["status"] == "APPROVED"
    assert out["blockers"] == []

def test_old_receipts_read_as_uniform_encoder():
    """Receipts predating encoder_mixed must not read as mixed."""
    from app.services.production_status import get_status

    s = get_status("laogongtuan")
    assert s["dimensions"].get("encoder") == "uniform"


def test_mixed_encoders_are_visible_not_blocking():
    """A mixed-encoder film decodes fine; status must show it, not hide it."""
    from app.services.production_status import get_status

    s = get_status("xlc-01")
    assert s["dimensions"].get("encoder") in ("uniform", "mixed")
