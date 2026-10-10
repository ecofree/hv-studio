#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import urllib.request
import json
import os
import wave
import time
import sys

token = 'sk-ws-H.PMLRMLI.Modk.MEUCID-SFLjT_9pncuhSTNa5Xa1JfQ2a8eFQopXLK6jJaiAPAiEAydpuIo5AuWP4Jx-66xGTqCYw9d4GuFAgscindAJys_0'
url = 'https://ws-ba6d5skqpa8m020f.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'

segments = [
    "贞观十七年，太极宫深处的东宫，悄然弥漫起一股令人窒息的血腥味。李世民一生最害怕的噩梦，以一种极度荒诞而残忍的方式，在他自己悉心培养的儿子们身上爆发了。",
    "嫡长子、皇太子李承乾，幼年聪慧，八岁就被立为储君，李世民请来天下名儒倾囊相授。然而随着年龄增长，承乾患上了严重的腿疾，走路一瘸一拐。而在崇尚骑射的李唐皇室，一个残疾的储君让年轻的太子感受到了空前的恐慌。",
    "更致命的是李世民自身的偏心。李世民极度钟爱四子魏王李泰，甚至允许他的轿子直接抬进朝堂。在李承乾眼里，这分明就是当年李建成与李世民双权力中心的幽灵重现！四弟就是当年的秦王，而自己就是那个即将被逼上绝路的李建成。"
]

instruction = "用沉稳苍凉、带有些许自嘲与沧桑感的历史说书人口吻，语速从容稳健，富有史学厚重感与戏剧张力"

audio_dir = r'D:\hv\resource\风趣历史.writing-DNA\scripts\audio'
os.makedirs(audio_dir, exist_ok=True)

temp_wavs = []
t0 = time.time()

for idx, seg in enumerate(segments, start=1):
    print(f">> 正在生成第 {idx}/{len(segments)} 段 (字数: {len(seg)}, 字节: {len(seg.encode('utf-8'))})...")
    payload = {
        'model': 'qwen3-tts-instruct-flash',
        'input': {'text': seg},
        'parameters': {
            'voice': 'Marcus',
            'instruction': instruction
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        audio_url = data['output']['audio']['url']
    
    seg_file = os.path.join(audio_dir, f'marcus_seg_{idx}.wav')
    urllib.request.urlretrieve(audio_url, seg_file)
    temp_wavs.append(seg_file)
    time.sleep(1)

# 无缝合并音频并加入 0.4 秒自然句间气口
out_file = os.path.join(audio_dir, 'marcus_1min_demo.wav')
first_wav = wave.open(temp_wavs[0], 'rb')
params = first_wav.getparams()
nchannels, sampwidth, framerate, _, comptype, compname = params
first_wav.close()

silence_frames = int(framerate * 0.4)
silence_bytes = b'\x00' * (silence_frames * nchannels * sampwidth)

total_frames = 0
with wave.open(out_file, 'wb') as out_wav:
    out_wav.setparams((nchannels, sampwidth, framerate, 0, comptype, compname))
    for i, wav_p in enumerate(temp_wavs):
        with wave.open(wav_p, 'rb') as in_w:
            frames = in_w.readframes(in_w.getnframes())
            out_wav.writeframes(frames)
            total_frames += in_w.getnframes()
        if i < len(temp_wavs) - 1:
            out_wav.writeframes(silence_bytes)
            total_frames += silence_frames

elapsed = time.time() - t0
dur = total_frames / float(framerate)

print("\n" + "="*40)
print(f"🎉 1 分钟 Marcus 历史说书人长音频生成完成！")
print(f"总耗时: {elapsed:.2f} 秒")
print(f"合成音频时长: {dur:.2f} 秒 ({dur/60:.2f} 分钟)")
print(f"采样率: {framerate} Hz | 声道: {nchannels}")
print(f"文件大小: {os.path.getsize(out_file)/(1024*1024):.2f} MB")
print(f"输出路径: {out_file}")
print("="*40)
