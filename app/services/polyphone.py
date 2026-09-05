"""
多音字预处理服务。

从 app/config/polyphone.yaml 加载规则，preprocess() 做最长匹配替换。
命中会写日志到 state/polyphone_hits.log，便于发现漏网之鱼。
"""
from __future__ import annotations

import atexit
import re
import time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = ROOT / "app" / "config" / "polyphone.yaml"
HITS_LOG = ROOT / "state" / "polyphone_hits.log"

# 规则 key 最短 2 字（D3）。单字规则等于对全文做无差别替换：一条坏规则
# （比如 `氏: 支`）就能把整篇剧本改坏，且像「摩羯座 → 摩杰座」这种误伤在
# 加规则时根本预料不到。罕见字要修就读音，写成词组（氐族 / 羌人 / 羯鼓）。
MIN_KEY_LEN = 2

# 命中日志：进程内缓冲，退出时一次性落盘（原来每命中一次就开一次文件 append，
# 782 行 = 782 次 open，且无上限增长）。超过 1MB 先轮转一次。
HITS_LOG_MAX_BYTES = 1_000_000

_RULES: dict[str, str] = {}
_RULES_BY_LEN: list[tuple[str, str]] = []  # 长→短
_HITS_COUNTER: Counter = Counter()
_HITS_BUFFER: list[str] = []
_LOADED = False


def _load(force: bool = False) -> None:
    global _RULES, _RULES_BY_LEN, _LOADED
    if _LOADED and not force:
        return
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = raw.get("rules", {})
    bad = [k for k in rules if len(str(k)) < MIN_KEY_LEN]
    if bad:
        # 硬拦而不是跳过：静默不生效比报错更糟，会让人以为规则加上了。
        raise ValueError(
            f"polyphone.yaml 有单字规则 {bad}（要求 key ≥ {MIN_KEY_LEN} 字）。"
            f"单字 = 全文无差别替换，误伤不可控（如 摩羯座→摩杰座）。"
            f"请改写成词组规则，例：氐族→低族、羌人→枪人。")
    # 规则按 key 长度降序，长匹配优先
    _RULES = dict(rules)
    _RULES_BY_LEN = sorted(rules.items(), key=lambda kv: -len(str(kv[0])))
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
    """只入缓冲，不碰文件（落盘见 flush_hits）。"""
    _HITS_BUFFER.append(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{src}\t{dst}\t{original[:80]}\n")


def flush_hits() -> int:
    """把缓冲的命中一次性落盘，返回写入行数。

    只开一次文件句柄；超过 HITS_LOG_MAX_BYTES 先把旧日志轮转成 *.1。
    由 atexit 自动调用，也可在长任务中途手工调。
    """
    global _HITS_BUFFER
    if not _HITS_BUFFER:
        return 0
    lines, _HITS_BUFFER = _HITS_BUFFER, []
    try:
        HITS_LOG.parent.mkdir(parents=True, exist_ok=True)
        if HITS_LOG.exists() and HITS_LOG.stat().st_size > HITS_LOG_MAX_BYTES:
            HITS_LOG.replace(HITS_LOG.with_name(HITS_LOG.name + ".1"))
        with HITS_LOG.open("a", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        return 0        # 诊断日志写失败不该影响出片
    return len(lines)


# 进程退出时兜底落盘（不强依赖调用方记得 flush）
atexit.register(flush_hits)


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
