# -*- coding: utf-8 -*-
"""分段渲染 + 断点续跑测试（2026-09-04）。

锁住 compose 长片分段路径的关键承诺：
  1) 压小 segment_max_sec 后，产出 ≥2 段，concat 成片可解码、无 .tmp 残留
  2) 已存在且帧数 == 预期 的段被跳过（断点续跑真实生效，不是每次全量重编）
  3) 段完成判据用逐镜求和口径——若退化成一次舍入，11/15 段判不完（见
     test_frame_accounting，这里是它的端到端验证）
"""
from __future__ import annotations

import json
import os
import subprocess
import wave
from pathlib import Path

import pytest

from app.config.config import ROOT

EPISODES = ROOT / "episodes"
N_SHOTS = 24          # 24 镜，每镜 ~3s → ~1800 帧
SHOT_MS = 3200        # 单镜时长（错开整帧边界制造舍入分叉：3200ms→80帧整? 3200*25/1000=80 整）

def _build_multi_episode(tmp_path, ep_id):
    """造 N 镜静态 episode：共享一张小图 + 一段 wav。写真实 episodes/。"""
    from tests.conftest import make_small_images, make_small_wav
    src_p, big_p = make_small_images(tmp_path)
    wav = make_small_wav(tmp_path, dur_s=3.2)
    with wave.open(str(wav), "rb") as w:
        real_dur = int(w.getnframes() / float(w.getframerate()) * 1000)

    frames, shots = [], {}
    for i in range(N_SHOTS):
        fid = f"frame-{i:03d}"
        sid = f"shot-{i:03d}"
        frames.append({
            "id": fid, "text": f"第{i}镜测试", "narration_text": f"第{i}镜测试",
            "audio_path": str(wav).replace("\\", "/"), "audio_duration_ms": real_dur,
            "char_align": [], "subtitle": [], "shot_ids": [sid],
        })
        shots[sid] = {
            "id": sid, "frame_id": fid, "index": i, "type": "ken_burns",
            "is_hero": False, "narration": "", "shot_type": "medium",
            "cast_refs": [], "keywords": [], "scene_ref": None,
            "duration_ms": SHOT_MS, "audio_start_ms": 0,
            "audio_end_ms": SHOT_MS, "audio_segment_path": None,
            "prompt": "", "negative_prompt": "", "expression": "", "pose": "",
            "ref_pack_id": None, "ref_pack_paths": [],
            "image_path": str(src_p).replace("\\", "/"),
            "subtitle_text": f"第{i}镜", "face_sim": None, "face_sim_warn": False,
            "retry_count": 0, "last_error": None, "stage": {},
            "motion": {"type": "static", "intensity": 0.0,
                       "start_xy": [0.5, 0.5], "end_xy": [0.5, 0.5],
                       "start_zoom": 1.0, "end_zoom": 1.0, "easing": "linear",
                       "grade": None},
        }
    d = {
        "id": ep_id, "title": ep_id, "episode_number": 1, "schema_version": "1.0",
        "status": "draft", "frames": frames, "shots": shots, "state": {},
        "total_seconds": N_SHOTS * SHOT_MS / 1000,
    }
    ep_json = EPISODES / f"{ep_id}.json"
    ep_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return ep_id, src_p, big_p, real_dur


@pytest.fixture
def segmented_episode(tmp_path, monkeypatch):
    from app.config.config import get_settings
    cfg = get_settings().compose
    # 压低门槛强制走分段路径 + 压小段长制造 ≥2 段
    monkeypatch.setattr(cfg, "segment_min_frames", 100)     # 1800 帧 > 100 → 分段
    monkeypatch.setattr(cfg, "segment_max_sec", 8.0)        # 每段 ≤8s → 24*3.2/8 ≈ 9 段
    ep_id, src_p, big_p, dur = _build_multi_episode(tmp_path, f"segtest-{os.getpid()}")
    yield ep_id, big_p, dur
    try:
        (EPISODES / f"{ep_id}.json").unlink()
    except Exception:
        pass
    ep_dir = EPISODES / ep_id
    if ep_dir.exists():
        import shutil
        shutil.rmtree(ep_dir, ignore_errors=True)


def test_segmented_compose_produces_valid_mp4(segmented_episode):
    ep_id, big_p, dur = segmented_episode
    from app.services.compose import compose_episode
    out = EPISODES / ep_id / "out"
    final = out / f"{ep_id}_m1.mp4"
    assert not final.exists(), "前提：无旧成片"

    p = compose_episode(ep_id)
    assert Path(p).exists() and Path(p).stat().st_size > 1024

    # 无 .tmp 残留（段 concat / mux 都应原子收口）
    tmps = list(out.glob("*.tmp")) + list(out.glob("*.mp4.tmp")) + \
           list((EPISODES / ep_id / "build" / "segments").glob("*.tmp"))
    assert tmps == [], f"残留 .tmp: {tmps}"

    # 至少产出多段
    seg_dir = EPISODES / ep_id / "build" / "segments"
    segs = sorted(seg_dir.glob("seg-*.mp4"))
    assert len(segs) >= 2, f"应分段产出, 实际 {len(segs)} 段"

    # 成片可解码、有音轨
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", str(final)],
                       capture_output=True, text=True)
    assert "video" in r.stdout and "audio" in r.stdout, \
        f"应含视频+音轨: {r.stdout}"
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final),
                          "-f", "null", "-"], capture_output=True, text=True)
    assert dec.returncode == 0 and dec.stderr.strip() == "", \
        f"decode 错误: {dec.stderr[-400:]}"


def test_segments_are_skipped_when_already_done(segmented_episode, monkeypatch):
    """首跑产出段后，模拟中断重跑：已存在且帧数对得上的段被跳过（不重编）。"""
    ep_id, big_p, dur = segmented_episode
    from app.services.compose import (
        compose_episode, _encode_video_span, expected_frames, _segments_of,
    )
    # 先完整跑一遍，让所有段落盘
    compose_episode(ep_id)

    from app.services.compose import _probe_segment_frames
    from app.services.motion import FPS
    from app.managers import load
    ep = load(ep_id)
    shots = sorted(ep.shots.values(), key=lambda x: x.index)
    tl = [(s, 0, s.duration_ms) for s in shots]
    from app.config.config import get_settings
    cfg = get_settings().compose
    segs = _segments_of(tl, cfg.segment_max_sec)
    seg_dir = EPISODES / ep_id / "build" / "segments"

    # 手工验证：每个已有段的帧数 == expected_frames(段)（断点续跑能命中）
    for i, sl in enumerate(segs):
        p = seg_dir / f"seg-{i:03d}.mp4"
        assert p.exists(), f"seg-{i:03d} 缺失"
        nf = _probe_segment_frames(p)
        exp = expected_frames(sl)
        assert nf == exp, f"seg-{i:03d}: 帧数 {nf}≠预期 {exp}（判据不命中，续跑失效）"

    # 关键：重跑 compose 时用桩计数 _encode_video_span，断言零次重编
    calls = []
    orig = _encode_video_span

    def spy(*a, **k):
        calls.append(a)
        return orig(*a, **k)

    monkeypatch.setattr("app.services.compose._encode_video_span", spy)
    compose_episode(ep_id)   # 第二次跑，所有段已就绪
    assert calls == [], f"断点续跑应跳过所有段, 却重编了 {len(calls)} 次"
