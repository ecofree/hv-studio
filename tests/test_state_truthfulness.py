# -*- coding: utf-8 -*-
"""P0-3 / P0-4 验收：状态必须由真实产物驱动；schema 损坏必须硬阻断。

P0-3：API / run_stage 不得空推状态到 DONE（无产物）。
P0-4：schema 不干净（如 shot 引用缺失 frame）→ 生产前拒绝，绝不付费生图。
"""
from __future__ import annotations

import pytest


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


@pytest.fixture
def _restore_delivered_episode():
    """produce 收尾会 save(ep) 落盘，而 hanwu-prologue 是**已交付**小集。

    让测试直接改写交付数据是脏的：模型字段一变（如 D2 删 Shot 死字段），
    跑一次测试就把交付集的 json 悄悄改掉，且 diff 看起来"像是数据自己变的"。
    跑之前快照、跑之后还原，测试保持只读。
    """
    from app.config.config import ROOT
    p = ROOT / "episodes" / "hanwu-prologue.json"
    backup = p.read_bytes() if p.exists() else None
    try:
        yield
    finally:
        if backup is not None:
            p.write_bytes(backup)


def test_produce_verify_derives_true_status(_restore_delivered_episode):
    """verify 只读，应在真实产物上导出 COMPOSED（而非盲迁移）。"""
    from app.orchestrator import produce
    # hanwu-prologue 是已交付小集（有 mp4），verify-only 不重编码
    res = produce("hanwu-prologue", stages=["verify"])
    assert res["status"] == "COMPOSED", res
    assert "verify" in res["stages_done"]
    assert res.get("final_mp4"), "verify 应返回 final_mp4 路径"


def test_empty_episode_with_script_can_start_produce():
    """空集 + 有 script.md → schema 放行，stage_script 能成为第一步。"""
    import json
    import os
    from pathlib import Path
    from app.config.config import ROOT
    from app.managers import load, save
    from app.models import Episode

    ep_dir = ROOT / "episodes"
    ep_id = f"emptytest-{os.getpid()}"
    ep = Episode(id=ep_id, title="empty", episode_number=1, status="draft")
    save(ep)
    # 写一份超短 script
    (ep_dir / ep_id).mkdir(parents=True, exist_ok=True)
    (ep_dir / ep_id / "script.md").write_text(
        "这是一个测试。用来验证空集能启动。", encoding="utf-8")

    try:
        from app.orchestrator import produce
        res = produce(ep_id, stages=["script"])
        assert res["status"] in ("SCRIPTED", "DRAFT"), res
        assert "script" in res["stages_done"]
        assert load(ep_id).frames, "script 应已切出 frames"
    finally:
        try:
            (ep_dir / f"{ep_id}.json").unlink()
        except Exception:
            pass
        import shutil
        shutil.rmtree(ep_dir / ep_id, ignore_errors=True)


def test_produce_default_skips_script_when_frames_exist():
    """已有 frames 的集，默认全链不重切脚本（防 splitter 规则一变重分帧）。"""
    import json
    import os
    from pathlib import Path
    from app.config.config import ROOT
    from app.managers import load, save
    from app.models import Episode, Frame

    ep_id = f"nors-{os.getpid()}"
    ep = Episode(id=ep_id, title="nors", episode_number=1, status="draft")
    # 造一个已有 frames 的集（模拟切过脚本）
    ep.frames = [Frame(id="frame-000", text="已有帧", audio_duration_ms=1000)]
    save(ep)
    # 有 script.md 但不该被重切
    (ROOT / "episodes" / ep_id).mkdir(parents=True, exist_ok=True)
    (ROOT / "episodes" / ep_id / "script.md").write_text(
        "全新内容。不应被默认跑进 script 阶段。", encoding="utf-8")
    try:
        from app.orchestrator import produce
        # 默认链（None）会先评估 script 适用性；无 assets/shots/compose 活 → 应仅跳过
        res = produce(ep_id)   # stages=None
        assert "script" not in res["stages_done"], \
            f"已有 frames 不应默认重切 script: {res['stages_done']}"
        # frames 仍保持原样（未被 rebuild_frames 改写）
        assert load(ep_id).frames[0].text == "已有帧"
    finally:
        try:
            (ROOT / "episodes" / f"{ep_id}.json").unlink()
        except Exception:
            pass
        import shutil
        shutil.rmtree(ROOT / "episodes" / ep_id, ignore_errors=True)


