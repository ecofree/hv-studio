r"""风趣历史 writing-DNA 剧本 → hv 剧集旁白源（一次成型，零成本）。

为什么要有这个脚本
------------------
《大唐草民录》四集连着做的时候，同一套清洗规则手抄了三遍，且每一遍都漏了
不同的东西（ep2 漏 `“财均者取强…”` 整行、ep3 差点漏五处整行加粗台词、
ep2/ep3 各漏一处 `[轻笑]`）。这些都不是审美问题，是**字面门禁与字幕完整性**
问题：`strip_script_markdown` 会把整行加粗当元数据删掉，字幕就永远少一句，
而所有门禁都照样绿灯（帧文本确实等于「清洗后的剧本」）。

本脚本把三处口径固定下来
----------------------
1. 旁白源 = **acts-only**：从第一个 `### 【第…幕` 开始，前面的作品类型/抖音
   发布文案整块丢掉。原因：文案与口播正文高度重复，v2 单元与帧文本都含它时，
   对齐 DP 会把整条音轨匹配给文案，末尾几十帧对不上语音（caomin-01 实测 238 帧
   里 116 帧是 0 采样）。
2. 方括号标注**只留白名单**：`[重音]` 与 `[停顿N秒]` 两边处理一致，必须留；
   其余（如 `[轻笑]`）在音轨里根本不存在、且 script_document 会把它当正文，
   留着会让 v2 单元文本与帧文本差几个字，scene_bindings 直接判不一致。
3. **整行加粗的台词去 `**`，不是删行**：`(时长预估…)` 这类元数据整行删掉是对的；
   `“闭门鼓后…笞二十！”`、诗句、`“就食东都。”` 这类是口播内容，删行等于删字幕。
   判据：整行加粗且内容以 `（` 开头 → 元数据；否则 → 台词，保内容去星号。

用法
----
    .venv-core\Scripts\python.exe bin\setup_episode_from_writing_dna.py \
        --ep caomin-04 \
        --src "resource/风趣历史.writing-DNA/scripts/大唐草民录_第四集_黄巢的私盐与帝国的葬礼.md"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.script import strip_script_markdown   # noqa: E402

BRACKET = re.compile(r'\[([^\]]{1,12})\]')
BOLD_LINE = re.compile(r'^\*\*(.*)\*\*$')
# 幕标头。写作 DNA 的成稿格式演进过两轮：
#   《大唐草民录》  ### 【第一幕：古代最狠的公摊面积】
#   《大唐修罗场》  ### 【第一幕 (A段)：翠微宫遗诏与帝王隐忍】（字数：943 字）
# 后者在「幕」与「：」之间插了括号分块标签，旧正则 `【第.幕[：:]` 直接失配，
# 工具以「找不到幕标头」退出 —— 新格式的剧本一条都建不了档（2026-10-13 实测）。
# 这里容许幕名与冒号之间存在一段非 】 非换行的标签。
ACT_HEAD = re.compile(r'^###\s*【第.幕[^】\n]{0,14}[：:]', re.M)
# script_document 认识的方括号标注：这两个必须留在旁白源里
KEEP_BRACKET = re.compile(r'^(重音|停顿[\d.]*秒)$')


def build_narration(text: str) -> tuple[str, dict]:
    m = ACT_HEAD.search(text)
    if not m:
        raise SystemExit('找不到 `### 【第…幕` 幕标头；这不是 writing-DNA 成稿格式')
    body = text[m.start():]

    kinds: dict[str, int] = {}
    for b in BRACKET.finditer(body):
        kinds[b.group(1)] = kinds.get(b.group(1), 0) + 1
    drop = {k for k in kinds if not KEEP_BRACKET.match(k)}
    if drop:
        body = BRACKET.sub(lambda x: '' if x.group(1) in drop else x.group(0), body)

    kept, meta = [], []
    out_lines = []
    for line in body.splitlines():
        b = BOLD_LINE.match(line)
        if b:
            inner = b.group(1)
            if inner.startswith('（'):
                meta.append(inner[:30])
                continue
            kept.append(inner[:44])
            line = inner
        out_lines.append(line)
    body = '\n'.join(out_lines)

    # 自检：清洗后仍须留住这些台词，否则字幕会静默少句
    probes = [b.group(1) for line in kept for b in BRACKET.finditer('')]
    clean = strip_script_markdown(body)
    missing = [k for k in kept if len(k) >= 6 and k[:6] not in clean]
    return body.strip() + '\n', dict(brackets=kinds, dropped=sorted(drop),
                                     bold_meta=len(meta), bold_kept=kept, missing=missing)


def main() -> int:
    ap = argparse.ArgumentParser(description='writing-DNA 成稿 → hv 剧集旁白源')
    ap.add_argument('--ep', required=True)
    ap.add_argument('--src', required=True)
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    text = src.read_text(encoding='utf-8')

    dst = ROOT / 'episodes' / args.ep
    dst.mkdir(parents=True, exist_ok=True)
    (dst / 'script.md').write_text(text, encoding='utf-8')
    body, info = build_narration(text)
    (dst / 'script_narration.md').write_text(body, encoding='utf-8')

    print('原稿      %s -> %d chars' % (src.name, len(text)))
    print('front matter dropped: %d chars' % (len(text) - len(body)))
    print('方括号标注: %s' % info['brackets'])
    print('移除的标注: %s（白名单外）' % info['dropped'])
    print('整行加粗: 元数据 %d 行删除 / 台词 %d 行保内容去星号'
          % (info['bold_meta'], len(info['bold_kept'])))
    for k in info['bold_kept']:
        print('    kept: %s' % k)
    if info['missing']:
        print('[x] 这些整行加粗台词在清洗后消失了（字幕会少句，必须改源）:')
        for k in info['missing']:
            print('    - %s' % k)
        return 2
    print('[OK] %s/script.md + script_narration.md 就绪（%d bytes）'
          % (dst, (dst / 'script_narration.md').stat().st_size))
    return 0


if __name__ == '__main__':
    sys.exit(main())