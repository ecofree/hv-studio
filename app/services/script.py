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
    """句长超过 max_chars 时，切分成长度可控的片段。逗号保留在左半段。

    目标：任一 frame 的旁白都不超过一个不触发 >8s 单镜的长度。中文 TTS
    ~4 字/s，8s ≈ 32 字；max_chars=18 对应 ~4.5s，天然安全。

    约束（两难权衡，勿回归）：
      - 左片段目标 ≤ max_chars；切不出时允许放宽到 HARD_CAP，保证
        绝不超过 ~26 字（≈6.5s，仍远低于 8s 上限），随后对左片段递归。
        （旧算法用 `cut < len/2`，对 119 字一口气带 、；的长句会切出
        33~50 字大块，单镜配音超 8s，compose 时序 gate 直接失败。）
      - 两侧都 ≥ MIN_FRAG_CHARS：不制造「归途上，」这类 <1.6s 碎片镜。
      - 只在有逗号处切，绝不中切 CJK；找不到任何可行逗号时才整句保留
        （此时即使略超 HARD_CAP，也不制造孤立标点/碎字片段）。
    """
    HARD_CAP = 26  # 放宽上限；>26 字 ≈6.5s，仍 <8s 时序硬限

    def _valid(left: str, right: str) -> bool:
        return len(left) >= MIN_FRAG_CHARS and len(right) >= MIN_FRAG_CHARS

    def _parts(left: str, right: str) -> list[str]:
        out = []
        if len(left) > max_chars:
            out += split_long_sentence(left, max_chars)
        else:
            out.append(left)
        out += split_long_sentence(right, max_chars)
        return out

    if len(sent) <= max_chars:
        return [sent]
    comma_pos = [i for i, c in enumerate(sent) if c in "，,、;；"]
    if not comma_pos:
        # 无逗号：宁可整句保留（常短于 HARD_CAP），不中切 CJK 造碎片
        return [sent] if len(sent) <= HARD_CAP else [sent[:HARD_CAP]] + split_long_sentence(sent[HARD_CAP:], max_chars)
    # 1) 优先：窗口 [MIN-1, max_chars-1] 内最右逗号（左片段 ≈ max_chars 且合规）
    for cut in reversed([p for p in comma_pos if MIN_FRAG_CHARS - 1 <= p < max_chars]):
        left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
        if _valid(left, right):
            return [left] + split_long_sentence(right, max_chars)
    # 2) 次选：放宽到 HARD_CAP，仍优先最右合规切点；左超 max_chars 则递归
    for cut in reversed([p for p in comma_pos if p < HARD_CAP]):
        left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
        if _valid(left, right):
            return _parts(left, right)
    # 3) 再放宽：任何能让两侧都 ≥ MIN 的逗号（处理句首连续短状语等极端）
    for cut in reversed(comma_pos):
        left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
        if _valid(left, right):
            return _parts(left, right)
    # 4) 兜底：整句保留（不制造孤立标点碎片）
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
