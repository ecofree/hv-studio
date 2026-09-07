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

import os
from pathlib import Path

from app.config.config import env_get
from app.models import Episode

from app.paths import episode_dir as _paths_episode_dir  # P1-6：调用期求值，可重定向


def _episode_dir(ep_id: str) -> Path:
    """episodes/{ep_id} —— 单集资产根目录。"""
    return _paths_episode_dir(ep_id)


def _out_dir(ep_id: str) -> Path:
    """episodes/{ep_id}/out —— 与 compose 的 out 同口径（勿用 json 父目录）。"""
    return _paths_episode_dir(ep_id) / "out"


# ---------------------------------------------------------------------------
# 真实 stage 回调（各自在成功后才允许状态迁移；失败抛错由 run_stage 置 FAILED）
# ---------------------------------------------------------------------------
def stage_script(ep: Episode, md: str | None = None,
                 merge_short: bool = False) -> None:
    """
    merge_short: 是否合并过短片段（≈<2.5s）。**逐集可选，默认 False**——
      laogongtuan 等既有多集是按未合并切分并已配好音/分镜的，打开会让帧数
      变化、scene_plan 与音频全错位。新集建档时开，并写入 fingerprint。

      ⚠️ 2026-09-06：此前这个开关**只能**通过 bin/build_episode_from_md.py
      --merge-short 在双栏分镜路径上打开；纯 script.md 建档走 produce 时
      split_script 被硬编码成 merge_short=False，CLI 无从开启 —— 于是新剧本
      ⚠️ 2026-09-06：此前这个开关**只能**通过 bin/build_episode_from_md.py
      --merge-short 在双栏分镜路径上打开；纯 script.md 建档走 produce 时
      split_script 被硬编码成 merge_short=False，CLI 无从开启 —— 于是新剧本
      切出的 6 个短帧（最短 '境。' 2 字）无法合并。现由 produce(merge_short=)
      一路传下来，CLI 有 --merge-short。

    md: 显式脚本正文（API POST /produce 传来）。None 时走 _load_script_md(ep)。
    D4：以前靠 produce 把正文塞进 ep.notes 再在这里读回，而 save(ep) 会把整篇
    剧本写进 episode.json。现在显式传参，notes 只留给人写备注。
    """
    from app.services.script import split_script, rebuild_frames

    md = _load_script_md(ep) if md is None else md
    if not md.strip():
        raise RuntimeError(f"episode {ep.id} 无脚本内容（script_path 为空或不存在）")
    # 优先 rebuild_frames（保留既有音频/shots 资产），否则 split_script
    if ep.frames and ep.shots:
        rebuild_frames(ep, md, merge_short=merge_short)
    else:
        ep.frames = split_script(ep, md, merge_short=merge_short)
    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 脚本切分后 frames 为空")
    print(f"  [orchestrator] script: frames={len(ep.frames)}")


