"""Validate persisted queue records before using paths, process IDs or budgets."""
import math
import re
from pathlib import Path
from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import fingerprint

STATUSES = {'QUEUED','RUNNING','RETRY_WAIT','NEEDS_REVIEW','BLOCKED_INPUT',
            'BUDGET_EXHAUSTED','FAILED','APPROVED'}


def integer(value, minimum=0):
    if type(value) is not int or value < minimum: raise ValueError('invalid integer in queue record')


def timestamp(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid queue timestamp')


def validate_job(job, path):
    if not isinstance(job,dict): raise ValueError('queue record must be an object')
    spec=job.get('spec')
    if not isinstance(spec,dict) or spec.get('batch_id') != path.stem:
        raise ValueError('batch specification does not match its file')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',spec['batch_id']): raise ValueError('invalid batch ID')
    ids=spec.get('episodes')
    if not isinstance(ids,list) or not ids: raise ValueError('episode list missing')
    for eid in ids: paths.episode_dir(eid)
    if len(set(ids))!=len(ids): raise ValueError('duplicate episode IDs')
    rows=job.get('episodes')
    if not isinstance(rows,dict) or set(rows)!=set(ids): raise ValueError('episode records do not match specification')
    options=spec.get('options')
    keys={'max_requests','max_characters','max_images'}
    # max_image_retries is optional (absent on old queue files); when present
    # it must be the only extra key.
    if not isinstance(options,dict) or set(options)-keys not in (set(), {'max_image_retries'}) \
            or not keys.issubset(set(options)): raise ValueError('invalid budget options')
    for value in options.values(): integer(value)
    integer(spec.get('timeout'),1);integer(spec.get('retries'))
    if spec['timeout']>14400 or spec['retries']>3: raise ValueError('invalid worker bounds')
    if not isinstance(job.get('events'),list): raise ValueError('invalid events')
    expected=path.parent/'budgets'/(fingerprint(['unattended',spec['batch_id']])+'.json')
    if not isinstance(job.get('budget_path'),str) or Path(job['budget_path']).resolve()!=expected.resolve():
        raise ValueError('budget path is not bound to this batch')
    ledger=read(expected)
    limits=dict(tts_requests=options['max_requests'],characters=options['max_characters'],image_requests=options['max_images'])
    if options.get('max_image_retries'):
        limits['image_retries'] = options['max_image_retries']
    if not isinstance(ledger,dict) or ledger.get('limits')!=limits: raise ValueError('budget ledger limits changed')
    used=ledger.get('used')
    if not isinstance(used,dict) or set(used)!=set(limits) or not isinstance(ledger.get('attempts'),list):
        raise ValueError('invalid budget ledger')
    for key,value in used.items():
        integer(value)
        if value>limits[key]: raise ValueError('budget usage exceeds limit')


def validate_row(row):
    if not isinstance(row,dict) or row.get('status') not in STATUSES: raise ValueError('invalid episode status')
    integer(row.get('attempts'))
    if 'authorized_attempt_limit' in row: integer(row['authorized_attempt_limit'],1)
    if 'retry_at' in row: timestamp(row['retry_at'])
    if row['status']=='RUNNING':
        if not isinstance(row.get('token'),str) or not re.fullmatch(r'[0-9a-f]{32}',row['token']):
            raise ValueError('invalid child token')
        # None/None is the valid interval between saving intent and spawning.
        if row.get('child_pid') is not None or row.get('child_created') is not None:
            integer(row.get('child_pid'),1);timestamp(row.get('child_created'))
    for key in ('candidate_path','error'):
        if key in row and not isinstance(row[key],str): raise ValueError('invalid '+key)
    if 'blockers' in row and (not isinstance(row['blockers'],list) or not all(isinstance(v,str) for v in row['blockers'])):
        raise ValueError('invalid blockers')


def load_result(path):
    value=read(path)
    if not isinstance(value,dict) or value.get('status') not in STATUSES-{'QUEUED','RUNNING'}:
        raise ValueError('invalid child result')
    validate_row(dict(value,attempts=0))
    # Child results cannot replace process ownership or the durable attempt count.
    return {k:v for k,v in value.items() if k in {'status','candidate_path','artifacts','identity','blockers','error'}}
