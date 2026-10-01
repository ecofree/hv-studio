# -*- coding: utf-8 -*-
"""Lock the parenthesised direction-cue exclusion.

Why: split_script's strip_script_markdown already drops （微混响）/（叹气） from
frame text (they must not reach subtitles), but parse_document counted them as
narration, so scene_bindings' "frame text == narration source text" binding could
never hold (caomin-01, 2026-09-29). These tests pin both halves: the cue form is
excluded from source_text, and ordinary parentheses stay narration.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"D:\hv")
from app.services.script_document import parse_document

pytestmark = pytest.mark.shared

def ids(text):
    return [u["unit_id"] for u in parse_document(text)["units"]]

def src(text):
    return "".join(u["source_text"] for u in parse_document(text)["units"])


def test_parenthesised_direction_cue_is_not_narration():
    doc = parse_document("自己还得亲自兼任保安和保洁。（微混响）")
    assert src(doc["source"] if isinstance(doc.get("source"), str) else
               "自己还得亲自兼任保安和保洁。（微混响）") == "自己还得亲自兼任保安和保洁。"
    cues = doc["delivery_cues"]
    assert any("微混响" in c["raw_text"] for c in cues), cues
    assert any("微混响" in c["raw_text"] and c["intent"] == "direction" for c in cues)


def test_ordinary_parentheses_stay_narration():
    text = "他写了一本书（《通典》），记载了当时的户口。"
    assert src(text) == "他写了一本书（《通典》），记载了当时的户口。", src(text)
    assert not [c for c in parse_document(text)["delivery_cues"] if "通典" in c["raw_text"]]


def test_pause_inside_parentheses_is_a_pause_cue():
    doc = parse_document("他停了一下（停顿1.5秒），然后继续。")
    assert src("他停了一下（停顿1.5秒），然后继续。") == "他停了一下，然后继续。"
    pau = [c for c in doc["delivery_cues"] if c["intent"] == "pause"]
    assert pau and pau[0]["pause_seconds"] == 1.5, doc["delivery_cues"]


def test_both_cue_forms_coexist():
    text = "甲（叹气），乙【语气：低沉】，丙[停顿0.5秒]。"
    doc = parse_document(text)
    kinds = {c["raw_text"] for c in doc["delivery_cues"]}
    assert "（叹气）" in kinds and "【语气：低沉】" in kinds, kinds
    assert src(text) == "甲，乙，丙。", src(text)


def test_unit_ids_stay_unique_after_excluding_cues():
    # parse_document groups by paragraph, so a single line is one unit; what must
    # hold is that ids remain unique and the cue text is gone from source_text.
    text = "第一句（微混响）。第二句（叹气）！第三句？"
    u = ids(text)
    assert len(u) == len(set(u)) >= 1, u
    assert src(text) == "第一句。第二句！第三句？", src(text)
