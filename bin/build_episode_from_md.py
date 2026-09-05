# -*- coding: utf-8 -*-
"""双栏分镜 md → episode 三件套 + frames（一次成型，帧序与 scene_plan 同源）。

为什么要有这个脚本
------------------
2026-09-05 lsm-15min 生产时，这套逻辑只活在工作站临时目录里
（%TEMP%/opencode/build_lsm15.py + init_lsm15.py），带来两个后果：

  1. 新片要重抄一遍清洗规则 → 项目最大可复现性缺口（审计 #4）。
  2. 临时脚本自建的 merge 逻辑与仓内 split_script 不同源：
     同一份 script.md，前者 324 帧、后者 392 帧，差 68 帧（21%）。
     任何人重跑 `stage script`，scene_plan 的 64 个帧区间就会整体错位，
     而装配阶段当时完全没有校验，只会静默产出一部错位的片子。

现在：解析/清洗/切分/合并全部复用 app.services.script，本脚本只负责
「读 md → 调切分 → 写四件产物」，并在 scene_plan 里写入帧指纹，
由 shot_builder 在装配时强制比对。

用法
----
    python bin/build_episode_from_md.py \\
        --ep lsm-15min \\
        --src "resource/风趣历史.writing-DNA/scripts/李世民_15分钟双栏影视化分镜剧本.md" \\
        --title "李世民15分钟" --series "风趣历史" --merge-short

    # 先看解析结果不动盘
    python bin/build_episode_from_md.py --ep x --src y.md --dry-run

    # 表格列序不同时（默认 镜号|...|画面|旁白，即 0/3/4）
    python bin/build_episode_from_md.py --ep x --src y.md --no-col 0 --visual-col 2 --narr-col 3

产物（episodes/{ep}/）
    script.md          清洗后旁白，一段一镜（空行分隔 = 段落边界 = merge 边界）
    image_prompts.json {镜号KEY: {"en": prompt}}  ← build_image_jobs 唯一数据源
    scene_plan.json    帧区间 + 底图 + 光线，含 frames_fingerprint
    ../{ep}.json       frames 直接落盘（不走 stage script，保证与 scene_plan 同源）

安全护栏
    · 目标 episode 已有 frames 时必须 --force 才覆盖（否则拒绝，防误毁既有音频）
    · 写出前对每一片做长度门禁检查，越界默认只 WARN，--strict 下转失败
    · --expect-shots N 校验解析出的镜数，防止表格改版后静默少解析
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.managers import save                                   # noqa: E402
from app.models import Episode, Frame                           # noqa: E402
from app.services.polyphone import preprocess                   # noqa: E402
from app.services.script import (                               # noqa: E402
    clean_narration, frames_fingerprint, merge_short_pieces,
    segment_script, split_long_sentence, split_script_text,
)
from app.services.shot_builder import GRADE_KEYS                # noqa: E402

# ---------------------------------------------------------------------------
# 镜头解析
# ---------------------------------------------------------------------------
ROW_NO = re.compile(r"\|\s*\*\*(\d+)\*\*\s*\|")       # | **1** | ...
PROMPT_IN_CELL = re.compile(r"\*Prompt:\s*(.*?)\*?\s*$", re.S)


def parse_shots(md: str, no_col: int, visual_col: int, narr_col: int,
                key_fmt: str = "L{:02d}") -> list[dict]:
    """解析双栏表格 → [{no, key, prompt, narr_raw}]。

    表格行内含 <br> 但无换行，所以逐行解析、不按段落。
    列号可覆盖：不同剧本模板的分栏数不一样（实测 6 列 / 5 列都出现过）。
    """
    shots: list[dict] = []
    need = max(no_col, visual_col, narr_col) + 1
    for line in md.splitlines():
        m = ROW_NO.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < need:
            continue
        visual, narr = cells[visual_col], cells[narr_col]
        pm = PROMPT_IN_CELL.search(visual)
        prompt = (pm.group(1) if pm else "").replace("<br>", " ").strip()
        prompt = prompt.rstrip("*").strip()
        no = int(m.group(1))
        shots.append({"no": no, "key": key_fmt.format(no),
                      "prompt": prompt, "narr_raw": narr})
    return shots


# ---------------------------------------------------------------------------
# 光线 grade 启发式
# ---------------------------------------------------------------------------
# 顺序敏感：先判「夜/阴森」，再判「火/血/战」，再判「雪/悲」，其余按序轮转。
# 关键词表提到模块级是为了可审计/可微调——它直接决定成片的冷暖基调。
G_NIGHT = r"夜|暴雨|梦魇|阴森|冤魂|地狱|噩梦|幽暗|漆黑|暴毙|七窍|鬼|魂"
G_HOT = r"火|血|战|杀|雷|烧|怒|焚|丹|毒|炮|锤|箭|刀|剑|屠|斩|墓"
G_COLD = r"雪|冰|霜|寒|冬|泥|雨|泪|哭|悲|凉|荒|陵|昭陵|退兵|死|葬|废|贬|崩溃|绝望"
GRADE_CYCLE = ["dawn", "noon", "dusk"]


def pick_grade(text: str, i: int) -> str:
    """按旁白+画面关键词挑光线基调。命中 fire 但同时有强冷意象时降级为 frost。"""
    if re.search(G_NIGHT, text):
        return "night"
    if re.search(G_HOT, text):
        return "frost" if (re.search(G_COLD, text)
                           and not re.search(r"火|血|战|杀|烧|焚", text)) else "fire"
    if re.search(G_COLD, text):
        return "frost"
    return GRADE_CYCLE[i % len(GRADE_CYCLE)]


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def build(args) -> int:
    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        print(f"[x] 源剧本不存在: {src}")
        return 2

    shots = parse_shots(src.read_text(encoding="utf-8"),
                        args.no_col, args.visual_col, args.narr_col,
                        key_fmt=args.key_fmt)
    if not shots:
        print(f"[x] 未从 {src.name} 解析出任何镜头行（检查 --no-col/--visual-col/--narr-col）")
        return 2
    if args.expect_shots and len(shots) != args.expect_shots:
        print(f"[x] 解析到 {len(shots)} 镜，期望 {args.expect_shots} 镜 "
              f"——表格结构可能变了，拒绝继续（去掉 --expect-shots 可强制）")
        return 2

    # 1) 清洗旁白
    narrs: list[str] = []
    for s in shots:
        cn = clean_narration(s["narr_raw"])
        if not cn:
            print(f"[x] {s['key']} 旁白清洗后为空，原值: {s['narr_raw'][:60]!r}")
            return 2
        narrs.append(cn)

    # 2) 切分（唯一入口，与 split_script 同源；merge 边界 = 段落 = 一个镜头）
    per_shot_pieces: list[list[str]] = []
    for cn in narrs:
        pieces: list[str] = []
        for sent in split_script_text(cn):
            pieces.extend(split_long_sentence(sent, max_chars=args.max_chars))
        per_shot_pieces.append(
            merge_short_pieces(pieces) if args.merge_short else pieces)

    pieces_all = [p for ps in per_shot_pieces for p in ps]
    lens = [len(p) for p in pieces_all]
    chars = sum(len(c) for c in narrs)
    print(f"{len(shots)}镜 / {len(pieces_all)}帧 / 旁白{chars}字，"
          f"预估音频 {chars / 3.9 / 60:.1f}min")
    print(f"单帧字数 最短{min(lens)} 最长{max(lens)}"
          f"{'  (已合并短片)' if args.merge_short else '  (未合并)'}")

    bad = [(i, p) for i, p in enumerate(pieces_all)
           if len(p) < args.min_chars or len(p) > args.max_join]
    if bad:
        print(f"[!] {len(bad)} 帧长度越界（<{args.min_chars} 或 >{args.max_join}）:")
        for i, p in bad[:10]:
            print(f"    #{i:03d} {len(p)}字: {p[:40]}")
        if args.strict:
            print("[x] --strict 下拒绝产出长度越界的分镜")
            return 2

    # 3) 指纹（把 scene_plan 锚定到这一次切分）
    fp = frames_fingerprint(pieces_all)
    print(f"帧指纹 {fp['count']}帧 sha1={fp['sha1']}")

    if args.dry_run:
        print("[dry-run] 未写盘。前 5 帧:")
        for i, p in enumerate(pieces_all[:5]):
            print(f"    frame-{i:03d} {p}")
        return 0

    out = ROOT / "episodes" / args.ep
    ep_path = ROOT / "episodes" / f"{args.ep}.json"

    # 4) 护栏：已有帧就别悄悄覆盖
    if ep_path.exists() and not args.force:
        try:
            old = json.loads(ep_path.read_text(encoding="utf-8"))
            if old.get("frames"):
                print(f"[x] {ep_path.name} 已有 {len(old['frames'])} 帧，"
                      f"覆盖会让既有音频/分镜失去锚点。确认要重建请加 --force。")
                return 2
        except Exception as e:
            print(f"[x] 读取既有 {ep_path.name} 失败 ({e})，加 --force 覆盖")
            return 2

    out.mkdir(parents=True, exist_ok=True)

    # 5) 落四件产物
    (out / "script.md").write_text("\n\n".join(narrs), encoding="utf-8")

    prompts = {s["key"]: {"en": s["prompt"]} for s in shots}
    empty_prompt = [s["key"] for s in shots if not s["prompt"]]
    if empty_prompt:
        print(f"[!] {len(empty_prompt)} 镜无 prompt: {empty_prompt[:8]}"
              f"（生图阶段会被 build_image_jobs 跳过）")
    (out / "image_prompts.json").write_text(
        json.dumps(prompts, ensure_ascii=False, indent=1), encoding="utf-8")

    scenes, images = [], {}
    idx = 0
    for si, (s, ps) in enumerate(zip(shots, per_shot_pieces)):
        n = len(ps)
        images[s["key"]] = {"path": f"episodes/{args.ep}/shots/{s['key']}.png"}
        grade = args.grade or pick_grade(narrs[si] + s["prompt"], s["no"])
        scenes.append({"frames": [idx, idx + n - 1], "image": s["key"],
                       "grades": [grade]})
        idx += n
    (out / "scene_plan.json").write_text(
        json.dumps({"episode": args.ep, "images": images, "scenes": scenes,
                    "frames_fingerprint": fp},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    ep = Episode(id=args.ep, title=args.title or args.ep,
                 series=args.series, episode_number=args.number)
    ep.script_path = str(out / "script.md").replace("\\", "/")
    # 直接落 frames：与 scene_plan 的帧区间同源，避免再走一次 stage script
    ep.frames = [Frame(id=f"frame-{i:03d}", text=p,
                       narration_text=preprocess(p))
                 for i, p in enumerate(pieces_all)]
    save(ep)

    hist: dict[str, int] = {}
    for sc in scenes:
        g = sc["grades"][0]
        hist[g] = hist.get(g, 0) + 1
    print(f"[OK] 已写 {out}/script.md image_prompts.json scene_plan.json")
    print(f"[OK] {ep_path.name}: {len(ep.frames)} frames 落盘")
    print(f"     grades: " + "  ".join(f"{k}={v}" for k, v in sorted(hist.items())))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="双栏分镜 md → episode 三件套 + frames（帧序与 scene_plan 同源）")
    ap.add_argument("--ep", required=True, help="episode id")
    ap.add_argument("--src", required=True, help="双栏分镜 md 路径")
    ap.add_argument("--title", default="")
    ap.add_argument("--series", default="风趣历史")
    ap.add_argument("--number", type=int, default=1)
    ap.add_argument("--key-fmt", default="L{:02d}",
                    help="底图 key 命名，默认 L{:02d}")
    # 表格列序
    ap.add_argument("--no-col", type=int, default=0)
    ap.add_argument("--visual-col", type=int, default=3)
    ap.add_argument("--narr-col", type=int, default=4)
    # 切分
    ap.add_argument("--merge-short", dest="merge_short", action="store_true",
                    default=True, help="合并过短片段（默认开，与 lsm-15min 一致）")
    ap.add_argument("--no-merge-short", dest="merge_short", action="store_false",
                    help="不合并（与 laogongtuan 一致）")
    ap.add_argument("--max-chars", type=int, default=18)
    ap.add_argument("--max-join", type=int, default=29, help="合并后单帧字数上限")
    ap.add_argument("--min-chars", type=int, default=6, help="单帧字数下限告警")
    ap.add_argument("--grade", default="",
                    help=f"强制单一光线，可选 {sorted(GRADE_KEYS)}")
    # 行为
    ap.add_argument("--expect-shots", type=int, default=0, help="校验解析镜数")
    ap.add_argument("--strict", action="store_true", help="长度越界即失败")
    ap.add_argument("--force", action="store_true", help="覆盖既有 frames")
    ap.add_argument("--dry-run", action="store_true")
    return build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
