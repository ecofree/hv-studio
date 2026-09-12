"""
背景音乐服务（M1 实装：极简版）。

设计取舍（关键决策）：
  - 资产位置约定：assets/bgm/{profile_name}.mp3，无文件则 pick_bgm 返回 None，
    compose 优雅跳过。降低用户接入门槛：放文件即生效，无需改 config。
  - 不做侧链 duck（sidechaincompress）：成本高（VAD + 多 filter 链），
    对竖屏短视频收益有限。改用「配置恒定低音量 + afade 头尾」方案，
    行业惯例（纪录片/解说）。bgm.volume（默认 0.18）控制总音量。
  - 不引入 VAD：300s 视频 sidechain 需要先跑 webrtcvad/silero 找语音段，
    推迟到后期若用户反馈人声被压盖再做。

公开 API：
  - pick_bgm(profile_name, base_dir=None) → Path | None
  - mix_voice_with_bgm(voice_wav, bgm_path, total_ms, volume, fade_out_sec,
                       bgm_fade_in_sec=2, sample_rate=44100, channels=1) → Path
    返回混合 wav 路径（写入 /tmp/voice_bgm_mix.wav 或 caller 指定 dir）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _project_root() -> Path:
    """项目根单一真源：config.ROOT（勿硬编码盘符）。"""
    try:
        from app.config.config import ROOT
        return ROOT
    except Exception:
        return Path(__file__).resolve().parent.parent.parent


def pick_bgm(profile_name: str, base_dir: str | Path | None = None) -> Path | None:
    """按约定路径查找 BGM 资产：无文件 → 返回 None（不抛错，compose 优雅跳过）。

    约定：{base_dir}/assets/bgm/{profile_name}.mp3
    支持 .wav/.mp3/.m4a/.flac 扩展名，按优先级 mp3 → wav → m4a → flac。
    """
    base = Path(base_dir) if base_dir else _project_root()
    candidates = [".mp3", ".wav", ".m4a", ".flac"]
    bgm_dir = base / "assets" / "bgm"
    if not bgm_dir.exists():
        return None
    for ext in candidates:
        p = bgm_dir / f"{profile_name}{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def mix_voice_with_bgm(
    voice_wav: str | Path,
    bgm_path: str | Path,
    total_ms: int,
    volume: float = 0.18,
    fade_out_sec: int = 5,
    bgm_fade_in_sec: int = 2,
    sample_rate: int = 44100,
    channels: int = 1,
    out_wav: str | Path | None = None,
) -> Path:
    """混合 voice + BGM 为单轨 wav。ffmpeg 一次性合成（不引入中间文件）。

    算法：
      1. BGM 用 -stream_loop -1 循环到 >= total 时长，再 trim 到总时长
      2. afade=t=in:st=0:d={fade_in} 头淡入；afade=t=out:st={end-fade_out}:d={fade_out} 尾淡出
      3. volume={volume}（来自 config profile.bgm）
      4. amix=inputs=2:duration=first:dropout_transition=0（人声为主轨）
      5. aresample 到目标采样率/声道

    返回：写入并返回 wav 路径。
    """
    voice = Path(voice_wav)
    bgm = Path(bgm_path)
    if not voice.exists():
        raise FileNotFoundError(f"voice wav not found: {voice}")
    if not bgm.exists():
        raise FileNotFoundError(f"bgm file not found: {bgm}")
    total_sec = total_ms / 1000.0
    if total_sec <= 0:
        raise ValueError(f"total_ms must be > 0, got {total_ms}")
    if out_wav is None:
        out_wav = voice.parent / "voice_bgm_mix.wav"
    out = Path(out_wav)

    # ffmpeg filter chain:
    #   [1:a] loop → trim to total_sec → afade in/out → volume → [bgm]
    #   [0:a] (voice, untouched) → [voice]
    #   [bgm][voice] amix=inputs=2:duration=first:dropout_transition=0 → [mix]
    fade_out_start = max(0, total_sec - fade_out_sec)
    filter_complex = (
        f"[1:a]atrim=0:{total_sec:.6f},"
        f"afade=t=in:st=0:d={bgm_fade_in_sec},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_sec},"
        f"volume={volume}[bgm];"
        f"[0:a]aresample={sample_rate}[voice];"
        f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95:level=0:latency=1[mix]"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", filter_complex,
        "-map", "[mix]",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(out),
    ]
    # Check ffmpeg available
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not on PATH (required for BGM mixing)")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(
            f"voice+BGM mix failed (rc={r.returncode}): {r.stderr[-600:]}"
        )
    if not out.exists() or out.stat().st_size < 100:
        raise RuntimeError(f"mixed wav missing or too small: {out}")
    return out
