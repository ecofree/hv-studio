import re
import sys
import json
from pathlib import Path

# 1. AI 烂俗套话、血腥脑补与结构穿帮词库
BANNED_PHRASES = [
    "七窍流血", "两腿筛糠", "大白话翻译过来", "翻译成大白话",
    "然而历史在这个节骨眼", "浮起了一丝冷笑", "气得胡子乱颤",
    "鲜血直接溅进", "大篇章在此完成结算", "大篇章完成结算",
    "饮战马鲜血", "咽泥沙", "抱头痛哭", "冷笑", "胡子乱颤",
    "梁胡景", "奚纯", "八个大字", "杨代王杨侑", "五代十国里那种"
]

# 2. 禁止在口播正文直接念出的冷僻年号与建元词
BANNED_ERA_PATTERNS = [
    r"(建元|改元)\s*[\u4e00-\u9fa5]{2}",
    r"(大业|秦兴|安乐|天兴|永徽|贞观|开元|天宝)[一二三四五六七八九十百千万元年]+"
]

def lint_script(md_path: str, facts_json_path: str = None):
    p = Path(md_path)
    if not p.exists():
        print(f"Error: file not found: {md_path}", file=sys.stderr)
        sys.exit(1)
    text = p.read_text(encoding="utf-8")
    errors = []

    # 规则1：拦截退化的 Markdown 列表导演标注（防 TTS 误读画面提示词）
    if re.search(r"^\s*[\*\-]\s*\*\*视听构图\*\*", text, re.M):
        errors.append("[格式严重错误] 发现 '* **视听构图**' 列表格式，必须全部改为 '【视听构图：...】'，否则 TTS 会将其当正文朗读！")

    if re.search(r"^#{3,4}\s+###", text, re.M):
        errors.append("[格式错误] 发现重复井号标题（如 '#### ###'），会导致分幕 parser 异常。")

    # 过滤出纯朗读段落
    paragraphs = [
        p.strip() for p in text.split("\n\n")
        if p.strip() and not p.strip().startswith(("#", ">", "【", "---"))
    ]

    # 规则2：拦截相邻段落高度重复（抓出复制粘贴段落）
    for i in range(len(paragraphs) - 1):
        p1, p2 = set(paragraphs[i]), set(paragraphs[i + 1])
        if len(paragraphs[i]) > 30 and len(paragraphs[i + 1]) > 30:
            overlap = len(p1 & p2) / max(len(p1 | p2), 1)
            if overlap > 0.72:
                errors.append(f"[段落重复错误] 相邻段落内容高度雷同（重合度 {overlap:.0%}）：'{paragraphs[i+1][:25]}...'")

    # 加载 Pass 1 事实锁
    facts_data = {}
    if facts_json_path and Path(facts_json_path).exists():
        facts_data = json.loads(Path(facts_json_path).read_text(encoding="utf-8"))
    whitelist = facts_data.get("verified_quotes", [])

    # 规则3A：拦截未在白名单中的长双引号
    for quote in re.findall(r"“([^”]+)”", text):
        if len(quote) > 4 and not any(quote in w for w in whitelist):
            errors.append(f"[伪造台词违规] 未在原典白名单中的长引号： “{quote}” -> 请改为第三人称间接转述！")

    # 规则3B：根治“冒号 + 第一人称”绕过双引号的假台词漏洞
    fake_dialogue_pattern = re.compile(
        r"(说|道|喊|大吼|进言|劝阻|炫耀|放话|吐出一句话|回绝道|下令|交代)[：:]\s*[^。\n]*(我|咱们|咱|寡人|朕|孤|本王|末将|儿臣|大王|陛下|世民)"
    )
    for line in paragraphs:
        m = fake_dialogue_pattern.search(line)
        if m:
            errors.append(f"[变相假台词违规] 检测到用冒号偷写第一人称对话：'{m.group(0)}' -> 严禁使用冒号直接引语，必须改为第三人称转述！")

    # 规则4：拦截口播中出现冷僻年号
    for line in paragraphs:
        for pat in BANNED_ERA_PATTERNS:
            m = re.search(pat, line)
            if m:
                errors.append(f"[纪年违规] 口播正文出现冷僻年号 '{m.group(0)}' -> 请删去或改为公元纪年，年号仅留给画面字幕！")

    # 规则5：拦截未核实的具体“某月某日”脑补（防四月十三日正午类编造）
    for line in paragraphs:
        m = re.search(r"[一二三四五六七八九十]+月[初十廿卅一二三四五六七八九]+日(正午|清晨|深夜|黄昏)?", line)
        if m and not any(m.group(0) in w for w in whitelist):
            errors.append(f"[日期脑补嫌疑] 出现具体日期 '{m.group(0)}' -> 若原典无具体日数，请改为模糊时间（如'四月中旬'）！")

    # 规则6：拦截 AI 禁词与血腥脑补
    for bad in BANNED_PHRASES:
        if bad in text:
            errors.append(f"[禁词/脑补违规] 命中禁用词： '{bad}'")

    # 规则7：人物结局逻辑自洽校验（防“既喝毒酒又被生擒”）
    fate_ledger = facts_data.get("entity_fate_ledger", {})
    for person, fate in fate_ledger.items():
        if "非饮鸩" in fate or "普通酒" in fate:
            if re.search(rf"{person}[^。\n]{{0,15}}饮鸩", text):
                errors.append(f"[人物结局矛盾] 事实锁规定 '{person}' 为 '{fate}'，但正文中出现了 '{person}...饮鸩'！")
        if "被俘" in fate and "未死" in fate:
            if re.search(rf"{person}[^。\n]{{0,10}}(战死|射杀|阵亡|身亡)", text):
                errors.append(f"[人物结局矛盾] 事实锁规定 '{person}' 为 '{fate}'，但正文中写成了阵亡！")

    # 规则8：检查事实密度配额（防强行拉长注水）
    clean_spoken = "\n".join(paragraphs)
    char_count = len(re.sub(r"\s+", "", clean_spoken))
    fact_atoms = facts_data.get("fact_atoms", [])
    if fact_atoms:
        ratio = char_count / len(fact_atoms)
        if ratio > 115:
            errors.append(
                f"[注水密度违规] 朗读正文共 {char_count} 字，但仅支撑了 {len(fact_atoms)} 条事实原子"
                f"（平均 {ratio:.1f} 字/条，超过 115 字红线）。请回 Pass 1B 补充新史料，或精简重复说教！"
            )

    if errors:
        print(f"❌ 剧本静态门禁未通过（共 {len(errors)} 处违规），已阻断进入 TTS 流程：")
        for idx, e in enumerate(errors, 1):
            print(f"  {idx}. {e}")
        sys.exit(1)
    else:
        atoms_str = f"，共支撑 {len(fact_atoms)} 条事实原子，平均 {char_count/len(fact_atoms):.1f} 字/条" if fact_atoms else ""
        print(f"✅ 剧本静态门禁通过（正文 {char_count} 字{atoms_str}），允许进入派生 TTS 文本生成。")

def main():
    args = sys.argv[1:]
    src = None
    facts = None
    if "--src" in args:
        idx = args.index("--src")
        src = args[idx + 1]
    if "--facts" in args:
        idx = args.index("--facts")
        facts = args[idx + 1]

    if not src and args:
        src = args[0]
        if len(args) > 1 and not args[1].startswith("--"):
            facts = args[1]

    if not src:
        print("Usage: python tools/script_linter.py <script_path> [facts_json_path]")
        sys.exit(1)

    lint_script(src, facts)

if __name__ == "__main__":
    main()
