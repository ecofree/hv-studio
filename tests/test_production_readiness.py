"""Release gates reject booleans and require hash-bound scenario evidence."""
import json
import time
import pytest

from app.services import batch_worker, production_status, production_readiness
from app.services.provenance import atomic_json, file_sha256

pytestmark = pytest.mark.production
REVISION = "a" * 40
SCENARIOS = {
    "multi_episode_current_rebuild": [
        {"event":"episode_rebuild_completed","episode_id":"one"},
        {"event":"episode_rebuild_completed","episode_id":"two"},
    ],
    "kill_resume": [
        {"event":"worker_terminated","episode_id":"one"},
        {"event":"resume_started","episode_id":"one"},
        {"event":"resume_completed","episode_id":"one"},
    ],
    "timeout_recovery": [
        {"event":"timeout_detected","episode_id":"one"},
        {"event":"recovery_started","episode_id":"one"},
        {"event":"recovery_completed","episode_id":"one"},
    ],
    "real_provider_models": [
        {"event":"provider_request_succeeded","provider":"fixture-provider",
         "model":"real-model-id","request_id":"provider-task-123","is_mock":False},
    ],
    "unattended_worker_full_batch": [
        {"event":"batch_started","batch_id":"batch-1"},
        {"event":"episode_candidate_completed","episode_id":"one"},
        {"event":"episode_candidate_completed","episode_id":"two"},
        {"event":"batch_completed","batch_id":"batch-1"},
    ],
}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(production_readiness.paths, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(batch_worker, 'queue_dir', lambda: tmp_path/'queue')
    monkeypatch.setattr(batch_worker, 'alive', lambda *args: True)
    monkeypatch.setattr(production_status, 'get_status',
                        lambda eid: {'approved': True, 'content_approved': True})
    atomic_json(tmp_path/'queue/worker.json',
                dict(pid=123, created_at=1, heartbeat=time.time()))
    artifact = tmp_path/'candidate.mp4'
    artifact.write_bytes(b"fixture-candidate")
    scenario_evidence = {}
    for name, rows in SCENARIOS.items():
        run_id = "run-"+name
        if name == "real_provider_models":
            provider_receipt = tmp_path/"provider-receipt.json"
            atomic_json(provider_receipt, dict(status="succeeded", request_id="provider-task-123",
                                               model="real-model-id", audio_sha256="b"*64))
            rows = [dict(rows[0], receipt_path=str(provider_receipt),
                         receipt_sha256=file_sha256(provider_receipt))]
        events = [dict(row, run_id=run_id, at=time.time()) for row in rows]
        events_path = tmp_path/(name+"-events.json")
        atomic_json(events_path, events)
        manifest = dict(schema_version=1, scenario=name, run_id=run_id,
                        source_fingerprint="current", git_revision=REVISION,
                        status="passed", started_at=time.time()-2, finished_at=time.time())
        manifest_path = tmp_path/(name+"-manifest.json")
        atomic_json(manifest_path, manifest)
        scenario_evidence[name] = dict(
            schema_version=1, scenario=name, run_id=run_id,
            source_fingerprint="current", git_revision=REVISION,
            manifest_path=str(manifest_path), manifest_sha256=file_sha256(manifest_path),
            events_path=str(events_path), events_sha256=file_sha256(events_path),
            artifacts={str(artifact):file_sha256(artifact)},
        )
    record = dict(schema_version=2, source_fingerprint="current", git_revision=REVISION,
                  artifacts={str(artifact):file_sha256(artifact)},
                  episodes=["one","two"], scenario_evidence=scenario_evidence)
    path = tmp_path/'acceptance/production-validation.json'
    atomic_json(path, record)
    return tmp_path, path, record


def test_structured_evidence_can_pass_only_with_all_scenarios(evidence):
    assert production_readiness.assess("current")["ready"]


def test_legacy_boolean_flags_are_not_production_evidence(evidence):
    _, path, record = evidence
    legacy = dict(source_fingerprint="current", artifacts=record["artifacts"],
                  episodes=["one","two"], **{key:True for key in SCENARIOS})
    atomic_json(path, legacy)
    result = production_readiness.assess("current")
    assert not result["ready"]
    assert any("schema_version=2" in blocker for blocker in result["blockers"])


def test_missing_scenario_evidence_blocks_readiness(evidence):
    _, path, record = evidence
    del record["scenario_evidence"]["kill_resume"]
    atomic_json(path, record)
    result = production_readiness.assess("current")
    assert not result["ready"]
    assert any("kill_resume" in blocker for blocker in result["blockers"])


def test_changed_event_log_invalidates_attestation(evidence):
    _, path, record = evidence
    event_path = record["scenario_evidence"]["timeout_recovery"]["events_path"]
    with open(event_path, "a", encoding="utf-8") as stream:
        stream.write(" ")
    result = production_readiness.assess("current")
    assert not result["ready"]
    assert any("events missing or changed" in blocker for blocker in result["blockers"])


def test_mock_provider_cannot_satisfy_real_provider_scenario(evidence):
    _, path, record = evidence
    scenario = record["scenario_evidence"]["real_provider_models"]
    events_path = scenario["events_path"]
    rows = json.loads(open(events_path, encoding="utf-8").read())
    rows[0]["is_mock"] = True
    atomic_json(events_path, rows)
    scenario["events_sha256"] = file_sha256(events_path)
    atomic_json(path, record)
    result = production_readiness.assess("current")
    assert not result["ready"]
    assert any("non-mock provider receipt" in blocker for blocker in result["blockers"])


def test_unapproved_delivery_still_blocks_readiness(evidence, monkeypatch):
    monkeypatch.setattr(production_status, 'get_status', lambda eid: dict(
        pipeline_version='production_v2', status='STAGES_COMPLETE', approved=False,
        content_approved=False, blockers=['no current approval']))
    result = production_readiness.assess('current')
    assert not result['ready']
    assert sum(1 for b in result['blockers']
               if b.endswith('requires current content approval')) == 2


def test_worker_record_must_be_fresh(evidence):
    root, _, _ = evidence
    (root/'queue/worker.json').write_text(
        json.dumps(dict(pid=123, created_at=1, heartbeat=time.time()+3600)),
        encoding='utf-8')
    assert not production_readiness.assess("current")["ready"]
