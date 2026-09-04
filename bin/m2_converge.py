#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 收脸验证：以「高清定妆照」为锚，用 edits 生成剧情 shot，看能否收敛。

前提：单锚 edits 用 shot-000(差锚) 只有 0.44。现在换「专用定妆照」(主脸
295-314px、conf0.82+) 当参考图，验证张骞脸能否朝锚收敛（sim>0.55）。

用法：python bin/m2_converge.py --scene 3
"""
import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.services.face import cosine, detect_faces, embed_face
from app.services.image import get_image
from app.models import ImageEditRequest

ANCHOR = Path("D:/hv/resource/ref_packs/zhangqian/01_three_quarter_left.png")

SCENES = {
    3: "张骞在匈奴营地被软禁，虽身穿胡地粗衣，仍将汉节紧握在怀，望向帐外，目光坚定。月夜，篝火，四周是匈奴毡帐。",
    2: "匈奴单于大帐内，火光摇曳。单于高坐兽皮宝座，摆手劝降；张骞正坐于下首，手持符节，神情坚毅不屈。",
    5: "张骞独自骑驼穿越无垠沙漠，衣袍猎猎，前方地平线隐约可见汉地烽燧与长城轮廓，落日金光。",
}
STYLE = ("ancient chinese historical painting style, cinematic composition, "
         "dramatic lighting, warm earthy tone, highly detailed")
IDENTITY = ("Preserve the exact identity, facial geometry, age, hairstyle, "
            "headdress and wardrobe of the man in the reference image. "
            "Render the new scene faithfully. ")


def b64_of(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", type=int, default=3)
    args = ap.parse_args()
    scene_text = SCENES.get(args.scene, SCENES[3])

    a_emb, _ = embed_face(ANCHOR)
    if a_emb is None:
        print(f"锚图 {ANCHOR} 无人脸"); return
    print(f"锚 {ANCHOR.name} embed=OK")

    prompt = f"{IDENTITY}{scene_text} {STYLE}"
    req = ImageEditRequest(model="gpt-image-2", prompt=prompt,
                           image=b64_of(ANCHOR), size="1080x1920", quality="low")
    img = get_image()
    print(f"edits 生成 scene {args.scene} ...")
    t0 = time.time()
    resp = img.edit(req)
    raw = base64.b64decode(resp.data[0].b64_json)
    out = Path(f"D:/hv/episodes/hw-001/shots/converge_s{args.scene}.png")
    out.write_bytes(raw)
    print(f"  -> {time.time()-t0:.1f}s  {len(raw)/1024:.0f}KB  {out.name}")

    # 产出人脸 vs 锚
    faces = detect_faces(out)
    p_emb, p_meta = embed_face(out)
    if p_emb is None:
        print("  产出无人脸"); return
    sim = cosine(a_emb, p_emb)
    print(f"  产出 {len(faces)}脸  锚 vs 产出 sim={sim:.3f}")
    print(f"  ['收敛OK' if sim>=0.55 else ('不足' if sim>=0.4 else '失败')]")
    print(f"  产出图: {out}")


if __name__ == "__main__":
    main()
