# -*- coding: utf-8 -*-
"""多音字预处理测试（D5）。

polyphone 是纯函数、零外部依赖，本该是全项目最好测的模块，此前 0 测试。
这里锁住三条承诺：
  1) 单字规则必须被硬拦（抄一份分类/判据就会漂移，这里锁"不漂移"的方式是锁行为）
  2) 最长匹配优先（大月氏 优先于 月氏）
  3) 命中日志：进程内缓冲 + flush 一次性落盘，不写盘时不产生文件
"""
from __future__ import annotations

import pytest

import app.services.polyphone as P


@pytest.fixture
def _rules_restored():
    """任何改了 CONFIG_PATH / 规则的用例，跑完必须还原真实规则表。"""
    old_path, old_loaded = P.CONFIG_PATH, P._LOADED
    try:
        yield
    finally:
        P.CONFIG_PATH = old_path
        P._LOADED = old_loaded
        P.reload()


def test_no_single_char_rule_in_shipped_config():
    """出厂规则表里不允许有单字规则（单字 = 全文无差别替换）。"""
    rules = P.rules()
    assert rules, "规则表不应为空"
    bad = [k for k in rules if len(str(k)) < P.MIN_KEY_LEN]
    assert bad == [], f"存在单字规则（会无差别替换全文）: {bad}"


def test_single_char_rule_is_rejected(_rules_restored, tmp_path):
    """加载含单字规则的 yaml → 硬抛 ValueError，而不是静默不生效。"""
    p = tmp_path / "p.yaml"
    p.write_text("rules:\n  氐: 低\n", encoding="utf-8")
    P.CONFIG_PATH = p
    with pytest.raises(ValueError, match="单字规则"):
        P.reload()


def test_phrase_rule_not_mangled_by_char_match(_rules_restored):
    """单字规则时期会把「摩羯座」改成「摩杰座」；改词组后不应再动。"""
    P.reload()
    assert P.preprocess("他是摩羯座", log_hits=False) == "他是摩羯座"
    # 词组规则仍然生效
    assert P.preprocess("氐族人建立了前秦", log_hits=False) == "低族人建立了前秦"
    assert P.preprocess("羯族与氐族", log_hits=False) == "杰族与低族"


def test_longest_match_wins(_rules_restored):
    """最长匹配优先：大月氏(zhī) 不能被短规则拆开误替。"""
    P.reload()
    out = P.preprocess("大月氏与乌孙", log_hits=False)
    assert "月支" in out, out          # 大月氏 → 大月支
    assert "氏" not in out, out        # 不该留下未处理的 氏


def test_hits_are_buffered_until_flush(_rules_restored, tmp_path, monkeypatch):
    """命中先进缓冲；只有 flush 才落盘，且一次写完。"""
    monkeypatch.setattr(P, "HITS_LOG", tmp_path / "hits.log")
    P.reload()
    P.preprocess("氐族人", log_hits=True)
    P.preprocess("羯族人", log_hits=True)
    assert not P.HITS_LOG.exists(), "flush 之前不应产生日志文件"
    n = P.flush_hits()
    assert n == 2, n
    lines = P.HITS_LOG.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "氐族" in lines[0] and "羯族" in lines[1]
    # 再 flush 一次应为空转，不重复写
    assert P.flush_hits() == 0
    assert len(P.HITS_LOG.read_text(encoding="utf-8").splitlines()) == 2


def test_hits_log_rotates_when_too_big(_rules_restored, tmp_path, monkeypatch):
    """超过 HITS_LOG_MAX_BYTES 时先轮转成 *.1，避免无限增长。"""
    log = tmp_path / "hits.log"
    log.write_text("x" * (P.HITS_LOG_MAX_BYTES + 10), encoding="utf-8")
    monkeypatch.setattr(P, "HITS_LOG", log)
    P.reload()
    P.preprocess("氐族人", log_hits=True)
    P.flush_hits()
    rotated = tmp_path / "hits.log.1"
    assert rotated.exists(), "旧日志应被轮转保留"
    assert log.stat().st_size < P.HITS_LOG_MAX_BYTES


def test_log_hits_false_writes_nothing(_rules_restored, tmp_path, monkeypatch):
    """显式关掉 log_hits（测试/批处理热路径）→ 完全不落盘。"""
    monkeypatch.setattr(P, "HITS_LOG", tmp_path / "hits.log")
    P.reload()
    for _ in range(5):
        P.preprocess("氐族人", log_hits=False)
    P.flush_hits()
    assert not P.HITS_LOG.exists()
