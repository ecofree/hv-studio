"""Versioned script import. Source and every excluded range remain inspectable.

No LLM rewrite, pronunciation replacement, timing guess or shot budget belongs here.
"""
from __future__ import annotations

import re
from app.services.provenance import fingerprint

_META = re.compile(r"^(?:作品类型|预估时长|时长预估|核心切口|格式规范|标题|作者|版本|目标时长)\s*[：:]")
# 元数据行的通用形状：`标签：值`。比 _META 的硬编码白名单宽得多 —— 白名单依赖
# 作者恰好用了那几种措辞，本集就因为「新稿写的是『核心命题』而没命中」踩过坑
# （见下方 quote 分支的长注释）。`标签：值` 是这类头部块的稳定结构。
_META_LABEL = re.compile(r"^[^：:]{1,12}\s*[：:]")
_CUE = re.compile(r"【[^】]*】|[\[［](?:停顿|重音|语气|叹息|音效|画面|转场)[^\]］]*[\]］]")
# 括号式表演/音效提示：（微混响）（叹气）（回响）（气音）等。这些不是口播内容——
# split_script 侧 strip_script_markdown 已把它们排除在字幕之外，若这里仍计入
# source_text，scene_bindings 的「帧文本 == 旁白源文本」绑定就永远不成立
# （caomin-01 实测：字幕正确排除了（微混响），门禁却判定两者不一致）。
# 与 _CUE 一样按 delivery cue 排除，并登记为 direction，交由成片呈现判定。
# 括号形态与白名单统一从 script 侧取，避免两处各写一份、各飘各的
# （2026-09-29 caomin-01：strip_script_markdown 已删（微混响），这里仍计入
#  source_text，字幕字面门禁因此永远报「帧文本 != 剧本正文」）。
from app.services.script import PAREN_CUE_WORDS as _PAREN_WORDS  # noqa: E402

_PAREN_CUE = re.compile(r"[（(][^（()）]{1,12}[)）]")
_PAUSE = re.compile(r"停顿\s*(\d+(?:\.\d+)?)\s*秒")


