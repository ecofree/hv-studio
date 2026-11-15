"""Bind real-provider acceptance to actual ledger attempts and output receipts."""
from pathlib import Path
from app.services.narration_pipeline import read
from app.services.provenance import file_sha256


def verify(event, verify_file):
    kind=event.get('provider_kind')
    if kind not in ('tts','image') or event.get('is_mock') is not False:
        raise ValueError('non-mock provider receipt required for TTS and images')
    receipt=read(verify_file(event.get('receipt_path'),event.get('receipt_sha256'),'provider receipt'))
    ledger=read(verify_file(event.get('budget_receipt_path'),event.get('budget_receipt_sha256'),'provider budget'))
    from app.services.provider_budget import validate_usage
    validate_usage(ledger['limits'],ledger['used'])
    if receipt.get('budget_path')!=event.get('budget_path') or not event.get('budget_path'):
        raise ValueError('provider ledger path differs')
    execution=dict(task_id=event['task_id'],attempt_id=event['attempt_id'],task_sha256=event['task_sha256'])
    matches=[r for r in ledger.get('attempts',[]) if r.get('id')==event.get('provider_attempt_id')]
    if (len(matches)!=1 or matches[0].get('status')!='succeeded' or matches[0].get('kind')!=kind
            or matches[0].get('execution')!=execution or receipt.get('execution')!=execution):
        raise ValueError('provider success is not bound to this paid execution')
    if receipt.get('model')!=event.get('model') or not event.get('model'):
        raise ValueError('provider model differs')
    output=verify_file(event.get('output_path'),event.get('output_sha256'),'provider output')
    if kind=='tts':
        if (receipt.get('status')!='succeeded' or receipt.get('request_id')!=event.get('request_id')
                or not event.get('request_id') or receipt.get('budget_attempt_id')!=event['provider_attempt_id']
                or receipt.get('audio_sha256')!=event['output_sha256']):
            raise ValueError('TTS receipt differs from output or dispatch')
    else:
        install=read(verify_file(event.get('install_path'),event.get('install_sha256'),'image installation'))
        if (receipt.get('attempt_id')!=event['provider_attempt_id'] or not receipt.get('response')
                or receipt.get('channel')!=event.get('provider')
                or install.get('request_fingerprint')!=receipt.get('request_fingerprint')
                or install.get('output_sha256')!=event['output_sha256']):
            raise ValueError('image output is not bound to returned response')
    return kind


def capture(capture, cases, budget):
    """Observe only successful calls made in this acceptance run, not old cache."""
    from app import paths
    from app.services.acceptance_capture import budget_snapshot
    ledger=budget.snapshot()
    attempts={r['id']:r for r in ledger['attempts'] if r.get('status')=='succeeded'
              and r.get('reserved_at',0)>=capture.manifest['started_at'] and r.get('execution')}
    snapshot=budget_snapshot(budget.path,capture.folder)
    seen=set()
    for case in cases:
        folder=paths.episode_dir(case['episode_id'])/'production/v2'
        # Read only this explicitly scoped acceptance episode's output tree.
        audio={file_sha256(p):p for p in folder.rglob('*.wav')}
        receipts=list(folder.rglob('effective_request.json'))
        receipts += list(folder.rglob('*.response.json'))
        for p in receipts:
            receipt=read(p);key=receipt.get('budget_attempt_id',receipt.get('attempt_id'))
            row=attempts.get(key)
            if row is None or key in seen:continue
            execution=row['execution'];kind=row['kind']
            if receipt.get('execution')!=execution:continue
            extras={}
            if kind=='tts':
                out=audio.get(receipt.get('audio_sha256'))
                if out is None:continue
            else:
                out=Path(str(p)[:-len('.response.json')]);install=out.with_suffix(out.suffix+'.request.json')
                if not out.is_file() or not install.is_file():continue
                extras=dict(install_path=str(install),install_sha256=file_sha256(install))
            task=dict(episode_id=case['episode_id'],task_id=execution['task_id'])
            capture.artifacts[str(out.resolve())]=file_sha256(out)
            capture.emit('provider_request_succeeded',task=task,attempt=execution['attempt_id'],
                task_sha256=execution['task_sha256'],provider_kind=kind,is_mock=False,
                provider=receipt.get('channel','dashscope'),model=receipt.get('model'),
                request_id=receipt.get('request_id'),provider_attempt_id=key,
                receipt_path=str(p.resolve()),receipt_sha256=file_sha256(p),
                output_path=str(out.resolve()),output_sha256=file_sha256(out),**snapshot,**extras)
            seen.add(key)
