# -*- coding: utf-8 -*-
"""lsm-15min 生产事故回归守门（2026-09-05）。

两个 P0 均为「已交付成片看起来正常、实际有问题」的静默故障，必须锁死：

1. P0-1 成片无声：_mux_full_audio 曾 `-c copy` 直拷 wav → 音频流是 pcm_s16le，
   装进 mp4 后大量播放器静音（码流有波形但听不见）。config 明明声明 aac/192k
   却全程无视。修复：视频 copy + 音频按 config 编码。
2. P0-2 verify 误报 FAILED：compose_episode 自载副本按真实音频重算时间轴落盘，
   外层 produce 的 save(ep) 却用内存旧对象覆盖磁盘真值 → verify 拿旧口径
   （33406）对新成片（33404）判失败，而 820MB 正确成片已在盘。
   修复：stage_compose 返回前从磁盘回同步 frames/shots。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import os
import subprocess
from pathlib import Path


from conftest import NEEDS_FFMPEG


# ---------------------------------------------------------------------------
# P0-1：mux 出的音轨必须是 config 声明的编码（aac），绝不能是 pcm
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P0-2：stage_compose 后 ep 的时序必须来自磁盘真值，而非内存旧对象
# ---------------------------------------------------------------------------
def test_stage_compose_resyncs_timeline_from_disk(monkeypatch, tmp_path,
                                                  tmp_episodes):
    """compose 后 ep.frames 必须回同步磁盘真值，否则外层 save 覆盖真值致 verify 误判。"""
    import json
    from app.managers import load, save
    from app.models import Episode, Frame, Shot
    from app.paths import episode_dir
    import app.orchestrator as orch

    ep_id = f"resync-{os.getpid()}"
    ep = Episode(id=ep_id, title="resync", episode_number=1, status="draft")
    save(ep)
    ep_dir = episode_dir(ep_id)      # P1-6：跟随 tmp_episodes 重定向
    out_dir = ep_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 目录在 tmp 下，无需 finally 清理
    # 磁盘真值：frame 时长 2910ms（compose 重算后的值）
    disk = load(ep_id)
    disk.frames = [Frame(id="frame-000", text="真值", audio_duration_ms=2910)]
    save(disk)

    # 内存旧对象：估计时长 3010ms（builder 估计，未含重算）
    stale = load(ep_id)
    stale.frames = [Frame(id="frame-000", text="旧值", audio_duration_ms=3010)]
    # stage_compose 要求 shots 非空（无 shots 视为未完成装配直接拒绝）
    stale.shots = {
        "shot-000": Shot(id="shot-000", frame_id="frame-000", index=0,
                         duration_ms=3010, audio_start_ms=0, audio_end_ms=3010,
                         image_path=str(tmp_path / "img.png"),
                         prompt="", subtitle_text="旧值"),
    }

    final = out_dir / f"{ep_id}_m1.mp4"
    # 造一个"成片"（内容任意，只要存在且 >1KB），并让 compose_episode 只落盘真值
    final.write_bytes(b"\x00" * 2048)

    def _fake_compose(eid, *a, **kw):
        # 模拟 compose_episode：自载副本 → 按真实音频重算 → 落盘真值
        d = load(eid)
        d.frames = [Frame(id="frame-000", text="真值", audio_duration_ms=2910)]
        save(d)
        return final

    monkeypatch.setattr("app.services.compose.compose_episode", _fake_compose)
    orch.stage_compose(stale)

    # 关键断言：内存对象已同步为磁盘真值 2910，而非停留在旧估计 3010
    assert stale.frames[0].audio_duration_ms == 2910, (
        f"stage_compose 未回同步磁盘真值：内存仍是旧估计 "
        f"{stale.frames[0].audio_duration_ms}（期望 2910）。"
        f"外层 save(ep) 会用旧值覆盖磁盘 → verify 拿旧口径判 FAILED（P0-2 复发）")


# ---------------------------------------------------------------------------
# 待办#5：libx264 退路不得用 NVENC 的 preset（p4 → x264 编码器直接失败）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 结构性缺口：splitter 下限与门禁下限的一致性
# ---------------------------------------------------------------------------
# 曾经 MIN_FRAG_CHARS 是手写常量 6（≈1.54s ≈ 38 帧），而时序门禁是 40 帧
# (1.6s) —— 切分器能产出自己的门禁会拒绝的碎片镜。2026-09-05 已改为从门禁
# 反推，两个下限同源，不可能再各飘各的。此例锁住「同源」而非某个魔数。
def test_splitter_min_not_below_gate_min_frames():
    """splitter 切出的最短片段不得低于门禁 MIN_FRAMES，否则自相矛盾。"""
    import math

    from app.services.script import MIN_FRAG_CHARS, TTS_CHARS_PER_SEC
    from app.services.timing_assert import MIN_FRAMES
    from app.services.motion import FPS

    est_sec = MIN_FRAG_CHARS / TTS_CHARS_PER_SEC
    est_frames = int(est_sec * FPS)
    assert est_frames >= MIN_FRAMES, (
        f"splitter 下限 {MIN_FRAG_CHARS} 字 ≈ {est_frames} 帧，"
        f"低于门禁 {MIN_FRAMES} 帧 → 切出的最短镜会被自己的门禁拒绝")


def test_min_frag_chars_is_derived_from_gate_not_hardcoded():
    """下限必须由门禁反推。写死魔数会让两处再次漂移（曾写死 6 vs 门禁 40）。"""
    import math

    from app.services.script import MIN_FRAG_CHARS, TTS_CHARS_PER_SEC
    from app.services.timing_assert import MIN_FRAMES
    from app.services.motion import FPS

    derived = math.ceil(MIN_FRAMES / FPS * TTS_CHARS_PER_SEC)
    assert MIN_FRAG_CHARS == derived, (
        f"MIN_FRAG_CHARS={MIN_FRAG_CHARS} 不等于由门禁推导的 {derived} "
        f"(ceil({MIN_FRAMES}/{FPS}×{TTS_CHARS_PER_SEC}))")


def test_overlong_sentence_is_cut_even_if_side_below_min_frag():
    """两个下限打架时「不超上限」必须赢，否则必然撞 MAX_FRAMES 门禁。

    真实回归：MIN_FRAG_CHARS 6→7 后，lsm-15min 的
    「大唐的天下，本质上是“关陇…”的一场超级并购案」唯一逗号在 6 字处，
    右侧 6 字 < 7 → 所有合规切点失效 → 旧逻辑整句保留 32 字 ≈ 205 帧
    > MAX_FRAMES=200，门禁硬失败。
    """
    from app.services.script import (
        MAX_FRAG_CHARS, MIN_FRAG_CHARS, split_long_sentence)
    from app.services.timing_assert import MAX_FRAMES
    from app.services.motion import FPS
    from app.services.script import TTS_CHARS_PER_SEC

    sent = "大唐的天下，本质上是“关陇军政风投股份有限公司”的一场超级并购案"
    assert len(sent) > MAX_FRAG_CHARS, "用例前提：该句应超过字数硬顶"
    pieces = split_long_sentence(sent, max_chars=18)
    assert len(pieces) > 1, "超长句被整句保留 → 单镜必然 >8s"
    for p in pieces:
        est_frames = len(p) / TTS_CHARS_PER_SEC * FPS
        assert est_frames <= MAX_FRAMES, (
            f"片段 {p!r}（{len(p)} 字 ≈ {est_frames:.0f} 帧）超过门禁上限 "
            f"{MAX_FRAMES} 帧")


def test_max_frag_chars_is_derived_from_gate():
    """上限同样要同源推导，不能手写（与下限同一套常量口径）。"""
    from app.services.script import MAX_FRAG_CHARS, TTS_CHARS_PER_SEC
    from app.services.timing_assert import MAX_FRAMES
    from app.services.motion import FPS

    derived = int(MAX_FRAMES / FPS * TTS_CHARS_PER_SEC)
    assert MAX_FRAG_CHARS == derived, (
        f"MAX_FRAG_CHARS={MAX_FRAG_CHARS} != 推导值 {derived} "
        f"(int({MAX_FRAMES}/{FPS}×{TTS_CHARS_PER_SEC}))")


def test_split_long_sentence_never_emits_sub_gate_fragment():
    """行为层验证：切长句不得产出低于门禁时长的碎片。

    这是「归途上，」那类 4 字前缀被独立成镜的直接回归。
    """
    from app.services.script import MIN_FRAG_CHARS, split_long_sentence
    from app.services.timing_assert import MIN_FRAMES
    from app.services.motion import FPS

    sent = "归途上，他又一次被匈奴俘获，扣留了一年有余。"
    for piece in split_long_sentence(sent, max_chars=18):
        est = int(len(piece) / 3.9 * FPS)
        assert len(piece) >= MIN_FRAG_CHARS or est >= MIN_FRAMES, (
            f"切出碎片 {piece!r}（{len(piece)} 字 ≈ {est} 帧 < 门禁 {MIN_FRAMES}）")


# ---------------------------------------------------------------------------
# 重切安全：id 相同 != 内容相同（切分规则一改就会踩）
# ---------------------------------------------------------------------------
def _ep_with_split(md: str):
    from app.models import Episode
    from app.services.script import split_script

    ep = Episode(id="rbdrift", title="t", episode_number=1)
    ep.frames = split_script(ep, md)
    for f in ep.frames:
        f.audio_path = f"/audio/{f.id}.wav"
        f.audio_duration_ms = 2000
    return ep


def test_rebuild_frames_drops_stale_audio_when_text_changed():
    """同 id 但文本变了 → 旧配音必须丢弃，否则新文本配旧音，静默音画错位。

    MIN_FRAG_CHARS 6→7 后 laogongtuan 有 5 帧、lsm-15min 有 15 帧属于此类。
    """
    from app.services.script import rebuild_frames

    md = "归途上，他又一次被匈奴俘获，扣留了一年有余，才得以回到长安。"
    ep = _ep_with_split(md)
    victim = ep.frames[0]
    victim.text = victim.text + "（改过）"
    out = {f.id: f for f in rebuild_frames(ep, md)}
    assert out[victim.id].audio_path is None, \
        "文本变了却继承了旧配音 → 成片音画错位且无任何报错"
    assert out[victim.id].audio_duration_ms in (None, 0), \
        "旧时长同样属于旧文本，不能继承"


def test_rebuild_frames_keeps_audio_when_text_identical():
    """未受影响的帧必须原样继承（否则每次重切都要重跑全片 TTS）。"""
    from app.services.script import rebuild_frames

    md = "归途上，他又一次被匈奴俘获，扣留了一年有余，才得以回到长安。"
    ep = _ep_with_split(md)
    out = {f.id: f for f in rebuild_frames(ep, md)}
    assert all(f.audio_path == f"/audio/{f.id}.wav" for f in out.values())
    assert all(f.audio_duration_ms == 2000 for f in out.values())


# ---------------------------------------------------------------------------
# 编码器级回退：NVENC 资源失败必须自动降级 libx264，不得整片失败
# ---------------------------------------------------------------------------
# 真实故障（2026-09-05 全量跑批实测）：
#   [h264_nvenc] CreateInputBuffer failed: out of memory (10)
#   → _encode_video_span 直接 raise → 整片 compose 失败。
# 而 _probe_nvenc 只用 320x568 的 0.05s 素材探测，占用极小、必然通过，
# 挡不住 1080x1920 真实编码时的显存/NVENC 会话耗尽。
#
# 这里用确定性的假失败驱动（不依赖真实 OOM，那在机器上时有时无）。
_NV_OOM = ("段编码中途断管：已写 2/54 帧。ffmpeg stderr:\n"
           "[h264_nvenc @ 0x1] CreateInputBuffer failed: out of memory (10): \n"
           "[vost#0:0/h264_nvenc] Error while opening encoder")
_NV_PARAM = ("段编码未完整收口：rc=-22 已写 0/54 帧。ffmpeg stderr:\n"
             "[h264_nvenc @ 0x1] Invalid argument")


def _install_fake_encoder(monkeypatch, fail_msg, ok_frames=54):
    """把 _encode_video_span_once 换成记账假实现，返回调用记录。"""
    from app.services import compose as C
    rec = {"calls": 0, "vcodecs": []}

    # 签名必须与真实 _encode_video_span_once 一致（含 2026-09-26 新增的
    # transitions 换图黑场参数）。测试替身漏了这个参数就会在降级重试路径上
    # 抛 TypeError，把「签名漂移」伪装成「降级逻辑坏了」。
    def fake(timeline_slice, upscaled, out_mp4, vcodec, timeout_sec=0,
             plan_frames=None, transitions=None):
        rec["calls"] += 1
        rec["vcodecs"].append(list(vcodec))
        if rec["calls"] == 1 and fail_msg:
            raise RuntimeError(fail_msg)
        return ok_frames

    monkeypatch.setattr(C, "_encode_video_span_once", fake)
    monkeypatch.setattr(C, "_HWENC_DISABLED", False)   # 每个用例从干净状态起
    return rec


def test_nvenc_oom_downgrades_to_libx264(monkeypatch, tmp_path):
    """NVENC 显存/会话耗尽 → 自动降级 libx264 重试一次并成功。"""
    from app.services import compose as C
    rec = _install_fake_encoder(monkeypatch, _NV_OOM)
    nv = ["-c:v", "h264_nvenc", "-profile:v", "high", "-level", "4.1",
          "-preset", "p4", "-cq", "20"]

    n = C._encode_video_span([], {}, tmp_path / "seg.mp4", nv, 0, None)

    assert n == 54
    assert rec["calls"] == 2, "应在降级后重试一次"
    assert rec["vcodecs"][0][1] == "h264_nvenc"
    assert rec["vcodecs"][1][1] == "libx264", f"第二次应走软件编码，实为 {rec['vcodecs'][1][:2]}"
    assert C._HWENC_DISABLED is True, "降级后须置全局闸门，避免后续段重复撞 OOM"


def test_nvenc_param_error_not_retried(monkeypatch, tmp_path):
    """参数类错误降级也必然失败，不能白重试（还掩盖真实原因）。"""
    from app.services import compose as C
    rec = _install_fake_encoder(monkeypatch, _NV_PARAM)
    nv = ["-c:v", "h264_nvenc", "-profile:v", "high", "-preset", "p4", "-cq", "20"]

    with pytest.raises(RuntimeError, match="Invalid argument"):
        C._encode_video_span([], {}, tmp_path / "seg.mp4", nv, 0, None)

    assert rec["calls"] == 1, "非资源类失败不得重试"
    assert C._HWENC_DISABLED is False


def test_degraded_flag_forces_software_for_later_segments(monkeypatch, tmp_path):
    """降级闸门置位后，调用方手里旧的 NVENC vcodec 也必须被换成软件编码。

    调用方在分段循环外只算了一次 vcodec，若不在入口处纠正，后续每段都会
    再撞一次 NVENC 再降级（2× 耗时）。
    """
    from app.services import compose as C
    rec = _install_fake_encoder(monkeypatch, None)   # 不再失败
    monkeypatch.setattr(C, "_HWENC_DISABLED", True)
    nv = ["-c:v", "h264_nvenc", "-profile:v", "high", "-preset", "p4", "-cq", "20"]

    C._encode_video_span([], {}, tmp_path / "seg.mp4", nv, 0, None)

    assert rec["calls"] == 1
    assert rec["vcodecs"][0][1] == "libx264", "闸门已置位却仍用了硬件编码器"
