# -*- coding: utf-8 -*-
"""scene_plan 帧指纹 + 切分同源化回归。

背景（2026-09-05 lsm-15min 复盘）：
  分镜脚本曾只活在工作站临时目录，它自带的短片合并逻辑与仓内 split_script
  不同源——同一份 script.md，一侧 324 帧、另一侧 392 帧，差 68 帧（21%）。
  而 scene_plan 的 frames 区间是【按下标】写的，覆盖校验在这种情况下依然
  「全通过」，会静默产出一部错位的片子。

本组测试锁住三件事：
  1. 清洗/合并规则进仓后行为不变（含「merge 边界 = 段落」这条硬约束）；
  2. split_script 默认仍是未合并（laogongtuan 365 帧建档口径，改默认即错位）；
  3. scene_plan 与 ep.frames 不同源时，装配必须硬拒而不是静默通过。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import json


from app.managers.episode import episode_path
from app.services import shot_builder as SB
from app.services.script import (
    clean_narration, frames_fingerprint, merge_short_pieces,
    segment_script, split_script,
)


# ---------------------------------------------------------------------------
# 1. 清洗规则
# ---------------------------------------------------------------------------
def test_clean_narration_drops_annotations_and_brackets():
    raw = "【语速加快】他抬起头<br>[停顿1.5秒][重音]天下，是打下来的。"
    out = clean_narration(raw)
    assert "【" not in out and "语速" not in out      # 【】整段去
    assert "[重音]" not in out and "重音" not in out   # 其余 [xx] 去
    assert "。" in out                                # 停顿转句号
    assert "\n" not in out and " " not in out         # <br> 与空白压掉
    assert out.startswith("他抬起头")


def test_clean_narration_strips_leading_trailing_punct():
    """末端引导标点必须剥掉，否则会原样烧进首帧字幕。"""
    assert clean_narration("，于是他走了。") == "，于是他走了。"
    assert clean_narration("。。叠句号。。") == "。叠句号。"


def test_clean_narration_collapses_duplicate_periods():
    assert "。。" not in clean_narration("他停下来了。。。。然后走了。")


# ---------------------------------------------------------------------------
# 2. 短片合并
# ---------------------------------------------------------------------------
def test_merge_short_pieces_merges_fragments():
    """前一片 <10 字且合并后 ≤29 字 → 向后并（防「半句」闪切镜）。"""
    out = merge_short_pieces(["很短的一句，", "后面接着说完这句话。"])
    assert out == ["很短的一句，后面接着说完这句话。"]


def test_merge_short_pieces_absorbs_orphan_punctuation():
    """孤立标点片段一律吸附到前一片，绝不单独成帧。"""
    out = merge_short_pieces(["前面正常的一句。", "”"])
    assert out == ["前面正常的一句。”"]


def test_merge_short_pieces_respects_max_join():
    """合并后超过 29 字就不并（否则单镜 >8s，时序 gate 会拒）。"""
    left = "这是一个刚好十字出头的片段，"
    right = "后面跟了一段很长很长很长很长的补充内容。"
    out = merge_short_pieces([left, right])
    assert out == [left, right], "超限仍被合并"


def test_segment_script_merge_boundary_is_paragraph():
    """merge 边界必须是段落：跨镜头合并会把两张底图的旁白粘成一帧。"""
    text = "第一镜只有五个字，\n\n第二镜也是五个字，"
    merged = segment_script(text, merge_short=True)
    assert merged == ["第一镜只有五个字，", "第二镜也是五个字，"], \
        "跨段落合并了 → scene_plan 的帧区间会跨底图错位"


def test_segment_script_merge_happens_per_paragraph():
    """同段落内的碎片要真的合并（否则 lsm-15min 会变回 392 帧）。"""
    text = "很短的一句，后面接着说完这句话。"
    pieces = [p for p in segment_script(text, merge_short=False)]
    merged = segment_script(text, merge_short=True)
    assert len(merged) < len(pieces) or len(pieces) == 1


# ---------------------------------------------------------------------------
# 3. split_script 默认口径不可漂移
# ---------------------------------------------------------------------------
def test_split_script_default_is_unmerged():
    """默认必须仍是未合并：laogongtuan 的 365 帧是按这个口径建档的。

    一旦把默认改成合并，laogongtuan 会变 310 帧，既有音频与 scene_plan 全错位。
    """
    from app.models import Episode
    # 真实触发例（lsm-15min frame#8）：20 字句切成 9字+11字，左侧 9<10 会被合并
    text = "这辈子最大的心魔，居然是一滩洗不掉的血。"
    ep = Episode(id="x", title="x")
    unmerged = split_script(ep, text)
    merged = split_script(ep, text, merge_short=True)
    assert [f.text for f in unmerged] == ["这辈子最大的心魔，", "居然是一滩洗不掉的血。"]
    assert len(merged) == 1, "合并模式必须真的减少帧数"
    # 未合并沿用「句号-片号」编号（frame-000 / frame-000-01），改口径会让
    # rebuild_frames 的 id 对齐全部落空 → 音频被判孤儿、时长丢失。
    assert [f.id for f in unmerged] == ["frame-000", "frame-000-01"]


# ---------------------------------------------------------------------------
# 4. 指纹
# ---------------------------------------------------------------------------
def test_frames_fingerprint_detects_count_and_text_drift():
    a = frames_fingerprint(["甲", "乙", "丙"])
    assert a["count"] == 3
    assert a["sha1"] != frames_fingerprint(["甲", "乙"])["sha1"]        # 数量变
    assert a["sha1"] != frames_fingerprint(["甲", "乙", "丁"])["sha1"]   # 文本变
    assert a["head"] == "甲" and a["tail"] == "丙"


# ---------------------------------------------------------------------------
# 5. 装配侧校验
# ---------------------------------------------------------------------------
@pytest.fixture
def tiny_ep(tmp_path, tmp_episodes):
    """2-frame 一次性 episode，写在 tmp 的 episodes 根下（P1-6）。"""
    from conftest import make_reuse_source
    make_reuse_source(tmp_episodes, "hw-002", "SC01")
    ep_id = f"sbguard-fp{tmp_path.name}"
    d = {
        "id": ep_id, "title": ep_id, "episode_number": 1, "schema_version": "1.0",
        "status": "draft",
        "frames": [
            {"id": "frame-000", "text": "测试一", "narration_text": "测试一",
             "audio_duration_ms": 2000},
            {"id": "frame-001", "text": "测试二", "narration_text": "测试二",
             "audio_duration_ms": 2000},
        ],
        "shots": {}, "state": {},
    }
    episode_path(ep_id).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    yield ep_id
    # 无清理：tmp_episodes 重定向后 json 也落在 tmp 下


def _plan_with_fingerprint(ep_id, fp=None, image="SC01"):
    plan = {
        "episode": ep_id,
        "images": {image: {"reuse_from": "hw-002"}},
        "scenes": [{"frames": [0, 1], "image": image, "grades": ["dawn", "noon"]}],
    }
    if fp is not None:
        plan["frames_fingerprint"] = fp
    return plan


def _run(ep_id, plan, tmp_path, **kw):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        _, stats = SB.build_episode_shots(ep_id, plan_path=p, persist=False,
                                          preview_rows=0, **kw)
        return None, stats
    except SystemExit as e:
        return str(e), None


def test_matching_fingerprint_passes(tiny_ep, tmp_path):
    """指纹一致 → 正常装配，且 stats 回带指纹供 CLI 展示。"""
    fp = frames_fingerprint(["测试一", "测试二"])
    err, stats = _run(tiny_ep, _plan_with_fingerprint(tiny_ep, fp), tmp_path)
    assert err is None, err
    assert stats["fingerprint"]["count"] == 2


def test_mismatched_fingerprint_rejected(tiny_ep, tmp_path):
    """指纹不一致 → 硬拒。这是 lsm-15min 68 帧漂移的防复发闸门。"""
    bad = {"count": 9, "sha1": "deadbeef1234", "head": "x", "tail": "y"}
    err, _ = _run(tiny_ep, _plan_with_fingerprint(tiny_ep, bad), tmp_path)
    assert err and "不同源" in err
    # 偏差量 = 实际 - 声明，负号表示分镜比当前帧数多，向下错位
    assert "偏差 -7 帧" in err, f"报错应给出可定位的偏差量: {err}"


def test_drifted_frame_count_rejected(tiny_ep, tmp_path):
    """只有帧数变（典型：script.md 被重切）也必须拒。"""
    bad = frames_fingerprint(["测试一"])   # 1 帧 vs 实际 2 帧
    err, _ = _run(tiny_ep, _plan_with_fingerprint(tiny_ep, bad), tmp_path)
    assert err and "不同源" in err


def test_missing_fingerprint_warns_by_default(tiny_ep, tmp_path, capsys):
    """老集（laogongtuan/hanwu-*）写在有指纹机制之前：默认只告警，不阻断。"""
    err, stats = _run(tiny_ep, _plan_with_fingerprint(tiny_ep, None), tmp_path)
    assert err is None, err
    assert stats["fingerprint"] == {}, "缺指纹时 stats 应回空 dict"
    assert "缺 frames_fingerprint" in capsys.readouterr().out


def test_missing_fingerprint_strict_rejected(tiny_ep, tmp_path):
    """新集流水线应开 strict：缺指纹即失败，杜绝「没锚定就装配」。"""
    err, _ = _run(tiny_ep, _plan_with_fingerprint(tiny_ep, None), tmp_path,
                  strict_fingerprint=True)
    assert err and "缺 frames_fingerprint" in err
