# -*- coding: utf-8 -*-
"""运维能力测试：底图冷备 / state gc / 端到端冒烟。

⚠️ 项目硬规则（tests/test_cleanup.py 用 AST 强制）：测试里**不得拼真实
episodes/ 路径**。需要落盘 episode 的一律用 conftest 的 `tmp_episodes`
夹具（它把 episodes_dir() 重定向到 tmp_path）。
"""
from __future__ import annotations

import os
import time

import pytest


# --------------------------------------------------------------------------- ①
def test_backup_shots_copies_and_preserves_mtime(tmp_episodes, tmp_path):
    """备份 shots/ 且保留 mtime —— upscale 的 @2x 缓存按源图 mtime 失效。"""
    from app.services import hv_ops
    from app.paths import episodes_dir

    ep_id = "ep-bktest"
    shots = episodes_dir() / ep_id / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    f = shots / "A01.png"
    f.write_bytes(b"\x89PNG" + b"\0" * 500)
    os.utime(f, (1000000, 1000000))

    r = hv_ops.backup_shots(ep_id, dest_root=tmp_path / "bk")
    assert r["copied"] == 1
    dst = tmp_path / "bk" / ep_id / "A01.png"
    assert dst.exists()
    assert abs(dst.stat().st_mtime - f.stat().st_mtime) < 2, "mtime 丢了"

    # 幂等：第二次全跳过
    r2 = hv_ops.backup_shots(ep_id, dest_root=tmp_path / "bk")
    assert r2["copied"] == 0 and r2["skipped"] == 1


def test_backup_missing_shots_is_noop(tmp_episodes, tmp_path):
    from app.services import hv_ops
    r = hv_ops.backup_shots("__nope__", dest_root=tmp_path)
    assert r.get("skipped") == "无 shots/ 目录"


# --------------------------------------------------------------------------- ②
def test_gc_removes_only_old_bt_dirs(monkeypatch, tmp_path):
    """gc 只删 _bt_* 且只删超过阈值的；其它目录一律不动（绝不 glob 后 rmtree）。"""
    import app.paths as paths
    from app.services import hv_ops

    st = tmp_path / "state"
    monkeypatch.setattr(paths, "state_dir", lambda: st)
    old, new, keep = st / "_bt_oldtest", st / "_bt_newtest", st / "important_dir"
    for d in (old, new, keep):
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.txt").write_text("x" * 100)
    os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))

    r = hv_ops.gc_state(older_than_days=7)
    assert not old.exists(), "超期 _bt_* 没被清"
    assert new.exists(), "近期目录被误删"
    assert keep.exists(), "非 _bt_* 目录被误删"
    assert r["removed"] == 1


def test_gc_dry_run_deletes_nothing(monkeypatch, tmp_path):
    import app.paths as paths
    from app.services import hv_ops
    st = tmp_path / "state"
    monkeypatch.setattr(paths, "state_dir", lambda: st)
    d = st / "_bt_drytest"
    d.mkdir(parents=True, exist_ok=True)
    os.utime(d, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    r = hv_ops.gc_state(older_than_days=7, dry_run=True)
    assert d.exists(), "dry-run 竟然真删了"
    assert r["dry_run"] is True


# --------------------------------------------------------------------------- ③
@pytest.mark.slow
def test_smoke_runs_full_chain(tmp_episodes):
    """端到端冒烟：跑通 script→tts→shots→compose（TTS/生图全打桩，零成本）。

    这条存在的意义：2026-09-09 补丁打穿 reshot() 时全量单测照样绿 ——
    没有冒烟就无法机械发现「链路断了」。
    """
    from app.services.hv_ops import run_smoke
    r = run_smoke()
    assert r.get("ok"), f"冒烟失败：{r}"
    assert r["frames"] >= 1 and r["shots"] >= 1
    assert r["bytes"] > 10000
