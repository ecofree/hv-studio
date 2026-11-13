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
# 代码围栏：Gemini TTS 专版把口播正文包在 ```text 围栏里，反引号不是口播内容。
# 与 script.strip_script_markdown 的 MD_FENCE 同口径。xlc-01 靠手工删围栏绕过，
# xlc-02 实测围栏标记直接进了 unit 文本（ASR 不可能听出反引号）。
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_CUE = re.compile(r"【[^】]*】|[\[［][^\]］\n]*[\]］]")
# 括号式表演/音效提示：（微混响）（叹气）（回响）（气音）等。这些不是口播内容——
# split_script 侧 strip_script_markdown 已把它们排除在字幕之外，若这里仍计入
# source_text，scene_bindings 的「帧文本 == 旁白源文本」绑定就永远不成立
# （caomin-01 实测：字幕正确排除了（微混响），门禁却判定两者不一致）。
# 与 _CUE 一样按 delivery cue 排除，并登记为 direction，交由成片呈现判定。
# 括号形态与白名单统一从 script 侧取，避免两处各写一份、各飘各的
# （2026-09-29 caomin-01：strip_script_markdown 已删（微混响），这里仍计入
#  source_text，字幕字面门禁因此永远报「帧文本 != 剧本正文」）。
from app.services.script import PAREN_CUE_WORDS as _PAREN_WORDS  # noqa: E402

_PAREN_CUE = re.compile(r"[（(][^（()）\n]+[)）]")
_PAUSE = re.compile(r"(?:停顿\s*(\d+(?:\.\d+)?)\s*秒|pause\s*(?:=|:|\s)\s*(\d+(?:\.\d+)?)\s*(?:seconds?|s))", re.I)
_DIRECTION_PREFIX = re.compile(r"^(?:语气|重音|叹息|叹气|轻笑|气音|微混响|音效|画面|转场|配乐|导演|停顿|pause\b)", re.I)
_AMBIGUOUS_PREFIX = re.compile(r"^(?:语音|声音|声线|音量)")
_EDITORIAL_LINE = re.compile(r"^\s*[-*+]\s+(?:\*\*)?(本幕利害切口|视听构图)(?:\*\*)?\s*[：:]")
_END_CARD = re.compile(r"^【片尾字幕】\s*[：:]?\s*$")


def _cue_spans(source):
    """Lex over the whole source so a multiline note is never read aloud."""
    spans = list(_CUE.finditer(source))
    for match in _PAREN_CUE.finditer(source):
        inner = match.group()[1:-1].strip()
        if (_DIRECTION_PREFIX.match(inner) or _AMBIGUOUS_PREFIX.match(inner) or
                (len(inner) <= 12 and any(w in inner for w in _PAREN_WORDS))):
            if not any(m.start() <= match.start() < m.end() for m in spans):
                spans.append(match)
    return sorted(spans, key=lambda m: m.start())
# 长段按句拆分的上限。v2 是"一句一镜"：段落即 unit 时，长段落独占几十秒
# 画面，图再多也换不进去（xlc-03 实测 36 unit 平均 24 秒，最长 86 秒）。
# 按句拆后 unit 粒度≈口播一句，plate 才能跟着解说走。只影响新 plan，
# 已落盘 plan 不会重跑（plan_id  pinned）。
MAX_UNIT_CHARS = 110
_MIN_TAIL_CHARS = 25
_SENT_END = re.compile(r"[。！？…]+")
# 分句边界：；：，（不含顿号，枚举项拆开无意义）。仅超长单句兜底用。
_CLAUSE_END = re.compile(r"[；：，]")



