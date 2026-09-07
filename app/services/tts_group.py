# -*- coding: utf-8 -*-
"""TTS 分组：按【段落】合成长音频，再按静音切回逐帧 wav。

动机（2026-09-06 用户实测反馈「语音太短，情感不好」）
--------------------------------------------------
逐帧合成 = 每帧只给 TTS 13~15 字，句子被切碎后模型没有上下文，韵律和情感
全塌。实测既有集音频时长：

    laogongtuan  中位 3.3s，135/365 帧 < 3s，27 帧 < 2s
    lsm-15min    中位 3.7s
    hanwu-ch3    中位 3.5s

改按段落合成后（lishimin-01 实测 50 段）：
    中位 4 帧 / 65 字 ≈ 16.7s —— 单次合成长度约 5 倍，且**总字数不变，
    花费基本不变**（TTS 按字符计费，只是请求次数少了）。

为什么必须切回逐帧 wav
----------------------
compose 是逐帧读 wav 拼时间线的（还带「磁盘时长 vs json 时长」校验），
帧时长同时决定每镜时长与字幕停留。所以段落合成之后**必须再切回逐帧文件**，
这样 `compose` / `shot_builder` / `timing_assert` 一行都不用改。

切片不是按字数硬砍：先按「字数 + 标点停顿权重」估位置，再在 ±SEARCH_MS
窗口里找能量最低处（真实静音）落刀，最后对齐零交叉点避免爆音。
找不到静音（连续语流）才退回估算位置。
"""
from __future__ import annotations

import re
import wave
from pathlib import Path

import numpy as np

# 单次合成的**目标**字数：把相邻段落往里塞，直到够这个数。
#   2026-09-06：段落中位只有 65 字(≈16.7s)，用户嫌短。实测服务端非流式
#   SpeechSynthesizer.call() 上限 2 万字符（中文按 2 计），本集全篇 3630 字
#   ≈7260 单位 —— 装得下。所以 65 字是我自己设的保守值，不是技术上限。
#   默认 400 字 ≈100s：再往上情感收益递减，而单次失败重做的代价变大。
#   CLI: --tts-group-chars N 可调（0/1 = 退回逐段不打包）。
TARGET_GROUP_CHARS = 400

# 硬顶：任何情况下单次请求不得超过（1000 汉字 = 2000 单位，离 2 万还很远）
MAX_GROUP_CHARS = 1000

# 标点停顿权重（单位≈字）：口播里「。」的停顿远长于「，」
_PUNCT_PAUSE = {
    "，": 1.0, ",": 1.0, "、": 0.8, ";": 1.4, "；": 1.6,
    ":": 1.0, "：": 1.2, "。": 2.4, ".": 2.0, "！": 2.4, "!": 2.4,
    "？": 2.4, "?": 2.4, "…": 2.0, "—": 0.8, "-": 0.4,
}

SEARCH_MS = 350      # 静音搜索窗口（单侧，下限）
ADAPT_MAX_MS = 2000  # 自适应窗口上限：再宽就会跨到别的停顿上
ENERGY_WIN_MS = 20   # 短时能量窗长
_NP = None


def _np():
    """惰性拿 numpy（本模块被 import 时不强制依赖）。"""
    global _NP
    if _NP is None:
        import numpy  # noqa: PLC0415
        _NP = numpy
    return _NP


def piece_weight(text: str) -> float:
    """片段的时长权重：字数 + 标点停顿。"""
    t = text or ""
    return float(len(t) + sum(_PUNCT_PAUSE.get(c, 0.0) for c in t))


# ---------------------------------------------------------------------------
# 分组：frames → 段落
# ---------------------------------------------------------------------------
def _pipeline_paragraphs(script_md: str, merge_short: bool) -> list[list[str]]:
    """复刻 split_script 的两种模式，返回「每段 → 该段的 piece 列表」。

    与 app/services/script.py::split_script 同源；若那边改了切分规则，
    这里必须跟着改，否则匹配不上（会安全退化成逐帧）。
    """
    from app.services import script as S

    text = S.strip_script_markdown(script_md)
    out: list[list[str]] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        pieces: list[str] = []
        for sent in S.split_script_text(para):
            pieces.extend(S.split_long_sentence(sent, max_chars=18))
        out.append(S.merge_short_pieces(pieces) if merge_short else pieces)
    return out


