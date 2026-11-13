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


def test_malformed_success_response_is_preserved_and_never_repaid(generator, monkeypatch, tmp_path):
    calls = []
    class Malformed:
        status_code = 200
        text = 'incomplete provider response'
        def json(self): raise ValueError('invalid JSON')
    monkeypatch.setattr(m.requests, 'post', lambda *a, **kw: calls.append(1) or Malformed())
    candidate = job(tmp_path, 'malformed')
    first = ProviderBudget(tmp_path/'first.json', image_requests=3)
    assert not m.generate_many([candidate], budget=first, progress=False)[candidate.id]
    assert len(calls) == 1 and first.snapshot()['used']['image_requests'] == 1
    assert candidate.out.with_suffix('.png.response.unparsed-response.json').is_file()
    second = ProviderBudget(tmp_path/'second.json', image_requests=3)
    assert not m.generate_many([candidate], budget=second, progress=False)[candidate.id]
    assert len(calls) == 1 and second.snapshot()['used']['image_requests'] == 0


def test_image_api_reports_observed_dimensions(tmp_path, monkeypatch):
    from app.controllers.v1.image import test, TestRequest
    path = tmp_path/'source.png'
    Image.new('RGB', (1024,1536)).save(path)
    monkeypatch.setattr(m, 'run_probe', lambda *a: path)
    result = test(TestRequest(prompt='camp', budget_id='offline', max_images=1), _tok=None)
    assert result['actual_size'] == [1024,1536]
    assert result['requested_size'] == '1080x1920'


def test_ssl_unknown_stops_only_affected_plate_across_models(generator,tmp_path,monkeypatch):
    def post(url, **kwargs):
        if kwargs['json']['prompt']=='uncertain':
            raise m.requests.exceptions.SSLError('EOF after POST')
        return SimpleNamespace(status_code=200,json=lambda:response().model_dump())
    monkeypatch.setattr(m.requests,'post',post)
    def planned(name,model,folder):
        request=ImageGenRequest(model=model,prompt=name)
        return m.ImageJob(name,request,tmp_path/folder/(name+'.png'),dispatch_directory=tmp_path/'guards')
    first=planned('uncertain',BASE,'first')
    other=planned('other',BASE,'second')
    b=ProviderBudget(tmp_path/'budget.json',image_requests=2)
    result=m.generate_many([first,other],progress=False,budget=b,provider_attempts_per_job=1)
    assert result['other'] and not result['uncertain'] and len(b.snapshot()['attempts'])==2
    alternative=planned('uncertain',SUN,'third')
    new=ProviderBudget(tmp_path/'resume.json',image_requests=1)
    repeated=m.generate_many([alternative,other],progress=False,budget=new,provider_attempts_per_job=1)
    assert repeated['other'] and not repeated['uncertain'] and not new.snapshot()['attempts']


def test_preflight_and_dispatch_share_image_parameter_rules():
    for size in ('auto', '1080x1920', '1024x1536', 'unverified-size'):
        request = ImageGenRequest(model=BASE, prompt='camp', size=size)
        if m.validate_image_params(size, 'low', BASE):
            with pytest.raises(ValueError): request.validate_for_subrouter()
        else: request.validate_for_subrouter()


def response():
    stream = io.BytesIO()
    Image.new("RGB", (1080, 1920), "blue").save(stream, format="PNG")
    return ImageGenResponse(data=[ImageGenItem(b64_json=base64.b64encode(stream.getvalue()).decode())])


def job(tmp_path, name, model=FLARE):
    return m.ImageJob(name, ImageGenRequest(model=model, prompt=name), tmp_path / f"{name}.png")


def test_scoped_supplier_save_keeps_other_supplier_history(generator):
    channels = generator.relay.all()
    assert len(channels) >= 2
    other = channels[1]
    generator.relay.record(other.name, False, 1)
    scoped = relay_mod.RelayManager([channels[0]])
    scoped.record(channels[0].name, True, 1)
    restored = relay_mod.RelayManager(channels)
    assert len(restored.health(other.name).history) == 1
    assert restored.health(other.name).history[0][0] is False
    # Reopening is not enough: the next production request must persist again.
    restored.record(other.name, True, 2)
    reopened = relay_mod.RelayManager(channels)
    assert len(reopened.health(other.name).history) == 2
    assert all(isinstance(row, tuple) for row in reopened.health(other.name).history)


