#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生图渠道**只读**可用性核对（零费用、零生成）
==========================================

为什么需要它
------------
config.yaml 里 `image_channels[].models` 是**声明值**，不是事实。2026-10-15
实测踩坑：cxk / qcyc / aicost 三个 key 全部照常返回 HTTP 200，但
`gpt-image-2` 已从服务商模型表里消失，派活时才以 503「分组 provider:qcyv
下模型 gpt-image-2 无可用渠道」失败 —— 每次尝试不到 1 秒，44 镜一张没出，
预算 31 次全部记为 failed_or_uncertain。

config.yaml 的既有规矩要求「每次启用渠道前用 GET /v1/models 当场核对」，
但仓库里原有的 bin/probe_*.py 全都会真的发起付费生成。本脚本只做那一步
核对：GET /v1/models，不带任何生成请求，不消耗预算。

用法
----
  python bin/probe_image_channels.py                 # 核对配置里声明的模型
  python bin/probe_image_channels.py --model gpt-image-2.5
  python bin/probe_image_channels.py --all           # 顺带列出服务端全部模型

退出码：0 = 至少一个 key 提供了目标模型；1 = 都没有（可据此停工）。
"""

import argparse
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少 requests：pip install requests")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def declared_routes():
    """从 config.yaml 读渠道声明（不 new 一个 app 运行时，避免连带副作用）。"""
    import yaml

    cfg = yaml.safe_load((ROOT / "app" / "config" / "config.yaml").read_text(encoding="utf-8"))
    return cfg.get("image_channels") or []


def probe(base, key, timeout=20):
    try:
        resp = requests.get(f"{base}/v1/models",
                            headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except Exception as exc:                       # 网络抖动不应中断核对
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    try:
        ids = sorted({m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)})
    except Exception as exc:
        return None, f"bad json: {type(exc).__name__}"
    return ids, None


def main():
    ap = argparse.ArgumentParser(description="只读核对生图渠道的模型清单（不发起任何生成）")
    ap.add_argument("--model", default=None, help="要核对的模型名，默认取 config image.primary.model")
    ap.add_argument("--all", action="store_true", help="打印每个 key 的全部在线模型")
    ap.add_argument("--timeout", type=int, default=20)
    args = ap.parse_args()

    env = load_env()
    routes = declared_routes()
    primary = args.model or env.get("IMAGE_MODEL") or "gpt-image-2"
    default_base = env.get("gpt-image-2_API_BASE", "https://subrouter.ai").rstrip("/")

    print(f"核对目标模型: {primary}\n")
    hit = False
    for route in routes:
        name = route.get("name", "?")
        key = env.get(route.get("key_env", ""), "")
        if not key:
            print(f"[{name}] 未配置 key（{route.get('key_env')}）— 跳过")
            continue
        base = env.get(route.get("base_env", "gpt-image-2_API_BASE"), default_base).rstrip("/")
        ids, err = probe(base, key, args.timeout)
        if err:
            print(f"[{name}] 探测失败: {err}")
            continue
        has = primary in ids
        hit = hit or has
        flag = "可用" if has else "不提供"
        print(f"[{name}] {flag} {primary} | 在线模型 {len(ids)} 个"
              + (f" | 声明: {route.get('models')}" if not has else ""))
        if args.all or not has:
            print(f"    在线: {ids}")
    print()
    print("结论: " + (f"{primary} 已恢复，可以发图" if hit else f"{primary} 当前无可用渠道，暂停生图"))
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
