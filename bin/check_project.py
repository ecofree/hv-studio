"""Executable acceptance gates. No providers, media deletion or fake approvals."""
import argparse
import importlib.metadata
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_api_health():
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        response = client.get('/healthz')
        if response.status_code != 200:
            raise RuntimeError('API health failed')


def run_bounded(command, timeout):
    from app.services.model_resources import stop_owned
    with subprocess.Popen(command, cwd=ROOT) as child:
        try:
            return child.wait(timeout=timeout)
        except BaseException:
            stop_owned(child)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tests', action='store_true')
    parser.add_argument('--no-media', action='store_true', help='skip tests requiring actual media encoding')
    parser.add_argument('--suite', choices=['production','shared','legacy','all'], default='production')
    parser.add_argument('--output', default='state/acceptance/latest.json')
    parser.add_argument('--require-ready', action='store_true')
    parser.add_argument('--allow-dirty', action='store_true')
    parser.add_argument('--api-health-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.api_health_only:
        check_api_health()
        return 0
    from app.services.provenance import atomic_json, file_sha256, fingerprint
    from app.services.local_asr import runtime_versions
    checks = {}
    output = ROOT/args.output
    def checkpoint(stage):
        atomic_json(output, dict(status='RUNNING', active_check=stage, checks=checks, production_ready=False,
                                 checked_at=datetime.now(timezone.utc).isoformat()))
    try:
        checkpoint('runtime')
        from bin.check_independence import inspect
        independent = inspect(ROOT)
        checks['independence'] = dict(status=independent['status'],issues=independent['issues'],
                                      checked_files=len(independent['checked_files']))
        if independent['status']!='PASS': raise RuntimeError('retired project dependency detected')
        checks['pinned_core'] = runtime_versions(Path(sys.executable), ROOT/'requirements.core.lock.txt')
        pip = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True, timeout=90)
        checks['pip_check'] = dict(exit_code=pip.returncode, output=pip.stdout.strip())
        if pip.returncode: raise RuntimeError('core pip check failed')
        # Distribution metadata can exist even when an offline wheel omitted its
        # modules. Exercise the actual runtime, including the populated queue UI.
        checks['runtime_imports'] = {}
        for module in ('numpy','PIL.Image','dateutil','pandas','streamlit'):
            importlib.import_module(module)
            checks['runtime_imports'][module] = 'passed'
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): raise RuntimeError('media tools missing')
        from app.services.compose import check_font_available
        check_font_available(); checks['media_dependencies'] = 'passed'
        checkpoint('api_health')
        health_limit = int(os.environ.get('HV_ACCEPTANCE_HEALTH_TIMEOUT', '45'))
        try:
            code = run_bounded([sys.executable, str(Path(__file__).resolve()), '--api-health-only'], health_limit)
            checks['api_health'] = 'passed' if code == 0 else dict(status='failed', exit_code=code)
        except subprocess.TimeoutExpired as exc:
            checks['api_health'] = dict(status='failed', reason=type(exc).__name__, timeout_seconds=health_limit)
        checkpoint('tests' if args.tests else 'readiness')
        if args.tests:
            expression = '' if args.suite == 'all' else args.suite
            if args.no_media: expression = f'({expression}) and not NEEDS_FFMPEG' if expression else 'not NEEDS_FFMPEG'
            if checks['api_health'] != 'passed':
                expression = f'({expression}) and not api_transport' if expression else 'not api_transport'
                checks['unverified_test_group'] = 'api_transport'
            checks['test_suite'] = args.suite
            test_limit = int(os.environ.get('HV_ACCEPTANCE_TEST_TIMEOUT', '1800'))
            try:
                tests_exit = run_bounded([sys.executable, '-m', 'pytest', '-q', '-m', expression,
                                          str(ROOT/'tests')], test_limit)
            except subprocess.TimeoutExpired:
                checks['tests_timeout_seconds'] = test_limit
                raise RuntimeError(f'{args.suite} tests exceeded {test_limit}s')
            checks['tests_exit'] = tests_exit
            if tests_exit: raise RuntimeError('offline acceptance tests failed')
        if checks['api_health'] != 'passed':
            raise RuntimeError('API health check failed or timed out')
        status, error = 'PASS', None
    except Exception as exc: status, error = 'FAIL', str(exc)
    from app.services.production_acceptance import source_fingerprint
    try:
        source_fp = source_fingerprint()
    except (OSError, ValueError) as exc:
        source_fp = None
        status, error = 'FAIL', 'project source fingerprint: '+str(exc)
    revision = subprocess.run(['git','rev-parse','HEAD'], cwd=ROOT, capture_output=True,text=True)
    dirty = subprocess.run(['git','status','--porcelain','--','app','bin','tests','scripts','.ai-project'],
                           cwd=ROOT,capture_output=True,text=True)
    from app.services.production_readiness import assess
    try:
        readiness = assess(source_fp) if source_fp else dict(ready=False, blockers=[error])
    except (ValueError, OSError, KeyError, TypeError) as exc:
        readiness = dict(ready=False, blockers=[str(exc)])
        status, error = 'FAIL', 'readiness evidence: '+str(exc)
    if args.require_ready and (not readiness['ready'] or (dirty.stdout.strip() and not args.allow_dirty)
                               or not args.tests or args.suite != 'production'):
        status, error = 'FAIL', 'unattended readiness requirements are not satisfied'
    from app.config.config import load_env
    result = dict(status=status, error=error, source_fingerprint=source_fp, checks=checks,
                  revision=revision.stdout.strip(), dirty_paths=dirty.stdout.splitlines(),
                  environment_fingerprint=fingerprint(load_env()), checked_at=datetime.now(timezone.utc).isoformat(),
                  requested_ready=args.require_ready, readiness=readiness,
                  scope='core_runtime_and_offline_code', production_ready=False,
                  remaining_acceptance='real current candidate, content disposition and human review')
    result['production_ready'] = bool(status=='PASS' and args.require_ready and readiness['ready'])
    atomic_json(output, result)
    print(json.dumps({k:result[k] for k in ('status','error','source_fingerprint','scope','production_ready')}, ensure_ascii=False))
    return 0 if status=='PASS' else 1


if __name__=='__main__': raise SystemExit(main())
