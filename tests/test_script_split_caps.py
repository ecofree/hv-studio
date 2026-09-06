# -*- coding: utf-8 -*-
"""split_long_sentence 的兜底链（2026-09-06《李世民四部曲》建档实测）。

前一轮修完 markdown 清洗后，拿真剧本建档又暴露三个**上限/下限失守**的问题，
全部出在「逗号切不出来」时的兜底分支：

  1. 无逗号长句按 HARD_CAP=26 硬砍，把「程序正义」劈成
     「…跟你讲程」+「序正义的。」——5 字孤儿镜，字幕断词。
     该句明明有「——」这个天然停顿点，却没被利用。
  2. 超 MAX_FRAG_CHARS(31) 的长句，所有逗号切点都被 MIN 下限否掉后，
     path4 直接 `return [sent]` 整句保留 → 实测切出 35 / 41 字帧，
     41 字 ≈10.5s 一镜撞穿 8s 门禁（上限违规是**必然**不过，不像下限）。
  3. merge_short_pieces 的「孤立标点吸附」分支不受 max_join 约束，
     能把已 30 字的片段再顶过 31 字硬顶。

本文件把这三处行为钉死，并加一条全脚本属性测试：任何帧都不得超硬顶。
"""
from __future__ import annotations

import pytest

from app.models import Episode
from app.services import script as S
from app.services.script import split_script


def _split(md: str, **kw):
    return split_script(Episode(id="t", title="t", episode_number=1), md, **kw)

# 真剧本里的两句（D:/hv/resource/风趣历史.writing-DNA/scripts/
#                  李世民四部曲_第一集_风投的暗涌.md）
SENT_EM_DASH = "被逼到连退休金都拿不到的绝境时——他们是不会跟你讲程序正义的。"
SENT_OVER_CAP = (
    "那是整个隋唐军事史上最惊险的一幕——"
    "三千五百名身披玄黑铁甲的幽州突骑，像一颗钢钉，"
)
SENT_ONE_COMMA = "陇西平原，薛举薛仁杲父子的西秦铁骑甚至打到了离长安只有几十里的浅水原。"


# ---------------------------------------------------------------------------
# 1) 破折号 / 省略号是次级切点
# ---------------------------------------------------------------------------
def test_em_dash_is_used_as_break_not_hard_chop():
    """无逗号长句必须在「——」处断，不得按字数硬砍出孤儿。"""
    parts = S.split_long_sentence(SENT_EM_DASH, max_chars=18)
    assert parts == ["被逼到连退休金都拿不到的绝境时——",
                     "他们是不会跟你讲程序正义的。"], parts
    # 旧行为：['被逼到连退休金都拿不到的绝境时——他们是不会跟你讲程', '序正义的。']
    assert all(len(p) >= S.MIN_FRAG_CHARS for p in parts)
    assert "序正义" not in "".join(parts[:1])


def test_ellipsis_is_also_a_break():
    sent = "他站在城头沉默了很久……远处传来三声闷雷般的战鼓"
    parts = S.split_long_sentence(sent, max_chars=18)
    assert len(parts) >= 2
    assert parts[0].endswith("……"), parts


# ---------------------------------------------------------------------------
# 2) 硬顶：任何片段都不得长于 MAX_FRAG_CHARS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sent", [SENT_OVER_CAP, SENT_EM_DASH, SENT_ONE_COMMA])
def test_no_piece_exceeds_max_frag_chars(sent):
    """上限违规是**必然**不过 8s 门禁的，兜底也绝不能放过。"""
    parts = S.split_long_sentence(sent, max_chars=18)
    assert max(len(p) for p in parts) <= S.MAX_FRAG_CHARS, parts


def test_over_cap_sentence_splits_at_em_dash_not_whole():
    """旧 path4 会把 41 字整句保留；现在必须先切开。"""
    parts = S.split_long_sentence(SENT_OVER_CAP, max_chars=18)
    assert len(parts) >= 2, parts
    assert parts[0] == "那是整个隋唐军事史上最惊险的一幕——", parts


