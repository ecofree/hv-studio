"""
统一真实编排器（P0-3 修复：状态只由产物推进，API/CLI 共用同一入口）。

背景（审计 P0-3）：此前 `app/controllers/v1/episode.py` 的 /run 直接调 `run_stage(ep)`
且不传真实执行函数，状态机仍逐级迁移到 done —— 没有任何音频/图/视频也被算"完成"，
状态不可作完成度或计费依据。

本模块把【真实的生产 stage】绑成可被 `run_stage(ep, stage_fn=...)` 驱动的回调，
并让状态只在对应产物校验成功后推进。CLI `hv produce` 与 API `POST .../produce` 共用它。

真实 stage 链（依赖序，仅跑适用项）：
  script   ep.script_path(或 script_md) → split 重建 frames     产物: frames 非空
  tts      frames → voice.synthesize → audio_path 落盘          产物: 每 frame 有 wav
  compose  需已有 shots → compose_episode → out/{id}_m1.mp4      产物: mp4 可解码
  verify   产物驱动的真值校验（mp4 存在 + 时序 + 解码）         返回真值

注意：shot 装配（句子→场景/运镜）是每部作品的人工创意步骤，不在通用自动链内；
对已有 shots 的 episode，本链可一路 script→tts→compose→verify 走通。
"""
from __future__ import annotations

from pathlib import Path

from app.models import Episode

_EPISODES = Path(__file__).resolve().parent.parent / "episodes"


def _episode_dir(ep_id: str) -> Path:
    """episodes/{ep_id} —— 单集资产根目录。"""
    return _EPISODES / ep_id


def _out_dir(ep_id: str) -> Path:
    """episodes/{ep_id}/out —— 与 compose.BASE/ep_id/out 同口径（勿用 json 父目录）。"""
    return _EPISODES / ep_id / "out"


# ---------------------------------------------------------------------------
# 真实 stage 回调（各自在成功后才允许状态迁移；失败抛错由 run_stage 置 FAILED）
# ---------------------------------------------------------------------------
def stage_script(ep: Episode) -> None:
    """重建 frames（script.md 或 ep.script_path）。产物校验：frames 非空。"""
    from app.services.script import split_script, rebuild_frames

    md = _load_script_md(ep)
    if not md.strip():
        raise RuntimeError(f"episode {ep.id} 无脚本内容（script_path 为空或不存在）")
    # 优先 rebuild_frames（保留既有音频/shots 资产），否则 split_script
    if ep.frames and ep.shots:
        rebuild_frames(ep, md)
    else:
        ep.frames = split_script(ep, md)
    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 脚本切分后 frames 为空")
    print(f"  [orchestrator] script: frames={len(ep.frames)}")


def stage_tts(ep: Episode) -> None:
    """frames → CosyVoice 合成落盘。产物校验：每 frame 音频文件存在。"""
    from pathlib import Path
    from app.managers.episode import episode_path
    from app.services.voice import synthesize

    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 无 frames，先跑 script stage")
    audio_dir = _episode_dir(ep.id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    synthesize(ep.frames, engine="cosyvoice", out_dir=audio_dir)
    missing = [f.id for f in ep.frames
               if not (f.audio_path and Path(f.audio_path).exists())]
    if missing:
        raise RuntimeError(f"episode {ep.id} 仍有 {len(missing)} 段缺音频: {missing[:10]}")
    total = sum(f.audio_duration_ms or 0 for f in ep.frames)
    print(f"  [orchestrator] tts: {len(ep.frames)}/{len(ep.frames)} 落盘，"
          f"旁白 {total/1000:.1f}s")


def _read_image_prompts(ep_id: str) -> dict:
    """读 episodes/{id}/image_prompts.json；缺文件返回 {}。"""
    p = _episode_dir(ep_id) / "image_prompts.json"
    if not p.exists():
        return {}
    import json
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"image_prompts.json 解析失败 {p}: {e}")


