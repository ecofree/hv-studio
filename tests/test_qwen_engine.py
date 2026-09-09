# -*- coding: utf-8 -*-
"""qwen 引擎 + SSML 路线的回归测试（P1-6 审计修复：新代码零测试）。"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------- HTTP 组装
def test_qwen_request_body_assembly(monkeypatch):
    """synthesize_qwen_whole 的请求体：instruction/hot_fix/rate/enable_ssml。"""
    import app.services.cosyvoice as cv

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            import base64 as b64
            return {"output": {"audio": {
                "data": b64.b64encode(b"mp3bytes").decode()}}}

    def fake_post(url, timeout=None, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(cv.requests, "post", fake_post)
    monkeypatch.setattr(cv, "_audio_duration_ms", lambda p: 0)
    import base64
    monkeypatch.setattr(cv.time, "sleep", lambda s: None) if hasattr(cv, "time") else None

    out = cv.Path(cv.__file__).parent / "_tmp_qwen_test.mp3"
    try:
        cv.synthesize_qwen_whole(
            "测试文本", out, voice="voice-x", model="qwen-audio-3.0-tts-flash",
            instruction="沉稳男声", hot_fix={"pronunciation": [{"石": "dan4"}]},
            rate=1.05, enable_ssml=True, overwrite=True)
        inp = captured["json"]["input"]
        assert inp["voice"] == "voice-x"
        assert inp["instruction"] == "沉稳男声"
        assert inp["hot_fix"] == {"pronunciation": [{"石": "dan4"}]}
        assert inp["rate"] == 1.05
        assert inp["enable_ssml"] is True
        assert captured["json"]["model"] == "qwen-audio-3.0-tts-flash"
    finally:
        out.unlink(missing_ok=True)


def test_qwen_ssml_flag_off_omits_param(monkeypatch):
    import app.services.cosyvoice as cv

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"output": {"audio": {"url": "http://x/y.mp3"}}}

    monkeypatch.setattr(cv.requests, "post",
                        lambda *a, **k: captured.update(json=k["json"]) or FakeResp())
    monkeypatch.setattr(cv.requests, "get", lambda url, timeout=None: type(
        "R", (), {"content": b"x"})())
    monkeypatch.setattr(cv, "_audio_duration_ms", lambda p: 0)

    out = cv.Path(cv.__file__).parent / "_tmp_qwen_test2.mp3"
    try:
        cv.synthesize_qwen_whole("文本", out, voice="v",
                                 model="qwen-audio-3.0-tts-flash",
                                 overwrite=True)
        assert "enable_ssml" not in captured["json"]["input"]
        assert "instruction" not in captured["json"]["input"]
    finally:
        out.unlink(missing_ok=True)


# ---------------------------------------------------------------- 重试逻辑
def test_qwen_quota_error_fails_fast(monkeypatch, tmp_path, capsys):
    """FreeTierOnly 是致命错误：不重试，且报错说人话（P0-2）。"""
    import app.services.cosyvoice as cv
    from app.services import voice as V

    calls = {"n": 0}

    def fake_qwen(*a, **k):
        calls["n"] += 1
        raise RuntimeError('[qwen-tts] HTTP 403: Free quota exhausted '
                           '{"code":"AllocationQuota.FreeTierOnly"}')

    monkeypatch.setattr(cv, "synthesize_qwen_whole", fake_qwen)
    monkeypatch.setattr(V, "_mp3_to_wav", lambda a, b: None)
    ok = V._try_one_qwen("测试文本", tmp_path / "x.wav")
    out = capsys.readouterr().out
    assert ok is False
    assert calls["n"] == 1, "配额错误重试了 —— 应一次即弃"
    assert "额度耗尽" in out, "报错没说人话"
    assert "充值" in out


def test_mixed_uses_majority_engine(tmp_path):
    """整集全 qwen → 不算 mixed（main=占帧数最多者，2026-09-08 修复）。"""
    from app.services.voice import engine_report

    man = {f"frame-{i:03d}": {"engine": "qwen", "fp": "x", "wav": f"{i}.wav"}
           for i in range(20)}
    (tmp_path / "_tts_manifest.json").write_text(
        __import__("json").dumps(man, ensure_ascii=False), encoding="utf-8")
    rep = engine_report(tmp_path)
    assert rep["mixed"] is False, "全 qwen 被误判为混引擎（main 写死 cosyvoice 的老 bug）"
    assert rep["by_engine"].get("qwen") == 20


def test_mixed_detects_real_mix(tmp_path):
    """真的混了两种引擎必须报 mixed。"""
    from app.services.voice import engine_report

    man = {f"frame-{i:03d}": {"engine": "qwen", "fp": "x", "wav": f"{i}.wav"}
           for i in range(10)}
    man["frame-999"] = {"engine": "cosyvoice", "fp": "y", "wav": "9.wav"}
    (tmp_path / "_tts_manifest.json").write_text(
        __import__("json").dumps(man, ensure_ascii=False), encoding="utf-8")
    rep = engine_report(tmp_path)
    assert rep["mixed"] is True
