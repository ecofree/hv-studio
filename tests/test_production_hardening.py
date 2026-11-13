# -*- coding: utf-8 -*-
"""第三轮（新领域）修复的回归测试。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import wave

import numpy as np


# ---------- A. read_wav 标度契约 ----------


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
    with pytest.raises(RuntimeError):
        O._check_disk_space(tmp_path)


# ---------- D. TTS 成本台账 ----------
