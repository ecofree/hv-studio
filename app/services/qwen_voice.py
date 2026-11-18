"""One effective Qwen recipe for new production; archived confirmations stay intact.

This policy repairs execution, not speaker selection. Historical requests remain
verifiable under their original contract and are never relabelled as new takes.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from app import paths
from app.services.provenance import file_sha256, fingerprint
from app.services.studio_workspace import asset_path

VERSION = 'qwen-series-v2'
SEED_POLICY = 'series-and-exact-text-v1'
CODE_ROOT = Path(__file__).resolve().parents[2]
AUDITION_ONLY = '第一段松弛地铺陈，第二段在“倒过来的”轻落重音，第三段讲清通关规矩和越境代价。'


def local_path(value):
    """Resolve archived aliases locally before stat/read, never fall back."""
    return asset_path(value)


def read(value):
    return json.loads(local_path(value).read_text(encoding='utf-8'))


def effective(contract):
    result = copy.deepcopy(contract)
    # Only the known audition sentence is removed. Other confirmed directions,
    # all source words, model, speaker, generation settings and speed survive.
    if (result.get('speaker', '').casefold() == 'dylan'
            and result.get('model') == 'Qwen3-TTS-12Hz-1.7B-CustomVoice'):
        result['instruction'] = result['instruction'].replace(AUDITION_ONLY, '')
    return result


def policy():
    return dict(version=VERSION, seed_policy=SEED_POLICY,
                worker_sha256=file_sha256(CODE_ROOT/'bin/qwen_worker.py'),
                policy_sha256=file_sha256(CODE_ROOT/'app/services/qwen_voice.py'),
                device='cuda:0', dtype='float16', attention='sdpa',
                language='chinese', non_streaming_mode=True)


def segment_seed(contract, text):
    c = effective(contract)
    # No episode ID, segment ordinal, cache order or absolute filename: a
    # restart or a pickup of the same words must not move the RNG stream.
    identity = [SEED_POLICY, c['seed'], c['model'], c['speaker'], c['mode'],
                c['instruction'], c['generation'], text]
    return int(fingerprint(identity)[:8], 16)


def bind_request(req, contract):
    c = effective(contract)
    return dict(req, instruction=c['instruction'], seed=segment_seed(c, req['text']),
                series_seed=c['seed'], qwen_policy=policy())


def runtime(contract):
    """Read-only path resolution. Does not import torch or start a service."""
    engine = read(contract['engine_manifest_path'])
    model = local_path(contract['model_path'])
    # Archived runtime_python and worker_path are evidence, never executables.
    # The project-owned interpreter and worker are the only production route.
    python = paths.ROOT/'.venv-qwen3-tts/Scripts/python.exe'
    if not python.is_file():
        raise ValueError('本项目缺少 .venv-qwen3-tts；不会借用原项目的配音环境')
    if not model.is_dir():
        raise ValueError('本地 Qwen 模型缺失：'+str(model))
    from app.services.studio_workspace import _relative_path
    files = {}
    for name, digest in engine['model_files'].items():
        relative = _relative_path(name, contract['model_path'])
        candidate = model.joinpath(*relative) if relative is not None else local_path(name)
        if not candidate.resolve().is_relative_to(model):
            raise ValueError('声音模型清单包含模型目录之外的文件')
        files[str(candidate)] = digest
    if not files:
        raise ValueError('声音模型缺少文件指纹')
    return dict(runtime_python=str(python.resolve()), model_path=str(model),
                worker_path=str(paths.ROOT/'bin/qwen_worker.py'),
                model_files=files, runtime=engine['runtime'],
                gpu_lock_path=str(model.parent/'qwen-gpu.lock'))


def reference_files(contract):
    """Only hash-bound clips from the existing selected voice confirmation.

    No first-generated-segment fallback: a drifting new take must never become
    the reference for the rest of a series.
    """
    c = contract
    if c.get('version') == 2:
        scheme = read(c['scheme_path'])
        c = read(scheme['origin_path'])
    review = read(c['confirmation_path'])
    result = []
    for row in review.get('files', []):
        if not isinstance(row, dict) or not row.get('path') or not row.get('sha256'):
            continue
        result.append(dict(path=str(local_path(row['path'])), sha256=row['sha256']))
    return result


def seed_every_request(seed, numpy, torch):
    """Called immediately before every inference, including resumed pickups."""
    import random
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError('invalid per-segment Qwen seed')
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def infer(model, req, numpy, torch):
    """Small injectable boundary; offline tests use a fake model only."""
    seed_every_request(req['seed'], numpy, torch)
    return model.generate_custom_voice(
        text=req['text'], speaker=req['speaker'], language=req['language'],
        instruct=req['instruction'], non_streaming_mode=True, **req['generation'])
