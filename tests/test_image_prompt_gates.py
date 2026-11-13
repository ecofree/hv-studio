# -*- coding: utf-8 -*-
"""Pin the image pre-spend gates: style words, fallback screening, dedup.

Why: xlc-01 paid 159-call scale thinking until a hand-authored plan cut it to
45; lishimin-01's 78 prompts all carry `photorealistic` against the house
negative clause with zero code noticing (2026-10-14 audit P1-1/P1-2, P0-3).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.production

from app.orchestrator import build_image_jobs, screen_prompt


def test_source_brief_is_used_on_restart_and_rejects_source_drift(tmp_path, monkeypatch):
    import json
    from app.services.production_inputs import prepare
    monkeypatch.setattr('app.orchestrator._episode_dir', lambda _: tmp_path/'ep')
    root = tmp_path/'v2'; root.mkdir()
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps({'source_sha256':'source-a','units':[
        {'unit_id':'u1','source_text':'旁白提到 cartoon 一词，画面仍为战后的军营。'}]}), encoding='utf-8')
    brief = root/'source_image_briefs.json'
    brief.write_text(json.dumps({'source_sha256':'source-a','images':{
        'unit-000':{'en':'Soldiers regroup beside an abandoned banner after the rain.'}}}), encoding='utf-8')
    ep = type('Ep', (), {'id':'brief-fixture'})()
    first, _ = prepare(ep, plan, root)
    second, _ = prepare(ep, plan, root)
    assert first[0].request_fingerprint() == second[0].request_fingerprint()
    assert 'Soldiers regroup' in second[0].req.prompt
    assert 'Narration:' not in second[0].req.prompt
    data = json.loads(brief.read_text()); data['source_sha256']='other-source'
    brief.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='another narration'):
        prepare(ep, plan, root)


def test_style_word_warns_by_default_and_blocks_in_strict():
    ok, reason, _ = screen_prompt("K", "A Tang general, photorealistic, cinematic.")
    assert ok and "风格词" in reason
    ok, reason, _ = screen_prompt("K", "A Tang general, cinematic.")
    assert ok and reason == "OK"


def test_text_request_hints_never_block():
    ok, reason, _ = screen_prompt("K", "Close-up of bamboo slips titled with historical text.")
    assert ok
    ok, reason, _ = screen_prompt("K", "宫门悬挂匾额，上书标题大字。")
    assert ok and "文字" in reason


def test_fallback_draft_blocks_style_words_before_any_spend(tmp_path, monkeypatch):
    """prepare() without scene_plan must refuse style-word narration."""
    from app.services import production_inputs as PI
    from app.services.narration_pipeline import read  # noqa: F401

    plan = {"source_sha256": "s", "units": [
        {"unit_id": "u1", "source_text": "贞观二十三年，photorealistic 大战。"},
        {"unit_id": "u2", "source_text": "李治独自枯坐甘露殿直至天明。"},
    ]}
    plan_path = tmp_path / "plan.json"
    import json
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr("app.orchestrator._episode_dir", lambda _eid: tmp_path / "ep")
    ep = type("Ep", (), {"id": "synthetic-gate"})()
    with pytest.raises(ValueError, match="风格词"):
        PI.prepare(ep, str(plan_path), tmp_path / "v2")
    assert not (tmp_path / "v2").exists()


def test_fallback_draft_accepts_short_but_clean_units(tmp_path, monkeypatch):
    from app.services import production_inputs as PI
    import json

    plan = {"source_sha256": "s", "units": [
        {"unit_id": "u1", "source_text": "太宗留下了生平最后一句话："},
        {"unit_id": "u2", "source_text": "“朕佳儿佳妇，今以付卿。”"},
    ]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr("app.orchestrator._episode_dir", lambda _eid: tmp_path / "ep")
    ep = type("Ep", (), {"id": "synthetic-gate"})()
    jobs, scene = PI.prepare(ep, str(plan_path), tmp_path / "v2")
    assert len(jobs) == 2
    assert (tmp_path / "v2" / "storyboards").is_dir()


def test_generate_many_pays_once_for_duplicate_requests(tmp_path, monkeypatch):
    """Two keys, one request string: a single provider call, two files."""
    import base64
    import io
    import threading
    from PIL import Image

    from app.config.config import Channel
    from app.models import ImageGenRequest, ImageGenResponse, ImageGenItem
    from app.services import image as image_mod
    from app.services.image import ImageGenerator, ImageJob, generate_many
    from app.services.provider_budget import ProviderBudget

    image_mod._QPS_INTERVAL = 0.0
    calls: list = []
    lock = threading.Lock()

    def _b64():
        buf = io.BytesIO()
        Image.new("RGB", (1080, 1920), "red").save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    class FakeRelay:
        def all(self):
            return [Channel(name="solo", base_url="https://mock.example",
                            api_key="sk-solo", model="gpt-image-2")]
        def health(self, name):
            return None
        def record(self, *a, **k):
            pass

    gen = ImageGenerator.__new__(ImageGenerator)
    gen.relay = FakeRelay()
    gen.max_attempts = 1
    budget = ProviderBudget(tmp_path / "budget.json", image_requests=5)

    def _post(ch, path, body):
        # Mirror the real ImageGenerator._post ledger contract: reserve first,
        # finish on the way out. (The fake must not bypass accounting, or the
        # "one bill" assertion below proves nothing.)
        from app.services.provider_budget import current_image_budget
        ledger = current_image_budget()
        token = ledger.reserve("image", "test-fp")
        try:
            with lock:
                calls.append(body["prompt"])
            return ImageGenResponse(data=[ImageGenItem(b64_json=_b64())])
        except BaseException:
            ledger.finish(token, False)
            raise
        ledger.finish(token, True)

    gen._post = _post
    # generate_many resolves the generator via the module singleton, so the
    # fake must be installed there -- otherwise this test spends real money.
    monkeypatch.setattr(image_mod, "_image", gen)
    monkeypatch.setattr(image_mod, "get_image", lambda: gen)
    req = ImageGenRequest(model="gpt-image-2", prompt="same scene", size="1080x1920", n=1)
    jobs = [ImageJob("A", req, tmp_path / "A.png"),
            ImageJob("B", req, tmp_path / "B.png")]
    res = generate_many(jobs, budget=budget, progress=False)
    assert res["A"] is not None and res["B"] is not None
    assert len(calls) == 1, calls
    assert budget.snapshot()["used"]["image_requests"] == 1
    assert Path(res["A"]).is_file() and Path(res["B"]).is_file()


def test_build_image_jobs_reports_duplicates_without_generating(tmp_path, monkeypatch, capsys):
    import app.orchestrator as ORC

    spec = {"A": {"en": "A Tang soldier standing on a wall at dawn"},
            "B": {"en": "A Tang soldier standing on a wall at dawn"}}
    monkeypatch.setattr(ORC, "_read_image_prompts", lambda _eid: spec)
    monkeypatch.setattr(ORC, "_episode_dir", lambda _eid: tmp_path / "ep")
    jobs = ORC.build_image_jobs("synthetic-dup")
    assert [j.id for j in jobs] == ["A", "B"]
    assert "重复请求" in capsys.readouterr().out
