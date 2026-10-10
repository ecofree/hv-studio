#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大唐草民生存录 · Gemini 3.8 Flash TTS 工业级批量渲染与合成引擎
使用原生 Python 标准库（无需安装第三方依赖），直接通过 Gemini 3.8 Flash TTS 模型生成高表现力音频并合并。
"""

import os
import sys
import re
import json
import time
import base64
import urllib.request
import urllib.error
import wave
import argparse

# 默认配置
if not os.getenv("http_proxy") and not os.getenv("https_proxy"):
    os.environ["http_proxy"] = "http://127.0.0.1:7897"
    os.environ["https_proxy"] = "http://127.0.0.1:7897"

# ⚠️ 密钥只从环境变量读，**不要再把实键写回本行**。
# 本行曾带一个可用的 Gemini key 作为 getenv 默认值；该文件要入库到 GitHub，
# 提交它等于把凭据公开到远端，且 git 历史里删不干净。改为缺失即报错。
DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not DEFAULT_API_KEY:
    raise SystemExit(
        "未设置 GEMINI_API_KEY。请先 `set GEMINI_API_KEY=<你的key>`（PowerShell 用 "
        "$env:GEMINI_API_KEY='...'）或 `--key` 显式传入。")
DEFAULT_MODEL = "gemini-3.8-flash-tts"
DEFAULT_VOICE = "Charon"  # Charon: 醇厚深沉、带悲悯与自嘲； Puck: 活泼俏皮、脱口秀戏谑

SCRIPT_MD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "大唐草民录_第一集_Gemini_TTS专版.md")
AUDIO_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")


def parse_acts_from_md(md_file_path):
    """从 Markdown 脚本中提取各幕文本"""
    if not os.path.exists(md_file_path):
        raise FileNotFoundError(f"未找到脚本文件: {md_file_path}")

    with open(md_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 匹配 ### 【第X幕：...】及其代码块内的 text
    pattern = r"### 【(第.幕[：:][^】]+)】.*?```text\s*\n(.*?)\n```"
    matches = re.findall(pattern, content, re.DOTALL)
    if not matches:
        raise ValueError("未能从 Markdown 中解析到任何分幕代码块！")
    return matches


def call_gemini_tts(text, api_key, model=DEFAULT_MODEL, voice=DEFAULT_VOICE, max_retries=3):
    """调用 Gemini 3.8 Flash TTS API 生成音频"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{
            "parts": [{"text": text.strip()}]
        }],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "temperature": 0.7,
            "topP": 0.95,
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice
                    }
                }
            }
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for p in parts:
                        if "inlineData" in p:
                            raw_b64 = p["inlineData"].get("data")
                            return base64.b64decode(raw_b64)
                raise RuntimeError(f"API 返回成功但未找到音频数据: {data}")
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            print(f"  [HTTP 错误 {e.code}] 尝试 {attempt}/{max_retries}: {err_msg}")
            if e.code == 429:
                print("  [触发速率限制] 等待 15 秒后重试...")
                time.sleep(15)
            else:
                time.sleep(3)
        except Exception as e:
            print(f"  [网络/系统错误] 尝试 {attempt}/{max_retries}: {e}")
            time.sleep(3)

    raise RuntimeError("达到最大重试次数，音频生成失败！")


