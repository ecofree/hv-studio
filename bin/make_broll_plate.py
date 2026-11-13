#!/usr/bin/env python
"""Compose an external still into a 940x1672 plate.

Source material is mostly landscape or square; the plates are portrait. Handing a
1486x1346 manuscript straight to the renderer would have it centre-cropped down
to a fragment, so the artifact is placed on the same warm ground the sandtables
use and captioned. One visual language across hand-drawn and borrowed material.

License is recorded alongside every import. A file with no license short name is
**rejected** -- an asset whose provenance cannot be named is not usable in a
published video, however good it looks.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))

from draw_sandtable import (ACCENT, H, INK_BOT, INK_TOP, TEXT, TEXT_DIM,  # noqa: E402
                            W, backdrop, font, label, wide)
import draw_sandtable as DS  # noqa: E402

API = 'https://commons.wikimedia.org/w/api.php'
OUT = ROOT / 'assets' / 'scenes'
LEDGER = ROOT / 'assets' / 'scenes' / 'SOURCES.json'
UA = 'hv-broll/1.0 (local short-video production)'


def api(params: dict) -> dict:
    url = API + '?' + urllib.parse.urlencode(dict(params, format='json'))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def fetch(title: str) -> dict:
    info = api({'action': 'query', 'titles': title, 'prop': 'imageinfo',
                'iiprop': 'url|size|extmetadata'})
    page = next(iter(info['query']['pages'].values()))
    if 'imageinfo' not in page:
        raise ValueError(f'Commons 上取不到 {title}')
    ii = page['imageinfo'][0]
    meta = ii.get('extmetadata', {}) or {}
    return {
        'title': title, 'url': ii['url'], 'width': ii['width'], 'height': ii['height'],
        'license': (meta.get('LicenseShortName', {}) or {}).get('value', '').strip(),
        'artist': (meta.get('Artist', {}) or {}).get('value', '').strip()[:200],
        'credit': (meta.get('Credit', {}) or {}).get('value', '').strip()[:200],
        'page': 'https://commons.wikimedia.org/wiki/' + urllib.parse.quote(title.replace(' ', '_')),
    }


def download(rec: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stem = rec['title'][5:].rsplit('.', 1)[0]
    dest = OUT / f'{stem}.jpg'
    if not dest.is_file():
        req = urllib.request.Request(rec['url'], headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            dest.write_bytes(r.read())
    rec['local'] = str(dest.relative_to(ROOT)).replace('\\', '/')
    rec['bytes'] = dest.stat().st_size
    return dest


def plate(src: Path, dest: Path, caption: str, credit: str) -> Path:
    """Place an artifact on the warm ground, caption it, letterbox to 9:16."""
    img = Image.new('RGB', (W, H), INK_BOT)
    d = ImageDraw.Draw(img)
    backdrop(d)

    label(d, (W // 2, 92), '出土文书', 58, TEXT)
    label(d, (W // 2, 158), caption, 30, TEXT_DIM)

    # fit into the display band, preserving aspect
    bx0, by0, bx1, by1 = 74, 236, W - 74, 1290
    bw, bh = bx1 - bx0, by1 - by0
    with Image.open(src) as im:
        art = im.convert('RGB')
        scale = min(bw / art.width, bh / art.height)
        nw, nh = max(1, int(art.width * scale)), max(1, int(art.height * scale))
        art = art.resize((nw, nh), Image.LANCZOS)
    px, py = bx0 + (bw - nw) // 2, by0 + (bh - nh) // 2
    d.rectangle([px - 8, py - 8, px + nw + 8, py + nh + 8],
                outline=(112, 92, 62), width=3)
    img.paste(art, (px, py))

    wide(d, [66, 1392, W - 66, 1540], fill=(44, 34, 25), outline=DS.WALL_DIM, width=2)
    label(d, (W // 2, 1444), caption, 29, TEXT)
    label(d, (W // 2, 1502), credit, 22, TEXT_DIM)
    label(d, (W // 2, 1610), '图像来自公共领域素材 · 已记录来源', 24, (150, 136, 112))

    dest.parent.mkdir(parents=True, exist_ok=True)
    DS.save_plate(img, dest)
    return dest


def record(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rows = json.loads(LEDGER.read_text(encoding='utf-8')) if LEDGER.is_file() else []
    rows = [r for r in rows if r.get('title') != rec['title']]
    rows.append(rec)
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')


def import_one(title: str, caption: str, credit: str, dest_name: str) -> Path:
    rec = fetch(title)
    if not rec['license']:
        raise ValueError(f'{title} 没有可用的许可信息，拒绝导入')
    src = download(rec)
    rec['caption'] = caption
    rec['credit'] = credit
    dest = OUT / dest_name
    plate(src, dest, caption, credit)
    rec['plate'] = str(dest.relative_to(ROOT)).replace('\\', '/')
    record(rec)
    print(f'  许可={rec["license"]}  {rec["width"]}x{rec["height"]}  -> {dest.name}')
    return dest


if __name__ == '__main__':
    import_one('File:British Library Dunhuang Go Manual.jpg',
               '敦煌出土写本 · 大英图书馆藏',
               'British Library / Wikimedia Commons',
               'dunhuang_british_library.jpg')