def parse_document(source: str, source_name: str = "script.md") -> dict:
    blocks, units, cues = [], [], []
    offset = 0
    paragraph = []
    occurrences = {}
    narration_column = None

    def flush():
        if not paragraph:
            return
        text = "".join(b["text"] for b in paragraph)
        if text.strip():
            occurrence = occurrences.get(text, 0)
            occurrences[text] = occurrence + 1
            uid = "narr-" + fingerprint([text, occurrence])[:16]
            mapping = [i for b in paragraph for i in b["source_map"]]
            units.append({"unit_id": uid, "source_text": text,
                          "approved_spoken_text": text, "display_text": text,
                          "source_map": mapping, "block_ids": [b["block_id"] for b in paragraph],
                          "delivery": {"instruction": "", "intent": "neutral"},
                          "cue_ids": [c["cue_id"] for c in cues if c["block_id"] in {b["block_id"] for b in paragraph}]})
        paragraph.clear()

    for line in source.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        start, end = offset, offset + len(line)
        offset = end
        stripped = raw.strip()
        block = {"block_id": f"b-{len(blocks):05d}", "start": start, "end": end,
                 "raw_text": line, "kind": "narration", "text": "", "source_map": [], "excluded": []}
        blocks.append(block)
        if not stripped:
            block["kind"] = "metadata"
            block["excluded"].append({"start":start,"end":end,"reason":"paragraph_separator"})
            flush()
            continue
        local_start, local_end = 0, len(raw)
        if stripped.startswith("|"):
            matches = list(re.finditer(r"[^|]+", raw))
            cells = [m.group().strip() for m in matches]
            if any(c in ("旁白", "解说", "口播", "旁白/台词") for c in cells):
                narration_column = next(i for i,c in enumerate(cells) if c in ("旁白","解说","口播","旁白/台词"))
                block["kind"] = "metadata"
            elif all(re.fullmatch(r"[:\-\s]+",c) for c in cells):
                block["kind"] = "metadata"
            elif narration_column is not None and narration_column < len(matches):
                flush()
                m = matches[narration_column]
                local_start, local_end = m.start(), m.end()
            else:
                block["kind"] = "metadata"
                block["review_required"] = True
        clean_label = re.sub(r"^[>\s（(]+|[）)]+$", "", stripped.replace("**", "").replace("__", ""))
        if stripped.startswith("#"):
            block["kind"] = "heading"
        elif _META.match(clean_label) or re.fullmatch(r"[-*_]{3,}",stripped):
            block["kind"] = "metadata"
        elif stripped.startswith(">"):
            block["kind"] = "quote"
            # 引用块按约定是「给人看的」头部元数据，本函数与
            # script.strip_script_markdown（MD_QUOTE 整行删除）口径一致，都排除。
            # 但若作者把旁白写进了引用块，它会**静默**丢掉：不进 TTS、不烧字幕、
            # 也不报错。_META 白名单识别元数据本就脆弱，所以这里不猜 —— 凡是
            # 不具备「标签：值」形状的引用块一律标记 review_required，交给人判断
            # 它到底是元数据还是被漏排的旁白。宁可多问一次，不要默默吃掉一段文案。
            if not (_META.match(clean_label) or _META_LABEL.match(clean_label)):
                block["review_required"] = True
        # ⚠️ quote 必须与 heading/metadata 同等排除（2026-09-24 实测事故）：
        #    markdown 引用块（> …）是「给人看的」头部元数据 —— 作品类型 / 预估时长 /
        #    核心命题 / 格式规范，既不该送 TTS、也不该烧字幕。此前 quote 分支只
        #    flush() 不排除，内容会被当正文 unit，与 script.split_script 的
        #    strip_script_markdown（它把 `^>` 整行删除）**口径不一致** →
        #    production_timeline.scene_bindings 的 frames↔units 比对必然失败：
        #      "scene frame source differs from current narration"
        #    之所以以前没暴露：_META 是硬编码标签白名单，旧稿头部恰好写作
        #    「核心切口 / 格式规范」命中白名单而绕过了 quote 分支；新稿写成
        #    「本集核心命题 / 格式特性」不在白名单，直接踩中。
        #    依赖白名单识别元数据本就脆弱 —— 引用块标记 `>` 才是可靠依据。
        if block["kind"] in ("heading", "metadata", "quote"):
            flush()
            block["excluded"].append({"start":start,"end":end,"reason":block["kind"]})
            continue
        keep = set(range(local_start, local_end))
        reasons = {}
        def exclude(a,b,reason):
            for i in range(a,b):
                keep.discard(i)
                reasons[i]=reason
        for pattern,is_paren in ((_CUE,False),(_PAREN_CUE,True)):
            for m in pattern.finditer(raw,local_start,local_end):
                pause = _PAUSE.search(m.group())
                if is_paren and not any(w in m.group() for w in _PAREN_WORDS):
                    continue                      # 普通括号（补语/括注）仍算旁白
                known = pause is not None or any(w in m.group() for w in ("语气", "重音", "叹息", "画面", "音效", "转场")) or is_paren
                cue = {"cue_id":f"cue-{len(cues):05d}", "block_id":block["block_id"],
                       "raw_text":m.group(),"start":start+m.start(),"end":start+m.end(),
                       "scope":"next_utterance", "intent": "pause" if pause else "direction",
                       "pause_seconds":float(pause[1]) if pause else None,
                       "confidence":1.0 if pause else 0.0, "review_required":not bool(pause),
                       "recognized":known,"user_override":None}
                cues.append(cue)
                exclude(m.start(),m.end(),"delivery_cue")
        for m in re.finditer(r"\*\*|__|<br\s*/?>",raw):
            exclude(m.start(),m.end(),"markdown")
        m = re.match(r"\s*>\s?",raw)
        if m: exclude(0,m.end(),"blockquote_format")
        # Trim line margins, preserving intra-word/inter-word whitespace.
        for i in list(keep):
            if i < len(raw)-len(raw.lstrip()) or i >= len(raw.rstrip()): exclude(i,i+1,"margin")
        while keep and raw[min(keep)].isspace(): exclude(min(keep),min(keep)+1,"margin")
        while keep and raw[max(keep)].isspace(): exclude(max(keep),max(keep)+1,"margin")
        chars = sorted(keep)
        block["text"] = "".join(raw[i] for i in chars)
        block["source_map"] = [start+i for i in chars]
        for i in range(len(line)):
            if i not in keep:
                reason = reasons.get(i,"non_narration_cell" if i<len(raw) else "line_break")
                exc=block["excluded"]
                if exc and exc[-1]["end"]==start+i and exc[-1]["reason"]==reason:
                    exc[-1]["end"]+=1
                else: exc.append({"start":start+i,"end":start+i+1,"reason":reason})
        if paragraph and paragraph[-1]["text"] and block["text"]:
            previous = paragraph[-1]
            if previous["text"][-1].isascii() and block["text"][0].isascii():
                previous["text"] += " "
                previous["source_map"].append(previous["end"]-1)
        if not block["text"] and any(c["block_id"]==block["block_id"] for c in cues):
            block["kind"]="direction"
        paragraph.append(block)
        if block["kind"] == "quote" or narration_column is not None: flush()
    flush()
    for cue in cues:
        for unit in units:
            positions=[i for i,pos in enumerate(unit["source_map"]) if pos>=cue["end"]]
            if positions:
                cue["unit_id"]=unit["unit_id"]
                cue["text_offset"]=positions[0]
                if cue["cue_id"] not in unit["cue_ids"]:unit["cue_ids"].append(cue["cue_id"])
                break
        else:
            cue["scope"]="end_of_document"
    return {"schema_version":"2.0", "source_name":source_name,"source_sha256":fingerprint(source),
            "source":source,"blocks":blocks,"delivery_cues":cues,"units":units,
            "editorial_review": {"status":"pending", "fact_check":"not_performed", "rewrites":[]}}


def spoken_text(source: str) -> str:
    return "\n\n".join(u["approved_spoken_text"] for u in parse_document(source)["units"])
