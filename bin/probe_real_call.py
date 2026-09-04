#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
subrouter.ai 真实调用探针（会产生极少量费用）。

用途：盲探（非法参数）无法确认 subrouter 的参数行为——它会静默忽略非法
size/quality 并真的去生成。因此必须做一次真实最小调用来钉死：
  1. 1024x1792 是否被真正支持（返回图的真实像素尺寸）
  2. 返回格式是 b64_json 还是 url
  3. quality=low 是否被接受、真实耗时
  4. edits 端点是否接受多张参考图（base64 数组 / multipart）

结果写入 logs/probe_real_call.json，图片写入 assets/probe/。
用法：
    python bin/probe_real_call.py            # 只跑生图（约 $0.006）
    python bin/probe_probe_real_call.py --edits    # 追加多参考图测试
    python bin/probe_probe_real_call.py --video    # 追加 I2V 测试（较贵，¥1-3）
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import struct
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# .env 加载（不依赖 dotenv，保持零依赖）
# --------------------------------------------------------------------------
def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def mask(key: str) -> str:
    return key[:8] + "..." + key[-4:] if len(key) > 16 else "***"


def png_size(data: bytes):
    """从 PNG 头解析真实像素尺寸，不依赖 PIL。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edits", action="store_true", help="测试多参考图图生图")
    ap.add_argument("--video", action="store_true", help="测试 I2V（较贵）")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    if not env:
        print("[FATAL] 未找到 .env，请先 cp .env.example .env 并填写密钥")
        return 2

    base = env.get("gpt-image-2_API_BASE", "https://subrouter.ai").rstrip("/")
    model = env.get("IMAGE_MODEL", "gpt-image-2")

    # 自动发现渠道：{provider}_{model}={key}
    suffix = f"_{model}"
    channels = {
        k[: -len(suffix)]: v for k, v in env.items() if k.endswith(suffix) and v.startswith("sk-")
    }
    if not channels:
        print(f"[FATAL] .env 中未发现任何 '*{suffix}' 形式的密钥")
        return 2

    out_dir = ROOT / "assets" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)

    report = {"base": base, "model": model, "channels": list(channels), "tests": {}}

    print(f"BASE  = {base}")
    print(f"MODEL = {model}")
    print(f"渠道  = {list(channels)}")
    print("=" * 72)

    # ------------------------------------------------------------------
    # T1: 真实生图 —— 验证 size/quality/返回格式/耗时
    # ------------------------------------------------------------------
    ch = sorted(channels)[0]
    key = channels[ch]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    prompt = (
        "西汉，戈壁黄昏。一位三十余岁的汉使，身着赭色深衣，手持褪色的旌节，"
        "独自立于沙丘之上。风沙漫卷，远处是低矮的土黄地平线。"
        "写实历史插画风格，电影感广角构图，暖橙色侧逆光，竖构图。"
    )
    body = {
        "model": model,
        "prompt": prompt,
        "size": "1024x1792",
        "quality": "low",
        "n": 1,
    }
    print(f"\n[T1] 真实生图  渠道={ch}({mask(key)})  size=1024x1792 quality=low")
    t0 = time.time()
    try:
        r = requests.post(f"{base}/v1/images/generations", headers=hdr, json=body,
                          timeout=args.timeout)
        dt = time.time() - t0
        print(f"     -> HTTP {r.status_code}  耗时 {dt:.1f}s")
        rec = {"status": r.status_code, "latency_s": round(dt, 2)}
        if r.status_code == 200:
            j = r.json()
            item = (j.get("data") or [{}])[0]
            rec["has_b64"] = "b64_json" in item
            rec["has_url"] = "url" in item
            rec["top_level_keys"] = sorted(j.keys())
            rec["item_keys"] = sorted(item.keys())
            if "b64_json" in item:
                raw = base64.b64decode(item["b64_json"])
                rec["bytes"] = len(raw)
                rec["png_size"] = png_size(raw)
                fp = out_dir / f"t1_{ch}_1024x1792.png"
                fp.write_bytes(raw)
                rec["saved"] = str(fp)
                print(f"     -> b64 图片 {len(raw)/1024:.0f}KB  真实像素 {png_size(raw)}")
                print(f"     -> 已保存 {fp}")
            elif "url" in item:
                rec["url"] = item["url"][:120]
                print(f"     -> 返回 URL: {item['url'][:100]}")
            if "usage" in j:
                rec["usage"] = j["usage"]
            # revised_prompt 是 gpt-image 系列的特征字段
            if item.get("revised_prompt"):
                rec["revised_prompt"] = item["revised_prompt"][:200]
                print(f"     -> revised_prompt: {item['revised_prompt'][:120]}")
        else:
            rec["body"] = r.text[:400]
            print(f"     -> {r.text[:300]}")
        report["tests"]["T1_generations"] = rec
    except Exception as e:
        report["tests"]["T1_generations"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"     -> EXC {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # T2: 多参考图（Character Lock 的基础能力）
    # ------------------------------------------------------------------
    if args.edits:
        print("\n[T2] 多参考图图生图")
        src = report["tests"].get("T1_generations", {}).get("saved")
        if not src:
            print("     -> 跳过：T1 未产出可用图片")
        else:
            raw = Path(src).read_bytes()
            b64 = base64.b64encode(raw).decode()
            for label, payload in (
                ("json_b64_array", {"model": model, "prompt": "保持人物一致，改为正面半身像",
                                    "image": [b64]}),
                ("json_b64_str", {"model": model, "prompt": "保持人物一致，改为正面半身像",
                                  "image": b64}),
            ):
                try:
                    t0 = time.time()
                    r = requests.post(f"{base}/v1/images/edits", headers=hdr,
                                      json=payload, timeout=args.timeout)
                    dt = time.time() - t0
                    print(f"     [{label}] -> {r.status_code}  {dt:.1f}s  {r.text[:160]}")
                    report["tests"].setdefault("T2_edits", {})[label] = {
                        "status": r.status_code, "latency_s": round(dt, 2),
                        "body": r.text[:300]}
                except Exception as e:
                    print(f"     [{label}] -> EXC {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # T3: I2V（昂贵，默认关闭）
    # ------------------------------------------------------------------
    if args.video:
        print("\n[T3] I2V 视频生成（会产生真实费用）")
        src = report["tests"].get("T1_generations", {}).get("saved")
        b64 = base64.b64encode(Path(src).read_bytes()).decode() if src else None
        for ep in ("/v1/videos", "/v1/video/generations"):
            payload = {
                "model": model,
                "prompt": "镜头缓慢推近，风沙吹动衣摆，人物静止不动",
                "image": b64,
                "seconds": 5,
            }
            try:
                t0 = time.time()
                r = requests.post(f"{base}{ep}", headers=hdr, json=payload, timeout=120)
                dt = time.time() - t0
                print(f"     [{ep}] -> {r.status_code}  {dt:.1f}s  {r.text[:260]}")
                report["tests"].setdefault("T3_video", {})[ep] = {
                    "status": r.status_code, "latency_s": round(dt, 2), "body": r.text[:400]}
            except Exception as e:
                print(f"     [{ep}] -> EXC {type(e).__name__}: {e}")

    fp = ROOT / "logs" / "probe_real_call.json"
    fp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