def build_image_jobs(ep_id: str) -> list["ImageJob"]:
    """从 image_prompts.json 构建生图任务（prompt 单源）。

    唯一数据源 = episodes/{id}/image_prompts.json：
      { "LGT01": {"en": "...", ...}, ... }
    每个键 → ImageJob(落盘 episodes/{id}/shots/{KEY}.png)。
    prompt = en + style_suffix + negative（扁平 ImageConfig 顶层字段）。
    幂等由 generate_many 内部按「已存在 >50KB」处理，本函数只建任务不生成。
    stage_images 与 batch_generate_lgt 共用本函数 → 消除 prompt 拼接漂移。
    """
    from app.config.config import get_settings
    from app.models import ImageGenRequest
    from app.services.image import ImageJob

    spec = _read_image_prompts(ep_id)
    if not spec:
        return []
    im = get_settings().get_profile().image
    # ImageConfig 是扁平结构：engine/size/quality/style_suffix/negative_clause
    # 全在顶层，无 primary（batch_generate_lgt 老脚本同样用顶层字段）。
    model = getattr(im, "engine", None) or "gpt-image-2"
    size = getattr(im, "size", "1080x1920") or "1080x1920"
    quality = getattr(im, "quality", "low") or "low"
    suffix = getattr(im, "style_suffix", "") or ""
    neg = getattr(im, "negative_clause", "") or ""
    shots_d = _episode_dir(ep_id) / "shots"
    shots_d.mkdir(parents=True, exist_ok=True)

    keys = sorted(k for k in spec if not k.startswith("_"))
    jobs: list[ImageJob] = []
    for key in keys:
        entry = spec[key]
        en = entry.get("en") if isinstance(entry, dict) else str(entry)
        if not en:
            continue
        prompt = f"{en}, {suffix}. {neg}".strip(", .") + "."
        jobs.append(ImageJob(
            id=key,
            req=ImageGenRequest(
                model=model, prompt=prompt,
                size=size, quality=quality, n=1,
            ),
            out_path=shots_d / f"{key}.png",
        ))
    return jobs


def stage_images(ep: Episode) -> None:
    """按 image_prompts.json 批量生图（三路并行 generate_many）。

    数据源唯一入口 = episodes/{id}/image_prompts.json（见 build_image_jobs）。
    已存在 >50KB 的图幂等跳过（不占渠道）。任一失败即 raise（stage 语义）。
    """
    jobs = build_image_jobs(ep.id)
    if not jobs:
        print(f"  [orchestrator] image: 无 {ep.id}/image_prompts.json 或全空，跳过生图")
        return
    from app.services.image import generate_many
    print(f"  [orchestrator] image: {len(jobs)} 张待生成/跳过（三路并行）")
    res = generate_many(jobs)
    failed = [k for k, v in res.items() if not k.endswith("__error") and v is None]
    if failed:
        raise RuntimeError(
            f"episode {ep.id} 有 {len(failed)} 张生图失败: "
            f"{[(k, res.get(k+'__error')) for k in failed[:5]]}")
    print(f"  [orchestrator] image: {len(jobs)} 张全部就绪")


