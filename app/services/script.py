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
# ⚠️ 闭合引号必须**全角半角都列**：此前只列了半角 " 和 '，中文成稿用的
# 是全角 ”（U+201D），于是「…不侵暴百姓。”他不仅」会被切在 。 之后、
# ” 之前，把右引号甩到下一帧开头，形成只有 1 个字的垃圾帧（2026-09-06
# 《李世民四部曲》实测 4 处，lsm-15min 的 script.md 有 11 处同类）。
# Python re 的 lookbehind 必须定长，所以用两个定长分支（2 字符 / 1 字符）。
#   - 分支1：句号 + 闭合符 → 切在**闭合符之后**（把 ” 留在本句尾）
#   - 分支2：句号后**不是**闭合符 → 切在句号之后
#   ⚠️ 分支2 必须带 (?!…) 负向预查，否则 `。”他` 会在「。后」和「”后」各切一刀，
#   切出 ['…百姓。', '”', '他不仅'] —— 反而制造出 1 字垃圾帧（2026-09-06 实测：
#   只加了 ” 却没加预查，新剧本的 ” 帧从 1 个涨到 4 个）。两分支必须互斥。
SENT_END = re.compile(
    r"(?:(?<=[。！？.!?][\"'」』）)》”’])"
    r"|(?<=[。！？.!?])(?![\"'」』）)》”’]))\s*")
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

# 无逗号长句的「次级切点」：破折号 / 省略号都是口播里的真停顿，切在它后面
# 语义完整，远好过按字数硬砍中分词语。仅在 comma_pos 为空时启用（不回归
# 既有分逗号逻辑，交付集实测无变化）。
SOFT_BREAK_CHARS = "—…"

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
      - 优先在逗号处切；无逗号时改用 —— / …；都没有才按字数硬砍，且
        硬砍不得留下 <MIN 的孤儿片段（绝不中切出「序正义的。」）。
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
    soft_pos = [i for i, c in enumerate(sent) if c in SOFT_BREAK_CHARS]
    if not comma_pos:
        # 无逗号：先试「—— / …」这类天然停顿，别急着硬砍。
        #   反例（lsm 第一集）：「…绝境时——他们是不会跟你讲程序正义的。」31 字
        #   全句无逗号 → 旧兜底按 HARD_CAP=26 硬砍，把「程序正义」劈成
        #   「…跟你讲程」+「序正义的。」（5 字孤儿镜）。破折号才是正确切点。
        for cut in reversed(soft_pos):
            left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
            if _valid(left, right):
                return _parts(left, right)
        if len(sent) <= HARD_CAP:
            return [sent]
        return _balanced_split(sent)
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
    # 3.6) ★ 必须切：整句已超字数硬顶（≈ MAX_FRAMES 帧）。
    #      ★ 两个下限打架时，「不长于上限」优先（2026-09-05）：
    #        下限违规只是「可能」不过门禁（TTS 实际时长常因标点停顿更长，
    #        且 merge_short 还能把碎片并回去）；上限违规是「必然」不过
    #        —— 32 字 ÷ 3.9 ≈ 8.2s ≈ 205 帧 > MAX_FRAMES=200。
    #
    #      旧的 3.5（「任何 p<MAX_FRAG_CHARS 的逗号，只要两侧非空就切」）
    #      已被本分支取代：它为了压住上限，会接受 5 字的「陇西平原，」孤儿，
    #      把问题从上限搬到下限。两侧都够 MIN 才算切开，切不出就中分。
    #
    #      反例 A（新集 016）：「那是整个隋唐军事史上最惊险的一幕——三千五百名
    #        身披玄黑铁甲的幽州突骑，像一颗钢钉，」41 字。逗号只在 34/40 位，
    #        path3 因右侧 6 字 <MIN 拒绝，3.5 因无 p<MAX_FRAG_CHARS 的逗号
    #        放弃 → 旧逻辑整句返回 41 字 ≈10.5s，一镜撞穿 8s 硬限。
    #        破折号切点给出 18 + 23，语义完整且都在上限内。
    if len(sent) > MAX_FRAG_CHARS:
        for cut in reversed([p for p in comma_pos if p < MAX_FRAG_CHARS]):
            left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
            if left and right:
                return _parts(left, right)
        # 逗号用不上：先试破折号 / 省略号这类软停顿（语义完整，远好过中切）
        for cut in reversed(soft_pos):
            left, right = sent[: cut + 1].strip(), sent[cut + 1 :].strip()
            if _valid(left, right):
                return _parts(left, right)
        # 都没有：中分（可能中切一个词，但两侧均衡且都不撞门禁）
        return _balanced_split(sent)
    # 4) 兜底：整句保留（不制造孤立标点碎片）
    return [sent]


# 引号 / 书名号内部视为不可切断区：把「“关陇军政风投股份｜有限公司”」这类
# 切口切出来比多一个短镜难看得多，宁可把切点挪一两个字。
_PROTECTED_PAIRS = (("“", "”"), ("\"", "\""), ("《", "》"), ("〈", "〉"))