def _match_paragraphs(texts: list[str], script_md: str | None) -> list[list[str]] | None:
    """把 frames 的文案序列与「按段落切分」的结果对齐。

    逐个模式试（default / merge-short），**完全相等**才认——宁可退化成逐帧，
    也不能把帧划进错的段落（那会让配音和画面对不上）。
    返回 None 表示对不上（脚本缺失、或被人工改过）。
    """
    if not script_md:
        return None
    for merge in (False, True):
        try:
            paras = _pipeline_paragraphs(script_md, merge)
        except Exception:  # noqa: BLE001 —— 脚本解析失败不该让 TTS 崩
            return None
        flat = [p for para in paras for p in para]
        if flat == texts:
            return paras
    return None


_SENT_ENDINGS = ("。", "！", "？", "…", "”", "）")
# 预算分组的下限：攒够这么多字才收口，否则宁可继续并（避免又造出短音频）
MIN_BUDGET_CHARS = 40


def _budget_groups(texts: list[str], max_chars: int) -> list[list[int]]:
    """无段落信息时按字数预算分组，**优先在句末收口**。

    不跨句子切断是底线：把「…称帝建国；」和下一句的开头拼在一起还行，
    把一句话从中间劈成两组是绝不允许的。
    """
    out: list[list[int]] = []
    cur: list[int] = []
    acc = 0
    for i, t in enumerate(texts):
        cur.append(i)
        acc += len(t)
        endable = t.rstrip().endswith(_SENT_ENDINGS)
        if acc >= max_chars or (endable and acc >= MIN_BUDGET_CHARS):
            out.append(cur)
            cur, acc = [], 0
    if cur:
        out.append(cur)
    return out


def _split_by_budget(idxs: list[int], texts: list[str],
                     max_chars: int) -> list[list[int]]:
    """超长段落按字数预算再切（不破坏原有顺序）。"""
    out: list[list[int]] = []
    cur: list[int] = []
    acc = 0
    for i in idxs:
        n = len(texts[i])
        if cur and acc + n > max_chars:
            out.append(cur)
            cur, acc = [], 0
        cur.append(i)
        acc += n
    if cur:
        out.append(cur)
    return out


def _frame_text(f) -> str:
    """取帧文案。Frame 对象 / dict 都认（后者便于脚本与测试直接喂 json）。"""
    if hasattr(f, "text"):
        return (f.text or "").strip()
    if isinstance(f, dict):
        return (f.get("text") or "").strip()
    return str(f).strip()


def plan_groups(frames, script_md: str | None = None,
                target_chars: int = TARGET_GROUP_CHARS,
                max_chars: int | None = None) -> list[list[int]]:
    """返回 frames 的下标分组；`[[0],[1],…]` 即退化为逐帧（= 旧行为）。

    target_chars: 单次合成的**目标**字数。相邻段落会往里打包直到够数 ——
      段落中位只有 65 字，不打包的话「按段落」其实只比逐帧长 5 倍，
      而服务端允许 2 万字符，远远没到瓶颈。
      target_chars<=1 表示不打包（严格一段一组）。
    """
    max_chars = max_chars or max(target_chars, MAX_GROUP_CHARS // 2)
    texts = [_frame_text(f) for f in frames]
    paras = _match_paragraphs(texts, script_md)
    if paras is None:
        # 脚本缺失 / 与 frames 不同源（既有多集多是这种：frames 是老切分规则
        # 的产物）。此时退回**按句边界的字数预算**分组，而不是逐帧 ——
        # 目的就是把 3.3s 的碎片拉长，逐帧等于没启用。
        return _budget_groups(texts, max_chars)

    groups: list[list[int]] = []
    idx = 0
    for pieces in paras:
        n = len(pieces)
        if n <= 0:
            continue
        idxs = list(range(idx, idx + n))
        idx += n
        groups.extend(_split_by_budget(idxs, texts, max_chars))
    # 帧数没对上（脚本与 frames 不同源）→ 整体退化成预算分组，绝不硬套
    if idx != len(frames) or not groups:
        return _budget_groups(texts, max(max_chars, target_chars))
    if target_chars > 1:
        groups = _pack_groups(groups, texts, target_chars, max_chars)
    return groups


def _pack_groups(groups: list[list[int]], texts: list[str],
                 target_chars: int, max_chars: int) -> list[list[int]]:
    """相邻段落打包到 target_chars（硬顶 max_chars），不跨帧序、不拆段落。"""
    out: list[list[int]] = []
    cur: list[int] = []
    acc = 0
    for g in groups:
        n = sum(len(texts[i]) for i in g)
        if cur and acc + n > max_chars:
            out.append(cur)
            cur, acc = [], 0
        cur.extend(g)
        acc += n
        if acc >= target_chars:
            out.append(cur)
            cur, acc = [], 0
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 切片：段落 wav → 逐帧 wav
# ---------------------------------------------------------------------------
def read_wav(path) -> tuple[np.ndarray, int]:
    """读 wav → (float32 单声道样本, 采样率)。"""
    np = _np()
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def write_wav(path, data: np.ndarray, sr: int) -> None:
    """写 16bit 单声道 wav（与既有产物同格式，compose 的采样率校验才过得去）。"""
    np = _np()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(data, -32768, 32767).astype("<i2").tobytes())


