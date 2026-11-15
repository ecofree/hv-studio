"""Verify structured, hash-bound production acceptance evidence.

This module deliberately rejects legacy boolean attestations. Scenario evidence
must point to immutable run manifests, event logs and actual artifact receipts.
It verifies provenance and required events; it does not itself execute paid
providers or claim that synthetic unit-test fixtures are real production runs.
"""
from __future__ import annotations

import re
import math
from pathlib import Path

from app.services.narration_pipeline import read
from app.services.provenance import file_sha256
from app.services.production_identity import artifacts_current

SCENARIOS = {
    "multi_episode_current_rebuild": {"required": ("episode_rebuild_completed",), "min_episodes": 2},
    "kill_resume": {"required": ("worker_terminated", "resume_started", "resume_completed")},
    "timeout_recovery": {"required": ("timeout_detected", "recovery_started", "recovery_completed")},
    "real_provider_models": {"required": ("provider_request_succeeded",)},
    "unattended_worker_full_batch": {
        "required": ("batch_started", "episode_candidate_completed", "batch_completed"),
        "min_episodes": 2,
    },
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _file(path, digest, label):
    if not isinstance(path, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} path/hash missing or malformed")
    if file_sha256(path) != digest:
        raise ValueError(f"{label} missing or changed")
    return Path(path).resolve()


def verify_scenario(name, record, source_fingerprint):
    """Verify one scenario against its run manifest, events and artifact hashes."""
    if name not in SCENARIOS or not isinstance(record, dict):
        raise ValueError("unknown or malformed production acceptance scenario")
    if record.get("schema_version") != 1 or record.get("scenario") != name:
        raise ValueError(f"{name}: unsupported evidence schema or scenario mismatch")
    if record.get("source_fingerprint") != source_fingerprint:
        raise ValueError(f"{name}: evidence belongs to another source revision")
    if not isinstance(record.get("run_id"), str) or not record["run_id"].strip():
        raise ValueError(f"{name}: run_id missing")
    if not isinstance(record.get("git_revision"), str) or not _GIT_SHA.fullmatch(record["git_revision"]):
        raise ValueError(f"{name}: exact git revision missing")

    manifest_path = _file(record.get("manifest_path"), record.get("manifest_sha256"), name+" manifest")
    events_path = _file(record.get("events_path"), record.get("events_sha256"), name+" events")
    manifest = read(manifest_path)
    events = read(events_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError(f"{name}: invalid run manifest")
    for key, expected in (("scenario", name), ("run_id", record["run_id"]),
                          ("source_fingerprint", source_fingerprint),
                          ("git_revision", record["git_revision"]), ("status", "passed")):
        if manifest.get(key) != expected:
            raise ValueError(f"{name}: run manifest {key} mismatch")
    started, finished = manifest.get("started_at"), manifest.get("finished_at")
    if (type(started) not in (int, float) or type(finished) not in (int, float)
            or not math.isfinite(started) or not math.isfinite(finished) or finished < started):
        raise ValueError(f"{name}: invalid run timestamps")
    if not isinstance(events, list) or not events:
        raise ValueError(f"{name}: event log is empty or malformed")
    if any(not isinstance(event, dict) or event.get("run_id") != record["run_id"]
           or not isinstance(event.get("event"), str)
           or type(event.get("at")) not in (int, float) or not math.isfinite(event["at"])
           for event in events):
        raise ValueError(f"{name}: event log contains unbound or malformed events")

    event_names = {event["event"] for event in events}
    missing = set(SCENARIOS[name]["required"]) - event_names
    if missing:
        raise ValueError(f"{name}: missing required events: {', '.join(sorted(missing))}")
    min_episodes = SCENARIOS[name].get("min_episodes", 0)
    if min_episodes:
        episode_ids = {event.get("episode_id") for event in events
                       if event["event"] in ("episode_rebuild_completed", "episode_candidate_completed")
                       and isinstance(event.get("episode_id"), str) and event["episode_id"]}
        if len(episode_ids) < min_episodes:
            raise ValueError(f"{name}: fewer than {min_episodes} distinct completed episodes")
    if name == "real_provider_models":
        valid = []
        for event in events:
            if (event["event"] != "provider_request_succeeded"
                    or event.get("is_mock") is not False
                    or not all(isinstance(event.get(k), str) and event[k].strip()
                               for k in ("provider", "model", "request_id"))):
                continue
            receipt_path = _file(event.get("receipt_path"), event.get("receipt_sha256"),
                                 "real provider receipt")
            receipt = read(receipt_path)
            if (isinstance(receipt, dict) and receipt.get("status") == "succeeded"
                    and receipt.get("request_id") == event["request_id"]
                    and receipt.get("model") == event["model"]
                    and isinstance(receipt.get("audio_sha256"), str)
                    and _SHA256.fullmatch(receipt["audio_sha256"])
                    and isinstance(event.get("audio_path"), str)
                    and event.get("audio_sha256") == receipt.get("audio_sha256")
                    and file_sha256(event["audio_path"]) == event["audio_sha256"]):
                valid.append(event)
        if not valid:
            raise ValueError("real_provider_models: no hash-bound successful non-mock provider receipt")

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts or not artifacts_current(artifacts):
        raise ValueError(f"{name}: scenario artifacts missing or changed")
    return {
        "scenario": name, "run_id": record["run_id"], "git_revision": record["git_revision"],
        "manifest_path": str(manifest_path), "events_path": str(events_path),
        "artifact_count": len(artifacts),
    }


def verify_validation(evidence, source_fingerprint):
    """Return verified scenario summaries; never accept legacy boolean flags."""
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 2:
        raise ValueError("production validation must use structured schema_version=2 evidence")
    if evidence.get("source_fingerprint") != source_fingerprint:
        raise ValueError("production validation is for another source revision")
    git_revision = evidence.get("git_revision")
    if not isinstance(git_revision, str) or not _GIT_SHA.fullmatch(git_revision):
        raise ValueError("production validation exact git revision is missing")
    scenarios = evidence.get("scenario_evidence")
    if not isinstance(scenarios, dict):
        raise ValueError("scenario_evidence is missing")
    summaries = {}
    blockers = []
    for name in SCENARIOS:
        try:
            record = scenarios.get(name)
            if not isinstance(record, dict) or record.get("git_revision") != git_revision:
                raise ValueError(f"{name}: scenario revision differs from validation revision")
            summaries[name] = verify_scenario(name, record, source_fingerprint)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(str(exc))
    episodes = evidence.get("episodes")
    if not isinstance(episodes, list) or len(set(episodes)) < 2:
        blockers.append("two current episode validations are required")
    else:
        for eid in episodes:
            if not isinstance(eid, str) or not eid:
                blockers.append("invalid episode ID in production validation")
                break
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts or not artifacts_current(artifacts):
        blockers.append("production validation artifacts are missing or changed")
    return dict(ready=not blockers, blockers=blockers, scenarios=summaries)


def collect_validation(source_fingerprint, git_revision, episodes, evidence_root):
    """Build the canonical attestation from independently written run evidence.

    Expected layout: <root>/<scenario>/{run.json,events.json,artifacts.json}.
    This collector hashes and verifies those files; it never creates scenario
    events or marks a scenario passed on behalf of a test/production runner.
    """
    root = Path(evidence_root).resolve()
    scenario_evidence, all_artifacts = {}, {}
    for name in SCENARIOS:
        folder = root/name
        manifest_path, events_path, artifacts_path = (
            folder/"run.json", folder/"events.json", folder/"artifacts.json")
        manifest, events, artifacts = read(manifest_path), read(events_path), read(artifacts_path)
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError(f"{name}: artifacts.json must contain path-to-sha256 entries")
        record = dict(
            schema_version=1, scenario=name, run_id=manifest.get("run_id"),
            source_fingerprint=source_fingerprint, git_revision=git_revision,
            manifest_path=str(manifest_path), manifest_sha256=file_sha256(manifest_path),
            events_path=str(events_path), events_sha256=file_sha256(events_path),
            artifacts=artifacts,
        )
        # Validate immediately; never write a green attestation for malformed evidence.
        verify_scenario(name, record, source_fingerprint)
        scenario_evidence[name] = record
        all_artifacts[str(manifest_path)] = file_sha256(manifest_path)
        all_artifacts[str(events_path)] = file_sha256(events_path)
        all_artifacts.update(artifacts)
    result = dict(schema_version=2, source_fingerprint=source_fingerprint,
                  git_revision=git_revision, episodes=list(episodes),
                  artifacts=all_artifacts, scenario_evidence=scenario_evidence)
    verified = verify_validation(result, source_fingerprint)
    if not verified["ready"]:
        raise ValueError("; ".join(verified["blockers"]))
    return result
