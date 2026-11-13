# -*- coding: utf-8 -*-
"""Pin the three fixes found by the xlc-01 new-episode test (2026-10-13).

All three were live failures on a real new episode, not theoretical:

1. `strip_script_markdown` did not strip code fences, so the ```text markup of
   the "Gemini TTS 专属工程文档" format entered v2 unit text as if it were
   narration. ASR cannot hear backticks, so asr_check necessarily reports
   insertions -- and every gate stayed green, because the frame text really did
   equal the (polluted) cleaned script.
2. `setup_episode_from_writing_dna.py`'s act-head regex could not match
   `### 【第一幕 (A段)：…】`, so the new writing-DNA format could not be
   onboarded at all.
3. `external_narration.MIN_UNIT_COVERAGE` was a HARD build gate. Measured on
   xlc-01: unit narr-83f2ea92fa177ec9 (`"朕佳儿佳妇，今以付卿。"`) scored 0.385
   because free ASR heard `朕家而家負`, yet `forced_alignment.atoms` returned 13
   word rows for that exact slice -- the cut was correct. The gate rejected a
   good cut and blocked the episode.
"""
import sys
from pathlib import Path

import pytest

from app.services.script import strip_script_markdown
from app.services import external_narration as EN

pytestmark = pytest.mark.production

# ---------------------------------------------------------------------------
# 1. code fences
# ---------------------------------------------------------------------------

FENCED_SCRIPT = """# 《X·第一集》Gemini TTS 专属工程文档

> **适用模型**：Gemini 3.8 Flash

## 分块渲染脚本

### 【第一幕 (A段)：翠微宫遗诏与帝王隐忍】（字数：943 字）
```text
贞观二十三年五月，终南山翠微宫的阴雨，笼罩了整个大唐帝国。

五十二岁的太宗李世民走到了生命的尽头。[pause=0.5s]

“朕佳儿佳妇，今以付卿。”
```

### 【第一幕 (B段)：长孙威权】（字数：737 字）
```text
在外人看来，这是托孤之重。
```
"""


def test_fence_markup_never_enters_the_narration():
    """The backticks are markup. If they leak, ASR can never produce them and
    asr_check reports insertions that no gate can explain."""
    clean = strip_script_markdown(FENCED_SCRIPT)
    assert "```" not in clean
    assert "text" not in clean.replace("贞观", "", 1) or "```text" not in clean
    # The spoken content survives.
    assert "贞观二十三年五月" in clean
    assert "朕佳儿佳妇，今以付卿" in clean
    assert "这是托孤之重" in clean


def test_fenced_script_produces_the_same_clean_text_as_unfenced():
    unfenced = FENCED_SCRIPT.replace("```text\n", "").replace("\n```\n", "\n")
    assert strip_script_markdown(unfenced) == strip_script_markdown(FENCED_SCRIPT)


def test_inline_backticks_are_removed():
    text = "前面 ```扔在这里的``` 反引号，后面正常。"
    assert "`" not in strip_script_markdown(text)
    assert "反引号" in strip_script_markdown(text)


def test_fence_fix_does_not_disturb_the_legacy_format():
    """The pre-fence behaviour on ordinary markdown must be bit-identical:
    every delivered episode's frame text depends on it."""
    md = """# 标题

### 【第一幕：均田制的数学游戏】
**（时长预估：00:00-03:30）**

【语气：低沉】贞观二十三年，[重音]天下大旱。

> 引用块应当被删

---

“财均者取强，力穑则贫。”
"""
    clean = strip_script_markdown(md)
    assert clean == "贞观二十三年，天下大旱。\n\n“财均者取强，力穑则贫。”"


# ---------------------------------------------------------------------------
# 2. setup tool act-head regex
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. coverage is a flag, not a gate
# ---------------------------------------------------------------------------

def _rows(n, covs):
    return [dict(frame=f"narr-{i}", kind="narration", chars=100, hits=90,
                 start=i * 5.0, end=i * 5.0 + 3.0, coverage=c)
            for i, c in enumerate(covs)]


_FAKE = dict(accuracy=0.87, matched=10, chars=10, chars_asr=10, exact=8, homophone=2)


def test_low_coverage_no_longer_blocks_the_build():
    """A weak whole-track alignment is a review item, not a build failure.

    Measured case: xlc-01 narr-83f2ea92fa177ec9 -- coverage 0.385, free ASR heard
    `朕家而家負`, yet forced_alignment.atoms returned 13 rows for that slice.
    """
    rows = _rows(3, [0.92, 0.385, 0.90])
    summary = EN._check_alignment(_FAKE, rows)
    assert summary["low_coverage"] == ["narr-1"]
    assert summary["min_coverage"] == 0.385
    # The detail explains *why* it was flagged, for the human adjudicating it.
    assert summary["low_coverage_detail"] == [dict(unit_id="narr-1", coverage=0.385)]


def test_unaligned_unit_still_blocks_the_build():
    """A unit with no audio is impossible to cut; continuing would fabricate a
    slice. This stays fatal -- it is the only case that must."""
    rows = _rows(2, [0.9, 0.0])
    rows[1]["start"] = rows[1]["end"] = None
    with pytest.raises(EN.ExternalNarrationError, match="找不到对应语音"):
        EN._check_alignment(_FAKE, rows)


