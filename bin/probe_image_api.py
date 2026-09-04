#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gpt-image-2 中转服务能力探测
=================================

目的：第三方中转的能力差异极大，盲写业务代码会大量返工。
      本脚本在动手前把端点能力逐项探清楚，产出可执行的配置建议。

验证项：
  P1  /v1/images/generations  是否可用
  P2  /v1/images/edits        是否可用（角色一致性的前提）
  P3  size=1024x1792          是否被接受（HD 竖屏）
  P4  quality 参数             low/medium/high 支持情况
  P5  多图参考 image[]        是否支持（Character Lock 依赖此项）
  P6  返回格式                 b64_json / url
  P7  参考图上传方式           本地文件 multipart / 仅公网 URL

用法：
  cp .env.example .env   # 填写 OPENAI_API_KEY 与 OPENAI_API_BASE
  python bin/probe_image_api.py

设计原则：
  - 只做探测，不做任何生产调用
  - 单项失败不中断，全部跑完再汇总
  - 输出可直接回填 config/models.yaml 的建议值
"""

import os
import sys
import json
import base64
import mimetypes
import uuid
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，请先安装：pip install requests")

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    """极简 .env 解析，避免额外依赖。"""
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# 生成一张最小的纯色 PNG 作为参考图测试素材（1x1 像素，避免实体文件依赖）
_TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

results = []


def record(pid, name, ok, detail, suggestion=""):
    results.append(
        {"id": pid, "name": name, "ok": ok, "detail": detail, "suggestion": suggestion}
    )
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {pid} {name}")
    print(f"         {detail}")
    if suggestion:
        print(f"         → 建议: {suggestion}")
    return ok


def make_multipart(fields, files):
    """构造 multipart/form-data，返回 (body, content_type)。"""
    boundary = f"----hvprobe{uuid.uuid4().hex}"
    buf = bytearray()
    for k, v in fields.items():
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        buf += f"{v}\r\n".encode()
    for field, (filename, content) in files.items():
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        ).encode()
        buf += f"Content-Type: {mimetypes.guess_type(filename)[0] or 'image/png'}\r\n\r\n".encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def main():
    load_env()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("OPENAI_API_BASE", "").rstrip("/")

    print("=" * 68)
    print("gpt-image-2 中转服务能力探测")
    print("=" * 68)

    if not api_key or api_key.startswith("sk-xxxx"):
        print("\n未检测到有效配置。请先：")
        print("  1. cp .env.example .env")
        print("  2. 填入 OPENAI_API_KEY 与 OPENAI_API_BASE")
        print("  3. 重新运行本脚本")
        return 1

    if not api_base:
        print("\n缺少 OPENAI_API_BASE（第三方中转必填）")
        return 1

    print(f"\nAPI base : {api_base}")
    print(f"Key      : {api_key[:8]}...{api_key[-4:]}\n")

    headers = {"Authorization": f"Bearer {api_key}"}
    model = os.environ.get("IMAGE_MODEL", "gpt-image-2")

    # ---------- P1: generations ----------
    print("[1/7] 探测 /v1/images/generations ...")
    try:
        r = requests.post(
            f"{api_base}/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": model,
                "prompt": "a single ancient chinese bronze lamp, plain background",
                "size": "1024x1792",
                "quality": "low",
                "n": 1,
            },
            timeout=120,
        )
        if r.status_code == 200:
            data = r.json().get("data", [{}])[0]
            fmt = "b64_json" if data.get("b64_json") else (
                "url" if data.get("url") else "unknown")
            record("P1", "generations 端点", True,
                   f"HTTP 200，返回格式 {fmt}",
                   f"返回格式记为 {fmt}，下载逻辑按此实现")
        else:
            record("P1", "generations 端点", False,
                   f"HTTP {r.status_code}: {r.text[:200]}",
                   "检查 model 名称与 size 是否被中转支持")
    except Exception as e:
        record("P1", "generations 端点", False, f"{type(e).__name__}: {e}",
               "检查网络、base_url 是否正确（注意是否需要带 /v1）")

    # ---------- P2 & P7: edits + 本地文件 multipart ----------
    print("[2/7] 探测 /v1/images/edits（本地文件 multipart）...")
    try:
        body, ct = make_multipart(
            {"model": model,
             "prompt": "keep the exact subject, place it in a dim palace hall",
             "size": "1024x1792",
             "quality": "low",
             "n": "1"},
            {"image[]": ("ref.png", _TINY_PNG)},
        )
        r = requests.post(f"{api_base}/images/edits",
                          headers={**headers, "Content-Type": ct},
                          data=body, timeout=120)
        if r.status_code == 200:
            record("P2", "edits 端点", True, "HTTP 200", "")
            record("P7", "参考图本地 multipart 上传", True,
                   "支持 image[] 本地文件上传",
                   "参考图可直接传本地文件，无需先传图床")
        else:
            record("P2", "edits 端点", False,
                   f"HTTP {r.status_code}: {r.text[:200]}",
                   "若中转不支持 edits，角色一致性方案需改为纯提示词描述")
    except Exception as e:
        record("P2", "edits 端点", False, f"{type(e).__name__}: {e}", "")

    # ---------- P5: 多图参考 ----------
    print("[3/7] 探测多图参考（image[] 重复字段，Character Lock 依赖）...")
    try:
        files = {}
        for i in range(3):
            files.setdefault("image[]", []).append(
                (f"ref{i}.png", _TINY_PNG))
        # requests 支持同名多文件：传 list of tuples
        files_list = [("image[]", (f"ref{i}.png", _TINY_PNG)) for i in range(3)]
        body, ct = make_multipart_files(
            {"model": model,
             "prompt": "preserve identity from all references",
             "size": "1024x1792",
             "quality": "low",
             "n": "1"},
            files_list,
        )
        r = requests.post(f"{api_base}/images/edits",
                          headers={**headers, "Content-Type": ct},
                          data=body, timeout=120)
        if r.status_code == 200:
            record("P5", "多图参考 image[]", True,
                   "接受 3 张参考图",
                   "角色参考包方案可行，建议每角色 10-16 张定妆照")
        else:
            record("P5", "多图参考 image[]", False,
                   f"HTTP {r.status_code}: {r.text[:200]}",
                   "多图不支持则只能用单张参考，一致性会明显下降")
    except Exception as e:
        record("P5", "多图参考 image[]", False, f"{type(e).__name__}: {e}", "")

    # ---------- P3: 尺寸矩阵 ----------
    print("[4/7] 探测尺寸支持矩阵 ...")
    size_ok = []
    for size in ["1024x1792", "1152x2048", "2160x3840"]:
        try:
            r = requests.post(
                f"{api_base}/images/generations",
                headers={**headers, "Content-Type": "application/json"},
                json={"model": model, "prompt": "test", "size": size,
                      "quality": "low", "n": 1},
                timeout=120,
            )
            if r.status_code == 200:
                size_ok.append(size)
        except Exception:
            pass
    record("P3", "尺寸支持", bool(size_ok),
           f"可用尺寸: {', '.join(size_ok) if size_ok else '无'}",
           f"当前配置 1024x1792 {'可用' if '1024x1792' in size_ok else '不可用，需改配置'}")

    # ---------- P4: quality ----------
    print("[5/7] 探测 quality 参数 ...")
    q_ok = []
    for q in ["low", "medium", "high"]:
        try:
            r = requests.post(
                f"{api_base}/images/generations",
                headers={**headers, "Content-Type": "application/json"},
                json={"model": model, "prompt": "test", "size": "1024x1792",
                      "quality": q, "n": 1},
                timeout=180,
            )
            if r.status_code == 200:
                q_ok.append(q)
        except Exception:
            pass
    record("P4", "quality 档位", bool(q_ok),
           f"支持: {', '.join(q_ok) if q_ok else '无'}",
           "low 迭代 + high 成片" if {"low", "high"} <= set(q_ok)
           else "按实际支持档位调整")

    # ---------- 汇总 ----------
    print("\n" + "=" * 68)
    print("汇总")
    print("=" * 68)
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['id']} {r['name']}")

    print(f"\n通过 {passed}/{len(results)}")
    print("\n建议：")
    for r in results:
        if r["suggestion"]:
            print(f"  - {r['suggestion']}")

    if not any(r["id"] == "P2" and r["ok"] for r in results):
        print("\n  ⚠️ edits 端点不可用 —— 角色一致性方案必须改用：")
        print("     详细外貌描述 + 固定 style_suffix + 人工抽检，")
        print("     或改用支持参考图的其他通道。")

    out = ROOT / "logs" / "probe_image_api.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已写入: {out}")
    return 0


def make_multipart_files(fields, files_list):
    """支持同名多文件的 multipart 构造。"""
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


if __name__ == "__main__":
    sys.exit(main())
