#!/usr/bin/env python
"""Generate ``episodes/{id}/overlays.json`` — title card + act cards.

Act positions are resolved from **compose.build_timeline**, the same function the
renderer uses, so a card can never drift from where the act actually starts on
the timeline. Hand-computed millisecond offsets would be a second arithmetic
path and would rot the first time a voice take changed length.

    python bin/build_overlays.py caomin-02 \
        --title "公摊面积 75%" --subtitle "都说大唐白送 100 亩地" --badge "第 2 集" \
        --acts "公摊面积高达 75%,自费打工人,辽东破产倒计时,勋官彩票与散伙"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.managers import load                                    # noqa: E402
from app.services import compose as C                            # noqa: E402
from app.services import overlay as O                            # noqa: E402

ACT_HEAD = re.compile(r'^### 【(第.幕)[：:]([^】]*)】.*$', re.M)
DIRECTION = re.compile(r'^【[^】]*】')
INLINE_DIR = re.compile(r'【[^】]*】')


def act_spoken_leads(ep_id: str) -> list[tuple[str, list[str]]]:
    """(act heading, leading spoken paragraphs) per act.

    Direction blocks (【画面：…】【音效：…】【语气：…】) are stripped: they are
    stage directions, never spoken, and never become frame text.
    """
    src = (ROOT / "episodes" / ep_id / "script_narration.md").read_text(encoding="utf-8")
    parts = ACT_HEAD.split(src)
    out = []
    for i in range(1, len(parts), 3):
        spoken = []
        for para in parts[i + 2].split("\n\n"):
            t = para.strip()
            if not t or t.startswith(("#", "*", ">", "|", "-", "---")):
                continue
            t = INLINE_DIR.sub("", DIRECTION.sub("", t)).strip()
            t = " ".join(t.split())
            if len(t) > 8:
                spoken.append(t)
        out.append((f"{parts[i]}：{parts[i + 1]}", spoken))
    return out


def group_leads(ep, frames, timeline):
    """group id -> (lead narration text, start_ms), from the renderer timeline."""
    leads, starts = {}, {}
    for sh, s0, _s1 in timeline:
        g = C._group_of(sh.frame_id)
        starts.setdefault(g, s0)
        if g not in leads:
            leads[g] = " ".join((frames[sh.frame_id].text or "").split())
    return leads, starts


def resolve_act_start(leads, starts, spoken):
    """Timeline ms at which an act begins, found by matching narration text.

    Paragraph counting does NOT work here: the episode has 47 frame groups
    (one per plate) regardless of how many paragraphs the act contains, so
    "the Nth paragraph" has no relationship to a frame id. Matching the act's
    first spoken sentence against each group's lead text is what actually ties
    script structure to the timeline -- and it fails loudly rather than placing a
    card tens of seconds into the wrong act.
    """
    for para in spoken[:4]:
        for size in (24, 14, 8):
            needle = para[:size]
            for g, text in leads.items():
                if text.startswith(needle) or needle in text:
                    return starts[g], g
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ep_id")
    ap.add_argument("--title")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--badge", default="")
    ap.add_argument("--acts", default="", help="逗号分隔；个数需与剧本幕数一致")
    ap.add_argument("--title-ms", type=int, default=3400, help="标题卡展示时长")
    ap.add_argument("--act-ms", type=int, default=1800, help="幕卡展示时长")
    ap.add_argument("--dim", type=float, default=0.32)
    ap.add_argument("--no-scrim", action="store_true")
    ap.add_argument("--act1-card", action="store_true",
                    help="也给第一幕插卡。第一幕起点就是 0，与标题卡重叠，默认跳过")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ep = load(args.ep_id)
    shots = sorted(ep.shots.values(), key=lambda s: s.index)
    frames = {f.id: f for f in ep.frames}
    timeline, total_ms = C.build_timeline(shots, frames)

    acts = act_spoken_leads(args.ep_id)
    if not acts:
        print(f"[overlay] {args.ep_id} 的 script_narration.md 里没有 ### 【第N幕…】标题")
        return 2

    leads, starts = group_leads(ep, frames, timeline)
    resolved = []
    for heading, spoken in acts:
        ms, gid = resolve_act_start(leads, starts, spoken)
        if ms is None:
            print(f"[overlay] 无法把「{heading[:20]}」对到时间轴 —— 该幕旁白与任何帧组都对不上")
            print("           拒绝写入：宁可不出卡，也不要把卡插进错误的幕。")
            return 2
        resolved.append((heading, ms, gid))
        print(f"[overlay] {heading[:24]:28s} 起点 {ms/1000:8.2f}s  组 {gid}")

    specs: list[O.Spec] = []
    cursor = 0
    if args.title:
        specs.append(O.Spec(
            kind=O.KIND_TITLE, start_ms=0, end_ms=args.title_ms,
            title=args.title, subtitle=args.subtitle, episode=args.badge,
            fade_ms=400, dim=args.dim, scrim=not args.no_scrim))
        cursor = args.title_ms

    names = [n.strip() for n in args.acts.split(",") if n.strip()]
    if names and len(names) != len(resolved):
        print(f"[overlay] 幕卡 {len(names)} 个，剧本 {len(resolved)} 幕，数量不符")
        return 2

    for idx, ((heading, start, _gid), name) in enumerate(
            zip(resolved, names or [None] * len(resolved))):
        if name is None:
            continue
        # 第一幕起点即 0，与片头标题卡是同一时刻 —— 在它之后 200ms 插卡不是
        # 幕边界，只是一个碰巧靠得很近的位置，反而会让人误以为第一幕从那里开始。
        if idx == 0 and not args.act1_card:
            continue
        start = max(start, cursor + 200)
        specs.append(O.Spec(
            kind=O.KIND_ACT, start_ms=int(start), end_ms=int(start) + args.act_ms,
            title=name, fade_ms=350, dim=args.dim * 0.7, scrim=not args.no_scrim))
        cursor = int(start) + args.act_ms

    O.validate(specs, total_ms=total_ms)

    out = O.path_for(args.ep_id)
    body = {"version": O.SCHEMA_VERSION, "overlays": [s.payload() for s in specs]}
    print(f"[overlay] {args.ep_id}  全片 {total_ms/1000:.1f}s，{len(specs)} 张卡：")
    for s in specs:
        print(f"           {s.kind:11s} {s.start_ms/1000:7.2f}s → {s.end_ms/1000:7.2f}s  {s.title}")
    print(f"[overlay] 指纹 {O.Plan(specs).fingerprint()}")
    if args.dry_run:
        print("[overlay] --dry-run，未写入")
        return 0
    import json
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[overlay] 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())