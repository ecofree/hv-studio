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


# 长句二次切分的最短片段保护。
#
# ★ 这里的数不是手写的，是从时序门禁反推出来的（2026-09-05 修复）。
#   曾经写死 6：6 字 / 3.9 字每秒 ≈ 1.54s ≈ 38 帧，而门禁 MIN_FRAMES=40 帧
#   （1.6s）—— **切分器能产出自己的门禁会拒绝的碎片镜**，属于自相矛盾。
#   改成同源推导后，门禁改了这里自动跟着变，两个下限不可能再各飘各的。
#
# 中文 TTS 语速实测（2026-09-03 审计 #30）：约 3.9 字/s。
TTS_CHARS_PER_SEC = 3.9


def _derive_min_frag_chars() -> int:
    """由「单镜不得短于 MIN_FRAMES 帧」反推最短片段字数。

    推导：秒数 = 帧数 / FPS；字数 = 秒数 × 语速。向上取整保证不欠。
    当前 = ceil(40 / 25 × 3.9) = ceil(6.24) = 7 字（原手写值 6 偏小）。
    """
    import math

    from app.services.motion import FPS
    from app.services.timing_assert import MIN_FRAMES

    return max(1, math.ceil(MIN_FRAMES / FPS * TTS_CHARS_PER_SEC))


MIN_FRAG_CHARS = _derive_min_frag_chars()


def _derive_max_frag_chars() -> int:
    """由「单镜不得长于 MAX_FRAMES 帧」反推单片段字数上限。

    与 MIN_FRAG_CHARS 同源推导，避免同一套常量里再出现第二个魔数。
    当前 = int(200 / 25 × 3.9) = 31 字（≈7.9s，贴着 8s 硬顶）。
    """
    from app.services.motion import FPS
    from app.services.timing_assert import MAX_FRAMES

    return int(MAX_FRAMES / FPS * TTS_CHARS_PER_SEC)


MAX_FRAG_CHARS = _derive_max_frag_chars()

# 触发例（写死 6 时的实际产物）：
#   22字句「归途上，他又一次被匈奴俘获，扣留了一年有余。」
#   旧算法在 2 处逗号里挑最接近中点的，把「归途上，」4字前缀切成独立镜(1.2s)。


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
    # 3.5) 必须切：整句已超字数硬顶（≈ MAX_FRAMES 帧）。
    #      ★ 两个下限打架时，「不长于上限」优先（2026-09-05）：
    #        下限违规只是「可能」不过门禁（TTS 实际时长常因标点停顿更长，
    #        且 merge_short 还能把碎片并回去）；上限违规是「必然」不过
    #        —— 32 字 ÷ 3.9 ≈ 8.2s ≈ 205 帧 > MAX_FRAMES=200。
    #        实测 MIN_FRAG_CHARS 6→7 后 lsm-15min 就有这么一句被整句保留。
    if len(sent) > MAX_FRAG_CHARS:
        for cut in reversed([p for p in comma_pos if p < MAX_FRAG_CHARS]):
            left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
            if left and right:
                return _parts(left, right)
    # 4) 兜底：整句保留（不制造孤立标点碎片）
    return [sent]


# ---------------------------------------------------------------------------
# 旁白清洗 / 短片合并 / 帧指纹
#
# 2026-09-05 自 lsm-15min 生产用的临时脚本（%TEMP%/opencode/build_lsm15.py）
# 同源化进仓。此前这三段逻辑只活在工作站临时目录里，导致：
#   · 新片必须重抄一遍清洗规则 → 最大可复现性缺口
#   · 临时的 merge_pieces 与仓内 split_script 不同源，lsm-15min 实测
#     两者相差 68 帧（392 vs 324），任何人重跑 `stage script` 都会让
#     scene_plan 的 64 个帧区间整体错位。
# 现在统一由 bin/build_episode_from_md.py 与 split_script 共用。
# ---------------------------------------------------------------------------
NARR_ANNOT = re.compile(r"【[^】]*】")                 # 【语气/导演标注】整段去
NARR_PAUSE = re.compile(r"[［\[]停顿[\d\.]*秒[］\]]")   # [停顿1.5秒] → 。（TTS 自然停）
NARR_BRACKET = re.compile(r"[［\[][^］\]]*[］\]]")     # [重音]/[叹息一声] 等去
PUNCT_ONLY = re.compile(r"[”\"'』）》〉，。、；：？！…—]+")


