#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""切分器改动的安全性闸门：HEAD 版 vs 工作区版**逐帧**比对。

改 `app/services/script.py` 的切分规则后必跑。它把 git HEAD 的 script.py
取到临时模块，与当前工作区版本对同一批剧本各跑一遍**完整管线**
（strip_script_markdown → split_script_text → split_long_sentence →
[merge_short_pieces]），列出帧数、最长帧、碎片数与首个差异处的上下文。

判定标准（勿放宽）：
  · 已交付集（laogongtuan / hanwu-* / hw-*）必须 **0 变化**；
  · 新集只接受「超顶帧被修好、碎片变少」这类改善，不接受帧数漂移。

用法：
    python bin/cmp_split.py                 # 默认模式
    python bin/cmp_split.py --merge-short   # 合并模式
    python bin/cmp_split.py --episodes laogongtuan lsm-15min
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_head_module() -> object:
    """把 HEAD 版 app/services/script.py 装载成独立模块。"""
    src = subprocess.run(
        ["git", "show", "HEAD:app/services/script.py"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="_oldscript_"))
    pkg = tmp / "_headscript"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "script.py").write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_headscript.script",
                                                  pkg / "script.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_headscript.script"] = mod
    spec.loader.exec_module(mod)
    return mod


def pipeline(mod, md: str, merge: bool) -> list[str]:
    """复刻 split_script 的两种模式（不构造 Episode，只要文案序列）。"""
    text = mod.strip_script_markdown(md)
    if not merge:
        out: list[str] = []
        for sent in mod.split_script_text(text):
            out.extend(mod.split_long_sentence(sent, max_chars=18))
        return out
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        pieces: list[str] = []
        for sent in mod.split_script_text(para):
            pieces.extend(mod.split_long_sentence(sent, max_chars=18))
        out.extend(mod.merge_short_pieces(pieces))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge-short", action="store_true")
    ap.add_argument("--episodes", nargs="*", default=None,
                    help="只看这几集（默认扫全部 episodes/*/script.md）")
    args = ap.parse_args()

    old = _load_head_module()
    from app.services import script as new  # noqa: E402

    scripts = sorted(ROOT.glob("episodes/*/script.md"))
    if args.episodes:
        want = set(args.episodes)
        scripts = [s for s in scripts if s.parent.name in want]

    print(f"mode: {'merge-short' if args.merge_short else 'default'}")
    hdr = (f"{'episode':<18} {'old':>5} {'new':>5} {'diff':>5}   "
           f"{'oldmax':>6} {'newmax':>6} {'old<8':>5} {'new<8':>5}")
    print(hdr)
    print("-" * len(hdr))
    changed: list[str] = []
    for sp in scripts:
        ep = sp.parent.name
        md = sp.read_text(encoding="utf-8")
        o = pipeline(old, md, args.merge_short)
        n = pipeline(new, md, args.merge_short)
        om = max((len(x) for x in o), default=0)
        nm = max((len(x) for x in n), default=0)
        os_ = sum(1 for x in o if len(x) < 8)
        ns_ = sum(1 for x in n if len(x) < 8)
        if o != n:
            changed.append(ep)
        print(f"{ep:<18} {len(o):>5} {len(n):>5} {len(n)-len(o):>+5}   "
              f"{om:>6} {nm:>6} {os_:>5} {ns_:>5}"
              f"{'   ← 有变化' if o != n else ''}")
        if o != n:
            for i in range(max(len(o), len(n))):
                a = o[i] if i < len(o) else "<无>"
                b = n[i] if i < len(n) else "<无>"
                if a != b:
                    for j in range(max(0, i - 1),
                                   min(max(len(o), len(n)), i + 3)):
                        x = o[j] if j < len(o) else "—"
                        y = n[j] if j < len(n) else "—"
                        print(f"    {'  ' if x == y else '>>'} [{j:>4}] old={x!r}")
                        if x != y:
                            print(f"       [{j:>4}] new={y!r}")
                    break
    print("-" * len(hdr))
    print(f"有变化: {changed or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
