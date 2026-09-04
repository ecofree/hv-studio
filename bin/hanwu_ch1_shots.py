#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
《汉武雄风》第一章「龙城飞将·北击匈奴」镜头装配（54 frame → 54 shot）

底图来源（本集新增 + 跨集复用）：
  - NEW-1~8            : episodes/hanwu-feature/shots/（bin/hanwu_new_scenes.py 产出）
  - SC01/SC02/SC07/SC08: episodes/hw-002/shots/（跨集复用，零新增成本）
      SC01 未央宫朝堂 → 刘彻登基/问群臣/捷报/转向西域的思考
      SC02 西域未知   → 章末「投向西域」的苍茫钩子
      SC07 匈奴王庭   → 单于受辱的匈奴视角
      SC08 草原       → 「漠南无王庭」的空寂草原

三轴派生（本地零 API 成本）：机位 zoom 阶梯 / 光线 grade / 运镜 motion 交替。

注意：grade 只接受 motion.GRADE_MAP 的 6 个合法值
      dawn/noon/dusk/night/fire/frost —— 写错会静默失效（原样返回）。

用法：
    python bin/hanwu_ch1_shots.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, "D:/hv")

from app.config.config import get_settings
from app.managers import save
from app.managers.episode import load
from app.models import MotionSpec, Shot

ROOT = Path("D:/hv")
EP = "hanwu-feature"
NEW_DIR = ROOT / "episodes" / EP / "shots"
REUSE_DIR = ROOT / "episodes" / "hw-002" / "shots"   # 跨集复用底图

# ---------------------------------------------------------------------------
# 场景表：帧区间 → 底图。
# 区间按 chapters 的实际旁白内容对齐（逐帧核对过，勿凭印象改，否则图文不符）。
# format: (起帧, 止帧, 场景ID, 名称, 光线循环)
# ---------------------------------------------------------------------------
SCENES = [
    (0,  2,  "NEW-1", "白登之围",      ["frost", "dawn", "dusk"]),
    (3,  6,  "NEW-2", "和亲车队",      ["dawn", "dawn", "noon", "dusk"]),
    (7,  11, "SC01",  "未央宫朝堂·复用", ["dawn", "dawn", "noon", "dawn", "dawn"]),
    (12, 17, "NEW-3", "马邑之谷",      ["dusk", "noon", "noon", "dusk", "dawn", "dusk"]),
    (18, 22, "NEW-4", "龙城夜袭",      ["night", "night", "fire", "dawn", "dawn"]),
    (23, 24, "SC01",  "未央宫朝堂·复用", ["dawn", "noon"]),
    (25, 25, "SC07",  "匈奴王庭·复用",  ["fire"]),
    (26, 30, "NEW-5", "河朔漠南大战",   ["dusk", "noon", "dusk", "noon", "dusk"]),
    (31, 35, "NEW-6", "少年霍去病拜将", ["dawn", "noon", "noon", "dawn", "dawn"]),
    (36, 37, "NEW-7", "河西之战",      ["dawn", "dusk"]),
    (38, 43, "NEW-8", "漠北封狼居胥",   ["dusk", "noon", "noon", "dusk", "dusk", "dusk"]),
    (44, 46, "SC08",  "草原·复用",     ["dawn", "noon", "dusk"]),
    (47, 49, "SC01",  "未央宫朝堂·复用", ["noon", "dawn", "dawn"]),
    (50, 53, "SC02",  "西域未知·复用",  ["dusk", "dusk", "dawn", "dawn"]),
]

# ---------------------------------------------------------------------------
# 机位/光线/运镜派生（config 驱动）
# ---------------------------------------------------------------------------
_settings = get_settings()
_PROF = _settings.get_profile()
MAX_ZOOM = _PROF.motion.max_zoom                     # 1.15
_FORBID_SAME = _PROF.motion.forbid_consecutive_same  # True

RAW_ZOOM_LADDER = [1.12, 1.26, 1.40, 1.18, 1.32, 1.08, 1.22]
ZOOM_LADDER = [min(z, MAX_ZOOM) for z in RAW_ZOOM_LADDER]

MOTION_POOL = ["push", "drift", "pull", "pan"]
INTENSITY_CYCLE = [0.07, 0.06, 0.08, 0.10, 0.05, 0.09, 0.10, 0.06]
_last_motion_type = None

GRADE_MAP_KEYS = {"dawn", "noon", "dusk", "night", "fire", "frost"}


def _img_path(sid: str) -> Path:
    return (NEW_DIR if sid.startswith("NEW-") else REUSE_DIR) / f"{sid}.png"


def _next_motion(desired: str) -> str:
    global _last_motion_type
    if _FORBID_SAME and desired == _last_motion_type:
        for alt in MOTION_POOL:
            if alt != _last_motion_type:
                desired = alt
                break
    _last_motion_type = desired
    return desired


