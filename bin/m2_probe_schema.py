#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
subrouter edits schema 盲探（只触校验错误，不真生成计费）。

上一轮报错 `images[].image_url is required` 揭示了字段名是 `images` 数组。
本脚本构造不同的 images 元素形态，靠 400/422 的缺参/非法提示反推 schema。

风险控制：用极短 prompt + quality=low + 指向不存在的 url，若被放行到生成
阶段会用假 url 失败或按 b64 生图——一旦看到非 4xx（如 200/5xx 生成类），
立即在后续变体停止，避免累計费用。每变体 1 次，最多 ~6 次。
"""
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")
from app.config.config import get_settings

TINY = Path("D:/hv/assets/probe") / "t1_aicost_1024x1792.png"
if not TINY.exists():
    # 用 hw-001 任一张图
    TINY = Path("D:/hv/episodes/hw-001/shots/shot-000.png")


def main():
    import requests
    from app.config.config import get_settings
    s = get_settings()
    # 取渠道 key
    ch = [c for c in s.image_channels()][0] if hasattr(s, "image_channels") else None
    # 手动从 env 读第一个渠道
    env = dict(line.strip().split("=", 1) for line in Path("D:/hv/.env").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#") and "=" in line)
    model = env.get("IMAGE_MODEL", "gpt-image-2")
    base = env.get("gpt-image-2_API_BASE", "https://subrouter.ai").rstrip("/")
    suffix = f"_{model}"
    keys = {k[: -len(suffix)]: v for k, v in env.items()
            if k.endswith(suffix) and v.startswith("sk-")}
    if not keys:
        print("无渠道 key"); return
    ch = sorted(keys)[0]
    key = keys[ch]
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    print(f"渠道 {ch}  model {model}  base {base}\n")
    b64 = base64.b64encode(TINY.read_bytes()).decode()

    variants = [
        ("images:[{image_b64}]", {"images": [{"image_b64": b64}]}),
        ("images:[{image_url:data}]", {"images": [{"image_url": "data:image/png;base64," + b64[:2000]}]}),
        ("images:[{image:data}]", {"images": [{"image": "data:image/png;base64," + b64[:2000]}]}),
        ("image_b64(str)", {"image": b64}),
        ("images:[{image_url:fake}]", {"images": [{"image_url": "https://example.invalid/x.png"}]}),
    ]
    for label, extra in variants:
        body = {"model": model, "prompt": "x", "size": "1024x1024",
                "quality": "low", "n": 1, **extra}
        t0 = time.time()
        try:
            r = requests.post(f"{base}/v1/images/edits", headers=hdr, json=body, timeout=60)
            msg = r.text[:200].replace("\n", " ")
            print(f"[{label:34s}] HTTP {r.status_code} {time.time()-t0:.1f}s  {msg}")
            if r.status_code == 200:
                print("  ^^ 200 = 该格式被接受并可生成（后续停止盲探）")
                break
        except Exception as e:
            print(f"[{label:34s}] EXC {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
