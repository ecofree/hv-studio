"""
【laogongtuan 专属 · 人工分镜单生成器】为 laogongtuan 依文本锚重建 scene_plan.json。

⚠️ 这是「这一集」的人工创意/编辑资产，不是通用自动化：
  - SCENES 列表里每一段的「起始锚(text 子串)、image、grade 主调」是人工逐段敲定的
    文本→图映射，编排器不可能知道「哪句台词配哪张图」。
  - 不要把它当通用模板复用到别的新集；新集应另行人工编写 scene_plan.json。
  - 保留此文件用于：当 splitter 切分版本变化导致帧序/边界变动时，靠锚重新对齐
    帧到 scene，重建 scene_plan.json（帧序无关，天然全覆盖）。

用法（任意目录，自动定位仓库根）：
    python episodes/laogongtuan/rebuild_plan.py

工作原理：每一 scene 给一个「起始锚」= 该 beat 开头旁白的独特子串；walk 全部帧，
把每帧归到「其文本包含哪条最新触发的锚」的 scene，得到连续不重叠的 [start,end]，
天然覆盖全部帧、与切分得到多少帧无关。
"""
from pathlib import Path
import json
import sys

# 仓库根：本文件在 <root>/episodes/<ep>/rebuild_plan.py → 上溯 2 级
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parents[2]))  # 让 app.* 可导入

from app.managers import load

ROOT = _THIS.parents[2]
ep = load('laogongtuan')
txts = [f.text for f in ep.frames]
N = len(txts)

# (image, 起始锚, grade 主调串 'a|b|c' 循环)
SCENES = [
    # ACT1 招聘/造神
    ("SC01",   "都说伴君如伴虎", "noon|dusk|dawn"),
    ("LGT01",  "后世那些编剧", "dawn|noon|dusk"),
    ("SC01",   "你看，刘彻十六岁", "noon|dusk|dawn"),
    ("LGT35",  "上面坐着崇尚黄老", "noon|dusk"),
    ("LGT02",  "刘彻心里那股邪火", "dusk|night"),
    ("LGT03",  "他的招聘标准极其清奇", "noon|dusk"),
    ("LGT36",  "刘彻要的，是那些", "dusk|noon"),
    ("LGT04",  "用刘彻自己的话说", "dawn|noon"),
    ("LGT05",  "于是，奇迹发生了", "noon|dawn|dusk"),
    ("LGT06",  "整个大汉王朝的年轻人全疯了", "dusk|fire"),
    ("LGT37",  "但他们当时并不知道", "night|fire"),
    # ACT2a 主父偃
    ("LGT07",  "主父偃前半生倒霉透顶", "frost|dawn"),
    ("LGT38",  "万般无奈，他跑到未央宫", "frost|dawn"),
    ("SC01",   "刘彻早晨看到奏折", "noon|dusk"),
    ("LGT39",  "刘彻找主父偃，是来当兄弟的吗", "noon|dusk"),
    ("LGT08",  "各地同姓诸侯王地盘太大", "noon|dusk"),
    ("LGT40",  "强硬削藩容易逼反诸侯", "noon|dusk"),
    ("LGT09",  "主父偃眼睛一眨", "dusk|night"),
    ("LGT10",  "一时间，主父偃权倾朝野", "noon|dusk"),
    ("LGT41",  "有朋友劝他", "dusk|noon"),
    ("LGT11",  "齐王自尽的消息传回长安", "dusk|night|fire"),
    ("LGT42",  "他明知道推恩令和调查齐王", "dusk|night|fire"),
    ("LGT12",  "你看，这就是替老板干脏活", "fire|night"),
    # ACT2b 桑弘羊
    ("LGT13",  "汉匈全面开战", "dusk|fire"),
    ("LGT14",  "桑弘羊是个算盘精", "dawn|noon"),
    ("LGT43",  "面对空空如也的国库", "noon|dusk"),
    ("LGT15",  "这哪是割韭菜", "dusk|fire"),
    ("LGT44",  "大汉朝的中产商人", "dusk|night"),
    ("LGT16",  "桑弘羊成了刘彻最好用的人肉印钞机", "night|fire"),
    ("LGT45",  "替皇帝背了一辈子与民争利", "fire|night"),
    ("LGT12",  "在刘彻的大棋盘上", "dusk|night"),
    # ACT3a 武将
    ("SC01",   "如果说文臣在刘彻眼里", "dusk"),
    ("NEW-4",  "不可否认的是，刘彻给卫青", "fire|dusk|dawn"),
    ("NEW-6",  "而那个被称为天降战神的霍去病", "dawn|noon|dusk"),
    ("LGT17",  "可是，你看过史记里那个被撕", "frost|dawn"),
    ("LGT46",  "他立下盖世奇功", "frost|dawn"),
    ("LGT18",  "他一辈子在朝廷里低眉顺眼", "frost|dawn"),
    ("SC01",   "满朝文武全看懂了老板的脸色", "noon|dusk|dawn"),
    ("LGT47",  "卫青不争不抢", "frost|dawn|noon"),
    ("NEW-6",  "至于霍去病", "frost|dawn|night"),
    ("LGT48",  "但如果霍去病多活二十年", "night|fire|frost"),
    # ACT3b 司马迁
    ("SC01",   "如果说卫青和霍去病，是用毕生", "noon|dusk"),
    ("NEW-8",  "天汉二年，李广的孙子李陵", "fire|dusk|night"),
    ("LGT19",  "消息传回长安", "fire|dusk"),
    ("LGT49",  "唯独太史令司马迁", "dusk|night"),
    ("LGT20",  "暴怒的皇帝下令", "night|frost"),
    ("LGT21",  "在阴暗潮湿的蚕室里", "night|frost"),
    ("LGT50",  "他痛不欲生，几次想自尽", "night|frost"),
    ("LGT22",  "司马迁在滴血的残躯上", "night|frost"),
    # ACT4a 巫蛊
    ("LGT23",  "步入老年的刘彻", "dusk|night"),
    ("LGT51",  "他天天在未央宫里喝露水", "dusk|night"),
    ("LGT24",  "而这一次，被推上刑场的", "dusk|dawn"),
    ("LGT25",  "酷吏江充，成了刘彻晚年", "night|fire"),
    ("LGT26",  "最后，江充把涂满毒液的铁锹", "fire|night"),
    ("LGT52",  "太子被迫起兵诛杀江充", "fire|night"),
    ("LGT27",  "他根本不听任何解释", "fire"),
    ("LGT28",  "最终，太子刘据兵败逃亡", "night|frost"),
    ("LGT53",  "整个卫氏家族", "night|frost"),
    ("LGT29",  "不久之后，白发苍苍的老臣令狐茂", "dawn|frost"),
    ("LGT54",  "那一刻，仿佛一道晴天霹雳", "dawn|frost"),
    ("LGT30",  "他下令将江充夷灭三族", "frost|dawn"),
    ("LGT55",  "风烛残年的老皇帝", "dusk|frost"),
    ("LGT31",  "征和四年，站在崩溃边缘", "dusk|night"),
    # ACT4b 罪己诏+钩弋+收束
    ("SC01",   "他颁布了轮台罪己诏", "frost|dawn"),
    ("LGT56",  "这是两千年帝制史上绝无仅有的一幕", "frost|dawn"),
    ("LGT32",  "后元二年，刘彻卧病不起", "dusk|night"),
    ("LGT33",  "大臣们大惊失色", "night|frost"),
    ("SC01",   "你看，这就是汉武大帝刘彻", "noon|dusk|dawn"),
    ("LGT34",  "但那份在绝对权力面前伴君如伴虎", "frost|dawn|dusk"),
]

