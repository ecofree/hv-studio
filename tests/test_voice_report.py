# -*- coding: utf-8 -*-
"""TTS 引擎打标 / 瞬时判定测试（D5）。

B2 加了「降级 edge 就在 manifest 打标 + 成片前体检」，但没有任何测试守着，
等于下一个改动随手就能把 mixed 检测改坏。这里锁：
  1) engine_report 的 mixed / degraded 口径（含"全主引擎不算 mixed"）
  2) TTS 的瞬时判定必须复用生图那份判据链，不许另起一份（写两份必漂移）
"""
from __future__ import annotations

import json

import app.services.voice as V


def _manifest(base, entries: dict):
    base.mkdir(parents=True, exist_ok=True)
    (base / "_tts_manifest.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_engine_report_empty_dir(tmp_path):
    r = V.engine_report(tmp_path)
    assert r == {"total": 0, "by_engine": {}, "mixed": False, "degraded": []}


def test_engine_report_all_main_is_not_mixed(tmp_path):
    _manifest(tmp_path, {
        "frame-000": {"engine": V.ENGINE_DASHSCOPE},
        "frame-001": {"engine": V.ENGINE_DASHSCOPE},
    })
    r = V.engine_report(tmp_path)
    assert r["total"] == 2
    assert r["by_engine"] == {V.ENGINE_DASHSCOPE: 2}
    assert r["mixed"] is False and r["degraded"] == []


def test_engine_report_detects_mixed_voices(tmp_path):
    """一部片子里混了 edge 降级 → mixed=True 且点名到帧（B2-2 的核心承诺）。"""
    _manifest(tmp_path, {
        "frame-000": {"engine": V.ENGINE_DASHSCOPE},
        "frame-002": {"engine": V.ENGINE_EDGE},
        "frame-001": {"engine": V.ENGINE_DASHSCOPE},
    })
    r = V.engine_report(tmp_path)
    assert r["mixed"] is True
    assert r["degraded"] == ["frame-002"], r["degraded"]
    assert r["by_engine"] == {V.ENGINE_DASHSCOPE: 2, V.ENGINE_EDGE: 1}


def test_engine_report_treats_missing_engine_as_degraded(tmp_path):
    """老 manifest 没有 engine 字段 → 按 unknown 计，同样算 degraded（不静默放过）。"""
    _manifest(tmp_path, {"frame-000": {"engine": V.ENGINE_DASHSCOPE},
                         "frame-001": {"no_engine": 1}})
    r = V.engine_report(tmp_path)
    assert r["by_engine"].get(V._ENGINE_UNKNOWN) == 1
    assert r["mixed"] is True and "frame-001" in r["degraded"]


def test_tts_transient_reuses_image_classifier(monkeypatch):
    """TTS 不再自己写一份异常分类：换掉 image.is_transient_error 必须跟着变。"""
    import app.services.image as IMG
    calls = []

    def fake(e):
        calls.append(e)
        return True

    monkeypatch.setattr(IMG, "is_transient_error", fake)
    err = RuntimeError("boom")
    assert V._is_transient(err) is True
    assert calls == [err], "应直接委托 image.is_transient_error，而不是自己判"
