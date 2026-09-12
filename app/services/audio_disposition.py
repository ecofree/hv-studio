"""Content-bound rejection records shared by caches, rendering and publication."""
from datetime import datetime,timezone
import json
from pathlib import Path
from app import paths
from app.services.provenance import atomic_json,file_sha256


def rejection(audio_sha256):
    if not audio_sha256:return None
    path=paths.state_dir()/'audio_rejections'/(audio_sha256+'.json')
    if not path.exists():return None
    record=json.loads(path.read_text(encoding='utf-8'))
    if record.get('decision')!='rejected' or audio_sha256 not in record.get('hashes',[]):
        raise ValueError('invalid audio rejection record')
    return record


def take_rejected(take):
    return bool(rejection(take.get('audio_sha256')) or rejection(take.get('raw_sha256')))


def require_usable(manifest):
    if any(take_rejected(take) for take in manifest['takes']):
        raise ValueError('audio rejected by listening review; replace the rejected take')


def reject_take(take,reviewer,findings,*,evidence_path=None):
    if not reviewer.strip() or not findings.strip():raise ValueError('reviewer and findings required')
    for kind in ('audio','raw'):
        if file_sha256(take[kind+'_path'])!=take[kind+'_sha256']:
            raise ValueError('audio changed before rejection')
    record={'decision':'rejected','reviewer':reviewer,'findings':findings,
            'reviewed_at':datetime.now(timezone.utc).isoformat(),
            'hashes':sorted({take['audio_sha256'],take['raw_sha256']}),
            'unit_id':take['unit_id']}
    if evidence_path:
        record.update(evidence_path=str(Path(evidence_path).resolve()),evidence_sha256=file_sha256(evidence_path))
    outputs=[]
    for sha in record['hashes']:
        path=paths.state_dir()/'audio_rejections'/(sha+'.json')
        if not path.exists():atomic_json(path,record)
        rejection(sha)  # Existing rejection cannot be silently cleared or overwritten.
        outputs.append(path)
    return outputs
