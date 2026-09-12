# -*- coding: utf-8 -*-
"""P0-1 验收：TTS 幂等必须由【旁白文本指纹】驱动，而非仅看 wav 是否存在。

对应 2026-09-04 审计 P0-1：
  - 旧行为：narration_text 改变后，旧音频被静默复用。
  - 修复后：文本变 → 删旧 wav 重合成 + _tts_manifest.json 指纹更新。
验收：文本不变复用（不重合成）；文本变则重合成。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import json
import os
import wave
from pathlib import Path


from app.models import Frame


@pytest.fixture(autouse=True)
def isolated_voice_asset(tmp_path,monkeypatch):
    """This cache test supplies audio and ASR stubs; it must not load a real model."""
    from app.config.config import get_settings
    from app.services import narration_pipeline as N
    from app.services.provenance import atomic_json, file_sha256, fingerprint
    from app.services import asr_check
    monkeypatch.setattr(asr_check, 'check', lambda path, text: asr_check.QualityResult(
        'passed', file_sha256(path), fingerprint(text), transcript=text))
    cache=tmp_path/'voice-cache.json';atomic_json(cache,dict(voice_id='cosyvoice-fixture',model='cosyvoice-v3.5-flash'))
    asset=tmp_path/'voice.json';N.import_voice(cache,asset)
    monkeypatch.setattr(get_settings().get_profile().voice,'voice_asset',str(asset))


def _mk_wav(p: Path, ms: int = 2000):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * ms / 1000))


def test_tts_unchanged_text_reuses(tmp_path, monkeypatch):
    """文本未变 → 复用已有 wav，不重合成。"""
    import app.services.voice as V

    # 打桩：不真调网络，直接写 wav
    def fake_cosy(text, wav, voice_prompt):
        _mk_wav(wav)
        return True
    monkeypatch.setattr(V, "_try_one_cosyvoice", fake_cosy)

    f1 = Frame(id="frame-000", text="测试句子甲", narration_text="测试句子甲")
    V.synthesize([f1], engine="cosyvoice", out_dir=tmp_path)
    assert (tmp_path / "frame-000.wav").exists()
    man = json.loads((tmp_path / "_tts_manifest.json").read_text(encoding="utf-8"))
    fp_old = man["frame-000"]["fp"]

    m1 = (tmp_path / "frame-000.wav").stat().st_mtime_ns
    V.synthesize([f1], engine="cosyvoice", out_dir=tmp_path)  # 同文本重跑
    m2 = (tmp_path / "frame-000.wav").stat().st_mtime_ns
    assert m1 == m2, "文本未变却被重合成"
    # 【bug 修复】必须从盘上重读一次 manifest，否则断言的是内存里没变过的 dict
    man2 = json.loads((tmp_path / "_tts_manifest.json").read_text(encoding="utf-8"))
    assert man2["frame-000"]["fp"] == fp_old, "文本未变但指纹应保持不变（重读盘后）"


def test_tts_changed_text_resynthesizes(tmp_path, monkeypatch):
    """文本变了 → 删旧 wav 重合成 + 指纹更新。"""
    import app.services.voice as V

    def fake_cosy(text, wav, voice_prompt):
        _mk_wav(wav)
        return True
    monkeypatch.setattr(V, "_try_one_cosyvoice", fake_cosy)

    f1 = Frame(id="frame-000", text="测试句子甲", narration_text="测试句子甲")
    V.synthesize([f1], engine="cosyvoice", out_dir=tmp_path)
    man = json.loads((tmp_path / "_tts_manifest.json").read_text(encoding="utf-8"))
    fp_old = man["frame-000"]["fp"]

    f2 = Frame(id="frame-000", text="测试句子乙改动", narration_text="测试句子乙改动")
    V.synthesize([f2], engine="cosyvoice", out_dir=tmp_path)
    man2 = json.loads((tmp_path / "_tts_manifest.json").read_text(encoding="utf-8"))
    assert man2["frame-000"]["fp"] != fp_old, "文本变后指纹应更新"
