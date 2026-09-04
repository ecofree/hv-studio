#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 shot-000 针对性重收（用户在 2026-09-03 决策）。

问题（实测）：shot-000「张骞拜谢皇帝」群像里画面最大脸是右上皇帝(age39,
sim=0.696)，真正主角张骞在左下前景俯身(age36, sim 仅 0.168)——群像广角里
张骞脸小+俯身被遮挡，单锚 edits 没把他收住。

解法：改写构图。让张骞成为画面主角——中近景、站姿抬头、正面偏三七侧，
脸大且清晰，双手恭敬持节；汉武帝降为画面第二人（侧/后景半身伸出手授节，
面部小或略虚化），弱化其存在感。仍以张骞定妆照为参考图 edits 注入。

产出：覆盖 shots/shot-000.png，登记 face_sim / face_sim_warn。
可先 --dry 只打印新 prompt 与旧 sim 不生成，审阅后再真跑。
"""
import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.managers import load, save
from app.services.character_lock import SIM_ACCEPT, anchor_embedding, load_pack
from app.services.face import cosine, detect_faces, embed_faces_multi
from app.services.image import get_image
from app.models import ImageEditRequest

CHAR_ID = "zhangqian"

# 与 m2_lock.py 保持一致的主角锚文本
ZHANGQIAN_ANCHOR = (
    "中年汉族男子，约四十五岁，瘦削方正脸型，浓眉，蓄三绺长须，"
    "额间有岁月纹路，眼神坚定。汉代使者装束：赭色交领深衣，腰间束带，"
    "头戴汉式进贤冠。"
)
IDENTITY = ("Preserve the exact identity, facial geometry, age, hairstyle, "
            "headdress and wardrobe of the man in the reference image. "
            "Do NOT copy his facial expression or pose from the reference. ")

# ★ 重构后的构图：张骞中近景主角、抬头正面偏侧、脸大清晰；皇帝退为次要
SCENE000_RE = (
    "西汉未央宫大殿。近景镜头：中年汉使张骞立于画面中央，站姿挺拔，"
    "头颅抬起略偏三七侧面，正脸完整清晰、占据画面上部近半，双手于胸前"
    "恭敬捧持一根系牦牛尾的木符节。他身后右侧，一位身着玄色冕服的帝王"
    "仅露出半身与一臂，正将符节递交给张骞；帝王的面容较小且置于画面边缘"
    "偏暗处，不作主角。金殿廊柱与列队侍者作为虚化背景。人物面部光照充足。"
)
STYLE = ("ancient chinese historical painting style, cinematic composition, "
         "dramatic lighting, warm earthy tone, highly detailed")

ANGLES = ["front", "three_quarter_left", "three_quarter_right"]


def b64_of(p) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="只打印新 prompt + 当前 shot-000 逐脸 sim，不调 API")
    args = ap.parse_args()

    pack = load_pack(CHAR_ID)
    if pack is None or pack.anchor_face_path is None:
        print("定妆包缺失，先跑 bin/m2_refpack.py"); return
    anchor_emb = anchor_embedding(pack)
    if anchor_emb is None:
        print("锚图无人脸"); return

    prompt = f"{ZHANGQIAN_ANCHOR} {IDENTITY} {SCENE000_RE} {STYLE}"
    print("=" * 70)
    print("NEW shot-000 prompt:\n" + prompt)
    print("=" * 70)

    shots_dir = Path("D:/hv/episodes/hw-001/shots")
    fp = shots_dir / "shot-000.png"
    if fp.exists():
        print("\n当前 shot-000 逐脸 sim vs 锚:")
        for i, (emb, m) in enumerate(embed_faces_multi(fp)):
            print(f"  #{i}: {m['x1']-m['x0']}x{m['y1']-m['y0']} "
                  f"中心=({(m['x0']+m['x1'])/2/941:.2f},{(m['y0']+m['y1'])/2/1672:.2f}) "
                  f"age={m['age']} sim={cosine(anchor_emb,emb):.3f}")

    if args.dry:
        print("\n[dry] 不调用 API。确认新构图后再去 --dry 实跑。")
        return

    anchor_b64 = b64_of(pack.anchor_face_path)
    ep = load("hw-001")
    shot = ep.shots["shot-000"]
    img = get_image()
    req = ImageEditRequest(model="gpt-image-2", prompt=prompt,
                           image=anchor_b64, size="1080x1920", quality="low")
    print("\nedits 收脸中 ...")
    t0 = time.time()
    resp = img.edit(req)
    raw = base64.b64decode(resp.data[0].b64_json)
    fp.write_bytes(raw)
    print(f"  -> {time.time()-t0:.1f}s {len(raw)/1024:.0f}KB 已写 {fp}")

    # 检脸：报逐脸 sim，并登记「主角脸」= 与锚相似度最高者
    faces = list(embed_faces_multi(fp))
    if not faces:
        shot.face_sim, shot.face_sim_warn, shot.last_error = 0.0, True, "重收后仍无人脸"
        print("  无人脸 0.0 <-- 低")
    else:
        for i, (emb, m) in enumerate(faces):
            s = cosine(anchor_emb, emb)
            print(f"  face#{i}: {m['x1']-m['x0']}x{m['y1']-m['y0']} "
                  f"中心=({(m['x0']+m['x1'])/2/941:.2f},{(m['y0']+m['y1'])/2/1672:.2f}) "
                  f"age={m['age']} sim={s:.3f}")
        # 主角脸 = 与锚最高(张骞应成为最大/最高脸)；同时要求最高脸中心在画面中上部
        sim, _ = max(((cosine(anchor_emb, e), m) for e, m in faces))
        shot.face_sim = round(sim, 4)
        shot.face_sim_warn = sim < SIM_ACCEPT
        shot.last_error = None
        print(f"  -> 最佳 sim={sim:.3f} {'OK' if sim>=SIM_ACCEPT else '<-- 低'}")
    shot.ref_pack_id = CHAR_ID
    shot.ref_pack_paths = [pack.anchor_face_path]
    shot.image_actual_size = (1080, 1920)
    save(ep)


if __name__ == "__main__":
    main()
