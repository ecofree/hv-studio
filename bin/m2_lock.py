#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2 批量收脸：以定妆照为锚，用 edits 重生成 hw-001 全部剧情 shot。

方法（已验证：scene3 收脸 sim=0.835 成功）：
  1. 加载 resource/ref_packs/zhangqian 定妆包，取 anchor_face 的 embed
  2. 对每个 shot，把 scene_prompt 用 ZHANGQIAN_ANCHOR + 该场景描述组 prompt
  3. 以定妆照为参考图调 /v1/images/edits（顶层 image=b64）
  4. 产出后 insightface 检脸，cosine vs 锚；sim>=SIM_OK 记过，否则标记重做

产出写到 shots/ 覆盖原图，登记 shot.face_sim / shot.ref_pack_id。
"""
import argparse
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "D:/hv")

from app.managers import load, save
from app.services.character_lock import SIM_ACCEPT, anchor_embedding, load_pack
from app.services.face import cosine, detect_faces, embed_face
from app.services.image import get_image
from app.models import ImageEditRequest

ZHANGQIAN_ANCHOR = (
    "中年汉族男子，约四十五岁，瘦削方正脸型，浓眉，蓄三绺长须，"
    "额间有岁月纹路，眼神坚定。汉代使者装束：赭色交领深衣，腰间束带，"
    "头戴汉式进贤冠。"
)
IDENTITY = ("Preserve the exact identity, facial geometry, age, hairstyle, "
            "headdress and wardrobe of the man in the reference image. "
            "Do NOT copy his facial expression or pose from the reference. ")

SCENES = {
    "shot-000": "大汉未央宫，清晨。年轻使臣张骞双手接过皇帝赐下的符节，俯身拜谢。金殿宏阔，两侧列队。",
    "shot-001": "荒漠戈壁，汉使一行在旷野被一队匈奴骑兵包围。匈奴骑士执弓围拢，张骞握节立于马侧，神色警觉。",
    "shot-002": "匈奴单于大帐内，火光摇曳。单于高坐兽皮宝座，摆手劝降；张骞正坐于下首，手持符节，神情坚毅不屈。",
    "shot-003": "张骞在匈奴营地被软禁，虽身穿胡地粗衣，仍将汉节紧握在怀，望向帐外，目光坚定。月夜，篝火，四周是匈奴毡帐。",
    "shot-004": "大雪纷飞的塞外，张骞与匈奴随从在草原放牧，须发已见斑白，但手中的汉节始终没有放下。苍茫辽阔的北地风光。",
    "shot-005": "张骞独自骑驼穿越无垠沙漠，衣袍猎猎，前方地平线隐约可见汉地烽燧与长城轮廓，落日金光。他终于踏上归途。",
}
STYLE = ("ancient chinese historical painting style, cinematic composition, "
         "dramatic lighting, warm earthy tone, highly detailed")

CHAR_ID = "zhangqian"


def b64_of(p) -> str:
    return base64.b64encode(Path(p).read_bytes()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只重做某个 shot-00X")
    args = ap.parse_args()

    pack = load_pack(CHAR_ID)
    if pack is None or pack.anchor_face_path is None:
        print("定妆包不存在或缺少 anchor，先跑 bin/m2_refpack.py"); return
    anchor_emb = anchor_embedding(pack)
    print(f"锚: {pack.anchor_face_path} embed={'OK' if anchor_emb is not None else 'FAIL'}")
    if anchor_emb is None:
        return
    anchor_b64 = b64_of(pack.anchor_face_path)

    ep = load("hw-001")
    img = get_image()
    shots_dir = Path("D:/hv/episodes/hw-001/shots")

    results = {}
    for sid in sorted(ep.shots):
        if args.only and sid != args.only:
            continue
        shot = ep.shots[sid]
        scene = SCENES.get(sid, shot.subtitle_text)
        prompt = f"{ZHANGQIAN_ANCHOR} {IDENTITY} {scene} {STYLE}"

        fp = shots_dir / f"{sid}.png"
        req = ImageEditRequest(model="gpt-image-2", prompt=prompt,
                               image=anchor_b64, size="1080x1920", quality="low")
        print(f"\n[{sid}] edits 收脸 ...")
        t0 = time.time()
        try:
            resp = img.edit(req)
            raw = base64.b64decode(resp.data[0].b64_json)
            fp.write_bytes(raw)
            print(f"  -> {time.time()-t0:.1f}s {len(raw)/1024:.0f}KB")
        except Exception as e:
            print(f"  -> FAILED {type(e).__name__}: {e}")
            results[sid] = -1
            continue

        # 检脸 + 一致性
        nf = len(detect_faces(fp))
        p_emb, meta = embed_face(fp)
        if p_emb is None:
            print(f"  -> {nf}脸 无人脸可评")
            results[sid] = 0.0
        else:
            sim = cosine(anchor_emb, p_emb)
            results[sid] = round(sim, 3)
            shot.face_sim = results[sid]
            shot.face_sim_warn = sim < SIM_ACCEPT
            shot.ref_pack_id = CHAR_ID
            shot.ref_pack_paths = [pack.anchor_face_path]
            shot.image_actual_size = (1080, 1920)
            print(f"  -> {nf}脸 sim={results[sid]:.3f} "
                  f"{'OK' if shot.face_sim>=SIM_ACCEPT else '<-- 低'}")

        save(ep)  # 每张都落盘，防中断丢失
        time.sleep(0.5)

    print("\n=== 收脸结果 ===")
    for sid, s in results.items():
        flag = "OK" if (s >= SIM_ACCEPT) else ("低" if s > 0 else "失败")
        print(f"  {sid}: {s if s>=0 else 'N/A'}  [{flag}]")


if __name__ == "__main__":
    main()
