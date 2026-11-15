"""Durable, shared limits at the actual provider-call boundary.

Reservations are charged before dispatch and never refunded after an ambiguous
failure or process death. A retry is a new billable attempt.
"""
from contextlib import contextmanager, nullcontext
from copy import copy, deepcopy
from contextvars import ContextVar
import time
import uuid
from pathlib import Path
from app.services.provenance import atomic_json, fingerprint
from app.services.narration_pipeline import read


class BudgetExceeded(RuntimeError): pass


_image_budget = ContextVar('image_budget', default=None)
_image_receipt = ContextVar('image_receipt', default=None)
_image_guard = ContextVar('image_guard', default=None)
_tts_budget = ContextVar('tts_budget', default=None)


@contextmanager
def tts_budget_scope(budget):
    scope = budget.execution_scope() if isinstance(budget, ProviderBudget) else nullcontext()
    with scope:
        token = _tts_budget.set(budget)
        try: yield
        finally: _tts_budget.reset(token)


def current_tts_budget():
    budget = _tts_budget.get()
    if not isinstance(budget, ProviderBudget):
        raise BudgetExceeded('TTS dispatch requires an explicit durable ProviderBudget')
    return budget


@contextmanager
def image_budget_scope(budget, response_path=None, request_fp=None, guard=None):
    # Image workers are separate threads. The scoped handle carries only the
    # verified task contract, not arbitrary caller ContextVars or mutable timers.
    scope = budget.execution_scope() if isinstance(budget, ProviderBudget) else nullcontext()
    with scope:
        token = _image_budget.set(budget)
        receipt_token = _image_receipt.set((response_path, request_fp) if response_path else None)
        guard_token = _image_guard.set(guard)
        try: yield
        finally:
            _image_guard.reset(guard_token)
            _image_receipt.reset(receipt_token)
            _image_budget.reset(token)


def current_image_budget():
    budget = _image_budget.get()
    if not isinstance(budget, ProviderBudget):
        raise BudgetExceeded('image dispatch requires an explicit durable ProviderBudget')
    return budget


@contextmanager
def ledger_lock(path):
    from app.services.process_lock import wait_file_lock
    with wait_file_lock(Path(path).with_suffix('.budget.lock'),timeout=10):
        yield