def merge_wav_files(wav_list, output_file, silence_ms=800):
    """将多个 WAV 文件按顺序拼接，并在幕间插入静音间隔"""
    if not wav_list:
        print("没有可供合并的音频文件。")
        return

    print(f"\n正在合并 {len(wav_list)} 个分幕音频到: {output_file}")
    
    first_wav = wave.open(wav_list[0], 'rb')
    params = first_wav.getparams()
    nchannels, sampwidth, framerate, _, comptype, compname = params
    first_wav.close()

    # 计算静音帧数据 (全 0)
    silence_frames = int(framerate * (silence_ms / 1000.0))
    silence_bytes = b'\x00' * (silence_frames * nchannels * sampwidth)

    total_frames = 0
    with wave.open(output_file, 'wb') as out_wav:
        out_wav.setparams((nchannels, sampwidth, framerate, 0, comptype, compname))
        
        for idx, wav_path in enumerate(wav_list):
            with wave.open(wav_path, 'rb') as in_wav:
                frames = in_wav.readframes(in_wav.getnframes())
                out_wav.writeframes(frames)
                total_frames += in_wav.getnframes()
                
            # 若不是最后一幕，则插入静音
            if idx < len(wav_list) - 1 and silence_ms > 0:
                out_wav.writeframes(silence_bytes)
                total_frames += silence_frames

    total_duration = total_frames / float(framerate)
    print(f">> 合并成功！总时长: {total_duration:.2f} 秒 ({total_duration / 60.0:.2f} 分钟)")


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="风趣历史 · Gemini TTS 批量生成引擎")
    parser.add_argument("--script", default=SCRIPT_MD_PATH, help="Markdown 脚本路径")
    parser.add_argument("--key", default=DEFAULT_API_KEY, help="Gemini API 密钥")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="TTS 模型名称 (例如 gemini-3.8-flash-tts, gemini-3.8-flash-lite-tts)")
    parser.add_argument("--voice", default=DEFAULT_VOICE, choices=["Charon", "Puck", "Kore", "Fenrir", "Aoede"], help="配音角色")
    parser.add_argument("--act", type=int, choices=[1, 2, 3, 4], help="仅生成指定幕次 (1~4)")
    parser.add_argument("--prefix", default="act", help="分幕音频文件前缀")
    parser.add_argument("--output-name", default=None, help="合并后的完整音频文件名")
    parser.add_argument("--skip-existing", action="store_true", dest="skip_existing", help="如果本地已有音频则跳过生成")
    parser.add_argument("--no-merge", action="store_true", help="生成后不执行自动合并")
    args = parser.parse_args()

    os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)
    acts = parse_acts_from_md(args.script)
    print(f"=== 成功加载剧本: {os.path.basename(args.script)}，共 {len(acts)} 幕 ===")
    print(f"使用模型: {args.model} | 音色: {args.voice} | 输出目录: {AUDIO_OUTPUT_DIR}\n")

    generated_files = []

    for i, (title, text) in enumerate(acts, start=1):
        if args.act and args.act != i:
            continue

        out_wav = os.path.join(AUDIO_OUTPUT_DIR, f"{args.prefix}{i}.wav")
        generated_files.append(out_wav)

        if args.skip_existing and os.path.exists(out_wav):
            print(f"[跳过] 第 {i} 幕: {title} (本地文件已存在)")
            continue

        print(f">> [正在渲染] 第 {i} 幕: {title} (字数: {len(text.strip())})...")
        start_t = time.time()
        audio_bytes = call_gemini_tts(text, args.key, model=args.model, voice=args.voice)
        
        with open(out_wav, "wb") as f:
            f.write(audio_bytes)

        elapsed = time.time() - start_t
        with wave.open(out_wav, "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
        print(f"  -> 第 {i} 幕完成！耗时: {elapsed:.1f}s | 生成音频时长: {dur:.1f}s ({dur/60:.2f}m)\n")
        time.sleep(3)

    # 如果渲染了多幕且未禁止合并，则自动拼接为完整音轨
    if not args.no_merge and len(acts) == 4 and (args.act is None or len(generated_files) == 4):
        all_act_files = [os.path.join(AUDIO_OUTPUT_DIR, f"{args.prefix}{i}.wav") for i in range(1, 5)]
        if all(os.path.exists(p) for p in all_act_files):
            if args.output_name:
                out_filename = args.output_name if args.output_name.endswith(".wav") else args.output_name + ".wav"
            else:
                base_name = os.path.splitext(os.path.basename(args.script))[0].replace("_Gemini_TTS专版", "")
                out_filename = f"{base_name}_完整解说音轨.wav"
            final_output = os.path.join(AUDIO_OUTPUT_DIR, out_filename)
            merge_wav_files(all_act_files, final_output, silence_ms=800)


if __name__ == "__main__":
    main()
