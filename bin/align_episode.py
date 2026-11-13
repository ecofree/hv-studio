#!/usr/bin/env python
"""Per-word timing for an episode -> ``episodes/{id}/word_timing.json``.

Sidecar, not an edit to ``{id}.json``: the episode json is the source of truth
that approval receipts and run records hash, and filling a display concern
(karaoke highlighting) into it would invalidate work that is already signed off.

Times are **milliseconds relative to each frame's own audio**, which is exactly
what the render loop needs: it knows ``s0 - sh.audio_start_ms`` as the frame's
offset on the timeline, so frame-relative ms needs no further bookkeeping.

    python bin/align_episode.py caomin-02 --workers 4
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.managers import load                                    # noqa: E402
from app.config.config import get_settings                       # noqa: E402
from app.services import forced_alignment as FA                  # noqa: E402
from app.services import local_asr                               # noqa: E402
from app.services.provenance import file_sha256, atomic_json     # noqa: E402
from app.services.narration_pipeline import read                  # noqa: E402

VERSION = 1
SR = 16000

# Whisper emits punctuation as standalone tokens with (near) zero duration:
# frame-000-04 comes back as Word(8, 9, 2900.0, 2900.0) where source[8] is a
# comma. A comma is not a word and has nothing to highlight -- worse, keeping it
# breaks offset translation, because the comma is rendered as a *space* on screen
# (compose.subtitle_display_text) so display.find('，') never matches.
# Non-speakable and zero-duration rows are dropped at the source, so the file
# only ever contains real words.
SPEAKABLE_RANGES = (
    (0x4E00, 0x9FFF),      # CJK 统一表意文字
    (0x3400, 0x4DBF),      # 扩展 A
    (0xF900, 0xFAFF),      # 兼容表意文字
)


def _speakable(text: str) -> bool:
    """True if the token contains at least one pronounceable character."""
    for ch in text:
        if ch.isalnum():
            return True
        o = ord(ch)
        for lo, hi in SPEAKABLE_RANGES:
            if lo <= o <= hi:
                return True
    return False


def _wav(path: Path):
    with wave.open(str(path)) as w:
        return w.getframerate(), w.getnframes()


def align_one(frame, ctx):
    """-> (frame_id, rows, audio_sha) or raises. Rows are source char offsets
    plus frame-relative ms."""
    src = (frame.text or '').strip()
    ap = Path(frame.audio_path)
    if not ap.is_file():
        raise FileNotFoundError(f'{frame.id} 音频缺失: {ap}')
    sr, n = _wav(ap)
    if sr != SR:
        raise ValueError(f'{frame.id} 采样率 {sr} != {SR}')
    rec, asr_path = local_asr.recognize(ap, context=ctx)
    res, align_path = FA.align(ap, src, rec, n / sr, ctx)
    rows = FA.atoms(dict(unit_id=frame.id, text=src, sample_start=0,
                         sample_end=n), res, SR)
    kept = [r for r in rows
            if _speakable(src[r['text_start']:r['text_end']])
            and r['sample_end'] > r['sample_start']]
    if not kept:
        raise ValueError(f'{frame.id} 对齐后没有可发音的词')
    return (frame.id,
            [[r['text_start'], r['text_end'],
              round(r['sample_start'] / SR * 1000, 1),
              round(r['sample_end'] / SR * 1000, 1)] for r in kept],
            {str(asr_path): file_sha256(asr_path), str(align_path): file_sha256(align_path)})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('ep_id')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 帧（调试用）')
    args = ap.parse_args()

    ep = load(args.ep_id)
    frames = sorted(ep.frames, key=lambda f: f.id)
    if args.limit:
        frames = frames[:args.limit]
    ctx = local_asr.preflight(get_settings().get_profile().asr)
    print(f'[align] {args.ep_id} {len(frames)} 帧，{args.workers} 线程')

    words, inputs, errors, done = {}, {}, [], 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(align_one, f, ctx): f for f in frames}
        for fut in as_completed(futs):
            f = futs[fut]
            try:
                fid, rows, prov = fut.result()
                words[fid] = rows
                inputs.update(prov)
                done += 1
                if done % 25 == 0:
                    print(f'   {done}/{len(frames)}')
            except Exception as exc:                       # noqa: BLE001
                errors.append({'frame_id': f.id, 'error': f'{type(exc).__name__}: {exc}'})
                print(f'   [align] {f.id} 失败: {exc}')

    if errors:
        print(f'[align] {len(errors)} 帧失败，先不落盘（半份字级数据会让字幕高亮随机缺词）')
        for e in errors[:5]:
            print('   ', e)
        return 1

    record = {
        'version': VERSION, 'ep_id': args.ep_id, 'sample_rate': SR,
        'method': 'local_forced_alignment_v1', 'status': 'machine_derived',
        'listening_approved': False,
        'adapter_sha256': file_sha256(Path(FA.__file__)),
        'inputs': inputs,
        'frames': len(words), 'words_total': sum(len(v) for v in words.values()),
        'words': words,
    }
    out = ROOT / 'episodes' / args.ep_id / 'word_timing.json'
    atomic_json(out, record)
    print(f'[align] 写入 {out}')
    print(f'[align] {len(words)} 帧 / {record["words_total"]} 词，'
          f'平均每帧 {record["words_total"]/max(1,len(words)):.1f} 词')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())