class ProviderBudget:
    def __init__(self, path, *, tts_requests=0, characters=0, image_requests=0,
                 image_retries=0):
        self.path = Path(path)
        self.limits = dict(tts_requests=tts_requests, characters=characters, image_requests=image_requests)
        # Compatibility: legacy image_retries extends the TOTAL attempt cap.
        # Both new requests and repeats consume that same cap; no separate pool.
        if type(image_retries) is not int or image_retries < 0:
            raise ValueError('invalid budget limits')
        if image_retries:
            self.limits['image_retries'] = image_retries
        if any(type(v) is not int or v < 0 for v in self.limits.values()): raise ValueError('invalid budget limits')
        from app.services.production_identity import require_task_budget, current_task, current_execution
        require_task_budget(self.path, self.limits)
        self._task_binding = (current_task(), current_execution())
        with ledger_lock(self.path):
            if self.path.exists():
                if read(self.path)['limits'] != self.limits: raise ValueError('budget limits changed; use a new explicit budget ID')
            else: atomic_json(self.path, dict(limits=self.limits, used=dict.fromkeys(self.limits, 0), attempts=[]))

    def for_current_task(self):
        """Make a scoped handle, preserving the exact ledger and authorization.

        Never mutate the caller's handle or construct/refill another ledger.
        Explicit rebinding must happen inside the new authorized task scope.
        """
        from app.services.production_identity import (
            current_task, current_execution, require_task_budget, TaskMismatch,
        )
        task, execution = current_task(), current_execution()
        if task is None:
            raise TaskMismatch('budget rebinding requires an active task')
        require_task_budget(self.path, self.limits)
        scoped = copy(self)
        scoped.limits = deepcopy(self.limits)
        scoped._task_binding = (task, execution)
        return scoped

    @contextmanager
    def execution_scope(self):
        """Restore this handle's task in a worker, refusing cross-attempt reuse."""
        from app.services.production_identity import (
            current_execution, require_execution, require_task_budget, task_scope,
        )
        task, execution = self._task_binding
        if task is None:
            yield
        elif current_execution() is not None:
            require_execution(current_execution(), execution)
            require_task_budget(self.path, self.limits)
            yield
        else:
            with task_scope(task, execution['attempt_id']):
                require_task_budget(self.path, self.limits)
                yield

    def reserve(self, kind, request_fingerprint, characters=0, *, supplier=None, queue_seconds=None):
        with self.execution_scope():
            return self._reserve(kind, request_fingerprint, characters,
                                 supplier=supplier, queue_seconds=queue_seconds)

    def _reserve(self, kind, request_fingerprint, characters=0, *, supplier=None, queue_seconds=None):
        if kind not in ('tts', 'image') or type(characters) is not int or characters < 0:
            raise ValueError('invalid provider reservation')
        from app.services.production_identity import require_task_budget
        require_task_budget(self.path, self.limits)
        charge = {kind+'_requests': 1}
        if kind == 'tts': charge['characters'] = characters
        with ledger_lock(self.path):
            state = read(self.path)
            for key, value in charge.items():
                cap = self.limits[key]
                if key == 'image_requests':
                    cap += self.limits.get('image_retries', 0)
                if state['used'][key]+value > cap:
                    raise BudgetExceeded(
                        f'provider budget exhausted: {key} '
                        f'({state["used"][key]}/{self.limits[key]}'
                        f'{("+"+str(self.limits.get("image_retries", 0))+" retries") if key == "image_requests" and self.limits.get("image_retries", 0) else ""})')
            for key, value in charge.items(): state['used'][key] += value
            token = uuid.uuid4().hex
            state['attempts'].append(dict(id=token, kind=kind, request_fingerprint=request_fingerprint,
                                          characters=characters, reserved_at=time.time(), status='reserved'))
            from app.services.production_identity import current_execution
            execution = current_execution()
            if execution is not None:
                state['attempts'][-1]['execution'] = execution
            if supplier is not None:
                state['attempts'][-1].update(supplier=supplier,queue_seconds=round(queue_seconds or 0.,3))
            atomic_json(self.path, state)
        return token

    def finish(self, token, succeeded):
        with ledger_lock(self.path):
            state = read(self.path)
            row = next(r for r in state['attempts'] if r['id'] == token)
            row.update(status='succeeded' if succeeded else 'failed_or_uncertain', completed_at=time.time())
            atomic_json(self.path, state)

    def snapshot(self): return read(self.path)

    def check_capacity(self, *, tts_requests=0, characters=0, image_requests=0):
        requested=dict(tts_requests=tts_requests, characters=characters, image_requests=image_requests)
        with ledger_lock(self.path):
            state=read(self.path)
            for key, count in requested.items():
                if count and state['used'][key]+count>self.limits[key]+(self.limits.get('image_retries',0) if key=='image_requests' else 0): raise BudgetExceeded(
                    'remaining provider budget insufficient: '+key
                    +f' ({state["used"][key]}/{self.limits[key]} used)'
                    +(' (legacy image_retries is included in the total attempt allowance)'
                       if key == 'image_requests' and self.limits.get('image_retries', 0) else ''))


def budget_path_for(root, identifier):
    """Pure address calculation; never creates or refills a ledger."""
    return Path(root)/'budgets'/(fingerprint(identifier)+'.json')


def budget_limits(options):
    limits = dict(tts_requests=options.get('max_requests', 0),
                  characters=options.get('max_characters', 0),
                  image_requests=options.get('max_images', 0))
    retries = options.get('max_image_retries', 0)
    if type(retries) is not int or retries < 0:
        raise ValueError('invalid budget limits')
    if retries: limits['image_retries'] = retries
    if any(type(v) is not int or v < 0 for v in limits.values()):
        raise ValueError('invalid budget limits')
    return limits


def for_run(root, identifier, options):
    return ProviderBudget(budget_path_for(root, identifier), **budget_limits(options))
