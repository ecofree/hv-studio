#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风趣历史 · Qwen3-TTS 本地 1.7B 旗舰剧本渲染引擎 (RTX 2060 12G)
专为《大唐修罗场》等多集季播长剧本设计：
1. 优先调用本地常驻 HTTP 服务 (http://127.0.0.1:8000/api/tts)，秒级出流无需重复加载模型；
2. 克隆模式保留参考音色，不支持的情绪指令明确报错，不自动切换音色；
3. 支持 Markdown 分幕提取、意群句块智能切分、自然呼吸停顿无缝拼接。
"""

import os
import sys
import re
import io
import time
import wave
import json
import argparse
import urllib.request
import urllib.error

from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

API_URL = "http://127.0.0.1:8000/api/tts"
DEFAULT_MD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "大唐修罗场_第一集_Gemini_TTS专版.md")
AUDIO_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
DEFAULT_MODE = "clone"
DEFAULT_REF_AUDIO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_male_Kai.wav")
DEFAULT_SPEAKER = "clone_kai"
DEFAULT_INSTRUCT = "正统大型历史纪录片旁白口吻，语速从容稳健，字字千钧。胸腔低频共鸣浑厚深沉，语气沉郁肃穆，带有冷峻的历史审判感，句间留有充分的呼吸停顿。"

def parse_acts_from_md(md_file_path):
    """从 Markdown 脚本中提取各幕文本"""
    if not os.path.exists(md_file_path):
        raise FileNotFoundError(f"未找到脚本文件: {md_file_path}")

    with open(md_file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 匹配 ### 【第X幕...】及其代码块内的 text
    pattern = r"### 【([^】]+)】.*?```text\s*\n(.*?)\n```"
    matches = re.findall(pattern, content, re.DOTALL)
    if not matches:
        # 兼容其他格式
        pattern2 = r"```text\s*\n(.*?)\n```"
        raw_blocks = re.findall(pattern2, content, re.DOTALL)
        if raw_blocks:
            matches = [(f"段落_{i+1}", block) for i, block in enumerate(raw_blocks)]
        else:
            raise ValueError("未能从 Markdown 中解析到任何分幕代码块！")
    return matches

def split_text_into_chunks(text, max_chars=180):
    """按标点智能切分，单块建议 100~200 字，保证语气与韵律的最佳控制"""
    # 替换 [pause=0.5s] 为中文标点停顿
    clean_text = re.sub(r'\[pause=[^\]]+\]', '……', text)
    sentences = re.split(r'([。！？；\n]+)', clean_text)
    chunks = []
    cur = ""
    for i in range(0, len(sentences), 2):
        s = sentences[i].strip()
        p = sentences[i+1] if i+1 < len(sentences) else ""
        piece = s + p
        if not piece.strip():
            continue
        if len(cur) + len(piece) <= max_chars:
            cur += piece
        else:
            if cur.strip():
                chunks.append(cur.strip())
            cur = piece
    if cur.strip():
        chunks.append(cur.strip())
    return chunks

def call_tts_api(text, speaker=DEFAULT_SPEAKER, instruct=None, mode=DEFAULT_MODE, ref_audio=None):
    """通过本地 HTTP 服务合成单句"""
    validate_voice_direction(mode, instruct)
    from app.services.studio_voice import require_dispatch
    require_dispatch(text,speaker,mode,instruct,ref_audio)
    payload = {
        "text": text,
        "mode": mode,
        "speaker": speaker,
        "language": "chinese",
        "ref_audio_path": ref_audio
    }
    if mode == 'custom':
        payload['instruct'] = instruct or ''
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def validate_voice_direction(mode, instruct):
    # Current local Base/clone endpoint never consumes instruct. Do not claim
    # that an emotion direction worked or silently switch to another speaker.
    if mode == 'clone' and instruct:
        raise ValueError('当前 clone 接口不支持 instruct 情绪控制；保留音色，请使用原声编辑或人工复录。')
    if mode not in ('clone', 'custom'):
        raise ValueError('unsupported voice mode')

_direct_model = None
def call_tts_direct(text, speaker=DEFAULT_SPEAKER, instruct=DEFAULT_INSTRUCT):
    """回退：直接通过本地 GPU 库加载合成"""
    from app.services.studio_voice import require_dispatch
    require_dispatch(text,speaker,'custom',instruct,None)
    global _direct_model
    import torch
    import soundfile as sf
    import safetensors
    _orig_safe_open = safetensors.safe_open
    def patched_safe_open(filename, framework, device="cpu", *, backend="pread"):
        return _orig_safe_open(filename, framework, device=device, backend=backend)
    safetensors.safe_open = patched_safe_open

    from qwen_tts import Qwen3TTSModel
    if _direct_model is None:
        print("  [本地备用模式] 正在加载 Qwen3-TTS 1.7B 模型到 GPU...")
        _direct_model = Qwen3TTSModel.from_pretrained(
            r"D:\models\qwen3-tts\Qwen3-TTS-12Hz-1.7B-CustomVoice",
            device_map="cuda:0",
            dtype=torch.float16,
            attn_implementation="sdpa",
        )
    wavs, sr = _direct_model.generate_custom_voice(
        text=text,
        speaker=speaker,
        language="chinese",
        instruct=instruct,
        non_streaming_mode=True,
    )
    buf = io.BytesIO()
    sf.write(buf, wavs[0], sr, format="WAV")
    buf.seek(0)
    return buf.read()

def render_act(act_title, act_text, output_path, speaker=DEFAULT_SPEAKER, instruct=None, *, budget=None):
    from app import paths
    validate_voice_direction(DEFAULT_MODE,instruct)
    from app.services.studio_voice import render_act as shared_render
    from app.services.provenance import file_sha256
    from pathlib import Path
    chunks=split_text_into_chunks(act_text,max_chars=160)
    return shared_render([(c,instruct or (DEFAULT_INSTRUCT if DEFAULT_MODE=='custom' else '')) for c in chunks],
        output_path,speaker=speaker,mode=DEFAULT_MODE,model='Qwen3-TTS-local',engine_revision=file_sha256(__file__),
        reference=DEFAULT_REF_AUDIO,provider=call_tts_api,budget=budget,cache=paths.state_dir()/'voice-takes')


def concatenate_wavs(wav_paths, output_path, silence_sec=0.8):
    """拼接多个 WAV 文件并加入指定时长的呼吸间隔"""
    if not wav_paths:
        return
    first_wav = wave.open(wav_paths[0], 'rb')
    params = first_wav.getparams()
    nchannels, sampwidth, framerate, _, comptype, compname = params
    first_wav.close()

    silence_frames = int(framerate * silence_sec)
    silence_bytes = b'\x00' * (silence_frames * nchannels * sampwidth)

    with wave.open(output_path, 'wb') as out_w:
        out_w.setparams((nchannels, sampwidth, framerate, 0, comptype, compname))
        for i, wp in enumerate(wav_paths):
            with wave.open(wp, 'rb') as in_w:
                out_w.writeframes(in_w.readframes(in_w.getnframes()))
            if i < len(wav_paths) - 1:
                out_w.writeframes(silence_bytes)

    duration = os.path.getsize(output_path) / (framerate * nchannels * sampwidth)
    print(f"\n[ALL DONE] 全集拼接完毕！")
    print(f"   总音轨路径: {output_path}")
    print(f"   全剧总时长: {duration/60:.2f} 分钟 ({duration:.1f} 秒)\n")

def main():
    from app.services.provider_budget import ProviderBudget
    from app.services.provenance import fingerprint,file_sha256
    parser=argparse.ArgumentParser(description='本地旁白：统一缓存与持久预算，默认不发起新调用')
    parser.add_argument('--script',default=DEFAULT_MD_PATH)
    parser.add_argument('--speaker',default=DEFAULT_SPEAKER)
    parser.add_argument('--act',type=int,default=1);parser.add_argument('--all',action='store_true')
    parser.add_argument('--budget');parser.add_argument('--max-requests',type=int,default=0)
    parser.add_argument('--max-characters',type=int,default=0)
    args=parser.parse_args();acts=parse_acts_from_md(args.script)
    budget=ProviderBudget(args.budget,tts_requests=args.max_requests,characters=args.max_characters) if args.budget else None
    ident=fingerprint([Path(args.script).read_text('utf-8'),args.speaker,DEFAULT_MODE,file_sha256(__file__)])[:20]
    out=Path(AUDIO_OUTPUT_DIR)/'studio-clone'/ident
    if not args.all and not 1<=args.act<=len(acts):raise ValueError('幕序号超出范围')
    for i,(title,text) in enumerate(acts,1):
        if args.all or args.act==i:render_act(title,text,out/f'act-{i:02}.wav',speaker=args.speaker,budget=budget)
    print(f'候选录音保存在 {out}；需实际试听，不自动替换完整音轨。')


if __name__ == "__main__":
    main()