def test_received_image_survives_channel_statistics_failure(generator, monkeypatch, tmp_path):
    calls=[]
    monkeypatch.setattr(generator, '_post_unlocked', lambda *a: calls.append(1) or response())
    def fail(): raise OSError('fixture health file unavailable')
    monkeypatch.setattr(generator.relay, '_save_locked', fail)
    candidate=job(tmp_path,'health-write')
    budget=ProviderBudget(tmp_path/'budget.json', image_requests=1)
    result=m.generate_many([candidate],budget=budget,progress=False)
    assert result[candidate.id] and candidate.is_done()
    assert len(calls)==1 and budget.snapshot()['used']['image_requests']==1
    assert json.loads(relay_mod.HEALTH_PATH.with_suffix('.error.json').read_text())['phase']=='channel_statistics'
    m.generate_many([candidate],budget=budget,progress=False)
    assert len(calls)==1


def test_unhealthy_supplier_never_receives_batch_dispatch(generator, tmp_path, monkeypatch):
    channels = generator.relay.all()
    bad = channels[0]
    for _ in range(3): generator.relay.record(bad.name, False, 1)
    seen = []
    def generate(req, channel=None):
        seen.append(channel.name)
        return response()
    monkeypatch.setattr(generator, 'generate', generate)
    out = m.generate_many([job(tmp_path, 'health-fixture', BASE)], progress=False,
                         budget=ProviderBudget(tmp_path/'budget.json', image_requests=1))
    assert out['health-fixture']
    assert seen and bad.name not in seen


def test_channel_health_recovers_when_old_failures_expire(monkeypatch):
    now = 100000.
    monkeypatch.setattr(relay_mod.time, 'time', lambda: now)
    health = relay_mod.ChannelHealth(configured()[0])
    health.history.extend([(False, 5., now - 7200)] * 4)
    assert health.is_healthy
    assert health.score() == 100.
    health.history.extend([(False, 5., now - 10)] * 3)
    assert not health.is_healthy
    health.history.extend([(True, 5., now)] * 4)
    assert health.is_healthy


def test_recovery_capacity_is_bounded_and_success_restores_slots(monkeypatch):
    clock = [100000.]
    monkeypatch.setattr(relay_mod, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep))
    health = relay_mod.ChannelHealth(configured()[0])
    health.history.extend((False, 1., clock[0]-i) for i in range(5, -1, -1))
    assert health.dispatch_capacity == 0
    clock[0] += 60
    assert health.dispatch_capacity == 1
    health.record(False, 1)
    assert health.dispatch_capacity == 0
    clock[0] += 60
    health.record(True, 1)
    assert health.dispatch_capacity == 0
    clock[0] += 60
    assert health.dispatch_capacity == 1
    health.record(True, 1)
    assert not health.is_healthy  # old ratio is still poor, but recovery is proven
    assert health.dispatch_capacity == health.channel.max_concurrency