def clean_narration(raw: str) -> str:
    """把双栏分镜 md 的「旁白」列清洗成可直接送 TTS + 烧字幕的纯文本。

    顺序不可调换：先去【】再处理 []（否则【】内容里的 [] 会被提前吃掉），
    最后压空白/叠句号；末端引导性标点必须剥掉，否则会原样烧进首帧字幕。
    """
    t = raw.replace("<br>", "\n")
    t = NARR_ANNOT.sub("", t)
    t = NARR_PAUSE.sub("。", t)
    t = NARR_BRACKET.sub("", t)
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"。{2,}", "。", t)
    return t.strip("，。、；：？！ \t")


MERGE_MIN_KEEP = 10   # 短于此长度视为「碎片」，尝试向后并（≈2.5s）
MERGE_MAX_JOIN = 29   # 合并后上限（≈7.4s，仍低于 spec.SPLIT_THRESHOLD_MS=6000
                      # 的冗余带。原注释写的 max_shot_ms 来自 config 的
                      # profile.rhythm —— 那一段全仓零消费者，H 批已删，
                      # 真源改在 app/services/spec.py）


def merge_short_pieces(pieces: list[str],
                       min_keep: int = MERGE_MIN_KEEP,
                       max_join: int = MERGE_MAX_JOIN) -> list[str]:
    """把过短片段向后合并，孤立标点一律吸附到前一片段。

    动机（lsm-15min 实测）：切分器允许的 MIN_FRAG_CHARS=6 对应 ≈1.5s，低于
    时序门禁 min_shot_ms=1600 的安全边际，会产生「很多历史小白以为，」这类
    半句闪切镜；合并后单镜普遍 ≥2.5s。

    ⚠️ 这是**逐集可选**行为，绝不是 split_script 的默认路径：
      laogongtuan 的 365 帧是按【未合并】切分并已配好音/分镜的，
      打开合并会变 310 帧 → 既有音频与 scene_plan 全错位。
    新集由 bin/build_episode_from_md.py --merge-short 显式开启，并写入指纹。
    """
    merged: list[str] = []
    for p in pieces:
        if merged and (PUNCT_ONLY.fullmatch(p)
                       or (len(merged[-1]) < min_keep
                           and len(merged[-1]) + len(p) <= max_join)):
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    # 收尾：末片仍是碎片则并入倒数第二片（若放得下）
    if len(merged) > 1 and len(merged[-1]) < min_keep \
            and len(merged[-2]) + len(merged[-1]) <= max_join:
        merged[-2] = merged[-2] + merged.pop()
    return merged


