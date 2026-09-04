#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
hw-002《张骞凿空》场景资产生成（S2）

只生成 24 张「场景资产底图」，供 93 个 frame 复用。
机位（zoom）/ 光线（grade）两轴由 bin/m2_shots.py 在本地派生，零额外 API 成本。

用法：
    python bin/m2_scenes.py            # 生成缺失的场景图（断点续跑）
    python bin/m2_scenes.py SC07 SC08  # 只重生成指定场景（先删旧图）
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

EP = "hw-002"
SHOTS_DIR = Path(f"D:/hv/episodes/{EP}/shots")
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 角色锚定（贯穿全集，保证张骞跨镜头是同一张脸）
# ---------------------------------------------------------------------------
ZHANGQIAN_ANCHOR = (
    "中年汉族男子，公元前二世纪的汉朝使者装束：赭色交领深衣，腰间束带，"
    "头戴汉式进贤冠，手持一根系着牦牛尾装饰的木质符节（使节之节）。"
)
STYLE_SUFFIX = (
    "ancient chinese historical painting style, cinematic composition, "
    "dramatic lighting, warm earthy tone, highly detailed, masterpiece"
)

# ---------------------------------------------------------------------------
# 24 幕场景资产
#   frames : (起, 止) 闭区间，对应 episodes/hw-002.json 的 frame 索引
#   anchor : 是否注入张骞角色锚定（他不在场的场景不注入，避免构图被绑架）
#   lights : 该场景的光线循环（本地调色，不消耗 API）
# ---------------------------------------------------------------------------
SCENES = [
    dict(id="SC01", name="未央宫朝堂", frames=(0, 6), anchor=False,
         lights=("dawn", "dawn", "noon", "dawn", "dawn", "noon", "dawn"),
         prompt="西汉未央宫前殿内景，清晨。年轻皇帝刘彻端坐御座，冕旒垂面，身披玄色龙袍，抬手向群臣发问。殿下两列文武百官身着深色朝服俯首肃立，无人敢应。巨大的木质殿柱林立，晨光自高窗斜射而入，尘埃在光柱中浮动，殿宇深邃宏阔。"),
    dict(id="SC02", name="西域未知", frames=(7, 10), anchor=False,
         lights=("noon", "dusk", "noon", "dusk"),
         prompt="广袤未知的西域荒景，无边的流沙戈壁上风暴卷起漫天黄沙，远处连绵雪山在尘霾中若隐若现，天空阴沉压顶，几只黑鸟掠过。天地之间有一支极小的人马剪影，几乎被荒原吞没，强烈的压迫感与未知感。"),
    dict(id="SC03", name="张骞应募", frames=(11, 12), anchor=True,
         lights=("dawn", "dawn"),
         prompt="西汉朝堂一角，强烈逆光。年轻的低级郎官从百官队列中迈步出列，跪拜于玉阶之前，身姿单薄却挺直，两侧官员侧目而视。晨光从他身后高大的殿门涌入，勾出人物的金色轮廓，地面是反光的青砖。"),
    dict(id="SC04", name="长安出发", frames=(13, 15), anchor=True,
         lights=("dawn", "noon", "dawn"),
         prompt="清晨的长安城门外，汉使使团整装待发。百余名随从牵马列队，马背驮着行囊与丝帛，为首的使者手持符节立于车队之前。远处是夯土城墙与巍峨城楼，薄雾未散，第一缕晨光落在旗帜与矛尖上。"),
    dict(id="SC05", name="河西走廊", frames=(16, 16), anchor=True,
         lights=("noon",),
         prompt="河西走廊的戈壁荒原，砾石遍地，一队人马与骆驼沿着古道的车辙向西行进，扬起淡淡的尘烟。地平线极远，天空辽阔，苍茫孤寂。"),
    dict(id="SC06", name="匈奴围困", frames=(17, 18), anchor=True,
         lights=("noon", "dusk"),
         prompt="戈壁之上，一队匈奴骑兵从三面高速包抄围合，骑手身着皮甲、挽弓搭箭，马蹄踏起滚滚烟尘。中央的汉使车队被迫停住，使者握节立于车旁，神情警觉，双方剑拔弩张地对峙。"),
    dict(id="SC07", name="匈奴王庭", frames=(19, 21), anchor=True,
         lights=("fire", "night", "fire"),
         prompt="匈奴单于的大帐内部，夜晚。单于端坐于铺着兽皮的高座，身披厚重裘衣，头戴皮冠，居高临下地俯视，面带讥讽。帐内火盆烈焰摇曳，皮帐壁上挂着弓箭与兽骨，气氛逼仄而充满威压。"),
    dict(id="SC08", name="草原羁押", frames=(22, 28), anchor=True,
         lights=("noon", "dusk", "noon", "dusk", "night", "noon", "dusk"),
         prompt="北方草原上的匈奴营地，白日。一座座圆顶毡帐散布，羊群在坡上吃草，一名匈奴女子牵着幼童立于帐前。远处的汉使独自站在草坡上，衣袍已半胡化，仍紧紧握着那根系着牦牛尾的汉节，望向东方。天空高远辽阔。"),
    dict(id="SC09", name="夜逃内乱", frames=(29, 30), anchor=False,
         lights=("night", "night"),
         prompt="深夜的匈奴营地陷入混乱，多处毡帐燃起大火，火把的光点四散奔窜，人喊马嘶。两骑黑影趁乱冲出营门，向西方策马疾驰，背后是燃烧的营地与混乱的人群。"),
    dict(id="SC10", name="西行葱岭", frames=(31, 33), anchor=True,
         lights=("frost", "frost", "night"),
         prompt="帕米尔高原的险峻雪山，风雪交加。一条狭窄的碎石山道盘绕在雪峰之间，两个裹着厚重毛皮的旅人牵马艰难跋涉，身影渺小。四周是冷青色的冰川与峭壁，天地肃杀。"),
    dict(id="SC11", name="大宛康居", frames=(34, 37), anchor=True,
         lights=("noon", "dusk", "noon", "dusk"),
         prompt="西域城邦景象，夯土城墙与圆顶土屋依绿洲而建，城外是灌溉的农田与成排白杨。一队西域骑兵护送着汉使骑马穿行在土墙街巷之间，骆驼队驮着货物涉过宽阔的河流。"),
    dict(id="SC12", name="大月氏妫水", frames=(38, 43), anchor=True,
         lights=("noon", "dawn", "noon", "dawn", "noon", "dawn"),
         prompt="妫水北岸的丰饶绿洲，水草肥美，土地肥沃。成群的牛羊在河边草甸上悠闲吃草，圆顶屋舍与果园散布其间，远处是青翠的山峦。居民安居乐业，一派平和富足，毫无战意。"),
    dict(id="SC13", name="大夏市集", frames=(44, 47), anchor=True,
         lights=("noon", "dusk", "noon", "dusk"),
         prompt="中亚古城大夏的露天集市，热闹繁华。土坯拱廊下摊位鳞次栉比，陈列着陶器、织物、香料与干果，头戴尖帽的西域商人操着各种语言讨价还价，人流如织，骆驼卧在街边，阳光下尘土浮动。"),
    dict(id="SC14", name="蜀布竹杖", frames=(48, 51), anchor=True,
         lights=("noon", "dusk", "noon", "dusk"),
         prompt="集市货摊前的特写：一双饱经风霜的手正拿起一根产自蜀地的邛竹杖，摊布上还铺着一块细密的蜀地麻布。货主是深目高鼻的西域商人，两人正在交谈。浅景深，背景是虚化的市集人流。"),
    dict(id="SC15", name="身毒道推演", frames=(52, 54), anchor=False,
         lights=("fire", "fire", "fire"),
         prompt="昏暗的室内，一幅摊开的羊皮与麻布地图铺在木案上，墨线勾着山川、城郭与道路。一只手正以炭条在图上从大夏向东南方向画出一条路线，烛火在旁摇曳，地图旁散落着写有注记的木牍。"),
    dict(id="SC16", name="归途再俘", frames=(55, 58), anchor=True,
         lights=("dusk", "night", "dusk", "night"),
         prompt="暮色中的长安城门，两名衣衫褴褛、满面风尘的旅人牵着一匹瘦马走到城下，守城士卒惊讶地看着他们。城墙高耸，城楼轮廓厚重，晚霞映在天边，护城河水泛着微光。"),
    dict(id="SC17", name="受封太中大夫", frames=(59, 59), anchor=True,
         lights=("noon",),
         prompt="未央宫殿内，衣冠整肃的朝会之上，汉使跪于御前双手接过诏令，皇帝高坐于御座之上。殿柱巍峨，日光透窗而入，仪仗肃穆庄严。"),
    dict(id="SC18", name="西域奇珍", frames=(60, 65), anchor=False,
         lights=("noon", "dusk", "noon", "dusk", "noon", "dusk"),
         prompt="汉宫庭院中陈列的西域奇珍：一匹毛色赤红、汗出如血的骏马被马夫牵着，旁边案几上摆着蜀地的竹杖与细布、异域的银质钱币，远处有西域幻术艺人在表演。观者围拢惊叹，宫廷廊柱为背景。"),
    dict(id="SC19", name="奏报竹简", frames=(66, 67), anchor=False,
         lights=("fire", "fire"),
         prompt="烛光下的书案，一编编竹简摊开，墨迹未干，简上密密写满文字。旁边摊着绘有山川城郭的帛图，笔与墨砚置于一侧。一只手正在新简上落笔，灯火跳动。"),
    dict(id="SC20", name="二次出使乌孙", frames=(68, 72), anchor=True,
         lights=("dawn", "noon", "dawn", "noon", "dawn"),
         prompt="规模浩大的汉使队伍西行于草原之上，三百人的使团队伍绵延不绝，六百匹马与成千上万的牛羊散布在水草丰美的河谷，旌旗招展，尘土飞扬，气势雄壮。"),
    dict(id="SC21", name="封侯与失侯", frames=(73, 74), anchor=False,
         lights=("fire", "night"),
         prompt="木案上置一方龟钮青铜侯印与紫色绶带，旁边散落着被免爵的简牍文书，烛火将熄，室内光线沉落，气氛苍凉。"),
    dict(id="SC22", name="病逝长安", frames=(75, 77), anchor=True,
         lights=("night", "fire", "night"),
         prompt="长安的一处宅室内，病榻之上，须发皆白的老者安然而卧，手中仍握着那根牦牛尾已被磨秃的汉节。窗外是暮色中的长安屋檐，室内一盏油灯将熄，光晕微弱。"),
    dict(id="SC23", name="史记凿空", frames=(78, 81), anchor=False,
         lights=("fire", "fire", "night", "fire"),
         prompt="幽暗的藏书室内，层层叠叠的竹简书卷堆至屋顶，一位史官伏案执笔，面前摊开的长卷上墨迹淋漓。一盏油灯照亮案面，光外是深不见底的黑暗与成排的简册。"),
    dict(id="SC24", name="丝路千年", frames=(82, 87), anchor=False,
         lights=("dawn", "dusk", "frost", "noon", "dusk", "dawn"),
         prompt="横贯欧亚大陆的宏大长卷：从东方的城墙出发，商队、僧侣与使节的剪影穿过沙漠、雪山、绿洲与城邦，一路向西延展至遥远的地平线。画面辽阔，晨昏交替的光影铺展在大地上。"),
]


