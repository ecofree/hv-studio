"""Durable, shared limits at the actual provider-call boundary.

Reservations are charged before dispatch and never refunded after an ambiguous
failure or process death. A retry is a new billable attempt.
"""
from contextlib import contextmanager
import time
import uuid
from pathlib import Path
from app.services.provenance import atomic_json, fingerprint
from app.services.narration_pipeline import read


class BudgetExceeded(RuntimeError): pass


@contextmanager
def ledger_lock(path):
    from app.services.process_lock import file_lock
    deadline = time.monotonic()+10
    while True:
        lock = file_lock(Path(path).with_suffix('.budget.lock'))
        try: lock.__enter__(); break
        except RuntimeError:
            if time.monotonic() >= deadline: raise
            time.sleep(.03)
    try: yield
    finally: lock.__exit__(None, None, None)


class ProviderBudget:
    def __init__(self, path, *, tts_requests=0, characters=0, image_requests=0):
        self.path = Path(path)
        self.limits = dict(tts_requests=tts_requests, characters=characters, image_requests=image_requests)
        if any(type(v) is not int or v < 0 for v in self.limits.values()): raise ValueError('invalid budget limits')
        with ledger_lock(self.path):
            if self.path.exists():
                if read(self.path)['limits'] != self.limits: raise ValueError('budget limits changed; use a new explicit budget ID')
            else: atomic_json(self.path, dict(limits=self.limits, used=dict.fromkeys(self.limits, 0), attempts=[]))

    def reserve(self, kind, request_fingerprint, characters=0):
        if kind not in ('tts', 'image') or type(characters) is not int or characters < 0:
            raise ValueError('invalid provider reservation')
        charge = {kind+'_requests': 1}
        if kind == 'tts': charge['characters'] = characters
        with ledger_lock(self.path):
            state = read(self.path)
            for key, value in charge.items():
                if state['used'][key]+value > self.limits[key]:
                    raise BudgetExceeded(f'provider budget exhausted: {key} ({state["used"][key]}/{self.limits[key]})')
            for key, value in charge.items(): state['used'][key] += value
            token = uuid.uuid4().hex
            state['attempts'].append(dict(id=token, kind=kind, request_fingerprint=request_fingerprint,
                                          characters=characters, reserved_at=time.time(), status='reserved'))
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
                if state['used'][key]+count>self.limits[key]: raise BudgetExceeded('remaining provider budget insufficient: '+key)


def for_run(root, identifier, options):
    return ProviderBudget(Path(root)/'budgets'/(fingerprint(identifier)+'.json'),
                          tts_requests=options.get('max_requests', 0), characters=options.get('max_characters', 0),
                          image_requests=options.get('max_images', 0))
