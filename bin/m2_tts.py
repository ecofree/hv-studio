#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全片配音（CosyVoice A 基准：pitch_rate=1.0，无 instruction）。

幂等（P0-1 修复）：默认只对「文本未变」的已合成段复用 wav；旁白文本变了则
自动删旧 wav 重合成（指纹记在 audio/_tts_manifest.json）。首次对旧目录建议
加 --force 建立指纹基线。
用法：
    python bin/m2_tts.py [--episode hw-002] [--force]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path("D:/hv")
sys.path.insert(0, str(ROOT))

from app.managers import save  # noqa: E402
from app.managers.episode import load  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", default="hw-002")
    ap.add_argument("--force", action="store_true",
                    help="强制全部重合成，并重建 _tts_manifest.json 指纹基线")
    args = ap.parse_args()

    ep = load(args.episode)
    out_dir = ROOT / "episodes" / args.episode / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"frames {len(ep.frames)}，engine=cosyvoice"
          + ("，--force" if args.force else ""))

    from app.services.voice import synthesize
    t0 = time.time()
    synthesize(ep.frames, engine="cosyvoice", out_dir=out_dir, force=args.force)
    save(ep)

    ok = sum(1 for f in ep.frames if f.audio_path and Path(f.audio_path).exists())
    total = sum((f.audio_duration_ms or 0) for f in ep.frames)
    miss = [f.id for f in ep.frames
            if not (f.audio_path and Path(f.audio_path).exists())]
    print(f"完成 {ok}/{len(ep.frames)}，音频总时长 {total/1000:.1f}s "
          f"({total/60000:.2f} 分)，用时 {(time.time()-t0)/60:.1f} 分钟")
    if miss:
        print(f"缺音频：{miss[:20]}")


if __name__ == "__main__":
    main()
