"""Create/verify an explicit production snapshot or restore it for inspection."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.production_backup import create_snapshot,verify_snapshot,restore_snapshot

def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='action',required=True)
    create=sub.add_parser('create');create.add_argument('--episode',action='append',required=True)
    create.add_argument('--destination',required=True);create.add_argument('--dry-run',action='store_true')
    check=sub.add_parser('verify');check.add_argument('--snapshot',required=True)
    restore=sub.add_parser('restore');restore.add_argument('--snapshot',required=True);restore.add_argument('--destination',required=True)
    args=parser.parse_args()
    if args.action=='create':result=create_snapshot(args.episode,args.destination,dry_run=args.dry_run)
    elif args.action=='restore':result=restore_snapshot(args.snapshot,args.destination)
    else:result={'verified_files':len(verify_snapshot(args.snapshot)['files'])}
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
