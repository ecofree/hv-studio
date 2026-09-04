#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
hw-002 镜头装配：24 张场景底图 → 88 个 shot。

三轴派生全部在本地完成，零额外 API 成本：
  1. 机位 zoom   : 全景 1.0 建立 → 中景/近景/特写循环，靠裁切窗打破重复；
                   档位由 config profile.motion.max_zoom 封顶（1.15）
  2. 光线 grade  : 场景脚本里预置的 dawn/noon/dusk/night/fire/frost 循环
  3. 运镜 motion : push/drift/pull/pan 交替；config forbid_consecutive_same
                   强制相邻镜头不同向

配置对齐：Task #31 —— max_zoom / forbid_consecutive_same 从 config.yaml 读取，
不再硬编码超限档位。
用法：
    python bin/m2_shots.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path("D:/hv")
sys.path.insert(0, str(ROOT))

from app.managers import save  # noqa: E402
from app.managers.episode import load  # noqa: E402
from app.models import MotionSpec, Shot, ShotType  # noqa: E402
from app.config.config import get_settings  # noqa: E402

EP = "hw-002"
SHOTS_DIR = ROOT / "episodes" / EP / "shots"

# ---- 配置驱动（Task #31 对齐 config.yaml）----
# 读取 profile.motion.max_zoom 封顶机位阶梯；forbid_consecutive_same 决定运镜
# 是否强制「相邻镜头不同向」。
_settings = get_settings()
_PROF = _settings.get_profile()
MAX_ZOOM = _PROF.motion.max_zoom          # 1.15
_FORBID_SAME = _PROF.motion.forbid_consecutive_same  # True

# 载入场景表（与生图脚本同源，避免两处维护）
_spec = importlib.util.spec_from_file_location("m2_scenes", ROOT / "bin" / "m2_scenes.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
SCENES = _m.SCENES

# 机位阶梯（场景内第 2 镜起循环）
#   1.0  = 全景（z=1 时裁切窗恰为 1080×1920，真 1:1 像素，最锐）
#   阶梯值来自 config profile.motion.max_zoom（1.15）封顶：任何档位不超过它，
#   否则有效像素不足、放大感失真。
_RAW_ZOOM_LADDER = [1.12, 1.26, 1.40, 1.18, 1.32, 1.08, 1.22]
ZOOM_LADDER = [min(z, MAX_ZOOM) for z in _RAW_ZOOM_LADDER]

# 运镜池：全部候选动作类型（去重保序）。相邻镜头是否不同向由 forbid_consecutive_same 控制。
_MOTION_POOL = ["push", "drift", "pull", "pan"]
INTENSITY_CYCLE = [0.07, 0.06, 0.08, 0.10, 0.05, 0.09, 0.10, 0.06]

# 全局「上一个已用运镜类型」，用于 forbid_consecutive_same 强制交替
_last_motion_type: str | None = None


def _next_motion_type(desired: str) -> str:
    """在 forbid_consecutive_same 下，避免与上一镜同型；否则直接用轮询 desired。"""
    global _last_motion_type
    if _FORBID_SAME and desired == _last_motion_type:
        # 在去掉同型后的候选里挑一个「次优」：按 MOTION 顺序取下一个可用
        for alt in _MOTION_POOL:
            if alt != _last_motion_type:
                desired = alt
                break
    _last_motion_type = desired
    return desired


def scene_of(i: int):
    for sc in SCENES:
        a, b = sc["frames"]
        if a <= i <= b:
            return sc, i - a
    raise KeyError(f"frame {i} 未落入任何场景")


def build_motion(i: int, j: int, zoom: float, grade: str) -> MotionSpec:
    # 全局运镜轮询（基础节奏），再交给 _next_motion_type 施加 forbid_consecutive_same
    base = _MOTION_POOL[i % len(_MOTION_POOL)]
    k = _next_motion_type(base)
    intensity = INTENSITY_CYCLE[i % len(INTENSITY_CYCLE)]

    # pan 需要横向移动余量，高倍机位下几乎看不出来 → 降级为 drift
    if k == "pan" and zoom > 1.15:
        k = "drift"

    start_xy = end_xy = None
    if k == "drift":
        # 纵向微移，方向逐镜交替
        start_xy, end_xy = ((0.5, 0.40), (0.5, 0.60)) if i % 2 == 0 \
            else ((0.5, 0.60), (0.5, 0.40))
    elif k == "pan":
        start_xy, end_xy = ((0.10, 0.50), (0.90, 0.50)) if i % 2 == 0 \
            else ((0.90, 0.50), (0.10, 0.50))

    return MotionSpec(type=k, intensity=intensity, start_xy=start_xy,
                      end_xy=end_xy, start_zoom=zoom, end_zoom=zoom, grade=grade)


def main():
    ep = load(EP)
    frames = ep.frames
    print(f"episode {EP}: {len(frames)} frames, {len(SCENES)} 场景底图")

    missing = []
    for sc in SCENES:
        if not (SHOTS_DIR / f"{sc['id']}.png").exists():
            missing.append(sc["id"])
    if missing:
        print(f"缺少底图：{missing}（先跑 bin/m2_scenes.py）")
        return

    ep.shots = {}
    rows = []
    for i, f in enumerate(frames):
        sc, j = scene_of(i)
        zoom = 1.0 if j == 0 else ZOOM_LADDER[(j - 1) % len(ZOOM_LADDER)]
        grade = sc["lights"][j % len(sc["lights"])]
        motion = build_motion(i, j, zoom, grade)

        sid = f"shot-{i:03d}"
        img = SHOTS_DIR / f"{sc['id']}.png"
        dur = f.audio_duration_ms or 3000
        ep.shots[sid] = Shot(
            id=sid, frame_id=f.id, index=i, type=ShotType.KEN_BURNS,
            duration_ms=dur, audio_start_ms=0, audio_end_ms=dur,
            prompt=f"[{sc['id']} {sc['name']}] {sc['prompt'][:60]}",
            # 字幕必须用 frame.text（原文），narration_text 含多音字替换
            subtitle_text=f.text,
            image_path=str(img).replace("\\", "/"),
            motion=motion,
        )
        f.shot_ids = [sid]
        if i < 6 or i % 12 == 0:
            rows.append((i, sc["id"], sc["name"], f"{zoom:.2f}", grade,
                         motion.type, f.text[:22]))

    save(ep)

    print("\n idx 场景  名称           机位  光线   运镜    文案")
    print(" " + "-" * 74)
    for i, sid, name, z, g, mt, txt in rows:
        print(f" {i:3d} {sid} {name:<14s} {z}  {g:<6s} {mt:<7s} {txt}")
    print(" " + "-" * 74)

    used = {s["id"]: 0 for s in SCENES}
    for i in range(len(frames)):
        used[scene_of(i)[0]["id"]] += 1
    print(f"\n共 {len(ep.shots)} 个 shot，复用 {len(SCENES)} 张底图，"
          f"归并比 {len(frames)/len(SCENES):.2f}:1")
    print("每镜复用次数：" + "  ".join(f"{k}:{v}" for k, v in used.items()))
    tot = sum((f.audio_duration_ms or 0) for f in frames)
    print(f"旁白总时长 {tot/1000:.1f}s（不含镜头间隙）")


if __name__ == "__main__":
    main()
