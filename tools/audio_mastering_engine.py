#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hv-studio 全局广播级音频母带重构与一致性母带处理引擎 (Audio Mastering Engine)
【方案 B+/C 生产级声学标准固化模块】

核心能力：
1. 频谱重塑滤波 (Spectral Shaping EQ)：
   - 75Hz 高通切除次低频机械杂音；
   - 180Hz +2.5dB 注入恒定胸腔共鸣低频；
   - 3200Hz -4.0dB 压制尖锐齿音与金属共鸣；
   - 7200Hz 低通统一声学天花板；
   - acompressor 软光电压缩平滑发音起伏；
   - loudnorm 严格锁定 EBU-R128 (-16.0 LUFS) 广播级响度。
2. 自适应环境微底噪无缝填充 (Adaptive Room Tone Infill · -65dB ~ -70dB)：
   - 杜绝硬插数字绝对零静音（消除抽真空断气感）；
   - 杜绝写死 -55dB 粉红噪声（消除 Noise Pumping 噪声呼吸效应）；
   - 自动采样或合成与切片原生底噪匹配的微弱室内温和底噪。
3. 35ms 等功率余弦交叉缝合 (Equal-Power Cosine Crossfade · 35ms)：
   - 消除切片接缝处任何 Pop/Click 直流跳变。
"""

import os
import sys
import wave
import subprocess
import io
import numpy as np
from pathlib import Path

FFMPEG_PATH = Path(r"D:\hv-studio\tools\ffmpeg\bin\ffmpeg.exe")

MASTER_SHAPING_FILTER = (
    "highpass=f=75,"
    "equalizer=f=180:t=q:w=1.2:g=2.5,"
    "equalizer=f=3200:t=q:w=1.0:g=-4.0,"
    "lowpass=f=7200,"
    "acompressor=threshold=-21dB:ratio=2.5:attack=15:release=200,"
    "loudnorm=I=-16:TP=-1.5:LRA=9"
)


def shape_chunk_audio(in_wav: Path, out_wav: Path):
    """单片段频谱重塑与响度归一化"""
    cmd = [
        str(FFMPEG_PATH),
        "-i", str(in_wav),
        "-af", MASTER_SHAPING_FILTER,
        "-ar", "24000",
        "-ac", "1",
        str(out_wav),
        "-y"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def generate_adaptive_room_tone(sample_data: np.ndarray, num_samples: int, sr: int = 24000) -> np.ndarray:
    """
    根据切片实际的最小能量静音段，自适应生成匹配的温和微底噪 (约 -65dB ~ -70dB)。
    严禁使用过大的 -55dB 噪声，彻底消灭 Noise Pumping 呼吸感。
    """
    if len(sample_data) < sr * 0.2:
        target_rms = 8.0
    else:
        # 计算小窗口 RMS，寻找最低静音段
        w_len = int(sr * 0.05)  # 50ms 窗口
        hops = len(sample_data) // w_len
        rms_vals = [
            np.sqrt(np.mean(sample_data[i*w_len:(i+1)*w_len].astype(np.float32)**2))
            for i in range(hops)
        ]
        # 排除绝对零与极高值，取底噪中位数
        valid_rms = [r for r in rms_vals if 1.0 <= r <= 40.0]
        if valid_rms:
            target_rms = float(np.median(valid_rms))
        else:
            target_rms = 8.0  # 默认在 -68dB 左右

    target_rms = max(4.0, min(16.0, target_rms))  # 严格钳制在安全区间
    noise = np.random.normal(0, target_rms, num_samples).astype(np.float32)
    
    # 简单的低通平滑，让底噪偏向温和粉红/布朗特征
    alpha = 0.3
    for i in range(1, len(noise)):
        noise[i] = alpha * noise[i] + (1 - alpha) * noise[i - 1]
        
    return np.clip(noise, -32768, 32767).astype(np.int16)


def stitch_wav_sequence(wav_list, out_wav: Path, pause_ms: int = 200, crossfade_ms: int = 35):
    """
    广播级序列缝合：
    1. 35ms 等功率余弦曲线 (Equal-Power Cosine Crossfade) 平滑头尾；
    2. 中间间隙填充自适应匹配微底噪 (Adaptive Room Tone Infill)，消除抽真空断气感。
    """
    if not wav_list:
        return

    sr = 24000
    n_fade = int(sr * (crossfade_ms / 1000.0))
    pause_samples = int(sr * (pause_ms / 1000.0))

    fade_in_curve = np.sin(np.linspace(0, np.pi / 2, n_fade)).astype(np.float32)
    fade_out_curve = np.cos(np.linspace(0, np.pi / 2, n_fade)).astype(np.float32)

    stitched_blocks = []
    
    for i, p in enumerate(wav_list):
        with wave.open(str(p), 'rb') as w:
            sr = w.getframerate()
            raw_frames = w.readframes(w.getnframes())
            data = np.frombuffer(raw_frames, dtype=np.int16).copy()

        if len(data) <= n_fade * 2:
            stitched_blocks.append(data)
        else:
            # 应用 35ms 等功率余弦淡入淡出
            data_f = data.astype(np.float32)
            data_f[:n_fade] *= fade_in_curve
            data_f[-n_fade:] *= fade_out_curve
            smoothed_data = np.clip(data_f, -32768, 32767).astype(np.int16)
            stitched_blocks.append(smoothed_data)

        # 在相邻片段之间填充自适应底噪
        if i < len(wav_list) - 1 and pause_samples > 0:
            room_tone = generate_adaptive_room_tone(data, pause_samples, sr=sr)
            stitched_blocks.append(room_tone)

    final_audio = np.concatenate(stitched_blocks)
    
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), 'wb') as out_w:
        out_w.setnchannels(1)
        out_w.setsampwidth(2)
        out_w.setframerate(sr)
        out_w.writeframes(final_audio.tobytes())


def master_pure_voice_track(voice_wav: Path, final_master_wav: Path):
    """
    纯净广播级终混处理：
    仅进行广播级 EBU-R128 (-16.0 LUFS) 响度锁定与安全真峰值防御 (-1.5 TP)。
    """
    cmd = [
        str(FFMPEG_PATH),
        "-i", str(voice_wav),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=9",
        "-ar", "24000",
        "-ac", "1",
        str(final_master_wav),
        "-y"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def apply_spectral_matching(in_wav: Path, out_wav: Path, tilt_db: float = 0.0):
    """
    轻度频谱倾斜对齐 (Spectral Tilt Matching · 30%~40% 微温和对齐)，
    使全剧不同切片的音色厚度与高低频分布高度统一。
    """
    if abs(tilt_db) < 0.2:
        if in_wav != out_wav:
            import shutil
            shutil.copyfile(in_wav, out_wav)
        return

    # 低频微调与高频反向补偿
    filter_str = f"equalizer=f=250:t=q:w=1.0:g={tilt_db:.1f},equalizer=f=4000:t=q:w=1.0:g={-tilt_db*0.7:.1f}"
    cmd = [
        str(FFMPEG_PATH),
        "-i", str(in_wav),
        "-af", filter_str,
        "-ar", "24000",
        "-ac", "1",
        str(out_wav),
        "-y"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
