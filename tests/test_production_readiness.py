"""Release gates reject incomplete or damaged evidence without losing the report."""
import time
import json
import pytest

from app.services import batch_worker, production_status, production_readiness
from app.services.provenance import atomic_json, file_sha256

pytestmark = pytest.mark.production


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(production_readiness.paths, 'state_dir', lambda: tmp_path)
    monkeypatch.setattr(batch_worker, 'queue_dir', lambda: tmp_path/'queue')
    monkeypatch.setattr(batch_worker, 'alive', lambda *args: True)
    # `content_approved` is the delivery-level truth; `approved` stays narrow
    # ("this v2 candidate"). assess keys on the former (2026-10-01: keying on
    # `approved` produced false blockers for episodes whose legacy delivery was
    # approved but whose v2 attempt was merely stale).
    monkeypatch.setattr(production_status, 'get_status',
                        lambda eid: {'approved': True, 'content_approved': True})
    atomic_json(tmp_path/'queue/worker.json', dict(pid=123, created_at=1, heartbeat=time.time()))
    artifact = tmp_path/'receipt.json'
    atomic_json(artifact, {'test_only': True})
    record = dict(source_fingerprint='current', artifacts={str(artifact):file_sha256(artifact)},
                  episodes=['one','two'], multi_episode_current_rebuild=True,
                  kill_resume=True, timeout_recovery=True, real_provider_models=True,
                  unattended_worker_full_batch=True)
    path = tmp_path/'acceptance/production-validation.json'
    atomic_json(path, record)
    return tmp_path, path, record


def test_release_requires_full_worker_batch_even_with_other_evidence(evidence):
    _, path, record = evidence
    assert production_readiness.assess('current')['ready']
    record['unattended_worker_full_batch'] = False
    atomic_json(path, record)
    result = production_readiness.assess('current')
    assert not result['ready']
    assert 'unattended_worker_full_batch validation is pending' in result['blockers']


@pytest.mark.parametrize('field,value', [
    ('episodes','one-two'), ('episodes',[['one'],'two']), ('episodes',['../outside','two']),
    ('artifacts',[]), ('artifacts',{'file':None}),
])
def test_malformed_evidence_is_a_blocker(evidence, field, value):
    _, path, record = evidence
    record[field] = value
    atomic_json(path, record)
    assert not production_readiness.assess('current')['ready']


@pytest.mark.parametrize('record', [[], None, {'heartbeat':'invalid'},
    {'pid':123,'created_at':1,'heartbeat':float('nan')},
    {'pid':123,'created_at':1,'heartbeat':time.time()+3600}])
def test_invalid_worker_record_cannot_pass(evidence, record):
    root, _, _ = evidence
    # Simulate damaged/external state, including values our own writer rejects.
    (root/'queue/worker.json').write_text(json.dumps(record), encoding='utf-8')
    assert not production_readiness.assess('current')['ready']


def test_invalid_root_and_damaged_episode_keep_readiness_report(evidence, monkeypatch):
    _, path, record = evidence
    atomic_json(path, [])
    assert not production_readiness.assess('current')['ready']
    atomic_json(path, record)
    def damaged(eid):
        raise TypeError('damaged episode state')
    monkeypatch.setattr(production_status, 'get_status', damaged)
    result = production_readiness.assess('current')
    assert not result['ready']
    assert len(result['blockers']) == 2


def test_masked_legacy_approval_satisfies_the_content_gate(evidence, monkeypatch):
    """The false-blocker regression, as a behaviour test.

    laogongtuan / lishimin-01 / lishimin-03 each carry a hash-bound legacy
    delivery *and* a stale or unreviewed v2 attempt. get_status honestly reports
    `approved=False` for the v2 attempt; readiness must still see the delivery.
    """
    _, path, record = evidence
    monkeypatch.setattr(production_status, 'get_status', lambda eid: dict(
        pipeline_version='production_v2', status='STALE', approved=False,
        content_approved=True, blockers=['candidate inputs changed']))
    assert production_readiness.assess('current')['ready']


def test_unapproved_delivery_still_blocks_readiness(evidence, monkeypatch):
    """content_approved=False must block even when the v2 attempt reports ok."""
    _, path, record = evidence
    monkeypatch.setattr(production_status, 'get_status', lambda eid: dict(
        pipeline_version='production_v2', status='STAGES_COMPLETE', approved=False,
        content_approved=False, blockers=['no current approval']))
    result = production_readiness.assess('current')
    assert not result['ready']
    assert sum(1 for b in result['blockers'] if b.endswith('requires current content approval')) == 2
