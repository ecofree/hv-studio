#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初唐异人传第一集 · Dylan/C3 逐幕情绪渲染

复用 generate_qwen3_local_audio.py 的分幕解析、句块切分与 API 调用，
保留逐句原始录音、请求指纹和音量记录，拼接后进入统一响度处理。

情绪表以第一集八幕的实际叙事功能为准（困境→试探→绝境→发愿→对抗→苦学→荣光→终卷），
每条指令在 C3「沉稳有力、咬字清晰、老纪录片旁白」基调上做单轴偏移，
不做多轴堆叠，避免模型把两个相反意图搅成平均数。
"""
import os
import sys
import io
import re
import time
import wave
import json
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT=Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.services.provenance import atomic_json, fingerprint, file_sha256
from app.services.provider_budget import ProviderBudget
from app.services.narration_leveling import repair, load, active_db
from generate_qwen3_local_audio import (  # noqa: E402
    parse_acts_from_md, split_text_into_chunks, call_tts_api,
    concatenate_wavs, validate_voice_direction,
)

SPEAKER = "dylan"
MODE = "custom"

ACT_TONES = ['沉稳叙事，带疑问与悬念', '紧张而克制，停顿利落', '凝重，节奏稍缓',
             '坚定，逐渐推进叙事', '克制而坚决', '苍凉而敬重', '昂扬而庄重', '肃穆沉静，克制不煽情']


def chunk_direction(act_index, text, chunk_index, count):
    direction='沉稳有力，咬字清晰，老纪录片旁白；'+ACT_TONES[act_index]+'。'
    if act_index==0 and chunk_index==0:direction+='开篇从容铺陈。'
    if act_index==3 and any(word in text for word in ('宁可','誓','绝不')):
        direction+='只在誓言句表达决心，靠重音和停顿体现。'
    if act_index==4 and '绝食' in text:direction+='涉及绝食日数时停顿清楚。'
    if act_index==6 and '十八' in text:direction+='十八日无人上台这一处略放慢。'
    if chunk_index==count-1:direction+='末句自然收束。'
    return direction+'保持自然稳定的整体说话音量；情绪通过语气、节奏和停顿表达，避免突然喊叫或压低整体音量。'


def render_act(title, text, output_path, *, act_index=0, budget=None, retry_uncertain=False):
    from app import paths
    from app.services.studio_voice import render_act as shared_render
    validate_voice_direction(MODE, ACT_TONES[act_index])
    chunks=split_text_into_chunks(text,max_chars=160)
    directed=[(c,chunk_direction(act_index,c,i,len(chunks))) for i,c in enumerate(chunks)]
    return shared_render(directed,output_path,speaker=SPEAKER,mode=MODE,
        model='Qwen3-TTS-12Hz-1.7B-CustomVoice',engine_revision=file_sha256(Path(__file__).with_name('generate_qwen3_local_audio.py')),
        provider=call_tts_api,budget=budget,retry_uncertain=retry_uncertain,cache=paths.state_dir()/'voice-takes')



def main():
    ap=argparse.ArgumentParser(description='按句保存原始录音与音量记录；零预算默认不调用模型')
    ap.add_argument('script',nargs='?',default=str(Path(__file__).with_name('初唐异人传_第一集_Gemini_TTS专版.md')))
    ap.add_argument('--budget',help='durable provider attempt ledger')
    ap.add_argument('--max-requests',type=int,default=0);ap.add_argument('--max-characters',type=int,default=0)
    ap.add_argument('--output-dir')
    args=ap.parse_args();script=args.script
    acts = parse_acts_from_md(script)
    print(f"解析到 {len(acts)} 幕")
    if len(acts) != len(ACT_TONES):
        raise SystemExit(
            f"情绪表有 {len(ACT_TONES)} 条，剧本解析出 {len(acts)} 幕，对不上。"
            f"请先补情绪表，不要让位置错位的指令静默生效。")

    identity=fingerprint([Path(script).read_text(encoding='utf-8'),file_sha256(__file__),SPEAKER,MODE])[:20]
    audio_dir = args.output_dir or str(Path(__file__).parent/'audio'/'versions'/identity)
    os.makedirs(audio_dir, exist_ok=True)
    budget=ProviderBudget(args.budget,tts_requests=args.max_requests,characters=args.max_characters) if args.budget else None
    files = []
    for i, (title, text) in enumerate(acts):
        safe = re.sub(r'[\\/*?:"<>|]', "_", title).strip()
        out = os.path.join(audio_dir, f"初唐异人传_情版_ep1_{i+1:02d}_{safe}.wav")
        files.append(render_act(title, text, out, act_index=i,budget=budget))

    full = os.path.join(audio_dir, "初唐异人传_第一集_情版完整音轨.wav")
    binding=Path(full).with_suffix('.raw.json')
    inputs={str(Path(p).resolve()):file_sha256(p) for p in files}
    if Path(full).exists():
        if not binding.exists():raise ValueError('existing full audio lacks a source binding')
        saved=json.loads(binding.read_text(encoding='utf-8'))
        if saved['inputs']!=inputs or saved['sha256']!=file_sha256(full):raise ValueError('full raw audio changed')
    else:
        concatenate_wavs(files, full, silence_sec=0.8)
        atomic_json(binding,dict(inputs=inputs,sha256=file_sha256(full)))
    boundaries=[];cursor=0
    for i,path in enumerate(files):
        r=json.loads(Path(path).with_suffix('.chunks.json').read_text(encoding='utf-8'))
        if i:boundaries.append(cursor-round(r['sample_rate']*.4))
        boundaries.extend(cursor+b for b in r['boundaries'])
        cursor+=r['samples']+(round(r['sample_rate']*.8) if i<len(files)-1 else 0)
    atomic_json(Path(full).with_suffix('.boundaries.json'),dict(source_sha256=file_sha256(full),boundaries=boundaries))
    fixed=Path(audio_dir)/'narration-balanced.wav'
    if fixed.exists():
        from app.services.narration_master import verify_repair
        verified=verify_repair(fixed.with_suffix('.level.json'))
        if verified['source_sha256']!=file_sha256(full):raise ValueError('balanced audio is stale')
    else:repair(full,fixed,boundaries=boundaries)
    atomic_json(Path(audio_dir)/'narration-result.json',dict(raw_master=full,repaired_master=str(fixed),
                repair_receipt=str(fixed.with_suffix('.level.json')),voice=SPEAKER,approved=False))
    with wave.open(full, "rb") as w:
        print(f"\n[ALL DONE] {full}\n   总时长 {w.getnframes()/w.getframerate()/60:.2f} 分钟")


if __name__ == "__main__":
    main()
