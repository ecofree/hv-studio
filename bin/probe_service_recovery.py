"""Exercise the real local API supervisor, including an owned child crash."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from app.services.provenance import atomic_json
    folder=ROOT/'state/remediation-20260912/service'/uuid.uuid4().hex;folder.mkdir(parents=True)
    # Reserve a free local test port, then hand it to the child supervisor.
    import socket
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    log=(folder/'supervisor.log').open('wb')
    supervisor=subprocess.Popen([sys.executable,str(ROOT/'bin/serve.py'),'--port',str(port),
                                  '--max-restarts','2','--state-dir',str(folder)],cwd=ROOT,stdout=log,stderr=log)
    def health():
        run=subprocess.run(['curl.exe','--silent','--max-time','2',f'http://127.0.0.1:{port}/healthz'],capture_output=True,text=True)
        return run.returncode==0 and json.loads(run.stdout or '{}').get('ok') is True
    def wait_ready(previous=None):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            record=folder/'service.json'
            if record.exists():
                value=json.loads(record.read_text())
                if value['child_pid']!=previous and health():return value
            time.sleep(.3)
        raise RuntimeError('API failed to become ready; see '+str(folder))
    try:
        first=wait_ready()
        owned=psutil.Process(first['child_pid']).children(recursive=True)
        for process in reversed(owned):
            try:process.kill()
            except psutil.NoSuchProcess:pass
        try:psutil.Process(first['child_pid']).kill()
        except psutil.NoSuchProcess:pass
        second=wait_ready(first['child_pid'])
        assert second['restarts']==1
        atomic_json(folder/'result.json',dict(status='passed',before=first,after=second,health_after_restart=True,provider_calls=0))
        print(json.dumps(dict(status='passed',port=port,restarts=second['restarts'],evidence=str(folder/'result.json'))))
    finally:
        (folder/'stop').write_text('operator stop after isolated verification')
        try:supervisor.wait(timeout=20)
        except subprocess.TimeoutExpired:
            for process in psutil.Process(supervisor.pid).children(recursive=True):process.kill()
            supervisor.kill();supervisor.wait()
        log.close()


if __name__=='__main__':main()
