"""Executable acceptance gates. No providers, media deletion or fake approvals."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tests', action='store_true')
    parser.add_argument('--suite', choices=['production','shared','legacy','all'], default='production')
    parser.add_argument('--output', default='state/acceptance/latest.json')
    args = parser.parse_args()
    from app.services.provenance import atomic_json, file_sha256, fingerprint
    from app.services.local_asr import runtime_versions
    checks = {}
    try:
        checks['pinned_core'] = runtime_versions(Path(sys.executable), ROOT/'requirements.core.lock.txt')
        pip = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True)
        checks['pip_check'] = dict(exit_code=pip.returncode, output=pip.stdout.strip())
        if pip.returncode: raise RuntimeError('core pip check failed')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): raise RuntimeError('media tools missing')
        from app.services.compose import check_font_available
        check_font_available(); checks['media_dependencies'] = 'passed'
        from app.main import app
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            response = client.get('/healthz')
            if response.status_code != 200: raise RuntimeError('API health failed')
        checks['api_health'] = 'passed'
        if args.tests:
            expression = '' if args.suite == 'all' else args.suite
            run = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-m', expression,
                                  str(ROOT/'tests')], cwd=ROOT)
            checks['test_suite'] = args.suite
            checks['tests_exit'] = run.returncode
            if run.returncode: raise RuntimeError('offline acceptance tests failed')
        status, error = 'PASS', None
    except Exception as exc: status, error = 'FAIL', str(exc)
    sources = {str(p.relative_to(ROOT)): file_sha256(p) for folder in ('app','bin','tests','scripts')
               for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.yaml','.ps1') and '__pycache__' not in str(p)}
    for relative in ('pyproject.toml', '.ai-project/contract.yaml'):
        sources[relative] = file_sha256(ROOT/relative)
    result = dict(status=status, error=error, source_fingerprint=fingerprint(sources), checks=checks,
                  scope='core_runtime_and_offline_code', production_ready=False,
                  remaining_acceptance='real current candidate, content disposition and human review')
    atomic_json(ROOT/args.output, result)
    print(json.dumps({k:v for k,v in result.items() if k!='checks'}, ensure_ascii=False))
    return 0 if status=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
