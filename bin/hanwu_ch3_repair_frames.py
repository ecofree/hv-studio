#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
hanwu-ch3 数据修复：frames 与 shots 对齐重建（P0-4）。

【根因（审计核实）】ch3.json 的 shots（101 条，frame-000..frame-066+子段）携带的是
权威成片旁白（与 script.md / 已交付 mp4 一致）；但 frames 列表在 08:50 被一次旁白
重切【覆盖成了另一段浓缩文本】（frame-000..frame-041，"凿空的意义"结尾），未同步
重建 shots → 48 个 shot.frame_id 在 frames dict 里缺失 → compose 一进就 KeyError。

【修复原则】以【shots 为唯一可信源】重建 frames（shots 与 script.md/成片一致）：
  - frames = 每个 shot.frame_id 一条（按 shot.index 去重排序）
  - id / text / narration_text 取自该 shot 的 subtitle_text（权威旁白全文）
  - audio_path 指回磁盘已有 wav（审计确认 101 个 shot.frame_id 全部有 wav）
  - audio_duration_ms 用 ffprobe 实测
  - char_align / subtitle / shot_ids 置空/回填（下游 compose 不需要 char_align）
修复后 compose 的 guards（磁盘 wav 校验 + 重算后时序 gate）应全过。

用法： python bin/hanwu_ch3_repair_frames.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("D:/hv")
sys.path.insert(0, str(ROOT))

from app.managers import save  # noqa: E402
from app.managers.episode import load  # noqa: E402


def _wav_ms(p: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
        capture_output=True, text=True).stdout.strip()
    return int(float(out) * 1000)


def main():
    ep = load("hanwu-ch3")

    # 0) 备份当前（可能损坏）json
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = ROOT / ".trash_hv" / "episodes" / f"hanwu-ch3.json.bak_{ts}_pre_repair"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "episodes" / "hanwu-ch3.json", bak)
    print(f"备份原 json -> {bak}")

    shots = sorted(ep.shots.values(), key=lambda s: s.index)
    audio_dir = ROOT / "episodes" / "hanwu-ch3" / "audio"

    # 1) 每个 shot.frame_id 一条 frame（去重保序）
    seen: dict[str, dict] = {}
    for sh in shots:
        fid = sh.frame_id
        wav = audio_dir / f"{fid}.wav"
        if not wav.exists():
            print(f"  !! {fid} 无 wav，跳过（将导致 compose 缺音频）")
            continue
        seen[fid] = {
            "id": fid,
            "text": sh.subtitle_text or "",
            "narration_text": sh.subtitle_text or "",
            "audio_path": str(wav).replace("\\", "/"),
            "audio_duration_ms": _wav_ms(wav),
            "char_align": [],
            "subtitle": [],
            "shot_ids": [sh.id],
        }

    frames = list(seen.values())
    print(f"重建 frames: {len(frames)} 条（原 {len(ep.frames)} 条，shots 引用 {len(shots)} 个 frame_id）")

    ep.frames = []
    from app.models import Frame
    for d in frames:
        ep.frames.append(Frame(**d))

    save(ep)

    # 2) 校验：frames↔shots 对齐
    fid_set = {f.id for f in ep.frames}
    shot_refs = {sh.frame_id for sh in ep.shots.values()}
    missing = sorted(shot_refs - fid_set)
    audio_ms = sum(f.audio_duration_ms or 0 for f in ep.frames)
    print(f"校验：frames={len(fid_set)} shots_refs={len(shot_refs)} missing={len(missing)} "
          f"音频总时长={audio_ms/1000:.1f}s")
    if missing:
        print(f"  !! 仍缺: {missing[:20]}")
        raise SystemExit(1)

    # 3) 时序 guards（compose 将用的口径）
    from app.services.compose import _group_of, _verify_audio_vs_disk, HEAD_MS, TAIL_MS
    from app.services.timing_assert import assert_timing_or_raise
    shs = sorted(ep.shots.values(), key=lambda s: s.index)
    frames_by_id = {f.id: f for f in ep.frames}
    prev_g, head_of = None, {}
    for sh in shs:
        g = _group_of(sh.frame_id)
        head_of[sh.id] = HEAD_MS if (prev_g is not None and g != prev_g) else 0
        prev_g = g
    tail_of = {}
    for i, sh in enumerate(shs):
        nxt = shs[i + 1] if i + 1 < len(shs) else None
        tail_of[sh.id] = TAIL_MS if (nxt is None or _group_of(nxt.frame_id) != _group_of(sh.frame_id)) else 0
    for sh in shs:
        f = frames_by_id[sh.frame_id]
        a = f.audio_duration_ms or 0
        sh.audio_start_ms = head_of[sh.id]
        sh.duration_ms = sh.audio_start_ms + a + tail_of[sh.id]
        sh.audio_end_ms = sh.audio_start_ms + a
    _verify_audio_vs_disk(shs, frames_by_id)
    assert_timing_or_raise(ep)
    save(ep)
    print("时序 guards 全过：磁盘 wav 校验 OK + 重算后 timing gate OK")
    print(f"修复完成。成片仍需 compose_episode('hanwu-ch3') 重合成（沙箱 RAM 允许时）。")


if __name__ == "__main__":
    main()