def _protected_spans(sent: str) -> set[int]:
    """引号/书名号**内部**的下标集合（不含定界符本身）。"""
    spans: set[int] = set()
    for open_ch, close_ch in _PROTECTED_PAIRS:
        start = None
        for i, c in enumerate(sent):
            if c == open_ch and start is None:
                start = i
            elif c == close_ch and start is not None:
                # 含闭引号本身：切点 c 表示左段止于 c-1，若 c ≤ 闭引号位置，
                # 闭引号就会被甩到右段 → 仍然算切在引号里（实测切出
                # 「…风投股份有限公司」+「”的一场超级并购案」）。
                spans.update(range(start + 1, i + 1))
                start = None
    return spans


# 无标点长句「中分」时，切口右侧**不宜开头**的字。
# 这些字绝大多数情况下是双音节词的后一音节（打到 / 功臣 / 长安 / 大军 …），
# 切在它们前面会把一个词劈成两帧 —— 实测踩过两处（2026-09-08）：
#   帧54  「…西秦铁骑甚至打」+「到了离长安只有几十里的浅水原。」
#   帧160 「…天策府内部几百名百战功」+「臣面临被和平解散的生存绝境。」（"功臣"被切）
# 中分兜底本来就是"两害相权"，这里只是让它在**同样合法**的候选里优先选不破词的位置；
# 找不到就照旧中分，绝不会因为挑位置而放弃切分（否则又撞 8s 硬顶）。
_NO_FRAG_START = set(
    "到了臣民兵马城门年月日天下上中里后前时事心力气地者家儿子儿头面出起开关"
    "军国士人官兵骑将相王侯公卿府州县道路边关山河江海风雨雪霜"
)


def _pick_cut(sent: str, mid: int, lo: int, hi: int, protected: set[int],
              avoid_after=None) -> int | None:
    """从中点向两侧扩散找切点。avoid_after(ch) 为真表示该位置不采用。"""
    for d in range(max(mid - lo, hi - mid) + 1):
        for cand in ((mid - d,) if d == 0 else (mid - d, mid + d)):
            if not (lo <= cand <= hi) or cand in protected:
                continue
            if avoid_after is not None and cand < len(sent) \
                    and avoid_after(sent[cand]):
                continue
            return cand
    return None


def _balanced_split(sent: str) -> list[str]:
    """无可用标点时的中分兜底：两侧都 ≥ MIN_FRAG_CHARS，保证递归收敛。

    两害相权：宁可中切一个词，也不接受
      · 「超硬顶整句保留」——必然撞 8s 门禁；
      · 「一侧只剩 5~7 字孤儿」——字幕断词（旧 HARD_CAP 硬砍的产物，
        如「…跟你讲程」+「序正义的。」）。

    切点从中点向两侧扩散，跳过引号/书名号内部，取第一个合法位置。
    """
    n = len(sent)
    lo, hi = MIN_FRAG_CHARS, n - MIN_FRAG_CHARS
    if lo > hi:
        return [sent]
    protected = _protected_spans(sent)
    mid = n // 2
    cut = _pick_cut(sent, mid, lo, hi, protected,
                    avoid_after=lambda c: c in _NO_FRAG_START)
    if cut is None:                      # 挑不到不破词的位置 → 照旧中分
        cut = _pick_cut(sent, mid, lo, hi, protected)
    if cut is None:
        cut = min(max(mid, lo), hi)
    if cut is None:  # 整句被保护包住：退回纯中分，认了
        cut = min(max(mid, lo), hi)
    left, right = sent[:cut].strip(), sent[cut:].strip()
    if not left or not right:
        return [sent]
    return [left] + split_long_sentence(right)


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


# ---------------------------------------------------------------------------
# markdown 前置剥离（2026-09-06《李世民四部曲》实测新增）
# ---------------------------------------------------------------------------
# 用户拿来的剧本常是「带完整格式标注的成稿」：# 标题、> 引用块（作品类型 /
# 预估时长 / 核心切口 / 格式规范）、**（时长预估：…）**、--- 分隔线，以及行内的
# 【语气/画面标注】与 [停顿0.5秒]。这些是给人看的，不是给 TTS 念、更不是烧字幕的。
#
# 此前 split_script **从不清洗**——clean_narration 只在
# bin/build_episode_from_md.py 的双栏分镜路径上被调用，而 stage_script 这条
# 官方生产链直接把 script.md 原文喂进来切。实测喂一份带标注的 3250 字剧本：
# 294 帧里 33 帧被污染，前 6 帧整段是 markdown 垃圾，会原样烧进字幕并送去配音。
MD_HEADING = re.compile(r"^#{1,6}\s*")                 # # / ### 标题行
MD_QUOTE = re.compile(r"^>")                           # > 引用块
MD_HR = re.compile(r"^([-*_])(?:\s*\1){2,}\s*$")       # --- / *** / ___ 水平线
MD_BOLD_LINE = re.compile(r"^\*\*.*\*\*$")             # 整行只有加粗（时长预估/情绪基调）
MD_EMPHASIS = re.compile(r"\*\*(.+?)\*\*")             # 行内 **强调** → 去掉星号留文本
MD_PARA_SPLIT = re.compile(r"\n\s*\n")


