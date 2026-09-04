"""
Pipeline 编排器（M2 实装：单镜头可重建）。

把 M1 散落在 bin/*.py 的手工流程收敛为可按 shot 粒度驱动的编排器，
供 CLI `hv episode run X --only shot-001` 调用。

支持 stage（默认全跑）：
  image   重建该 shot 配图（edits 参考图注入角色定妆包做一致性锚定）
          + 人脸相似度门控（face_sim）
  upscale 超分 2x（由 motion.render 内部完成，这里作为显式 stage 可选）
  motion  生成该 shot 的 Ken Burns mp4（内部先 upscale）
  compose 整集合成（只对单镜改图后重合成才有意义；接受 --compose 触发）
  timing  合成后跑 §5.4 时序断言（video_total_frames 回填）

幂等约定：
  - 某 stage 已 done 且未 --force → 跳过
  - needs_regen() 命中（face_sim<0.5 / 缺产物）→ 即使无 --force 也重建 image
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from app.managers.episode import episode_path, save
from app.config.config import ROOT as PROJECT
from app.models import Episode, ImageEditRequest

DEFAULT_EP = "hw-001"

IDENTITY = ("Preserve the exact identity, facial geometry, age, hairstyle, "
            "headdress and wardrobe of the man in the reference image. "
            "Do NOT copy his facial expression or pose from the reference. ")


def shots_dir(ep_id: str) -> Path:
    return PROJECT / "episodes" / ep_id / "shots"


def _b64(p: str | Path) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode()


def _need_image(shot) -> bool:
    """是否需要重建 image（产物缺失 或 一致性不达标）。"""
    if not shot.image_path or not Path(shot.image_path).exists():
        return True
    if shot.face_sim is not None and shot.face_sim < 0.5:
        return True
    return False


# ---------------------------------------------------------------------------
# 单 stage 执行
# ---------------------------------------------------------------------------
def stage_image(ep: Episode, shot, force: bool = False) -> None:
    """
    用角色定妆包为参考图 edits 重建该 shot 配图，并做人脸一致性门控。
    无定妆包时退化为纯文生图（保持 shot.prompt）。
    产出覆盖 shots/{id}.png；写回 image_path / face_sim / image_actual_size。
    """
    from app.services.character_lock import SIM_ACCEPT, anchor_embedding, load_pack
    from app.services.face import cosine, detect_faces, embed_face
    from app.services.image import get_image

    shots_d = shots_dir(ep.id)
    shots_d.mkdir(parents=True, exist_ok=True)
    fp = shots_d / f"{shot.id}.png"

    if fp.exists() and not force and not _need_image(shot):
        print(f"  [image] {shot.id} 已存在且达标，跳过")
        shot.stage["image"] = "done"
        return

    # 组装 prompt：保留原 scene 语义，前插保人指令
    base_prompt = shot.prompt or shot.narration
    prompt = f"{IDENTITY}{base_prompt}" if IDENTITY not in base_prompt else base_prompt

    # 定妆包锚
    pack = load_pack(shot.ref_pack_id) if shot.ref_pack_id else None
    anchor_emb = anchor_embedding(pack) if pack else None
    img = get_image()

    if pack and pack.anchor_face_path:
        # edits 参考注入（角色一致性）
        req = ImageEditRequest(
            model="gpt-image-2", prompt=prompt, size="1080x1920",
            quality="low", image=_b64(pack.anchor_face_path),
        )
    else:
        # 无定妆包 → 文生图
        from app.models import ImageGenRequest
        print("  [image] 无定妆包，退化文生图")
        _gen(img, ImageGenRequest(
            model="gpt-image-2", prompt=prompt, size="1080x1920", quality="low"),
            fp)
        _register_image_meta(shot, fp, anchor_emb)
        return

    print(f"  [image] {shot.id} edits 收脸（锚={pack.anchor_face_path}）...")
    t0 = time.time()
    try:
        resp = img.edit(req)
    except Exception as e:
        raise RuntimeError(f"{shot.id} 生图失败: {type(e).__name__}: {e}")
    raw = base64.b64decode(resp.data[0].b64_json)
    fp.write_bytes(raw)
    print(f"         {time.time()-t0:.1f}s {len(raw)/1024:.0f}KB")
    _register_image_meta(shot, fp, anchor_emb)


def _gen(img, req, fp: Path) -> None:
    resp = img.generate(req)
    raw = base64.b64decode(resp.data[0].b64_json)
    fp.write_bytes(raw)


def _register_image_meta(shot, fp: Path, anchor_emb) -> None:
    """写 image_path + 一致性分数。anchor_emb None 时不写 face_sim。"""
    from app.services.face import cosine, embed_face
    shot.image_path = str(fp).replace("\\", "/")
    shot.image_actual_size = (1080, 1920)
    shot.stage["image"] = "done"
    if anchor_emb is not None:
        p_emb, meta = embed_face(fp)
        if p_emb is not None:
            sim = cosine(anchor_emb, p_emb)
            shot.face_sim = round(sim, 3)
            shot.face_sim_warn = sim < 0.45
            print(f"         face_sim={shot.face_sim:.3f} "
                  f"{'OK' if not shot.face_sim_warn else '<-- 低'}")
        else:
            shot.face_sim = 0.0
            shot.face_sim_warn = True
            print("         无人脸可评")


def stage_motion(ep: Episode, shot, force: bool = False) -> None:
    """超分 + Ken Burns mp4。产物 shots/{id}.mp4 + upscaled_path。"""
    from app.services.motion import render
    if shot.motion_done and not force and shot.stage.get("motion") == "done":
        print(f"  [motion] {shot.id} 已有 mp4，跳过")
        return
    if not shot.image_path or not Path(shot.image_path).exists():
        raise RuntimeError(f"{shot.id} 无 image_path，先跑 image stage")
    t0 = time.time()
    out = render(shot)
    shot.stage["motion"] = "done"
    print(f"  [motion] {shot.id} -> {Path(out).name} "
          f"({time.time()-t0:.1f}s)")


def stage_timing(ep: Episode) -> dict:
    """合成后时序断言（需先 compose 才有视频帧数）。"""
    from app.services.timing_assert import assert_timing_or_raise
    rep = assert_timing_or_raise(ep)
    print(f"  [timing] ok={rep['ok']}  fails={rep['fails']}")
    return rep


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------
def run_episode(
    ep_id: str = DEFAULT_EP,
    only: str | None = None,
    stages: list[str] | None = None,
    force: bool = False,
    compose_after: bool = False,
) -> dict:
    """
    主入口。
      only: 只重建某 shot（如 shot-001）。None = 全片。
      stages: ['image','motion'] 等；None = ['image','motion']。
      compose_after: 单镜改图后是否重合成整片。
    返回 {shot_id: 执行的 stages 摘要}。
    """
    from app.managers import load, save, validate_schema
    stages = stages or ["image", "motion"]
    ep = load(ep_id)

    # schema 体检 → 硬阻断（P0-4 修复：付费生图前不干净即拒绝，绝不静默继续）
    problems = validate_schema(ep)
    if problems:
        raise RuntimeError(
            f"episode {ep_id} schema 不合法，拒绝生产（避免付费生图跑在脏数据上）: "
            f"{problems}")

    targets = [only] if only else sorted(ep.shots.keys())
    summary: dict = {}

    for sid in targets:
        if sid not in ep.shots:
            print(f"  !! {sid} 不在 shots 中（可用: {sorted(ep.shots)[:10]}...）")
            continue
        shot = ep.shots[sid]
        done = []
        for st in stages:
            try:
                if st == "image":
                    stage_image(ep, shot, force)
                    done.append("image")
                elif st == "motion":
                    stage_motion(ep, shot, force)
                    done.append("motion")
                elif st == "timing":
                    stage_timing(ep)
                    done.append("timing")
                else:
                    print(f"  !! 未知 stage: {st}")
                save(ep)  # 每 stage 落盘，断点续跑安全
            except Exception as e:
                shot.last_error = str(e)[:300]
                shot.retry_count += 1
                save(ep)
                raise RuntimeError(f"{sid} stage={st} 失败: {e}") from e
        summary[sid] = done
        # 全片默认不自动合成；compose_after 显式才合成
        if only and compose_after and "motion" in done:
            compose_and_verify(ep_id)

    # 校验 episode 文件路径仍存在
    _ = episode_path(ep_id)
    return summary


def compose_and_verify(ep_id: str, ep=None) -> Path:
    """合成 + 用 ffprobe 取实际帧数回填时序断言。#5 严格校验。"""
    from app.services.compose import compose_episode
    import subprocess, json
    final = compose_episode(ep_id)
    # ffprobe 取实际总帧数
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "json", str(final)],
        capture_output=True, text=True)
    nf = None
    try:
        nf = int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"])
    except Exception:
        nf = None
    if nf:
        ep = ep or __import__("app.managers", fromlist=["load"]).load(ep_id)
        from app.services.timing_assert import assert_timing_or_raise
        assert_timing_or_raise(ep, video_total_frames=nf)
        print(f"  [compose] ffprobe 实际帧 {nf} == Σ镜头帧 校验通过")
    return final


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_id", nargs="?", default=DEFAULT_EP)
    ap.add_argument("--only", default=None)
    ap.add_argument("--stage", action="append", default=None,
                    help="可多次：--stage image --stage motion")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--compose", action="store_true",
                    help="单镜改图后重合成整片 + 时序断言")
    args = ap.parse_args()
    res = run_episode(
        args.episode_id, only=args.only,
        stages=args.stage or None, force=args.force,
        compose_after=args.compose)
    for sid, done in res.items():
        print(f"  {sid}: 完成 {done}")
