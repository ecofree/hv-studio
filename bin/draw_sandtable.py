#!/usr/bin/env python
"""Self-drawn narrative sandtables, rendered to 940x1672 plates.

Drawn with PIL rather than matplotlib: the whole render pipeline is PIL, the
box is memory-constrained, and these need to sit beside warm ink-wash plates --
so they are drawn in the same warm palette instead of a cold technical-blue map.

Narrative, not cartographic. Ward boundaries are exaggerated and the labels are
the ones the narration actually names. Academic accuracy would be worse here:
the shot exists to answer "where is this relative to that", not to be surveyed.

    python bin/draw_sandtable.py changan  -> assets/sandtables/changan_fangshi.png
    python bin/draw_sandtable.py tongguan  -> assets/sandtables/tongguan_route.png
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 940, 1672
OUT = ROOT / 'assets' / 'sandtables'
FONT_PATH = 'C:/Windows/Fonts/NotoSerifSC-VF.ttf'

# ---------------------------------------------------------------------------
# Delivered size: 540x960, not the 940x1672 we lay out in.
# ---------------------------------------------------------------------------
# `plan_frames` treats zoom=1.0 as "one source pixel per output pixel", not "show
# the whole plate". A 940x1672 plate is upscaled 2x to 1880x3344 and then cropped
# to a 1080x1920 window -- only **57% of the plate is ever visible**, for every
# plate in the series, not just these. Laid out at 940-wide, the map's title and
# captions sat in the region that is always thrown away.
#
# 540x960 upscaled 2x is exactly 1080x1920, so the crop box equals the image and
# the composition survives intact. Verified against plan_frames, not assumed.
PLATE_W, PLATE_H = 540, 960


def save_plate(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.resize((PLATE_W, PLATE_H), Image.LANCZOS).save(path)
    return path

# Warm ink-wash palette, matched by eye against the existing plates.
INK_TOP    = (38, 28, 20)
INK_BOT    = (26, 19, 14)
WALL       = (201, 162, 39)
WALL_DIM   = (150, 118, 34)
BLOCK      = (92, 71, 48)
BLOCK_EDGE = (138, 108, 68)
AVENUE     = (176, 138, 74)
TEXT       = (240, 230, 210)
TEXT_DIM   = (176, 160, 136)
ACCENT     = (214, 178, 106)
WATER      = (58, 74, 86)

_cache: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _cache:
        _cache[size] = ImageFont.truetype(FONT_PATH, size)
    return _cache[size]


def backdrop(d: ImageDraw.ImageDraw) -> None:
    """Vertical warm gradient -- flat fills look pasted on next to graded plates."""
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=tuple(int(INK_TOP[i] + (INK_BOT[i] - INK_TOP[i]) * t) for i in range(3)))


def label(d, xy, text, size=30, fill=TEXT, anchor='mm'):
    d.text(xy, text, font=font(size), fill=fill, anchor=anchor)


def wide(d, box, fill=None, outline=None, width=4, r=6):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def changan(path: Path) -> Path:
    """Chang'an ward grid -- 皇城, 朱雀大街, 东/西市, wards, walls and gates."""
    img = Image.new('RGB', (W, H), INK_BOT)
    d = ImageDraw.Draw(img)
    backdrop(d)

    label(d, (W // 2, 92), '长安城坊市格局', 62, TEXT)
    label(d, (W // 2, 162), '一百零八坊 · 朱雀大街 · 东市西市', 30, TEXT_DIM)
    label(d, (W // 2, 214), '终 南 山', 28, (128, 124, 100))

    top, bot = 256, 1244
    cx = W // 2
    x0, x1 = cx - 384, cx + 384

    # 城墙外廓（东南内凹 = 曲江）
    wall = [(x0 + 46, top), (x1 - 12, top + 26), (x1, top + 150),
            (x1 - 24, bot - 70), (x1 - 160, bot), (x0 + 66, bot - 12),
            (x0 - 12, bot - 230), (x0, top + 120)]
    d.polygon(wall, fill=(46, 36, 26))
    d.line(wall + [wall[0]], fill=WALL, width=9, joint='curve')

    # 皇城 / 宫城（北侧正中）
    hx0, hx1, hy0, hy1 = cx - 168, cx + 168, top + 56, top + 236
    wide(d, [hx0, hy0, hx1, hy1], fill=(62, 49, 34), outline=ACCENT, width=5)
    label(d, (cx, hy0 + 52), '皇　城', 36, ACCENT)
    label(d, (cx, hy0 + 132), '太极殿 · 大明宫', 24, TEXT_DIM)

    # 宫城东西两侧的狭长坊
    for i in range(5):
        y = hy0 + 6 + i * 34
        for x in (x0 + 72, x1 - 246):
            d.rectangle([x, y, x + 174, y + 24], fill=BLOCK, outline=BLOCK_EDGE, width=1)

    # 南城棋盘：明确避开朱雀大街，不靠"跳过相交列"的补救逻辑
    gy0, row_h, rh_gap = 648, 96, 16
    cols = [(96, 100), (206, 100), (316, 100), (524, 100), (634, 100), (744, 100)]
    for r in range(5):
        y = gy0 + r * (row_h + rh_gap)
        if y + row_h > bot - 40:
            break
        for c, (x, cw) in enumerate(cols):
            wide(d, [x, y, x + cw, y + row_h], fill=BLOCK, outline=BLOCK_EDGE, width=2, r=4)
            if (r * len(cols) + c) % 3 == 0:
                label(d, (x + cw // 2, y + row_h // 2), '坊', 24, TEXT_DIM)

    # 朱雀大街：贯通南北的宽轴线
    ave_w = 66
    d.rectangle([cx - ave_w // 2, hy1, cx + ave_w // 2, bot - 46], fill=AVENUE)
    d.line([(cx, hy1), (cx, bot - 46)], fill=(214, 180, 116), width=2)

    # 东西两市：画在棋盘**之后**，否则会被坊格盖住
    for name, mx in (('西市', 290), ('东市', 650)):
        wide(d, [mx - 78, gy0 - 128, mx + 78, gy0 - 26], fill=(78, 61, 42),
             outline=WALL_DIM, width=4)
        label(d, (mx, gy0 - 77), name, 38, ACCENT)

    # 城门：标签一律放在城内一侧，绝不压在门洞上
    for gx, gy, name, dy in ((cx, top, '玄武门', 56), (cx, bot - 24, '明德门', -62),
                             (x1 - 8, top + 372, '春明门', 52), (x0 + 6, top + 420, '光宅门', 52)):
        d.rectangle([gx - 30, gy - 11, gx + 30, gy + 11], fill=WALL)
        label(d, (gx, gy + dy), name, 26, ACCENT)

    # 渭河：收在地图带内，不吞掉底部说明区
    d.rectangle([0, bot + 18, W, bot + 104], fill=WATER)
    for x in range(50, W, 92):
        d.arc([x - 68, bot + 12, x + 68, bot + 110], 200, 340, fill=(122, 144, 156), width=3)
    label(d, (W // 2, bot + 61), '渭　河', 32, (154, 176, 190))

    # 说明条：全部落在深色底上
    wide(d, [66, 1400, W - 66, 1548], fill=(44, 34, 25), outline=WALL_DIM, width=2)
    label(d, (W // 2, 1448), '宫城居北 · 朱雀大街纵贯南北', 29, TEXT)
    label(d, (W // 2, 1506), '东市西市分列轴线两侧', 29, TEXT)
    label(d, (W // 2, 1612), '出坊须经坊门 · 天黑击鼓闭门', 27, TEXT_DIM)

    return save_plate(img, path)


def tongguan(path: Path) -> Path:
    """Huang Chao's march: 曹州 → 汴州 → 洛阳 → 潼关 → 长安 (880 CE).

    Nodes are listed in **travel order**, so the polyline and its arrowheads run
    the way the army actually moved. Listing them north-first put the arrows
    pointing at Chang'an as if it were the origin -- a map that inverts the whole
    point of the story.
    """
    img = Image.new('RGB', (W, H), INK_BOT)
    d = ImageDraw.Draw(img)
    backdrop(d)

    label(d, (W // 2, 92), '黄巢进军路线', 62, TEXT)
    label(d, (W // 2, 162), '曹州 → 汴州 → 洛阳 → 潼关 → 长安', 30, TEXT_DIM)

    # (name, x, y, radius, capital, label_dx, label_dy)
    nodes = [
        ('曹　州', 640, 1452, 24, False,  96,   0),
        ('汴　州', 372, 1230, 24, False, -104,   0),
        ('洛　阳', 566,  968, 24, False,  100,   0),
        ('潼　关', 296,  646, 28, False, -100,   0),
        ('长　安', 472,  318, 30, True,     0,  96),
    ]
    pts = [(x, y) for _n, x, y, _r, _c, _dx, _dy in nodes]

    # 黄河（潼关在其北岸）
    d.line([(0, 512), (W, 486)], fill=WATER, width=26)
    label(d, (120, 478), '黄　河', 30, (150, 172, 186), anchor='lm')

    d.line(pts, fill=(206, 120, 60), width=13, joint='curve')
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        ang = math.atan2(by - ay, bx - ax)
        mx, my = ax + (bx - ax) * 0.52, ay + (by - ay) * 0.52
        d.polygon([(mx + 26 * math.cos(ang), my + 26 * math.sin(ang)),
                   (mx + 20 * math.cos(ang + 2.5), my + 20 * math.sin(ang + 2.5)),
                   (mx + 20 * math.cos(ang - 2.5), my + 20 * math.sin(ang - 2.5))],
                  fill=(230, 142, 72))

    for name, x, y, r, capital, dx, dy in nodes:
        if capital:
            d.ellipse([x - r - 16, y - r - 16, x + r + 16, y + r + 16],
                      outline=(228, 140, 70), width=5)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(58, 40, 24),
                  outline=ACCENT if capital else WALL_DIM, width=4)
        # 标签推到路线一侧，避免连线压字
        label(d, (x + dx, y + dy), name, 38 if capital else 34,
              ACCENT if capital else TEXT)
        if capital:
            label(d, (x, y - r - 52), '陷落 880 年', 28, (228, 140, 70))

    wide(d, [66, 742, W - 66, 866], fill=(44, 34, 25), outline=WALL_DIM, width=2)
    label(d, (W // 2, 788), '潼关：关城夹于道与河之间', 28, TEXT)
    label(d, (W // 2, 832), '一夫当关 · 破了它就再无屏障', 28, TEXT)

    wide(d, [66, 1568, W - 66, 1648], fill=(52, 30, 22), outline=(228, 140, 70), width=2)
    label(d, (W // 2, 1608), '攻破潼关 → 长安不设防', 30, (242, 202, 162))

    return save_plate(img, path)


MAPS = {'changan': changan, 'tongguan': tongguan}

if __name__ == '__main__':
    which = sys.argv[1] if len(sys.argv) > 1 else 'all'
    names = list(MAPS) if which == 'all' else [which]
    for n in names:
        if n not in MAPS:
            print(f'未知沙盘 {n}，可选: {", ".join(MAPS)}')
            raise SystemExit(2)
        print('已生成', MAPS[n](OUT / f'{n}.png'))