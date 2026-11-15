"""Causal contracts for acceptance evidence; no work dispatch or approvals.

Hashes establish integrity, not origin/authenticity against a local administrator.
Real acceptance must be produced by the supervised runner, not handwritten flags.
"""
import math
import re
from pathlib import Path
from app.services.narration_pipeline import read
from app.services.provenance import file_sha256

_COMPLETIONS = {'episode_rebuild_completed', 'episode_candidate_completed',
                'resume_completed', 'recovery_completed'}
_RECOVERY = {
    'kill_resume': ('worker_started', 'worker_terminated', 'resume_started', 'resume_completed'),
    'timeout_recovery': ('worker_started', 'timeout_detected', 'worker_terminated',
                         'recovery_started', 'recovery_completed'),
}


def _digest(value, size):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{%d}' % size, value) is not None


def _budget(event):
    p, sha = event.get('budget_receipt_path'), event.get('budget_receipt_sha256')
    if not isinstance(p, str) or not _digest(sha, 64) or file_sha256(p) != sha:
        raise ValueError('recovery budget snapshot missing or changed')
    receipt = read(p)
    from app.services.provider_budget import validate_usage
    validate_usage(receipt['limits'], receipt['used'])
    return receipt


def validate_chain(name, manifest, events, artifacts, *, require_real=True):
    mode = manifest.get('evidence_mode')
    if mode not in ('real', 'offline') or require_real and mode != 'real':
        raise ValueError('real acceptance cannot use offline or unclassified evidence')
    started, finished = manifest['started_at'], manifest['finished_at']
    last = started
    for i, event in enumerate(events, 1):
        at = event.get('at')
        if (type(event.get('sequence')) is not int or event['sequence'] != i
                or type(at) not in (float, int) or not math.isfinite(at)
                or not last <= at <= finished):
            raise ValueError('event order, sequence or run time window is invalid')
        last = at
        if event['event'] not in ('batch_started', 'batch_completed'):
            if (not isinstance(event.get('episode_id'), str) or not event['episode_id']
                    or not _digest(event.get('task_id'), 64)
                    or not _digest(event.get('attempt_id'), 32)):
                raise ValueError('event must bind an episode, task and execution attempt')
        if event['event'] in _COMPLETIONS:
            p, sha = event.get('artifact_path'), event.get('artifact_sha256')
            if not isinstance(p, str) or not _digest(sha, 64) or artifacts.get(p) != sha:
                raise ValueError('completion is not bound to its scenario artifact')
            if file_sha256(p) != sha:
                raise ValueError('completed artifact changed')
    if name in _RECOVERY:
        sequence = _RECOVERY[name]
        selected = [e for e in events if e['event'] in sequence]
        if [e['event'] for e in selected] != list(sequence):
            raise ValueError('recovery events are missing, repeated or out of causal order')
        first, ended = selected[0], next(e for e in selected if e['event']=='worker_terminated')
        begin, final = selected[-2:]
        if len({(e['episode_id'], e['task_id']) for e in selected}) != 1:
            raise ValueError('recovery crosses episode or task')
        original = selected[:-2]
        if (any(e['attempt_id'] != first['attempt_id'] for e in original)
                or begin['attempt_id'] != final['attempt_id']
                or begin['attempt_id'] == first['attempt_id']
                or begin.get('previous_attempt_id') != first['attempt_id']):
            raise ValueError('recovery attempt linkage is invalid')
        pid, created = first.get('pid'), first.get('process_created_at')
        if (type(pid) is not int or pid <= 0 or type(created) not in (int,float)
                or not math.isfinite(created) or created <= 0
                or (ended.get('pid'), ended.get('process_created_at')) != (pid, created)
                or type(ended.get('exit_code')) is not int):
            raise ValueError('terminated process identity/exit evidence is invalid')
        before, after = _budget(first), _budget(final)
        if (first.get('budget_path') != final.get('budget_path')
                or not isinstance(first.get('budget_path'), str)
                or before['limits'] != after['limits']
                or any(after['used'].get(k, -1) < v for k,v in before['used'].items())):
            raise ValueError('recovery replaced budget or erased spending')
        # Existing reserved calls cannot disappear or silently become new calls.
        originals = {r['id']:r for r in before['attempts']}
        later = {r['id']:r for r in after['attempts']}
        for key, row in originals.items():
            if key not in later or any(later[key].get(k) != row.get(k)
                    for k in ('kind','request_fingerprint','characters','reserved_at')):
                raise ValueError('recovery lost or rewrote a prior provider attempt')
    if name == 'unattended_worker_full_batch':
        if events[0]['event'] != 'batch_started' or events[-1]['event'] != 'batch_completed':
            raise ValueError('batch completion must bracket episode execution')
        batch = events[0].get('batch_id')
        if not isinstance(batch, str) or not batch or any(e.get('batch_id') != batch for e in events):
            raise ValueError('batch evidence mixes batch identities')
    completed = [e['episode_id'] for e in events if e['event'] in _COMPLETIONS]
    if name in ('multi_episode_current_rebuild', 'unattended_worker_full_batch'):
        if len(completed) < 2 or len(completed) != len(set(completed)):
            raise ValueError('batch requires distinct completed episodes, without duplicates')
    return sorted(set(completed))
