# -*- coding: utf-8 -*-
"""K 批（2026-09-05）验收：P1-6 路径隔离 + P1-8 build/ 中间产物清理。

两条主线：
  A. episodes 根必须能被重定向（app.paths），测试不再写真实 episodes/
  B. hv clean 必须白名单清理、绝不碰图片资产与成片、默认 dry-run
"""
from __future__ import annotations

import pytest
from pathlib import Path

from app.services import cleanup
from app.services.cleanup import clean_episodes, scan_episode_build


# ---------------------------------------------------------------------------
# A. P1-6：episodes 根单一真源 + 可重定向
# ---------------------------------------------------------------------------
def test_episodes_root_is_call_time_evaluated(monkeypatch, tmp_path):
    """episodes 根必须是**调用期**求值，否则测试无法重定向。

    此前 4 处各自 `ROOT/"episodes"` 在 import 期固化 → 测试只能真写
    D:/hv/episodes/ → 只能靠「按前缀通配删」兜底 → 有误删交付集风险。
    """
    import app.paths as P
    monkeypatch.setattr(P, "episodes_dir", lambda: tmp_path / "eps")

    from app.managers.episode import episode_path
    from app import orchestrator as orch
    from app.services import shot_builder as sb

    assert episode_path("x") == tmp_path / "eps" / "x.json"
    assert orch._episode_dir("x") == tmp_path / "eps" / "x"
    assert orch._out_dir("x") == tmp_path / "eps" / "x" / "out"
    assert sb._default_plan_path("x") == tmp_path / "eps" / "x" / "scene_plan.json"


def test_no_module_hardcodes_episodes_root():
    """app/ 下不得再有 import 期固化的 episodes 根常量（EPISODES_DIR/BASE/_EPISODES）。"""
    import re
    import app.paths
    from app.config.config import ROOT

    bad = []
    for f in (ROOT / "app").rglob("*.py"):
        if f.name == "paths.py":
            continue
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(EPISODES_DIR|BASE|_EPISODES)\s*=", src,
                             re.MULTILINE):
            bad.append(f"{f.relative_to(ROOT)}:{m.group(1)}")
    assert bad == [], f"app/ 仍有 import 期 episodes 根常量: {bad}"


def test_tests_do_not_write_real_episodes():
    """tests/ 里不得再出现拼真实 episodes/ 的**代码**（注释与文档字符串不算）。

    用 AST 而不是正则：这个项目到处在注释里写「不要再拼 ROOT/'episodes'」，
    正则会把这些劝阻性注释本身当成违规（2026-09-05 实测误报 3 处）。
    唯一真正允许的例外：test_state_truthfulness 对已交付集 hanwu-prologue
    的只读校验——那是有意为之，它要验证「verify 不改交付数据」。
    """
    import ast
    from app.config.config import ROOT

    bad = []
    for f in (ROOT / "tests").rglob("*.py"):
        if f.name in ("conftest.py", "test_state_truthfulness.py"):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            l, r = node.left, node.right
            if (isinstance(l, ast.Name) and l.id == "ROOT"
                    and isinstance(r, ast.Constant) and r.value == "episodes"):
                bad.append(f"{f.name}:{node.lineno}")
    assert bad == [], f"tests/ 仍在拼真实 episodes 路径: {bad}"


def test_delivery_episode_is_never_swept(tmp_path, monkeypatch):
    """会话末兜底清扫必须对交付集免疫。

    关键：**不能只靠 git ls-files** —— laogongtuan / lsm-15min 是最新的两部
    成片却都未入库。所以判据是「名单 ∪ 目录下有 out/*.mp4」。
    """
    from conftest import is_delivery_episode, PROTECTED_EPISODES
    import conftest

    fake_root = tmp_path / "episodes"
    fake_root.mkdir()
    monkeypatch.setattr(conftest, "EPISODES", fake_root)

    # 1) 名单内的集：光有 json 也算交付集
    (fake_root / "laogongtuan.json").write_text("{}", encoding="utf-8")
    assert is_delivery_episode("laogongtuan"), "未入库的交付集必须受保护"

    # 2) 名单外、但有成片：自动受保护（对未来新增集自动生效）
    d = fake_root / "brand-new-film"
    (d / "out").mkdir(parents=True)
    (d / "out" / "brand-new-film_m1.mp4").write_bytes(b"\x00" * 10)
    assert is_delivery_episode("brand-new-film"), "有成片即视为交付集"

    # 3) 纯测试集（无成片）不受保护，才能被清扫
    (fake_root / "smoke-999.json").write_text("{}", encoding="utf-8")
    (fake_root / "smoke-999").mkdir()
    assert not is_delivery_episode("smoke-999"), "测试集不该被误判成交付集"

    assert {"hw-001", "hw-002", "lsm-15min"} <= set(PROTECTED_EPISODES)


