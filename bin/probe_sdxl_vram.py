#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本机 CUDA 显存真实上限探测 + 模型可行性判定
==============================================

背景（重要教训）：
  torch.cuda.mem_get_info() 报告的 free 值**不可信**。
  本机报告 12.00 GB 总显存 / 10.95 GB 空闲，
  但实测单进程累积分配上限仅 6.00 GB，
  剩余约 4.95 GB 无法被 CUDA 分配（Windows WDDM 驱动模型下的典型现象）。

  因此判断"某个模型能不能跑"，不能看报告值，必须实测累积分配。

判定方法：
  用 0.25 GB 小块逐步累积（排除碎片因素），找到真实的分配天花板，
  再与各模型的权重 + 激活需求比对。
"""

import torch

torch.cuda.empty_cache()
free0, total = torch.cuda.mem_get_info()

print("=" * 68)
print(f"GPU      : {torch.cuda.get_device_name(0)}")
print(f"报告总显存: {total / 1024**3:.2f} GB")
print(f"报告空闲  : {free0 / 1024**3:.2f} GB   <-- 此值不可信，需实测")
print("=" * 68)

# ---------- 1. 实测真实分配上限 ----------
print("\n[1] 实测单进程累积分配上限（0.25 GB 小块）")
held = []
usable = 0.0
for i in range(64):
    try:
        held.append(torch.empty(int(0.25 * 1024**3 // 2),
                                dtype=torch.float16, device="cuda"))
        usable += 0.25
    except torch.OutOfMemoryError:
        break

free1, _ = torch.cuda.mem_get_info()
print(f"    实际可用上限 : {usable:.2f} GB")
print(f"    分配后报告空闲: {free1 / 1024**3:.2f} GB")
print(f"    合计         : {usable + free1 / 1024**3:.2f} GB  (与报告总量吻合)")
print(f"    不可分配部分 : {free1 / 1024**3:.2f} GB  <-- WDDM 驱动模型下的虚高")

del held
torch.cuda.empty_cache()

# ---------- 2. 模型需求 vs 实测上限 ----------
# (名称, 权重 GB fp16, 激活 GB 估算, 备注)
models = [
    ("SDXL fp16 全量",        6.95, 2.5, "UNet 5.14 + bigG 1.39 + TE-L 0.25 + VAE 0.17"),
    ("SDXL fp8 / GGUF Q5",    4.20, 2.5, "量化版，质量略降"),
    ("SDXL + CPU offload",    6.95, 0.5, "权重在 GPU、按需搬运，速度极慢"),
    ("SD 1.5 + 古风 LoRA",    2.10, 1.5, "可跑，但画质明显低于 SDXL"),
    ("RealESRGAN x2 (tile)",  0.07, 1.2, "tile=512 分块，安全"),
    ("Qwen3-TTS 0.6B fp16",   1.20, 1.0, "安全"),
    ("Qwen3-TTS 1.7B fp16",   3.40, 1.5, "安全"),
    ("Kokoro-82M",            0.08, 0.0, "CPU 运行，不占显存"),
    ("Wan2.2 I2V 1.3B",       2.60, 3.0, "临界，需实测"),
]

print(f"\n[2] 模型可行性判定（实测上限 {usable:.2f} GB）")
print(f"    {'模型':<24}{'权重':>7}{'激活':>7}{'合计':>7}   判定")
print("    " + "-" * 62)

verdicts = {}
for name, w, act, note in models:
    need = w + act
    ok = need <= usable
    verdicts[name] = (ok, need)
    mark = "可运行" if ok else "不可行"
    print(f"    {name:<24}{w:>6.2f}{act:>7.1f}{need:>7.2f}   {mark}")

print(f"\n    注：{note}" if False else "")
for name, w, act, note in models:
    ok, need = verdicts[name]
    if not ok:
        print(f"    · {name} 超出 {need - usable:.2f} GB —— {note}")

# ---------- 3. 结论 ----------
print("\n" + "=" * 68)
print("结论")
print("=" * 68)
sdxl_ok = verdicts["SDXL fp16 全量"][0]
if not sdxl_ok:
    print(f"SDXL fp16 全量需要 {verdicts['SDXL fp16 全量'][1]:.2f} GB，")
    print(f"实测上限仅 {usable:.2f} GB，超出 {verdicts['SDXL fp16 全量'][1] - usable:.2f} GB。")
    print()
    print("=> 本机无法原生运行 SDXL。可行替代：")
    if verdicts["SDXL fp8 / GGUF Q5"][0]:
        print("   - SDXL 量化版（fp8 / GGUF Q5）：可行，画质略降")
    print("   - SD 1.5 + 古风 LoRA：可行，但画质差距明显")
    print("   - SDXL + CPU offload：可行但极慢，不适合批量")
    print()
    print("=> 生图主力应走 gpt-image-2 API，本地方案仅作应急兜底。")
    print("   注意：降级链中的「本地 SDXL」需改为量化版，不可写原生 SDXL。")

print()
print("连带影响：")
print("  · RealESRGAN 超分必须开 tile 模式（tile=512），否则大图会爆显存")
print("  · Qwen3-TTS 0.6B / 1.7B 均在预算内，本地运行安全")
print("  · 任何新增模型上线前，先跑本脚本比对实测上限")

out = f"{__file__.rsplit(chr(92), 2)[0]}/logs/vram_probe.json"
import json
from pathlib import Path
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text(json.dumps({
    "reported_total_GB": round(total / 1024**3, 2),
    "reported_free_GB": round(free0 / 1024**3, 2),
    "measured_usable_GB": usable,
    "unallocatable_GB": round(free1 / 1024**3, 2),
    "models": {n: {"need_GB": round(v[1], 2), "ok": v[0]}
               for n, v in verdicts.items()},
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n结果已写入 logs/vram_probe.json")