def build_prompt(sc: dict) -> str:
    parts = []
    if sc.get("anchor"):
        parts.append(ZHANGQIAN_ANCHOR)
    parts.append(sc["prompt"])
    parts.append(STYLE_SUFFIX)
    return " ".join(parts)


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
    scenes = [s for s in SCENES if not targets or s["id"] in targets]

    todo = [s for s in scenes
            if not (SHOTS_DIR / f"{s['id']}.png").exists()
            or (SHOTS_DIR / f"{s['id']}.png").stat().st_size <= 200_000]
    print(f"场景总数 {len(scenes)}，待生成 {len(todo)}，并发 3")
    print("=" * 62)

    results, failed = [], []
    t_all = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(gen_one, s): s for s in todo}
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

    # 失败串行补一轮（换渠道）
    if failed:
        print(f"\n串行重试 {len(failed)} 个：{failed}")
        again = [s for s in SCENES if s["id"] in failed]
        for sc in again:
            try:
                r = gen_one(sc)
                print(f"  {sc['id']} OK(retry)  {r['seconds']}s  {sc['name']}")
                failed.remove(sc["id"])
            except Exception as e:
                print(f"  {sc['id']} FAIL(retry)  {str(e)[:160]}")

    (SHOTS_DIR / "scenes_meta.json").write_text(
        json.dumps([{k: v for k, v in s.items() if k != "prompt"} for s in SCENES]
                   + [{"prompts": {s["id"]: build_prompt(s) for s in SCENES}}],
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 62)
    print(f"完成 {len(scenes) - len(failed)}/{len(scenes)}，"
          f"用时 {(time.time() - t_all) / 60:.1f} 分钟")
    if failed:
        print(f"仍失败：{failed}")


if __name__ == "__main__":
    main()
