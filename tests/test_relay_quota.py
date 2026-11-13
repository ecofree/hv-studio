# -*- coding: utf-8 -*-
"""渠道健康度落盘：配额快照必须能 JSON 序列化。

2026-09-07 审计发现的真 bug：`hv relay sync` **100% 崩**
    TypeError: Object of type datetime is not JSON serializable
根因 `QuotaSnapshot.fetched_at` 是 datetime，而 `to_dict()` 用的是
`model_dump()`（保留 datetime 对象），不是 `model_dump(mode="json")`。

后果不止命令崩：崩溃点在 `write_text` 之前 → 配额永远刷不进
`state/relay_health.json` → `score()` 里那条 -500 配额惩罚永不生效 →
**渠道没钱时调度器不会降权**，只能靠一次次失败慢慢学。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.shared


import json
from datetime import datetime


def _health_with_quota():
    from app.managers.relay import ChannelHealth
    from app.config.config import Channel
    from app.models.relay import QuotaSnapshot

    ch = Channel(name="probe", base_url="http://127.0.0.1:1", api_key="x", model="m")
    h = ChannelHealth(ch)
    h.last_quota = QuotaSnapshot(
        has_payment_method=True,
        soft_limit_usd=12.5,
        hard_limit_usd=20.0,
        system_hard_limit_usd=20.0,
        access_until=2000000000,
        channel="probe",
    )
    h.last_quota_fetch = datetime(2026, 9, 7, 12, 0, 0)
    return h


def test_to_dict_is_json_serializable():
    """这就是当初崩的那一行：json.dumps(to_dict())。"""
    d = _health_with_quota().to_dict()
    s = json.dumps(d, ensure_ascii=False, indent=2)     # 不抛即通过
    assert "probe" in s


def test_quota_fetched_at_survives_roundtrip():
    """datetime 要变成字符串落盘，且能反序列化回 datetime。"""
    h = _health_with_quota()
    d = h.to_dict()
    assert isinstance(d["last_quota"]["fetched_at"], str), \
        "fetched_at 必须是字符串，datetime 会让 json.dumps 炸"

    from app.managers.relay import ChannelHealth
    from app.config.config import Channel

    back = ChannelHealth.from_dict(Channel(name="probe", base_url="b", api_key="k", model="m"), d)
    assert isinstance(back.last_quota_fetch, datetime)
    assert back.last_quota_fetch == datetime(2026, 9, 7, 12, 0, 0)
    assert back.last_quota.soft_limit_usd == 12.5


def test_snapshot_does_not_crash_with_quota():
    """`hv relay status` 打印 snapshot()，有配额时也不能炸。"""
    snap = {
        "probe": {
            "score": 100.0,
            "healthy": True,
            "n_history": 0,
            "ok_rate": 1.0,
            "last_quota_usd": _health_with_quota().last_quota.soft_limit_usd,
        }
    }
    assert json.dumps(snap, ensure_ascii=False)


def test_recently_unhealthy_channels_still_pickable(tmp_path, monkeypatch):
    """全不健康时 pick() 必须仍能返回一个渠道（强取最高分），不能抛。"""
    from app.managers.relay import ChannelHealth, RelayManager
    from app.config.config import Channel
    from app.managers import relay
    import time
    monkeypatch.setattr(relay, 'HEALTH_PATH', tmp_path/'health.json')

    chs = [Channel(name=f"c{i}", base_url="http://x", api_key="k", model="m") for i in range(3)]
    m = RelayManager(channels=chs)
    for name, h in m._channels.items():
        # 32 次里只成功 5 次 → is_healthy 判 False（门槛 >0.5）
        h.history.clear()
        for i in range(32):
            h.history.append((i < 5, 1.0, time.time()))
    assert all(not h.is_healthy for h in m._channels.values())
    assert m.pick() is not None


# ---------------------------------------------------------------------------
# 渠道评分权重（2026-09-23 修正）
# ---------------------------------------------------------------------------
def _health(hist, quota_usd=None):
    """造一个带历史的 ChannelHealth。"""
    from app.managers.relay import ChannelHealth
    h = ChannelHealth(channel="x")
    for ok, lat, ts in hist:
        h.history.append((ok, lat, ts))
    if quota_usd is not None:
        from app.models.relay import QuotaSnapshot
        h.last_quota = QuotaSnapshot(
            has_payment_method=True, soft_limit_usd=quota_usd,
            hard_limit_usd=quota_usd, system_hard_limit_usd=quota_usd,
            access_until=0, channel="x")
    return h


def test_unavailable_channel_ranks_below_slow_reliable_one():
    """**核心契约**：全失败渠道必须排在「慢但可靠」渠道之后。

    这是 2026-09-23 实测的真实故障：旧权重 (err×60, p50×2) 下
      cxk  87.5%成功/p50 54s → −15.5
      qcyc 全失败 /p50  3s → 34.0   ← 不可用的反而优先
    且错误率饱和在 1.0 后不再恶化 → 永不自愈，每张图都先浪费一次失败。
    约束：全失败渠道的分数必须 < 慢但可靠渠道。
    """
    import time
    now = time.time()
    slow_reliable = _health([(True, 54, now - 60)] * 28
                            + [(False, 15, now - 3600)] * 4)
    all_fail_fast = _health([(False, 3, now - 60)] * 5)
    assert all_fail_fast.score() < slow_reliable.score(), (
        f"不可用渠道({all_fail_fast.score():.1f}) 竟排在慢但可靠渠道"
        f"({slow_reliable.score():.1f}) 之前 —— 权重回归到了 err 惩罚不足的旧值")
    assert all_fail_fast.score() < 0 < slow_reliable.score()


def test_unknown_channel_gets_one_chance():
    """无历史渠道给基准分 100 —— 保留「新渠道被尝试一次」的机会，
    否则恢复一个渠道后永远轮不到它。"""
    assert _health([]).score() == 100.0


def test_latency_alone_does_not_bury_reliable_channel():
    """慢本身不该是致命的：100% 成功但 54s 的渠道必须仍是正分。"""
    import time
    h = _health([(True, 54, time.time() - 60)] * 16)
    assert h.score() > 0, "纯延迟就把可靠渠道打成负分 —— 延迟权重过重"


def test_recent_failure_still_hard_penalised():
    """30s 内的失败仍是一票否决级惩罚（保持原有语义）。"""
    import time
    h = _health([(True, 20, time.time() - 60)] * 10
                + [(False, 5, time.time() - 5)])
    assert h.score() < -500, "近期失败没有硬降权"


def test_quota_penalty_still_applies():
    """配额不足仍扣 500（保持原有语义）。"""
    import time
    rich = _health([(True, 20, time.time() - 60)] * 10, quota_usd=50.0)
    poor = _health([(True, 20, time.time() - 60)] * 10, quota_usd=0.5)
    assert poor.score() == rich.score() - 500
