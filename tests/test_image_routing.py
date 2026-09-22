"""Offline supplier/model routing, concurrency and cache regression coverage."""

import pytest

pytestmark = pytest.mark.production

import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import yaml
from PIL import Image

from app.config.config import Channel, configured_channels
from app.managers import relay as relay_mod
from app.models import ImageGenRequest, ImageEditRequest, ImageGenResponse, ImageGenItem
from app.services import image as m
from app.services.provider_budget import ProviderBudget

BASE = "gpt-image-2"
NEW = "gpt-image-2.5"
FLARE = NEW + "-flare"
SUN = NEW + "-sunburst"


def configured():
    raw = yaml.safe_load((Path(__file__).parents[1] / "app/config/config.yaml").read_text("utf-8"))
    return configured_channels({f"{name}_{BASE}": "sk-offline" for name in
                                ("cxk", "aicost", "qcyc")}, raw["image_channels"])


@pytest.fixture
def generator(monkeypatch, tmp_path):
    monkeypatch.setattr(relay_mod, "HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(relay_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda n: 0)
    gen = m.ImageGenerator.__new__(m.ImageGenerator)
    gen.relay = relay_mod.RelayManager(configured())
    gen.max_attempts = 3
    monkeypatch.setattr(m, "get_image", lambda: gen)
    return gen


def response():
    stream = io.BytesIO()
    Image.new("RGB", (1080, 1920), "blue").save(stream, format="PNG")
    return ImageGenResponse(data=[ImageGenItem(b64_json=base64.b64encode(stream.getvalue()).decode())])


def job(tmp_path, name, model=FLARE):
    return m.ImageJob(name, ImageGenRequest(model=model, prompt=name), tmp_path / f"{name}.png")


@pytest.mark.parametrize("model,names", [(BASE, {"cxk", "aicost"}), (NEW, {"cxk"}),
                                        (FLARE, {"cxk"}), (SUN, {"cxk"})])
def test_configured_support_matrix(model, names):
    assert {c.name for c in configured() if c.supports(model)} == names
    assert all(c.max_concurrency == 1 for c in configured())


def test_missing_keys_fail_closed_and_duplicate_rejected():
    route = dict(name="cxk", key_env="existing", models=[NEW])
    assert configured_channels({}, [route]) == []
    with pytest.raises(ValueError, match="duplicate"):
        configured_channels({}, [route, route])


@pytest.mark.parametrize("request_class", [ImageGenRequest, ImageEditRequest])
def test_direct_request_filters_model_and_rejects_disabled(generator, monkeypatch, request_class, tmp_path):
    seen = []
    monkeypatch.setattr(generator, "_post_unlocked", lambda c, p, b: seen.append(c.name) or ImageGenResponse())
    budget = ProviderBudget(tmp_path/'direct-budget.json', image_requests=1)
    req = request_class(model=NEW, prompt="test")
    call = generator.edit if request_class is ImageEditRequest else generator.generate
    call(req, budget=budget)
    assert seen == ["cxk"]
    for channel in [next(c for c in configured() if c.name == "aicost"),
                    Channel(name="qcyc", api_key="test", base_url="https://mock.example",
                            model=NEW, enabled=False)]:
        with pytest.raises(m.ImageError, match="不支持"):
            call(req, channel=channel, budget=budget)
    assert seen == ["cxk"]


def test_unsupported_batch_spends_no_budget(generator, monkeypatch, tmp_path):
    budget = ProviderBudget(tmp_path / "budget.json", image_requests=2)
    monkeypatch.setattr(generator, "_post", lambda *a: pytest.fail("transport must not run"))
    with pytest.raises(m.ImageError, match="整批未发起"):
        m.generate_many([job(tmp_path, "valid"), job(tmp_path, "invalid", "unsupported")], budget=budget)
    assert not (tmp_path / "valid.png").exists()
    assert not budget.snapshot()["attempts"]


def test_variants_share_supplier_concurrency_and_run_both_suppliers(generator, monkeypatch, tmp_path):
    active = {"cxk": 0, "aicost": 0}
    peak = dict(active)
    seen = []
    lock = threading.Lock()
    both = threading.Event()
    resp = response()

    def transport(c, path, body):
        with lock:
            active[c.name] += 1
            peak[c.name] = max(peak[c.name], active[c.name])
            seen.append((c.name, body["model"]))
            if all(active.values()):
                both.set()
        assert both.wait(5), "independent suppliers should overlap"
        time.sleep(0.01)
        with lock:
            active[c.name] -= 1
        return resp

    monkeypatch.setattr(generator, "_post_unlocked", transport)
    # Direct and batch traffic both use the same supplier slots.
    requests = [(c, ImageGenRequest(model=model, prompt="test"))
                for c in configured()
                for model in ((FLARE, SUN) if c.name == "cxk" else (BASE, BASE))]
    budget = ProviderBudget(tmp_path/'concurrent-budget.json', image_requests=8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(generator.generate, r, c, budget=budget) for c, r in requests]
        for f in futures:
            f.result()
    assert peak == {"cxk": 1, "aicost": 1}
    seen.clear()
    jobs = [job(tmp_path, str(i), model) for i, model in enumerate([FLARE, SUN, NEW, BASE])]
    results = m.generate_many(jobs, workers=20, progress=False, budget=budget)
    assert all(results[j.id] for j in jobs)
    assert len(seen) == 4
    assert all(model == BASE or name == "cxk" for name, model in seen)


def test_failover_keeps_model_and_charges_actual_attempts(generator, monkeypatch, tmp_path):
    seen = []
    resp = response()
    def transport(c, path, body):
        seen.append((c.name, body["model"]))
        if c.name == "cxk":
            raise m.ImageError("provider rejected request")
        return resp
    monkeypatch.setattr(generator, "_post_unlocked", transport)
    budget = ProviderBudget(tmp_path / "budget.json", image_requests=2)
    task = job(tmp_path, "fallback", BASE)
    result = m.generate_many([task], budget=budget, progress=False)
    assert result[task.id]
    assert seen == [("cxk", BASE), ("aicost", BASE)]
    assert len(budget.snapshot()["attempts"]) == 2
    assert task.req.model == BASE


def test_cached_image_does_not_need_channel_and_model_change_invalidates(monkeypatch, tmp_path):
    task = job(tmp_path, "cached", BASE)
    task.install(base64.b64decode(response().data[0].b64_json), task.request_fingerprint())
    monkeypatch.setattr(m, "get_image", lambda: pytest.fail("cached image must bypass channel pool"))
    assert m.generate_many([task]) == {task.id: str(task.out)}
    task.req.model = FLARE
    assert not task.is_done()


def test_prompt_model_override_is_bound_to_request(tmp_episodes, monkeypatch):
    from app import orchestrator
    ep = tmp_episodes / "routing"
    ep.mkdir()
    (ep / "image_prompts.json").write_text(json.dumps({"shot": {"en": "Historical landscape", "model": SUN}}))
    jobs = orchestrator.build_image_jobs("routing", prescreen=False)
    assert jobs[0].req.model == SUN
