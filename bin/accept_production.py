"""Prepare, authorize and run repeatable acceptance without automatic approvals."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services import acceptance_runner as R


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    plan=sub.add_parser('plan');plan.add_argument('--episodes',nargs='+',required=True)
    plan.add_argument('--output',required=True);plan.add_argument('--offline',action='store_true')
    plan.add_argument('--max-requests',type=int,default=0);plan.add_argument('--max-characters',type=int,default=0)
    plan.add_argument('--max-images',type=int,default=0);plan.add_argument('--timeout',type=int,default=3600)
    auth=sub.add_parser('authorize');auth.add_argument('--plan',required=True)
    auth.add_argument('--operator',required=True);auth.add_argument('--reason',required=True)
    auth.add_argument('--allow-paid',action='store_true');auth.add_argument('--allow-faults',action='store_true')
    run=sub.add_parser('run');run.add_argument('--plan',required=True);run.add_argument('--scenarios',nargs='+');run.add_argument('--resume',action='store_true')
    a=p.parse_args()
    if a.action=='plan':
        value=R.make_plan(a.episodes,dict(max_requests=a.max_requests,max_characters=a.max_characters,max_images=a.max_images),
                          a.output,mode='offline' if a.offline else 'real',timeout=a.timeout)
    elif a.action=='authorize':
        value=dict(authorization=str(R.authorize(a.plan,a.operator,a.reason,allow_paid=a.allow_paid,allow_faults=a.allow_faults)))
    else:value=R.run(a.plan,scenarios=a.scenarios,resume=a.resume)
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 1 if any(v.get('status')!='PASS' for v in value.get('scenarios',{}).values()) else 0


if __name__=='__main__':raise SystemExit(main())
