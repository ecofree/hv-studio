# -*- coding: utf-8 -*-
"""SSML 生成器（W5 路线）：逐句 rate + 多音字 phoneme + 转义顺序。"""
from __future__ import annotations

import pytest


def test_sentence_split_and_rates():
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["平铺叙述的一句。", "感叹的一句！", "组末收束的一句。"])
    assert out.count("<speak") == 3
    assert 'rate="1.05"' in out            # 基础
    assert 'rate="1.12"' in out            # 感叹
    assert 'rate="0.98"' in out            # 组末收束


def test_question_rate():
    from app.services.voice import build_group_ssml
    # 疑问句不在组末（组末收束 0.86 优先级更高，另有用例覆盖）
    out = build_group_ssml(["大唐开国靠的是什么？靠的是关陇军事贵族的门荫特权！",
                             "组末收束的一句。"])
    assert 'rate="1.05"' in out
    assert 'rate="1.12"' in out


def test_phoneme_inserted_for_historical_names():
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["尉迟恭站在城楼上。"])
    assert '<phoneme alphabet="py" ph="yu4 chi2">尉迟</phoneme>' in out


def test_phoneme_longest_match_first():
    """「吐谷浑」「吐蕃」共享前缀「吐」，长词必须优先匹配。"""
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["吐谷浑西迁，吐蕃崛起。"])
    assert 'ph="tu3 yu4 hun2">吐谷浑</phoneme>' in out
    assert 'ph="tu3 bo1">吐蕃</phoneme>' in out


def test_xml_escaping_does_not_break_phoneme_tags():
    """转义必须先于 phoneme 插入，否则标签被一起转义（实测踩坑）。"""
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["尉迟恭站在城楼上。"])
    assert "&lt;phoneme" not in out
    assert "<phoneme" in out


def test_ampersand_escaped():
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["张三和李四。A&B。"])
    assert "A&amp;B" in out


def test_no_nesting():
    from app.services.voice import build_group_ssml
    out = build_group_ssml(["第一句。", "第二句！"])
    assert "<speak><speak" not in out
