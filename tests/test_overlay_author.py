"""Authoring the title/act sidecar from headings and unit timings."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.production

from app.services import overlay as O
from app.services import overlay_author as A


DOCUMENT = {
    "blocks": [
        {"block_id": "b0", "kind": "heading",
         "raw_text": "### 【第一幕：关霜与封关：一张走不通的通关牒文】\n"},
        {"block_id": "b1", "kind": "narration", "raw_text": "第一段旁白\n"},
        {"block_id": "b2", "kind": "heading",
         "raw_text": "### 【第二幕：刀尖上的带路人：荒原深处的回首试探】\n"},
        {"block_id": "b3", "kind": "narration", "raw_text": "第二段旁白\n"},
    ],
    "units": [
        {"unit_id": "u1", "block_ids": ["b1"]},
        {"unit_id": "u2", "block_ids": ["b3"]},
    ],
}

QUALITY = {
    "takes": [
        {"unit_id": "u1", "sample_start": 0, "sample_end": 24000 * 10},
        {"unit_id": "u2", "sample_start": 24000 * 10, "sample_end": 24000 * 20},
    ],
}


@pytest.fixture()
def sidecar(tmp_path, monkeypatch):
    p = tmp_path / "overlays.json"
    monkeypatch.setattr(O, "path_for", lambda ep_id: p)
    return p


def _write_inputs(tmp_path):
    document = tmp_path / "document.json"
    quality = tmp_path / "quality.json"
    document.write_text(json.dumps(DOCUMENT, ensure_ascii=False), encoding="utf-8")
    quality.write_text(json.dumps(QUALITY), encoding="utf-8")
    return document, quality


def test_acts_map_headings_to_first_narration_unit():
    acts = A.acts_from_document(DOCUMENT, QUALITY)
    assert [a.label for a in acts] == ["第一幕", "第二幕"]
    assert [a.start_ms for a in acts] == [0, 10000]
    assert [a.unit_id for a in acts] == ["u1", "u2"]
    assert acts[0].short == "关霜与封关"
    assert acts[0].long == "一张走不通的通关牒文"


def test_author_folds_first_act_into_title_and_writes_valid_sidecar(tmp_path, sidecar):
    document, quality = _write_inputs(tmp_path)
    A.author("ytrm-99", document, quality, series="初唐异人传", episode_number=1, total_ms=20000)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["version"] == O.SCHEMA_VERSION
    assert [o["kind"] for o in data["overlays"]] == ["title_card", "act_card"]
    title, act = data["overlays"]
    assert title["title"] == "初唐异人传"
    assert title["subtitle"] == "第一幕 · 一张走不通的通关牒文"
    assert title["episode"] == "第1集"
    assert act["start_ms"] == 10000 and act["end_ms"] == 12800
    assert act["title"] == "荒原深处的回首试探"
    assert act["subtitle"] == "第二幕 · 刀尖上的带路人"
    plan = O.load("ytrm-99")
    assert len(plan.specs) == 2


def test_blank_studio_title_falls_back_to_series_name(tmp_path, sidecar):
    document, quality = _write_inputs(tmp_path)
    A.author("ytrm-99", document, quality, series="初唐异人传", title="", total_ms=20000)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["overlays"][0]["title"] == "初唐异人传"


def test_existing_sidecar_is_not_overwritten_without_force(tmp_path, sidecar):
    document, quality = _write_inputs(tmp_path)
    A.author("ytrm-99", document, quality, series="初唐异人传", total_ms=20000)
    original = sidecar.read_text(encoding="utf-8")
    A.author("ytrm-99", document, quality, series="别的名字", total_ms=20000)
    assert sidecar.read_text(encoding="utf-8") == original


def test_force_rewrites_the_sidecar(tmp_path, sidecar):
    document, quality = _write_inputs(tmp_path)
    A.author("ytrm-99", document, quality, series="初唐异人传", total_ms=20000)
    A.author("ytrm-99", document, quality, series="初唐异人传", title="新的钩子", total_ms=20000, force=True)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["overlays"][0]["title"] == "新的钩子"


def test_heading_without_narration_is_ignored(tmp_path, sidecar):
    document = {
        "blocks": [
            {"block_id": "b0", "kind": "heading", "raw_text": "### 【第一幕：空幕】\n"},
        ],
        "units": [],
    }
    quality = {"takes": [{"unit_id": "u1", "sample_start": 0, "sample_end": 24000 * 5}]}
    assert A.acts_from_document(document, quality) == []
    document_path = tmp_path / "document.json"
    quality_path = tmp_path / "quality.json"
    document_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    A.author("ytrm-99", document_path, quality_path, series="初唐异人传", total_ms=5000)
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert [o["kind"] for o in data["overlays"]] == ["title_card"]


def test_first_act_is_on_first_card_even_with_half_a_second_of_leading_silence(tmp_path,sidecar):
    document,quality=_write_inputs(tmp_path)
    value=json.loads(quality.read_text(encoding='utf-8'))
    value['takes'][0]['sample_start']=12000
    quality.write_text(json.dumps(value),encoding='utf-8')
    A.author('fixture',document,quality,series='系列',total_ms=20000)
    cards=json.loads(sidecar.read_text(encoding='utf-8'))['overlays']
    assert len(cards)==2 and '第一幕' in cards[0]['subtitle'] and cards[0]['start_ms']==0
