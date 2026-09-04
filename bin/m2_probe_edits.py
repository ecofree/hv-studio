#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 edits 通道探测（花一次真钱，验证关键前提）。

目标：subrouter 的 /v1/images/edits 是否支持"参考图 + 保持人物身份"？
      用 shot-000（张骞正脸锚）为参考，生成 shot-003 场景（软禁特写），
      看产出图张骞脸是否更贴近 shot-000（cosine > 0.6 视为 edits 有效）。

用法：
  python bin/m2_probe_edits.py
"""
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.services.face import cosine, embed_face
from app.services.image import get_image
from app.models import ImageEditRequest

BASE = Path("D:/hv/episodes/hw-001/shots")
ANCHOR = BASE / "shot-000.png"       # 张骞正脸锚（未央宫拜谢）
TARGET_SCENE = "shot-003"            # 重生成目标场景（软禁特写，M1 换脸最明显）
OUT = BASE / "probe_edits.png"

SCENE_PROMPT = (
    "张骞在匈奴营地被软禁，虽身穿胡地粗衣，仍将汉节紧握在怀，"
    "望向帐外，目光坚定。月夜，篝火，四周是匈奴毡帐。"
)
STYLE = (
    "ancient chinese historical painting style, cinematic composition, "
    "dramatic lighting, warm earthy tone, highly detailed"
)
IDENTITY = (
    "Preserve the exact identity, facial geometry, age, hairstyle, "
    "headdress and wardrobe of the man in the reference image. "
    "Render the new scene faithfully."
)


def b64_of(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def main():
    # 1) 锚的 embed
    a_emb, a_meta = embed_face(ANCHOR)
    print(f"anchor {ANCHOR.name}: embed={'OK' if a_emb is not None else 'FAIL'}")
    if a_emb is None:
        print("锚图无人脸，无法继续"); return

    # 2) 构造 edits 请求（参考图 + 场景）
    prompt = f"{IDENTITY} {SCENE_PROMPT} {STYLE}"
    req = ImageEditRequest(
        model="gpt-image-2", prompt=prompt,
        image=b64_of(ANCHOR), size="1080x1920", quality="low",
    )
    img = get_image()
    print(f"\n调 edits，prompt 前 60 字: {prompt[:60]}...")
    t0 = time.time()
    resp = img.edit(req)
    item = resp.data[0]
    raw = base64.b64decode(item.b64_json)
    OUT.write_bytes(raw)
    print(f"  返回 {time.time()-t0:.1f}s, {len(raw)/1024:.0f}KB, revised_prompt:\n    {(item.revised_prompt or '')[:200]}")

    # 3) 检查产出人脸相似度
    p_emb, p_meta = embed_face(OUT)
    if p_emb is None:
        print(f"\n产出图无人脸（可能参考图导致姿势/构图不佳）")
    else:
        sim = cosine(a_emb, p_emb)
        verdict = "edits 保人有效" if sim >= 0.60 else (
            "edits 保人效果不足" if sim >= 0.40 else "edits 几乎没保人")
        print(f"\nanchor vs 产出: sim={sim:.3f}  [{verdict}]")
    print(f"\n产出图: {OUT}")


if __name__ == "__main__":
    main()
