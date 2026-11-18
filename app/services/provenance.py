"""Content identities and atomic metadata writes (never include credentials)."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import time
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager
from functools import lru_cache

_hashes = ContextVar('status_hashes', default=None)


@contextmanager
def status_hashes(*, display=False):
    """Reuse unchanged files within one check. Only display reads share process memory."""
    token = _hashes.set(({}, display))
    try:
        yield
    finally:
        _hashes.reset(token)


def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _checked_digest(path, stamp):
    digest = _hash_file(path)
    if _stamp(path) != stamp:
        raise OSError('file changed while checking: '+str(path))
    return digest


_display_digest = lru_cache(maxsize=4096)(_checked_digest)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_sha256(path) -> str:
    session = _hashes.get()
    if session is None:
        return _hash_file(path)
    cache, display = session
    path = Path(path).absolute()
    stamp = _stamp(path)
    key = (path, stamp)
    if key not in cache:
        cache[key] = (_display_digest if display else _checked_digest)(path, stamp)
    return cache[key]


def _hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def replace_file(source, destination, timeout=2.0):
    """Windows readers/antivirus can briefly deny replacement of an open file."""
    deadline = time.monotonic()+timeout
    while True:
        try:
            os.replace(source,destination)
            return
        except PermissionError as exc:
            if os.name!='nt' or exc.winerror not in (5,32,33) or time.monotonic()>=deadline: raise
            time.sleep(.025)


def atomic_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
