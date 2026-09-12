"""Content identities and atomic metadata writes (never include credentials)."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import time
from pathlib import Path


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_sha256(path) -> str:
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
