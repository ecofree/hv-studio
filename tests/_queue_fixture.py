"""Offline child commands that obey the real versioned result contract."""
import json
import sys
from pathlib import Path
from app.services.narration_pipeline import read


def result_command(spec, eid, token, payload, *, pause=None):
    if isinstance(payload, dict):
        payload = dict(payload)
        if 'execution' not in payload:
            payload['execution'] = read(spec)['episodes'][eid].get('execution')
    target = Path(spec).parent/'results'/(token+'.json')
    code = ('import json,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); '
            'p.parent.mkdir(exist_ok=True); '
            'pause=sys.argv[3]; '
            'pathlib.Path(pause).write_text("pause requested") if pause else None; '
            'time.sleep(0.1) if pause else None; '
            'p.write_text(sys.argv[2],encoding="utf-8")')
    return [sys.executable, '-c', code, str(target), json.dumps(payload), str(pause) if pause else '']
