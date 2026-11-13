"""Gain-only narration repair; the production module is the sole implementation.

Creates a new WAV and a hash-bound .level.json receipt. Never overwrites raw
audio or changes tempo. Split boundaries can be supplied as integer samples.
"""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.services.narration_leveling import repair, measure


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('src');ap.add_argument('dst')
    ap.add_argument('--boundaries',help='JSON with source_sha256 and boundaries in samples')
    ap.add_argument('--lufs',type=float,default=-23.)
    ap.add_argument('--tp',type=float,default=-2.)
    ap.add_argument('--tempo',type=float,default=1.)
    ap.add_argument('--no-loudnorm',action='store_true',help='Keep measured input integrated loudness')
    a=ap.parse_args()
    if a.tempo!=1:ap.error('Repair preserves subtitle timing. Create a separate speed version before repair.')
    boundaries=[]
    if a.boundaries:
        from app.services.provenance import file_sha256
        b=json.loads(Path(a.boundaries).read_text(encoding='utf-8'))
        if b['source_sha256']!=file_sha256(a.src):ap.error('Boundary file belongs to another recording')
        boundaries=b['boundaries']
    r=repair(a.src,a.dst,boundaries=boundaries,
             target_lufs=measure(a.src)['lufs'] if a.no_loudnorm else a.lufs,peak_db=a.tp)
    print(json.dumps({k:r[k] for k in ['output_path','before','after','samples','sample_rate','loudness_target_reached']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
