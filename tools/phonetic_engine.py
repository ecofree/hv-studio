#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hv-studio 全局发音标准化与剧本多音字扫描审计引擎 (Phonetic Engine)
用于解决所有历史解说长视频的跨剧集发音跑偏、多音字读错与不一致问题。

核心能力：
1. 全局词典统一加载（global_phonetic_lexicon.json）
2. 文本无损绝对单音字置换（normalize_phonetics）
3. 剧本录音前高危多音字静态扫描门禁（audit_script）
"""

import os
import sys
import json
import re
from pathlib import Path

LEXICON_PATH = Path(__file__).resolve().parents[1] / "resource/pronunciation/global_phonetic_lexicon.json"
SLTX_LEXICON_PATH = Path(__file__).resolve().parents[1] / "resource/pronunciation/sltx_historical_phonetic_lexicon.json"


def recording_acts(md_path):
    """Build the recorder's spoken acts from the studio's source-mapped handoff."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.services.script_handoff import derive
    payload = derive(Path(md_path).read_bytes().decode("utf-8-sig"))
    units = {u["unit_id"]: u["approved_spoken_text"] for u in json.loads(payload["document.json"])["units"]}
    acts = []
    for act in json.loads(payload["authoring.json"])["acts"]:
        title = re.search(r"【(.*?)】", act["heading"])
        text = "\n\n".join(units[key] for key in act["unit_ids"])
        acts.append((title[1] if title else act["heading"], normalize_phonetics(text)))
    return acts

# 高危多音字预警清单（历史人文解说中最易翻车的字）
HIGH_RISK_CHARS = {
    "乐": ["yuè (音乐/乐曲/乐府/礼乐/奏乐)", "lè (快乐/享乐/安居乐业/乐趣)"],
    "长": ["zhǎng (长子/长官/长兄/族长/生长)", "cháng (长久/长治久安/长河/漫长)"],
    "行": ["xíng (品行/行军/行省/行动/颁行)", "háng (行伍/行列/行业/银行)"],
    "重": ["chóng (重整/重新/重复/重申/重演)", "zhòng (重创/重要/沉重/严重)"],
    "朝": ["cháo (朝廷/朝觐/历朝/改朝换代)", "zhāo (朝夕/朝令夕改/朝气/朝暮)"],
    "更": ["gēng (更迭/更替/更改/少不更事/三更)", "gèng (更加/更好/更甚)"],
    "将": ["jiàng (主将/大将/中郎将/偏将/将士)", "jiāng (将来/将要/即将)"],
    "相": ["xiàng (宰相/丞相/相貌/出将入相)", "xiāng (互相/相助/相信)"],
    "调": ["diào (调动/调遣/调兵/声调/租庸调)", "tiáo (调整/调解/调和/风调雨顺)"],
    "量": ["liáng (衡量/丈量/商量/量入为出)", "liàng (数量/力量/容量/胆量)"],
    "差": ["chāi (差使/出差/听差)", "chā (差距/差异/偏差)", "chà (差不多/差劲)", "cī (参差)"],
    "降": ["xiáng (投降/降将/受降)", "jiàng (降落/降职/降雨)"],
    "骑": ["jì (一人一骑/单骑/轻骑/万骑/铁骑)", "qí (骑马/坐骑)"],
    "汗": ["kè hán (可汗/大汗)", "hàn (汗水/流汗)"],
    "蕃": ["bō (吐蕃/西蕃)", "fān (生蕃/蕃薯)"],
    "谷": ["yù (吐谷浑)", "gǔ (山谷/稻谷)"],
    "创": ["chuāng (重创/金疮/创伤/疮痍)", "chuàng (创造/创始)"],
    "咽": ["yān (咽喉/要咽)", "yàn (吞咽)", "yè (哽咽/哭咽)"],
    "冠": ["guān (衣冠/冠冕/弱冠)", "guàn (夺冠/冠军)"],
    "传": ["zhuàn (史传/列传/自传/别传)", "chuán (传奇/流传/传道)"]
}

def load_lexicon():
    if not LEXICON_PATH.exists():
        raise FileNotFoundError(f"发音词典不存在: {LEXICON_PATH}")
    with open(LEXICON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # 汇总所有分类下的 mapping
    combined = {}
    for group_key, group_val in data.get("rules", {}).items():
        mapping = group_val.get("mapping", {})
        combined.update(mapping)

    # 加载《隋乱唐兴》系列专属历史多音字词典（如果存在）
    if SLTX_LEXICON_PATH.exists():
        try:
            with open(SLTX_LEXICON_PATH, "r", encoding="utf-8") as sf:
                sltx_data = json.load(sf)
            for group_key, group_val in sltx_data.get("rules", {}).items():
                combined.update(group_val.get("mapping", {}))
        except Exception as e:
            print(f"[WARN] 加载专属发音词典失败: {e}", file=sys.stderr)
    
    # 强制执行“最长词优先匹配（Longest-Match-First）”，防止长词被短词截断误伤
    sorted_items = sorted(combined.items(), key=lambda x: len(x[0]), reverse=True)
    return dict(sorted_items)

def normalize_phonetics(text):
    """
    将剧本正文中的高危多音字和生僻字，
    根据全局音典转换为普通话中 100% 唯一的绝对单音字。
    """
    lexicon = load_lexicon()
    processed = text
    for original, target in lexicon.items():
        processed = processed.replace(original, target)
    return processed

def audit_script(script_text):
    """
    剧本录音前【静态扫描审计门禁】：
    检索剧本中出现的所有高危多音字，
    检查其是否已被全局音典覆盖。
    若存在未覆盖的高危上下文，输出预警报告。
    """
    lexicon = load_lexicon()
    already_covered_keys = set(lexicon.keys())

    warnings = []
    lines = script_text.splitlines()

    for line_idx, line in enumerate(lines, 1):
        line_clean = line.strip()
        if not line_clean or line_clean.startswith("#") or line_clean.startswith("---"):
            continue

        covered = set()
        for term in already_covered_keys:
            for hit in re.finditer(re.escape(term), line_clean):
                covered.update(range(hit.start(), hit.end()))
        for pos, char in enumerate(line_clean):
            if pos not in covered and char in HIGH_RISK_CHARS:
                warnings.append({
                    "line": line_idx, "char": char, "options": " / ".join(HIGH_RISK_CHARS[char]),
                    "context": line_clean[max(0,pos-8):pos+9],
                })

    return warnings

def main():
    if len(sys.argv) < 2:
        print("用法: python phonetic_engine.py <剧本文件路径>")
        return

    script_path = Path(sys.argv[1])
    if not script_path.exists():
        print(f"错误: 找不到文件 {script_path}")
        return

    text = script_path.read_text(encoding="utf-8")
    print(f"=== 正在对剧本进行【发音门禁扫描】: {script_path.name} ===")
    
    lexicon = load_lexicon()
    print(f"当前全局共享音典已加载规则: {len(lexicon)} 条。")

    warnings = audit_script(text)
    print(f"\n扫描完成！共发现 {len(warnings)} 处未完全规则锁定的高危多音字上下文：\n")

    if warnings:
        for idx, w in enumerate(warnings[:25], 1):
            print(f"[{idx:02d}] 第 {w['line']} 行 | 高危字【{w['char']}】 ({w['options']})")
            print(f"     上下文: ...{w['context']}...")
        if len(warnings) > 25:
            print(f"\n... 还有 {len(warnings) - 25} 处已省略。")
    else:
        print("🎉 恭喜！本剧本中所有专有名词与高危多音字均已被全局音典 100% 覆盖锁死！")

if __name__ == "__main__":
    main()
