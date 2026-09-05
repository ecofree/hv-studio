# -*- coding: utf-8 -*-
"""
通用「剧本 → 分镜」声明式构建器（shot_builder）。

目标：把 hanwu_ch1_shots.py / hanwu_ch3_shots.py 这类「每章一份硬编码 SCENES +
逐帧手工对齐」的脚本，收敛成一份【声明式 scene_plan】+ 一个可复用引擎。

用法（CLI）：
    python -m app.services.shot_builder  build  <ep_id>  [--plan path] [--preview]
    python -m app.services.shot_builder  inspect <ep_id>

scene_plan 是一份 JSON，描述「哪些帧落到哪张底图、每张底图用什么光线循环」，
其余（机位 zoom 阶梯、运镜轮换、grade 循环、时长预扣）全部由引擎按 config 派生，
新片无需再写任何 shots 脚本。

字段约定见 build_episode_shots() 的 docstring。所有帧必须恰好落入一个 scene；
引擎会硬校验缺帧/越界/缺图/非法 grade，任一项不过即拒绝，绝不产出错位 shots。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

warnings = __import__("warnings")
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent.parent

from app.config.config import get_settings
from app.managers import save
from app.managers.episode import load
from app.models import MotionSpec, Shot
from app.services.script import frames_fingerprint

GRADE_KEYS = {"dawn", "noon", "dusk", "night", "fire", "frost"}
MOTION_POOL = ["push", "drift", "pull", "pan"]

_settings = get_settings()
_PROF = _settings.get_profile()
MAX_ZOOM = _PROF.motion.max_zoom                      # 1.15
_FORBID_SAME = _PROF.motion.forbid_consecutive_same   # True

RAW_ZOOM_LADDER = [1.12, 1.26, 1.40, 1.18, 1.32, 1.08, 1.22]
ZOOM_LADDER = [min(z, MAX_ZOOM) for z in RAW_ZOOM_LADDER]
INTENSITY_CYCLE = [0.07, 0.06, 0.08, 0.10, 0.05, 0.09, 0.10, 0.06]

HEAD_MS, TAIL_MS = 100, 150   # 需与 compose.HEAD_MS/TAIL_MS 一致


# ---------------------------------------------------------------------------
# scene_plan schema
# ---------------------------------------------------------------------------
# {
#   "episode": "hanwu-feature",
#   "images": {
#     "NEW-1":  {"path": "episodes/hanwu-feature/shots/NEW-1.png"},  # 显式
#     "SC01":   {"reuse_from": "hw-002"},                            # 复用他集
#   },
#   "scenes": [
#     {"frames": [0, 2],   "image": "NEW-1", "grades": ["frost","dawn","dusk"]},
#     {"frames": [3, 6],   "image": "NEW-2", "grades": ["dawn","dawn","noon","dusk"]},
#     ...
#   ]
# }
# frames 为闭区间 [start, end]（frame 下标，与 ep.frames 顺序一致）。


def _default_plan_path(ep_id: str) -> Path:
    return ROOT / "episodes" / ep_id / "scene_plan.json"


def _resolve_image(images_cfg: dict, sid: str) -> Path:
    """把 images[sid] 解析成绝对底图路径。支持 {'path':...} 或 {'reuse_from':...}。"""
    spec = images_cfg.get(sid)
    if not spec:
        raise KeyError(f"scene_plan.images 缺 {sid}")
    if spec.get("path"):
        p = Path(spec["path"])
        if not p.is_absolute():
            p = ROOT / p
        return p
    if spec.get("reuse_from"):
        return ROOT / "episodes" / spec["reuse_from"] / "shots" / f"{sid}.png"
    raise KeyError(f"image {sid} 需有 path 或 reuse_from")


def _check_frames_fingerprint(ep_id: str, plan: dict, texts: list[str],
                              strict: bool = False) -> dict:
    """把 scene_plan 锚定到当前的 ep.frames 上，不一致即拒。

    为什么必须校验（lsm-15min 事故复盘，2026-09-05）：
      scene_plan 的 frames 区间是【按下标】写的，本身不含任何文本信息。
      只有帧数恰好对得上，装配才是正确的；一旦 script.md 被重切，
      帧数从 324 变 392，64 个 scene 的区间会整体错位，而覆盖校验
      （每帧恰落一个 scene）依然「全通过」——它会静默产出一部错位的片子。

    strict: 无指纹（老集）是否也视为失败。默认 False（只告警），
      因为 laogongtuan / hanwu-* 的分镜都写在有指纹机制之前。
    """
    declared = plan.get("frames_fingerprint")
    if not declared:
        msg = (f"[shot_builder] scene_plan 缺 frames_fingerprint：无法证明这份分镜"
               f"锚定在当前 ep.frames（{len(texts)} 帧）上。\n"
               f"  建议：用 bin/build_episode_from_md.py 重建 scene_plan 以写入指纹，"
               f"或加 --strict-fingerprint 把缺指纹当失败。")
        if strict:
            raise SystemExit(msg.replace("建议", "strict 模式下视为失败；建议"))
        print("  [!] " + msg)
        return {}

    actual = frames_fingerprint(texts)
    if declared.get("count") != actual["count"] or \
            declared.get("sha1") != actual["sha1"]:
        raise SystemExit(
            f"[shot_builder] scene_plan 与 ep.frames 不同源，拒绝装配！\n"
            f"  分镜声明: {declared.get('count')} 帧 sha1={declared.get('sha1')} "
            f"首「{declared.get('head', '')}」尾「{declared.get('tail', '')}」\n"
            f"  实际frames: {actual['count']} 帧 sha1={actual['sha1']} "
            f"首「{actual['head']}」尾「{actual['tail']}」\n"
            f"  偏差 {actual['count'] - (declared.get('count') or 0):+d} 帧 —— "
            f"继续装配会让所有 scene 的帧区间错位。\n"
            f"  修复二选一：① 把 script.md 还原到写分镜时的版本后重跑 stage script；\n"
            f"              ② 重跑 bin/build_episode_from_md.py 重建 scene_plan"
            f"（--merge-short 开关必须与该集建档时一致）。")
    return actual


def build_episode_shots(ep_id: str, plan_path: Path | None = None,
                        preview_rows: int | None = 12, persist: bool = True,
                        strict_fingerprint: bool = False):
    """按 scene_plan 为 ep 重建 shots。返回 (ep, stats)。

    - 校验：scene 覆盖全部帧且不重叠、无空洞；底图存在；grade 合法；
      帧指纹与 ep.frames 同源（见 _check_frames_fingerprint）。
    - 派生：同底图首帧 zoom=1.0，后续走 ZOOM_LADDER；grade 按 scene.grades 循环；
      运镜 MOTION_POOL 轮换（禁连续同型）；时长预扣呼吸段（隔离组 +250ms）。
    """
    ep = load(ep_id)
    frames = ep.frames
    if not frames:
        raise SystemExit(f"[shot_builder] episode {ep_id} 无 frames（先跑 script 切分）")
    plan_path = plan_path or _default_plan_path(ep_id)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    # ---- 帧指纹锚定：必须在做覆盖校验之前，错位时给出可定位的报错 ----
    fp = _check_frames_fingerprint(ep_id, plan, [f.text for f in frames],
                                   strict=strict_fingerprint)

    images_cfg = plan.get("images", {})
    scenes = plan.get("scenes", [])

    # ---- 帧覆盖校验：0..N-1 每帧恰落一个 scene ----
    n = len(frames)
    covered: list[str | None] = [None] * n
    for si, sc in enumerate(scenes):
        a, b = sc["frames"]
        if not (0 <= a <= b < n):
            raise SystemExit(f"[shot_builder] scene[{si}] 帧区间 {a}-{b} 越界 (共{n}帧)")
        for i in range(a, b + 1):
            if covered[i] is not None:
                raise SystemExit(f"[shot_builder] 帧 {i} 被 scene[{covered[i]}] 与 "
                                 f"scene[{si}] 重复覆盖")
            covered[i] = si
    gaps = [i for i, c in enumerate(covered) if c is None]
    if gaps:
        raise SystemExit(f"[shot_builder] 帧未落任何 scene：{gaps[:20]}")

    # ---- 底图 + grade 前置校验 ----
    image_paths: dict[str, Path] = {}
    used_images: set[str] = set()
    for si, sc in enumerate(scenes):
        sid = sc["image"]
        used_images.add(sid)
        if sid not in image_paths:
            image_paths[sid] = _resolve_image(images_cfg, sid)
        for g in sc.get("grades", ["dawn"]):
            if g not in GRADE_KEYS:
                raise SystemExit(f"[shot_builder] scene[{si}] 非法 grade {g} "
                                 f"(合法: {sorted(GRADE_KEYS)})")
    missing = sorted(s for s, p in image_paths.items() if not p.exists())
    if missing:
        raise SystemExit(f"[shot_builder] 缺底图：{missing}")

    # ---- 构建 ----
    last_motion: str | None = None

    def _next_motion(desired: str) -> str:
        nonlocal last_motion
        if _FORBID_SAME and desired == last_motion:
            for alt in MOTION_POOL:
                if alt != last_motion:
                    desired = alt
                    break
        last_motion = desired
        return desired

    def _build_motion(zoom, mtype, intensity, grade) -> MotionSpec:
        if mtype == "push":
            sxy, exy, sz, ez = (0.5, 0.5), (0.5, 0.5), zoom, zoom + intensity
        elif mtype == "pull":
            sxy, exy, sz, ez = (0.5, 0.5), (0.5, 0.5), zoom, max(1.0, zoom - intensity)
        elif mtype == "drift":
            sxy, exy, sz, ez = (0.46, 0.5), (0.54, 0.5), zoom, zoom
        else:  # pan
            sxy, exy, sz, ez = (0.44, 0.48), (0.56, 0.52), zoom, zoom
        return MotionSpec(type=mtype, intensity=round(intensity, 3),
                          start_xy=sxy, end_xy=exy,
                          start_zoom=round(sz, 3), end_zoom=round(ez, 3),
                          grade=grade)

    ep.shots = {}
    rows = []
    no_audio: list[str] = []      # 缺 audio_duration_ms 的帧（B5：不再静默按 3s 兜底）
    for i, f in enumerate(frames):
        si = covered[i]
        sc = scenes[si]
        sid = sc["image"]
        grades = sc.get("grades", ["dawn"])
        j = i - sc["frames"][0]                # 该 scene 内第 j 帧
        zoom = 1.0 if j == 0 else ZOOM_LADDER[(j - 1) % len(ZOOM_LADDER)]
        grade = grades[j % len(grades)]
        mt = _next_motion(MOTION_POOL[i % len(MOTION_POOL)])
        motion = _build_motion(zoom, mt, INTENSITY_CYCLE[i % len(INTENSITY_CYCLE)], grade)

        shot_id = f"shot-{i:03d}"
        # B5：此前 `f.audio_duration_ms or 3000` 静默捏造时长 —— 装配出的 shots
        # 元数据本身是假的，而 compose 之前的任何消费方（预演、时长预估、
        # 时序断言的早期形态）都会被它误导，且全程无一行提示。
        # 兜底值仍保留（否则整链直接崩），但必须留下明确痕迹。
        a_dur = f.audio_duration_ms
        if not a_dur:
            no_audio.append(f.id)
            a_dur = 3000
        dur = a_dur + HEAD_MS + TAIL_MS       # 隔离组 compose 会重算为 head+a+tail
        ep.shots[shot_id] = Shot(
            id=shot_id, frame_id=f.id, index=i, type="ken_burns",
            duration_ms=dur, audio_start_ms=HEAD_MS, audio_end_ms=HEAD_MS + a_dur,
            prompt=f"[{sid}]", subtitle_text=f.text,
            image_path=str(image_paths[sid]).replace("\\", "/"),
            motion=motion,
        )
        f.shot_ids = [shot_id]
        if preview_rows and (i < 4 or i % 8 == 0):
            rows.append((i, sid, f"{zoom:.2f}", grade, mt, f.text[:18]))

    if no_audio:
        print(f"[shots][告警] {len(no_audio)}/{n} 帧缺 audio_duration_ms，"
              f"已按 3000ms 兜底装配：{no_audio[:10]}"
              f"{' …' if len(no_audio) > 10 else ''}")
        print("[shots][告警] 这些镜头的 duration_ms 是假的；请先跑 "
              "`hv episode produce <ep> --stage tts` 补音频后重新装配。")

    if persist:
        save(ep)

    stats = {
        "episode": ep_id, "frames": n, "shots": len(ep.shots),
        "base_images": len(used_images),
        "reuse_ratio": round(n / len(used_images), 2),
        "fingerprint": fp,   # {} = 该分镜无指纹（老集），未做锚定校验
        "no_audio_frames": no_audio,   # B5：缺音频的帧（消费方可据此拒绝继续）
        "images_per_base": {k: sum(1 for i in range(n)
                                   if scenes[covered[i]]["image"] == k)
                            for k in sorted(used_images)},
    }
    if preview_rows:
        print("\n idx 底图   机位  光线   运镜  文案")
        print(" " + "-" * 72)
        for i, sid, z, g, mt, txt in rows:
            print(f" {i:3d} {sid:<8s} {z}  {g:<6s} {mt:<7s} {txt}")
        print(" " + "-" * 72)
        print(f"\n共 {len(ep.shots)} shot / {len(used_images)} 张底图，"
              f"归并比 {stats['reuse_ratio']}:1")
        print("每图复用：" + "  ".join(f"{k}:{v}" for k, v in sorted(stats['images_per_base'].items())))
        tot = sum(f.audio_duration_ms or 0 for f in frames)
        print(f"旁白 {tot/1000:.1f}s = {tot/60000:.2f}min")
    return ep, stats


def inspect_episode(ep_id: str):
    from app.managers.episode import validate_schema
    ep = load(ep_id)
    problems = validate_schema(ep)
    print(f"[inspect] {ep_id}: frames={len(ep.frames)} shots={len(ep.shots)}")
    if problems:
        print("  schema 问题：")
        for p in problems:
            print("   -", p)
    else:
        print("  schema OK（无 dangling shot 引用）")
    return problems


def _cli():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, ep_id = sys.argv[1], sys.argv[2]
    if cmd == "build":
        plan = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[3] == "--plan" else None
        strict = "--strict-fingerprint" in sys.argv
        _, st = build_episode_shots(ep_id, plan_path=plan,
                                    strict_fingerprint=strict)
        print(f"\n[build] {ep_id} → {st['shots']} shots OK")
        if st["fingerprint"]:
            print(f"  帧指纹 {st['fingerprint']['count']}帧 "
                  f"sha1={st['fingerprint']['sha1']} [已锚定]")
        else:
            print("  [!] scene_plan 无帧指纹，未做锚定校验")
    elif cmd == "inspect":
        inspect_episode(ep_id)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