def test_over_cap_sentence_is_cut_even_if_only_leading_comma():
    """超顶句即使只有句首逗号也必须切开（上限优先于下限）。

    这里会留下「陇西平原，」5 字短片——**这是有意为之**：下限违规只是
    「可能」不过门禁（TTS 带逗号停顿常比估算长），上限违规是「必然」不过。
    短片由 merge_short 吸收，见下一条测试。
    """
    parts = S.split_long_sentence(SENT_ONE_COMMA, max_chars=18)
    assert max(len(p) for p in parts) <= S.MAX_FRAG_CHARS, parts
    assert len(parts) >= 2, parts


def test_merge_short_absorbs_the_leading_comma_orphan():
    """上一条留下的短片，经 merge_short 后不应再有 <MIN 的帧。"""
    frames = _split(SENT_ONE_COMMA, merge_short=True)
    assert all(len(f.text) >= S.MIN_FRAG_CHARS for f in frames), \
        [f.text for f in frames]


def test_balanced_split_avoids_cutting_inside_quotes():
    """中分不得把「“关陇军政风投股份|有限公司”」这类切口切出来。"""
    sent = "本质上是“关陇军政风投股份有限公司”的一场超级并购案"
    parts = S._balanced_split(sent)
    assert len(parts) >= 2
    joined = "".join(parts)
    assert joined == sent, "中分不得丢字"
    # 切点不能落在引号内部
    for p in parts[:-1]:
        assert p.count("“") == p.count("”"), f"切在引号内: {parts!r}"


def test_balanced_split_never_returns_orphan():
    long_ = "秦王李世民在虎牢关以三千五百玄甲铁骑硬撼窦建德的十万大军并生擒之"
    parts = S._balanced_split(long_)
    assert len(parts) >= 2
    assert all(len(p) >= S.MIN_FRAG_CHARS for p in parts), parts


def test_balanced_split_degenerate_input():
    """长度不足 2*MIN 时不得越界/空转。"""
    short = "短句"
    assert S._balanced_split(short) == [short]


# ---------------------------------------------------------------------------
# 3) merge_short 的吸附也要受硬顶约束
# ---------------------------------------------------------------------------
def test_punct_only_absorption_respects_hard_cap():
    """30 字片段再吸附一个「，」不得顶穿 MAX_FRAG_CHARS。"""
    base = "一" * 30
    out = S.merge_short_pieces([base, "，"])
    assert max(len(p) for p in out) <= S.MAX_FRAG_CHARS, out


def test_merge_short_never_exceeds_hard_cap_on_long_script():
    md = SENT_OVER_CAP + "\n\n" + SENT_ONE_COMMA + "\n\n" + SENT_EM_DASH
    frames = _split(md, merge_short=True)
    assert max(len(f.text) for f in frames) <= S.MAX_FRAG_CHARS


def test_split_script_default_mode_respects_hard_cap():
    """默认（未合并）模式同样不得出现超硬顶帧。"""
    md = SENT_OVER_CAP + "\n\n" + SENT_ONE_COMMA + "\n\n" + SENT_EM_DASH
    frames = _split(md)
    assert max(len(f.text) for f in frames) <= S.MAX_FRAG_CHARS


# ---------------------------------------------------------------------------
# 4) 回归护栏：这些常量不是手写的，改了必须同步改门禁
# ---------------------------------------------------------------------------
def test_caps_are_derived_not_hardcoded():
    assert S.MAX_FRAG_CHARS == 31, "由 MAX_FRAMES/FPS/语速 推导，非魔数"
    assert S.MIN_FRAG_CHARS == 7
    assert S.MIN_FRAG_CHARS * 2 < S.MAX_FRAG_CHARS, "中分兜底需要两侧都够下限"