# ---------------------------------------------------------------------------
# B. P1-8：hv clean
# ---------------------------------------------------------------------------
def _fake_ep(root: Path, ep_id: str = "cleanme") -> Path:
    """造一个带完整中间产物的假集（含图片资产与成片，用来验证绝不误删）。"""
    d = root / ep_id
    (d / "build").mkdir(parents=True)
    (d / "build" / "segments").mkdir()
    (d / "out").mkdir()
    (d / "shots").mkdir()
    (d / "audio").mkdir()

    (d / "build" / "narration.wav").write_bytes(b"n" * 4096)
    (d / "build" / "voice_bgm_mix.wav").write_bytes(b"m" * 2048)
    (d / "build" / "subs.ass").write_text("x", encoding="utf-8")
    (d / "build" / "concat.txt").write_text("x", encoding="utf-8")
    (d / "build" / "video.mp4").write_bytes(b"v" * 1024)
    (d / "build" / "segments" / "seg-000.mp4").write_bytes(b"s" * 8192)
    (d / "build" / "segments" / "seg-001.mp4").write_bytes(b"s" * 8192)
    (d / "build" / "segments" / "params.json").write_text("{}", encoding="utf-8")
    (d / "build" / "check").mkdir()
    (d / "build" / "check" / "f_1.0s.png").write_bytes(b"P" * 512)  # 图片资产！

    # 绝不可碰的东西
    (d / "out" / f"{ep_id}_m1.mp4").write_bytes(b"F" * 4096)          # 成片
    (d / "shots" / "SC01.png").write_bytes(b"P" * 512)                # 底图
    (d / "audio" / "frame-000.wav").write_bytes(b"a" * 512)           # 真实音频
    return d


def test_scan_lists_intermediates_but_never_images(tmp_path):
    d = _fake_ep(tmp_path)
    skipped: list[dict] = []
    items = scan_episode_build(d, include_segments=True, _skipped=skipped)

    names = {Path(i["path"]).name for i in items}
    assert "narration.wav" in names and "voice_bgm_mix.wav" in names
    assert "subs.ass" in names and "concat.txt" in names and "video.mp4" in names
    assert "seg-000.mp4" in names and "seg-001.mp4" in names

    # 图片资产：既不在待清列表，也要被**记账**（静默跳过=白名单失效还看不出来）
    assert "f_1.0s.png" not in names
    assert any("f_1.0s.png" in s["path"] for s in skipped), \
        "被硬闸拦下的图片必须记账，否则等于静默忽略"


def test_segments_are_opt_in(tmp_path):
    """段默认不清——它是一等公民缓存，删了下次要全量重编。"""
    d = _fake_ep(tmp_path)
    assert not [i for i in scan_episode_build(d) if i["tier"] == "segments"]
    assert [i for i in scan_episode_build(d, include_segments=True)
            if i["tier"] == "segments"]


def test_dry_run_deletes_nothing(tmp_path):
    d = _fake_ep(tmp_path)
    before = {p: p.stat().st_size for p in d.rglob("*") if p.is_file()}

    res = clean_episodes({"cleanme": d}, include_segments=True, apply=False)
    assert res["deleted"] == 0 and res["deleted_bytes"] == 0
    assert res["total_bytes"] > 0, "dry-run 也要报出能清出多少"

    after = {p: p.stat().st_size for p in d.rglob("*") if p.is_file()}
    assert before == after, "dry-run 不得改动任何文件"


def test_apply_deletes_only_whitelisted_and_spares_assets(tmp_path):
    """--apply 之后：白名单项消失，成片/底图/真实音频一个不少。"""
    d = _fake_ep(tmp_path)

    res = clean_episodes({"cleanme": d}, include_segments=True, apply=True)
    assert res["deleted"] >= 6

    # 该删的没了
    assert not (d / "build" / "narration.wav").exists()
    assert not (d / "build" / "voice_bgm_mix.wav").exists()
    assert not (d / "build" / "subs.ass").exists()
    assert not (d / "build" / "segments" / "seg-000.mp4").exists()

    # 绝不能没的还在
    assert (d / "out" / "cleanme_m1.mp4").exists(), "成片被删了！"
    assert (d / "shots" / "SC01.png").exists(), "底图被删了！"
    assert (d / "audio" / "frame-000.wav").exists(), "真实音频被删了！"
    assert (d / "build" / "check" / "f_1.0s.png").exists(), "图片资产被删了！"


def test_out_and_shots_are_outside_scan_scope(tmp_path):
    """out/ 与 shots/ 根本不在扫描范围内——结构性隔离，不是靠条件判断。"""
    d = _fake_ep(tmp_path)
    items = scan_episode_build(d, include_segments=True)
    paths = [str(i["path"]) for i in items]
    assert not any("/out/" in p.replace("\\", "/") for p in paths)
    assert not any("/shots/" in p.replace("\\", "/") for p in paths)


def test_whitelist_has_no_stale_entries():
    """白名单里若出现图片扩展名，硬闸会把它静默吞掉——直接查名单本身。"""
    img = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    for name in cleanup.BUILD_FILES + cleanup.SEGMENT_STATE_FILES:
        assert Path(name).suffix.lower() not in img, \
            f"白名单里出现了图片文件 {name}：会被硬闸静默拦下却没人知道"