# 解析锚 → 首帧索引；顺序校验（每个锚只在其应处的区间内找）
resolved = []
prev_i = -1
for image, anchor, palette in SCENES:
    # 只在上一 scene 起点之后找，天然支持同一短语在别处重复出现
    idx = next((i for i in range(prev_i + 1, N)
                if anchor[:10] in txts[i] or anchor in txts[i]), None)
    if idx is None:
        raise SystemExit(f"锚未命中(prev={prev_i}): {anchor}")
    resolved.append((image, anchor, palette, idx))
    prev_i = idx

# 组装 scenes: 每 scene 覆盖 [start, 下一scene-1]，末 scene 到 N-1
scenes = []
for k, (image, anchor, palette, start) in enumerate(resolved):
    end = resolved[k + 1][3] - 1 if k + 1 < len(resolved) else N - 1
    pals = palette.split("|")
    grades = [pals[i % len(pals)] for i in range(end - start + 1)]
    scenes.append({"frames": [start, end], "image": image, "grades": grades})

# 校验全覆盖
occ = [None] * N
for sc in scenes:
    a, b = sc["frames"]
    for i in range(a, b + 1):
        occ[i] = sc["image"]
gaps = [i for i, c in enumerate(occ) if c is None]
if gaps:
    raise SystemExit(f"空洞帧: {gaps}")

# images
reuse = {"SC01": "hw-002", "NEW-4": "hanwu-feature", "NEW-6": "hanwu-feature", "NEW-8": "hanwu-feature"}
images = {k: {"reuse_from": v} for k, v in reuse.items()}
for k in sorted({sc["image"] for sc in scenes if sc["image"].startswith("LGT")}):
    images[k] = {"path": f"episodes/laogongtuan/shots/{k}.png"}

plan = {"episode": "laogongtuan", "images": images, "scenes": scenes}
out = ROOT / "episodes/laogongtuan/scene_plan.json"
out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

from collections import Counter
cnt = Counter(sc["image"] for sc in scenes)
print(f"scene_plan 重建: {len(scenes)} scenes / {N} 帧 / {len(images)} 底图 (新{len([k for k in images if k.startswith('LGT')])})")
print(f"归并(逐scene) {N/len(images):.1f}:1")
print("复用锚被引次数:", {k: cnt[k] for k in reuse})
print("全覆盖, 空洞=", bool(gaps))
