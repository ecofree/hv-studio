# -*- coding: utf-8 -*-
"""#6 生图优化项回归：transient 重试 / prompt 预筛 / cost 台账 / 进度 ETA。

外加一条顺手发现的真 bug 回归：save_b64_image 曾因 `def` 行被误删，
函数体变成 generate_many return 之后的死代码（能 import、能编译，但
hasattr 为 False），cli.py 与 controllers/v1/image.py 一调用就 ImportError。
自 5160de3 起已坏，必须钉死。

重试策略为什么要测：原实现「任何失败都直接换渠道」，一次 429 抖动就把
三个渠道池的配额各烧一遍；而三池是各自独立的 quota，这是真金白银。
"""
from __future__ import annotations

import base64
import json
import types
from pathlib import Path
from queue import Queue

import pytest

from app.managers.episode import episode_path
from app.services import image as m

# P1-6：不再用模块级 ROOT_EPISODES 常量（那是写死真实 D:/hv/episodes 的写法）。
# 需要单集目录的用例改用 tmp_episodes 夹具返回的根，或 app.paths.episode_dir()。


def _ep_dir(ep_id: str) -> Path:
    """当前生效的 episodes 根下该集目录（测试期被 tmp_episodes 重定向到 tmp）。"""
    from app.paths import episode_dir
    return episode_dir(ep_id)


# ---------------------------------------------------------------------------
# 0. save_b64_image 回归（5160de3 删 def 行事故）
# ---------------------------------------------------------------------------
def test_save_b64_image_is_importable_and_writable(tmp_path):
    """符号必须在，且能建父目录 + 写盘。参数顺序 (b64, path)。"""
    assert hasattr(m, "save_b64_image"), (
        "save_b64_image 又丢了：cli.py:139 与 controllers/v1/image.py:37 "
        "按 (b64, path) 调用它")
    payload = b"\x89PNG\r\n\x1a\n" + b"payload" * 64
    b64 = base64.b64encode(payload).decode()
    out = tmp_path / "deep" / "nested" / "a.png"
    p = m.save_b64_image(b64, out)
    assert p.exists() and p.read_bytes() == payload   # 自动建父目录


def test_image_module_has_no_dead_code_after_return():
    """generate_many 的 return 之后不应再出现 4 空格缩进的孤儿语句。"""
    import inspect
    src = inspect.getsource(m.generate_many)
    lines = src.splitlines()
    after = [ln for ln in lines if ln.startswith("    ") and ln.strip()]
    # 只允许函数体本身；出现 return 之后的缩进语句即为死代码
    assert after, "generate_many 源码解析异常"
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == "return result")
    tail = [ln for ln in lines[idx + 1:] if ln.strip()]
    assert tail == [], f"return 之后还有语句（死代码）: {tail[:3]}"


# ---------------------------------------------------------------------------
# 1. 瞬时故障分类
# ---------------------------------------------------------------------------
def test_mk_error_classifies_by_status():
    assert isinstance(m._mk_error("ch", 429, "rate limit"), m.TransientImageError)
    assert isinstance(m._mk_error("ch", 500, "boom"), m.TransientImageError)
    assert isinstance(m._mk_error("ch", 503, "unavailable"), m.TransientImageError)
    assert not isinstance(m._mk_error("ch", 400, "bad"), m.TransientImageError)
    assert not isinstance(m._mk_error("ch", 401, "unauthorized"), m.TransientImageError)
    assert not isinstance(m._mk_error("ch", 403, "forbidden"), m.TransientImageError)


def test_is_transient_on_requests_exceptions():
    import requests
    assert m.is_transient_error(requests.Timeout("t"))
    assert m.is_transient_error(requests.ConnectionError("c"))
    # SSL 继承 ConnectionError，但证书问题重试一万次也一样 → 不可重试
    assert not m.is_transient_error(requests.exceptions.SSLError("bad cert"))


