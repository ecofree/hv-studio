"""Import an immutable editorial revision and a zero-cost narration plan.

Existing episode source, audio and media bindings are preserved.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.services.script_handoff import ACT_HEAD, derive, write_revision
from app.services.provenance import atomic_json
from app.services.narration_pipeline import prepare


def build_narration(text: str) -> tuple[str, dict]:
    payloads = derive(text)
    cues = json.loads(payloads['directions.json'])['cues']
    return payloads['narration.md'].decode('utf-8'), {
        'dropped': [], 'missing': [], 'directions': len(cues),
        'extraction': json.loads(payloads['extraction.json']),
    }


def import_revision(episode: str, source: Path, root: Path = ROOT) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', episode):
        raise ValueError('Invalid episode id')
    text = Path(source).read_bytes().decode('utf-8')
    from app import paths
    dst = paths.episode_dir(episode) if Path(root).resolve() == paths.ROOT.resolve() else Path(root) / 'episodes' / episode
    target = write_revision(text, dst / 'editorial' / 'revisions', {'episode': episode})
    plan = prepare(target / 'narration.md', dst / 'editorial' / 'plans')
    pointer = {'schema_version': '1', 'revision': str(target.resolve()),
               'source': str((target / 'source.md').resolve()),
               'plan': str(plan.resolve()), 'status': 'editorial_candidate',
               'active_production_unchanged': True, 'audio_binding': 'new_recording_required'}
    atomic_json(dst / 'editorial' / 'current.json', pointer)
    return pointer


def main() -> int:
    ap = argparse.ArgumentParser(description='writing-DNA → 可校验的新文案版本及旁白计划（零调用）')
    ap.add_argument('--ep', required=True)
    ap.add_argument('--src', required=True)
    args = ap.parse_args()
    try:
        source = Path(args.src)
        result = import_revision(args.ep, source if source.is_absolute() else ROOT / source)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