def _zero_cross(data: np.ndarray, pos: int, span: int) -> int:
    """把切点挪到最近的零交叉点，避免硬切爆音。"""
    np = _np()
    lo = max(1, pos - span)
    hi = min(len(data) - 1, pos + span)
    if hi <= lo:
        return pos
    seg = data[lo:hi]
    idx = np.where(np.sign(seg[:-1]) != np.sign(seg[1:]))[0]
    if len(idx) == 0:
        return pos
    # 选离 pos 最近的那个
    cand = idx + lo
    return int(cand[np.argmin(np.abs(cand - pos))])


def slice_samples(data: np.ndarray, sr: int, weights: list[float],
                  search_ms: int = SEARCH_MS) -> list[np.ndarray]:
    """按权重把一段样本切成 len(weights) 段，切点落在静音上。"""
    np = _np()
    n = len(data)
    k = len(weights)
    if k <= 1 or n == 0:
        return [data]

    total = float(sum(weights)) or 1.0
    win = max(1, int(sr * ENERGY_WIN_MS / 1000))
    search = max(1, int(sr * search_ms / 1000))
    guard = max(1, int(sr * 0.05))          # 至少留 50ms，别切出空段
    sq = data * data
    csum = np.concatenate(([0.0], np.cumsum(sq)))

    def _energy_at(pos: int) -> float:
        a = max(0, pos - win // 2)
        b = min(n, pos + win // 2)
        return float(csum[b] - csum[a]) if b > a else float("inf")

    cuts = [0]
    rem = float(sum(weights))          # 剩余权重（含当前段）
    for j in range(k - 1):
        w = float(weights[j])
        rem -= w
        # ★ 局部重新锚定：每一步都按「剩余音频 / 剩余权重」估下一段的长度，
        #   而不是一次性按全局比例算。组越长，TTS 语速越不可能全程恒定 ——
        #   全局比例会让后半段越偏越远，重锚定则每步都吸收掉已发生的偏差，
        #   这样把组从 65 字放大到几百字，切片才不会漂。
        span = n - cuts[-1]
        denom = w + rem
        est = cuts[-1] + (int(round(span * w / denom)) if denom > 0 else span)

        # 自适应窗口：段越长，估算越可能漂，窗口就得越宽。
        #   ⚠️ 但**不能**简单取「最安静的点」——窗口一宽，最近的那个停顿未必
        #   是最安静的，就会跳到别的停顿上（某帧偏长、下一帧偏短）。
        #   所以要的是「**离估算位置最近**的那个安静点」。
        seg_ms = 1000.0 * (span * w / denom if denom > 0 else span) / sr
        win_s = max(int(sr * search_ms / 1000),
                    int(sr * min(0.25 * seg_ms, ADAPT_MAX_MS) / 1000))
        lo = max(cuts[-1] + guard, est - win_s)
        hi = min(n - guard, est + win_s)
        best = min(max(est, lo), max(lo, hi))
        if hi > lo:
            step = max(1, int(sr * 0.005))          # 5ms 步长，够快也够细
            ps = np.arange(lo, hi, step)
            a = np.clip(ps - win // 2, 0, n)
            b = np.clip(ps + win // 2, 0, n)
            e = csum[b] - csum[a]
            e_est = _energy_at(est)
            quiet = np.where(e < 0.5 * e_est)[0]     # 明显更安静的候选
            if len(quiet):
                # 取离估算位置**最近**的安静点（不是最安静的）
                best = int(ps[quiet[np.argmin(np.abs(ps[quiet] - est))]])
        cuts.append(max(cuts[-1] + guard, min(best, n - guard)))
    cuts.append(n)

    return [data[cuts[j]:cuts[j + 1]] for j in range(k)]


def group_text(texts: list[str]) -> str:
    """组内文案拼成一次请求的正文。

    各片段本身已带句读/句末标点，直接相接就是自然的中文语流 ——
    **不要**插空格或换行，那会让 TTS 多出不该有的停顿。
    """
    return "".join(t for t in texts if t)
