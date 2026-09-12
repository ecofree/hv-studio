"""Shared production encoder, mux and hardware fallback behavior."""

import pytest

pytestmark = pytest.mark.shared

import subprocess
from pathlib import Path
from conftest import NEEDS_FFMPEG

@NEEDS_FFMPEG
def test_mux_audio_uses_configured_codec_not_pcm(tmp_path):
    """mux 音轨必须按 config 编码（aac），-c copy 会把 pcm_s16le 装进 mp4 致静音。"""
    from app.config.config import get_settings

    cfg = get_settings().compose

    # 造最小素材：1s 静音视频(无音轨) + 1s wav
    vid = tmp_path / "v.mp4"
    wav = tmp_path / "a.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=25:duration=1",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-an", "-f", "mp4", str(vid)], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1",
         "-ar", "44100", "-ac", "1", str(wav)], check=True)

    out = tmp_path / "muxed.mp4"
    # 【bug 修复】原版手搓 ffmpeg 命令测的是「ffmpeg 配 -c:a aac 是否工作」（恒真），
    # 没调用被测函数。要真测的是 compose 的 _run_audio_mux 是否把 wav 转成 aac。
    from app.services.compose import _run_audio_mux
    _run_audio_mux(vid, wav, out, cfg.audio_codec, cfg.audio_bitrate)

    codec = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
         str(out)], capture_output=True, text=True).stdout.strip()

    assert codec == cfg.audio_codec, (
        f"mux 音轨应为 {cfg.audio_codec}，实为 {codec!r}。"
        f"若为 pcm_s16le 说明又退回 `-c copy` 直拷 wav → 播放器静音（P0-1 复发）")


def test_x264_fallback_preset_is_valid_for_libx264():
    """libx264 退路 preset 必须来自 config.fallbacks，不得用 nvenc 的 p4。

    p4 是 h264_nvenc 的 preset，libx264 不认 → 无 NVENC 机器走退路时 ffmpeg
    报 Invalid argument、产出 0 字节（实测对照：medium 8333B / p4 0B）。
    """
    from app.services.compose import _build_vcodec

    vcodec = _build_vcodec(False)  # 强制走 libx264 退路
    params = " ".join(str(x) for x in vcodec)
    assert "libx264" in params, params

    preset = vcodec[vcodec.index("-preset") + 1]
    valid_x264 = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow", "placebo"}
    assert preset in valid_x264, (
        f"libx264 退路 preset={preset!r} 非法（合法值 {sorted(valid_x264)}）。"
        f"典型的错：误用 NVENC 的 p4（config.compose.preset），x264 无此值直接失败")
