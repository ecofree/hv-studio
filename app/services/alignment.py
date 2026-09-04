"""
强制对齐服务（M1 实装：linear_estimate）。

将 wav 总时长按"字符权重"分配到每个字符：
  - 中文/英文字母：1.0
  - 数字：0.5（通常读得快）
  - 标点：0（边界，与前一字符同时结束；或记为短停顿 0.3）

⚠️ 真实情况（人声）：语速不均。linear_estimate 是"凑合用"方案。
   视频的镜头边界出现轻微"抢字/拖字"是预期的。
   M2 升级：faster-whisper word-level → 按词在原文中占比拆字
   M4 升级：FunASR/PaddleSpeech 真字符级
"""
from __future__ import annotations

import wave
from pathlib import Path

from app.models import CharToken, Frame

# 字符 → 权重（0 = 标点并入前字；>0 = 独立时长单位）
PUNCT = set("，。！？、；：,.!?;:")
NUM = set("0123456789")


def _char_weight(ch: str) -> float:
    if ch.isspace():
        return 0
    if ch in PUNCT:
        return 0  # 标点不占独立时长，附在前字末
    if ch in NUM:
        return 0.5
    return 1.0


def _wav_duration_ms(wav_path: str | Path) -> int:
    with wave.open(str(wav_path), "rb") as w:
        nframes = w.getnframes()
        rate = w.getframerate()
        return int(nframes / rate * 1000)


def linear_estimate(frames: list[Frame]) -> list[CharToken]:
    """
    为每个 frame 生成 char-level 时间戳。
    返回 list[CharToken]；同时写回 frame.char_align。
    """
    for f in frames:
        if not f.audio_path or not Path(f.audio_path).exists():
            continue
        text = f.narration_text or f.text
        if not text:
            continue
        dur_ms = _wav_duration_ms(f.audio_path)
        # 计算总权重
        weights = [_char_weight(c) for c in text]
        total_w = sum(weights) or 1.0
        per_unit = dur_ms / total_w

        tokens: list[CharToken] = []
        t = 0
        for ch, w in zip(text, weights):
            if w == 0 and ch in PUNCT:
                # 标点：把停顿加到前一 token
                if tokens:
                    tokens[-1].end_ms = min(dur_ms, t + int(per_unit * 0.3))
                continue
            dur = int(per_unit * w)
            tokens.append(CharToken(char=ch, start_ms=t, end_ms=t + dur, conf=1.0))
            t += dur
        # 强制最后 token end = 总时长
        if tokens:
            tokens[-1].end_ms = dur_ms
        f.char_align = tokens
    return [t for f in frames for t in f.char_align]


def forced_align(frames: list[Frame]) -> None:
    """M1 主入口（与 linear_estimate 等价；M2 接 faster-whisper）。"""
    linear_estimate(frames)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", required=True)
    args = ap.parse_args()

    from app.managers import load, save

    ep = load(args.episode)
    if not ep.frames:
        print(f"[!] {args.episode} 没有 frame")
        return
    linear_estimate(ep.frames)
    save(ep)
    for f in ep.frames[:3]:
        print(f"\n=== {f.id}  ({f.audio_duration_ms}ms) ===")
        for tk in f.char_align[:15]:
            print(f"  {tk.char!r:6s} {tk.start_ms:5d}-{tk.end_ms:5d}ms")


if __name__ == "__main__":
    main()