def parse_document(source: str, source_name: str = "script.md", *, authoring_layout=True) -> dict:
    blocks, units, cues = [], [], []
    offset = 0
    paragraph = []
    occurrences = {}
    narration_column = None
    cue_spans = _cue_spans(source)
    registered = set()
    end_card = False

    def _split_ranges(text):
        """Split a long paragraph into ranges [(a, b)].

        Two-level boundaries: sentence ends (。、！？…) first; clause ends
        (；：，) only when no viable sentence cut fits the window. A chunk
        must reach _MIN_TAIL_CHARS to be cut (no stub fragments); a single
        oversized sentence without clauses stays whole rather than breaking
        mid-sentence. A tiny tail merges into the previous chunk.
        A paragraph without sentence punctuation or under the cap stays
        whole (old behavior).
        """
        strong = [m.end() for m in _SENT_END.finditer(text)]
        weak = [m.end() for m in _CLAUSE_END.finditer(text)]
        if not strong or len(text) <= MAX_UNIT_CHARS:
            return [(0, len(text))]
        bounds = sorted(set(strong + weak))
        strong_set = set(strong)
        ranges, start = [], 0
        n = len(text)
        while start < n:
            cands = [e for e in bounds
                     if start < e <= start + MAX_UNIT_CHARS
                     and e - start >= _MIN_TAIL_CHARS
                     and e in strong_set]
            if not cands:
                cands = [e for e in bounds
                         if start < e <= start + MAX_UNIT_CHARS
                         and e - start >= _MIN_TAIL_CHARS]
            if not cands:
                beyond = [e for e in bounds if e > start + MAX_UNIT_CHARS]
                end = beyond[0] if beyond else n
            else:
                end = max(cands)
            if end < n and n - end <= _MIN_TAIL_CHARS and end - start <= MAX_UNIT_CHARS:
                # Tiny tail merges -- but only into a chunk that is not
                # already oversized, or one pathological sentence would glue
                # the whole paragraph back together.
                rem = [e for e in bounds if e > end]
                end = rem[0] if rem else n
            ranges.append((start, end))
            start = end
        return [(a, b) for a, b in ranges if b > a]

    def flush():
        if not paragraph:
            return
        text = "".join(b["text"] for b in paragraph)
        # Block char spans in paragraph-text coordinates, for cue/block mapping.
        spans, cursor = [], 0
        for b in paragraph:
            spans.append((cursor, cursor + len(b["text"])))
            cursor += len(b["text"])
        mapping_all = [i for b in paragraph for i in b["source_map"]]
        if text.strip():
            for a, b in _split_ranges(text):
                sub = text[a:b]
                if not sub.strip():
                    continue
                occurrence = occurrences.get(sub, 0)
                occurrences[sub] = occurrence + 1
                uid = "narr-" + fingerprint([sub, occurrence])[:16]
                overlap = {paragraph[i]["block_id"] for i, (s, e) in enumerate(spans)
                           if s < b and e > a}
                overlap_cues = [c["cue_id"] for c in cues if c["block_id"] in overlap]
                units.append({"unit_id": uid, "source_text": sub,
                              "approved_spoken_text": sub, "display_text": sub,
                              "source_map": mapping_all[a:b],
                              "block_ids": sorted(overlap),
                              "delivery": {"instruction": "", "intent": "neutral"},
                              "cue_ids": overlap_cues})
        paragraph.clear()

    for line in source.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        start, end = offset, offset + len(line)
        offset = end
        stripped = raw.strip()
        block = {"block_id": f"b-{len(blocks):05d}", "start": start, "end": end,
                 "raw_text": line, "kind": "narration", "text": "", "source_map": [], "excluded": []}
        blocks.append(block)
        # Explicit authoring fields are retained as inspectable blocks. Unknown
        # list annotations stop handoff instead of becoming accidental narration.
        if authoring_layout:
            if stripped.startswith('#'):
                end_card = False
            if _END_CARD.fullmatch(stripped):
                end_card = True
            editorial = _EDITORIAL_LINE.match(raw)
            if end_card or editorial or re.match(r'^\s*[-*+]\s+\S', raw):
                flush()
                block['kind'] = 'end_card' if end_card else 'editorial_direction'
                block['label'] = '片尾字幕' if end_card else editorial.group(1) if editorial else 'unknown'
                if not end_card and not editorial:
                    block['review_required'] = True
                block['excluded'].append({'start': start, 'end': end, 'reason': block['kind']})
                continue
        spanning_cue = any(m.start() < start and m.end() > start for m in cue_spans)
        if not stripped and not spanning_cue:
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
        if spanning_cue:
            pass  # A continuation of a direction is not a heading or metadata.
        elif stripped.startswith("#"):
            block["kind"] = "heading"
        elif _FENCE.match(stripped):
            block["kind"] = "metadata"
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
        for m in cue_spans:
            if m.end() <= start + local_start or m.start() >= start + local_end:
                continue
            inner = m.group()[1:-1].strip()
            pause = _PAUSE.fullmatch(inner)
            seconds = float(next(g for g in pause.groups() if g is not None)) if pause else None
            valid_pause = seconds is not None and 0 < seconds <= 30
            is_paren = m.group()[0] in "（("
            known = bool(_DIRECTION_PREFIX.match(inner)) or (is_paren and not _AMBIGUOUS_PREFIX.match(inner))
            if inner.lower().startswith(("停顿", "pause")):
                known = valid_pause
            if m.start() not in registered:
                registered.add(m.start())
                channel = "pause" if pause else next((value for prefix, value in (
                    ("画面", "visual"), ("音效", "sfx"), ("配乐", "music"),
                    ("转场", "transition"), ("导演", "director")) if inner.startswith(prefix)),
                    "performance" if known else "unknown")
                cue = {"cue_id":f"cue-{len(cues):05d}", "block_id":block["block_id"],
                       "raw_text":m.group(),"start":m.start(),"end":m.end(),
                       "channel":channel,
                       "scope":"next_utterance", "intent": "pause" if pause else "direction",
                       "pause_seconds":seconds,
                       "confidence":1.0 if valid_pause else 0.0, "review_required":not valid_pause,
                       "recognized":known,"user_override":None}
                cues.append(cue)
            exclude(max(local_start, m.start()-start), min(local_end, m.end()-start), "delivery_cue")
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
    # One cue has one target; block overlap must not attach it to every split unit.
    for unit in units:
        unit["cue_ids"] = []
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
