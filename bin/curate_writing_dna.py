"""Rebuild a traceable, style-only corpus without modifying source material.

Offsets are zero-based Unicode code points into UTF-8-decoded raw text,
including original CR/LF characters. End offsets are exclusive. Every raw
character belongs to a kept range or a removed range. No historical claim is
fact-checked by this mechanical cleaning step.
"""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata


SCHEMA = "writing-dna-curation/v1"
PROMOTIONS = {
    "2019-02-08_每日必读的几个优秀微信公众号.md",
    "2019-02-13_2018年火爆朋友圈的公众号,有几个你关注的.md",
}
# Reviewed literal boundaries; not keyword deletion across historical prose.
FOOTER_STARTS = (
    "搞笑八卦历史人物故事", "预览时标签不可点", "修改于",
    "var first_sceen__time", "苦逼的原创不易", "原创不易",
    "看完，赏个赞", "看完赏个赞", "看完打赏一个赞", "未完待续，先点个赞",
    "谢谢大家的支持，你们的鼓励是我前进的动力", "点点“在看”", "点“在看”",
    "点个“在看”", "心情不好，求点“在看”", "写字的是我，排版的是我",
    "转发和点“在看”", "祝大家新春快乐，平平安安，发大财以后给我打打赏",
    "最近太忙了，没有时间写，更新慢", "最近实在太忙了，更新慢",
    "天天白嫖、连个“好看”都不舍得点", "呐呐，对会讲故事的文艺青年好一点吧，给他点点赞",
)
INLINE_CTA_SUFFIX = "，给个赞好不好咯？"
FORBIDDEN_WEB = re.compile(r"document\.(?:getElementById|addEventListener)|window\.getSelection|预览时标签不可点|var first_sceen__time")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stats(text: str) -> dict:
    return {
        "characters": len(text),
        "characters_lf_normalized": len(text.replace("\r\n", "\n").replace("\r", "\n")),
        "han_characters": sum("\u4e00" <= c <= "\u9fff" for c in text),
        "non_whitespace_characters": sum(not c.isspace() for c in text),
    }


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def ranges_from_labels(text: str, labels: list[str | None]) -> tuple[list, list]:
    kept, removed = [], []
    start = 0
    while start < len(text):
        label = labels[start]
        end = start + 1
        while end < len(text) and labels[end] == label:
            end += 1
        item = {
            "start": start, "end": end,
            "start_line": text.count("\n", 0, start) + 1,
            "end_line": text.count("\n", 0, end - 1) + 1,
            "sha256": sha(text[start:end].encode("utf-8")),
            "characters": end - start,
        }
        if label is None:
            kept.append(item)
        else:
            item["reason"] = label
            removed.append(item)
        start = end
    return kept, removed


def clean_one(path: Path, root: Path) -> tuple[dict, str, str]:
    data = path.read_bytes()
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    divider = next((i for i, line in enumerate(lines[:12]) if line.strip() == "---"), None)
    if divider is None:
        raise ValueError(f"Missing metadata/body delimiter: {path.name}")
    header_end = sum(map(len, lines[:divider + 1]))
    header = text[:header_end]
    title = next((line.lstrip("\ufeff")[2:].strip() for line in lines[:divider] if line.lstrip("\ufeff").startswith("# ")), path.stem)
    url = re.search(r"原文链接\*\*\s*:\s*(\S+)", header)
    date = re.search(r"发布日期\*\*\s*:\s*(.+)", header)
    author = re.search(r"作者\*\*\s*:\s*(.+)", header)
    if not url:
        raise ValueError(f"Missing source URL: {path.name}")
    labels: list[str | None] = [None] * len(text)
    labels[:header_end] = ["source_header_moved_to_manifest"] * header_end
    if path.name in PROMOTIONS:
        labels[header_end:] = ["excluded_account_promotion"] * (len(text) - header_end)
    else:
        offset = header_end
        for line in lines[divider + 1:]:
            value = line.strip()
            if any(value.startswith(prefix) for prefix in FOOTER_STARTS):
                # These reviewed markers introduce terminal site/author material.
                # Record script separately even if it follows the channel slogan.
                for j in range(offset, len(text)):
                    labels[j] = "page_or_author_footer"
                break
            if value.endswith(INLINE_CTA_SUFFIX):
                pos = text.find(INLINE_CTA_SUFFIX, offset, offset + len(line))
                labels[pos:pos + len(INLINE_CTA_SUFFIX)] = ["inline_reader_call_to_action"] * len(INLINE_CTA_SUFFIX)
            offset += len(line)
        script = re.search(r"(?m)^[ \t]*var first_sceen__time", text[header_end:])
        if script:
            start = header_end + script.start()
            end = text.find("预览时标签不可点", start)
            if end == -1:
                end = len(text)
            labels[start:end] = ["website_javascript"] * (end - start)
    # Remove only exterior blank space; keep internal layout verbatim.
    remaining = [i for i, label in enumerate(labels) if label is None]
    while remaining and text[remaining[0]].isspace():
        labels[remaining.pop(0)] = "exterior_whitespace"
    while remaining and text[remaining[-1]].isspace():
        labels[remaining.pop()] = "exterior_whitespace"
    kept, removed = ranges_from_labels(text, labels)
    body = "".join(text[x["start"]:x["end"]] for x in kept)
    if FORBIDDEN_WEB.search(body):
        raise ValueError(f"Unclassified website residue: {path.name}")
    meta = root / "_meta" / (path.stem + ".json")
    entry = {
        "raw_path": path.relative_to(root).as_posix(), "raw_sha256": sha(data),
        "raw_bytes": len(data), "raw_stats": stats(text), "title": title,
        "source_url": url.group(1), "published_date_as_recorded": date.group(1).strip() if date else None,
        "author_as_recorded": author.group(1).strip() if author else None,
        "retrieved_at": None, "retrieved_at_status": "not_recorded_in_raw_header",
        "historical_truth_status": "not_fact_checked", "rights_status": "not_reviewed",
        "allowed_use": "local_style_study_only_not_historical_evidence_or_publication_copy",
        "legacy_metadata": {"path": meta.relative_to(root).as_posix(), "sha256": sha(meta.read_bytes()) if meta.exists() else None, "semantic_tags_adopted": False},
        "status": "excluded_promotion" if path.name in PROMOTIONS else "included_style_only",
        "kept_ranges": kept, "removed_ranges": removed,
        "clean_body_sha256": sha(body.encode("utf-8")), "clean_body_stats": stats(body),
        "output_path": None, "duplicate_of": None, "sample_weight": 0 if path.name in PROMOTIONS else 1,
        "topic_tags": [], "structure_pattern": None,
        "editorial_annotation_status": "not_individually_reviewed",
    }
    # This flag is evidenced by the literal string, not by inherited labels.
    fiction = next((s for s in ("《倚天屠龙记》", "《笑傲江湖》", "《野叟曝言》", "转自知乎") if s in body), None)
    entry["content_flags"] = [{"kind": "mentions_literature_or_repost", "evidence": fiction, "raw_offset": text.find(fiction)}] if fiction else []
    if 0 < len(body) < 300:
        entry["content_flags"].append({"kind": "short_fragment_not_complete_long_form_example", "evidence": {"body_characters": len(body)}})
    return entry, body, text


