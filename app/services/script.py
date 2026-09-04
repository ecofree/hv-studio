"""
脚本切分服务（M1 实装）。

输入：episodes/{id}/script.md（纯文本，每段一个 Frame）
输出：ep.frames 列表

切分规则：
  1. 段落：双换行分段
  2. 句：句末标点（。！？.!?）+ 空白断句
  3. 长度限制：单句音频 > 6s 时在最近逗号处再切
  4. 数字转中文：100 → 一百（可选，TTS 兼容性更好）
  5. polyphone.preprocess()：替换多音字

注：暂不调 TTS 估算时长，按"3 字/秒"粗估；M2 接真实测速。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config.config import get_settings
from app.models import Episode, Frame

# 句末标点（中英）
# 陷阱：中文引号/括号常紧跟句末标点，如  ……汉肯听我乎？"
#   若只按标点切，切点落在 ？ 之后，" 之前 → 引号粘到下一句开头，
#   还会吃掉换行，形成 '"\n张骞被扣留了。' 这种脏 frame（字幕会烧进乱码）。
# Python re 的 lookbehind 必须定长，所以用两个定长分支（2 字符 / 1 字符）。
SENT_END = re.compile(r'(?:(?<=[。！？.!?]["\'」』）)》])|(?<=[。！？.!?]))\s*')
# 中文/英文逗号
COMMA = re.compile(r"(?<=[,，、;；])\s*")


def split_script_text(text: str) -> list[str]:
    """
    文本 → 句子列表。先按段落分，段内按句末标点断句，
    长句再按逗号切。
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        # 段落内先按句末标点断
        for sent in re.split(SENT_END, para):
            sent = sent.strip()
            if not sent:
                continue
            out.append(sent)
    return out


# 长句二次切分的最短片段保护：中文 TTS 实测约 3.9 字/s。
# 若切出的任一侧 < MIN_FRAG_CHARS，其预估时长很可能 < 1.6s(约6.3字)，
# 会违背「镜头不短于1.6s防闪切」的时序契约（§5.4，见 2026-09-03 审计 #30）。
# 此时宁可保留整句让 TTS 一气呵成（≤ MAX_CHARS+余量，实测仍在上限内），
# 也不要制造 4~5 字的碎片镜。典型触发例：
#   22字句「归途上，他又一次被匈奴俘获，扣留了一年有余。」
#   旧算法在 2 处逗号里挑最接近中点的，把「归途上，」4字前缀切成独立镜(1.2s)。
MIN_FRAG_CHARS = 6   # 对应约 1.5s+，留足安全边际


def split_long_sentence(sent: str, max_chars: int = 18) -> list[str]:
    """句长超过 max_chars 时，在最近逗号处切。逗号保留在左半段。

    改进（2026-09-03）：切分保证左右两段都不短于 MIN_FRAG_CHARS，
    否则放弃本次切分（保留整句）。杜绝「归途上，」这类 <1.6s 的碎片镜。
    """
    if len(sent) <= max_chars:
        return [sent]
    # 找所有逗号位置（字符索引，非 regex match.start）
    comma_pos = [i for i, c in enumerate(sent) if c in "，,、;；"]
    if not comma_pos:
        return [sent]
    # 离中点最近、且 < len/2 的逗号（优先让左半为主段）
    half = len(sent) // 2
    left_candidates = [p for p in comma_pos if p < half]
    # 逐候选从最右到最左试：取第一个能让两侧都 ≥ MIN_FRAG_CHARS 的切点
    for cut in sorted(left_candidates, reverse=True):
        left = sent[: cut + 1].strip()
        right = sent[cut + 1 :].strip()
        if len(left) >= MIN_FRAG_CHARS and len(right) >= MIN_FRAG_CHARS:
            return [left] + split_long_sentence(right, max_chars)
    # 所有逗号都切不出两侧足够长的块（如句首连续多个短状语）→ 不切，整句保留
    return [sent]


def split_script(ep: Episode, script_md: str) -> list[Frame]:
    """
    主入口。生成 ep.frames 列表。

    关键约定（曾出过 bug，勿改）：
      切分必须在【原文】上做，polyphone 替换只作用于 narration_text。
      否则「匈奴单于」会被替换成「匈奴缠于」并直接烧进字幕。

      - frame.text            = 原文片段  → 字幕显示 / 人工校对
      - frame.narration_text  = 替换后片段 → 送 TTS（多音字读音修正）
    """
    s = get_settings()
    profile = s.get_profile()
    settings = profile.rhythm

    from app.services.polyphone import preprocess
    raw_sents = split_script_text(script_md)
    frames: list[Frame] = []
    for i, sent in enumerate(raw_sents):
        # 长句切分（在原文上切）
        for j, piece in enumerate(split_long_sentence(sent, max_chars=18)):
            frame = Frame(
                id=f"frame-{i:03d}-{j:02d}" if j > 0 else f"frame-{i:03d}",
                text=piece,                    # 原文 → 字幕
                narration_text=preprocess(piece),  # 替换后 → TTS
            )
            frames.append(frame)
    return frames


def rebuild_frames(ep: Episode, script_md: str) -> list[Frame]:
    """
    按 script.md 重新切分，但保留已有资产（按 frame.id 对齐）：
      audio_path / audio_duration_ms / char_align / subtitle / shot_ids

    用途：调完切分规则或多音字词典后，只重切文本，不重跑 TTS。
    """
    old = {f.id: f for f in ep.frames}
    new_frames = split_script(ep, script_md)
    for f in new_frames:
        o = old.get(f.id)
        if not o:
            continue
        f.audio_path = o.audio_path
        f.audio_duration_ms = o.audio_duration_ms
        f.char_align = o.char_align
        f.subtitle = o.subtitle
        f.shot_ids = o.shot_ids
    ep.frames = new_frames
    return new_frames


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("script_md_path")
    ap.add_argument("--episode", required=True, help="episode_id")
    args = ap.parse_args()

    from app.managers import load, save

    text = Path(args.script_md_path).read_text(encoding="utf-8")
    ep = load(args.episode)
    ep.frames = split_script(ep, text)
    save(ep)
    print(f"生成了 {len(ep.frames)} 个 frame")
    for f in ep.frames[:5]:
        print(f"  {f.id}  {f.narration_text}")


if __name__ == "__main__":
    main()
