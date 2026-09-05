#!/usr/bin/env python
"""打印所有 episode / scene_plan 的基线指纹，用于回归比对。

用法：
    python bin/fp_episodes.py

指纹算法：json.dumps(obj, ensure_ascii=False, sort_keys=True) 的 md5 前 12 位。
（字段顺序、缩进、换行都不影响，只比内容。）

配合回归检查：
    python bin/fp_episodes.py > /tmp/fp_before.txt
    # ... 改动 + 跑测试 ...
    python bin/fp_episodes.py > /tmp/fp_after.txt
    diff /tmp/fp_before.txt /tmp/fp_after.txt

另打印 notes 长度（应恒为 0：剧本正文不许塞进 ep.notes）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def fp(obj) -> str:
    return hashlib.md5(
        json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def main() -> int:
    eps = sorted(BASE.glob("episodes/*.json"))
    for ep in eps:
        try:
            d = json.loads(ep.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"episode  {ep.stem:16s} ERROR {e}")
            continue
        shots = d.get("frames") or d.get("shots") or []
        print(
            f"episode  {ep.stem:16s} fp={fp(d):12s} "
            f"shots={len(shots):4d} notes_len={len(d.get('notes') or '')}"
        )

    for sp in sorted(BASE.glob("episodes/*/scene_plan.json")):
        d = json.loads(sp.read_text(encoding="utf-8"))
        shots = d.get("shots", d) if isinstance(d, dict) else d
        print(f"scene    {sp.parent.name:16s} fp={fp(d):12s} shots={len(shots)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
