"""Explicit cleanup of completed V2 render intermediates; never source media."""
from pathlib import Path
import re

from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import file_sha256, fingerprint


def _inside(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('cleanup path escapes the episode')
    for part in (path, *path.parents):
        if part == root: break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('cleanup does not follow links')
    return path


def plan(ep_id, *, include_segments=False):
    folder = paths.episode_dir(ep_id)
    root = _inside(folder/'production/v2/renders', folder)
    items, skipped = [], []
    for render in sorted(root.glob('*')):
        if not re.fullmatch(r'[0-9a-f]{32}', render.name): continue
        try:
            _inside(render, folder)
            state_path, receipt_path = render/'render_state.json', render/'candidate.json'
            state, receipt = read(state_path), read(receipt_path)
            candidate = _inside(render/'candidate.mp4', folder)
            if (state.get('status') != 'completed' or not candidate.is_file()
                    or Path(receipt['candidate_path']).resolve() != candidate.resolve()
                    or state.get('candidate_sha256') != receipt.get('candidate_sha256')):
                raise ValueError('completed candidate evidence missing')
            names = [render/'concat.video.mp4']
            if include_segments:
                names += [render/'segments'/f for f in sorted(p.name for p in (render/'segments').glob('seg-*.mp4'))
                          if re.fullmatch(r'seg-\d+\.mp4', f)]
            for p in names:
                _inside(p, folder)
                if not p.is_file(): continue
                stat = p.stat()
                items.append(dict(path=str(p.resolve()),size=stat.st_size,mtime_ns=stat.st_mtime_ns,
                    tier='segments' if p.parent.name=='segments' else 'concat',
                    receipt=str(receipt_path.resolve()),receipt_sha256=file_sha256(receipt_path),
                    candidate=str(candidate.resolve()),candidate_sha256=receipt['candidate_sha256']))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            skipped.append(dict(path=str(render),reason=str(exc)))
    body = dict(episode_id=ep_id,include_segments=bool(include_segments),items=items,skipped=skipped,
                total_bytes=sum(i['size'] for i in items))
    return dict(body,plan_sha256=fingerprint(body))


def apply(ep_id, preview):
    """Use the same episode lock as production and recheck the displayed plan."""
    from app.services.production_storage import episode_execution
    if preview.get('episode_id') != ep_id: raise ValueError('cleanup episode changed')
    with episode_execution(ep_id):
        current = plan(ep_id,include_segments=preview['include_segments'])
        if current != preview: raise ValueError('cleanup list changed; refresh the preview')
        # Check every candidate before deleting anything. No encoding/decoding.
        for candidate, sha in {(i['candidate'],i['candidate_sha256']) for i in current['items']}:
            if file_sha256(candidate) != sha: raise ValueError('candidate changed; intermediates retained')
        deleted, total = 0, 0
        for item in current['items']:
            p = _inside(item['path'],paths.episode_dir(ep_id))
            if p.stat().st_size != item['size'] or p.stat().st_mtime_ns != item['mtime_ns']:
                raise ValueError('intermediate changed; remaining files retained')
            p.unlink()
            deleted += 1; total += item['size']
        return dict(deleted=deleted,deleted_bytes=total)
