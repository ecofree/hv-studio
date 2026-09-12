"""Local process supervisor with bounded restart and a durable operator stop."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--max-restarts',type=int,default=5)
    parser.add_argument('--state-dir',default='state/service')
    args=parser.parse_args()
    from app.services.process_lock import file_lock
    from app.services.provenance import atomic_json
    folder=ROOT/args.state_dir; folder.mkdir(parents=True,exist_ok=True)
    stop=folder/'stop'; record=folder/'service.json'
    if stop.exists(): raise RuntimeError('operator stop is present; remove it explicitly to restart')
    with file_lock(folder/'supervisor.lock'):
        restarts=0
        while not stop.exists():
            with (folder/'api.log').open('ab') as log:
                child=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1',
                                        '--port',str(args.port)],cwd=ROOT,stdout=log,stderr=log,
                                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                value=dict(supervisor_pid=os.getpid(),child_pid=child.pid,restarts=restarts,status='running',port=args.port,started_at=time.time())
                atomic_json(record,value)
                try:
                    while child.poll() is None and not stop.exists(): time.sleep(.25)
                finally:
                    if child.poll() is None:
                        import psutil
                        # Windows venv launchers can have a second Python child.
                        # Stop the owned tree, never unrelated listeners/processes.
                        try:
                            owned=psutil.Process(child.pid).children(recursive=True)
                            for process in reversed(owned):
                                try:process.terminate()
                                except psutil.NoSuchProcess:pass
                        except psutil.NoSuchProcess:owned=[]
                        child.terminate()
                        try:child.wait(timeout=10)
                        except subprocess.TimeoutExpired:child.kill();child.wait()
                value.update(exit_code=child.returncode,status='stopped' if stop.exists() else 'exited')
                atomic_json(record,value)
            if stop.exists(): break
            if restarts>=args.max_restarts:
                value['status']='restart_limit_reached';atomic_json(record,value);return 1
            restarts+=1
            until=time.monotonic()+min(2**restarts,30)
            while time.monotonic()<until and not stop.exists():time.sleep(.25)
    return 0


if __name__=='__main__':raise SystemExit(main())
