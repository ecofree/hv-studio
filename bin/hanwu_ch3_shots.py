#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
《汉武雄风》第三章「天马与帝国·丝路贯通」镜头装配（96 frame → 96 shot）

底图来源（本集新增 + 跨集复用）：
  - NEW-9~12   : episodes/hanwu-feature/shots/（bin/hanwu_new_scenes.py 产出）
  - SC01/SC02/SC11/SC13/SC18/SC19/SC20/SC22/SC23/SC24 : episodes/hw-002/shots/
      SC01 未央宫朝堂 → 刘彻决策/回诏
      SC02 西域未知   → 「在此之前中原对西方是一片空白」
      SC11 大宛康居   → 大宛/贰师城求马
      SC13 大夏市集   → 长安国际都市、西域商人
      SC19 奏报竹简   → 晚年诏书
      SC20 二次出使乌孙 → 张骞二次西行/细君公主和亲
      SC22 病逝长安   → 手持汉节的张骞（章末收束）
      SC23 史记凿空   → 司马迁「凿空」
      SC24 丝路千年   → 驼铃/物产交流/千年之路

区间按逐帧核对过的实际旁白内容对齐（勿凭印象改，否则图文不符）。
grade 只接受 dawn/noon/dusk/night/fire/frost 六个合法值。

用法：
    python bin/hanwu_ch3_shots.py
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
EP = "hanwu-ch3"
NEW_DIR = ROOT / "episodes" / "hanwu-feature" / "shots"   # NEW-1~12 所在
REUSE_DIR = ROOT / "episodes" / "hw-002" / "shots"        # 跨集复用底图

# (起帧, 止帧, 场景ID, 名称, 光线循环)
SCENES = [
    (0,  1,  "SC01",  "朝堂·复用",        ["dawn", "noon"]),
    (2,  5,  "SC20",  "二次出使乌孙·复用", ["dawn", "noon", "dusk", "dawn"]),
    (6,  7,  "SC20",  "二次出使乌孙·复用", ["dawn", "noon"]),
    (8,  11, "NEW-9", "万国来朝",        ["dawn", "noon", "dusk", "dawn"]),
    (12, 18, "SC20",  "乌孙和亲·复用",    ["dusk", "dawn", "noon", "dusk",
                                          "dawn", "noon", "dusk"]),
    (19, 21, "NEW-9", "万国来朝",        ["noon", "dawn", "noon"]),
    (22, 27, "SC13",  "大夏市集·复用",    ["noon", "dawn", "noon", "dusk",
                                          "dawn", "noon"]),
    (28, 30, "SC01",  "朝堂·复用",        ["noon", "dawn", "noon"]),
    (31, 32, "SC11",  "大宛康居·复用",    ["noon", "dawn"]),
    (33, 36, "SC11",  "大宛康居·复用",    ["dawn", "noon", "dusk", "dusk"]),
    (37, 38, "SC01",  "朝堂·复用",        ["dawn", "dusk"]),
    (39, 40, "NEW-12", "玉门关·复用",     ["dawn", "dusk"]),
    (41, 45, "NEW-10", "贰师城之战",      ["noon", "dusk", "dawn", "noon", "dusk"]),
    (46, 48, "NEW-12", "玉门关·复用",     ["dusk", "dawn", "dusk"]),
    (49, 50, "SC01",  "朝堂·复用",        ["dawn", "dusk"]),
    (51, 53, "NEW-12", "玉门关·复用",     ["dusk", "frost", "dawn"]),
    (54, 58, "NEW-10", "贰师城之战",      ["dawn", "noon", "dusk", "dusk", "dawn"]),
    (59, 63, "NEW-11", "天马入长安",      ["dusk", "dawn", "noon", "dusk", "dawn"]),
    (64, 70, "NEW-12", "河西四郡玉门关",  ["noon", "dawn", "noon", "dusk",
                                          "dawn", "dusk", "dawn"]),
    (71, 83, "SC24",  "丝路千年·复用",    ["dawn", "noon", "dusk", "dawn", "noon",
                                          "dusk", "dawn", "noon", "dusk", "dawn",
                                          "noon", "dusk", "dawn"]),
    (84, 85, "SC01",  "朝堂·复用",        ["dusk", "dawn"]),
    (86, 87, "SC19",  "奏报竹简·复用",    ["fire", "night"]),
    (88, 88, "SC23",  "史记凿空·复用",    ["night"]),
    (89, 89, "SC02",  "西域未知·复用",    ["dusk"]),
    (90, 96, "SC24",  "丝路千年·复用",    ["dawn", "noon", "dusk", "dawn",
                                          "noon", "dusk", "dawn"]),
    # 章末回环：SC03「张骞应募」正是「郎官出列、跪拜玉阶」的画面，
    # 与第二章开场同一张底图，构成全片首尾呼应。
    (97, 100, "SC03", "张骞应募·复用",   ["dawn", "dawn", "noon", "dawn"]),
]

_settings = get_settings()
_PROF = _settings.get_profile()
MAX_ZOOM = _PROF.motion.max_zoom
_FORBID_SAME = _PROF.motion.forbid_consecutive_same

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
        dur = f.audio_duration_ms or 3000
        ep.shots[sid] = Shot(
            id=sid, frame_id=f.id, index=i, type="ken_burns",
            duration_ms=dur, audio_start_ms=0, audio_end_ms=dur,
            prompt=f"[{sid_img} {name}]",
            subtitle_text=f.text,
            image_path=str(_img_path(sid_img)).replace("\\", "/"),
            motion=motion,
        )
        f.shot_ids = [sid]
        if i < 4 or i % 10 == 0:
            rows.append((i, sid_img, name, f"{zoom:.2f}", light,
                         motion.type, f.text[:20]))

    save(ep)

    print("\n idx 底图     名称            机位  光线   运镜    文案")
    print(" " + "-" * 80)
    for i, sid, name, z, g, mt, txt in rows:
        print(f" {i:3d} {sid:<9s} {name:<14s} {z}  {g:<6s} {mt:<7s} {txt}")
    print(" " + "-" * 80)

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
