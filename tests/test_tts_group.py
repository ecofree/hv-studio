# -*- coding: utf-8 -*-
"""段落级 TTS：分组与静音切片（全程不联网、不花钱）。

背景（2026-09-06 用户反馈「语音太短，情感不好」）：
逐帧合成只给 TTS 13~15 字 → 韵律塌。改成按段落合成（中位 65 字 ≈16.7s），
再按静音切回逐帧 wav —— compose 逐帧读 wav 拼时间线，所以**必须切回去**，
否则下游全得改。

本文件只测不花钱的部分：
  · 分组是否真的按段落、是否与 frames 严格对齐；
  · 切片是否落在静音上、是否字字不丢、是否长度守恒。
真实 TTS 效果（情感是否变好）只能靠试听，不写断言。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import numpy as np

from app.services import tts_group as G
from app.services import script as S

# 与真实剧本同构：markdown 标题 + 引用块 + 【标注】+ 两个段落
MD = """# 《测试·第一集》

> **作品类型**：历史解说

第一段的第一句话，它比较长所以需要被切成两个片段来上屏。
这是第一段的第二句。

【语气：压低】第二段只有一句，但它也不短。
"""

# 合成用的假 wav：静音 + 三段“语音”（方波）+ 静音，段间留长静音
SR = 16000


def _tone(ms: int, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(SR * ms / 1000)) / SR
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.float32)


def _sil(ms: int) -> np.ndarray:
    return np.zeros(int(SR * ms / 1000), dtype=np.float32)


def _wav_with_gaps(parts_ms: list[int], gap_ms: int = 400) -> np.ndarray:
    out = [_sil(gap_ms // 2)]
    for i, ms in enumerate(parts_ms):
        out.append(_tone(ms, 200.0 + 40 * i))
        if i < len(parts_ms) - 1:
            out.append(_sil(gap_ms))
    out.append(_sil(gap_ms // 2))
    return np.concatenate(out)


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------
def _frames_from(md: str, merge_short: bool = False):
    from app.models import Episode
    from app.services.script import split_script
    return split_script(Episode(id="t", title="t", episode_number=1),
                        md, merge_short=merge_short)


@pytest.mark.parametrize("merge", [False, True])
def test_groups_follow_paragraphs(merge):
    """target_chars=1（不打包）时，分组应严格等于段落。"""
    frames = _frames_from(MD, merge_short=merge)
    groups = G.plan_groups(frames, MD, target_chars=1)
    assert len(groups) >= 2, "两个段落至少分两组"
    # 每组内的帧必须是**连续**下标，且覆盖全部帧、不重不漏
    flat = [i for g in groups for i in g]
    assert flat == list(range(len(frames)))
    for g in groups:
        assert g == list(range(g[0], g[-1] + 1))


@pytest.mark.parametrize("merge", [False, True])
def test_group_pieces_concatenate_to_paragraph(merge):
    """组内帧的文案拼起来 = 该段落的原文（不插空格/换行）。"""
    frames = _frames_from(MD, merge_short=merge)
    paras = G._pipeline_paragraphs(MD, merge)
    groups = G.plan_groups(frames, MD, target_chars=1)
    assert len(groups) == len(paras)
    for g, para in zip(groups, paras):
        joined = G.group_text([frames[i].text for i in g])
        assert joined == "".join(para)


def _assert_budget_fallback(frames, groups, max_chars=150):
    """预算兜底：覆盖全帧、每段连续，且不在句子中间收口。"""
    flat = [i for g in groups for i in g]
    assert flat == list(range(len(frames)))
    for g in groups[:-1]:
        last = frames[g[-1]].text.rstrip()
        chars = sum(len(frames[i].text) for i in g)
        assert last.endswith(G._SENT_ENDINGS) or chars >= max_chars, \
            f"在句子中间收口了: {last!r}"


def test_no_script_falls_back_to_budget_grouping():
    """脚本缺失 → 退回按句边界的字数预算，而不是逐帧（逐帧等于没启用）。"""
    frames = _frames_from(MD)
    groups = G.plan_groups(frames, None)
    _assert_budget_fallback(frames, groups)


def test_mismatched_script_falls_back_to_budget_grouping():
    """frames 与脚本不同源（老切分规则的产物）→ 不许硬套段落，退回预算分组。"""
    frames = _frames_from(MD)
    groups = G.plan_groups(frames, "# 别的剧本\n\n完全不一样的内容。\n")
    _assert_budget_fallback(frames, groups)


def test_budget_grouping_never_cuts_mid_sentence():
    """预算分组的收口只能落在句末（或撞到字数上限）。"""
    md = "第一句的内容，还挺长的呢。第二句也不错，同样不短。第三句收尾。"
    frames = _frames_from(md)
    groups = G.plan_groups(frames, None, max_chars=150)
    _assert_budget_fallback(frames, groups)


def test_overlong_paragraph_is_split_by_budget():
    md = "一句话。" * 60          # 240 字，一段
    frames = _frames_from(md)
    groups = G.plan_groups(frames, md, target_chars=150, max_chars=150)
    for g in groups:
        assert sum(len(frames[i].text) for i in g) <= 150


def test_real_script_lishimin_grouping():
    """真剧本：段落数、覆盖率（target_chars=1 = 严格按段落）。"""
    from pathlib import Path
    p = Path("episodes/lishimin-01/script.md")
    if not p.exists():
        pytest.skip("缺少 lishimin-01 剧本")
    md = p.read_text(encoding="utf-8")
    frames = _frames_from(md, merge_short=True)
    groups = G.plan_groups(frames, md, target_chars=1)
    flat = [i for g in groups for i in g]
    assert flat == list(range(len(frames)))
    sizes = [len(g) for g in groups]
    assert 40 <= len(groups) <= 60, f"段落数异常: {len(groups)}"
    assert max(sizes) >= 4, "应当存在多帧段落，否则等于没启用"


# ---------------------------------------------------------------------------
# 打包：把 65 字的小段落并成几百字（65 字是保守值，服务端上限 2 万字符）
# ---------------------------------------------------------------------------
def test_packing_raises_group_size():
    frames = _frames_from(MD, merge_short=False)
    small = G.plan_groups(frames, MD, target_chars=1)
    packed = G.plan_groups(frames, MD, target_chars=10 ** 6)
    assert len(packed) < len(small)
    assert [i for g in packed for i in g] == list(range(len(frames)))


@pytest.mark.parametrize("target", [150, 400, 800])
def test_packing_respects_target_and_hard_cap(target):
    from pathlib import Path
    p = Path("episodes/lishimin-01/script.md")
    if not p.exists():
        pytest.skip("缺少 lishimin-01 剧本")
    md = p.read_text(encoding="utf-8")
    frames = _frames_from(md, merge_short=True)
    groups = G.plan_groups(frames, md, target_chars=target)
    chars = [sum(len(frames[i].text) for i in g) for g in groups]
    assert max(chars) <= G.MAX_GROUP_CHARS, "硬顶失效"
    # 打包后每组都应接近目标（最后一组除外）
    body = [c for c in chars[:-1]]
    if body and len(chars) > 1:
        assert min(body) >= min(target, 40), f"还有过小的组: {sorted(body)[:3]}"


def test_packing_never_reorders_or_drops_frames():
    frames = _frames_from(MD)
    for t in (1, 50, 400, 10 ** 6):
        groups = G.plan_groups(frames, MD, target_chars=t)
        assert [i for g in groups for i in g] == list(range(len(frames)))


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------
def test_slice_lands_in_silence():
    """三段等长语音、段间 400ms 静音 → 切点必须落在静音里。"""
    data = _wav_with_gaps([1000, 1000, 1000], gap_ms=400)
    segs = G.slice_samples(data, SR, [1.0, 1.0, 1.0])
    assert len(segs) == 3
    for s in segs:
        assert len(s) > 0
    # 切点处不应是响的
    n = 0
    for s in segs[:-1]:
        n += len(s)
        assert abs(float(data[n])) < 50.0, f"切在响的地方: {data[n]}"


def test_slice_is_lossless():
    """切完再拼必须字字不丢（时长守恒，不能吞帧）。"""
    data = _wav_with_gaps([800, 1200, 900, 700], gap_ms=300)
    segs = G.slice_samples(data, SR, [1.0, 1.0, 1.0, 1.0])
    assert sum(len(s) for s in segs) == len(data)
    assert np.allclose(np.concatenate(segs), data)


def test_slice_respects_weights():
    """权重大的段拿到的音频更长。"""
    data = _wav_with_gaps([1000, 1000], gap_ms=200)
    segs = G.slice_samples(data, SR, [3.0, 1.0])
    assert len(segs[0]) > len(segs[1])


def test_slice_picks_nearest_quiet_point_not_quietest():
    """窗口变宽后必须选**离估算最近**的安静点，而不是全场最安静的那个。

    构造：估算位置两侧各有一个停顿，远的那个更安静。
    选「最安静」会跳到远处 → 前一段过长、后一段过短（字幕与画面对不上）。
    """
    # [1s 语音][近停顿 300ms 弱静音][1s 语音][远停顿 600ms 全静音][1s 语音]
    data = np.concatenate([
        _tone(1000),
        (_tone(300) * 0.05).astype(np.float32),   # 弱静音（不太安静）
        _tone(1000),
        _sil(600),                                # 全静音（更安静）
        _tone(1000),
    ])
    segs = G.slice_samples(data, SR, [1.0, 1.0, 1.0])
    first = len(segs[0])
    # 真值：第一段结束在 1.0~1.3s 之间（近停顿），绝不能跑到 2.3s 的远停顿
    assert SR * 0.9 <= first <= SR * 1.45, f"跳到远处停顿了: {first/SR:.2f}s"


def test_slice_tolerates_moderate_rate_drift():
    """语速与字数权重不完全成正比时（±20%），靠自适应窗口 + 就近安静点拉回。

    ⚠️ 这条只承诺「中等漂移」。比例估算扛不住成倍的语速差（比如某段突然慢 3 倍），
    那种情况只能靠真正的强制对齐（ASR），本项目没做 —— 真出现了会由
    voice 的切片偏差自检打印告警，而不是静默出错。
    """
    # 真实时长 2.0s / 3.0s / 2.0s，但权重给成等权（估算会说各 2.33s）
    data = np.concatenate([_tone(2000), _sil(400),
                           _tone(3000), _sil(400), _tone(2000)])
    segs = G.slice_samples(data, SR, [1.0, 1.0, 1.0])
    b1 = len(segs[0])
    b2 = b1 + len(segs[1])
    assert SR * 1.9 <= b1 <= SR * 2.5, f"第一刀漂了: {b1/SR:.2f}s"
    assert SR * 5.3 <= b2 <= SR * 5.9, f"第二刀漂了: {b2/SR:.2f}s"


def test_slice_single_and_empty():
    data = _tone(300)
    assert len(G.slice_samples(data, SR, [1.0])) == 1
    assert len(G.slice_samples(np.array([], dtype=np.float32), SR, [1.0])) == 1


def test_wav_roundtrip(tmp_path):
    p = tmp_path / "a.wav"
    data = _tone(250)
    G.write_wav(p, data, SR)
    back, sr = G.read_wav(p)
    assert sr == SR
    assert len(back) == len(data)
    assert np.max(np.abs(back - data)) < 10.0      # 16bit 量化误差内


# ---------------------------------------------------------------------------
# 权重
# ---------------------------------------------------------------------------
def test_punctuation_adds_weight():
    """句末标点的停顿权重必须 > 句读（切片位置才估得准）。"""
    assert G.piece_weight("他说，") - G.piece_weight("他说") > 0
    assert G.piece_weight("他说。") > G.piece_weight("他说，")


def test_group_text_has_no_separator():
    assert G.group_text(["甲，", "乙。"]) == "甲，乙。"
    assert G.group_text(["甲。", "", "乙。"]) == "甲。乙。"
