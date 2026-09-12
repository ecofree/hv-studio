"""Run the local durable production queue; does not enqueue or authorize work."""
import argparse
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--queue-dir')
    parser.add_argument('--execute')
    parser.add_argument('--episode')
    parser.add_argument('--token')
    parser.add_argument('--owner-pid', type=int)
    parser.add_argument('--owner-created', type=float)
    parser.add_argument('--supervisor-pid', type=int)
    parser.add_argument('--supervisor-created', type=float)
    args = parser.parse_args()
    from app.services.batch_worker import run_once, execute, queue_dir
    if args.execute: return execute(args.execute,args.episode,args.token,args.owner_pid,args.owner_created)
    folder=Path(args.queue_dir or queue_dir())
    if args.supervisor_pid:
        import os,threading,psutil
        from app.services.batch_worker import alive
        def watch_supervisor():
            while alive(args.supervisor_pid,args.supervisor_created): time.sleep(.5)
            for child in psutil.Process().children(recursive=True):
                try: child.kill()
                except psutil.NoSuchProcess: pass
            os._exit(75)
        threading.Thread(target=watch_supervisor,daemon=True).start()
    from app.services.process_lock import file_lock
    with file_lock(folder/'service.lock'):
        while not (folder/'STOP').exists():
            run_once(folder)
            if args.once: break
            time.sleep(2)
    return 0


if __name__=='__main__': raise SystemExit(main())