def _build_motion(zoom: float, motion_type: str, intensity: float,
                  light: str) -> MotionSpec:
    if motion_type == "push":
        start_xy, end_xy = (0.50, 0.50), (0.50, 0.50)
        start_zoom, end_zoom = zoom, zoom + intensity
    elif motion_type == "pull":
        start_xy, end_xy = (0.50, 0.50), (0.50, 0.50)
        start_zoom, end_zoom = zoom, max(1.0, zoom - intensity)
    elif motion_type == "drift":
        start_xy, end_xy = (0.46, 0.50), (0.54, 0.50)
        start_zoom, end_zoom = zoom, zoom
    else:  # pan
        start_xy, end_xy = (0.44, 0.48), (0.56, 0.52)
        start_zoom, end_zoom = zoom, zoom
    return MotionSpec(type=motion_type, intensity=round(intensity, 3),
                      start_xy=start_xy, end_xy=end_xy,
                      start_zoom=round(start_zoom, 3),
                      end_zoom=round(end_zoom, 3), grade=light)


def scene_of(i: int):
    for (a, b, sid, name, lights) in SCENES:
        if a <= i <= b:
            return sid, name, lights, i - a
    raise KeyError(f"frame {i} 未落入任何场景")


def main():
    ep = load(EP)
    frames = ep.frames
    if not frames:
        raise SystemExit("episode 无 frame，先跑 script 切分")

    # 底图完整性 + grade 合法性前置校验
    missing = sorted({sid for (_, _, sid, _, _) in SCENES
                      if not _img_path(sid).exists()})
    if missing:
        raise SystemExit(f"缺少底图：{missing}")
    bad_grades = sorted({g for (_, _, _, _, ls) in SCENES for g in ls
                         if g not in GRADE_MAP_KEYS})
    if bad_grades:
        raise SystemExit(f"非法 grade（会静默失效）：{bad_grades}")

    n_new = len({sid for (_, _, sid, _, _) in SCENES if sid.startswith("NEW-")})
    n_reuse = len({sid for (_, _, sid, _, _) in SCENES}) - n_new
    print(f"frames: {len(frames)} | 底图 {n_new} 新增 + {n_reuse} 跨集复用 "
          f"| max_zoom {MAX_ZOOM}")

    ep.shots = {}
    rows = []
    for i, f in enumerate(frames):
        sid_img, name, lights, j = scene_of(i)
        zoom = 1.0 if j == 0 else ZOOM_LADDER[(j - 1) % len(ZOOM_LADDER)]
        light = lights[j % len(lights)]
        mt = _next_motion(MOTION_POOL[i % len(MOTION_POOL)])
        motion = _build_motion(zoom, mt, INTENSITY_CYCLE[i % len(INTENSITY_CYCLE)],
                               light)

        sid = f"shot-{i:03d}"
        # 1:1 frame→shot 时，相邻 shot 跨 frame 边界 → head/tail 都会加。
        # 故持久化 duration_ms 预扣 250ms（head=100 + tail=150），避免
        # compose 重算后仍出现 < 1.6s 的碎镜（timing gate #2 拦截）。
        a_dur = f.audio_duration_ms or 3000
        dur = a_dur + 250
        ep.shots[sid] = Shot(
            id=sid, frame_id=f.id, index=i, type="ken_burns",
            duration_ms=dur, audio_start_ms=0, audio_end_ms=a_dur,
            prompt=f"[{sid_img} {name}]",
            subtitle_text=f.text,          # 原文 → 字幕（勿用 narration_text）
            image_path=str(_img_path(sid_img)).replace("\\", "/"),
            motion=motion,
        )
        f.shot_ids = [sid]
        if i < 4 or i % 8 == 0:
            rows.append((i, sid_img, name, f"{zoom:.2f}", light,
                         motion.type, f.text[:20]))

    save(ep)

    print("\n idx 底图    名称             机位  光线   运镜    文案")
    print(" " + "-" * 78)
    for i, sid, name, z, g, mt, txt in rows:
        print(f" {i:3d} {sid:<8s} {name:<15s} {z}  {g:<6s} {mt:<7s} {txt}")
    print(" " + "-" * 78)

    used: dict[str, int] = {}
    for i in range(len(frames)):
        k = scene_of(i)[0]
        used[k] = used.get(k, 0) + 1
    print(f"\n共 {len(ep.shots)} shot / {len(used)} 张底图，"
          f"归并比 {len(frames)/len(used):.2f}:1")
    print("每图复用：" + "  ".join(f"{k}:{v}" for k, v in sorted(used.items())))
    tot = sum((f.audio_duration_ms or 0) for f in frames)
    print(f"旁白 {tot/1000:.1f}s = {tot/60000:.2f}min")


if __name__ == "__main__":
    main()