def stage_assets(ep: Episode) -> None:
    """生图 ∥ TTS 并行（两家独立厂商/配额，可真并行）。两侧都结束才返回。

    - 线程 A：stage_tts（DashScope）
    - 线程 B：stage_images（subrouter 三渠道）
    一侧失败不丢另一侧已落盘产物；save(ep) 由外层在返回后统一写一次。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    def _run_t(stage_fn, name):
        def inner():
            try:
                stage_fn(ep)
            except Exception as e:
                return (name, e)
            return (name, None)
        return inner

    # 若都无活可干（无 frames 又无 prompts/图全在），直接返回不空转
    has_frames = bool(ep.frames)
    spec = _read_image_prompts(ep.id)
    has_prompts = bool(spec)

    tasks = []
    if has_frames:
        tasks.append(_run_t(stage_tts, "tts"))
    if has_prompts:
        tasks.append(_run_t(stage_images, "images"))
    if not tasks:
        print("  [orchestrator] assets: 无 TTS 帧且无生图 prompts，跳过")
        return

    errors = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for name, err in ex.map(lambda f: f(), tasks):
            if err is not None:
                errors[name] = err
    if errors:
        msg = "; ".join(f"{n}: {e}" for n, e in errors.items())
        raise RuntimeError(f"assets 阶段部分失败: {msg}")


def stage_compose(ep: Episode) -> None:
    """整集合成成片。产物校验：mp4 生成 + 可解码。"""
    from app.services.compose import compose_episode
    if not ep.shots:
        raise RuntimeError(f"episode {ep.id} 无 shots（shot 装配是人工创意步骤），"
                           f"无法自动合成。")
    final = compose_episode(ep.id)
    if not final.exists() or final.stat().st_size < 1024:
        raise RuntimeError(f"episode {ep.id} 合成产物缺失或过小: {final}")
    print(f"  [orchestrator] compose: {final.name} 生成")


def stage_verify(ep: Episode) -> dict:
    """产物驱动的真值校验：mp4 存在 + ffprobe 帧数对齐时序 + decode 零错。"""
    import subprocess
    from app.services.timing_assert import assert_timing_or_raise

    final = _out_dir(ep.id) / f"{ep.id}_m1.mp4"
    if not final.exists():
        raise RuntimeError(f"episode {ep.id} 无成片 {final}（未合成或已删）")

    # 用 count_packets（容器层数包，不解码）而非 count_frames（整片解码）：
    # H.264 每包≈每帧，20min 长片用 count_frames 会整片解码数百MB，
    # 在低 pagefile 机器上被 SIGTERM/超时，verify 误判损坏。count_packets 秒回。
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "json", str(final)],
        capture_output=True, text=True, timeout=90)
    import json
    nf = None
    try:
        nf = int(json.loads(probe.stdout)["streams"][0]["nb_read_packets"])
    except Exception:
        nf = None
    if nf is None:
        raise RuntimeError(f"episode {ep.id} 成片无法读取帧数（可能损坏）: {final}")
    assert_timing_or_raise(ep, video_total_frames=nf)

    # decode-to-null 零错
    dec = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"],
        capture_output=True, text=True)
    if dec.returncode != 0:
        raise RuntimeError(f"episode {ep.id} 成片解码错误: {dec.stderr[-500:]}")

    size = final.stat().st_size
    print(f"  [orchestrator] verify: {final.name} {size/1024/1024:.1f}MB "
          f"{nf}帧 解码0错 时序对齐 ✓")
    return {"ok": True, "frames": nf, "bytes": size}


def _load_script_md(ep: Episode) -> str:
    """取脚本文本：优先 ep.script_path 指向的文件，其次 notes 兜底。"""
    if ep.script_path:
        p = Path(ep.script_path)
        if p.exists():
            return p.read_text(encoding="utf-8")
    # 约定默认路径 episodes/{id}/script.md
    cand = _episode_dir(ep.id) / "script.md"
    if cand.exists():
        ep.script_path = str(cand)
        return cand.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# 编排入口（CLI 与 API 共用）
# ---------------------------------------------------------------------------
STAGE_FN = {
    "script": stage_script,
    "tts": stage_tts,
    "images": stage_images,
    "assets": stage_assets,
    "shots": lambda ep: _stage_shots_skip(ep),
    "compose": stage_compose,
    "verify": stage_verify,
}


def _stage_shots_skip(ep: Episode) -> None:
    """shots 装配是人工创意步骤（需 scene_plan.json），自动链里仅确认就绪。

    若已有 shots 且 frames 引用对齐则通过；否则说明尚未人工装配，
    跳过 compose（compose 会因无 shots 明确报错而非静默出片）。
    """
    n_shots = len(ep.shots)
    if n_shots:
        print(f"  [orchestrator] shots: 已装配 {n_shots} 镜，继续")
    else:
        print("  [orchestrator] shots: 无 scene_plan/装配，跳过（分镜需人工创意）")


def produce(
    ep_id: str,
    stages: list[str] | None = None,
    script_md: str | None = None,
) -> dict:
    """
    跑真实生产链（不滥用 rigid 状态枚举空推）；状态由产物真值导出。
      stages: None=全跑适用的；或 ['script','tts','compose','verify'] 子集。
    返回 {ep_id, status, stages_done, verify}。
    """
    from app.managers import load, save
    from app.managers.episode import validate_schema
    from app.models import EpisodeStatus

    ep = load(ep_id)

    # schema 硬阻断（P0-4 修复）：任何生产前，schema 不干净即拒绝，绝不到付费生图。
    # allow_empty：空集若即将切脚本（有 script.md 或 notes），放行让 stage_script
    # 成为第一步（否则「官方入口启动官方第一步」被鸡生蛋卡死）。
    if script_md:
        ep.notes = script_md  # 临时注入脚本；若无 script_path 用 notes 兜底
    has_script = bool((_load_script_md(ep) or (ep.notes or "").strip()))
    _can_start_from_empty = (not ep.frames and not ep.shots) and has_script
    problems = validate_schema(ep, allow_empty=_can_start_from_empty)
    if problems:
        raise RuntimeError(
            f"episode {ep.id} schema 不合法，拒绝生产: {problems}")

    # 依赖序（script → assets[=tts∥images] → shots → compose → verify）
    # assets 取代旧的独立 tts 位置：内部并行跑配音与生图，仅跑适用侧。
    order = ["script", "assets", "shots", "compose", "verify"]
    if stages is None:
        runnable = []
        has_assets_work = bool(ep.frames) or bool(_read_image_prompts(ep.id))
        for st in order:
            if st == "script":
                if not has_script:
                    print("  [orchestrator] 无脚本，跳过 script")
                    continue
                if ep.frames and not ep.notes:
                    # 已有 frames（先前切过脚本）：默认不重切，避免 splitter 规则一变
                    # 就重分帧 + 重 TTS(钱) + 重合成(小时)。需显式 --stage script 强制。
                    print("  [orchestrator] 已有 frames，默认跳过 script"
                          "（如需重切请显式 --stage script）")
                    continue
            if st == "assets" and not has_assets_work:
                print("  [orchestrator] 无 TTS 帧且无 prompts，跳过 assets")
                continue
            if st == "shots" and not ep.shots:
                # shots 装配是人工创意（需 scene_plan）；无则走跳过桩，让 compose 因无
                # shots 明确报错而非静默。保留它在链上以便日志清晰。
                pass
            if st == "compose" and not ep.shots:
                print("  [orchestrator] 无 shots，跳过 compose（shot 装配需人工创意）")
                continue
            if st == "verify" and not (_out_dir(ep.id) / f"{ep.id}_m1.mp4").exists():
                # 无成片不跑 verify（会误报）。末尾产物驱动的状态块会按需补 verify。
                continue
            runnable.append(st)
        stages = runnable

    done = []
    for st in stages:
        if st not in STAGE_FN:
            raise RuntimeError(f"未知 stage: {st}（可选 {list(STAGE_FN)}）")
        STAGE_FN[st](ep)            # 真实干活；失败即抛 → 不标记完成
        done.append(st)
        save(ep)                    # 每 stage 落盘，断点续跑安全
        # 关键：compose_episode 内部按真实音频【重载重算】时间轴并落盘，
        # 但它操作的是自载副本，不回写本函数持有的 ep。若此处不复载，
        # 后续 verify 用的是 shot_builder 存的旧 duration（每镜含独立 +250ms
        # pad），与成片实际帧数对不上 → #5 误报「视频帧数 ≠ Σ镜头帧」。
        # 复载让内存态紧跟磁盘真值（各组共享 pad 的重算口径），verify 正确。
        ep = load(ep_id)

    # —— 产物驱动的真值状态（不是盲迁移）——
    verify = {}
    final = _out_dir(ep_id) / f"{ep_id}_m1.mp4"
    has_mp4 = final.exists() and final.stat().st_size > 1024
    has_frames = bool(ep.frames)
    has_shots = bool(ep.shots)
    verify_already_ran = "verify" in done
    if has_frames and has_shots and has_mp4:
        # 有 shots+frames+成片：verify（若尚未在 stage 循环跑过）；通过才算 COMPOSED
        try:
            if verify_already_ran:
                # stage 循环已 verify 成功（未抛），这里仅复核产物仍在
                verify = {"ok": True, "final_mp4": str(final)}
            else:
                verify = stage_verify(ep)
            ep.status = EpisodeStatus.COMPOSED
        except Exception as e:
            verify = {"ok": False, "error": str(e)}
            ep.status = EpisodeStatus.FAILED
    elif has_frames and not has_shots:
        ep.status = EpisodeStatus.SCRIPTED
    elif has_mp4 and not has_frames:
        ep.status = EpisodeStatus.COMPOSED
    else:
        ep.status = EpisodeStatus.DRAFT
    save(ep)

    return {"ep_id": ep_id, "status": ep.status.name, "stages_done": done,
            "verify": verify, "final_mp4": str(final) if has_mp4 else None}
