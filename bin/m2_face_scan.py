#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 人脸一致性验证脚本（任务13 前哨）。

在 hw-001 6 张 shot 图上跑人脸检测 + embed，
打印每人脸数量 / 置信度 / embed 是否可得，并算 anchor->各 shot 相似度，
验证 insightface 能否检出「shot-003 换脸」。

用法：
  python bin/m2_face_scan.py [episode]
"""
import sys
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.services.face import cosine, detect_faces, embed_face


def main():
    ep = sys.argv[1] if len(sys.argv) > 1 else "hw-001"
    shots_dir = Path(f"D:/hv/episodes/{ep}/shots")
    pngs = sorted(shots_dir.glob("shot-0*.png"))
    if not pngs:
        print("没有找到 shot 图"); return

    print("=== 各 shot 人脸检测 ===")
    embeds = {}
    meta = {}
    for p in pngs:
        faces = detect_faces(p)
        if not faces:
            print(f"  {p.name}: 未检测到人脸!")
            continue
        n = len(faces)
        biggest = max(faces, key=lambda f: (f.x1 - f.x0) * (f.y1 - f.y0))
        bw, bh = biggest.x1 - biggest.x0, biggest.y1 - biggest.y0
        e, m = embed_face(p)
        print(f"  {p.name}: {n} 脸, 最大脸 {bw}x{bh}@{biggest.conf:.2f}, "
              f"embed={'OK' if e is not None else 'FAIL'}")
        if e is not None:
            embeds[p.name] = e
            meta[p.name] = m

    if len(embeds) < 2:
        print("\n不足以做相似度对比"); return

    print("\n=== anchor(shot-000) -> 各 shot 相似度 ===")
    anchor_name = pngs[0].name
    anchor = embeds[anchor_name]
    for name, e in embeds.items():
        sim = cosine(anchor, e)
        flag = "  <-- 疑似换脸" if sim < 0.50 else ""
        print(f"  {anchor_name} vs {name}: {sim:.3f}{flag}")


if __name__ == "__main__":
    main()
