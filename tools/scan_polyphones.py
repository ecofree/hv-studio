#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import re
from pathlib import Path

TOOLS_DIR = Path(r"D:\hv-studio\tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from phonetic_engine import normalize_phonetics

script_path = Path(r"D:\hv-studio\workspace\episodes\sltx-17-user-shouchan-liji\editorial\narration.md")
raw = script_path.read_text(encoding="utf-8")
norm = normalize_phonetics(raw)

targets = {
    "长": ["长子", "长官", "兄长", "长兄", "成长", "增长", "长治久安", "长夜", "长安"],
    "行": ["行伍", "行业", "行列", "颁行", "推行", "执行", "举行", "言行"],
    "重": ["重申", "重整", "重围", "重重", "重大", "重要", "重臣"],
    "朝": ["朝廷", "朝夕", "朝令夕改", "改朝换代", "隋朝", "唐朝"],
    "将": ["将军", "大将", "名将", "诸将", "将士", "主将", "骁将"],
    "乐": ["礼乐", "乐府", "安乐", "享乐", "奏乐"]
}

print("=== 剧本多音字扫描诊断 ===")
for char, common_words in targets.items():
    raw_cnt = len(re.findall(re.escape(char), raw))
    norm_cnt = len(re.findall(re.escape(char), norm))
    print(f"字【{char}】: 原始出现 {raw_cnt} 次 -> 词典单音置换后剩余 {norm_cnt} 次 (置换了 {raw_cnt - norm_cnt} 次)")
    
    # 查找尚未置换的剩余词
    remaining_words = []
    for m in re.finditer(rf".{{0,2}}{char}.{{0,2}}", norm):
        context = m.group(0).strip()
        if context not in remaining_words and len(context) >= 2:
            remaining_words.append(context)
    if remaining_words:
        print(f"   未置换上下文抽检: {remaining_words[:8]}")
