"""Serialize production storage claims across CLI, API and the durable worker."""
from functools import wraps
from pathlib import Path
import math
import tempfile


def production_storage(fn):
    @wraps(fn)
    def guarded(ep_id, *args, **kwargs):
        from app.paths import state_dir, episode_dir
        from app.managers import load
        from app.orchestrator import _load_script_md, _check_disk_space
        from app.services.process_lock import file_lock
        # One local render/generation episode at a time. Per-supplier image jobs
        # inside that episode remain parallel. No deletion of historical assets.
        episode_dir(ep_id)
        with file_lock(state_dir()/'production-storage.lock'):
            text = kwargs.get('script_md')
            if text is None: text = _load_script_md(load(ep_id))
            seconds = max(60, len(text)/3)
            # 15-minute measured working set ~2.4 GB, with double headroom.
            need_mb = max(2500, math.ceil(seconds * 6 + 512))
            root = episode_dir(ep_id)
            while not root.exists(): root = root.parent
            _check_disk_space(root, need_mb=need_mb)
            _check_disk_space(Path(tempfile.gettempdir()), need_mb=need_mb)
            return fn(ep_id, *args, **kwargs)
    return guarded
