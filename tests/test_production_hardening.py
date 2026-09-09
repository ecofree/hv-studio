# -*- coding: utf-8 -*-
"""第三轮（新领域）修复的回归测试。"""
from __future__ import annotations

import wave

import numpy as np
import pytest


# ---------- A. read_wav 标度契约 ----------
def _write_wav(path, samples_i16: np.ndarray, sr: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples_i16.astype("<i2").tobytes())


def test_read_wav_is_int16_scale_not_normalized(tmp_path):
    """read_wav 返回 ±32768 标度（不是 [-1,1]）—— 契约必须显式钉住。

    2026-09-08 血泪：按「[-1,1]」的直觉做 clip 会把波形削成方波。
    """
    from app.services.tts_group import read_wav

    p = tmp_path / "a.wav"
    _write_wav(p, np.array([0, 32767, -32768, 16384], dtype=np.int16))
    data, sr = read_wav(p)
    assert sr == 16000
    assert abs(data[1]) > 1000, "read_wav 不该归一化（应在 ±32768 标度）"


def test_read_wav_float_is_normalized(tmp_path):
    from app.services.tts_group import read_wav_float

    p = tmp_path / "b.wav"
    _write_wav(p, np.array([0, 32767, -32768], dtype=np.int16))
    data, _ = read_wav_float(p)
    assert data.max() <= 1.0 and data.min() >= -1.0
    assert abs(data[1] - 1.0) < 0.01


def test_read_wav_roundtrip_no_clipping(tmp_path):
    """用 read_wav_float 读数再写回，不应削波（旧的 clip(-1,1) bug）。"""
    from app.services.tts_group import read_wav_float

    p = tmp_path / "c.wav"
    src = (np.sin(np.linspace(0, 20, 4000)) * 30000).astype(np.int16)
    _write_wav(p, src)
    data, _ = read_wav_float(p)
    back = np.clip(data, -1.0, 1.0) * 32767
    # 归一化后幅值应接近原始（方波化会让大量样本贴到 ±32767）
    assert np.abs(back).max() > 20000
    assert (np.abs(back) > 32000).sum() < len(back) * 0.1


# ---------- B. _grp_*.wav 纳入清理扫描 ----------
def test_grp_wav_is_scanned_as_tmp(tmp_path):
    """段落合成中间产物 _grp_*.wav 必须被 clean 扫到（注释曾承诺但没实现）。"""
    from app.services.cleanup import scan_episode_build

    ep = tmp_path / "ep-x"
    (ep / "audio").mkdir(parents=True)
    f = ep / "audio" / "_grp_00000.wav"
    f.write_bytes(b"RIFF" + b"\0" * 1000)
    found = scan_episode_build(ep)
    paths = [str(x["path"]) for x in found]
    assert any("_grp_" in p for p in paths), "_grp_*.wav 没被扫到 → 每集残留 28M"


def test_scan_never_touches_shots(tmp_path):
    """结构性保证：扫描绝不碰 shots/（花钱生成且跨集复用）。"""
    from app.services.cleanup import scan_episode_build

    ep = tmp_path / "ep-y"
    (ep / "shots").mkdir(parents=True)
    (ep / "shots" / "L01.png").write_bytes(b"\x89PNG" + b"\0" * 2000)
    found = scan_episode_build(ep)
    assert not any("shots" in str(x["path"]) for x in found)


# ---------- C. 磁盘预检分级 ----------
def test_disk_precheck_raises_only_below_hard_floor(monkeypatch, tmp_path):
    """低于 800MB 硬底拒绝；800~2500MB 只告警（低磁盘机器不该被一刀切）。"""
    from app import orchestrator as O

    class DU:
        def __init__(self, free_mb):
            self.free = int(free_mb * 1024 * 1024)

    import shutil as _sh
    monkeypatch.setattr(_sh, "disk_usage", lambda p: DU(700))
    with pytest.raises(RuntimeError):
        O._check_disk_space(tmp_path)

    monkeypatch.setattr(_sh, "disk_usage", lambda p: DU(1200))
    O._check_disk_space(tmp_path)          # 只告警，不抛


# ---------- D. TTS 成本台账 ----------
def test_tts_cost_ledger_records(monkeypatch, tmp_path):
    """TTS 侧此前零记录（出 6 遍片花了多少钱无法回答）。"""
    from app.services import voice as V

    monkeypatch.setattr(V, "_tts_record_cost", V._tts_record_cost)  # noqa
    # 直接调函数验证写入（接线处已在 synthesize 内 try 包裹）
    import app.services.voice as _v
    orig = _v._tts_record_cost
    try:
        import os
        cwd = os.getcwd()
        os.chdir(tmp_path)
        (tmp_path / "state").mkdir(exist_ok=True)
        orig("ep-test", "qwen-audio-3.0-tts-flash", 3630, True)
        p = tmp_path / "state" / "tts_cost_ledger.json"
        assert p.exists(), "TTS 台账没写"
        import json
        rows = json.loads(p.read_text(encoding="utf-8"))
        assert rows[-1]["chars"] == 3630
        assert rows[-1]["model"] == "qwen-audio-3.0-tts-flash"
    finally:
        os.chdir(cwd)
        _v._tts_record_cost = orig


def test_tts_cost_ledger_never_blocks(tmp_path):
    """台账写坏不能阻塞生产。"""
    from app.services import voice as V
    import os
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state").chmod(0o500)         # 只读目录 → 写入必失败
        V._tts_record_cost("x", "m", 1, True)     # 不该抛
    finally:
        (tmp_path / "state").chmod(0o700)
        os.chdir(cwd)