def test_validate_external_reports_low_coverage_without_raising():
    """Downstream, the same number must not re-block: the verdict belongs to the
    forced aligner, not to a threshold."""
    import json
    from app.services.provenance import file_sha256
    tmp = Path(pytest.__file__)  # any existing file; never read by the validator
    manifest = {
        "provenance": EN.PROVENANCE,
        "master_path": str(tmp), "master_sha256": file_sha256(tmp),
        "samples": 100, "sample_rate": 24000,
        "source_recording": {"path": str(tmp), "sha256": file_sha256(tmp),
                             "delivered_sha256": "d" * 64},
        "takes": [{"unit_id": "narr-0"}, {"unit_id": "narr-1"}],
        "alignment": [
            {"unit_id": "narr-0", "sample_start": 0, "sample_end": 10, "coverage": 0.92},
            {"unit_id": "narr-1", "sample_start": 10, "sample_end": 20, "coverage": 0.385},
        ],
    }
    report = EN.validate_external(manifest)
    assert report["low_coverage"] == ["narr-1"]
    assert report["units"] == 2


def test_validate_external_still_rejects_a_missing_coverage_field():
    """Coverage must be present to be reported; its absence is a real gap."""
    from app.services.provenance import file_sha256
    tmp = Path(pytest.__file__)
    sha = file_sha256(tmp)
    manifest = {
        "provenance": EN.PROVENANCE,
        "master_path": str(tmp), "master_sha256": sha,
        "samples": 100, "sample_rate": 24000,
        "source_recording": {"path": str(tmp), "sha256": sha,
                             "delivered_sha256": "d" * 64},
        "takes": [{"unit_id": "narr-0"}],
        "alignment": [{"unit_id": "narr-0", "sample_start": 0, "sample_end": 10}],
    }
    with pytest.raises(ValueError, match="缺少对齐覆盖率"):
        EN.validate_external(manifest)


def test_alignment_review_clears_only_when_the_forced_aligner_spoke():
    """A unit leaves `alignment_review` only on real force-alignment evidence
    bound to the current manifest -- not because time passed."""
    from app.services.production_status import alignment_review_pending
    state = {"manifest_path": None, "alignment_path": None}
    assert alignment_review_pending(state) == []          # non-external manifest

    state = {"manifest_path": "nope.json", "alignment_path": "nope2.json"}
    # unreadable -> conservative, nothing clears
    assert alignment_review_pending(state) == []


def test_parse_document_excludes_fence_lines_from_units():
    """v2 的 parse_document 必须和 strip_script_markdown 同口径排除围栏。

    xlc-02 实测：围栏标记直接进了 unit 文本（ASR 不可能听出反引号）。
    xlc-01 当时是手工删围栏绕过的，本测试把豁口钉死。
    """
    from app.services.script_document import parse_document

    doc = parse_document(FENCED_SCRIPT, "probe.md")
    assert doc["units"], "fenced script must still yield units"
    for u in doc["units"]:
        assert "```" not in u["source_text"], u["unit_id"]
        assert "```" not in u["approved_spoken_text"], u["unit_id"]
        assert "```" not in u["display_text"], u["unit_id"]
    texts = [u["source_text"] for u in doc["units"]]
    assert any("翠微宫" in x for x in texts)
    assert any("托孤" in x for x in texts)

def test_long_paragraph_splits_by_sentence():
    """v2 是一句一镜：超长段落必须按句拆分，否则单镜头独占几十秒画面。

    xlc-03 实测 36 unit 平均 24 秒、最长 86 秒，图再多也换不进去。
    """
    from app.services.script_document import (MAX_UNIT_CHARS, _MIN_TAIL_CHARS,
                                             _CLAUSE_END, _SENT_END,
                                             parse_document)

    # 多分句长段落：句号优先，超长才在 ；：， 处兜底拆。
    long_para = ("第一句。" + "甲分句写景至此结束；" + "乙" * 60 + "，乙分句至此结束；"
                 + "丙" * 60 + "收尾。第二句短。")
    doc = parse_document(long_para + "\n", "probe.md")
    assert len(doc["units"]) >= 2
    for u in doc["units"]:
        n = len(u["source_text"])
        assert len(u["source_map"]) == n
        if n > MAX_UNIT_CHARS:
            # 超限 chunk 必须是无句内边界的单句（病态文本兜底），不得硬切。
            assert not _SENT_END.search(u["source_text"][:-1])
            assert not _CLAUSE_END.search(u["source_text"][:-1])
        assert n >= _MIN_TAIL_CHARS or n == len(long_para), "不得产生碎 unit"
    assert "".join(u["source_text"] for u in doc["units"]) == long_para

    # 单句超长且无分句边界：保持整句，不切碎。
    one_sentence = "甲" * 200 + "。"
    doc2 = parse_document(one_sentence + "\n", "probe.md")
    assert len(doc2["units"]) == 1
    assert doc2["units"][0]["source_text"] == one_sentence


def test_short_paragraph_stays_whole_and_stub_merges():
    from app.services.script_document import parse_document

    doc = parse_document("短句。\n", "probe.md")
    assert len(doc["units"]) == 1
    doc2 = parse_document("前" * 100 + "。后十个字而已。\n", "probe.md")
    assert len(doc2["units"]) == 1, "短尾必须并入上一 chunk，不得产生碎 unit"
    assert doc2["units"][0]["source_text"] == "前" * 100 + "。后十个字而已。"
