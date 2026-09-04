#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
《汉武雄风》新增场景资产生成（一、三章 NEW-1~12）

复用 hw-002 的 m2_scenes 同款风格/尺寸/渠道池。只生成「场景资产底图」，
机位(zoom)/光线(grade) 派生留到 shots 层本地做。

用法：
    python bin/hanwu_new_scenes.py            # 生成全部缺失 NEW-1..12
    python bin/hanwu_new_scenes.py NEW-1 NEW-2  # 只重生成指定（先删旧图）
"""
import base64
import json
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, "D:/hv")

from app.models import ImageGenRequest
from app.services.image import get_image

EP = "hanwu-feature"
SHOTS_DIR = Path(f"D:/hv/episodes/{EP}/shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 角色锚定：新章主角（刘彻／卫青／霍去病），仅他出现的场景注入
# ---------------------------------------------------------------------------
LIUCHE_ANCHOR = (
    "青年汉族帝王，公元前二世纪汉武帝，头戴冕旒、身披玄色绣金龙袍，"
    "面容英武端严，蓄须。"
)
WEIQING_ANCHOR = (
    "中年汉族武将，汉朝将军装束：玄甲与绛色战袍，腰间佩剑，面容坚毅沉着。"
)
HUOQUBING_ANCHOR = (
    "少年汉族武将，约十七八岁英气逼人，汉朝轻甲与白色战袍，束发高冠，"
    "目光锐利，年少封侯的昂扬气度。"
)
XIONGNU_ANCHOR = (
    "匈奴单于/将领形象，皮甲裘帽，粗犷虬髯，骑射装束。"
)

STYLE_SUFFIX = (
    "ancient chinese historical painting style, cinematic composition, "
    "dramatic lighting, warm earthy tone, highly detailed, masterpiece"
)

# anchor: 无 → False；单个角色名字符串 → 注入对应锚定；多角色 tuple → 都注入
NEW_SCENES = [
    # ---------- 第一章 · 北击匈奴 ----------
    dict(id="NEW-1", name="白登之围", anchor=XIONGNU_ANCHOR,
         prompt="公元前二百年白登之围：冰雪覆盖的山谷中，汉军步骑被漫山遍野的匈奴骑兵围困，旗帜残破，士卒裹着破旧裘衣御寒，困于风雪。谷口匈奴弓骑环列，弯刀映雪。极致的困顿与压抑。"),
    dict(id="NEW-2", name="和亲车队", anchor=False,
         prompt="汉宫和亲：清晨长安宫墙下，一队披红挂彩的车驾向北而去，车上坐着盛装和亲的汉家公主，帷幔低垂，车队后是肃立的宫墙与旗幡。悲戚而庄重，红与土黄主调。"),
    dict(id="NEW-3", name="马邑之谷", anchor=XIONGNU_ANCHOR,
         prompt="马邑之谋：太行山北麓的马邑峡谷，谷道两侧山崖密布伏兵旗帜若隐若现，匈奴单于勒马立于谷口外止步不前，狐疑地望向空无一物的谷道与诱饵牛羊。紧张的对峙。"),
    dict(id="NEW-4", name="龙城夜袭", anchor=False,
         prompt="龙城之战：夜色中，汉军铁骑高举火把如潮水般冲入匈奴祭天圣地龙城，火光映照下旌旗猎猎，骑兵挥剑冲锋，匈奴营帐在火中坍倒。动感与壮烈。"),
    dict(id="NEW-5", name="河朔漠南大战", anchor=False,
         prompt="河朔之战：广袤草原上两军铁骑对垒冲杀，烟尘蔽日，汉军玄甲骑兵与匈奴弓骑混战，旌旗翻卷，马嘶人喊。宏大战争场面，动感构图。"),
    dict(id="NEW-6", name="少年霍去病拜将", anchor=("LIUCHE", "HUOQUBING"),
         prompt="长安未央宫朝堂，年轻的霍去病跪于丹墀受封，少年将军英气勃勃，武帝高坐御座赐印。殿宇巍峨，日光穿窗，少年封侯的荣光。"),
    dict(id="NEW-7", name="河西之战", anchor=("HUOQUBING",),
         prompt="祁连山下河西走廊，少年将军率汉骑铁流般冲过河谷，马蹄踏碎晨霜，远处祁连雪山连绵，敌骑溃散。壮阔苍茫，动势强烈。"),
    dict(id="NEW-8", name="漠北封狼居胥", anchor=("HUOQUBING",),
         prompt="漠北决战后的狼居胥山：大漠孤山之上，汉军旌旗林立，少年将军登坛祭天，将士山呼，苍鹰盘旋。天高云阔，大漠尽头残阳如血。凯旋的壮烈与孤绝。"),
    # ---------- 第三章 · 天马与帝国 ----------
    dict(id="NEW-9", name="万国来朝", anchor=("LIUCHE",),
         prompt="西域诸国使团入未央宫献礼：殿前广场各国使节驼马骈阗，捧献珍宝，武帝高坐丹墀受贺。异域服饰与汉宫庄严相映，盛世气象。"),
    dict(id="NEW-10", name="贰师城之战", anchor=False,
         prompt="大宛贰师城之战：汉军云梯冲车围攻夯土城墙西域城邦，箭矢如雨，城上守军投石，城外汉军铁甲如潮。激烈的攻城战，烟尘与火光。"),
    dict(id="NEW-11", name="天马入长安", anchor=False,
         prompt="汗血宝马入长安：一匹赤红如血、汗出如血的骏马昂首立于宫苑，肌理如铁，鬃尾飞扬，夕光为其镀上金边。马夫执缰，气象雄浑。"),
    dict(id="NEW-12", name="河西四郡玉门关", anchor=False,
         prompt="河西走廊尽头的玉门关：雄浑的夯土关城矗立戈壁，关下大道向西延展，商旅驼队与汉军戍卒并行，远处祁连雪山。边关的苍茫与通达。"),
]


def _anchor_text(a) -> str:
    if isinstance(a, tuple):
        parts = []
        for x in a:
            if x == "LIUCHE":
                parts.append(LIUCHE_ANCHOR)
            elif x == "HUOQUBING":
                parts.append(HUOQUBING_ANCHOR)
        return " ".join(parts)
    return a  # 直接字符串锚定（如 XIONGNU_ANCHOR）


def build_prompt(sc: dict) -> str:
    parts = []
    if sc.get("anchor"):
        parts.append(_anchor_text(sc["anchor"]))
    parts.append(sc["prompt"])
    parts.append(STYLE_SUFFIX)
    return " ".join(p for p in parts if p)


def gen_one(sc: dict) -> dict:
    fp = SHOTS_DIR / f"{sc['id']}.png"
    if fp.exists() and fp.stat().st_size > 200_000:
        return dict(id=sc["id"], skipped=True, path=str(fp))
    prompt = build_prompt(sc)
    req = ImageGenRequest(model="gpt-image-2", prompt=prompt,
                          size="1080x1920", quality="low")
    t0 = time.time()
    resp = get_image().generate(req)
    raw = base64.b64decode(resp.data[0].b64_json)
    fp.write_bytes(raw)
    return dict(id=sc["id"], name=sc["name"], skipped=False, path=str(fp),
                seconds=round(time.time() - t0, 1), kb=round(len(raw) / 1024),
                prompt=prompt)


def main():
    targets = sys.argv[1:]
    todo = [s for s in NEW_SCENES
            if not targets or s["id"] in targets]
    pending = [s for s in todo
               if not (SHOTS_DIR / f"{s['id']}.png").exists()
               or (SHOTS_DIR / f"{s['id']}.png").stat().st_size <= 200_000]
    print(f"目标 {len(todo)}，待生成 {len(pending)}，并发 3")
    print("=" * 62)

    results, failed = [], []
    t_all = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(gen_one, s): s for s in pending}
        for fu in as_completed(futs):
            sc = futs[fu]
            try:
                r = fu.result()
                if r.get("skipped"):
                    print(f"  {sc['id']} 已存在，跳过  {sc['name']}")
                else:
                    print(f"  {sc['id']} OK  {r['seconds']}s  {r['kb']}KB  {sc['name']}")
                results.append(r)
            except Exception as e:
                print(f"  {sc['id']} FAIL  {type(e).__name__}: {str(e)[:160]}")
                failed.append(sc["id"])

    if failed:
        print(f"\n串行重试 {len(failed)} 个：{failed}")
        again = [s for s in NEW_SCENES if s["id"] in failed]
        for sc in again:
            try:
                r = gen_one(sc)
                print(f"  {sc['id']} OK(retry)  {r['seconds']}s  {sc['name']}")
                failed.remove(sc["id"])
            except Exception as e:
                print(f"  {sc['id']} FAIL(retry)  {str(e)[:160]}")

    (SHOTS_DIR / "scenes_meta.json").write_text(
        json.dumps({"episode": EP,
                    "prompts": {s["id"]: build_prompt(s) for s in NEW_SCENES}},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 62)
    print(f"完成 {len(NEW_SCENES) - len(failed)}/{len(NEW_SCENES)}，"
          f"用时 {(time.time() - t_all) / 60:.1f} 分钟")
    if failed:
        print(f"仍失败：{failed}")


if __name__ == "__main__":
    main()
