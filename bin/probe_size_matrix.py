#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分辨率钳位矩阵探测。

背景（重要）：
    subrouter.ai 的 gpt-image-2 会保留请求的长宽比，但把总像素量钳制在某个
    档位。实测请求 1024x1792（1.84MP）实际返回 948x1659（1.57MP），
    长宽比 0.5714 完全一致，总像素被压到 ≈1024x1536 的水位。

    这直接影响超分倍率、裁切余量、运镜 pan 幅度的全部计算。
    因此配置里的 size 只能当"请求提示"，流水线必须以返回图的真实像素为准。

本脚本用 low 档（约 $0.006/张）实测若干 (size, quality) 组合，
输出真实返回尺寸，用于确定：
    1. 低质量档的像素上限是多少
    2. 请求小于上限时是否被精确兑现
    3. quality 是否会抬高上限

结果写入 logs/probe_size_matrix.json，图片写入 assets/probe/size_*
用法：
    python bin/probe_size_matrix.py                 # 默认矩阵（4 组，约 $0.024）
    python bin/probe_size_matrix.py --high          # 追加 high 档（贵，谨慎）
    python bin/probe_size_matrix.py --only 1024x1024,1536x2752
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


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


def png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


DEFAULT_MATRIX = [
    ("1024x1024", "low"),
    ("1024x1536", "low"),
    ("1024x1792", "low"),
    ("1536x2752", "low"),   # 4:7 同比例但更大，测试是否仍被钳到 1.57MP
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=str, default="",
                    help="逗号分隔的 size 列表，覆盖默认矩阵")
    ap.add_argument("--high", action="store_true", help="追加 high 档测试（贵）")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--channel", type=str, default="", help="指定渠道名")
    args = ap.parse_args()

    env = load_env(ROOT / ".env")
    if not env:
        print("[FATAL] 未找到 .env")
        return 2

    base = env.get("gpt-image-2_API_BASE", "https://subrouter.ai").rstrip("/")
    model = env.get("IMAGE_MODEL", "gpt-image-2")
    channels = {k[: -len(f"_{model}")]: v
                for k, v in env.items()
                if k.endswith(f"_{model}") and v.startswith("sk-")}
    if not channels:
        print("[FATAL] .env 中未发现渠道密钥")
        return 2

    ch = args.channel if args.channel in channels else sorted(channels)[0]
    key = channels[ch]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    if args.only:
        matrix = [(s.strip(), "low") for s in args.only.split(",") if s.strip()]
    else:
        matrix = list(DEFAULT_MATRIX)
    if args.high:
        matrix += [("1024x1792", "high"), ("1536x2752", "high")]

    out_dir = ROOT / "assets" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(exist_ok=True)

    prompt = "竖构图，黄土高坡上的汉代关隘，黄昏，写实历史插画，电影感广角"

    rows = []
    print(f"渠道={ch}  model={model}")
    print(f"{'请求 size':>12} {'qual':>5} | {'实际尺寸':>12} {'MP':>6} {'KB':>7} {'耗时':>7}  状态")
    print("-" * 72)

    for size, quality in matrix:
        body = {"model": model, "prompt": prompt, "size": size,
                "quality": quality, "n": 1}
        t0 = time.time()
        rec = {"requested_size": size, "requested_quality": quality}
        try:
            r = requests.post(f"{base}/v1/images/generations", headers=hdr,
                              json=body, timeout=args.timeout)
            dt = time.time() - t0
            rec["status"] = r.status_code
            rec["latency_s"] = round(dt, 2)
            if r.status_code == 200:
                item = (r.json().get("data") or [{}])[0]
                if "b64_json" in item:
                    raw = base64.b64decode(item["b64_json"])
                    dim = png_size(raw)
                    rec["actual_size"] = list(dim) if dim else None
                    rec["actual_mp"] = round(dim[0] * dim[1] / 1e6, 3) if dim else None
                    rec["kb"] = round(len(raw) / 1024)
                    fp = out_dir / f"size_{size}_{quality}.png"
                    fp.write_bytes(raw)
                    rec["saved"] = str(fp)
                    s = f"{dim[0]}x{dim[1]}" if dim else "?"
                    mp = f"{rec['actual_mp']}" if dim else "?"
                    print(f"{size:>12} {quality:>5} | {s:>12} {mp:>6} "
                          f"{rec['kb']:>7} {dt:>6.1f}s  OK")
                elif "url" in item:
                    rec["url"] = item["url"][:120]
                    print(f"{size:>12} {quality:>5} | {'URL返回':>12} "
                          f"{'-':>6} {'-':>7} {dt:>6.1f}s  OK(url)")
                else:
                    print(f"{size:>12} {quality:>5} | {'无图片字段':>12} "
                          f"{'-':>6} {'-':>7} {dt:>6.1f}s  ??")
            else:
                rec["body"] = r.text[:300]
                print(f"{size:>12} {quality:>5} | {'-':>12} {'-':>6} "
                      f"{'-':>7} {dt:>6.1f}s  HTTP {r.status_code}")
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            print(f"{size:>12} {quality:>5} | EXC {type(e).__name__}")
        rows.append(rec)

    # 结论推断
    print("\n" + "=" * 72)
    ok = [r for r in rows if r.get("actual_mp")]
    if ok:
        mps = sorted({r["actual_mp"] for r in ok})
        print(f"实际产出 MP 集合: {mps}")
        print(f"最大: {max(mps)} MP   最小: {min(mps)} MP")
        clamped = [r for r in ok
                   if r["requested_size"] != f"{r['actual_size'][0]}x{r['actual_size'][1]}"]
        if clamped:
            print("被钳位的请求：")
            for r in clamped:
                print(f"   {r['requested_size']:>12} -> "
                      f"{r['actual_size'][0]}x{r['actual_size'][1]}")

    fp = ROOT / "logs" / "probe_size_matrix.json"
    fp.write_text(json.dumps({"channel": ch, "rows": rows}, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\n报告已写入 {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