def strip_script_markdown(text: str) -> str:
    """剥掉剧本成稿里的 markdown 结构与导演标注，只留可朗读正文。

    按**段落**处理（`\\n\\s*\\n`），段内逐行清洗后**直接拼接**——markdown 里
    同一段落的软换行就是续行，合并才是对的。若保留 `\\n`，它会跟着帧文本
    一起烧进字幕（实测 73 帧含换行符）。

    ⚠️ 标点的 strip 必须在**段级**做，不能在行级：软换行处的行尾常有
    「地下钱炉，」这种逗号，行级 strip 会把它吃掉，拼接后句子粘连。
    同理，跨行的引号对（“若能从我……” / ”）也只有在合并后才是完整的一句，
    否则 `”` 会被单独切成一帧 1 字帧。

    行内标注交给与双栏分镜路径同一套口径（【】→ 删、[停顿N秒]→。、[] → 删），
    避免两处规则各飘各的。
    """
    kept: list[str] = []
    for para in MD_PARA_SPLIT.split(text):
        lines: list[str] = []
        for line in para.splitlines():
            s = line.strip()
            if not s:
                continue
            if MD_HEADING.match(s) or MD_QUOTE.match(s) or MD_HR.match(s) \
                    or MD_BOLD_LINE.match(s):
                continue                              # 整行是结构/元数据 → 删
            s = MD_EMPHASIS.sub(r"\1", s)             # **强调** → 强调
            s = NARR_ANNOT.sub("", s)                 # 【语气/画面标注】
            s = NARR_PAUSE.sub("。", s)               # [停顿N秒] → 。
            s = NARR_BRACKET.sub("", s)               # [重音] 等
            lines.append(s)
        if not lines:
            continue
        # ⚠️ 这里**绝不能 strip 句末标点**：clean_narration 末尾那句
        # .strip("，。、；：？！") 是给双栏分镜的【旁白单元格】用的（每格一句，
        # 末尾句号交给 TTS 自然停）。而 split_script 切的是整篇，句末句号
        # 既是字幕的一部分，也是下一句切分的依据——剥掉它会让所有既有集的
        # 帧文本整体少一个句号（2026-09-06 实测回归：laogongtuan 45 帧、
        # hanwu-ch3 72 帧被剥掉句尾句号）。只压空白与叠句号。
        joined = re.sub(r"\s+", "", "".join(lines))   # 段内软换行合并
        joined = re.sub(r"。{2,}", "。", joined)
        if joined:
            kept.append(joined)
    return "\n\n".join(kept)


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
            # 孤立标点吸附也受硬顶约束：max_join(29) < MAX_FRAG_CHARS(31)，
            # 但吸附是在「已合格片段」后面追加，不受 max_join 管 → 单独封顶，
            # 否则 30 字片段 + '，' 也能顶到 31 字以上，白瞎了切分器的上限保证。
            if len(merged[-1]) + len(p) > MAX_FRAG_CHARS:
                merged.append(p)
            else:
                merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    # 收尾：末片仍是碎片则并入倒数第二片（若放得下）
    # ⚠️ 必须先 pop 再赋值：`merged[-2] = merged[-2] + merged.pop()` 是先算
    # 右侧（pop 使长度减 1）再对左侧下标赋值，len==2 时 pop 后 merged[-2]
    # 越界 → IndexError。2026-09-06 新剧本 merge_short 建档时实测崩溃。
    if len(merged) > 1 and len(merged[-1]) < min_keep \
            and len(merged[-2]) + len(merged[-1]) <= max_join:
        last = merged.pop()
        merged[-1] = merged[-1] + last
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

    # 前置剥离 markdown 与导演标注（见 strip_script_markdown 的实测事故）。
    # 放在切分**之前**：剥掉标注会改变句子边界（如 [停顿0.5秒]→。），
    # 先清洗再切分才能得到正确的帧数。
    md = strip_script_markdown(script_md)
    frames: list[Frame] = []

    def _mk(i: int, j: int, piece: str) -> Frame:
        return Frame(
            id=f"frame-{i:03d}-{j:02d}" if j > 0 else f"frame-{i:03d}",
            text=piece,                        # 原文 → 字幕
            narration_text=preprocess(piece),  # 替换后 → TTS
        )

    if not merge_short:
        for i, sent in enumerate(split_script_text(md)):
            for j, piece in enumerate(split_long_sentence(sent, max_chars=18)):
                frames.append(_mk(i, j, piece))
        return frames

    for i, para in enumerate(re.split(r"\n\s*\n", md.strip())):
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
