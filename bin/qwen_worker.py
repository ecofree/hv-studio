"""Project-owned offline Qwen worker. Importing this file loads no model."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def gpu_lease(path):
    """Same OS byte-lock protocol as existing local Qwen installations."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b', buffering=0) as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


def event(**data):
    print('@@'+json.dumps(data, ensure_ascii=False), flush=True)


def validate_dispatch(job, contract_path):
    from app.services import qwen_voice as Q, studio_voice_contract as C
    from app.services.provenance import fingerprint
    req=Q.read(job['request_path']);bound=C.validate(req)
    if (bound is None or Q.local_path(req['voice_contract']['path'])!=Q.local_path(contract_path)
            or req.get('qwen_policy')!=Q.policy() or req['mode']!='custom'
            or len(req['text'])>bound['max_chunk_characters']):
        raise ValueError('request differs from the selected Qwen recipe')
    budget=Q.read(job['budget_path'])
    attempt=next((a for a in budget['attempts'] if a['id']==job['attempt_id']),None)
    if (not attempt or attempt['kind']!='tts' or attempt['status']!='reserved'
            or attempt['request_fingerprint']!=fingerprint(req) or attempt['characters']!=len(req['text'])):
        raise ValueError('Qwen worker requires the matching reserved budget attempt')
    return req


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', required=True)
    parser.add_argument('--dispatch', required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      CUBLAS_WORKSPACE_CONFIG=':4096:8')
    def no_network(*a, **kw):
        raise RuntimeError('Qwen production is offline')
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    from app.services import qwen_voice as Q, studio_voice_contract as C
    from app.services.provenance import fingerprint, file_sha256
    contract = C.load(args.contract)
    contract_sha256=file_sha256(Q.local_path(args.contract))
    validate_dispatch(Q.read(args.dispatch), args.contract)
    runtime = Q.runtime(contract)
    with gpu_lease(runtime['gpu_lock_path']):
        import numpy as np
        import safetensors
        import soundfile as sf
        import torch
        original = safetensors.safe_open
        def safe_open(filename, framework, device='cpu', *, backend='pread'):
            return original(filename, framework, device=device, backend=backend)
        safetensors.safe_open = safe_open
        from qwen_tts import Qwen3TTSModel
        torch.set_num_threads(4)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.use_deterministic_algorithms(True, warn_only=True)
        Q.seed_every_request(contract['seed'], np, torch)
        started = time.monotonic()
        model = Qwen3TTSModel.from_pretrained(runtime['model_path'], device_map='cuda:0',
                                             dtype=torch.float16, attn_implementation='sdpa')
        event(state='ready', policy=Q.policy(), load_seconds=round(time.monotonic()-started, 2))
        used_attempts=set()
        for line in sys.stdin:
            try:
                job = json.loads(line)
                if job.get('action') == 'close': break
                req = validate_dispatch(job,args.contract)
                if req['voice_contract']['sha256']!=contract_sha256:
                    raise ValueError('selected voice contract changed while the worker was running')
                attempt=(str(Q.local_path(job['budget_path'])),job['attempt_id'])
                if attempt in used_attempts:raise ValueError('budget attempt already dispatched; preserve its outcome')
                used_attempts.add(attempt)
                out = Q.local_path(job['output_path'])
                if out.exists(): raise ValueError('worker output already exists; preserve it for recovery')
                out.parent.mkdir(parents=True, exist_ok=True)
                wavs, sr = Q.infer(model, req, np, torch)
                sf.write(out, wavs[0], sr, subtype='PCM_16')
                event(state='done', request_fingerprint=fingerprint(req), effective_seed=req['seed'],
                      samples=len(wavs[0]), sample_rate=sr, audio_sha256=file_sha256(out))
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                event(state='error', error=f'{type(exc).__name__}: {exc}')
    return 0


if __name__ == '__main__': raise SystemExit(main())
