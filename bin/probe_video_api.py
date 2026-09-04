#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
中转服务 I2V（图生视频）能力扫描器
=====================================

背景：中转服务的视频接口命名千奇百怪，文档往往缺失或过期。
      盲写业务代码会大量返工，本脚本先把端点摸清楚。

★ 核心设计：零成本探测
  用「明显无效的参数」去请求，靠状态码反推端点是否存在：
    404 / 405  → 端点不存在
    400 / 422  → 端点存在，只是参数不对（命中！）
    401 / 403  → 端点存在，鉴权问题
    200        → 端点存在且参数被接受（意外收获，可能产生费用）
  这样在找到正确端点前，不会产生任何真实的视频生成费用。

两阶段：
  阶段一（默认，零成本）  扫描端点 + 模型名，输出可用矩阵
  阶段二（需 --live 确认）对命中端点做真实生成调用，验证参数与返回结构

用法：
  python bin/probe_video_api.py            # 阶段一，零成本
  python bin/probe_video_api.py --live     # 阶段二，会真实计费
"""

import os
import sys
import json
import uuid
import base64
import mimetypes
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，请先安装：pip install requests")

ROOT = Path(__file__).resolve().parent.parent

CANDIDATE_ENDPOINTS = [
    "/v1/videos/generations",
    "/v1/video/generations",
    "/v1/videos",
    "/v1/video/create",
    "/v1/video/submit",
    "/v1/images/videos",
    "/v1/generations/video",
    "/v1/i2v/generations",
    "/v1/images/generations",   # 部分中转把视频也挂在 images 下
]

CANDIDATE_MODELS = [
    "seedance-2.0", "seedance-1.5", "seedance-1.0-pro",
    "kling-v2", "kling-v1.6", "kling-v1",
    "wan-2.2-i2v", "wan-2.1-i2v",
    "vidu-2", "vidu-1.5",
    "hailuo-02", "hailuo-i2v",
    "jimeng-i2v", "doubao-seedance",
]

IMAGE_FIELD_VARIANTS = ["image", "image_url", "image_urls", "images", "file"]

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# 状态码语义
VERDICT = {
    200: "EXISTS_OK",      # 端点存在且参数被接受（可能产生费用！）
    400: "EXISTS",         # 端点存在，参数不符 —— 这是我们要的信号
    401: "EXISTS_AUTH",    # 端点存在，鉴权失败
    403: "EXISTS_AUTH",
    404: "MISSING",        # 端点不存在
    405: "MISSING",
    422: "EXISTS",         # 参数校验失败，端点存在
    429: "EXISTS",         # 限流，端点存在
}


def load_env():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def probe(base, key, endpoint, model):
    """单次探测：故意用不完整的参数，靠状态码判断端点是否存在。"""
    url = f"{base}{endpoint}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # 只给 model，不给必填的图片字段 → 期望返回 400 而非 404
    payload = {"model": model, "prompt": "__probe__"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        return {
            "endpoint": endpoint,
            "model": model,
            "status": r.status_code,
            "verdict": VERDICT.get(r.status_code, "UNKNOWN"),
            "body": r.text[:180],
        }
    except requests.exceptions.Timeout:
        return {"endpoint": endpoint, "model": model, "status": -1,
                "verdict": "TIMEOUT", "body": ""}
    except Exception as e:
        return {"endpoint": endpoint, "model": model, "status": -2,
                "verdict": "ERROR", "body": f"{type(e).__name__}: {e}"}


def make_multipart(fields, files_list):
    boundary = f"----hvprobe{uuid.uuid4().hex}"
    buf = bytearray()
    for k, v in fields.items():
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        buf += f"{v}\r\n".encode()
    for field, (filename, content) in files_list:
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        ).encode()
        buf += b"Content-Type: image/png\r\n\r\n"
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def live_test(base, key, endpoint, model):
    """阶段二：真实生成调用（会计费），验证图片字段命名与返回结构。"""
    content = base64.b64decode(_TINY_PNG_B64)
    findings = []
    for field in IMAGE_FIELD_VARIANTS:
        try:
            body, ct = make_multipart(
                {"model": model, "prompt": "slow zoom in, cinematic"},
                [(field, ("ref.png", content))],
            )
            r = requests.post(f"{base}{endpoint}",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": ct},
                              data=body, timeout=180)
            text = r.text[:300]
            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    j = {}
                # 判断是否异步
                keys = set()
                if isinstance(j, dict):
                    keys = set(j.keys())
                    if "data" in j and isinstance(j["data"], list) and j["data"]:
                        keys |= set(j["data"][0].keys())
                async_hint = "id" in keys or "task_id" in keys or "taskId" in keys
                findings.append({
                    "image_field": field, "status": 200,
                    "async": async_hint,
                    "top_keys": sorted(keys)[:12],
                    "body": text,
                })
                break
            else:
                findings.append({"image_field": field, "status": r.status_code,
                                 "async": None, "top_keys": [], "body": text})
        except Exception as e:
            findings.append({"image_field": field, "status": -1,
                             "async": None, "top_keys": [],
                             "body": f"{type(e).__name__}: {e}"})
    return findings


def main():
    load_env()
    live = "--live" in sys.argv

    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("OPENAI_API_BASE", "").rstrip("/")

    print("=" * 70)
    print("中转服务 I2V 能力扫描器" + ("（LIVE 模式，会真实计费）" if live else "（探测模式，零成本）"))
    print("=" * 70)

    if not api_key or api_key.startswith("sk-xxxx"):
        print("\n未检测到有效配置。请先：")
        print("  1. cp .env.example .env")
        print("  2. 填入 OPENAI_API_KEY 与 OPENAI_API_BASE")
        return 1
    if not api_base:
        print("\n缺少 OPENAI_API_BASE")
        return 1

    print(f"\nAPI base : {api_base}")
    print(f"端点候选 : {len(CANDIDATE_ENDPOINTS)} 个")
    print(f"模型候选 : {len(CANDIDATE_MODELS)} 个")
    print(f"组合总数 : {len(CANDIDATE_ENDPOINTS) * len(CANDIDATE_MODELS)}")
    print("\n扫描中（并发 8，每个超时 25s）...\n")

    tasks = [(ep, m) for ep in CANDIDATE_ENDPOINTS for m in CANDIDATE_MODELS]
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(probe, api_base, api_key, ep, m): (ep, m) for ep, m in tasks}
        done = 0
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 20 == 0:
                print(f"  ... {done}/{len(tasks)}")

    # 汇总
    exists = [r for r in results if r["verdict"] in ("EXISTS", "EXISTS_OK", "EXISTS_AUTH")]
    ok200 = [r for r in results if r["verdict"] == "EXISTS_OK"]

    print("\n" + "=" * 70)
    print("扫描结果")
    print("=" * 70)

    if not exists:
        print("\n未发现任何视频端点。可能原因：")
        print("  1. 该中转只代理图片，不代理视频")
        print("  2. 视频接口路径不在候选列表内（需你提供文档或实际路径）")
        print("  3. base_url 需要不同的前缀（例如 /api/v1 而非 /v1）")
        print("\n建议：把中转提供的接口文档发我，我按实际路径对接。")
    else:
        by_ep = {}
        for r in exists:
            by_ep.setdefault(r["endpoint"], []).append(r)
        for ep, items in sorted(by_ep.items()):
            print(f"\n  端点 {ep}")
            for it in items:
                mark = "★" if it["verdict"] == "EXISTS_OK" else " "
                print(f"    {mark} {it['model']:<18} HTTP {it['status']:<4} {it['verdict']}")

        if ok200:
            print("\n  ⚠️ 上面标 ★ 的组合返回 200，说明请求被接受并可能已计费。")

    # 阶段二
    live_findings = []
    if live and exists:
        best = (ok200 or exists)[0]
        print("\n" + "=" * 70)
        print(f"LIVE 验证：{best['endpoint']} / {best['model']}")
        print("=" * 70)
        live_findings = live_test(api_base, api_key, best["endpoint"], best["model"])
        for f in live_findings:
            if f["status"] == 200:
                print(f"\n  ✓ 图片字段名 = {f['image_field']}")
                print(f"    异步模式   = {'是（需轮询 task_id）' if f['async'] else '否（同步返回）'}")
                print(f"    返回顶层键 = {f['top_keys']}")
                print(f"    响应片段   = {f['body'][:200]}")
                break
        else:
            print("\n  所有图片字段命名均未通过：")
            for f in live_findings:
                print(f"    {f['image_field']:<12} HTTP {f['status']}  {f['body'][:100]}")
    elif exists:
        print("\n提示：加 --live 参数可对命中端点做真实生成验证（会计费）。")

    out = ROOT / "logs" / "probe_video_api.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"results": results, "live": live_findings},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