def segment_script(text: str, max_chars: int = 18,
                   merge_short: bool = False) -> list[str]:
    """文本 → 帧文案列表。builder 与 split_script 共用的唯一切分入口。

    merge 的边界是【段落】，不是全文：一段 = 分镜表里的一个镜头，
    跨镜头合并会把两张底图的旁白粘成一帧。
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        pieces: list[str] = []
        for sent in split_script_text(para):
            pieces.extend(split_long_sentence(sent, max_chars=max_chars))
        out.extend(merge_short_pieces(pieces) if merge_short else pieces)
    return out


def frames_fingerprint(texts: list[str], head_tail: int = 24) -> dict:
    """帧序列指纹：用于把 scene_plan 锚定到具体的一次切分结果。

    装配 shots 时比对，不一致即拒——避免出现「分镜按 A 切分写、episode 按
    B 切分存」导致的静默错位（lsm-15min 实测 68 帧漂移，21%）。

    只取 count + sha1 + 首尾预览：全文入库会让 scene_plan 体积翻倍，
    而 count/sha1 已足以发现任何错位，首尾预览用于人工一眼定位。
    """
    import hashlib
    joined = "\n".join(texts)
    return {
        "count": len(texts),
        "sha1": hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12],
        "head": (texts[0][:head_tail] if texts else ""),
        "tail": (texts[-1][-head_tail:] if texts else ""),
    }


def split_script(ep: Episode, script_md: str,
                 merge_short: bool = False) -> list[Frame]:
    """
    主入口。生成 ep.frames 列表。

    关键约定（曾出过 bug，勿改）：
      切分必须在【原文】上做，polyphone 替换只作用于 narration_text。
      否则「匈奴单于」会被替换成「匈奴缠于」并直接烧进字幕。

      - frame.text            = 原文片段  → 字幕显示 / 人工校对
      - frame.narration_text  = 替换后片段 → 送 TTS（多音字读音修正）

    merge_short=True 时按【段落】边界做短片合并（见 merge_short_pieces），
    此时 frame.id 的 i 是「段号」而非「句号」。默认 False：既有集
    （laogongtuan 365 帧等）都是按未合并切分建档的，改默认即错位。
    """
    from app.services.polyphone import preprocess
    frames: list[Frame] = []

    def _mk(i: int, j: int, piece: str) -> Frame:
        return Frame(
            id=f"frame-{i:03d}-{j:02d}" if j > 0 else f"frame-{i:03d}",
            text=piece,                        # 原文 → 字幕
            narration_text=preprocess(piece),  # 替换后 → TTS
        )

    if not merge_short:
        for i, sent in enumerate(split_script_text(script_md)):
            for j, piece in enumerate(split_long_sentence(sent, max_chars=18)):
                frames.append(_mk(i, j, piece))
        return frames

    for i, para in enumerate(re.split(r"\n\s*\n", script_md.strip())):
        if not para.strip():
            continue
        pieces: list[str] = []
        for sent in split_script_text(para):
            pieces.extend(split_long_sentence(sent, max_chars=18))
        for j, piece in enumerate(merge_short_pieces(pieces)):
            frames.append(_mk(i, j, piece))
    return frames


def rebuild_frames(ep: Episode, script_md: str,
                   merge_short: bool = False) -> list[Frame]:
    """
    按 script.md 重新切分，但保留已有资产（按 frame.id 对齐）：
      audio_path / audio_duration_ms / char_align / subtitle / shot_ids

    用途：调完切分规则或多音字词典后，只重切文本，不重跑 TTS。

    ⚠️ merge_short 必须与建档时一致：不一致会让 frame.id 的编号基准从
    「句号」变成「段号」，id 对齐全部落空 → 音频被判为孤儿、时长丢失。

    ⚠️ id 相同 ≠ 内容相同：调过切分规则后（如 MIN_FRAG_CHARS 6→7），
    同一 id 上的文本可能已经变了。此时若照旧继承 audio_path，就会把
    **旧文本的配音挂到新文本上** —— 没有报错、没有缺文件，成片里却是
    音画错位。故凡文本变更的帧一律丢弃旧音频，让 TTS 按文本指纹重合成。
    """
    old = {f.id: f for f in ep.frames}
    new_frames = split_script(ep, script_md, merge_short=merge_short)
    drift: list[str] = []
    for f in new_frames:
        o = old.get(f.id)
        if not o:
            continue
        if o.text != f.text:
            # 文本变了：旧配音属于旧文本，继承它就是静默音画错位
            drift.append(f.id)
            continue
        f.audio_path = o.audio_path
        f.audio_duration_ms = o.audio_duration_ms
        f.char_align = o.char_align
        f.subtitle = o.subtitle
        f.shot_ids = o.shot_ids
    ep.frames = new_frames
    if drift:
        print(f"  [script] 重切后 {len(drift)} 帧文本变更，已丢弃其旧音频"
              f"（需重跑 TTS）: {drift[:10]}"
              + (f" …共 {len(drift)} 帧" if len(drift) > 10 else ""))
    missing = [f.id for f in new_frames if not f.audio_path]
    if missing and ep.frames != [] and any(o.audio_path for o in old.values()):
        print(f"  [script] 其中 {len(missing)} 帧无音频"
              f"（新切分产生或文本变更），重跑 tts stage 补齐")
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
