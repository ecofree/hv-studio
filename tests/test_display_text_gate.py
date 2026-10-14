# -*- coding: utf-8 -*-
"""Pin P1-3: display_text is tamper-checked and is karaoke's index source.

Why: validate_timeline compares cue text against manifest alignment text
(display_text), but the plan-vs-document gate only checked the other three
text fields -- a hand-edited display_text flowed into subtitles with every
gate green. And karaoke indexed source_text, so the two diverged the moment
display differed from source.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.production

from app.services import narration_pipeline as N
from test_production_v2 import assembly


def test_edited_display_text_fails_the_plan_gate(tmp_path):
    _, plan, asset, _, _, _ = assembly(tmp_path)
    units = json.loads(Path(plan).read_text(encoding="utf-8"))["units"]
    assert all("display_text" in u for u in units)
    tampered = json.loads(Path(plan).read_text(encoding="utf-8"))
    tampered["units"][0]["display_text"] = "有人改了字幕。"
    Path(plan).write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from source document"):
        N.generation_quote(plan, asset)


def test_untouched_plan_passes_with_display_checked(tmp_path):
    # assembly() already synthesized, so nothing is pending -- but the gate
    # (now including display_text) must pass without raising.
    _, plan, asset, _, _, _ = assembly(tmp_path)
    quote = N.generation_quote(plan, asset)
    assert quote["requests"] == 0


def test_karaoke_indexes_display_text(tmp_path):
    """When display differs from source, highlight follows display (the
    string the subtitle gate compares), not source."""
    from app.services import karaoke as K

    plan = {"units": [{"unit_id": "u1", "source_text": "ABCDEFG",
                       "display_text": "ABXY"}]}
    alignment = {"words": {"u1": [
        {"text_start": 0, "text_end": 1, "sample_start": 0, "sample_end": 100},
        {"text_start": 1, "text_end": 2, "sample_start": 100, "sample_end": 200},
    ]}}
    timeline = {"subtitles": [{"unit_id": "u1", "text": "ABXY",
                               "text_start": 0, "text_end": 4,
                               "sample_start": 0, "sample_end": 200}]}
    index = K.build(timeline, plan, alignment)
    assert index.cues, "display-sourced index must produce cues"
    # And the old fallback still works for plans predating display_text.
    plan_old = {"units": [{"unit_id": "u1", "source_text": "ABXY"}]}
    index_old = K.build(timeline, plan_old, alignment)
    assert index_old.cues
