# -*- coding: utf-8 -*-
"""P0-3 / P0-4 验收：状态必须由真实产物驱动；schema 损坏必须硬阻断。

P0-3：API / run_stage 不得空推状态到 DONE（无产物）。
P0-4：schema 不干净（如 shot 引用缺失 frame）→ 生产前拒绝，绝不付费生图。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


def test_run_stage_requires_real_stage_fn():
    """run_stage 不带 stage_fn 必须拒绝推进（否则是虚假完成）。"""
    from app.models import Episode
    from app.managers import run_stage
    ep = Episode(id="tmpfake", title="tmpfake", episode_number=0)
    with pytest.raises(Exception):
        run_stage(ep)  # 无真实执行 → 应 raise，不得空推


def test_validate_schema_detects_dangling_shot_ref():
    """构造 shot.frame_id 不在 frames 的 episode → schema 必须报出。"""
    from app.models import Episode, Shot, Frame
    ep = Episode(id="bad", title="bad", episode_number=0)
    ep.frames = [Frame(id="frame-000", text="a")]
    ep.shots = {
        "shot-000": Shot(
            id="shot-000", frame_id="frame-999", index=0,  # 引用了不存在的 frame
            duration_ms=1000, audio_start_ms=0, audio_end_ms=1000,
            prompt="p", image_path="x.png",
        ),
    }
    from app.managers.episode import validate_schema
    problems = validate_schema(ep)
    assert any("shot-000" in pr and "frame-999" in pr for pr in problems), problems


def test_produce_verify_derives_true_status(smoke_episode):
    """verify 只读，应在真实产物上导出 COMPOSED（而非盲迁移）。"""
    from app.orchestrator import produce_legacy as produce
    from app.services.compose import compose_episode
    ep_id=smoke_episode[0]
    compose_episode(ep_id)
    res = produce(ep_id, stages=["verify"])
    assert res["status"] == "COMPOSED", res
    assert "verify" in res["stages_done"]
    assert res.get("final_mp4"), "verify 应返回 final_mp4 路径"


def test_empty_episode_with_script_can_start_produce(tmp_episodes):
    """空集 + 有 script.md → schema 放行，stage_script 能成为第一步。"""
    import json
    import os
    from app.managers import load, save
    from app.models import Episode
    from app.paths import episodes_dir

    ep_dir = episodes_dir()          # P1-6：跟随 tmp_episodes 重定向
    ep_id = f"emptytest-{os.getpid()}"
    ep = Episode(id=ep_id, title="empty", episode_number=1, status="draft")
    save(ep)
    # 写一份超短 script
    (ep_dir / ep_id).mkdir(parents=True, exist_ok=True)
    (ep_dir / ep_id / "script.md").write_text(
        "这是一个测试。用来验证空集能启动。", encoding="utf-8")

    from app.orchestrator import produce_legacy as produce
    res = produce(ep_id, stages=["script"])
    assert res["status"] in ("SCRIPTED", "DRAFT"), res
    assert "script" in res["stages_done"]
    assert load(ep_id).frames, "script 应已切出 frames"
    # 无清理：一切落在 tmp 下


def test_produce_default_skips_script_when_frames_exist(tmp_episodes, monkeypatch):
    """已有 frames 的集，默认全链不重切脚本（防 splitter 规则一变重分帧）。"""
    import json
    import os
    from pathlib import Path
    from app.managers import load, save
    from app.models import Episode, Frame
    from app.paths import episode_dir

    ep_id = f"nors-{os.getpid()}"
    ep = Episode(id=ep_id, title="nors", episode_number=1, status="draft")
    # 造一个已有 frames 的集（模拟切过脚本）
    ep.frames = [Frame(id="frame-000", text="已有帧", audio_duration_ms=1000)]
    save(ep)
    # 有 script.md 但不该被重切
    _ep_dir = episode_dir(ep_id)     # P1-6：跟随 tmp_episodes 重定向
    _ep_dir.mkdir(parents=True, exist_ok=True)
    (_ep_dir / "script.md").write_text(
        "全新内容。不应被默认跑进 script 阶段。", encoding="utf-8")
    from app.orchestrator import produce_legacy as produce
    # 默认链（None）会先评估 script 适用性；无 assets/shots/compose 活 → 应仅跳过
    monkeypatch.setattr("app.orchestrator.stage_assets",lambda *a,**k:None)
    res = produce(ep_id)   # stages=None
    assert "script" not in res["stages_done"], \
        f"已有 frames 不应默认重切 script: {res['stages_done']}"
    # frames 仍保持原样（未被 rebuild_frames 改写）
    assert load(ep_id).frames[0].text == "已有帧"


    # 无清理：一切落在 tmp 下


def test_audit_orphan_wavs_safe_criterion(tmp_episodes):
    """孤儿 wav 判据：被 frames 或 manifest 引用的保留，孤儿才删，apply 才删。

    回归守门：audit-wav 命令的判据是「frames ∪ manifest 都未引用才删」，非按文件名
    （曾误加 frame-NN-NN 子帧 regex 导致漏检 _orig.wav / 非子帧孤儿）。
    """
    import os
    import json as _json
    from pathlib import Path
    from app.managers import load, save
    from app.models import Episode, Frame
    from app.paths import episode_dir

    ep_id = f"wavaud-{os.getpid()}"
    ep = Episode(id=ep_id, title="wavaud", episode_number=1, status="draft")
    save(ep)
    audio = episode_dir(ep_id) / "audio"   # P1-6：跟随 tmp_episodes 重定向
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "frame-000.wav").write_bytes(b"\x00" * 100)   # 将被 frames 引用
    (audio / "frame-999-01.wav").write_bytes(b"\x00" * 100)  # 旧子帧格式孤儿
    (audio / "old_base.wav").write_bytes(b"\x00" * 100)    # 孤儿但非子帧格式
    (audio / "man-kept.wav").write_bytes(b"\x00" * 100)    # 被 manifest 引用
    (audio / "_tts_manifest.json").write_text(
        _json.dumps({"man-kept": {"fp": "x", "wav": "man-kept.wav"}}), encoding="utf-8")

    from app.managers.episode import audit_orphan_wavs
    ep2 = load(ep_id)
    ep2.frames = [Frame(id="frame-000", text="x",
                        audio_path=str(audio / "frame-000.wav").replace("\\", "/"))]
    save(ep2)

    # dry-run：只列不删
    r = audit_orphan_wavs(ep_id, apply=False)
    names = set(r["orphans"])
    assert "frame-000.wav" not in names, "被 frames 引用的不得判孤儿"
    assert "man-kept.wav" not in names, "被 manifest 引用的不得判孤儿"
    assert "frame-999-01.wav" in names
    assert "old_base.wav" in names, "非子帧孤儿也应被认作孤儿(判据非文件名)"
    assert all((audio / w).exists() for w in names), "dry-run 不得删"

    # apply：只删孤儿，保留被引用的
    r2 = audit_orphan_wavs(ep_id, apply=True)
    assert r2["deleted"] == 2, r2
    assert (audio / "frame-000.wav").exists()
    assert (audio / "man-kept.wav").exists()
    assert not (audio / "frame-999-01.wav").exists()
    assert not (audio / "old_base.wav").exists()
    # 无清理：一切落在 tmp 下
