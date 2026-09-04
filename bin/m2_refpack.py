#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 定妆包生成：为张骞生成多角度正面特写，作为角色锚。

设计（基于 2026-09-03 实测教训）：
  - 纯色中性背景 + 半身/胸像特写 → 脸占画面大比例，embed 可靠
  - 用统一 ZQ_ANCHOR 文案保持身份一致，多角度轮流生成
  - 生成后自检：这组图彼此 sim 应 >0.6（同一角色的不同角度）；
    挑「对其它图平均 sim 最高」的一张作 anchor_face
  - 产出写 resource/ref_packs/zhangqian/{meta.json, *.png}

用法：python bin/m2_refpack.py [--n 6] [--out zhangqian]
"""
import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.services.face import cosine, detect_faces, embed_face
from app.services.image import get_image
from app.models import ImageGenRequest

# 张骞统一角色锚（与 m1_shots 一致）
ZQ_ANCHOR = (
    "中年汉族男子，约四十五岁，瘦削方正脸型，浓眉，蓄三绺长须，"
    "额间有岁月纹路，眼神坚定。汉代使者装束：赭色交领深衣，腰间束带，"
    "头戴汉式进贤冠。"
)
# 定妆照要中性背景 + 大脸特写，方便人脸 embed
PORTRAIT_SUFFIX = (
    "character design sheet, bust portrait, centered face, "
    "neutral warm grey studio background, even soft lighting, "
    "sharp facial features, detailed, consistent identity"
)
# 各角度/表情
ANGLES = [
    ("front", "facing directly forward toward viewer"),
    ("three_quarter_left", "slightly turned toward viewer's left, three-quarter view"),
    ("right", "turned to viewer's right, profile right"),
    ("left", "turned to viewer's left, profile left"),
    ("front_serious", "facing forward, solemn determined expression"),
    ("front_gentle", "facing forward, composed steady expression"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(ANGLES))
    ap.add_argument("--out", default="zhangqian")
    args = ap.parse_args()

    out_dir = Path(f"D:/hv/resource/ref_packs/{args.out}")
    out_dir.mkdir(parents=True, exist_ok=True)

    img = get_image()
    imgs = []
    for i, (angle, desc) in enumerate(ANGLES[: args.n]):
        fp = out_dir / f"{i:02d}_{angle}.png"
        if fp.exists():
            print(f"  {fp.name} 已存在，跳过")
            imgs.append((angle, fp))
            continue
        prompt = f"{ZQ_ANCHOR} {desc}. {PORTRAIT_SUFFIX}"
        req = ImageGenRequest(model="gpt-image-2", prompt=prompt,
                              size="1080x1920", quality="low")
        print(f"[{i+1}/{min(args.n,len(ANGLES))}] 生成 {angle} ...")
        t0 = time.time()
        try:
            resp = img.generate(req)
            raw = base64.b64decode(resp.data[0].b64_json)
            fp.write_bytes(raw)
            print(f"    -> {time.time()-t0:.1f}s  {len(raw)/1024:.0f}KB")
            imgs.append((angle, fp))
        except Exception as e:
            print(f"    -> FAILED {type(e).__name__}: {e}")
        time.sleep(0.5)

    if not imgs:
        print("无可用定妆照"); return

    # 自检：两两相似度矩阵 + 挑 anchor
    print("\n=== 定妆照自检（人脸 embed 两两 sim）===")
    embs = {}
    for angle, fp in imgs:
        e, meta = embed_face(fp)
        n = len(detect_faces(fp))
        if e is None:
            print(f"  {fp.name}: 无人脸!")
        else:
            embs[fp.name] = e
            print(f"  {fp.name}: {n}脸 embed=OK")
    if len(embs) < 2:
        print("不足两张可用"); return

    # 平均相似度：每张 vs 其它
    avg = {}
    for name, e in embs.items():
        others = [cosine(e, embs[n2]) for n2 in embs if n2 != name]
        avg[name] = sum(others) / len(others) if others else 0
    names = sorted(avg, key=lambda n: -avg[n])
    print("\n=== 平均一致度（越高越适合作锚）===")
    for n in names:
        print(f"  {n}: {avg[n]:.3f}")

    anchor = names[0]
    # 移动/标记 anchor
    meta = {
        "character_id": args.out,
        "character_name": "张骞",
        "anchor_face": anchor,
        "images": [{"file": n, "angle": n.split("_", 1)[1].rsplit(".", 1)[0]} for n in embs],
    }
    # 存 embedding 供离线用
    import json
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n定妆包: {out_dir}")
    print(f"  anchor = {anchor} (avg sim {avg[anchor]:.3f})")


if __name__ == "__main__":
    main()
