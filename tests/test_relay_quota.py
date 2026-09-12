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


def test_stale_channels_still_pickable():
    """全不健康时 pick() 必须仍能返回一个渠道（强取最高分），不能抛。"""
    from app.managers.relay import ChannelHealth, RelayManager
    from app.config.config import Channel

    chs = [Channel(name=f"c{i}", base_url="http://x", api_key="k", model="m") for i in range(3)]
    m = RelayManager(channels=chs)
    for name, h in m._channels.items():
        # 32 次里只成功 5 次 → is_healthy 判 False（门槛 >0.5）
        h.history.clear()
        for i in range(32):
            h.history.append((i < 5, 1.0, 0.0))
    assert all(not h.is_healthy for h in m._channels.values())
    assert m.pick() is not None
