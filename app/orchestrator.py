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
    from app.services.script_document import parse_document
    from app.services.provenance import atomic_json
    atomic_json(_episode_dir(ep.id)/"script_document.v2.json", parse_document(md))
    # 优先 rebuild_frames（保留既有音频/shots 资产），否则 split_script
    if ep.frames and ep.shots:
        rebuild_frames(ep, md, merge_short=merge_short)
    else:
        ep.frames = split_script(ep, md, merge_short=merge_short)
    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 脚本切分后 frames 为空")
    print(f"  [orchestrator] script: frames={len(ep.frames)}")


def stage_tts(ep: Episode, fallback: bool = False,
              granularity: str = "paragraph",
              group_chars: int | None = None,
              workers: int | None = None) -> None:
    """frames → CosyVoice 合成落盘。产物校验：每 frame 音频文件存在。

    granularity：
      "frame"（**默认**，2026-09-07 起）逐帧合成。
      "paragraph" 按段落合成长音频再切回逐帧。

      ⚠️ 为什么默认改成 frame（保守选择，2026-09-07 实测）：段落级在长上下文
      下会把「晋阳令刘文静」断成「晋阳 / 令刘文静」，且 ≥45 字必错，
      而段落级中位长度正是 65 字 —— 结构性错误，不是偶发。插空格 / 逗号 /
      破折号都救不回来，模型会忽略这些韵律提示。只有逐帧（≈11 字上下文）
      实测断对，与用户听感一致。
      代价：每帧多约 300ms 首尾静音（全片约 +8%），但那是**纯静音**，
      不动语音本身；用户明确表示宁可多这 20 多秒。
      文本里没有「地名/官名+人名」紧连专名时，可手动切 paragraph 换句间韵律。
      workers: TTS 并发路数。None = 按粒度自动（逐帧 2 / 段落 4，
        见 voice._resolve_workers）；逐帧档 220 帧 = 220 次请求，
        4 路并发实测撞 Throttling.RateQuota。
      ⚠️ 段落边界靠 ep.script_path 还原；还原不出会**安全退化**成按句预算分组。

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
    from app.services.voice import synthesize, DEFAULT_ENGINE as DEFAULT_VOICE_ENGINE

    if not ep.frames:
        raise RuntimeError(f"episode {ep.id} 无 frames，先跑 script stage")
    audio_dir = _episode_dir(ep.id) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
        # 引擎从配置读取（2026-09-08 起：qwen-audio-3.0-tts / cosyvoice 可切换）。
    # 之前这里写死 "cosyvoice"，导致 engine: qwen 配置完全不起作用。
    from app.config.config import get_settings
    _eng = (get_settings().get_profile().voice.engine
            or DEFAULT_VOICE_ENGINE)

    # P1-4（审计）：narration_text 的读音修复方式与引擎绑定——
    #   cosyvoice 靠 polyphone 同音字替换；qwen 靠 hot_fix（需要原文）。
    #   切引擎后不重生成 → 多音字全读错。双向防护告警（None 不误报）。
    from app.services.polyphone import preprocess as _pp
    _ssml_on = False
    try:
        _ssml_on = bool(get_settings().get_profile().voice.tts_ssml)
    except Exception:  # noqa: BLE001
        pass
    _mismatch = []
    # SSML 开启时读音由 <phoneme> 直接钉死，narration_text 用原文即可 → 不告警
    for f in ([] if _ssml_on else ep.frames):
        _nt = f.narration_text
        if _nt is None:
            continue
        if (_nt == f.text) != (_eng == "qwen") and \
                _pp(f.text, log_hits=False) != f.text:
            _mismatch.append(f.id)
    if _mismatch:
        print(f"  [tts][告警] 引擎 {_eng} 与 narration_text 的读音修复方式不匹配"
              f"（{len(_mismatch)} 帧，如 {_mismatch[:3]}）。"
              f"cosyvoice 需要同音字替换版、qwen 需要原文版——"
              f"请用 --stage script 重生成（⚠️ 会重切帧，scene_plan 需重建）。")
    rep = synthesize(ep.frames, engine=_eng, out_dir=audio_dir,
                     fallback_to_edge=fallback,
                     granularity=granularity,
                     script_md=_load_script_md(ep),
                     target_chars=group_chars,
                     workers=workers)
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
        _by = rep.get("by_engine") or {}
        _main = (max(_by.items(), key=lambda kv: kv[1])[0] if _by
                 else "cosyvoice")
        print(f"  [orchestrator][告警] 音频目录存在非主引擎（{_main}）音色的帧 "
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

# 风格词黑名单：与 AGENTS.md「项目画风固定」同源。写进 en 会与 negative_clause
# 正面冲突，模型择一后实际风格不可预期（caomin-01 全 36 张返工先例）。
# 拉丁词用词界匹配，免得 "context" 被 "text" 误伤；CJK 词无需词界。
_STYLE_BANNED_LATIN = (
    r"cartoon", r"anime", r"ink-wash", r"infographic", r"photorealistic",
    r"3d render", r"chiaroscuro", r"silk-scroll", r"photograph",
    r"digital art", r"manga", r"comic",
)
_STYLE_BANNED_CJK = ("水墨", "插画", "写实", "油画", "水彩", "二次元", "厚涂")
# 注意："flat" 不在表内——"flat daylight" 类正常英文会被误伤，已实测
# （xlc-01 XL03/XL10/XL18）。真要写扁平风格请走配置，不得写进 en。

# 疑似“要求图中渲染文字”：negative_clause 含 no text，进 en 等于自相矛盾。
# 这类表述歧义大（画卷轴道具 vs 要可读标题），只提示、永不拦截。
_TEXT_REQUEST_CJK = ("标题", "字幕", "标牌", "海报", "水印")
_TEXT_REQUEST_LATIN = (
    r"watermark", r"caption", r"label", r"title card", r"lettering",
    r"readable text", r"legible",
)


def _word_hit(low: str, words) -> str | None:
    import re as _re
    for w in words:
        if _re.search(r"(?<![a-z])" + w + r"(?![a-z])", low):
            return w
    return None


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
    hit = _word_hit(low, _STYLE_BANNED_LATIN) or next(
        (w for w in _STYLE_BANNED_CJK if w in norm), None)
    if hit:
        # 风格词进 en 会与 negative_clause 打架：默认 warn（老集不炸），
        # strict/HV_PROMPT_PRESCREEN_STRICT 下由调用方升级为拦截。
        return True, f"风格词疑似与 house look 冲突（{hit}），请走配置改画风", norm
    for w in _TEXT_REQUEST_CJK:
        if w in norm:
            return True, f"疑似要求图中渲染文字（{w}），negative 含 no text", norm
    hit = _word_hit(low, _TEXT_REQUEST_LATIN)
    if hit:
        return True, f"疑似要求图中渲染文字（{hit}），negative 含 no text", norm
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
                     only: set[str] | None = None, strict: bool = False) -> list["ImageJob"]:
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
    from app.models import ImageGenRequest, ImageEditRequest
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
            if reason.startswith("风格词") and (strict or env_get("HV_PROMPT_PRESCREEN_STRICT") in (
                    "1", "true", "yes")):
                # 风格词在 strict 下升级为拦截：它与 negative_clause 的冲突是
                # 确定性的，不是一次警告能对冲的。默认 warn 不炸老集。
                blocked.append((key, reason))
                continue
            if reason != "OK":
                warned.append((key, reason))
            prompt = _compose_prompt(norm, suffix, neg)
            if len(prompt) > PROMPT_MAX_CHARS:   # 成品仍可能超长（后缀很长时）
                warned.append((key, f"成品超长已截断 {len(prompt)}"
                                    f"→{PROMPT_MAX_CHARS}"))
                prompt = prompt[:PROMPT_MAX_CHARS]
        job_model = entry.get('model', model) if isinstance(entry, dict) else model
        request = ImageGenRequest(model=job_model, prompt=prompt, size=size, quality=quality, n=1)
        references = ()
        reference = entry.get('reference_path') if isinstance(entry, dict) else None
        if isinstance(entry, dict) and entry.get('reference_paths'):
            if len(entry['reference_paths']) != 1:
                raise ValueError('this image channel supports one explicit character reference per job')
            reference = entry['reference_paths'][0]
        if reference:
            import base64
            from PIL import Image
            path = Path(reference)
            if not path.is_absolute(): path = _episode_dir(ep_id)/path
            with Image.open(path) as im:
                if min(im.size) < 64: raise ValueError('character reference is too small')
                im.verify()
            # Encoded bytes stay inside the provider request; never log them.
            request = ImageEditRequest(model=job_model, prompt=prompt, size=size, quality=quality, n=1,
                                       image=base64.b64encode(path.read_bytes()).decode('ascii'))
            references = (str(path.resolve()),)
        jobs.append(ImageJob(
            id=key,
            req=request,
            out_path=shots_d / f"{key}.png",
            reference_paths=references,
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
        if blocked and (strict or env_get("HV_PROMPT_PRESCREEN_STRICT") in (
                "1", "true", "yes")):
            raise RuntimeError(
                f"episode {ep_id} 有 {len(blocked)} 条 prompt 未通过预筛，"
                f"已按 HV_PROMPT_PRESCREEN_STRICT 拒绝生图: {blocked[:10]}")
    # 同请求去重报告：generate_many 会按 request_fingerprint 只付一次钱，
    # 这里只报告、不改 jobs（键与落盘路径仍一一对应）。
    _fp_first: dict = {}
    _dups: list = []
    for job in jobs:
        try:
            _fp = job.request_fingerprint()
        except Exception:
            continue
        if _fp in _fp_first:
            _dups.append((_fp_first[_fp], job.id))
        else:
            _fp_first[_fp] = job.id
    if _dups:
        print(f"  [orchestrator] 重复请求 {len(_dups)} 组，生图时只付一次钱："
              + ", ".join(f"{a}={b}" for a, b in _dups[:10])
              + (" …" if len(_dups) > 10 else ""))
    return jobs


def stage_images(ep: Episode, budget=None, only: set[str] | None = None) -> None:
    """按 image_prompts.json 批量生图（三路并行 generate_many）。

    数据源唯一入口 = episodes/{id}/image_prompts.json（见 build_image_jobs）。
    已存在 >50KB 的图幂等跳过（不占渠道）。任一失败即 raise（stage 语义）。

    only: 只生成这些 key。**补单张图时必用**——此前 stage_images 始终处理
    全部待生成 key，于是「只想补 D40」会把所有缺图一起拉进来，预算被抢占、
    单张补不上（2026-09-29 caomin-01 实测：3 槽预算被 4 张瓜分全军覆没）。

    花钱前的镜长预检就压在这里（generate_many 调用**之前**）：镜长上限 #3 原本
    只在 compose 阶段断言，而 compose 在全部图片付完钱之后才跑。lishimin-03
    实测：285 帧里 2 帧是旁白真放慢到 8.03s/8.24s，预算花完才被拦下。
    切帧器按 TTS_CHARS_PER_SEC=3.9 估算字数，而真人真人旁白有戏剧性停顿，
    字数不是时长的可靠预测 —— 只有实测音频能提前判定。
    """
    import os

    from app.services.timing_assert import assert_pacing_or_raise, projected_shot_pacing
    jobs = build_image_jobs(ep.id, only=only)
    if not jobs:
        what = f"（only={sorted(only)} 无匹配）" if only else ""
        print(f"  [orchestrator] image: 无 {ep.id}/image_prompts.json 或全空{what}，跳过生图")
        return
    # 只对【真正要发起调用】的子集判定：已存在的图不再花钱，其时长不该成为新拦路虎。
    pending = [j for j in jobs if not (os.path.exists(j.out_path) and j.is_done())]
    if ep.frames and pending:
        if not projected_shot_pacing(ep)["ok"]:
            assert_pacing_or_raise(ep)   # 必然抛，给出可执行指引
        # 镜长过了不代表字面对了：错字集照样会把 30+ 张图的预算烧光。
        from app.services.subtitle_fidelity import assert_subtitle_fidelity
        assert_subtitle_fidelity(ep)
        print(f"  [orchestrator] image: 镜长预检 + 字幕字面门禁通过"
              f"（{len(ep.frames)} 帧，待生成 {len(pending)} 张）")
    from app.services.image import generate_many
    print(f"  [orchestrator] image: {len(jobs)} 张待生成/跳过（三路并行）")
    res = generate_many(jobs, budget=budget)
    failed = [k for k, v in res.items() if not k.endswith("__error") and v is None]
    if failed:
        raise RuntimeError(
            f"episode {ep.id} 有 {len(failed)} 张生图失败: "
            f"{[(k, res.get(k+'__error')) for k in failed[:5]]}")
    print(f"  [orchestrator] image: {len(jobs)} 张全部就绪")


def stage_assets(ep: Episode, tts_fallback: bool = False,
                 tts_granularity: str = "paragraph",
                 tts_group_chars: int | None = None,
                 tts_workers: int | None = None,
                 budget=None) -> None:
    """TTS 先行、生图随后（**串行**，两侧都结束才返回）。

    为什么不并行（2026-09-28 改，lishimin-03 实测）：原先用
    ThreadPoolExecutor 让 TTS ∥ 生图同跑。但镜长预检（stage_images 开头）
    判的是 frame.audio_duration_ms —— 并行时生图侧可能读到 TTS 之前的
    0 值或旧值，预检要么空过、要么误报。让「花钱前必须校验」成立，
    就必须保证校验读到的是**刚生成完的真实时长**，即 TTS 必须先完成。
    代价是失去两者的重叠（生图本就是长杆，实际影响有限）。

    - stage_tts 失败：不进生图（时长不可信，镜长无从判定）。
    - stage_images 失败：TTS 产物已落盘，可断点续跑。
    """
    has_frames = bool(ep.frames)
    spec = _read_image_prompts(ep.id)
    has_prompts = bool(spec)
    if not has_frames and not has_prompts:
        print("  [orchestrator] assets: 无 TTS 帧且无生图 prompts，跳过")
        return

    if has_frames:
        stage_tts(ep, fallback=tts_fallback, granularity=tts_granularity,
                  group_chars=tts_group_chars, workers=tts_workers)
    if has_prompts:
        stage_images(ep, budget=budget)


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

    from app.services.media_integrity import verify_media
    integrity = verify_media(final)

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

    # 字幕字面门禁：verify 至今只校验时长与帧数，没有一项读字幕**字面**。
    # caomin-01/lishimin-04 的「天克寒」「高勾离」就是这样带着 verify ok
    # 交付的（2026-09-29）。成片已存在但字幕偏离原文时，必须在这里拦下。
    from app.services.subtitle_fidelity import assert_subtitle_fidelity
    fidelity = assert_subtitle_fidelity(ep)

    # 外部音频绑定：音轨被换 / 帧序被改 / 切分被动，成片就不再对应这份素材。
    from app.services.external_audio import verify_binding
    binding = verify_binding(ep)
    if not binding.get('ok'):
        raise RuntimeError(
            f"episode {ep.id} 外部音频绑定失效:\n  - " + "\n  - ".join(binding['fails'])
            + "\n  处置：重新执行 episode import-audio（或重跑音频导入 stage）"
              "使帧偏移与音轨重新绑定，再重新合成。")
    bind_note = (f" 音频绑定{binding.get('frames', 0)}帧"
                 if not binding.get("skipped") else "")

    size = final.stat().st_size
    # 注意：此处打印字符须 GBK 安全（Windows 默认代码页 cp936）。曾用 ✓(U+2713)
    # 在 gbk stdout 抛 UnicodeEncodeError，produce 校验成功却以失败退出。改 [OK]。
    warn = f" 字幕漂移{len(fidelity.get('warns', []))}处" if fidelity.get("warns") else ""
    print(f"  [orchestrator] verify: {final.name} {size/1024/1024:.1f}MB "
          f"{nf}帧 解码0错 时序对齐 字面OK{warn}{bind_note} [OK]")
    # 用 get 不用下标：无剧本正文可比对时报告里没有 source_chars，直接下标
    # 会把「缺少基线」变成 KeyError，而不是一条可读的状态（2026-09-29）。
    return {"ok": True, "frames": nf, "bytes": size,
            "subtitle_fidelity": {k: fidelity.get(k) for k in
                                  ("ok", "applicable", "frames", "source_chars")}}


def reshot(ep_id: str, keys: list[str], do_verify: bool = True, *, budget_id=None, max_images=0) -> dict:
    """Generate scoped image candidates; preserve published media and manual shot edits."""
    import uuid
    from app.managers import load
    from app.services.image import generate_many
    from app.services.provenance import atomic_json, file_sha256
    keys = list(dict.fromkeys(k.strip() for k in keys if k and k.strip()))
    if not keys: raise ValueError("reshot requires image keys")
    with _episode_lock(ep_id):
        ep = load(ep_id)
        jobs = build_image_jobs(ep_id, only=set(keys))
        if {j.id for j in jobs} != set(keys):
            raise ValueError("requested images were blocked or missing; no generation performed")
        if not budget_id: raise ValueError('reshot requires an explicit stable budget ID')
        from app.services.provider_budget import for_run
        from app.services.provenance import fingerprint
        budget = for_run(_episode_dir(ep_id)/'production', ['reshot',budget_id],dict(max_images=max_images))
        candidate = _episode_dir(ep_id)/"candidates"/fingerprint(['reshot',budget_id,keys])
        candidate.mkdir(parents=True, exist_ok=True)
        before = ep.model_dump(mode="json")
        old_paths={j.id:str(j.out_path).replace("\\","/") for j in jobs}
        for job in jobs: job.out_path=candidate/(job.id+".png")
        result=generate_many(jobs, budget=budget)
        if any(not j.is_done() for j in jobs):
            raise RuntimeError(f"candidate generation failed; previous revision preserved: {candidate}")
        for shot in ep.shots.values():
            for job in jobs:
                if shot.image_path and Path(shot.image_path).resolve()==Path(old_paths[job.id]).resolve():
                    shot.image_path=str(job.out_path.resolve()).replace("\\","/")
                    shot.upscaled_path=None
        atomic_json(candidate/"episode.json",ep.model_dump(mode="json"))
        atomic_json(candidate/"source_revision.json",before)
        atomic_json(candidate/"images.json",[{"key":j.id,"path":str(j.out_path),"sha256":file_sha256(j.out_path)} for j in jobs])
        return {"ep_id":ep_id,"status":"CANDIDATE_READY","reshot_keys":keys,"removed":[],
                "candidate_episode":str(candidate/"episode.json"),"content_review":"pending", "approved":False}


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
def can_assemble_shots(ep_id: str) -> bool:
    """是否具备装配 shots 的条件（有 frames + 有 scene_plan + 底图齐备）。

    供 produce 的自动链决定 shots 该不该进 runnable —— 条件不足时**不排进**，
    从而不会在 stages_done 里留下一条没干活的假记录。
    """
    from app.managers import load
    from app.services.shot_builder import _resolve_image
    from app.services.narration_pipeline import read
    ep = load(ep_id)
    if not ep.frames or not (_episode_dir(ep_id) / "scene_plan.json").is_file():
        return False
    try:
        plan = read(_episode_dir(ep_id) / "scene_plan.json")
        images = plan.get("images", {})
        return all(_resolve_image(images, sc["image"]).exists()
                   for sc in plan.get("scenes", []))
    except Exception:
        return False


def stage_shots(ep: Episode, plan_path: Path | None = None,
                strict_fingerprint: bool = False) -> None:
    """按 scene_plan.json 真实装配 shots（不再是无操作桩）。

    此前 STAGE_FN['shots'] 绑的是 _stage_shots_skip —— 只打印一行就返回 None，
    于是 `produce --stage shots` 会把 "shots" 记进 stages_done 而一个镜头都没装。
    无人值守下这就是**假绿**：批次记录显示该集分镜已完成，实际 compose 仍会
    因无 shots 报错。分镜的**创意**是人工的，但**装配**是确定性的机械步骤，
    没有理由留在自动链外。

    先 dry-run 预检（不落盘），确认底图/grade/帧指纹/运镜连续性全过再 persist；
    与 `hv episode shots` 走同一条 build_episode_shots，避免两条路行为分叉。
    """
    from app.managers import load as _load
    from app.services.shot_builder import build_episode_shots, inspect_episode
    if not ep.frames:
        raise RuntimeError(
            f"episode {ep.id} 无 frames，无法装配 shots（先跑 --stage script）")
    default = _episode_dir(ep.id) / "scene_plan.json"
    path = Path(plan_path) if plan_path else default
    if not path.is_file():
        raise RuntimeError(
            f"episode {ep.id} 缺分镜文件 {path}：shots 装配需要人工编写的 scene_plan.json"
            f"（底图登记与帧区间）。可用 hv episode shots {ep.id} --dry-run 预检。")
    _, dry = build_episode_shots(ep.id, plan_path=path, persist=False,
                                 preview_rows=0, strict_fingerprint=strict_fingerprint)
    fresh, stats = build_episode_shots(ep.id, plan_path=path, persist=True,
                                       preview_rows=0, strict_fingerprint=strict_fingerprint)
    problems = inspect_episode(ep.id)
    if problems:
        raise RuntimeError(f"episode {ep.id} shots 装配后 schema 不合法: {problems}")
    # build_episode_shots 自载副本并落盘；必须同步回本对象，否则外层 save(ep)
    # 会用装配前的空 shots 覆盖磁盘真值（与 stage_compose 同一类坑）。
    ep.frames = fresh.frames
    ep.shots = fresh.shots
    print(f"  [orchestrator] shots: 装配 {stats['shots']} 镜 / "
          f"{stats['base_images']} 张底图（归并 {stats['reuse_ratio']}:1）")


# 注意：STAGE_FN 必须在上面这些 stage 函数【之后】定义 —— 字典在模块加载时
# 就求值，函数体在其后定义会 NameError。
STAGE_FN = {
    "script": stage_script,
    "tts": stage_tts,
    "images": stage_images,
    "assets": stage_assets,
    "shots": stage_shots,
    "compose": stage_compose,
    "verify": stage_verify,
}


def _check_disk_space(ep_dir, need_mb: int = 2500):
    """渲染前磁盘预检：防止渲到一半磁盘满 → 半截成片（2026-09-09 审计）。

    单集 15min 实测约 2.4G（build 1.2G + shots 678M + out 567M + audio 55M）。
    """
    import shutil
    try:
        free = shutil.disk_usage(str(ep_dir)).free / 1024 / 1024
    except OSError as exc:
        raise RuntimeError('cannot verify free disk space before production') from exc
    # 分级：低于建议值只告警（低磁盘机器不该被一刀切拒绝），低于硬底才拒绝
    if free < need_mb:
        raise RuntimeError(
            f"[produce] 磁盘空间不足：剩余 {free:.0f}MB，本任务需要至少 {need_mb}MB；未开始付费生产。")


def _check_toolchain(*, need_font: bool = False):
    """工具链预检：ffmpeg/ffprobe 必须在 PATH 里，否则不许开工。

    为什么提前到这一步（lishimin-03 实测，2026-09-28）：legacy 路径此前没有
    这道预检，结果是 59 张图付完、287 镜装配完，compose 走到 ffmpeg 调用才抛
    FileNotFoundError —— 钱和时间全花在一个开工就该发现的缺失上。
    字幕要烧字，故 compose 前还要确认字体。v2 走 production_inputs.preflight_media，
    那里已有同样检查；这里只挑 legacy 真正需要的，避免强依赖 ASR 模型。
    """
    import shutil
    missing = [x for x in ('ffmpeg', 'ffprobe') if shutil.which(x) is None]
    if missing:
        raise RuntimeError(
            f"[produce] 缺少必需工具 {'/'.join(missing)}（需在 PATH 中）；未开始付费生产。")
    if need_font:
        from app.services.compose import check_font_available
        check_font_available()


def _episode_lock(ep_id: str):
    from app.services.process_lock import file_lock
    return file_lock(_episode_dir(ep_id)/".produce.lock")


def _with_episode_lock(fn):
    """produce 的 episode 锁装饰器（不改动函数体，降低回归风险）。"""
    import functools

    @functools.wraps(fn)
    def wrapper(ep_id, *a, **kw):
        with _episode_lock(ep_id):
            return fn(ep_id, *a, **kw)

    return wrapper


@_with_episode_lock
def produce_legacy(
    ep_id: str,
    stages: list[str] | None = None,
    script_md: str | None = None,
    tts_fallback: bool = False,
    merge_short: bool = False,
    tts_granularity: str = "paragraph",
    tts_group_chars: int | None = None,
    tts_workers: int | None = None,
    max_images: int = 0,
    budget_id: str | None = None,
    image_only: set[str] | None = None,
) -> dict:
    """
    跑真实生产链（不滥用 rigid 状态枚举空推）；状态由产物真值导出。
      stages: None=全跑适用的；或 ['script','tts','compose','verify'] 子集。
      image_only: 只生成这些 image key（补单张图时用）。None = 全部缺失的。
        此前 stage_images 永远处理全部待生成 key，想补 1 张会被其余缺图
        抢走预算（2026-09-29 caomin-01 实测）。
      tts_granularity: "frame"（**默认**，正确性优先，见 stage_tts 的实测说明）；
        "paragraph" 按段落合成长音频再切回逐帧，句间韵律更好，但长上下文下
        会把「地名/官名+人名」这类紧连专名读断，且 ≥45 字必错。
      tts_fallback: cosyvoice 失败是否静默降级 edge（B2-3）。交付集建议 False。
      merge_short: 合并过短片段（见 stage_script）。**新集建档才开**，
        既有多集开它会让帧数变化 → 音频与 scene_plan 错位。默认 False。
      max_images / budget_id: 生图硬预算。**必填**才允许真正发起图片调用
        （provider 边界本就无预算即拒，这里提前到编排层给出可执行报错，
         并让 CLI 的 --max-images/--budget-id 在 legacy 路径上真正生效）。
        此前 app/cli.py 在 pipeline=legacy-preview 分支里整份替换了 options，
        这两个参数被静默丢弃 —— 同坑 #38（--merge-short 当年也是这样失效的）。
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
    _check_disk_space(_episode_dir(ep_id))
    # 工具链预检必须在**任何付费调用之前**（lishimin-03：59 张图付完才发现
    # ffmpeg 不在 PATH，compose 跑到一半才炸）。要烧字幕故连带查字体。
    _check_toolchain(need_font=True)

    has_script = bool((_load_script_md(ep) or (script_md or "")).strip())
    _can_start_from_empty = (not ep.frames and not ep.shots) and has_script
    problems = validate_schema(ep, allow_empty=_can_start_from_empty)
    if problems:
        raise RuntimeError(
            f"episode {ep.id} schema 不合法，拒绝生产: {problems}")

    # 生图预算：没有显式预算就不允许真正发起调用（provider 边界本就无预算即拒，
    # 这里提前一层，给出「怎么修」而不是让调用栈深处抛 BudgetExceeded）。
    # 注意只在【本轮真的会跑 images】时才要求预算 —— 否则 `--stage shots`/
    # `--stage compose` 这类不生图的排障命令会被无谓地挡在门外。
    _will_generate = stages is None or bool({"assets", "images"} & set(stages))
    _prompts_present = bool(_read_image_prompts(ep.id))
    wants_images = _prompts_present and _will_generate
    if wants_images and (not budget_id or max_images <= 0):
        raise RuntimeError(
            f"episode {ep_id} 有生图任务但未给预算：拒绝发起图片调用。"
            f"请显式传 --budget-id <稳定ID> --max-images <上限>"
            f"（重试也计入额度，故上限应含重试余量）。")
    image_budget = None
    if wants_images:
        from app.services.provider_budget import for_run
        image_budget = for_run(_episode_dir(ep_id) / "production",
                               ["legacy", budget_id], dict(max_images=max_images))
        # 预检容量而非等 dispatch 撞顶：额度不足要在开跑前说清差多少。
        image_budget.check_capacity(image_requests=len(
            [j for j in build_image_jobs(ep_id) if not j.is_done()]))

    # 依赖序（script → assets[=tts→images 串行] → shots → compose → verify）
    order = ["script", "assets", "shots", "compose", "verify"]
    if stages is None:
        runnable = []
        has_assets_work = bool(ep.frames) or _prompts_present
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
            if st == "shots" and not ep.shots and not can_assemble_shots(ep_id):
                # 条件不足就【不排进 runnable】：此前是无操作桩，打印一行后
                # "shots" 仍被记进 stages_done —— 无人值守下这就是假绿。
                print("  [orchestrator] 缺 scene_plan 或底图未齐，跳过 shots"
                      "（装配需人工编写的 scene_plan.json）")
                continue
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
        group_chars=tts_group_chars, workers=tts_workers)
    stage_fn["assets"] = lambda ep: stage_assets(
        ep, tts_fallback=tts_fallback, tts_granularity=tts_granularity,
        tts_group_chars=tts_group_chars, tts_workers=tts_workers,
        budget=image_budget)
    # 生图预算必须一路传到 dispatch 边界。同坑 #38：只加参数不透传 =
    # 参数静默失效，而这次失效的后果是「无预算的付费调用」。
    stage_fn["images"] = lambda ep: stage_images(ep, budget=image_budget, only=image_only)
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
    # 本轮重装了分镜却没重新合成 → 现有成片已陈旧（它对应的是上一版分镜的时长）。
    # 这时跑 verify 只会报「视频总帧数 ≠ Σ镜头帧」——那是 shot_builder 的每镜
    # +250ms 口径与 compose 的「组共享 pad」口径之差（本集实测 1150 帧），
    # 读起来像渲染 bug，其实只是片子过期。直说过期，不制造假故障。
    shots_rebuilt = "shots" in done and "compose" not in done
    if has_frames and has_shots and has_mp4 and shots_rebuilt:
        verify = {"ok": False, "stale": True,
                  "error": f"分镜已重装，但成片 {final.name} 仍是上一版分镜渲染的，"
                           f"已过期。请重跑 --stage compose（再 verify）以对齐时间轴。"}
        ep.status = EpisodeStatus.SCRIPTED
    elif has_frames and has_shots and has_mp4:
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
        ep.status = EpisodeStatus.FAILED
    else:
        ep.status = EpisodeStatus.DRAFT
    save(ep)

    return {"ep_id": ep_id, "status": ep.status.name, "run_ok":ep.status != EpisodeStatus.FAILED,
            "requested_stages":stages, "pipeline_version":"legacy_preview_v1", "production_ready":False, "approved":False, "content_review":"pending", "stages_done": done,
            "verify": verify, "final_mp4": str(final) if has_mp4 else None}


def produce(ep_id, stages=None, script_md=None, *, pipeline="v2", **options):
    """Shared production dispatch. Historical frame rendering requires explicit opt-in."""
    if pipeline == "legacy-preview":
        if stages is None or any(s in ('assets', 'tts', 'images') for s in stages):
            raise ValueError('paid legacy stages are retired; use v2 with an explicit durable budget')
        return produce_legacy(ep_id, stages=stages, script_md=script_md, **options)
    if pipeline != "v2":
        raise ValueError("pipeline must be v2 or legacy-preview")
    from app.services.episode_production import run
    return run(ep_id, stages=stages, script_md=script_md, **options)
