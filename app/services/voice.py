"""
TTS 语音合成服务（引擎可切换）。

默认引擎：dashscope 阿里云百炼 CosyVoice v3.5-flash，声音设计的中老年沉稳纪录片男声。
回退引擎：edge-tts（微软在线 TTS，免费零 GPU）。

强制：整段生成（不切句拼接），避免机械念清单感。每段(frame)是一个完整叙事，
独立合成一段音频，供下游强对齐/字幕/合成使用。

接口：
  synthesize(frames, engine=...) -> None
    写入 frame.audio_path (16k mono PCM wav) + frame.audio_duration_ms

用法：
  # 命令行（默认 cosyvoice 引擎）
  python -m app.services.voice --episode episode-001
  # 显式回退 edge
  python -m app.services.voice --episode episode-001 --engine edge
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config.config import env_get
from app.models import Frame

# ---------------------------------------------------------------------------
# 引擎常量
# ---------------------------------------------------------------------------
ENGINE_DASHSCOPE = "cosyvoice"   # 主用：阿里云百炼 CosyVoice v3.5
ENGINE_QWEN = "qwen"             # qwen-audio-3.0-tts（免费额度大，
                                 # 设计音色+instruction+hot_fix 都支持）
ENGINE_EDGE = "edge"             # 回退：微软 edge-tts
DEFAULT_ENGINE = ENGINE_DASHSCOPE
# manifest 里「这帧是谁念的」不可考时用它（历史 audio 目录建档早于打标功能）。
# 统计音色一致性时 unknown 单独计数，不冒充 cosyvoice —— 冒充就等于掩盖。
_ENGINE_UNKNOWN = "unknown"

# DashScope 用沉稳纪录片男声（声音设计 voice_prompt）
# 2026-09-09 与 cosyvoice.DEFAULT_VOICE_PROMPT 同步为戏剧化描述（P1-1）。
# ⚠️ 两处必须一致（tests/test_tts_defaults.py 有同步断言）：get_voice_id 的
#    缓存键是 (prefix, target_model) 不含 prompt —— 不一致且 .cache 清空后
#    音色会用错误 prompt 重建（播报腔回归）。
DASHSCOPE_VOICE_PROMPT = (
    "成熟的中年男声，嗓音低沉浑厚、带胸腔共鸣、富有磁性。叙述节奏起伏明显："
    "平铺时舒缓低沉，紧张时语速收紧、语气压低，关键处会刻意放慢并加重，"
    "句末有自然的收束与停顿。情绪饱满、有戏剧张力与压迫感，"
    "适合悬念感强的历史解说，避免一字一顿的播音腔和新闻播报腔。"
)

# edge-tts 中文男声（回退用）
DEFAULT_VOICE_EDGE = "zh-CN-YunxiNeural"     # 云希 - 沉稳青年男声
ALT_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "yunhao": "zh-CN-YunhaoNeural",
}


# ---------------------------------------------------------------------------
# 通用：mp3 → 16k mono PCM wav + 时长
# ---------------------------------------------------------------------------
def _mp3_to_wav(mp3: Path, wav: Path, sample_rate: int = 16000) -> None:
    """ffmpeg 转 16k mono PCM wav（下游按 spec.NARRATION_SR 用 wave 读，需 PCM）。"""
    wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", str(mp3),
        "-ar", str(sample_rate), "-ac", "1", "-acodec", "pcm_s16le",
        str(wav),
    ], check=True, capture_output=True)


def _audio_duration_ms(path: Path) -> int:
    out = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], check=True, capture_output=True, text=True).stdout.strip()
    return int(float(out) * 1000)


# ---------------------------------------------------------------------------
# 幂等指纹（P0-1 修复：旧音频复用要认「文本变了没有」）
# ---------------------------------------------------------------------------
# 背景（审计 P0-1）：此前的幂等只认 wav 文件存在 —— narration_text 改了但旧
# wav 还在 → 静默复用旧音（真实复现过：合并句子后重跑 TTS，frame-000 因 wav
# 已存在而复用 1560ms 旧音频）。修法：给每段旁白一个内容指纹（md5），复用
# 前先比对指纹；文本变了则删旧 wav 重新合成。指纹记录在音频目录的 sidecar
# manifest，避免改动 episodes/{id}.json 的 v1.0 schema（不破坏既有成片契约）。
_MANIFEST_NAME = "_tts_manifest.json"
_FP_NOTE = None  # 惰性：进程内只提示一次 manifest 缺失（向后兼容旧 audio 目录）


def _tts_record_cost(ep_id: str, model: str, chars: int, ok: bool) -> None:
    """TTS 成本台账（2026-09-09 审计）：生图有 cost_ledger，TTS 侧此前**零记录**
    —— 出了 6 遍片子花了多少钱无法回答。TTS 按字符计费，字数现成，先记量。
    """
    import json as _j
    import time as _t
    from pathlib import Path as _P
    p = _P("state/tts_cost_ledger.json")
    try:
        rows = _j.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        rows.append({"ts": _t.strftime("%Y-%m-%d %H:%M:%S"), "ep": ep_id,
                     "model": model, "chars": int(chars), "ok": bool(ok)})
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_j.dumps(rows, ensure_ascii=False, indent=0),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 台账坏了不能阻塞生产
        pass


def _tts_strategy_tag() -> str:
    """TTS 策略指纹：engine + model + 影响产出的关键配置。

    P0-1（2026-09-08 审计）：旧指纹只含文本 —— 切引擎/调 rate/改 SSML 档位后，
    旧音频会被静默复用（当日被迫手动 mv 音频目录 6 次，全是这个缺陷的人工代偿）。
    策略变化 → 指纹变化 → 全量重合成，这才是正确语义。
    """
    try:
        from app.config.config import get_settings
        v = get_settings().get_profile().voice
        eng = (v.engine or DEFAULT_ENGINE)
        # 只把**当前引擎真正会用到的**配置纳入指纹：改 qwen_* 不该让 cosyvoice
        # 的音频失效，反之亦然（否则每次动无关配置都全量重合成）。
        if eng == ENGINE_QWEN:
            parts = [v.qwen_model or "", str(v.qwen_rate or ""),
                     (v.qwen_instruction or "")[:32]]
        else:
            parts = [v.cosyvoice_model or "", (v.instruction or "")[:32]]
        return "|".join([eng]
                         + parts
                         + ["ssml" if getattr(v, "tts_ssml", False) else "",
                            DASHSCOPE_VOICE_PROMPT[:32]])
    except Exception:  # noqa: BLE001 —— 配置读不到就退化为纯文本指纹
        return ""


def _text_fingerprint(text: str) -> str:
    """旁白内容指纹（16 进制前 12 位）= 策略指纹 + 文本指纹。

    ⚠️ 策略（引擎/模型/rate/SSML 档位）参与指纹：策略变了 → 旧音频全部判失效。
    """
    norm = _normalize_numbers(text).strip()
    payload = f"{_tts_strategy_tag()}|{norm}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]


def _manifest_path(base: Path) -> Path:
    return base / _MANIFEST_NAME


def _load_manifest(base: Path) -> dict:
    p = _manifest_path(base)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(base: Path, manifest: dict) -> None:
    """原子写 manifest（.tmp → replace），避免中断留半截。"""
    p = _manifest_path(base)
    try:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=0),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 —— manifest 只是加速/失效判断，坏了不阻塞合成
        pass


# ---------------------------------------------------------------------------
# DashScope CosyVoice 引擎（默认）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TTS 瞬时重试（整改 B2-1，审计 P0-2）
# ---------------------------------------------------------------------------
# 生图在 2026-09-04 就有 transient 分类 + 同渠道重试，TTS 一直没有：一次 429
# 抖动就直接判「失败」→ 走 edge 降级 → **整片中间换个音色**。TTS 同样花钱、
# 抖动同样常见，却被当成了致命错误。
#
# 判据**直接复用** image.is_transient_error（类型→状态码→requests 异常→文案指纹），
# 不在 TTS 侧另写一份分类逻辑 —— 写两份就必然漂移，这是本项目反复踩的坑。
# I 批（2026-09-05）：改走 env_get（.env 优先、真实环境变量兜底）。
# 此前直读 os.environ，这个键写进 .env 是无效的。
_TTS_TRANSIENT_RETRY = int(env_get("HV_TTS_TRANSIENT_RETRY", "2"))

# 并发路数的「按粒度自动取值」（2026-09-07）。
#   逐帧档：一帧一次请求 —— lishimin-01 220 帧 = 220 次。实测 4 路并发突发会撞
#     Throttling.RateQuota（D 档 83 次请求只成功 25 帧，降到 2 才补完），取 2。
#   段落档：请求数少一个数量级（220 帧 → 9 组），可以跑满，取 4。
# 可用 --tts-workers 或 HV_TTS_WORKERS 显式覆盖。
_TTS_WORKERS_FRAME = int(env_get("HV_TTS_WORKERS_FRAME", "2"))
_TTS_WORKERS_PARAGRAPH = int(env_get("HV_TTS_WORKERS_PARAGRAPH", "4"))
_TTS_WORKERS_OVERRIDE = env_get("HV_TTS_WORKERS", "") or None


# 显式关闭「自然语言风格指令」的哨兵。
# 为什么需要：instruction=None 表示「沿用 config.yaml」，而回检失败时
# 要的是「这一组强制不用指令」—— 两者语义不同，不能用 None 兼表。
INSTRUCTION_OFF = object()


def _resolve_workers(workers: int | None, granularity: str) -> int:
    """算出实际并发路数，并**打印出来** —— 静默选值等于埋雷。"""
    if workers is None and _TTS_WORKERS_OVERRIDE:
        try:
            workers = int(_TTS_WORKERS_OVERRIDE)
        except ValueError:
            workers = None
    if workers is not None:
        return max(1, int(workers))
    return (_TTS_WORKERS_FRAME if granularity == "frame"
            else _TTS_WORKERS_PARAGRAPH)


# 破折号（连续 2 个以上）：**必须剥掉**，否则会被念出来。
# 实测（2026-09-08，正确归一化后的成片 ASR）：
#   帧59「…唯一选择——把自己的两个嫡子」→ 听写「…唯一选择。马琳把自己的两个嫡子」
#   帧11「…孤本——」→ 变成硬句号
# 即 `——` 要么被念成杂音，要么变硬停。音频里毫无意义。
# 替换成逗号保留停顿，再归一化连续标点（避免出现「，，」「，。」）。
_TTS_DASH_RUN = re.compile(r"-{2,}|—{2,}|－{2,}")
_TTS_PUNCT_COLLAPSE = [
    (re.compile(r"，\s*，+"), "，"),
    (re.compile(r"，\s*(?=[。！？])"), ""),
    (re.compile(r"(?<=[。！？])\s*，"), ""),
]


def tts_clean(text: str) -> str:
    """送 TTS 前的文本清洗（唯一真源，三处取值点共用）。

    ⚠️ 必须在算文本指纹**之前**调用，否则旧 wav 不会被判失效。
    """
    if not text:
        return text
    out = _TTS_DASH_RUN.sub("，", text)
    for pat, rep in _TTS_PUNCT_COLLAPSE:
        out = pat.sub(rep, out)
    return out.strip()


def frame_tts_text(f) -> str:
    """取一帧真正要送 TTS 的文本（narration_text 优先，已做 polyphone 替换）。"""
    return tts_clean((getattr(f, "narration_text", None) or f.text or "").strip())


def _is_transient(e: BaseException) -> bool:
    """TTS 侧瞬时判定。复用生图的判据链（唯一真源），只补 TTS 特有的空产物。"""
    from app.services.image import is_transient_error
    return is_transient_error(e)


def _tts_backoff(attempt: int) -> float:
    """退避 + jitter：0.5/1/2…8s 封顶。并发 workers 路同时退避时靠 jitter 错峰。"""
    import random
    return min(0.5 * (2 ** attempt), 8.0) * (0.7 + 0.6 * random.random())


def _voice_shaping_defaults() -> dict:
    """
    从 config.yaml 的 profile.voice 读音色塑形默认值。

    兜底全部为 None = 服务端原生音色（即 A 基准：pitch_rate 1.0、无 instruction）。
    ⚠️ 2026-09-03 试听结论：压低音调(pitch<1)会让男声发闷、失真，
       试听了 6 个候选后用户选回 A 基准。所以这里兜底必须是 None，
       不要自作主张加"浑厚"默认值——调音只在 config.yaml 显式配置时生效。
    """
    base: dict = {"pitch_rate": None, "speech_rate": None,
                  "volume": None, "instruction": None}
    # qwen 专属开关不在塑形语义里，单独读
    try:
        base["tts_ssml"] = getattr(
            get_settings().get_profile().voice, "tts_ssml", False)
    except Exception:  # noqa: BLE001
        base["tts_ssml"] = False
    try:
        from app.config.config import get_settings
        v = get_settings().get_profile().voice
        for k in base:
            if hasattr(v, k):
                val = getattr(v, k)
                if val is not None:
                    base[k] = val
    except Exception:  # noqa: BLE001
        pass
    return base


def _pick_instruction(cfg: dict, instruction) -> str | None:
    """None = 沿用 config.yaml；INSTRUCTION_OFF = 强制不用；其余按字面值。"""
    if instruction is INSTRUCTION_OFF:
        return None
    if instruction is None:
        return cfg.get("instruction")
    return instruction


def _synth_cosyvoice(text: str, out_mp3: Path, voice_prompt: str | None = None,
                     pitch_rate: float | None = None,
                     speech_rate: float | None = None,
                     volume: int | None = None,
                     instruction: str | None = None) -> None:
    """
    整段合成→mp3。voice_prompt 默认沉稳纪录片男声。
    音色塑形参数缺省时取 config.yaml profile.voice 的配置
    （pitch_rate<1 压低音调 → 更浑厚，改善"声音薄"）。
    """
    from app.services.cosyvoice import synthesize_dashscope_whole, get_voice_id
    d = _voice_shaping_defaults()
    _model = None
    try:
        from app.config.config import get_settings
        _model = get_settings().get_profile().voice.cosyvoice_model or None
    except Exception:  # noqa: BLE001
        _model = None
    _, _ = synthesize_dashscope_whole(
        text, out_mp3,
        voice_prompt=voice_prompt or DASHSCOPE_VOICE_PROMPT,
        model=_model,
        # SSML 探测：text 以 <speak 开头 → 开启（与 qwen 路径同一判据）
        enable_ssml=text.lstrip().startswith("<speak"),
        response_format="mp3",
        overwrite=True,
        pitch_rate=d["pitch_rate"] if pitch_rate is None else pitch_rate,
        speech_rate=d["speech_rate"] if speech_rate is None else speech_rate,
        volume=d["volume"] if volume is None else volume,
        instruction=_pick_instruction(d, instruction),
    )


# ---------------------------------------------------------------------------
# edge-tts 引擎（回退）
# ---------------------------------------------------------------------------
async def _synth_edge(text: str, out_mp3: Path, voice: str, rate: str, volume: str) -> None:
    import edge_tts
    comm = edge_tts.Communicate(text, voice=voice, rate=rate, volume=volume)
    await comm.save(str(out_mp3))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def synthesize(
    frames: list[Frame],
    engine: str = DEFAULT_ENGINE,
    voice_prompt: str | None = None,   # 仅 cosyvoice 用
    voice: str = DEFAULT_VOICE_EDGE,   # 仅 edge 用
    rate: str = "+0%",
    volume: str = "+0%",
    out_dir: Path | None = None,
    fallback_to_edge: bool = True,     # cosyvoice 失败是否降级 edge
    force: bool = False,               # 强制全部重合成（忽略幂等指纹）
    # None = 按粒度自动取（见 _resolve_workers）。2026-09-07 前写死 4 且没有
    # CLI 开关 —— 默认档退回逐帧后请求数是段落档的 20 倍，4 路必定撞限流。
    workers: int | None = None,
    granularity: str = "paragraph",    # "paragraph" 按段落合成后切回帧 | "frame" 逐帧
    script_md: str | None = None,      # paragraph 模式需要（用来还原段落边界）
    target_chars: int | None = None,   # paragraph 模式的单次目标字数（None=默认 400）
) -> None:
    """
    合成 frames 的配音。每段写入 frame.audio_path(16k wav) + frame.audio_duration_ms。
    engine: "cosyvoice" | "edge"。

    granularity 两档（默认 "frame"，2026-09-07 起）：
      "frame"      逐帧合成，每帧独立请求。**默认**，正确性优先：实测长上下文
                   下 CosyVoice 会把「晋阳令刘文静」断成「晋阳 / 令刘文静」，
                   且 ≥45 字必错；只有 ≈11 字的逐帧档读对。
      "paragraph"  按段落合成长音频（中位 65 字）再按静音切回逐帧 wav，
                   句间韵律更连贯。代价是上面那个断词风险（结构性，非偶发）。

    ⚠️ 切回逐帧是必须的：compose 逐帧读 wav 拼时间线，帧时长还决定每镜时长。
    ⚠️ 段落边界靠 script_md 还原；还原不出（脚本缺失/被改过）→ **安全退化成
       按句边界的字数预算分组**，绝不把帧划进错的段落。

    幂等（P0-1 修复，见模块头注释）：默认对【文本未变】的已合成段复用旧 wav；
    但若本段旁白文本【变了】（指纹不同），则删除旧 wav 重新合成，杜绝
    「narration_text 改了、旧音频被静默复用」的 P0。文本指纹记在音频目录
    `_tts_manifest.json`（frame_id → 内容指纹），不改 episodes/{id}.json schema。
      - 旧 audio 目录（无 manifest）：向后兼容——无法判断文本是否变，按存在复用，
        但首次运行会打一次提示（_FP_NOTE），建议跑一次 --force 建立指纹基线。
      - force=True：忽略指纹与已存在 wav，全部重合成（并重建 manifest）。
    """
    from app.config.config import ROOT
    from app.services import tts_group as _tts_group   # 段落合成 + 静音切片
    base = out_dir or (ROOT / "episodes" / "default" / "audio")
    base.mkdir(parents=True, exist_ok=True)

    engine = engine or DEFAULT_ENGINE
    manifest = {} if force else _load_manifest(base)
    legacy_missing_manifest = not force and not (base / _MANIFEST_NAME).exists()

    import threading
    _man_lock = threading.Lock()
    degraded: list[str] = []      # 本次「请求 cosyvoice、实际用 edge」的帧（B2-2）

    def _render(text: str, wav: Path, tag: str,
                instruction=None) -> tuple[bool, str]:
        """真正发声：cosyvoice 失败可选降级 edge。返回 (是否成功, 实际引擎)。

        used 记录**实际**发声的引擎：降级后它是 edge，与请求的 engine 不同。
        这个标记会写进 manifest，是「本片音色是否一致」的唯一证据（B2-2）。
        """
        ok, used = False, engine
        if engine == ENGINE_QWEN:
            # qwen 的 instruction/hot_fix 来自 config（全局风格），
            # 不接收逐次 instruction 参数（W1 实测：全程一条指令最稳）
            ok = _try_one_qwen(text, wav)
        elif engine == ENGINE_DASHSCOPE:
            if instruction is None:
                ok = _try_one_cosyvoice(text, wav, voice_prompt)
            else:
                ok = _try_one_cosyvoice(text, wav, voice_prompt,
                                       instruction=instruction)
            if not ok and fallback_to_edge:
                print(f"  [voice] {tag} cosyvoice 失败，降级 edge "
                      f"（音色与其余帧不同，已记入 _tts_manifest.json）")
                _one_edge(text, wav, voice, rate, volume)
                ok = wav.exists()
                used = ENGINE_EDGE if ok else engine
        else:  # edge
            _one_edge(text, wav, voice, rate, volume)
            ok = wav.exists()
        return ok, used

    def _synth_one(f: Frame) -> None:
        """单个 frame：幂等判断 + 合成 + 回填。线程独立（各 frame 各 wav 互不干扰）。"""
        text = frame_tts_text(f)
        if not text:
            return
        fp = _text_fingerprint(text)
        wav = base / f"{f.id}.wav"

        # ---- 幂等短路：文本未变 + 已有成品 wav → 复用（force 时跳过） ----
        if not force and wav.exists():
            with _man_lock:
                prev = manifest.get(f.id)
            if prev is not None and prev.get("fp") == fp:
                f.audio_path = str(wav).replace("\\", "/")
                if f.audio_duration_ms is None:
                    f.audio_duration_ms = _audio_duration_ms(wav)
                # 复用：引擎沿用 manifest 记的（可能是上轮的 cosyvoice，
                # 也可能是当时降级过的 edge）—— 这才是「本片音色是否一致」的真值。
                with _man_lock:
                    prev.setdefault("engine", _ENGINE_UNKNOWN)
                return
            if prev is None:
                # 无 manifest（旧目录）：无法判断文本是否变。向后兼容按存在复用，
                # 但旁白文本若真变了这里会静默漏检 —— 靠 _FP_NOTE 提示用户跑 --force。
                global _FP_NOTE
                with _man_lock:
                    if not _FP_NOTE:
                        print("[voice] 检测到旧 audio 目录无 _tts_manifest.json，无法校验"
                              "「文本是否已变」。如需精确幂等请跑一次 --force 建立指纹基线。")
                        _FP_NOTE = True
                f.audio_path = str(wav).replace("\\", "/")
                if f.audio_duration_ms is None:
                    f.audio_duration_ms = _audio_duration_ms(wav)
                with _man_lock:
                    manifest[f.id] = {"fp": fp, "wav": wav.name,
                                      "engine": _ENGINE_UNKNOWN}
                return
            # 文本变了（prev.fp != fp）：删旧 wav，落到下面重新合成
            print(f"  [voice] {f.id} 旁白文本已变(fp {prev.get('fp')}→{fp})，"
                  f"删除旧音频 {wav.name} 重新合成")
            try:
                wav.unlink()
            except Exception:
                pass

        # ---- 真正合成：cosyvoice 失败可选降级 edge ----
        ok, used = _render(text, wav, f.id)
        if not ok:
            raise RuntimeError(f"[voice] {f.id} 合成失败(engine={engine})")
        f.audio_path = str(wav).replace("\\", "/")
        f.audio_duration_ms = _audio_duration_ms(wav)
        with _man_lock:
            manifest[f.id] = {"fp": fp, "wav": wav.name, "engine": used}
            if used != engine:
                degraded.append(f.id)

    def _report_slice_deviation(fs, texts, segs, sr, gid) -> None:
        """切片偏差自检：实际时长 vs 按字数权重的期望，差太多就点名。

        比例估算扛不住成倍的语速差（某段突然慢很多）。真发生了不静默——
        打印出来让人能定位是哪一帧，再决定缩小分组还是接受。
        """
        ws = [_tts_group.piece_weight(t) for t in texts]
        tot = sum(ws) or 1.0
        real = sum(len(s) for s in segs) / sr * 1000
        bad = []
        for f, w, s in zip(fs, ws, segs):
            want = real * w / tot
            got = len(s) / sr * 1000
            if want > 0 and not (0.66 * want <= got <= 1.5 * want):
                bad.append(f"{f.id} 期望{want:.0f}ms 实得{got:.0f}ms")
        if bad:
            print(f"  [voice][告警] 段落 {gid} 切片偏差过大（TTS 语速与字数权重"
                  f"不成比例）：{bad[:5]}{' …' if len(bad) > 5 else ''}")
            print("  [voice][提示] 可减小 --tts-group-chars，或听一下那几帧是否"
                  "被切在词中间")

    def _synth_group(idxs: list[int]) -> None:
        """段落模式：一组帧合成**一次**，再按静音切回逐帧 wav。

        幂等粒度仍是**帧**：组内所有帧 wav 都在且指纹未变 → 整组跳过。
        任一片段文本变了 → 整组重合成（段落级音频不能只补一帧）。
        """
        if len(idxs) == 1:
            return _synth_one(frames[idxs[0]])

        fs = [frames[i] for i in idxs]
        texts = [frame_tts_text(f) for f in fs]
        if any(not t for t in texts):        # 空文本无法参与拼接 → 退回逐帧
            for i in idxs:
                _synth_one(frames[i])
            return
        fps = [_text_fingerprint(t) for t in texts]
        wavs = [base / f"{f.id}.wav" for f in fs]

        if not force and all(w.exists() for w in wavs) and all(
                (manifest.get(f.id) or {}).get("fp") == fp
                for f, fp in zip(fs, fps)):
            for f, w in zip(fs, wavs):
                f.audio_path = str(w).replace("\\", "/")
                if f.audio_duration_ms is None:
                    f.audio_duration_ms = _audio_duration_ms(w)
            return

        if _voice_shaping_defaults().get("tts_ssml", False):
            gtext = build_group_ssml(texts)      # 逐句 rate + phoneme（引擎无关）
        else:
            gtext = _tts_group.group_text(texts)
        # 段落 wav 是中间产物，落在 audio/ 下（hv clean --build 会收走）
        gwav = base / f"_grp_{idxs[0]:05d}.wav"
        ok, used = _render(gtext, gwav, f"grp{idxs[0]:05d}({len(fs)}帧)")
        if not ok:
            raise RuntimeError(f"[voice] 段落 {idxs[0]} 合成失败(engine={engine})")

        # ---- 发音回检 ----
        # 根因（2026-09-08 实测，可复现）：`instruction` 会让模型在每组
        # 首句「进入角色」时失稳，句首若是生僻专名就被念糊
        # （「尉迟恭在后院」→「尉迟恭为后院」）。同一个文本重合成结果完全一致，
        # 所以能被稳定检出 → 检出后这一组去掉指令重合成即可。
        v_cfg = _voice_shaping_defaults()
        _cfg_instr = v_cfg.get("instruction")
        if engine == ENGINE_DASHSCOPE and _cfg_instr:
            from app.services import asr_check as _ac
            _good, _why = _ac.check(gwav, gtext)
            if not _good:
                # 两种不合格，处置不同：
                #  · 英文串 = 硬信号（不依赖阈值，中文旁白里绝不该有英文）
                #    → **必定**去掉指令重合成
                #  · 仅 PER 超限 = 软信号（阈值未标定，可能是同音误判）
                #    → 默认只告警；开启 HV_ASR_DOWNGRADE=1 才降级
                _hard = _ac.suspicious(_ac.transcribe(gwav) or "")
                if not (_hard or _ac.downgrade_enabled()):
                    print(f"  [voice][告警] 段落 {idxs[0]} 发音回检：{_why}")
                else:
                    print(f"  [voice] 段落 {idxs[0]} 回检不合格（{_why}）"
                          f"→ 去掉风格指令重合成")
                    ok, used = _render(
                        gtext, gwav, f"grp{idxs[0]:05d}(无指令)",
                        instruction=INSTRUCTION_OFF)
                    if not ok:
                        raise RuntimeError(
                            f"[voice] 段落 {idxs[0]} 回检后重合成失败")
                    _good2, _why2 = _ac.check(gwav, gtext)
                    print(f"  [voice] 段落 {idxs[0]} 重合成后：{_why2}")

        data, sr = _tts_group.read_wav(gwav)
        segs = _tts_group.slice_samples(
            data, sr, [_tts_group.piece_weight(t) for t in texts])
        if len(segs) != len(fs):            # 理论上不会发生，兜底免得静默错位
            raise RuntimeError(
                f"[voice] 段落 {idxs[0]} 切片数 {len(segs)} != 帧数 {len(fs)}")

        gw = _report_slice_deviation(fs, texts, segs, sr, idxs[0])
        for f, seg, w in zip(fs, segs, wavs):
            _tts_group.write_wav(w, seg, sr)
            f.audio_path = str(w).replace("\\", "/")
            f.audio_duration_ms = int(round(len(seg) / sr * 1000))
        with _man_lock:
            for f, fp in zip(fs, fps):
                manifest[f.id] = {"fp": fp, "wav": f"{f.id}.wav", "engine": used}
            if used != engine:
                degraded.extend(f.id for f in fs)

    # 分组：paragraph 模式按段落，frame 模式退化成逐帧
    if granularity == "paragraph":
        tc = _tts_group.TARGET_GROUP_CHARS if target_chars is None else target_chars
        groups = _tts_group.plan_groups(frames, script_md, target_chars=tc)
        n_par = sum(1 for g in groups if len(g) > 1)
        if n_par:
            print(f"  [voice] 段落级合成：{len(frames)} 帧 → {len(groups)} 组"
                  f"（{n_par} 组为多帧段落）")
        else:
            print("  [voice] 段落级合成：还原不出段落边界，退回「按句边界的字数预算」"
                  "分组（仍会把碎片并长，只是不保证贴着段落）")
        todo = groups
        runner = _synth_group
    else:
        todo = [[i] for i in range(len(frames))]
        runner = _synth_group

    # 并发合成（workers 路）。幂等段近乎瞬时；真合成段打 DashScope/edge。
    n = max(1, min(_resolve_workers(workers, granularity), len(todo)))
    print(f"  [voice] 并发 {n} 路（粒度={granularity}，--tts-workers 可覆盖）")
    if n <= 1 or len(todo) <= 1:
        for g in todo:
            runner(g)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            # 任一组失败 → futures 传播首个异常（保持原「失败即抛」语义）
            list(ex.map(runner, todo))

    # 收口：落 manifest。文本已删的段不残留旧指纹（manifest 只保留本次 frames 覆盖的键）。
    if not legacy_missing_manifest or force:
        # engine 必须跟着走：这段重建若丢掉它，「本片音色是否一致」就查无实据了。
        keep = {}
        for f in frames:
            t = frame_tts_text(f)
            if not t:
                continue
            prev = manifest.get(f.id) or {}
            keep[f.id] = {
                "fp": _text_fingerprint(t),
                "wav": f"{f.id}.wav",
                "engine": prev.get("engine", _ENGINE_UNKNOWN),
            }
        _save_manifest(base, keep)
    else:
        # 旧目录首次运行（无 manifest）：把「本次遇到的既有成品」指纹写回，建立基线
        _save_manifest(base, manifest)

    # 音色一致性汇总（B2-2）：宁可吵，不可静默。降级进成片而无人知晓，
    # 是那种「发出去才发现、且无法挽回」的错误。
    if degraded:
        print(f"  [voice][告警] 本集有 {len(degraded)} 帧因 cosyvoice 失败改用 "
              f"edge 合成，音色与其余帧不同：{degraded[:10]}"
              f"{' …' if len(degraded) > 10 else ''}")
        print(f"  [voice][告警] 建议修好后对该集跑 --force 重合成；"
              f"交付集请改用 --no-fallback 让失败直接暴露。")
    return engine_report(base)


def engine_report(base: "Path | str") -> dict:
    """统计音频目录里各引擎的帧数——「本片音色是否一致」的只读体检。

    返回 {"total": n, "by_engine": {引擎: 帧数}, "mixed": bool,
          "degraded": [实际非主引擎的帧 id...]}。
    mixed=True 意味着这部片子里混了至少两种声音，成片前应处理。
    """
    from pathlib import Path as _P
    b = _P(base)
    man = _load_manifest(b)
    by: dict[str, int] = {}
    for _k, v in man.items():
        if isinstance(v, dict):
            by[v.get("engine", _ENGINE_UNKNOWN)] = \
                by.get(v.get("engine", _ENGINE_UNKNOWN), 0) + 1
    # 主引擎 = 占帧数最多的引擎（而非写死的 cosyvoice）：
    # 2026-09-08 起整集可整体跑在 qwen 引擎上，220/220 全是 qwen 是
    # 「音色一致」，旧逻辑（main=cosyvoice）会误报「成片会中途换人声」。
    main = (max(by.items(), key=lambda kv: kv[1])[0] if by else ENGINE_DASHSCOPE)
    degraded = [k for k, v in man.items()
                if isinstance(v, dict) and v.get("engine", _ENGINE_UNKNOWN) != main]
    return {
        "total": len(man),
        "by_engine": by,
        "mixed": len([k for k in by if k != main]) > 0,
        "degraded": sorted(degraded),
    }


# ---------------------------------------------------------------------------
# SSML 生成（qwen 引擎，W5 路线 2026-09-08 定稿）
# ---------------------------------------------------------------------------
# 设计原则（全部来自当日实测）：
#   · 起伏只用 rate —— 实测单调可复现（1.0→6.0s / 0.8→6.8s / 0.6→7.9s）；
#     pitch 的输出不跟随标注值（尾部 F0 97~172Hz 乱跳），**绝不用**。
#   · 多音字用 <phoneme> —— 拼音+声调直接钉死，取代 cosyvoice 的同音字替换。
#   · 不用 instruction —— 它是整段提示，模型自由发挥 → 组间 F0 88~186Hz 漂移。
#   · 不用音质重铸型标签（[whispers]/[tired]/[like dracula]）—— qwen 实测
#     [whispers] 让 F0 飙到 186Hz，听感即"换了个人"。

# 历史读音表（词 → 拼音，空格分隔，数目必须与字数一致）。
# ⚠️ 顺序无关（匹配时按词长倒序），但**避免同形异音词**：「万石」安全
#    （磨刀石不含"万石"子串），单字「石」绝不入表。
_SSML_PHONEMES: dict[str, str] = {
    "尉迟": "yu4 chi2", "长孙": "zhang3 sun1", "可汗": "ke4 han2",
    "单于": "chan2 yu2", "冒顿": "mo4 du2", "月氏": "yue4 zhi1",
    "龟兹": "qiu1 ci2", "大宛": "da4 yuan1", "康居": "kang1 qu2",
    "乌孙": "wu1 sun1", "身毒": "juan1 du2", "阏氏": "yan1 zhi1",
    "吐蕃": "tu3 bo1", "回鹘": "hui2 hu2", "高句丽": "gao1 gou1 li2",
    "吐谷浑": "tu3 yu4 hun2", "柔然": "rou2 ran2", "突厥": "tu1 jue2",
    "鲜卑": "xian1 bei1", "契丹": "qi4 dan1", "休屠": "xiu1 chu2",
    "昆邪": "hun2 ye2", "先零": "xian1 lian2", "郦食其": "li4 yi4 ji1",
    "伍子胥": "wu3 zi3 xu1", "范雎": "fan4 ju1", "乐毅": "yue4 yi4",
    "曹参": "cao2 can1", "华佗": "hua4 tuo2", "李悝": "li3 kui1",
    "呼韩邪": "hu1 han2 ye2", "扁鹊": "bian3 que4", "马邑": "ma3 yi4",
    "焉耆": "yan1 qi2", "氐族": "di1 zu2", "羌族": "qiang1 zu2",
    "羯族": "jie2 zu2", "骑都尉": "ji4 du1 wei4", "万石": "wan4 dan4",
}
_SSML_SENT_SPLIT = re.compile(r"(?<=[。！？；…])")
# ⚠️ 2026-09-09 实测：cosyvoice-v3.5-flash 的天然语速 ~3.3 字/秒，比 qwen 慢。
#    SSML rate 0.92 会让长句撞 8s 硬顶（30 字 → 9.8s），整片也拖到 18.8min。
#    故整体上调：base 1.05 / 感叹 1.12 / 疑问 1.05 / 组末 0.98，
#    长句（≥25 字）额外 +0.30。组合效果 ≈ 4.3 字/秒（正常解说语速）。
_SSML_RATE_DEFAULT = 1.05
_SSML_RATE_EXCL = 1.12      # 感叹
_SSML_RATE_QUES = 1.05      # 疑问
_SSML_RATE_GROUP_TAIL = 0.98  # 组末收束（略慢，留悬念）


def _ssml_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _apply_phonemes(sent: str) -> str:
    for word in sorted(_SSML_PHONEMES, key=len, reverse=True):
        if word in sent:
            sent = sent.replace(
                word,
                f'<phoneme alphabet="py" ph="{_SSML_PHONEMES[word]}">{word}</phoneme>')
    return sent


def build_group_ssml(texts: list[str]) -> str:
    """一组帧的文本 → SSML（逐句 <speak rate> + <phoneme>）。

    策略（确定性，无随机）：
      · 基础语速 0.92；感叹句 0.96；疑问句 0.90；组末句 0.86（收束留悬念）
      · 句子按 。！？；… 切分，一句一个 <speak>（可并列、不可嵌套）
    """
    parts: list[str] = []
    n = len(texts)
    for gi, t in enumerate(texts):
        sents = [x for x in _SSML_SENT_SPLIT.split(t) if x.strip()]
        for si, sent in enumerate(sents):
            rate = _SSML_RATE_DEFAULT
            if sent.rstrip().endswith("！"):
                rate = _SSML_RATE_EXCL
            elif sent.rstrip().endswith("？"):
                rate = _SSML_RATE_QUES
            # 长句自动提速：0.92 档下 ≥25 字会超过单镜 8s 硬顶
            # （实测 30 字 ≈ 8.1s → 209 帧 > 200）。组末句让位给收束档。
            _plain = len(re.sub(r"[，。！？；：、…—\s“”（）《》·]", "", sent))
            if _plain >= 25 and not (gi == n - 1 and si == len(sents) - 1):
                rate = min(1.6, rate + 0.30)
            if gi == n - 1 and si == len(sents) - 1:
                rate = _SSML_RATE_GROUP_TAIL
            # 先转义原文，再插 phoneme 标签（顺序反了会把标签一起转义掉）
            body = _apply_phonemes(_ssml_escape(sent.strip()))
            parts.append(f'<speak rate="{rate:.2f}">{body}</speak>')
    return "".join(parts)


def _try_one_qwen(text: str, wav: Path,
                  retries: int | None = None) -> bool:
    """qwen-audio-3.0-tts 整段合成并转 wav（带瞬时重试）。

    instruction / hot_fix 从 config（profile.voice）读取：
      qwen_instruction / qwen_hot_fix。voice 由 qwen_model 对应的设计音色给出。
    """
    n = _TTS_TRANSIENT_RETRY if retries is None else max(0, int(retries))
    last: BaseException | None = None
    from app.config.config import get_settings
    from app.services.cosyvoice import get_voice_id
    v = get_settings().get_profile().voice
    model = v.qwen_model or "qwen-audio-3.0-tts-plus"
    voice = get_voice_id(model, DASHSCOPE_VOICE_PROMPT)
    # P1-5（审计）：读音修复单一真源 = _SSML_PHONEMES（SSML phoneme 与
    # hot_fix 共用一张表），config 里只保留**短语级** replace（量词类）。
    hf_cfg = v.qwen_hot_fix or {}
    hot_fix = {
        "pronunciation": [{w: py} for w, py in _SSML_PHONEMES.items()],
        "replace": hf_cfg.get("replace", []),
    }
    for attempt in range(n + 1):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_mp3 = Path(tmp) / "tts.mp3"
                from app.services.cosyvoice import synthesize_qwen_whole
                synthesize_qwen_whole(
                    text, tmp_mp3, voice=voice, model=model,
                    instruction=v.qwen_instruction,
                    hot_fix=hot_fix, rate=v.qwen_rate,
                    enable_ssml=text.lstrip().startswith("<speak"),
                    overwrite=True)
                if not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0:
                    raise RuntimeError("qwen-tts 返回空音频(empty body)")
                _mp3_to_wav(tmp_mp3, wav)
            if wav.exists():
                return True
            last = RuntimeError("qwen-tts 合成后未生成 wav")
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            # 配额耗尽是**致命**错误：重试无意义，且要把原因说人话
            if "FreeTierOnly" in msg or "Free quota exhausted" in msg                     or ("403" in msg and "quota" in msg.lower()):
                print(f"  [voice][额度耗尽] {model} 免费额度已用完"
                      f"（HTTP 403 FreeTierOnly）。"
                      f"处理：①控制台充值；②切换 qwen_model 到有额度的档位；"
                      f"③等免费额度刷新。本次 TTS 终止。")
                return False
            if _is_transient(e) and attempt < n:
                time.sleep(_tts_backoff(attempt + 1))
                continue
            break
    if last is not None:
        print(f"  [voice] qwen-tts 失败: {type(last).__name__}: {str(last)[:160]}")
    return False


def _try_one_cosyvoice(text: str, wav: Path, voice_prompt: str | None,
                       retries: int | None = None,
                       instruction=None) -> bool:
    """CosyVoice 整段合成并转 wav。成功 True；瞬时故障先原地重试再判失败。

    B2-1 之前：任何异常都立即 return False → 调用方走 edge 降级 → 音色突变。
    一次网络抖动换来一部片子里换个人说话，代价与故障完全不成比例。
    现在：瞬时（429/5xx/超时/连接重置/空产物）退避重试 ≤retries 次仍失败才判负；
    致命错误（鉴权失败、参数非法）一次都不重试 —— 重试也必然失败，只是拖慢。
    """
    n = _TTS_TRANSIENT_RETRY if retries is None else max(0, int(retries))
    last: BaseException | None = None
    for attempt in range(n + 1):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_mp3 = Path(tmp) / "tts.mp3"
                _synth_cosyvoice(text, tmp_mp3, voice_prompt,
                                 instruction=instruction)
                if not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0:
                    # 空产物按瞬时处理：中转偶发回 200 但 body 为空，重试通常就好
                    raise RuntimeError("cosyvoice 返回空音频(empty body)")
                _mp3_to_wav(tmp_mp3, wav)
            if wav.exists():
                return True
            last = RuntimeError("cosyvoice 合成后未生成 wav")
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_transient(e) or attempt >= n:
                break
            wait = _tts_backoff(attempt)
            print(f"  [voice] cosyvoice 瞬时故障，{wait:.1f}s 后重试 "
                  f"{attempt + 1}/{n}: {type(e).__name__}: {e}")
            time.sleep(wait)
    print(f"  [voice] cosyvoice 异常: {last}")
    return False


def _one_edge(text: str, wav: Path, voice: str, rate: str, volume: str) -> None:
    """edge-tts 合成并转 wav。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_mp3 = Path(tmp) / "tts.mp3"
        asyncio.run(_synth_edge(text, tmp_mp3, voice, rate, volume))
        _mp3_to_wav(tmp_mp3, wav)