def test_is_transient_falls_back_to_message_signature():
    """无状态码的自建 ImageError（如空 data）靠文案指纹判定。"""
    assert m.is_transient_error(m.ImageError("[ch] connect timeout"))
    assert m.is_transient_error(m.ImageError("[ch] 空响应 data: upstream 502"))
    assert not m.is_transient_error(m.ImageError("[ch] prompt 违规被拒"))


# ---------------------------------------------------------------------------
# 2. 同渠道原地重试 vs 换路
# ---------------------------------------------------------------------------
class _FakeGen:
    """按脚本依次返回结果；记录每次调用的渠道名。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def generate(self, req, channel=None):
        self.calls.append(channel.name)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if hasattr(item, "data"):      # 已是 response（用于造空 data 场景）
            return item
        return types.SimpleNamespace(data=[item])


def _ok_item(n: int = 60000):
    return types.SimpleNamespace(
        b64_json=base64.b64encode(b"x" * n).decode(), url=None)


def _channels(*names):
    return [types.SimpleNamespace(name=n) for n in names]


def _run_worker(monkeypatch, channels, script, jobs, force=False):
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda a: 0.0)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    q = Queue()
    for j in jobs:
        q.put(j)
    res: dict = {}
    m._generate_many_worker(_FakeGen(script), q, res, channels, force)
    return res


def _job(tmp_path, key="LGT01"):
    return m.ImageJob(
        id=key,
        req=types.SimpleNamespace(model="gpt-image-2", size="1080x1920",
                                  quality="low", n=1, prompt="p"),
        out_path=tmp_path / f"{key}.png",
    )


def test_transient_error_retries_on_same_channel(monkeypatch, tmp_path):
    """429 类故障必须原地重试，一次抖动不能烧掉三池配额。"""
    monkeypatch.setattr(m, "_MAX_TRANSIENT_RETRY", 2)
    job = _job(tmp_path)
    job._channel_idx = 0
    chs = _channels("A", "B")
    script = [m.TransientImageError("[A] HTTP 429"),
              m.TransientImageError("[A] HTTP 429"),
              _ok_item()]
    res = _run_worker(monkeypatch, chs, script, [job])
    assert res[job.id] is not None
    assert job._transient == 2 and job._attempts == 3
    assert (tmp_path / "LGT01.png").exists()


def test_transient_retry_does_not_switch_channel(monkeypatch, tmp_path):
    """与上条同源，但直接断言「渠道没换」——这是本项的核心收益。"""
    monkeypatch.setattr(m, "_MAX_TRANSIENT_RETRY", 2)
    job = _job(tmp_path)
    job._channel_idx = 0
    gen = _FakeGen([m.TransientImageError("[A] 429"), _ok_item()])
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda a: 0.0)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    q = Queue()
    q.put(job)
    res: dict = {}
    m._generate_many_worker(gen, q, res, _channels("A", "B", "C"), False)
    assert res[job.id] is not None
    assert gen.calls == ["A", "A"], f"瞬时故障却换了渠道: {gen.calls}"


def test_fatal_error_switches_channel_immediately(monkeypatch, tmp_path):
    """参数/合规类失败重试无意义 → 立刻换路。"""
    job = _job(tmp_path)
    job._channel_idx = 0
    gen = _FakeGen([m.ImageError("[A] HTTP 400 invalid"), _ok_item()])
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda a: 0.0)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    q = Queue()
    q.put(job)
    res: dict = {}
    m._generate_many_worker(gen, q, res, _channels("A", "B"), False)
    assert res[job.id] is not None
    assert gen.calls == ["A", "B"]      # A 只打了一次
    assert job._transient == 0


def test_transient_exhausted_then_switch(monkeypatch, tmp_path):
    """原地重试耗尽后才换路，不是无限重试。"""
    monkeypatch.setattr(m, "_MAX_TRANSIENT_RETRY", 1)
    job = _job(tmp_path)
    job._channel_idx = 0
    gen = _FakeGen([m.TransientImageError("[A] 429"),
                    m.TransientImageError("[A] 429"),
                    _ok_item()])
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda a: 0.0)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    q = Queue()
    q.put(job)
    res: dict = {}
    m._generate_many_worker(gen, q, res, _channels("A", "B"), False)
    assert res[job.id] is not None
    assert gen.calls == ["A", "A", "B"]  # A 打满 1+1=2 次才换


def test_empty_data_treated_as_transient(monkeypatch, tmp_path):
    """空 data 多为上游瞬时抽风，按瞬时处理（原实现当致命 → 白换路）。"""
    monkeypatch.setattr(m, "_MAX_TRANSIENT_RETRY", 2)
    job = _job(tmp_path)
    job._channel_idx = 0
    gen = _FakeGen([types.SimpleNamespace(data=[]), _ok_item()])
    monkeypatch.setattr(m, "_throttle", lambda: None)
    monkeypatch.setattr(m, "_backoff", lambda a: 0.0)
    monkeypatch.setattr(m, "record_cost", lambda **kw: {})
    q = Queue()
    q.put(job)
    res: dict = {}
    m._generate_many_worker(gen, q, res, _channels("A", "B"), False)
    assert res[job.id] is not None
    assert gen.calls == ["A", "A"]


def test_all_channels_fail_records_error(monkeypatch, tmp_path):
    job = _job(tmp_path)
    job._channel_idx = 0
    script = [m.TransientImageError("[A] 429")] * 2 + [m.ImageError("[B] 400")]
    monkeypatch.setattr(m, "_MAX_TRANSIENT_RETRY", 1)
    res = _run_worker(monkeypatch, _channels("A", "B"), script, [job])
    assert res[job.id] is None
    assert "LGT01__error" in res and "B" in res["LGT01__error"]


def test_idempotent_skip_does_not_call_channel(tmp_path):
    """已存在 >50KB → 跳过且不占渠道（这条老行为不能被重试改动破坏）。"""
    out = tmp_path / "LGT01.png"
    out.write_bytes(b"x" * (60 * 1024))
    job = m.ImageJob(id="LGT01", req=types.SimpleNamespace(), out_path=out)
    gen = _FakeGen([_ok_item()])
    q = Queue()
    q.put(job)
    res: dict = {}
    m._generate_many_worker(gen, q, res, _channels("A"), False)
    assert res["LGT01"] == str(out)
    assert gen.calls == []


# ---------------------------------------------------------------------------
# 3. cost 台账
# ---------------------------------------------------------------------------
def test_record_cost_appends_and_summarizes(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_COST_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(m, "_PRICES_PATH", tmp_path / "prices.json")
    j1 = _job(tmp_path, "LGT01")
    j1._attempts, j1._transient = 2, 1
    e = m.record_cost(job=j1, channel="aicost", n_bytes=123456)
    assert e["channel"] == "aicost" and e["bytes"] == 123456
    assert e["transient_retries"] == 1 and e["attempts"] == 2
    assert e["cost"] is None                    # 无价格表 → 不编造金额
    assert e["model"] == "gpt-image-2"

    j2 = _job(tmp_path, "LSM002")
    m.record_cost(job=j2, channel="qcyc", n_bytes=100)
    s = m.cost_summary()
    assert s["total_images"] == 2
    assert s["by_channel"] == {"aicost": 1, "qcyc": 1}
    assert s["by_episode"] == {"LGT": 1, "LSM": 1}
    assert s["unpriced"] == 2 and s["cost_total"] is None


def test_record_cost_prices_when_table_exists(monkeypatch, tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"gpt-image-2:low": 0.02}), encoding="utf-8")
    monkeypatch.setattr(m, "_COST_PATH", tmp_path / "cost.json")
    monkeypatch.setattr(m, "_PRICES_PATH", prices)
    m.record_cost(job=_job(tmp_path, "LGT01"), channel="aicost", n_bytes=1)
    m.record_cost(job=_job(tmp_path, "LGT02"), channel="aicost", n_bytes=1)
    s = m.cost_summary()
    assert s["cost_total"] == 0.04 and s["unpriced"] == 0


def test_cost_summary_since_filter(monkeypatch, tmp_path):
    path = tmp_path / "cost.json"
    rows = [{"ts": "2026-09-01T10:00:00", "job": "A1", "channel": "c",
             "model": "gpt-image-2", "quality": "low", "size": "1080x1920",
             "cost": None},
            {"ts": "2026-09-04T10:00:00", "job": "A2", "channel": "c",
             "model": "gpt-image-2", "quality": "low", "size": "1080x1920",
             "cost": None}]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(m, "_COST_PATH", path)
    monkeypatch.setattr(m, "_PRICES_PATH", tmp_path / "p.json")
    assert m.cost_summary()["total_images"] == 2
    assert m.cost_summary(since="2026-09-04")["total_images"] == 1


def test_record_cost_never_breaks_generation(monkeypatch, tmp_path):
    """台账写盘失败（磁盘满/权限）绝不能中断生图。"""
    monkeypatch.setattr(m, "_COST_PATH", tmp_path / "no" / "such" / "dir" / "c.json")
    monkeypatch.setattr(m, "STATE_DIR", tmp_path / "no" / "such")
    e = m.record_cost(job=_job(tmp_path, "LGT01"), channel="a", n_bytes=1)
    assert e["job"] == "LGT01"       # 仍返回条目，只是没落盘


# ---------------------------------------------------------------------------
# 4. 进度 / ETA
# ---------------------------------------------------------------------------
def test_progress_tick_and_eta():
    p = m._Progress(total=10)
    for _ in range(3):
        p.tick(ok=True)
    p.tick(ok=False)
    d, o, f, el, eta = p.snap()
    assert (d, o, f) == (4, 3, 1)
    assert el >= 0 and eta is not None and eta >= 0


def test_progress_eta_none_before_first_tick():
    p = m._Progress(total=5)
    d, o, f, el, eta = p.snap()
    assert d == 0 and eta is None      # 没有样本不该编造 ETA


def test_fmt_dur():
    assert m._fmt_dur(65) == "01:05"
    assert m._fmt_dur(3725) == "1:02:05"
    assert m._fmt_dur(-5) == "00:00"


# ---------------------------------------------------------------------------
# 5. prompt 预筛
# ---------------------------------------------------------------------------
def test_screen_prompt_blocks_empty_and_placeholder():
    from app.orchestrator import screen_prompt
    ok, reason, _ = screen_prompt("L01", "   ")
    assert not ok and "空" in reason
    ok, reason, _ = screen_prompt("L02", "")
    assert not ok
    ok, reason, _ = screen_prompt("L03", "TODO")
    assert not ok and "占位" in reason
    ok, reason, _ = screen_prompt("L04", "待补")
    assert not ok


def test_screen_prompt_blocks_too_short():
    from app.orchestrator import screen_prompt
    ok, reason, _ = screen_prompt("L01", "一个人")
    assert not ok and "过短" in reason


def test_screen_prompt_normalizes_whitespace():
    """分镜 md 里的换行必须压平，否则长度判定与去重都失真。"""
    from app.orchestrator import screen_prompt
    ok, reason, norm = screen_prompt("L01", "西汉戈壁黄昏，\n  电影感，写实风格")
    assert ok and "\n" not in norm and norm == "西汉戈壁黄昏， 电影感，写实风格"


def test_screen_prompt_truncates_overlong():
    from app.orchestrator import screen_prompt, PROMPT_MAX_CHARS
    ok, reason, norm = screen_prompt("L01", "西" * (PROMPT_MAX_CHARS + 200))
    assert ok and len(norm) == PROMPT_MAX_CHARS and "截断" in reason


def test_screen_prompt_passes_real_prompt():
    from app.orchestrator import screen_prompt
    ok, reason, _ = screen_prompt(
        "L01", "Wide shot, Han dynasty desert at dusk, cinematic, realistic")
    assert ok and reason == "OK"


# ---------------------------------------------------------------------------
# 6. build_image_jobs 预筛集成
# ---------------------------------------------------------------------------
@pytest.fixture
def screen_ep(tmp_path, tmp_episodes):
    from app.services import image as _unused  # noqa: F401
    # 必须带连字符：conftest 的会话级清扫按 f"{ns}-" 前缀匹配残留目录
    ep_id = f"imgscreen-{tmp_path.name}"
    d = _ep_dir(ep_id)
    d.mkdir(parents=True, exist_ok=True)
    spec = {
        "LGT01": {"en": "Wide shot, Han dynasty desert at dusk, cinematic"},
        "LGT02": {"en": "   "},                    # 空 → 拦
        "LGT03": {"en": "Close"},                  # 过短 → 拦
        "LGT04": {"en": "TODO"},                   # 占位符 → 拦
        "LGT05": {"en": "Medium shot, emperor in palace hall, dramatic light"},
        "_meta": {"note": "下划线开头不参与"},
    }
    (d / "image_prompts.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    yield ep_id
    # 无清理：目录在 tmp_episodes 根下，pytest 回收 tmp_path 即可


def test_build_image_jobs_blocks_bad_prompts(screen_ep, capsys):
    from app.orchestrator import build_image_jobs
    jobs = build_image_jobs(screen_ep, prescreen=True)
    ids = [j.id for j in jobs]
    assert ids == ["LGT01", "LGT05"], f"未拦住坏 prompt: {ids}"
    out = capsys.readouterr().out
    assert "拦截" in out and "LGT02" in out and "LGT03" in out


def test_build_image_jobs_without_prescreen_keeps_legacy(screen_ep):
    """prescreen=False 时必须回到老行为（5 条全建），保证向后兼容。"""
    from app.orchestrator import build_image_jobs
    jobs = build_image_jobs(screen_ep, prescreen=False)
    assert len(jobs) == 5


def test_build_image_jobs_strict_mode_raises(screen_ep, monkeypatch):
    from app.orchestrator import build_image_jobs
    monkeypatch.setenv("HV_PROMPT_PRESCREEN_STRICT", "1")
    with pytest.raises(RuntimeError, match="未通过预筛"):
        build_image_jobs(screen_ep, prescreen=True)


def test_compose_prompt_is_single_source(screen_ep):
    """拼接口径唯一：预筛与建任务必须产出完全相同的字符串。"""
    from app.orchestrator import _compose_prompt, _image_prompt_parts
    s, n = _image_prompt_parts()
    assert _compose_prompt("abc", s, n) == f"abc, {s}. {n}".strip(", .") + "."


def test_screen_image_prompts_reports_duplicates(tmp_path, tmp_episodes):
    """同 prompt 生两张 = 白烧一倍钱，预筛要能提前指出来。"""
    from app.orchestrator import screen_image_prompts
    ep_id = f"imgdup-{tmp_path.name}"
    d = _ep_dir(ep_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "image_prompts.json").write_text(json.dumps({
        "L01": {"en": "Wide shot, Han dynasty desert at dusk, cinematic"},
        "L02": {"en": "Wide shot, Han dynasty desert at dusk, cinematic"},
    }, ensure_ascii=False), encoding="utf-8")
    try:
        rep = screen_image_prompts(ep_id)
        assert rep["passed"] == ["L01", "L02"]
        assert rep["dup"] == [("L01", "L02")]
        assert rep["blocked"] == []
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# H 批（2026-09-05）：config.image.primary 必须真被读取
# ---------------------------------------------------------------------------
def test_validate_image_params_flags_illegal_values():
    """subrouter 对非法 size/quality 是**静默忽略**——照收钱照生成，只按它
    自己的默认值出图。这是唯一能在花钱之前拦住的口径。"""
    assert m.validate_image_params("1080x1920", "low", "gpt-image-2") == []
    assert m.validate_image_params("1024x1536", "high") == []

    bad = m.validate_image_params("9999x9999", "ultra", "")
    assert any("size" in b for b in bad), f"非法 size 未报: {bad}"
    assert any("quality" in b for b in bad), f"非法 quality 未报: {bad}"
    assert any("model" in b for b in bad), f"空 model 未报: {bad}"


def _fake_settings(size: str, quality: str, strict: bool):
    """最小 settings 替身：只含 build_image_jobs 会读的字段。"""
    return types.SimpleNamespace(
        image=types.SimpleNamespace(
            primary=types.SimpleNamespace(
                model="gpt-image-2", engine="gpt-image-2",
                size=size, quality=quality,
                strict_param_validation=strict)),
        get_profile=lambda: types.SimpleNamespace(
            image=types.SimpleNamespace(
                engine="gpt-image-2", size="1080x1920", quality="low",
                style_suffix="", negative_clause="")),
    )


def test_strict_param_validation_decides_before_spending(tmp_path, monkeypatch,
                                                         tmp_episodes):
    """strict_param_validation=true 时非法参数必须在生图前拒绝。

    该开关此前全仓零消费者 —— 设成 true 也不会有任何事发生，非法 size 会
    真去生成、真计费，回来一堆比例不对的图（H 批让它真正生效）。
    """
    import app.config.config as C
    from app.orchestrator import build_image_jobs

    ep_id = f"smoke-imgstrict-{tmp_path.name}"
    d = _ep_dir(ep_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "image_prompts.json").write_text(
        json.dumps({"LGT01": {"en": "ancient chinese palace hall " * 12}}),
        encoding="utf-8")
    # 目录已落在 tmp_episodes 根下，pytest 回收 tmp_path 即可，无需 finally rmtree
    # 合法参数 + strict=true：必须放行，不能误伤正常生产
    monkeypatch.setattr(
        C, "get_settings", lambda: _fake_settings("1080x1920", "low", True))
    jobs = build_image_jobs(ep_id, prescreen=False)
    assert jobs and jobs[0].req.size == "1080x1920"

    # 非法参数 + strict=true：必须在花钱之前拒绝
    monkeypatch.setattr(
        C, "get_settings", lambda: _fake_settings("9999x9999", "ultra", True))
    with pytest.raises(RuntimeError, match="strict_param_validation"):
        build_image_jobs(ep_id, prescreen=False)

    # 非法参数 + strict=false（生产默认）：放行，不阻碍出片
    monkeypatch.setattr(
        C, "get_settings", lambda: _fake_settings("9999x9999", "ultra", False))
    jobs = build_image_jobs(ep_id, prescreen=False)
    assert jobs and jobs[0].req.size == "9999x9999"


def test_image_primary_is_the_source_of_engine_params(monkeypatch, tmp_path,
                                                      tmp_episodes):
    """engine 参数（model/size/quality）必须来自 config.image.primary。

    此前全部取自 profile.image，顶层 image 整段（含 strict_param_validation）
    零消费者——改了完全不生效。
    """
    import app.config.config as C
    from app.orchestrator import build_image_jobs

    ep_id = f"smoke-imgsrc-{tmp_path.name}"
    d = _ep_dir(ep_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "image_prompts.json").write_text(
        json.dumps({"LGT01": {"en": "ancient chinese palace hall " * 12}}),
        encoding="utf-8")
    monkeypatch.setattr(
        C, "get_settings", lambda: _fake_settings("1024x1536", "high", False))
    req = build_image_jobs(ep_id, prescreen=False)[0].req
    assert req.size == "1024x1536", f"size 没读 image.primary: {req.size}"
    assert req.quality == "high", f"quality 没读 image.primary: {req.quality}"
