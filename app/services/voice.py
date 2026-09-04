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
import re
import subprocess
import tempfile
from pathlib import Path

from app.models import Frame

# ---------------------------------------------------------------------------
# 引擎常量
# ---------------------------------------------------------------------------
ENGINE_DASHSCOPE = "cosyvoice"   # 主用：阿里云百炼 CosyVoice v3.5-flash
ENGINE_EDGE = "edge"             # 回退：微软 edge-tts
DEFAULT_ENGINE = ENGINE_DASHSCOPE

# DashScope 用沉稳纪录片男声（声音设计 voice_prompt）
DASHSCOPE_VOICE_PROMPT = (
    "一位沉稳的中年男声播音员，声音低沉浑厚、富有磁性，语速平稳，吐字清晰，"
    "适合历史纪录片旁白或新闻播报。"
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
    """ffmpeg 转 16k mono PCM wav（下游 alignment 用 wave 模块读，需 PCM）。"""
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


def _text_fingerprint(text: str) -> str:
    """旁白内容指纹（16 进制前 12 位）。用于判断「同一句是否已按当前文本合成」。"""
    norm = _normalize_numbers(text).strip()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


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
    from app.services.cosyvoice import synthesize_dashscope_whole
    d = _voice_shaping_defaults()
    _, _ = synthesize_dashscope_whole(
        text, out_mp3,
        voice_prompt=voice_prompt or DASHSCOPE_VOICE_PROMPT,
        response_format="mp3",
        overwrite=True,
        pitch_rate=d["pitch_rate"] if pitch_rate is None else pitch_rate,
        speech_rate=d["speech_rate"] if speech_rate is None else speech_rate,
        volume=d["volume"] if volume is None else volume,
        instruction=d["instruction"] if instruction is None else instruction,
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
    workers: int = 4,                  # 并发合成路数（2026-09-04：此前串行 14.5min/365 帧）
) -> None:
    """
    逐 frame 合成。每段写入 frame.audio_path(16k wav) + frame.audio_duration_ms。
    engine: "cosyvoice" | "edge"。

    幂等（P0-1 修复，见模块头注释）：默认对【文本未变】的已合成段复用旧 wav；
    但若本段旁白文本【变了】（指纹不同），则删除旧 wav 重新合成，杜绝
    「narration_text 改了、旧音频被静默复用」的 P0。文本指纹记在音频目录
    `_tts_manifest.json`（frame_id → 内容指纹），不改 episodes/{id}.json schema。
      - 旧 audio 目录（无 manifest）：向后兼容——无法判断文本是否变，按存在复用，
        但首次运行会打一次提示（_FP_NOTE），建议跑一次 --force 建立指纹基线。
      - force=True：忽略指纹与已存在 wav，全部重合成（并重建 manifest）。
    """
    from app.config.config import ROOT
    base = out_dir or (ROOT / "episodes" / "default" / "audio")
    base.mkdir(parents=True, exist_ok=True)

    engine = engine or DEFAULT_ENGINE
    manifest = {} if force else _load_manifest(base)
    legacy_missing_manifest = not force and not (base / _MANIFEST_NAME).exists()

    import threading
    from concurrent.futures import ThreadPoolExecutor
    _man_lock = threading.Lock()

    def _synth_one(f: Frame) -> None:
        """单个 frame：幂等判断 + 合成 + 回填。线程独立（各 frame 各 wav 互不干扰）。"""
        text = (f.narration_text or f.text or "").strip()
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
                    manifest[f.id] = {"fp": fp, "wav": wav.name}
                return
            # 文本变了（prev.fp != fp）：删旧 wav，落到下面重新合成
            print(f"  [voice] {f.id} 旁白文本已变(fp {prev.get('fp')}→{fp})，"
                  f"删除旧音频 {wav.name} 重新合成")
            try:
                wav.unlink()
            except Exception:
                pass

        # ---- 真正合成：cosyvoice 失败可选降级 edge ----
        ok = False
        if engine == ENGINE_DASHSCOPE:
            ok = _try_one_cosyvoice(text, wav, voice_prompt)
            if not ok and fallback_to_edge:
                print(f"  [voice] {f.id} cosyvoice 失败，降级 edge")
                _one_edge(text, wav, voice, rate, volume)
                ok = wav.exists()
        else:  # edge
            _one_edge(text, wav, voice, rate, volume)
            ok = wav.exists()

        if not ok:
            raise RuntimeError(f"[voice] {f.id} 合成失败(engine={engine})")
        f.audio_path = str(wav).replace("\\", "/")
        f.audio_duration_ms = _audio_duration_ms(wav)
        with _man_lock:
            manifest[f.id] = {"fp": fp, "wav": wav.name}

    # 并发合成（workers 路）。幂等段近乎瞬时；真合成段打 DashScope/edge。
    n = max(1, min(workers, len(frames)))
    if n <= 1 or len(frames) <= 1:
        for f in frames:
            _synth_one(f)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            # 任一帧失败 → futures 传播首个异常（保持原「失败即抛」语义）
            list(ex.map(_synth_one, frames))

    # 收口：落 manifest。文本已删的段不残留旧指纹（manifest 只保留本次 frames 覆盖的键）。
    if not legacy_missing_manifest or force:
        keep = {f.id: {"fp": _text_fingerprint((f.narration_text or f.text or "").strip()),
                       "wav": f"{f.id}.wav"}
                for f in frames if (f.narration_text or f.text or "").strip()}
        _save_manifest(base, keep)
    else:
        # 旧目录首次运行（无 manifest）：把「本次遇到的既有成品」指纹写回，建立基线
        _save_manifest(base, manifest)


def _try_one_cosyvoice(text: str, wav: Path, voice_prompt: str | None) -> bool:
    """CosyVoice 整段合成并转 wav。成功 True。"""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_mp3 = Path(tmp) / "tts.mp3"
            _synth_cosyvoice(text, tmp_mp3, voice_prompt)
            if not tmp_mp3.exists() or tmp_mp3.stat().st_size == 0:
                return False
            _mp3_to_wav(tmp_mp3, wav)
        return wav.exists()
    except Exception as e:  # noqa: BLE001
        print(f"  [voice] cosyvoice 异常: {e}")
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
    ap.add_argument("--engine", choices=[ENGINE_DASHSCOPE, ENGINE_EDGE],
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
    total = sum(f.audio_duration_ms or 0 for f in ep.frames)
    print(f"完成：总时长 {total/1000:.1f}s")
    for f in ep.frames[:5]:
        print(f"  {f.id}  {(f.audio_duration_ms or 0)/1000:.2f}s  {f.audio_path}")


if __name__ == "__main__":
    main()
