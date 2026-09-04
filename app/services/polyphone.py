"""
多音字预处理服务。

从 app/config/polyphone.yaml 加载规则，preprocess() 做最长匹配替换。
命中会写日志到 state/polyphone_hits.log，便于发现漏网之鱼。
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "app" / "config" / "polyphone.yaml"
HITS_LOG = ROOT / "state" / "polyphone_hits.log"

_RULES: dict[str, str] = {}
_RULES_BY_LEN: list[tuple[str, str]] = []  # 长→短
_HITS_COUNTER: Counter = Counter()
_LOADED = False


def _load(force: bool = False) -> None:
    global _RULES, _RULES_BY_LEN, _LOADED
    if _LOADED and not force:
        return
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = raw.get("rules", {})
    # 规则按 key 长度降序，长匹配优先
    _RULES = dict(rules)
    _RULES_BY_LEN = sorted(rules.items(), key=lambda kv: -len(kv[0]))
    _LOADED = True


def reload() -> None:
    _load(force=True)


def rules() -> dict[str, str]:
    _load()
    return dict(_RULES)


def preprocess(text: str, log_hits: bool = True) -> str:
    """
    将历史专有名词按 polyphone.yaml 替换为同音常用字。
    替换采用最长匹配优先（如"大月氏"优先于"月氏"）。
    """
    _load()
    if not text:
        return text
    out = text
    for src, dst in _RULES_BY_LEN:
        if src in out:
            out = out.replace(src, dst)
            _HITS_COUNTER[src] += 1
            if log_hits:
                _log_hit(src, dst, text)
    return out


def _log_hit(src: str, dst: str, original: str) -> None:
    HITS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with HITS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{src}\t{dst}\t{original[:80]}\n")


def hit_stats() -> dict[str, int]:
    return dict(_HITS_COUNTER)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", type=str, help="直接处理一段文本")
    ap.add_argument("--stats", action="store_true", help="显示命中统计")
    ap.add_argument("--rules", action="store_true", help="列出所有规则")
    args = ap.parse_args()

    if args.text is not None:
        print(preprocess(args.text))
    elif args.stats:
        for k, v in sorted(hit_stats().items(), key=lambda kv: -kv[1]):
            print(f"{k:8s} → {v:5d} 次")
    elif args.rules:
        for k, v in rules().items():
            print(f"{k:8s} → {v}")
    else:
        samples = [
            "张骞被匈奴扣押十年",
            "卫青、霍去病出击匈奴",
            "单于冒顿派阏氏看守",
            "汉武帝派张骞出使大月氏",
        ]
        for s in samples:
            print(f"{s}\n  → {preprocess(s)}\n")


if __name__ == "__main__":
    main()