def build_artifacts(root: Path) -> tuple[dict, dict[str, bytes]]:
    rows = [clean_one(p, root) for p in sorted((root / "raw").glob("*.md"))]
    if not rows:
        raise ValueError("No raw Markdown files")
    # Conservative pairs: identical content OR same normalized title and >=.95
    # body similarity. Other similar pairs are surfaced for review, not merged.
    groups, candidates = [], []
    norms = [normalized(body) for _, body, _ in rows]
    shingles = [{s[k:k + 5] for k in range(max(0, len(s) - 4))} for s in norms]
    canonical = list(range(len(rows)))
    for i, (a, _, _) in enumerate(rows):
        if a["status"] == "excluded_promotion":
            continue
        for j in range(i):
            b = rows[j][0]
            if b["status"] == "excluded_promotion":
                continue
            same_title = normalized(a["title"]) == normalized(b["title"])
            equal = norms[i] == norms[j]
            union = shingles[i] | shingles[j]
            overlap = len(shingles[i] & shingles[j]) / len(union) if union else 0
            if not (same_title or equal or overlap >= .5):
                continue
            score = 1.0 if equal else SequenceMatcher(None, norms[i], norms[j], autojunk=False).ratio()
            pair = {"files": [b["raw_path"], a["raw_path"]], "body_similarity": round(score, 8), "same_normalized_title": same_title}
            if equal or (same_title and score >= .95):
                canonical[i] = canonical[j]
                groups.append({**pair, "decision": "single_weight", "canonical": rows[canonical[j]][0]["raw_path"]})
                break
            if same_title or score >= .8:
                candidates.append({**pair, "decision": "review_required_separate_weights"})
    outputs: dict[str, bytes] = {}
    for i, (entry, body, text) in enumerate(rows):
        if canonical[i] != i:
            entry["status"] = "excluded_duplicate"
            entry["duplicate_of"] = rows[canonical[i]][0]["raw_path"]
            entry["sample_weight"] = 0
            # Body is preserved in raw; range records explain its exclusion.
            entry["removed_ranges"].extend({**r, "reason": "duplicate_body_not_exported"} for r in entry["kept_ranges"])
            entry["removed_ranges"].sort(key=lambda r: r["start"])
            entry["kept_ranges"] = []
        elif entry["status"] == "included_style_only":
            relative = "texts/" + Path(entry["raw_path"]).name
            entry["output_path"] = relative
            outputs[relative] = body.encode("utf-8")
    counts = Counter(e["status"] for e, _, _ in rows)
    summary = {
        "raw_files": len(rows), "included_unique_texts": counts["included_style_only"],
        "excluded_promotions": counts["excluded_promotion"], "excluded_duplicates": counts["excluded_duplicate"],
        "raw_characters": sum(e["raw_stats"]["characters"] for e, _, _ in rows),
        "raw_characters_lf_normalized": sum(e["raw_stats"]["characters_lf_normalized"] for e, _, _ in rows),
        "raw_han_characters": sum(e["raw_stats"]["han_characters"] for e, _, _ in rows),
        "exported_characters": sum(len(b.decode("utf-8")) for b in outputs.values()),
        "exported_characters_lf_normalized": sum(stats(b.decode("utf-8"))["characters_lf_normalized"] for b in outputs.values()),
        "exported_han_characters": sum(stats(b.decode("utf-8"))["han_characters"] for b in outputs.values()),
        "removed_characters_by_reason": dict(sorted(Counter({reason: sum(r["characters"] for e, _, _ in rows for r in e["removed_ranges"] if r["reason"] == reason) for reason in {r["reason"] for e, _, _ in rows for r in e["removed_ranges"]}}).items())),
        "files_with_javascript_removed": sum(any(r["reason"] == "website_javascript" for r in e["removed_ranges"]) for e, _, _ in rows),
        "files_with_footer_removed": sum(any(r["reason"] == "page_or_author_footer" for r in e["removed_ranges"]) for e, _, _ in rows),
    }
    manifest = {
        "schema": SCHEMA, "normative_version": "5.0", "historical_truth_status": "not_fact_checked",
        "generator_sha256": sha(Path(__file__).read_bytes()),
        "offset_convention": "zero_based_unicode_codepoint_end_exclusive_original_line_endings",
        "character_convention": "all_decoded_characters_including_CR_LF; Han=U+4E00..U+9FFF; no_word_count_claim",
        "deduplication": "same normalized body, or same NFKC alphanumeric title and body SequenceMatcher >= 0.95; earliest lexicographic filename retained; other pairs screened with 5-gram Jaccard >=0.5 then SequenceMatcher >=0.8, review only",
        "summary": summary, "duplicate_groups": groups, "near_duplicate_review": candidates,
        "entries": [entry for entry, _, _ in rows],
    }
    outputs["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    outputs["README.md"] = (f"""# 可重建的语料正文（v5）

本目录由 `bin/curate_writing_dna.py` 生成。原始 {summary['raw_files']} 份抓取保留在 `../raw/`；
导出 {summary['included_unique_texts']} 份不同正文，排除 {summary['excluded_promotions']} 份公众号推广、{summary['excluded_duplicates']} 份重复抓取。
导出正文 {summary['exported_characters']} 个字符、{summary['exported_han_characters']} 个汉字；字符口径含原换行，汉字口径为 U+4E00..U+9FFF。

这些是清洗后的语言观察材料，**不是已经核验的历史范例**。所有正文的历史真实性为 `not_fact_checked`，
逐篇编辑标注尚未完成，使用权也未核验；仅限现有本地语料的研究整理，不能直接复制成新稿或用于公开再发布。
事实写作要另查原典；文本清洗不会纠正原作者的推测、偏见或虚构对白。

`manifest.json` 保存 raw 字节哈希、原文链接、原始发布日期、逐段保留/移除范围与原因、重复关系和统计。
抓取时间缺失记为 null，不把链接中的时间参数猜成抓取时间。不采用 `_meta` 中未经逐篇核验的统一标签。
每个输出正文严格由其保留范围拼接，不另加标题或尾注；引用来源请带清单中的原文链接。

在项目根目录重建：

```powershell
.\\.venv-core\\Scripts\\python.exe -X utf8 bin/curate_writing_dna.py
.\\.venv-core\\Scripts\\python.exe -X utf8 bin/curate_writing_dna.py --check
```

这是可检索清洗库，不要求把所有正文塞进提示词。优先使用 `../语言DNA.md` 的三组已注明核查范围的短例，
再由编辑为某个写作问题挑选短段；不得把本目录的 `included_style_only` 当作事实或艺术批准。
""").encode("utf-8")
    return manifest, outputs


def run(root: Path, output: Path, check: bool = False) -> dict:
    root, output = root.resolve(), output.resolve()
    for protected in (root / "raw", root / "_meta", root / "scripts"):
        if output == protected or protected in output.parents or output in protected.parents:
            raise ValueError("Output overlaps protected source material")
    manifest, artifacts = build_artifacts(root)
    current_texts = {p.relative_to(output).as_posix() for p in (output / "texts").glob("*.md")} if output.exists() else set()
    extras = current_texts - set(artifacts)
    if extras:
        raise ValueError(f"Unmanaged/stale generated texts require review: {sorted(extras)}")
    mismatches = []
    for relative, data in artifacts.items():
        target = output / relative
        if check:
            if not target.exists() or target.read_bytes() != data:
                mismatches.append(relative)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if mismatches:
        raise ValueError(f"Curated output mismatch: {mismatches}")
    for e in manifest["entries"]:
        if sha((root / e["raw_path"]).read_bytes()) != e["raw_sha256"]:
            raise ValueError("Raw source changed during curation")
    return manifest["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "resource/风趣历史.writing-DNA")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.output or args.root / "curated", args.check), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