def test_real_batch_flow_recovers_cooling_supplier_without_repay(generator, tmp_path, monkeypatch):
    clock = [100000.]
    monkeypatch.setattr(relay_mod, 'time', SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep))
    channel = next(c for c in generator.relay.all() if c.name == 'qcyc')
    monkeypatch.setattr(generator.relay, 'all', lambda: [channel])
    health = generator.relay.health(channel.name)
    health.history.extend((False, 1., clock[0]-i) for i in range(5, -1, -1))
    original_take = m._SupplierQueue.take
    def take(queue):
        # Advance the cooldown deterministically, still exercise the real queue.
        if not getattr(queue, '_test_clock', False):
            queue._test_clock = True
            original_wait = queue.changed.wait
            def wait(timeout=None):
                assert timeout is not None
                clock[0] += timeout
                return original_wait(.002)
            queue.changed.wait = wait
        return original_take(queue)
    monkeypatch.setattr(m._SupplierQueue, 'take', take)
    seen, active, peak = [], [0], [0]
    lock = threading.Lock()
    payload = response().model_dump()
    def post(url, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            seen.append(kwargs['json']['prompt'])
        time.sleep(.15)
        with lock: active[0] -= 1
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(m.requests, 'post', post)
    jobs = [job(tmp_path, 'recover-'+str(i)) for i in range(8)]
    budget = ProviderBudget(tmp_path/'recover-budget.json', image_requests=8)
    result = m.generate_many(jobs, budget=budget, progress=False)
    assert all(result[j.id] for j in jobs)
    assert len(seen) == len(set(seen)) == 8
    assert peak[0] == 3
    assert budget.snapshot()['used']['image_requests'] == 8
    again = m.generate_many(jobs, budget=budget, progress=False)
    assert all(again[j.id] for j in jobs) and len(seen) == 8


def test_capacity_estimate_uses_completed_attempts_after_queueing(tmp_path, monkeypatch):
    supplier = SimpleNamespace(name='offline', max_concurrency=2, supports=lambda model: model == BASE)
    relay = SimpleNamespace(all=lambda: [supplier],
                            health=lambda name: SimpleNamespace(is_healthy=True, history=[(True, 900)]))
    budget = tmp_path / 'budget.json'
    attempts = [dict(id=key, kind='image', supplier='offline', status=status, reserved_at=100,
                     completed_at=end, queue_seconds=400) for key, status, end in
                [('a', 'succeeded', 110), ('b', 'succeeded', 120), ('c', 'unknown', 180)]]
    budget.write_text(json.dumps({'attempts': attempts}), encoding='utf-8')
    costs = tmp_path / 'costs.json'
    costs.write_text(json.dumps([dict(channel='offline', model=BASE, attempt_id=a['id'],
                                      budget_path=str(budget)) for a in attempts]), encoding='utf-8')
    monkeypatch.setattr(m, '_COST_PATH', costs)
    estimate = m.capacity_estimate(BASE, 5, relay=relay, budget_path=budget)
    assert estimate['routes'] == [dict(name='offline', slots=2, samples=2, median_seconds=15.)]
    assert estimate['seconds'] == 45.
    costs.write_text('[]', encoding='utf-8')
    assert m.capacity_estimate(BASE, 5, relay=relay, budget_path=budget)['seconds'] is None


def test_chinese_policy_rejection_and_http_400_stop_request():
    assert m.stop_reason(m._mk_error('offline',400,'提示词违反服务规则'))
    assert m.stop_reason(m._mk_error('offline',400,'invalid request'))
    assert m.stop_reason(m._mk_error('offline',503,'service unavailable')) is None


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


def test_explicit_single_attempt_does_not_fail_over_or_retry(generator, monkeypatch, tmp_path):
    generator.relay = relay_mod.RelayManager([
        Channel(name=name, api_key="test", base_url="https://mock.example", model=BASE)
        for name in ("first", "second")
    ])
    seen=[]
    def transport(c, path, body):
        seen.append(c.name)
        raise m.TransientImageError("temporary provider failure")
    monkeypatch.setattr(generator, "_post_unlocked", transport)
    budget=ProviderBudget(tmp_path/'single-attempt.json',image_requests=1)
    task=job(tmp_path,'one-attempt',BASE)
    result=m.generate_many([task],budget=budget,progress=False,provider_attempts_per_job=1)
    assert result[task.id] is None
    assert seen==['first']
    assert budget.snapshot()['used']['image_requests']==1


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


def test_resaving_authored_brief_preserves_reference_model_and_unchanged_bytes(tmp_episodes,monkeypatch):
    from app.services import studio_workflow as workflow
    folder=tmp_episodes/'routing'
    folder.mkdir()
    target=folder/'image_prompts.json'
    before={'hero':{'en':'A general at the gate','model':SUN,'reference_path':'actor.png'}}
    target.write_text(json.dumps(before),encoding='utf-8')
    monkeypatch.setattr(workflow,'identity',lambda eid:'snapshot')
    original=target.read_bytes()
    workflow.save_image_brief('routing','hero',before['hero']['en'],expected_identity='snapshot',model=SUN)
    assert target.read_bytes()==original
    workflow.save_image_brief('routing','hero','A general at the palace gate',expected_identity='snapshot')
    after=json.loads(target.read_text('utf-8'))['hero']
    assert (after['model'],after['reference_path'])==(SUN,'actor.png')


def test_default_routing_at_dispatch_for_people_inserts_crops_and_reference_edits(generator, tmp_path, monkeypatch):
    from app.config import config
    from app.services.scene_models import plan_scene_models
    settings = config.get_settings().model_copy(update={'channels': configured()})
    monkeypatch.setattr('app.services.scene_models.get_settings', lambda: settings)
    monkeypatch.setattr('app.services.scene_models.get_relay', lambda: generator.relay)
    main = 'gpt-image-2.6-sunburst'
    crop = main + '-4k'
    briefs = {'people': '两人步入大殿', 'crowd': {'en': 'Officers gather in the courtyard'},
              'seal': {'shot_type': 'insert'}, 'close': {}, 'explicit': {'model': BASE},
              'ceiling': {}, 'character': {'shot_type': 'insert'}}
    scene = {'unit_scenes': [{'image': k} for k in briefs]}
    scene['unit_scenes'][-2]['motion'] = {'max_zoom': 1.6}
    directing = {'visual_events': [{'image_id': 'close', 'motion': {'end_zoom': 1.6}}]}
    reference = tmp_path/'character-reference.png'
    Image.new('RGB', (64, 64)).save(reference)

    def batch():
        jobs = [job(tmp_path, k, BASE) for k in briefs]
        jobs[-1] = m.ImageJob('character', ImageEditRequest(model=BASE, prompt='character',
                              image=base64.b64encode(reference.read_bytes()).decode()),
                              tmp_path/'character.png', (str(reference),))
        return jobs

    def planned(jobs, persist):
        plan_scene_models(tmp_path, jobs, briefs, scene=scene, directing=directing, persist=persist)
        return {j.id: j.req.model for j in jobs}

    expected = planned(batch(), False)
    assert expected['close'].endswith('-4k') and expected['explicit'] == BASE
    ordinary = [expected[k] for k in ('people', 'crowd', 'seal', 'ceiling', 'character')]
    assert len(set(ordinary)) == 5 and all(not m.endswith('-4k') for m in ordinary)
    assert not (tmp_path/'scene-models.json').exists()
    jobs = batch()
    assert planned(jobs, True) == expected
    assert isinstance(jobs[-1].req, ImageEditRequest) and jobs[-1].reference_paths == (str(reference),)
    sent = {}
    def post(channel, endpoint, body):
        sent[body['prompt']] = (body['model'], channel.name, endpoint)
        return response()
    monkeypatch.setattr(generator, '_post_unlocked', post)
    budget = ProviderBudget(tmp_path/'budgets/test.json', image_requests=len(jobs))
    for j in jobs:
        j.out_path = tmp_path/'images'/j.request_fingerprint()/j.out.name
    m.generate_many(jobs, budget=budget, progress=False)
    assert {k: v[0] for k,v in sent.items()} == expected
    assert sent['character'][2].endswith('/edits') and sent['close'][1] == 'cxk'
    assert all(j.is_done() for j in jobs)
    assert planned(batch(), False) == expected
    m.generate_many(jobs, budget=budget, progress=False)
    assert len(budget.snapshot()['attempts']) == len(jobs)


def test_free_supplier_skips_incompatible_jobs_and_fills_all_five_slots(generator, tmp_path, monkeypatch):
    from collections import Counter
    from app.config import config
    from app.services import scene_models
    settings = config.get_settings().model_copy(update={'channels': configured()})
    monkeypatch.setattr(scene_models, 'get_settings', lambda: settings)
    monkeypatch.setattr(scene_models, 'get_relay', lambda: generator.relay)
    fresh = [job(tmp_path, f'scene-{i}', BASE) for i in range(20)]
    duplicate = job(tmp_path, 'duplicate', BASE)
    duplicate.req = fresh[-1].req.model_copy()
    scene_models.plan_scene_models(tmp_path, [*fresh, duplicate], {}, persist=False)
    models = {j.req.model for j in fresh}
    assert models == {m for c in configured() if c.enabled for m in c.models if not m.endswith('-4k')}
    assert duplicate.req.model == fresh[-1].req.model
    exclusive = [job(tmp_path, f'crop-{i}', 'gpt-image-2.6-4k') for i in range(5)]
    jobs = [*exclusive, *fresh, duplicate]
    first_wave = threading.Barrier(5, timeout=8)
    sent = []
    lock = threading.Lock()
    pixels = response()
    def transport(channel, endpoint, body):
        with lock:
            sent.append(channel.name)
            first = len(sent) <= 5
        if first:
            first_wave.wait()
        return pixels
    monkeypatch.setattr(generator, '_post_unlocked', transport)
    budget = ProviderBudget(tmp_path/'budget.json', image_requests=25)
    result = m.generate_many(jobs, budget=budget, progress=False)
    assert all(result[j.id] for j in jobs)
    assert Counter(sent[:5]) == {'cxk': 2, 'qcyc': 3}
    assert len(sent) == len(budget.snapshot()['attempts']) == 25


def test_unavailable_models_fall_back_before_dispatch_without_extra_attempts(generator, tmp_path, monkeypatch):
    from app.config import config
    from app.services import scene_models
    channels = [c for c in configured() if c.name == 'qcyc']
    settings = config.get_settings().model_copy(update={'channels': channels})
    monkeypatch.setattr(scene_models, 'get_settings', lambda: settings)
    monkeypatch.setattr(scene_models, 'get_relay', lambda: generator.relay)
    scene = {'scenes': [{'image': 'crop', 'motion': {'start_zoom': 1.7}}]}
    def chosen():
        plate = job(tmp_path, 'crop', BASE)
        scene_models.plan_scene_models(tmp_path, [plate], {}, scene=scene)
        return plate.req.model
    assert chosen() == 'gpt-image-2.6-sunburst'
    channels[0].models = ['gpt-image-2.6-flare', BASE]
    assert chosen() == 'gpt-image-2.6-flare'
    channels[0].models = [FLARE, BASE]
    assert chosen() == FLARE
    channels[0].models = [BASE]
    assert chosen() == BASE
    channels[0].models = [BASE, 'gpt-image-2.6-sunburst']
    plates = [job(tmp_path, k, BASE) for k in ('crop', 'new')]
    scene_models.plan_scene_models(tmp_path, plates, {}, scene=scene)
    assert [p.req.model for p in plates] == [BASE, 'gpt-image-2.6-sunburst']


def test_old_cache_and_reserved_images_stay_bound_per_request(generator, tmp_path, monkeypatch):
    from app.config import config
    from app.services import scene_models
    from app.services.provenance import atomic_json
    settings = config.get_settings().model_copy(update={'channels': configured()})
    monkeypatch.setattr(scene_models, 'get_settings', lambda: settings)
    monkeypatch.setattr(scene_models, 'get_relay', lambda: generator.relay)
    old = job(tmp_path, 'old', BASE)
    cache = tmp_path/'images'/old.request_fingerprint()/'old.png.request.json'
    atomic_json(cache, {'request_fingerprint': old.request_fingerprint()})
    pending = job(tmp_path, 'pending', FLARE)
    budget = ProviderBudget(tmp_path/'budgets/held.json', image_requests=1)
    budget.reserve('image', pending.provider_fingerprint())
    fresh = job(tmp_path, 'fresh', BASE)
    approved = job(tmp_path, 'approved', SUN)
    jobs = [job(tmp_path, k.id, 'gpt-image-2.6-sunburst') for k in (old, pending, approved, fresh)]
    scene_models.plan_scene_models(tmp_path, jobs, {}, approved={'approved': approved.request_fingerprint()})
    assert [j.req.model for j in jobs] == [BASE, FLARE, SUN, 'gpt-image-2.6-sunburst']
    assert [j.model_bound for j in jobs] == [True, True, True, False]
    assert json.loads((tmp_path/'scene-models.json').read_text()).keys() == {'fresh'}
    assert len(budget.snapshot()['attempts']) == 1
    calls = []
    monkeypatch.setattr(generator, '_post_unlocked', lambda ch, path, body: calls.append(body['prompt']) or response())
    resumed = ProviderBudget(tmp_path/'budgets/resume.json', image_requests=2)
    result = m.generate_many([jobs[1], jobs[-1]], budget=resumed, progress=False)
    assert calls == ['fresh'] and not result['pending'] and result['fresh']


def test_story_editor_review_and_preparation_agree_across_old_and_new_approvals(tmp_episodes, monkeypatch, generator):
    from app import paths
    from app.managers import load
    from app.services import production_admission as admission, studio_workflow as workflow, studio_workspace
    from app.services.production_inputs import prepare
    from app.services.provenance import atomic_json, fingerprint
    from app.config import config
    monkeypatch.setattr('app.services.voice_assets.resolve_voice_asset', lambda: (None, {'version': 'fixture'}))
    monkeypatch.setattr('app.services.scene_models.get_relay', lambda: generator.relay)
    settings = config.get_settings().model_copy(update={'channels': configured()})
    monkeypatch.setattr('app.services.scene_models.get_settings', lambda: settings)
    eid = 'model-review'
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    studio_workspace.create_episode(eid, '夹具', '侍卫进门。', scope='full')
    doc = workflow.document(eid)
    directory = paths.episode_dir(eid)
    root = directory/'production/v2'
    prompt = 'A bronze seal rests on the wooden desk at the entrance of the palace.'
    workflow.save_image_brief(eid, 'seal', prompt, expected_identity=workflow.identity(eid))
    atomic_json(directory/'scene_plan.json', {'source_sha256': doc['source_sha256'],
                'unit_scenes': [{'unit_id': u['unit_id'], 'image': 'seal'} for u in doc['units']]})
    def planned(preview=True):
        return prepare(load(eid), doc, root, preview=preview)[0][0]
    old_review = admission.inputs(eid, _legacy=True)
    atomic_json(directory/'production/input-approval.json', {'decision': 'approved_inputs', 'reviewer': 'fixture',
                'finding': 'fixture', 'input_fingerprint': fingerprint(old_review)})
    assert planned().req.model == BASE and admission.check(eid)['eligible']
    workflow.save_image_brief(eid, 'seal', prompt, shot_type='insert', expected_identity=workflow.identity(eid))
    preview = planned()
    assert preview.req.model == 'gpt-image-2.6-sunburst'
    material = admission.inputs(eid)
    assert material['image_requests'] == {'seal': preview.request_fingerprint()}
    assert not (root/'scene-models.json').exists()
    admission.record_approval(eid, 'fixture', 'fixture', expected_fingerprint=fingerprint(material))
    assert planned(False).request_fingerprint() == preview.request_fingerprint()
    assert workflow.assign_scene_models(eid, ['seal'], model=BASE, expected_identity=workflow.identity(eid)) == {}
    workflow.save_image_brief(eid, 'seal', prompt, shot_type='close_up', expected_identity=workflow.identity(eid))
    assert planned().req.model == 'gpt-image-2.6-sunburst'
    assert not admission.check(eid)['eligible']
    assert workflow.assign_scene_models(eid, ['seal'], model=BASE, expected_identity=workflow.identity(eid)) == {'seal': BASE}
    assert planned().req.model == BASE


def _hold_supplier_slot(folder, started, release):
    import time
    relay_mod.STATE_DIR=Path(folder)
    relay=relay_mod.RelayManager([Channel(name='fixture',api_key='fixture',base_url='https://mock.invalid',model=BASE,max_concurrency=2)])
    with relay.request_slot('fixture'):
        started.put(time.monotonic())
        release.wait(20)


def test_configured_slots_are_shared_across_processes(tmp_path):
    import multiprocessing
    from queue import Empty
    ctx=multiprocessing.get_context('spawn')
    arrivals=ctx.Queue(); release=ctx.Event()
    procs=[ctx.Process(target=_hold_supplier_slot,args=(str(tmp_path),arrivals,release)) for _ in range(3)]
    try:
        for proc in procs:proc.start()
        arrivals.get(timeout=15);arrivals.get(timeout=15)
        with pytest.raises(Empty):arrivals.get(timeout=.3)
        release.set()
        arrivals.get(timeout=15)
        for proc in procs:proc.join(10);assert proc.exitcode==0
    finally:
        release.set()
        for proc in procs:
            if proc.is_alive():proc.terminate();proc.join(5)
        arrivals.close()
