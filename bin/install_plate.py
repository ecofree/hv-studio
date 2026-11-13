"""Swap hand-made plates into an episode's shots/ directory.

Replacing the file is enough: compose calls upscale(sh.image_path) on every run,
so it picks the new bytes up. What this buys us is scoping -- we can tell
exactly which segments the fingerprint change will invalidate *before*
spending 40 minutes of encoding.

    python bin/install_plate.py caomin-03 D03 assets/sandtables/changan.png
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.managers import load                                    # noqa: E402
from app.services import compose as C                            # noqa: E402
from app.services.provenance import file_sha256                  # noqa: E402


def sha(p: Path) -> str:
    return file_sha256(p)[:16]


def main() -> int:
    eid, key, src_rel = sys.argv[1], sys.argv[2], sys.argv[3]
    src = ROOT / src_rel
    if not src.is_file():
        print(f'源图不存在: {src}')
        return 2
    dest = ROOT / 'episodes' / eid / 'shots' / f'{key}.png'
    if not dest.is_file():
        print(f'{eid} 没有 {key}.png，无法替换（该集可能还没生图）')
        return 2
    from PIL import Image
    with Image.open(src) as im:
        w, h = im.size
        if min(w, h) < 256:
            print(f'源图太小 {w}x{h}')
            return 2

    bak = ROOT / 'assets' / 'sandtables' / '_replaced'
    bak.mkdir(parents=True, exist_ok=True)
    keep = bak / f'{eid}_{key}.png'
    if not keep.is_file():
        shutil.copy2(dest, keep)

    before = sha(dest)
    shutil.copy2(src, dest)
    after = sha(dest)
    print(f'{eid} {key}: sha {before} -> {after}  ({w}x{h})')
    print(f'  原图备份: {keep.relative_to(ROOT)}')

    # which segments will re-encode
    ep = load(eid)
    shots = sorted(ep.shots.values(), key=lambda s: s.index)
    tl, total = C.build_timeline(shots, {f.id: f for f in ep.frames})
    touched = [sh for sh, _, _ in tl if Path(sh.image_path).resolve() == dest.resolve()]
    times = [s0 / 1000 for sh, s0, s1 in tl if sh in touched]
    print(f'  受影响镜头 {len(touched)} 个，时间 {min(times):.1f}s ~ {max(times):.1f}s')

    # Segment boundaries MUST come from the same config compose reads. Hardcoding
    # a value here reported seg-000 when the real layout put the change in
    # seg-002/003 -- a wrong answer to "what will re-encode" is worse than not
    # answering, because it sends you looking in the wrong place.
    from app.config.config import get_settings
    cfg = get_settings().compose
    # segment_min_frames is a threshold on TOTAL FRAMES, not on shot count.
    # Comparing len(tl)=295 against 2500 sent every episode down the
    # single-segment branch, so the prediction named seg-000 for all of them.
    n_frames = C.expected_frames(tl)
    if n_frames < cfg.segment_min_frames:
        segs, mode = [tl], 'single'
    else:
        segs, mode = C._segments_of(tl, cfg.segment_max_sec), 'segmented'
    print(f'  分段: 总帧={n_frames} mode={mode} 段数={len(segs)} 段长上限={cfg.segment_max_sec}s')
    for i, sl in enumerate(segs):
        lo = min(s0 for _sh, s0, _s1 in sl) / 1000
        hi = max(s1 for _sh, _s0, s1 in sl) / 1000
        if times and not (hi < min(times) or lo > max(times)):
            print(f'    -> 段 seg-{i:03d} 将重编（{lo:.1f}s ~ {hi:.1f}s）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())