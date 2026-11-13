"""Source preservation and review boundaries for the rebuildable style corpus."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.production
SPEC = importlib.util.spec_from_file_location(
    "writing_dna_curator", Path(__file__).resolve().parents[1] / "bin/curate_writing_dna.py"
)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def source(root, name="one.md", title="史料观察", body="正文里说，大臣求关注不代表读者推广。"):
    raw = root / "raw" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes((f"# {title}\r\n\r\n- **作者**: 观察者\r\n- **发布日期**: 2018\r\n"
                     "- **原文链接**: https://example.test/source\r\n\r\n---\r\n\r\n"
                     + body + "\r\n").encode("utf-8"))
    meta = root / "_meta" / (raw.stem + ".json")
    meta.parent.mkdir(exist_ok=True)
    meta.write_text('{"source_types":["已核验的正史"],"topic_tags":["统一标签"]}', encoding="utf-8")
    return raw, meta


def test_raw_bytes_ranges_and_unverified_metadata_are_preserved(tmp_path):
    body = "正文里说，大臣求关注。\r\n\r\n内部段落不改。"
    raw, meta = source(tmp_path, body=body + "\r\n\r\n搞笑八卦历史人物故事\r\n"
                               "var first_sceen__time = 1;\r\ndocument.addEventListener('copy');\r\n预览时标签不可点")
    before = raw.read_bytes(), meta.read_bytes()
    manifest, artifacts = C.build_artifacts(tmp_path)
    entry = manifest["entries"][0]
    assert artifacts[entry["output_path"]].decode("utf-8") == body
    text = raw.read_bytes().decode("utf-8")
    ranges = sorted(entry["kept_ranges"] + entry["removed_ranges"], key=lambda r: r["start"])
    assert ranges[0]["start"] == 0 and ranges[-1]["end"] == len(text)
    assert all(a["end"] == b["start"] for a, b in zip(ranges, ranges[1:]))
    assert all(C.sha(text[r["start"]:r["end"]].encode("utf-8")) == r["sha256"] for r in ranges)
    assert sum(r["characters"] for r in ranges) == len(text)
    assert entry["historical_truth_status"] == "not_fact_checked"
    assert not entry["legacy_metadata"]["semantic_tags_adopted"]
    assert entry["topic_tags"] == [] and entry["retrieved_at"] is None
    assert (raw.read_bytes(), meta.read_bytes()) == before
    assert entry["raw_stats"]["characters"] > entry["raw_stats"]["characters_lf_normalized"]


def test_account_promotions_have_no_export_and_complete_removal_record(tmp_path):
    source(tmp_path, name=sorted(C.PROMOTIONS)[0], title="推广", body="长按二维码关注")
    manifest, outputs = C.build_artifacts(tmp_path)
    e = manifest["entries"][0]
    assert e["status"] == "excluded_promotion" and e["output_path"] is None
    assert e["sample_weight"] == 0 and not e["kept_ranges"]
    assert not any(p.startswith("texts/") for p in outputs)
    assert sum(r["characters"] for r in e["removed_ranges"]) == e["raw_stats"]["characters"]


def test_duplicate_versions_have_one_weight_and_reconstructable_exclusion(tmp_path):
    source(tmp_path, "a.md", "故事（二）", "这是一段故事。\r\n旧人离开，新人到来。")
    source(tmp_path, "b.md", "故事(二)", "这是一段故事。旧人离开，新人到来。\r\n预览时标签不可点")
    manifest, outputs = C.build_artifacts(tmp_path)
    a, b = manifest["entries"]
    assert a["sample_weight"] == 1 and b["sample_weight"] == 0
    assert b["duplicate_of"] == "raw/a.md" and not b["kept_ranges"]
    assert any(r["reason"] == "duplicate_body_not_exported" for r in b["removed_ranges"])
    assert len([p for p in outputs if p.startswith("texts/")]) == 1
    assert manifest["duplicate_groups"][0]["body_similarity"] == 1


def test_same_title_different_text_is_not_silently_deduplicated(tmp_path):
    source(tmp_path, "a.md", "同题", "甲乙丙丁戊")
    source(tmp_path, "b.md", "同题", "山川江河湖海")
    manifest, _ = C.build_artifacts(tmp_path)
    assert manifest["summary"]["included_unique_texts"] == 2
    assert manifest["near_duplicate_review"][0]["decision"] == "review_required_separate_weights"


def test_reader_cta_removal_does_not_delete_historical_use_of_attention(tmp_path):
    body = "大臣求关注，皇帝点赞，这些词也可能在故事里。\r\n这个故事结束啦，给个赞好不好咯？"
    source(tmp_path, body=body)
    manifest, outputs = C.build_artifacts(tmp_path)
    result = outputs[manifest["entries"][0]["output_path"]].decode("utf-8")
    assert result == "大臣求关注，皇帝点赞，这些词也可能在故事里。\r\n这个故事结束啦"


def test_rebuild_check_is_deterministic_and_detects_tampering(tmp_path):
    source(tmp_path)
    out = tmp_path / "curated"
    C.run(tmp_path, out)
    original = (out / "manifest.json").read_bytes()
    C.run(tmp_path, out)
    assert original == (out / "manifest.json").read_bytes()
    C.run(tmp_path, out, check=True)
    (out / "texts/one.md").write_text("被改过", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        C.run(tmp_path, out, check=True)
    assert (out / "texts/one.md").read_text(encoding="utf-8") == "被改过"


@pytest.mark.parametrize("folder", ["raw", "raw/sub", "_meta", "scripts", "."])
def test_protected_output_locations_are_rejected_before_writes(tmp_path, folder):
    raw, _ = source(tmp_path)
    original = raw.read_bytes()
    with pytest.raises(ValueError, match="overlaps"):
        C.run(tmp_path, tmp_path / folder)
    assert raw.read_bytes() == original


def test_unknown_web_script_fails_without_export(tmp_path):
    source(tmp_path, body="正文。\r\nwindow.getSelection();\r\n更多文字")
    with pytest.raises(ValueError, match="residue"):
        C.run(tmp_path, tmp_path / "curated")
    assert not (tmp_path / "curated").exists()


def test_missing_source_information_fails_closed(tmp_path):
    raw, _ = source(tmp_path)
    raw.write_text("# 只有正文，缺失来源和边界", encoding="utf-8")
    with pytest.raises(ValueError, match="delimiter"):
        C.build_artifacts(tmp_path)


def test_unmanaged_output_is_preserved_for_review(tmp_path):
    source(tmp_path)
    out = tmp_path / "curated"
    (out / "texts").mkdir(parents=True)
    extra = out / "texts/editorial.md"
    extra.write_text("人工笔记", encoding="utf-8")
    with pytest.raises(ValueError, match="require review"):
        C.run(tmp_path, out)
    assert extra.read_text(encoding="utf-8") == "人工笔记"
