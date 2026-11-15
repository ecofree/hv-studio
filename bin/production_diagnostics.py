"""Export a source-bound review queue and metrics without production calls."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services import production_diagnostics as D


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--episode',required=True)
    p.add_argument('--output');p.add_argument('--baseline');a=p.parse_args()
    value=D.report(a.episode)
    if a.baseline:value['comparison']=D.compare(json.loads(Path(a.baseline).read_text('utf-8')),value)
    if a.output:print(D.write_report(value,a.output))
    else:print(json.dumps(value,ensure_ascii=False,indent=2))
    return 1 if value['errors'] else 0


if __name__=='__main__':raise SystemExit(main())