def test_stage_images_uses_flat_imageconfig(monkeypatch):
    """stage_images 读扁平 ImageConfig(engine/size/quality 顶层) 而非不存在 primary.

    回归守门：batch_generate_lgt 用 im.engine/im.size/im.quality；stage_images 曾误用
    im.primary.* 会 AttributeError（真 bug，已修）。mock generate_many 断言入参正确。
    """
    import os
    import json as _json
    from pathlib import Path
    from app.config.config import ROOT
    from app.managers import load, save
    from app.models import Episode

    ep_id = f"imgcfg-{os.getpid()}"
    ep = Episode(id=ep_id, title="imgcfg", episode_number=1, status="draft")
    save(ep)
    ep_dir = ROOT / "episodes" / ep_id
    (ep_dir / "shots").mkdir(parents=True, exist_ok=True)
    # 用真实长度的 prompt（实测 laogongtuan/lsm-15min 最短 141、中位 ~190）。
    # 用 "desert" 这种 6 字玩具串会被 prompt 预筛以「过短」拦下——那正是
    # 预筛的本意（漏写/占位符不该发请求烧配额），不是本测试要覆盖的行为。
    (ep_dir / "image_prompts.json").write_text(
        _json.dumps({
            "LGT01": {"en": "Wide shot, Han dynasty emissary on the Silk Road "
                            "at dusk, cinematic lighting, realistic"},
            "LGT02": {"en": "Medium shot, endless desert dunes under a bleak "
                            "sky, cold色调, film grain, realistic"},
        }, ensure_ascii=False), encoding="utf-8")

    captured = {}

    def _fake_generate_many(jobs, **kw):
        captured["n"] = len(jobs)
        captured["reqs"] = [(j.id, j.req) for j in jobs]
        # 模拟全部成功（不落真文件，仅路径）
        from pathlib import Path as _P
        return {j.id: str(_P("shots") / f"{j.id}.png") for j in jobs}

    try:
        from app import orchestrator
        # stage_images 在函数体内 `from app.services.image import ... generate_many`，
        # 故须 patch 源模块而非 orchestrator 属性。
        from app.services import image as image_mod
        monkeypatch.setattr(image_mod, "generate_many", _fake_generate_many)
        from app.managers import load as _load
        orchestrator.stage_images(_load(ep_id))
        assert captured["n"] == 2
        # 每个 req 用扁平 config 落参，无 primary 崩溃，且能取出 size/quality/model
        for jid, req in captured["reqs"]:
            assert req.size  # 扁平 size（曾是 im.primary.size → AttributeError）
            assert req.quality
            assert req.model
            assert "LGT" in jid
    finally:
        try:
            (ROOT / "episodes" / f"{ep_id}.json").unlink()
        except Exception:
            pass
        import shutil
        shutil.rmtree(ep_dir, ignore_errors=True)


def test_audit_orphan_wavs_safe_criterion():
    """孤儿 wav 判据：被 frames 或 manifest 引用的保留，孤儿才删，apply 才删。

    回归守门：audit-wav 命令的判据是「frames ∪ manifest 都未引用才删」，非按文件名
    （曾误加 frame-NN-NN 子帧 regex 导致漏检 _orig.wav / 非子帧孤儿）。
    """
    import os
    import json as _json
    from pathlib import Path
    from app.config.config import ROOT
    from app.managers import load, save
    from app.models import Episode, Frame

    ep_id = f"wavaud-{os.getpid()}"
    ep = Episode(id=ep_id, title="wavaud", episode_number=1, status="draft")
    save(ep)
    audio = ROOT / "episodes" / ep_id / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "frame-000.wav").write_bytes(b"\x00" * 100)   # 将被 frames 引用
    (audio / "frame-999-01.wav").write_bytes(b"\x00" * 100)  # 旧子帧格式孤儿
    (audio / "old_base.wav").write_bytes(b"\x00" * 100)    # 孤儿但非子帧格式
    (audio / "man-kept.wav").write_bytes(b"\x00" * 100)    # 被 manifest 引用
    (audio / "_tts_manifest.json").write_text(
        _json.dumps({"man-kept": {"fp": "x", "wav": "man-kept.wav"}}), encoding="utf-8")

    from app.managers.episode import audit_orphan_wavs
    try:
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
    finally:
        try:
            (ROOT / "episodes" / f"{ep_id}.json").unlink()
        except Exception:
            pass
        import shutil
        shutil.rmtree(ROOT / "episodes" / ep_id, ignore_errors=True)
