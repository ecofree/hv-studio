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

CONFIG_PATH = Path(__file__).parents[1] / "app/config/config.yaml"


def raw_config():
    return yaml.safe_load(CONFIG_PATH.read_text("utf-8"))


def configured():
    raw = raw_config()
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


def test_supports_matches_the_models_listed_in_config():
    """supports() 必须与 config 声明的 models 列表逐条一致。

    这条是本测试组的核心不变量。**不要**把支持矩阵硬编码成一份副本来断言：
    config 里并发数、渠道启停、模型清单都是按实测数据合法变动的（2026-09-26
    就把 cxk 1→2、qcyc 1→3 并调整了模型清单），副本式断言只会在每次合法改动
    后变红，逼人要么改代码要么改测试——最后往往是改测试，等于什么都没验。
    改为断言「Channel.supports 的判定 == config.models」，这样：
      - config 与 supports() 实现漂移会被抓住；
      - 合法配置改动不会让测试变红。
    """
    channels, routes = configured(), {r["name"]: r for r in raw_config()["image_channels"]}
    assert channels, "没有任何渠道通过校验"
    for c in channels:
        declared = routes[c.name].get("models") or [routes[c.name].get("model")]
        for model in declared:
            assert c.supports(model), f"{c.name}.supports({model}) 与 config.models 不符"
        for model in {BASE, NEW, FLARE, SUN}:
            expected = c.enabled and model in declared
            assert c.supports(model) is expected, (
                f"{c.name} 对 {model} 的判定是 {c.supports(model)}，"
                f"但 config（enabled={c.enabled}, models={declared}）推出的是 {expected}")


def test_disabled_channel_supports_nothing():
    off = [c for c in configured() if not c.enabled]
    for c in off:
        for model in (BASE, NEW, FLARE, SUN):
            assert not c.supports(model), f"已停用的 {c.name} 仍声称支持 {model}"


def test_primary_engine_has_at_least_one_routable_channel():
    """默认引擎必须至少有一个可用渠道。

    这正是 2026-09-26 踩到的生产故障：cxk 的 models 里加了新变体却漏了另一条
    线路的登记，生图时直接报「无可用渠道支持模型」。故障在配置里，不在代码里，
    所以验收必须有这条兜底。
    """
    primary = raw_config()["image"]["primary"]["engine"]
    assert any(c.supports(primary) for c in configured()), (
        f"image.primary.engine={primary!r} 没有任何启用的渠道支持 —— "
        f"生图一定会在派活时失败")


def test_concurrency_is_applied_and_within_declared_bounds():
    """.env 的 key 齐备时，config 的 max_concurrency 必须真的生效。

    配置写了并发却没被读进去，等于单渠道串行 —— 表现为生图慢一倍但没有任何报错，
    是那种只能靠肉眼发现的退化。取值范围由 ChannelRoute 的 ge/le 约束另行保证。
    """
    routes = {r["name"]: r for r in raw_config()["image_channels"]}
    for c in configured():
        assert c.max_concurrency == routes[c.name].get("max_concurrency", 1)
        assert 1 <= c.max_concurrency <= 16



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
    for channel, model in [
        (Channel(name="base-only", api_key="test", base_url="https://mock.example", model=BASE), NEW),
        (Channel(name="aicost", api_key="test", base_url="https://mock.example", model=BASE, enabled=False), BASE),
    ]:
        with pytest.raises(m.ImageError, match="不支持"):
            call(request_class(model=model, prompt="test"), channel=channel, budget=budget)
    assert seen == ["cxk"]


def test_unsupported_batch_spends_no_budget(generator, monkeypatch, tmp_path):
    budget = ProviderBudget(tmp_path / "budget.json", image_requests=2)
    monkeypatch.setattr(generator, "_post", lambda *a: pytest.fail("transport must not run"))
    with pytest.raises(m.ImageError, match="整批未发起"):
        m.generate_many([job(tmp_path, "valid"), job(tmp_path, "invalid", "unsupported")], budget=budget)
    assert not (tmp_path / "valid.png").exists()
    assert not budget.snapshot()["attempts"]


def test_variants_share_supplier_concurrency_and_run_both_suppliers(generator, monkeypatch, tmp_path):
    # Exercise generic concurrency with explicit synthetic suppliers, independently
    # of which real suppliers are currently available in production.
    generator.relay = relay_mod.RelayManager([
        Channel(name="first", api_key="test", base_url="https://mock.example", model=BASE, models=[BASE, NEW, FLARE, SUN]),
        Channel(name="second", api_key="test", base_url="https://mock.example", model=BASE),
    ])
    active = {"first": 0, "second": 0}
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
                for c in generator.relay.all()
                for model in ((FLARE, SUN) if c.name == "first" else (BASE, BASE))]
    budget = ProviderBudget(tmp_path/'concurrent-budget.json', image_requests=8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(generator.generate, r, c, budget=budget) for c, r in requests]
        for f in futures:
            f.result()
    assert peak == {"first": 1, "second": 1}
    seen.clear()
    jobs = [job(tmp_path, str(i), model) for i, model in enumerate([FLARE, SUN, NEW, BASE])]
    results = m.generate_many(jobs, workers=20, progress=False, budget=budget)
    assert all(results[j.id] for j in jobs)
    assert len(seen) == 4
    assert all(model == BASE or name == "first" for name, model in seen)


def test_failover_keeps_model_and_charges_actual_attempts(generator, monkeypatch, tmp_path):
    generator.relay = relay_mod.RelayManager([
        Channel(name=name, api_key="test", base_url="https://mock.example", model=BASE)
        for name in ("first", "second")
    ])
    seen = []
    resp = response()
    def transport(c, path, body):
        seen.append((c.name, body["model"]))
        if c.name == "first":
            raise m.ImageError("provider rejected request")
        return resp
    monkeypatch.setattr(generator, "_post_unlocked", transport)
    budget = ProviderBudget(tmp_path / "budget.json", image_requests=2)
    task = job(tmp_path, "fallback", BASE)
    result = m.generate_many([task], budget=budget, progress=False)
    assert result[task.id]
    assert seen == [("first", BASE), ("second", BASE)]
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
