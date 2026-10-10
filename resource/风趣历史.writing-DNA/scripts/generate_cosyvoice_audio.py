#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风趣历史 · Qwen3-TTS / CosyVoice v3.5 旗舰工业级语音合成引擎
针对阿里云百炼 MAAS 业务空间专属端点优化，原生支持 qwen3-tts-instruct-flash（自然语言情绪指令控制）。
支持 Markdown 分幕剧本批量渲染、断点续传与多幕无缝合并。
"""

import os
import sys
import re
import json
import time
import wave
import argparse
import urllib.request
import urllib.error

# 默认配置
DEFAULT_WORKSPACE = "ws-ba6d5skqpa8m020f"
DEFAULT_BASE_HOST = f"https://{DEFAULT_WORKSPACE}.cn-beijing.maas.aliyuncs.com"
DEFAULT_API_KEY = os.getenv(
    "DASHSCOPE_API_KEY",
    "sk-ws-H.PMLRMLI.Modk.MEUCID-SFLjT_9pncuhSTNa5Xa1JfQ2a8eFQopXLK6jJaiAPAiEAydpuIo5AuWP4Jx-66xGTqCYw9d4GuFAgscindAJys_0"
)

DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_VOICE = "Kai"  # Kai (凯): 纯正浑厚深沉男低音(~107Hz); Ethan (晨煦): 标准成熟北方男中音(~152Hz); Nofish (不吃鱼): 沉稳叙事男声
DEFAULT_INSTRUCTION = "用沉稳苍凉、带有些许自嘲与沧桑感的历史说书人口吻，语速从容稳健，富有史学厚重感与戏剧张力"

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


def normalize_wav_bytes(data: bytes) -> bytes:
    """修复 DashScope 流式 WAV 头的 0x7fffffff 占位符，校准 RIFF 和 data 块真实长度"""
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return data
    b = bytearray(data)
    total_size = len(b)
    # RIFF chunk 大小（除去 RIFF 4字节与自身4字节，即 total_size - 8）
    b[4:8] = (total_size - 8).to_bytes(4, 'little')
    # 查找 data chunk 并校正其真实大小
    idx = 12
    while idx < total_size - 8:
        chunk_id = b[idx:idx+4]
        chunk_sz = int.from_bytes(b[idx+4:idx+8], 'little')
        if chunk_id == b'data':
            actual_data_sz = total_size - (idx + 8)
            b[idx+4:idx+8] = actual_data_sz.to_bytes(4, 'little')
            break
        idx += 8 + chunk_sz
    return bytes(b)


def split_text_into_chunks(text, max_bytes=1200):
    """按段落与句末标点将长文本智能切分为自然意群块（默认 <= 1200 字节，约 400 汉字），避免破坏声音连贯性"""
    raw_sentences = re.split(r'([。！？；\n]+)', text)
    chunks = []
    cur = ""
    for i in range(0, len(raw_sentences), 2):
        sentence = raw_sentences[i]
        punct = raw_sentences[i+1] if i+1 < len(raw_sentences) else ""
        piece = sentence + punct
        if not piece.strip():
            continue
        test_cur = cur + piece
        if len(test_cur.encode('utf-8')) <= max_bytes:
            cur = test_cur
        else:
            if cur.strip():
                chunks.append(cur.strip())
            cur = piece
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _call_qwen_single_chunk(chunk_text, api_key, model, voice, instruction, base_host, max_retries=3):
    """调用百炼底层单次合成接口"""
    url = f"{base_host}/api/v1/services/aigc/multimodal-generation/generation"
    payload = {
        "model": model,
        "input": {"text": chunk_text.strip()},
        "parameters": {"voice": voice}
    }
    if instruction and "instruct" in model:
        payload["parameters"]["instruction"] = instruction

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                output = data.get("output", {})
                audio_info = output.get("audio", {})
                audio_url = audio_info.get("url")
                if not audio_url:
                    raise RuntimeError(f"返回数据中未包含 audio.url: {data}")
                with urllib.request.urlopen(audio_url, timeout=120) as audio_resp:
                    raw_bytes = audio_resp.read()
                    return normalize_wav_bytes(raw_bytes)
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            print(f"  [HTTP 错误 {e.code}] 尝试 {attempt}/{max_retries}: {err_msg}")
            if e.code == 429:
                print("  [触发速率限制] 等待 10 秒后重试...")
                time.sleep(10)
            else:
                time.sleep(3)
        except Exception as e:
            print(f"  [网络错误] 尝试 {attempt}/{max_retries}: {e}")
            time.sleep(3)

    raise RuntimeError("达到最大重试次数，音频生成失败！")


def call_qwen_tts_api(text, api_key=DEFAULT_API_KEY, model=DEFAULT_MODEL,
                      voice=DEFAULT_VOICE, instruction=DEFAULT_INSTRUCTION,
                      base_host=DEFAULT_BASE_HOST):
    """
    工业级通用语音合成入口：
    自动检查文本字节数，1200 字节（约 400 汉字）内单次一气呵成生成，确保胸腔共鸣与音色 100% 连贯统一；
    超过限制时按自然段落标点切片，并在句间平滑插入 0.35 秒微呼吸静音，无缝合并。
    """
    text_clean = text.strip()
    if len(text_clean.encode('utf-8')) <= 1200:
        return _call_qwen_single_chunk(text_clean, api_key, model, voice, instruction, base_host)

    # 超过限制，自动切分
    chunks = split_text_into_chunks(text_clean, max_bytes=1200)
    print(f"  [长文本自动切片] 全文共 {len(text_clean)} 字，已拆分为 {len(chunks)} 个自然意群句块...")
    
    import io
    wav_bytes_list = []
    for c_idx, c_text in enumerate(chunks, start=1):
        print(f"    -> 渲染句块 {c_idx}/{len(chunks)} ({len(c_text)}字)...")
        b = _call_qwen_single_chunk(c_text, api_key, model, voice, instruction, base_host)
        wav_bytes_list.append(b)
        time.sleep(1)

    # 内存内无缝拼接
    first_wav = wave.open(io.BytesIO(wav_bytes_list[0]), 'rb')
    params = first_wav.getparams()
    nchannels, sampwidth, framerate, _, comptype, compname = params
    first_wav.close()

    silence_frames = int(framerate * 0.35)
    silence_bytes = b'\x00' * (silence_frames * nchannels * sampwidth)

    out_buf = io.BytesIO()
    with wave.open(out_buf, 'wb') as out_w:
        out_w.setparams((nchannels, sampwidth, framerate, 0, comptype, compname))
        for idx, wb in enumerate(wav_bytes_list):
            with wave.open(io.BytesIO(wb), 'rb') as in_w:
                out_w.writeframes(in_w.readframes(in_w.getnframes()))
            if idx < len(wav_bytes_list) - 1:
                out_w.writeframes(silence_bytes)

    return out_buf.getvalue()


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
                
            if idx < len(wav_list) - 1 and silence_ms > 0:
                out_wav.writeframes(silence_bytes)
                total_frames += silence_frames

    total_duration = total_frames / float(framerate)
    print(f">> 合并成功！总时长: {total_duration:.2f} 秒 ({total_duration / 60.0:.2f} 分钟)")


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="Qwen3-TTS / CosyVoice v3.5 工业级语音合成引擎")
    parser.add_argument("--script", default=None, help="Markdown 脚本路径 (批量分幕渲染)")
    parser.add_argument("--text", default=None, help="单次直接合成的文本内容")
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=["qwen3-tts-instruct-flash", "qwen3-tts-flash"], help="TTS 模型名称")
    parser.add_argument("--key", default=DEFAULT_API_KEY, help="API Key")
    parser.add_argument("--voice", default=DEFAULT_VOICE, choices=["Kai", "Ethan", "Nofish", "Cherry", "Serena", "Chelsie", "Momo"], help="配音角色 (Kai: 深沉男低音, Ethan: 稳健男中音, Nofish: 叙事男声, Cherry: 阳光女声)")
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION, help="情绪与自然语言演播指令")
    parser.add_argument("--act", type=int, choices=[1, 2, 3, 4], help="仅生成指定幕次 (1~4)")
    parser.add_argument("--prefix", default="qwen_act", help="分幕音频文件前缀")
    parser.add_argument("--output-name", default=None, help="合并后的完整音频文件名")
    parser.add_argument("--skip-existing", action="store_true", help="如果本地已有音频则跳过生成")
    args = parser.parse_args()

    os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

    # 模式一：单句测试模式
    if args.text:
        out_wav = os.path.join(AUDIO_OUTPUT_DIR, args.output_name if args.output_name else "qwen_single_test.wav")
        print(f"=== 单句测试合成 ===")
        print(f"模型: {args.model} | 音色: {args.voice}")
        print(f"指令: {args.instruction}")
        t0 = time.time()
        audio_bytes = call_qwen_tts_api(args.text, api_key=args.key, model=args.model,
                                        voice=args.voice, instruction=args.instruction)
        with open(out_wav, "wb") as f:
            f.write(audio_bytes)
        print(f">> 合成成功！耗时: {time.time()-t0:.2f}s | 保存至: {out_wav}")
        return

    # 模式二：Markdown 分幕批量工程模式
    script_path = args.script if args.script else SCRIPT_MD_PATH
    acts = parse_acts_from_md(script_path)
    print(f"=== 成功加载剧本: {os.path.basename(script_path)}，共 {len(acts)} 幕 ===")
    print(f"使用模型: {args.model} | 音色: {args.voice}")
    print(f"演播指令: {args.instruction}")
    print(f"输出目录: {AUDIO_OUTPUT_DIR}\n")

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
        audio_bytes = call_qwen_tts_api(
            text=text,
            api_key=args.key,
            model=args.model,
            voice=args.voice,
            instruction=args.instruction
        )
        
        with open(out_wav, "wb") as f:
            f.write(audio_bytes)

        elapsed = time.time() - start_t
        with wave.open(out_wav, "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
        print(f"  -> 第 {i} 幕完成！耗时: {elapsed:.1f}s | 生成音频时长: {dur:.1f}s ({dur/60:.2f}m)\n")
        time.sleep(2)

    # 自动合并全部 4 幕
    if len(acts) == 4 and (args.act is None or len(generated_files) == 4):
        all_act_files = [os.path.join(AUDIO_OUTPUT_DIR, f"{args.prefix}{i}.wav") for i in range(1, 5)]
        if all(os.path.exists(p) for p in all_act_files):
            if args.output_name:
                out_filename = args.output_name if args.output_name.endswith(".wav") else args.output_name + ".wav"
            else:
                base_name = os.path.splitext(os.path.basename(script_path))[0].replace("_Gemini_TTS专版", "")
                out_filename = f"{base_name}_QwenTTS解说音轨.wav"
            final_output = os.path.join(AUDIO_OUTPUT_DIR, out_filename)
            merge_wav_files(all_act_files, final_output, silence_ms=800)


if __name__ == "__main__":
    main()
