# -*- coding: utf-8 -*-
"""TTS 文本清洗 + 中分不破词 + 多音字不误伤。

对应 2026-09-08 看片反馈里确认的三处真问题：
  1. `——` 被念出来（帧59 听写成「马琳」、帧11 变硬句号）→ tts_clean 剥掉
  2. 无标点长句中分把词劈开（帧54「打到」、帧160「功臣」）→ _balanced_split 避开
  3. 「骑都尉」读 qí（应 jì）→ polyphone 整词替换，且不能误伤「尉迟恭」
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------- 破折号
@pytest.mark.parametrize("src,want", [
    ("幸好，历史还漏下了一本没来得及烧毁的孤本——",
     "幸好，历史还漏下了一本没来得及烧毁的孤本，"),
    ("一个在当时当刻保全全家性命的唯一选择——把自己的两个嫡子，",
     "一个在当时当刻保全全家性命的唯一选择，把自己的两个嫡子，"),
    ("那是整个隋唐军事史上最惊险的一幕——",
     "那是整个隋唐军事史上最惊险的一幕，"),
])
def test_tts_clean_strips_dash_run(src, want):
    from app.services.voice import tts_clean
    assert tts_clean(src) == want


def test_tts_clean_leaves_normal_text():
    from app.services.voice import tts_clean
    s = "武德九年六月初三夜。长安城的大雨终于停了。"
    assert tts_clean(s) == s


def test_tts_clean_no_double_punct():
    from app.services.voice import tts_clean
    out = tts_clean("句子末尾——")
    assert "，，" not in out and "，。" not in out


def test_tts_clean_is_idempotent():
    from app.services.voice import tts_clean
    s = "选择——把自己的两个嫡子，"
    assert tts_clean(tts_clean(s)) == tts_clean(s)


# ---------------------------------------------------------------- 中分不破词
def test_balanced_split_keeps_da_dao_together():
    """帧54：不能切成「…甚至打」+「到了离长安…」。"""
    from app.services.script import split_long_sentence
    s = "陇西平原，薛举薛仁杲父子的西秦铁骑甚至打到了离长安只有几十里的浅水原。"
    parts = split_long_sentence(s)
    assert not any(p.endswith("甚至打") for p in parts)


def test_balanced_split_keeps_gong_chen_together():
    """帧160：不能切成「…百战功」+「臣面临…」（"功臣"被劈）。"""
    from app.services.script import split_long_sentence
    s = "一边是天策府内部几百名百战功臣面临被和平解散的生存绝境。"
    parts = split_long_sentence(s)
    assert not any(p.endswith("百战功") for p in parts)


def test_balanced_split_still_splits_when_no_good_cut():
    """挑不到不破词的位置时必须照旧切，不能因为挑位置而放弃切分。"""
    from app.services.script import split_long_sentence
    s = "了了了了了了了了了了了了了了了了了了了了了了了了了了了了了了"
    parts = split_long_sentence(s)
    assert len(parts) > 1


def test_dash_break_point_still_works():
    """反例 A（旧回归）：破折号切点仍要给出 18 + 23。"""
    from app.services.script import split_long_sentence
    s = "那是整个隋唐军事史上最惊险的一幕——三千五百名身披玄黑铁甲的幽州突骑，像一颗钢钉，"
    parts = split_long_sentence(s)
    assert len(parts) >= 2
    assert parts[0].endswith("——")


# ---------------------------------------------------------------- 多音字
def test_qi_du_wei_replaced():
    from app.services.polyphone import preprocess
    assert preprocess("正四品骑都尉；", log_hits=False) == "正四品季都尉；"


def test_wei_chi_gong_not_broken_by_qi_rule():
    """整词替换：不能因为「骑都尉」规则而把「尉迟恭」改坏。"""
    from app.services.polyphone import preprocess
    assert preprocess("尉迟恭", log_hits=False) == "玉迟恭"


def test_both_in_same_script():
    """同一篇里两种「尉」读法并存，互不干扰。"""
    from app.services.polyphone import preprocess
    s = "正四品骑都尉；长孙无忌、尉迟恭"
    out = preprocess(s, log_hits=False)
    assert "季都尉" in out and "玉迟恭" in out and "掌孙" in out