def stage_tts(ep: Episode, fallback: bool = True,
              granularity: str = "paragraph",
              group_chars: int | None = None) -> None:
    """frames → CosyVoice 合成落盘。产物校验：每 frame 音频文件存在。

    granularity（2026-09-06）：
      "paragraph"（默认）按段落合成长音频再切回逐帧 —— 逐帧只给 TTS 13~15 字，
      句子被切碎后韵律/情感塌陷（用户实测反馈）。字数不变所以花费基本不变。
      "frame" 退回旧的逐帧合成。
      ⚠️ 段落边界靠 ep.script_path 还原；还原不出会**安全退化**成逐帧并打印提示。

    fallback（整改 B2-3，审计 P0-2 + 复核新发现 N2）：
      cosyvoice 失败时是否静默降级 edge。此前本函数**根本没传**这个参数，
      于是永远取 voice.synthesize 的默认值 True —— 而 `--no-fallback` 只存在于
      voice 的独立 CLI 与 bin/ 脚本，**produce 这条官方生产链上无从关闭**。
      结果：官方入口出片，cosyvoice 一抖就自动换个音色往下跑，成片无任何标记。
      现在由 produce(tts_fallback=) 显式传入，CLI 有 --no-fallback。
      交付集建议关掉：宁可失败重跑，也不要一部片子里有两种声音。
    """
    from pathlib import Path
    from app.managers.episode import episode_path
    from app.services.voice import synthesize

    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 无 frames，先跑 script stage")
    audio_dir = _episode_dir(ep.id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    rep = synthesize(ep.frames, engine="cosyvoice", out_dir=audio_dir,
                     fallback_to_edge=fallback,
                     granularity=granularity,
                     script_md=_load_script_md(ep),
                     target_chars=group_chars)
    missing = [f.id for f in ep.frames
               if not (f.audio_path and Path(f.audio_path).exists())]
    if missing:
        raise RuntimeError(f"episode {ep.id} 仍有 {len(missing)} 段缺音频: {missing[:10]}")
    total = sum(f.audio_duration_ms or 0 for f in ep.frames)
    print(f"  [orchestrator] tts: {len(ep.frames)}/{len(ep.frames)} 落盘，"
          f"旁白 {total/1000:.1f}s")
    # 音色一致性体检：降级帧与非 cosyvoice 帧都会在这里显形（manifest 打标的消费方）
    if rep.get("mixed"):
        deg = rep.get("degraded", [])
        print(f"  [orchestrator][告警] 音频目录存在非 cosyvoice 音色的帧 "
              f"{len(deg)}/{rep.get('total', 0)}：{deg[:10]}"
              f"{' …' if len(deg) > 10 else ''}")
        print(f"  [orchestrator][告警] 成片会中途换人声；建议跑 "
              f"--stage tts --force 重合成，或用 --no-fallback 让失败暴露。")


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


# ---------------------------------------------------------------------------
# prompt 预筛（2026-09-04）
# ---------------------------------------------------------------------------
# 生图是全程唯一真花钱 + 唯一有外部配额的环节。把「必然失败或必然白花钱的
# prompt」在发请求前拦掉，比事后对着渠道后台对账便宜得多。
# 典型事故：分镜 md 某行漏写 en → prompt 只剩 ", 电影感." → 照样发请求、
# 照样扣配额、回来一张与剧情无关的图，且因为文件 >50KB 被幂等跳过，
# 整季都发现不了。
#
# 阈值依据（实测 episodes/*/image_prompts.json）：
#   laogongtuan n=56 最短141 中位192 / lsm-15min n=64 最短146 中位181
# 故 12 字门槛距真实最短值有 10 倍余量，只拦漏写与占位符，不做质量判断。
# I 批（2026-09-05）：改走 env_get（.env 优先、真实环境变量兜底）。
# 此前直读 os.environ，这些键写进 .env 是无效的。
PROMPT_MIN_CHARS = int(env_get("HV_PROMPT_MIN_CHARS", "12"))
PROMPT_MAX_CHARS = int(env_get("HV_PROMPT_MAX_CHARS", "4000"))

# 占位符/未完成标记：出现即视为漏写（不区分大小写）
_PROMPT_PLACEHOLDER = (
    "todo", "tbd", "fixme", "xxx", "placeholder", "待补", "待定", "占位",
    "test", "测试", "示例", "sample", "n/a", "null", "none",
)


def screen_prompt(key: str, prompt: str) -> tuple[bool, str, str]:
    """发请求前的 prompt 体检。返回 (ok, reason, normalized)。

    ★ 检的是**分镜原始 en**，不是拼上 style_suffix/negative 之后的成品：
      风格后缀本身就有十几二十字，若对成品做「空/过短」判定，en 漏写会被
      后缀掩盖成一条看似正常的长 prompt —— 于是照样发请求、照样扣配额，
      回来一张与剧情无关的图，还因为 >50KB 被幂等永久跳过，整季都发现不了。

    ok=False → 必须拦下（发=白烧配额）；ok=True → 放行，reason 可能是
    "OK" 或已处置的提示（如 "截断 4200→4000"）。
    normalized 是压平空白后的原始文本。
    """
    raw = (prompt or "").strip()
    # 分镜 md 里 prompt 常带换行/多空格 → 先压平，避免长度与去重判定失真
    norm = " ".join(raw.split())
    if not norm or norm.strip(" ,.。-") == "":
        return False, "空 prompt（分镜 md 该行漏写 en？）", norm
    low = norm.lower()
    for ph in _PROMPT_PLACEHOLDER:
        if low == ph or low.startswith(ph + " ") or low.startswith(ph + ","):
            return False, f"疑似占位符/未完成（{ph}）", norm
    if len(norm) < PROMPT_MIN_CHARS:
        return False, f"过短（{len(norm)} 字 < {PROMPT_MIN_CHARS}）", norm
    if len(norm) > PROMPT_MAX_CHARS:
        return True, f"超长已截断 {len(norm)}→{PROMPT_MAX_CHARS}", norm[:PROMPT_MAX_CHARS]
    return True, "OK", norm


def screen_image_prompts(ep_id: str) -> dict:
    """对整集 image_prompts 做预筛体检，返回报告（不发任何请求）。

    报告结构：{"passed": [key...], "blocked": [(key, reason)...],
               "warned": [(key, reason)...], "dup": [(a, b)...]}
    """
    spec = _read_image_prompts(ep_id)
    im_cfg_parts = _image_prompt_parts()
    seen: dict = {}
    rep: dict = {"passed": [], "blocked": [], "warned": [], "dup": []}
    for key in sorted(k for k in spec if not k.startswith("_")):
        entry = spec[key]
        en = entry.get("en") if isinstance(entry, dict) else str(entry)
        prompt = _compose_prompt(en, *im_cfg_parts)
        ok, reason, _ = screen_prompt(key, en)
        if not ok:
            rep["blocked"].append((key, reason))
            continue
        if reason != "OK":
            rep["warned"].append((key, reason))
        # 去重在【成品】上做：真正重复烧钱的是发出去的字符串相同
        if prompt in seen:
            rep["dup"].append((seen[prompt], key))
        else:
            seen[prompt] = key
        rep["passed"].append(key)
    return rep


def _image_prompt_parts() -> tuple[str, str]:
    """取 (style_suffix, negative_clause)；与 build_image_jobs 同源。"""
    from app.config.config import get_settings
    im = get_settings().get_profile().image
    return (getattr(im, "style_suffix", "") or "",
            getattr(im, "negative_clause", "") or "")


def _compose_prompt(en: str, suffix: str, neg: str) -> str:
    """prompt 拼接唯一口径（build_image_jobs 与预筛共用，防漂移）。"""
    return f"{en}, {suffix}. {neg}".strip(", .") + "."


def build_image_jobs(ep_id: str, prescreen: bool = True,
                     only: set[str] | None = None) -> list["ImageJob"]:
    """从 image_prompts.json 构建生图任务（prompt 单源）。

    only：只构建这些 key（用于单镜重建 reshot）。None = 全部。

    唯一数据源 = episodes/{id}/image_prompts.json：
      { "LGT01": {"en": "...", ...}, ... }
    每个键 → ImageJob(落盘 episodes/{id}/shots/{KEY}.png)。
    prompt = en + style_suffix + negative（扁平 ImageConfig 顶层字段）。
    幂等由 generate_many 内部按「已存在 >50KB」处理，本函数只建任务不生成。
    stage_images 与 batch_generate_lgt 共用本函数 → 消除 prompt 拼接漂移。

    prescreen=True（默认）：先跑 screen_prompt 体检，拦下空/占位符/过短的
    prompt —— 这些发过去只会白烧配额，且会因为「回来个 >50KB 的图」
    被幂等机制永久跳过，事后极难发现。被拦的键会明确打印，不会静默消失。
    """
    from app.config.config import get_settings
    from app.models import ImageGenRequest
    from app.services.image import ImageJob

    spec = _read_image_prompts(ep_id)
    if not spec:
        return []
    im = get_settings().get_profile().image
    # 引擎参数（model/size/quality）单一真源 = config.image.primary。
    #   此前全部取自 profile.image，导致顶层 image 整段（**含
    #   strict_param_validation**）零消费者 —— 改了完全不生效（H 批）。
    #   profile.image 只留「风格」相关：style_suffix / negative_clause。
    #   两边 engine/size/quality 当前值一致，此切换行为中性。
    prim = getattr(get_settings().image, "primary", None)
    model = (getattr(prim, "model", None) or getattr(prim, "engine", None)
             or getattr(im, "engine", None) or "gpt-image-2")
    size = (getattr(prim, "size", None) or getattr(im, "size", None) or "1080x1920")
    quality = (getattr(prim, "quality", None) or getattr(im, "quality", None) or "low")
    suffix = getattr(im, "style_suffix", "") or ""
    neg = getattr(im, "negative_clause", "") or ""

    # strict_param_validation：非法参数在**花钱之前**拒绝整批，而不是等中转站
    # 静默忽略、照常计费、回来一堆比例不对的图（该开关此前无任何消费者）。
    if getattr(prim, "strict_param_validation", False):
        from app.services.image import validate_image_params
        bad = validate_image_params(size, quality, model)
        if bad:
            raise RuntimeError(
                "strict_param_validation=true 且生图参数非法，拒绝生成"
                "（避免白烧配额）：\n  " + "\n  ".join(bad))
    shots_d = _episode_dir(ep_id) / "shots"
    shots_d.mkdir(parents=True, exist_ok=True)

    keys = sorted(k for k in spec if not k.startswith("_"))
    if only:
        unknown = sorted(set(only) - set(keys))
        if unknown:
            raise RuntimeError(
                f"episode {ep_id} 的 image_prompts.json 中没有这些 key: {unknown}")
        keys = [k for k in keys if k in only]
    jobs: list[ImageJob] = []
    blocked: list = []
    warned: list = []
    for key in keys:
        entry = spec[key]
        en = entry.get("en") if isinstance(entry, dict) else str(entry)
        if not en:
            blocked.append((key, "en 字段为空"))
            continue
        prompt = _compose_prompt(en, suffix, neg)
        if prescreen:
            ok, reason, norm = screen_prompt(key, en)
            if not ok:
                blocked.append((key, reason))
                continue
            if reason != "OK":
                warned.append((key, reason))
            prompt = _compose_prompt(norm, suffix, neg)
            if len(prompt) > PROMPT_MAX_CHARS:   # 成品仍可能超长（后缀很长时）
                warned.append((key, f"成品超长已截断 {len(prompt)}"
                                    f"→{PROMPT_MAX_CHARS}"))
                prompt = prompt[:PROMPT_MAX_CHARS]
        jobs.append(ImageJob(
            id=key,
            req=ImageGenRequest(
                model=model, prompt=prompt,
                size=size, quality=quality, n=1,
            ),
            out_path=shots_d / f"{key}.png",
        ))

    if prescreen and (blocked or warned):
        print(f"  [orchestrator] prompt 预筛: 通过 {len(jobs)} / "
              f"拦截 {len(blocked)} / 提示 {len(warned)}")
        for k, r in blocked[:20]:
            print(f"      [拦截] {k}: {r}")
        for k, r in warned[:20]:
            print(f"      [提示] {k}: {r}")
        if len(blocked) > 20:
            print(f"      … 另有 {len(blocked)-20} 条被拦截")
        # 默认只拦不炸（老集可能有历史短 prompt，不该一夜之间全跑不起来）；
        # 需要「预筛即门禁」时用 HV_PROMPT_PRESCREEN_STRICT=1。
        if blocked and env_get("HV_PROMPT_PRESCREEN_STRICT") in (
                "1", "true", "yes"):
            raise RuntimeError(
                f"episode {ep_id} 有 {len(blocked)} 条 prompt 未通过预筛，"
                f"已按 HV_PROMPT_PRESCREEN_STRICT 拒绝生图: {blocked[:10]}")
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


def stage_assets(ep: Episode, tts_fallback: bool = True,
                 tts_granularity: str = "paragraph",
                 tts_group_chars: int | None = None) -> None:
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
        tasks.append(_run_t(
            lambda e: stage_tts(e, fallback=tts_fallback,
                                granularity=tts_granularity,
                                group_chars=tts_group_chars), "tts"))
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
    # compose_episode 自载副本按真实音频重算时间轴并落盘；必须同步回本对象，
    # 否则外层 produce 的 save(ep) 会用旧时长覆盖磁盘真值，导致 verify 用
    # 旧口径对新成片（2026-09-05 lsm-15min 实测：首镜缺 100ms head pad，
    # 旧口径多算 2 帧，verify 误报 FAILED）。只同步 frames/shots，不碰状态。
    from app.managers.episode import load as _load
    fresh = _load(ep.id)
    ep.frames = fresh.frames
    ep.shots = fresh.shots
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
    # 注意：此处打印字符须 GBK 安全（Windows 默认代码页 cp936）。曾用 ✓(U+2713)
    # 在 gbk stdout 抛 UnicodeEncodeError，produce 校验成功却以失败退出。改 [OK]。
    print(f"  [orchestrator] verify: {final.name} {size/1024/1024:.1f}MB "
          f"{nf}帧 解码0错 时序对齐 [OK]")
    return {"ok": True, "frames": nf, "bytes": size}


def reshot(ep_id: str, keys: list[str], do_verify: bool = True) -> dict:
    """重建指定底图 → 重新装配 shots → 重新合成（单镜重建）。

    替代旧 `hv episode run X --only shot-001`（managers/pipeline，C3 已删）。

    旧实现靠人脸相似度锚定（face.py / character_lock.py）保证换图后角色一致，
    那套能力随 C3 一并删除。现在「指定重建哪一张」用最直接的手段：**删掉旧图**。
    生图的幂等是按「shots/{KEY}.png 已存在且 >50KB」跳过，所以删文件本身就是
    最小可用的选择机制，不必再引入一套并行的 key 过滤语义。
    upscale 的 @2x 缓存按源图 mtime 失效，删图重生成后会自动重做，无需手工清。

    步骤：删图 → 只生这几个 key → 按 scene_plan 重新装配 → 合成 →（可选）verify。
    """
    from app.managers import load, save
    from app.services.image import generate_many
    from app.services.shot_builder import build_episode_shots

    keys = [k.strip() for k in keys if k and k.strip()]
    if not keys:
        raise RuntimeError("reshot 需要至少一个 --key（底图 key，如 LGT01）")

    ep = load(ep_id)
    spec = _read_image_prompts(ep_id)
    unknown = [k for k in keys if k not in spec]
    if unknown:
        raise RuntimeError(
            f"episode {ep_id} image_prompts.json 中没有 {unknown}；"
            f"可用 key 共 {len(spec)} 个")

    shots_d = _episode_dir(ep_id) / "shots"
    removed = []
    for k in keys:
        p = shots_d / f"{k}.png"
        if p.exists():
            p.unlink()
            removed.append(k)
    print(f"  [orchestrator] reshot: 已删除 {len(removed)}/{len(keys)} 张旧图 "
          f"（{removed}），准备重新生成")

    jobs = build_image_jobs(ep_id, only=set(keys))
    if not jobs:
        raise RuntimeError(f"episode {ep_id} 没有可重建的生图任务（key={keys}）")
    res = generate_many(jobs)
    failed = [k for k, v in res.items() if not k.endswith("__error") and v is None]
    if failed:
        raise RuntimeError(f"episode {ep_id} 重建生图失败: {failed}")

    # 重新装配（scene_plan 驱动；帧指纹会校验 frames 未变）
    ep2, _st = build_episode_shots(ep_id, persist=True, preview_rows=0)
    print(f"  [orchestrator] reshot: 重新装配 {len(ep2.shots)} 镜")

    stage_compose(ep2)      # 内部会按真实音频重载并回同步 ep2
    out = {"ep_id": ep_id, "reshot_keys": keys, "removed": removed,
           "shots": len(ep2.shots), "final_mp4": None}
    if do_verify:
        out["verify"] = stage_verify(ep2)
    out["final_mp4"] = str(_out_dir(ep_id) / f"{ep_id}_m1.mp4")
    save(ep2)
    return out


def _load_script_md(ep: Episode) -> str:
    """取脚本文本：ep.script_path 指向的文件，或约定路径 episodes/{id}/script.md。

    D4：不再用 ep.notes 兜底——notes 会被 checkpoint 失败逻辑占用
    （managers/episode.py 写 "[label] FAILED: ..."），拿它当脚本源等于把一条
    报错文本切成帧去配音。脚本要么落文件、要么由调用方显式传入。
    """
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
    tts_fallback: bool = True,
    merge_short: bool = False,
    tts_granularity: str = "paragraph",
    tts_group_chars: int | None = None,
) -> dict:
    """
    跑真实生产链（不滥用 rigid 状态枚举空推）；状态由产物真值导出。
      stages: None=全跑适用的；或 ['script','tts','compose','verify'] 子集。
      tts_granularity: "paragraph"（默认）按段落合成长音频再切回逐帧，
        改善韵律/情感；"frame" 退回逐帧合成（见 stage_tts）。
      tts_fallback: cosyvoice 失败是否静默降级 edge（B2-3）。交付集建议 False。
      merge_short: 合并过短片段（见 stage_script）。**新集建档才开**，
        既有多集开它会让帧数变化 → 音频与 scene_plan 错位。默认 False。
    返回 {ep_id, status, stages_done, verify}。
    """
    from app.managers import load, save
    from app.managers.episode import validate_schema
    from app.models import EpisodeStatus

    ep = load(ep_id)

    # schema 硬阻断（P0-4 修复）：任何生产前，schema 不干净即拒绝，绝不到付费生图。
    # allow_empty：空集若即将切脚本（有 script.md 或调用方传了正文），放行让
    # stage_script 成为第一步（否则「官方入口启动官方第一步」被鸡生蛋卡死）。
    # D4：这里原本 `ep.notes = script_md`，save(ep) 会把整篇剧本写进 episode.json。
    has_script = bool((_load_script_md(ep) or (script_md or "")).strip())
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
                if ep.frames and not script_md:
                    # 已有 frames（先前切过脚本）：默认不重切，避免 splitter 规则一变
                    # 就重分帧 + 重 TTS(钱) + 重合成(小时)。需显式 --stage script 强制。
                    # D4：判据从 `not ep.notes` 改成显式 `not script_md`（同一语义，
                    # 但不再依赖"调用方有没有污染过 notes"这种隐式信号）。
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

    # tts/assets 需要透传 tts_fallback；STAGE_FN 是模块级映射，不在原地改它
    # （API 与 CLI 共用），只在本轮调用上覆盖一份。
    stage_fn = dict(STAGE_FN)
    # ⚠️ 必须**无条件**覆盖，不能写成 `if not tts_fallback:` ——
    #   那样只有 --no-fallback 时闭包才带参数，其余情况 tts_granularity 会被
    #   静默丢掉（同坑 #38：--merge-short 当年就是这么失效的）。
    stage_fn["tts"] = lambda ep: stage_tts(
        ep, fallback=tts_fallback, granularity=tts_granularity,
        group_chars=tts_group_chars)
    stage_fn["assets"] = lambda ep: stage_assets(
        ep, tts_fallback=tts_fallback, tts_granularity=tts_granularity,
        tts_group_chars=tts_group_chars)
    if script_md or merge_short:
        # D4：把正文用闭包喂给 stage_script，而不是写进 ep.notes 再读回。
        # merge_short 也必须走这个闭包——STAGE_FN 里存的是默认参数版本，
        # 不覆盖的话 CLI 的 --merge-short 会被静默丢掉（2026-09-06 实测：
        # 加了参数仍是 248 帧，因为根本没传进去）。
        stage_fn["script"] = lambda ep: stage_script(
            ep, md=script_md, merge_short=merge_short)

    done = []
    for st in stages:
        if st not in stage_fn:
            raise RuntimeError(f"未知 stage: {st}（可选 {list(STAGE_FN)}）")
        stage_fn[st](ep)            # 真实干活；失败即抛 → 不标记完成
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
