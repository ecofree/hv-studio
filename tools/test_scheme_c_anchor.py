#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scheme C 对照实验脚本：前缀声学锚定 + 自动静音裁切 vs 原始无锚点冷启动
测试样本：易引发自回归模型语义劫持的剧情高冲突、多引号、强转折段落
"""

import sys
import io
import re
import wave
import numpy as np
from pathlib import Path

TOOLS_DIR = Path(r"D:\hv-studio\tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from gemini_tts_engine import (
    call_gemini_tts_raw,
    detect_fundamental_f0,
    get_wav_duration
)

ANCHOR_TEXT = "接下来，请听这一段内容。"


def sanitize_text(text: str) -> str:
    """清洗戏精触发器：剥离双引号、降级激烈标点"""
    text = re.sub(r'[！!]+', '。', text)
    text = re.sub(r'[\?？]+', '。', text)
    text = re.sub(r'[…]{2,}|\.{3,}', '，', text)
    text = text.replace('“', '').replace('”', '').replace('"', '').replace("'", '')
    text = re.sub(r'【[^】]+】', '', text)
    return text.strip()


def trim_anchor_numpy(wav_bytes: bytes, sr: int = 24000) -> tuple[bytes, float]:
    """通过静音侦测算法精准裁切开头的锚点引导词"""
    data = np.frombuffer(wav_bytes, dtype=np.int16)
    
    # 锚点句通常在 1.0s ~ 3.5s 结束，在该区间内检测静音窗口
    f_sz = int(sr * 0.05)   # 50ms 帧长
    hop = int(sr * 0.02)    # 20ms 步长
    n_hops = (len(data) - f_sz) // hop
    
    min_hop = int(1.0 * sr / hop)
    max_hop = int(3.5 * sr / hop)
    
    in_sil = False
    consec_sil = 0
    needed_consec = int(0.18 * sr / hop)  # 持续 180ms 的低能量静音
    
    cut_sample = int(2.0 * sr)  # 兜底截断时间点
    
    for i in range(min_hop, min(n_hops, max_hop)):
        s_idx = i * hop
        chunk = data[s_idx:s_idx + f_sz]
        rms = np.sqrt(np.mean(chunk.astype(np.float32)**2))
        if rms < 500:
            if not in_sil:
                in_sil = True
            consec_sil += 1
        else:
            if in_sil and consec_sil >= needed_consec:
                cut_sample = s_idx
                break
            in_sil = False
            consec_sil = 0
            
    trimmed_data = data[cut_sample:]
    cut_sec = cut_sample / sr
    
    bio = io.BytesIO()
    with wave.open(bio, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(trimmed_data.tobytes())
        
    return bio.getvalue(), cut_sec


def measure_start_f0(wav_bytes: bytes, sec: float = 3.0, sr: int = 24000) -> float:
    """提取音频开头 N 秒的平均基频"""
    data = np.frombuffer(wav_bytes, dtype=np.int16).astype(np.float32)
    length = int(sr * sec)
    if len(data) < length:
        return 0.0
    sub = data[:length] - np.mean(data[:length])
    corr = np.fft.irfft(np.fft.rfft(sub, n=2*len(sub)) * np.conj(np.fft.rfft(sub, n=2*len(sub))))[:len(sub)]
    if corr[0] <= 0:
        return 0.0
    corr /= corr[0]
    min_lag = int(sr / 320)
    max_lag = int(sr / 70)
    plag = np.argmax(corr[min_lag:max_lag]) + min_lag
    return float(sr / plag)


def main():
    print("=" * 70)
    print("【Scheme C 声学锚定与防漂移实操对照实验】")
    print("=" * 70)
    
    # 测试段落 1：强情绪、带引号、易漂移历史段落（江都夜变）
    raw_text = (
        '大业十四年早春三月，当关中大兴城在李唐的治理下逐步恢复平静之时，'
        '千里之外的江南江都，却迎来了中国历史上最血腥、最惨烈的一场帝国谢幕大剧！'
        '杨广抚摸着自己的脖颈，惨然苦笑道：“好头颅，谁当斫之？”'
        '这位昔日意气风发的盛世帝王，早已在精神层面上彻底崩溃放弃。'
    )
    clean_text = sanitize_text(raw_text)
    anchored_payload = f"{ANCHOR_TEXT}\n\n{clean_text}"
    
    print(f"原始剧本文本 ({len(raw_text)} 字): {raw_text[:60]}...")
    print(f"戏精清洗后 ({len(clean_text)} 字): {clean_text[:60]}...")
    print("-" * 70)
    
    # A 组：无锚点直接冷启动（传统方式）
    print(">>> 正在生成 [A 组：无锚点原始冷启动]...")
    wav_a, model_a, k_a = call_gemini_tts_raw(clean_text, voice="Charon", seed=42)
    f0_start_a = measure_start_f0(wav_a, sec=3.0)
    mean_a, p90_a, max_a = detect_fundamental_f0(wav_a)
    dur_a = get_wav_duration(wav_a)
    
    # B 组：有前缀声学引导词 + 毫秒级自动裁切
    print(">>> 正在生成 [B 组：固定前缀声学锚定 + 自动裁切]...")
    wav_b_raw, model_b, k_b = call_gemini_tts_raw(anchored_payload, voice="Charon", seed=42)
    wav_b, cut_sec = trim_anchor_numpy(wav_b_raw)
    f0_start_b = measure_start_f0(wav_b, sec=3.0)
    mean_b, p90_b, max_b = detect_fundamental_f0(wav_b)
    dur_b = get_wav_duration(wav_b)
    
    print("=" * 70)
    print("【A/B 对照实验核心声学指标对齐】")
    print(f"A 组 (无锚点冷启动):")
    print(f"  - 起始前 3 秒基频: {f0_start_a:.1f} Hz")
    print(f"  - 全切片基频分布: 均值={mean_a:.1f} Hz | P90={p90_a:.1f} Hz | Max={max_a:.1f} Hz")
    print(f"  - 正文总时长: {dur_a:.2f} 秒")
    print("-" * 70)
    print(f"B 组 (Scheme C 锚定裁切, 自动裁除锚点 {cut_sec:.2f} 秒):")
    print(f"  - 起始前 3 秒基频: {f0_start_b:.1f} Hz  <-- 重点观察首句是否平稳沉稳！")
    print(f"  - 全切片基频分布: 均值={mean_b:.1f} Hz | P90={p90_b:.1f} Hz | Max={max_b:.1f} Hz")
    print(f"  - 正文总时长: {dur_b:.2f} 秒")
    print("=" * 70)
    
    out_dir = Path(r"D:\hv-studio\workspace\test_scheme_c")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 测试段落 2：Act 6 Chunk 3（此前触发 3 次质检拦截的制度短文本）
    raw_text2 = (
        '李渊更下令删繁就简，彻底废除隋朝大业年间严刑峻法，颁行《武德律》，'
        '确立名例、卫禁、职制、户婚、厩库、擅兴、贼盗、斗讼、诈伪、杂律、捕亡、断狱十二篇，'
        '改绞斩为流徙，慎用大辟死刑，确立了德礼为政教之本，刑罚为政教之用的法治原则。'
    )
    clean_text2 = sanitize_text(raw_text2)
    anchored_payload2 = f"{ANCHOR_TEXT}\n\n{clean_text2}"
    
    print("\n" + "=" * 70)
    print("【测试段落 2：Act 6 Chunk 3 制度法治总结（易漂移短文本）】")
    print("=" * 70)
    
    print(">>> 正在生成 [A 组：无锚点原始冷启动]...")
    wav_a2, _, _ = call_gemini_tts_raw(clean_text2, voice="Charon", seed=42)
    f0_start_a2 = measure_start_f0(wav_a2, sec=3.0)
    mean_a2, p90_a2, max_a2 = detect_fundamental_f0(wav_a2)
    dur_a2 = get_wav_duration(wav_a2)
    
    print(">>> 正在生成 [B 组：固定前缀声学锚定 + 自动裁切]...")
    wav_b2_raw, _, _ = call_gemini_tts_raw(anchored_payload2, voice="Charon", seed=42)
    wav_b2, cut_sec2 = trim_anchor_numpy(wav_b2_raw)
    f0_start_b2 = measure_start_f0(wav_b2, sec=3.0)
    mean_b2, p90_b2, max_b2 = detect_fundamental_f0(wav_b2)
    dur_b2 = get_wav_duration(wav_b2)
    
    print("=" * 70)
    print("【测试 2 声学指标对比】")
    print(f"A 组 (无锚点冷启动): 首句 3 秒 F0={f0_start_a2:.1f} Hz | 均值={mean_a2:.1f} Hz | P90={p90_a2:.1f} Hz | Max={max_a2:.1f} Hz | 时长={dur_a2:.1f}s")
    print(f"B 组 (锚定裁切, 裁掉{cut_sec2:.2f}s): 首句 3 秒 F0={f0_start_b2:.1f} Hz | 均值={mean_b2:.1f} Hz | P90={p90_b2:.1f} Hz | Max={max_b2:.1f} Hz | 时长={dur_b2:.1f}s")
    print("=" * 70)

    p_a2 = out_dir / "test_A2_unanchored_raw.wav"
    p_b2 = out_dir / "test_B2_anchored_trimmed.wav"
    p_a2.write_bytes(wav_a2)
    p_b2.write_bytes(wav_b2)
    print(f"测试 2 音频已落盘: {p_a2} | {p_b2}")


if __name__ == "__main__":
    main()
