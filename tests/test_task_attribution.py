"""Merge reconciliation: preserve the remote contract and add missing attribution."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from app.services import production_identity as I, production_metrics as M
from app.services import image as images
from app.services.provider_budget import ProviderBudget, image_budget_scope
from app.services.provenance import file_sha256
from test_image_routing import generator, job, response

pytestmark = pytest.mark.production


def task_for(budget, owner='one', stages=('script',)):
    return I.make_task('task-attribution', origin='batch', owner=owner,
                      request={'work': list(stages)}, inputs={'source': 'fixture'},
                      budget_path=budget.path, limits=budget.limits, stages=stages)


def test_ledger_links_only_new_attempts_without_relabeling_or_refilling(tmp_path):
    budget = ProviderBudget(tmp_path/'budget.json', tts_requests=3, characters=30)
    budget.reserve('tts', 'legacy-request', 1)
    legacy = budget.snapshot()['attempts'][0].copy()
    task = task_for(budget)
    with I.task_scope(task, 'a'*32) as first:
        bound = budget.for_current_task()
        token = bound.reserve('tts', 'new-one', 1)
        bound.finish(token, False)
    with I.task_scope(task, 'b'*32) as second:
        budget.reserve('tts', 'new-two', 1)
    state = budget.snapshot()
    assert state['attempts'][0] == legacy and 'execution' not in legacy
    assert state['attempts'][1]['execution'] == first
    assert state['attempts'][2]['execution'] == second
    assert state['attempts'][1]['status'] == 'failed_or_uncertain'
    assert state['used']['tts_requests'] == 3 and state['used']['characters'] == 3
    assert budget._task_binding == (None, None)
    assert state['limits'] == budget.limits


def test_legacy_call_remains_unattributed(tmp_path):
    budget = ProviderBudget(tmp_path/'legacy.json', image_requests=1)
    budget.reserve('image', 'legacy')
    assert 'execution' not in budget.snapshot()['attempts'][0]


def test_scope_captured_at_construction_survives_worker_and_is_cleaned(tmp_path):
    path = tmp_path/'budget.json'
    task = I.make_task('task-attribution', origin='studio', owner='one', request={}, inputs={},
                      budget_path=path, limits=dict(tts_requests=0, characters=0, image_requests=1),
                      stages=['images'])
    with I.task_scope(task, 'c'*32) as ref:
        budget = ProviderBudget(path, image_requests=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(budget.reserve, 'image', 'worker').result(timeout=10)
        assert executor.submit(I.current_execution).result(timeout=10) is None
    assert budget.snapshot()['attempts'][0]['execution'] == ref
    assert I.current_execution() is None


def test_scoped_handle_cannot_leak_into_another_attempt(tmp_path):
    budget = ProviderBudget(tmp_path/'shared.json', tts_requests=2, characters=10)
    task = task_for(budget)
    with I.task_scope(task, 'a'*32):
        first = budget.for_current_task()
    before = file_sha256(budget.path)
    with I.task_scope(task, 'b'*32) as current:
        with pytest.raises(I.TaskMismatch, match='different task or execution'):
            first.reserve('tts', 'wrong-attempt', 1)
        assert file_sha256(budget.path) == before
        rebound = first.for_current_task()
        rebound.reserve('tts', 'explicitly-rebound', 1)
    assert budget.snapshot()['attempts'][0]['execution'] == current
    assert first._task_binding[1]['attempt_id'] == 'a'*32


def test_rebinding_requires_current_task_and_does_not_create_ledger(tmp_path):
    budget = ProviderBudget(tmp_path/'budget.json', image_requests=1)
    before = file_sha256(budget.path)
    with pytest.raises(I.TaskMismatch, match='active task'):
        budget.for_current_task()
    assert file_sha256(budget.path) == before


def test_worker_rejects_substituted_ledger_before_spending(tmp_path):
    budget = ProviderBudget(tmp_path/'budget.json', image_requests=1)
    other = ProviderBudget(tmp_path/'other.json', image_requests=1)
    with I.task_scope(task_for(budget), 'a'*32):
        scoped = budget.for_current_task()
    scoped.path = other.path
    before = file_sha256(other.path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(I.TaskMismatch, match='budget differs'):
            executor.submit(scoped.reserve, 'image', 'wrong-ledger').result(timeout=10)
    assert file_sha256(other.path) == before


def test_image_scope_cleans_task_context_on_exception(tmp_path):
    budget = ProviderBudget(tmp_path/'budget.json', image_requests=1)
    with I.task_scope(task_for(budget), 'a'*32) as execution:
        scoped = budget.for_current_task()
    assert I.current_execution() is None
    with pytest.raises(RuntimeError, match='fixture failure'):
        with image_budget_scope(scoped):
            assert I.current_execution() == execution
            raise RuntimeError('fixture failure')
    assert I.current_execution() is None
    assert scoped.snapshot()['used']['image_requests'] == 0


def test_actual_image_threads_keep_task_attribution_and_cache_cost(generator, tmp_path, monkeypatch):
    budget = ProviderBudget(tmp_path/'images.json', image_requests=2)
    task = task_for(budget, stages=('images',))
    barrier = threading.Barrier(2)
    observed = []
    payload = response()
    def post(*args):
        observed.append((threading.get_ident(), I.current_execution()))
        barrier.wait(timeout=10)
        return payload
    monkeypatch.setattr(generator, '_post_unlocked', post)
    jobs = [job(tmp_path, 'attribution-first'), job(tmp_path, 'attribution-second')]
    with I.task_scope(task, 'd'*32) as execution:
        scoped = budget.for_current_task()
        result = images.generate_many(jobs, workers=2, budget=scoped, progress=False)
        again = images.generate_many(jobs, workers=2, budget=scoped, progress=False)
    assert all(result[j.id] and again[j.id] for j in jobs)
    assert len(observed) == 2 and len({tid for tid, _ in observed}) == 2
    assert all(ref == execution for _, ref in observed)
    assert all(row['execution'] == execution for row in budget.snapshot()['attempts'])
    assert budget.snapshot()['used']['image_requests'] == 2
    assert budget._task_binding == (None, None) and I.current_execution() is None


def test_stage_timings_snapshot_execution_without_claiming_success(tmp_path):
    budget = ProviderBudget(tmp_path/'budget.json')
    with I.task_scope(task_for(budget), 'a'*32) as execution:
        with M.measure() as timer:
            timer.begin('script')
            ref = I.current_execution(); ref['attempt_id'] = 'b'*32
            timer.finish()
    assert timer.rows[0]['execution'] == execution
    assert 'approved' not in timer.rows[0] and 'passed' not in timer.rows[0]
    assert budget.snapshot()['attempts'] == []
    with M.measure() as legacy:
        legacy.begin('legacy')
    assert 'execution' not in legacy.rows[0]


def test_v2_decorator_scopes_preexisting_budget_without_mutating_it(tmp_path):
    # Exercise the real decorator without invoking source/media work.
    @I.task_bound_run
    def run(ep_id, stages=None, script_md=None, *, max_requests=0, max_characters=0,
            max_images=0, timeline=None, voice_asset=None, timeout=300, budget=None,
            budget_id=None, image_keys=None, external_master=None, max_image_retries=0,
            provider_attempts_per_image=None, progress=None, audio_mode='auto'):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(budget.reserve, 'tts', 'v2-worker', 1).result(timeout=10)
        return I.current_execution()
    budget = ProviderBudget(tmp_path/'v2.json', tts_requests=2, characters=10)
    with I.task_scope(task_for(budget), 'e'*32) as inherited:
        assert run('task-attribution', stages=['script'], budget=budget) == inherited
    direct = run('task-attribution', stages=['script'], script_md='fixture', budget=budget)
    assert direct['task_id'] != inherited['task_id']
    assert [r['execution'] for r in budget.snapshot()['attempts']] == [inherited, direct]
    assert budget._task_binding == (None, None)