def settings_root() -> Path:
    from app.config.config import ROOT
    return ROOT


# ---------------------------------------------------------------------------
# 数字规范化
# ---------------------------------------------------------------------------
_CN_DIGITS = "零一二三四五六七八九"


def _normalize_numbers(text: str) -> str:
    """阿拉伯数字 → 中文（年份按位读，历史类更自然）。"""
    text = re.sub(r"\b(\d{4})\b", lambda m: "".join(_CN_DIGITS[int(c)] for c in m.group(1)), text)
    return text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", required=True)
    ap.add_argument("--engine", choices=[ENGINE_DASHSCOPE, ENGINE_QWEN, ENGINE_EDGE],
                    default=DEFAULT_ENGINE, help="合成引擎")
    ap.add_argument("--voice", default=DEFAULT_VOICE_EDGE, help="edge 引擎音色")
    ap.add_argument("--rate", default="+0%")
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--no-fallback", action="store_true",
                    help="cosyvoice 失败时不做 edge 降级")
    ap.add_argument("--force", action="store_true",
                    help="强制全部重合成（忽略幂等指纹，重建 _tts_manifest.json）")
    args = ap.parse_args()

    from app.managers import load, save, episode_path
    ep = load(args.episode)
    if not ep.frames:
        print(f"[!] episode {args.episode} 没有 frame，先跑 script")
        return
    # 音频目录 = episodes/{id}/audio（与 m2_tts.py 一致）
    out_dir = episode_path(args.episode).parent / "audio"
    print(f"合成 {len(ep.frames)} 段，engine={args.engine}"
          + ("，--force" if args.force else ""))
    synthesize(
        ep.frames,
        engine=args.engine,
        voice=args.voice,
        rate=args.rate,
        volume=args.volume,
        out_dir=out_dir,
        fallback_to_edge=not args.no_fallback,
        force=args.force,
    )
    save(ep)
    # TTS 成本台账：按合成字符数记量（生图有 cost_ledger，TTS 此前零记录）
    try:
        from app.config.config import get_settings as _gs
        _v = _gs().get_profile().voice
        _model = (_v.qwen_model if (getattr(_v, "engine", None) == "qwen")
                  else None) or _cosyvoice_model_from_env()
        _tts_record_cost(ep_id, _model,
                         sum(len(frame_tts_text(f)) for f in ep.frames), True)
    except Exception:  # noqa: BLE001
        pass

    total = sum(f.audio_duration_ms or 0 for f in ep.frames)
    print(f"完成：总时长 {total/1000:.1f}s")
    for f in ep.frames[:5]:
        print(f"  {f.id}  {(f.audio_duration_ms or 0)/1000:.2f}s  {f.audio_path}")


if __name__ == "__main__":
    main()
