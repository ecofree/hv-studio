# -*- coding: utf-8 -*-
"""checkpoint 轮转测试（D6）。

checkpoint 每 stage 一份、文件名带微秒时间戳 → 永不复写、只增不减。
这里锁：写第 N+1 份时，最旧的被删，且保留的永远是最近的 KEEP 份。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.shared


from app.managers import episode as EM
from app.models import Episode


def test_rotation_keeps_only_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(EM, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    ep = Episode(id="rot", title="rot", episode_number=1)

    paths = []
    for i in range(6):
        paths.append(EM.checkpoint(ep, f"stage-{i}", keep=3))

    left = EM.list_checkpoints("rot")
    assert len(left) == 3, [p.name for p in left]
    # 保留的必须是最后 3 个（文件名按时间戳递增）
    assert [p.name for p in left] == [p.name for p in paths[-3:]]
    # 前 3 个已被删
    for p in paths[:3]:
        assert not p.exists()


def test_no_rotation_when_under_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(EM, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    ep = Episode(id="rot2", title="rot2", episode_number=1)
    for i in range(3):
        EM.checkpoint(ep, f"s{i}", keep=10)
    assert len(EM.list_checkpoints("rot2")) == 3


def test_keep_zero_disables_rotation(tmp_path, monkeypatch):
    """HV_CHECKPOINT_KEEP=0 / keep=0 → 保留全部（需要手工排查时别自动删）。"""
    monkeypatch.setattr(EM, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    ep = Episode(id="rot3", title="rot3", episode_number=1)
    for i in range(5):
        EM.checkpoint(ep, f"s{i}", keep=0)
    assert len(EM.list_checkpoints("rot3")) == 5


def test_rotate_on_empty_dir_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(EM, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    assert EM.rotate_checkpoints("nope", keep=3) == []
