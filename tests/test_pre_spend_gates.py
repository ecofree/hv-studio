# -*- coding: utf-8 -*-
"""花钱前的镜长预检 / shots 真实装配 / legacy 预算与工具链预检。

三处都来自 lishimin-03 实测事故（2026-09-28），锁的是「失败必须发生在花钱之前」：
  1. 镜长上限 #3 原本只在 compose 断言，而 compose 在 59 张图付完之后才跑；
     那一集 2 帧旁白真放慢到 8.03s/8.24s（18/29 字，都没超 MAX_FRAG_CHARS），
     结果是花完钱才被拦下。预检必须在 generate_many 之前。
  2. STAGE_FN['shots'] 曾是无操作桩，`--stage shots` 会把 shots 记进
     stages_done 而一镜未装 —— 无人值守下的假绿。
  3. produce_legacy 曾既无预算参数也无 ffmpeg 预检：CLI 的 --max-images/
     --budget-id 在 legacy 分支被静默丢弃，且缺 ffmpeg 要等 compose 跑到
     一半才炸（钱已花）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.production

from app.models import Episode, Frame
from app.services.motion import FPS, frames_for_duration_ms
from app.services.timing_assert import (
    HEAD_MS, MAX_FRAMES, MIN_FRAMES, TAIL_MS, TimingError, _group_of,
    assert_pacing_or_raise, projected_shot_pacing,
)


def _ep(frame_specs) -> Episode:
    """frame_specs: [(id, audio_ms)] —— 同一 id 前缀视为同一「源句子组」。"""
    ep = Episode(id="pace", title="pace", episode_number=1)
    ep.frames = [Frame(id=fid, text="t", narration_text="旁白" * 4,
                       audio_path=None, audio_duration_ms=ms)
                 for fid, ms in frame_specs]
    return ep


# --------------------------------------------------------------------------
# 1) 镜长预检
# --------------------------------------------------------------------------
def test_group_of_has_single_source_in_spec():
    """帧组号必须是单一真源（spec.group_of），compose 与 timing_assert 只能转发。

    此前两处各写一份 `parts[1] if len(parts)>1 else frame_id`。这类手抄副本
    正是 spec.py 开头警告的事故源（改一处忘一处 → 静默时间轴错位），所以
    现在锁的是「都从 spec 取」，而不是「两份实现碰巧相等」。
    """
    from app.services import spec
    from app.services.compose import _group_of as compose_group_of
    for fid in ("frame-000", "frame-014-05", "frame-102-02", "frame-045-02-01", "weird"):
        assert _group_of(fid) == spec.group_of(fid) == compose_group_of(fid), fid
    # 转发而非重写：两个模块里都不该再有 parts[1] 这种解析
    import inspect
    for mod in (compose_group_of, _group_of):
        assert "parts[1]" not in inspect.getsource(mod)


def test_pacing_passes_for_in_bounds_frames():
    ep = _ep([("frame-000", 3000), ("frame-000-01", 3000), ("frame-001", 3000)])
    rep = projected_shot_pacing(ep)
    assert rep["ok"], rep["fails"]
    assert assert_pacing_or_raise(ep)["ok"]


def test_pacing_adds_head_pad_only_at_group_boundary():
    """组内帧无 head；跨组的帧有 HEAD_MS。与 compose 的 head_of 规则一致。"""
    ep = _ep([("frame-000", 3000), ("frame-000-01", 3000), ("frame-001", 3000)])
    rep = projected_shot_pacing(ep)
    assert rep["per_frame"]["frame-000"]["head_ms"] == 0        # 首帧无前帧
    assert rep["per_frame"]["frame-000-01"]["head_ms"] == 0     # 同组
    assert rep["per_frame"]["frame-001"]["head_ms"] == HEAD_MS  # 跨组


def test_pacing_adds_tail_pad_only_at_group_end():
    ep = _ep([("frame-000", 3000), ("frame-000-01", 3000), ("frame-001", 3000)])
    rep = projected_shot_pacing(ep)
    assert rep["per_frame"]["frame-000-01"]["tail_ms"] == TAIL_MS  # 组尾
    assert rep["per_frame"]["frame-001"]["tail_ms"] == TAIL_MS     # 末帧


def test_overlong_frame_is_rejected_before_spend():
    """事故复现：18 字旁白真放慢到 8.03s，字数没超上限但镜长超了。"""
    ep = _ep([("frame-000", 8025), ("frame-001", 3000)])
    rep = projected_shot_pacing(ep)
    assert not rep["ok"]
    assert len(rep["over"]) == 1
    assert rep["over"][0]["frames"] > MAX_FRAMES
    with pytest.raises(TimingError, match="生图之前"):
        assert_pacing_or_raise(ep)


def test_pacing_error_message_is_actionable():
    """报错要能指导处置：指出帧 id、实测时长、超出帧数、字数，并禁止改规格绕过。"""
    ep = _ep([("frame-000", 8025)])
    with pytest.raises(TimingError) as ei:
        assert_pacing_or_raise(ep)
    text = str(ei.value)
    for token in ("frame-000", "8025ms", "200", "字", "生图之前"):
        assert token in text, token
    # 上限是成片规格，必须明确禁止绕过（否则下一个人会去改常量）
    assert "不要靠改 MAX_FRAMES 绕过" in text


def test_too_short_frame_is_rejected():
    ep = _ep([("frame-000", 900), ("frame-001", 3000)])
    rep = projected_shot_pacing(ep)
    assert not rep["ok"] and len(rep["under"]) == 1


def test_zero_duration_frames_are_reported_as_not_ready():
    """audio 全 0 时不能静默过检——那等于「音频没生成就允许花钱」。"""
    ep = _ep([("frame-000", 0), ("frame-001", 0)])
    rep = projected_shot_pacing(ep)
    assert not rep["ok"]
    assert any("audio_duration_ms=0" in f for f in rep["fails"]), rep["fails"]


def test_pacing_projection_matches_shot_frames_for_in_bounds():
    """投影口径必须与 compose 算出的镜长一致，否则要么漏判要么误伤。"""
    ep = _ep([("frame-000", 3000), ("frame-000-01", 2000), ("frame-001", 5000)])
    rep = projected_shot_pacing(ep)
    for f in ep.frames:
        row = rep["per_frame"][f.id]
        assert row["frames"] == frames_for_duration_ms(row["shot_ms"], FPS)


# --------------------------------------------------------------------------
# 2) shots 阶段：真实装配，而非无操作桩
# --------------------------------------------------------------------------
def test_stages_done_is_not_claimed_by_a_noop_shot_stage():
    """回归锁：STAGE_FN['shots'] 必须是真装配函数，不能是打日志的空桩。"""
    import inspect

    import app.orchestrator as o
    assert o.STAGE_FN["shots"] is o.stage_shots
    src = inspect.getsource(o.stage_shots)
    assert "build_episode_shots" in src, "shots 阶段必须真的调用 shot_builder"


def test_stage_shots_without_scene_plan_raises():
    """显式 --stage shots 而无 scene_plan 时必须报错，不能静默成功。"""
    import app.orchestrator as o
    ep = _ep([("frame-000", 3000)])
    with pytest.raises(RuntimeError, match="scene_plan"):
        o.stage_shots(ep, plan_path=None)


def test_can_assemble_shots_is_false_without_storyboard(tmp_episodes):
    """条件不足时不得进 runnable，否则又会在 stages_done 留下假记录。"""
    import app.orchestrator as o
    from app.managers import save
    ep = _ep([("frame-000", 3000)])
    ep.id = "no-story"
    save(ep)
    assert o.can_assemble_shots("no-story") is False


# --------------------------------------------------------------------------
# 3) legacy 路径：预算必填 + 工具链预检
# --------------------------------------------------------------------------
def test_legacy_produce_refuses_images_without_budget(tmp_episodes, monkeypatch):
    """有生图任务却没给预算时，必须在花钱前拒绝并说清怎么修。"""
    import app.orchestrator as o
    from app.managers import save
    ep = Episode(id="no-budget", title="t", episode_number=1)
    save(ep)
    d = tmp_episodes / ep.id
    d.mkdir(parents=True, exist_ok=True)
    # 空集需有正文才能起步（schema 的 allow_empty 判据），与真实新集建档一致
    (d / "script.md").write_text("> 作品类型：测试\n\n正文一段。\n", encoding="utf-8")
    (d / "image_prompts.json").write_text('{"X1": {"en": "a test plate"}}',
                                           encoding="utf-8")
    monkeypatch.setattr(o, "_check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(o, "_check_toolchain", lambda **k: None)
    monkeypatch.setattr(o, "build_image_jobs", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="未给预算"):
        o.produce_legacy(ep.id, stages=["assets"])


def test_legacy_produce_threads_budget_into_image_stage(tmp_episodes, monkeypatch):
    """--max-images/--budget-id 必须在 legacy 路径真的传到 stage_images。"""
    import app.orchestrator as o
    from app.managers import save
    ep = Episode(id="with-budget", title="t", episode_number=1)
    save(ep)
    d = tmp_episodes / ep.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "script.md").write_text("> 作品类型：测试\n\n正文一段。\n", encoding="utf-8")
    (d / "image_prompts.json").write_text('{"X1": {"en": "a test plate"}}',
                                           encoding="utf-8")
    monkeypatch.setattr(o, "_check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(o, "_check_toolchain", lambda **k: None)
    monkeypatch.setattr(o, "build_image_jobs", lambda *a, **k: [])
    seen = {}

    # 替身签名必须跟着 stage_images 走：新增 only 形参后这里也要接受，
    # 否则真实调用多传 only= 会 TypeError（2026-09-29 P3 加 only 时踩到）。
    def fake_images(ep, budget=None, only=None):
        seen["budget"] = budget
        seen["limits"] = None if budget is None else budget.limits
        seen["only"] = only
    monkeypatch.setattr(o, "stage_images", fake_images)
    # produce_legacy 内部按 STAGE_FN 覆盖 images，故直接验证闭包产物
    o.produce_legacy(ep.id, stages=["images"], max_images=7, budget_id="b1")
    assert seen["budget"] is not None
    assert seen["limits"]["image_requests"] == 7


def test_legacy_produce_does_not_demand_budget_for_non_image_stages(
        tmp_episodes, monkeypatch):
    """回归锁：预算只在真要生图时要求。否则 `--stage shots` 这类排障命令
    会被「未给预算」无谓挡下（ep3 有 image_prompts.json，但不生图）。"""
    import app.orchestrator as o
    from app.managers import save
    ep = Episode(id="shots-only", title="t", episode_number=1)
    ep.frames = [Frame(id="frame-000", text="t", narration_text="旁白",
                       audio_path=None, audio_duration_ms=3000)]
    save(ep)
    d = tmp_episodes / ep.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "image_prompts.json").write_text('{"X1": {"en": "a test plate"}}',
                                           encoding="utf-8")
    monkeypatch.setattr(o, "_check_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(o, "_check_toolchain", lambda **k: None)
    monkeypatch.setattr(o, "build_image_jobs", lambda *a, **k: [])
    called = []
    # 注意：produce_legacy 内部是 `stage_fn = dict(STAGE_FN)` 快照，
    # 所以要 patch 字典项，patch 模块属性对派发无效（这本身也是该留意的细节）。
    monkeypatch.setitem(o.STAGE_FN, "shots", lambda e, **k: called.append(1))
    out = o.produce_legacy(ep.id, stages=["shots"])   # 不给预算
    assert "shots" in out["stages_done"], out
    assert called, "shots 阶段应真的被调用"


def test_cli_legacy_branch_keeps_budget_flags():
    """回归锁：cli.py 的 legacy-preview 分支曾整份替换 options，
    把 --max-images/--budget-id 静默丢弃（同坑 #38：--merge-short）。"""
    from pathlib import Path
    src = Path("app/cli.py").read_text(encoding="utf-8")
    i = src.index('args.pipeline == "legacy-preview"')
    branch = src[i:i + 700]
    assert "max_images=args.max_images" in branch
    assert "budget_id=args.budget_id" in branch


def test_toolchain_preflight_reports_missing_ffmpeg(monkeypatch):
    """缺 ffmpeg 必须在开工前报，而不是 compose 跑到一半才炸。"""
    import shutil as _shutil

    import app.orchestrator as o
    monkeypatch.setattr(_shutil, "which", lambda x: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        o._check_toolchain()
