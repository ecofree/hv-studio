"""
subrouter.ai 渠道池健康度管理。

设计：
  - 滚动窗口：每个渠道保留最近 N=32 次请求的 (ok, latency_s)
  - 选渠道：综合 error_rate + p50 latency + 上次失败时间
  - 配额：定期拉取（默认 1 次/小时）每个 key 的 soft_limit_usd
  - 状态持久化到 state/relay_health.json（重启可恢复）

为什么不用轮询？
  纯轮询在某个 key 临时 429 时会持续打到死 key。
  加权后会自动让出，但保守起见仍做最少 1 次/分钟轮询。
"""
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

import requests

from app.config.config import Channel, get_settings
from app.models import QuotaSnapshot

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = ROOT / "state"
HEALTH_PATH = STATE_DIR / "relay_health.json"

WINDOW_SIZE = 32
QUOTA_TTL = timedelta(hours=1)
REQUEST_COOLDOWN_AFTER_FAIL = timedelta(seconds=30)


class ChannelHealth:
    """单个渠道的滚动窗口 + 配额缓存。"""
    def __init__(self, channel: Channel):
        self.channel = channel
        self.history: deque[tuple[bool, float, float]] = deque(maxlen=WINDOW_SIZE)
        # (ok, latency_s, ts_epoch)
        self.last_quota: QuotaSnapshot | None = None
        self.last_quota_fetch: datetime | None = None

    # ---- 记录一次请求 ----
    def record(self, ok: bool, latency_s: float) -> None:
        self.history.append((ok, latency_s, time.time()))

    # ---- 评分 ----
    def score(self) -> float:
        """综合得分，**越高越优先**。无历史时给基准分 100。"""
        if not self.history:
            return 100.0
        n = len(self.history)
        ok = sum(1 for h in self.history if h[0])
        err_rate = 1 - ok / n
        p50 = sorted(h[1] for h in self.history)[n // 2]
        # 最近一次失败时间：失败后 30s 内大幅降权
        last_fail = max((h[2] for h in self.history if not h[0]), default=0)
        cooldown_penalty = 0
        if last_fail and time.time() - last_fail < REQUEST_COOLDOWN_AFTER_FAIL.total_seconds():
            cooldown_penalty = -1000
        # 配额不足惩罚（剩余 < $1）
        quota_penalty = 0
        if self.last_quota and self.last_quota.soft_limit_usd < 1.0:
            quota_penalty = -500
        return 100 - err_rate * 60 - p50 * 2 + cooldown_penalty + quota_penalty

    @property
    def is_healthy(self) -> bool:
        """健康度门槛：error rate < 50% 且 p50 < 60s"""
        if len(self.history) < 3:
            return True
        n = len(self.history)
        ok = sum(1 for h in self.history if h[0])
        return (ok / n) > 0.5

    def to_dict(self) -> dict:
        return {
            "channel_name": self.channel.name,
            "history": list(self.history),
            # ⚠️ 必须 mode="json"：QuotaSnapshot.fetched_at 是 datetime，
            # 默认 model_dump() 会**保留 datetime 对象**而非转成字符串，
            # 下游 json.dumps 直接抛 TypeError: Object of type datetime is not
            # JSON serializable（2026-09-07 审计发现，hv relay sync 因此 100% 崩）。
            # 后果不止是命令崩：配额刷不出来 → score() 的 -500 配额惩罚永不生效
            # → 渠道没钱时调度器不会降权。model_dump_json() 也行，但这里要 dict。
            "last_quota": (self.last_quota.model_dump(mode="json")
                           if self.last_quota else None),
            "last_quota_fetch": self.last_quota_fetch.isoformat() if self.last_quota_fetch else None,
        }

    @classmethod
    def from_dict(cls, channel: Channel, d: dict) -> "ChannelHealth":
        h = cls(channel)
        h.history = deque(d.get("history", []), maxlen=WINDOW_SIZE)
        if d.get("last_quota"):
            h.last_quota = QuotaSnapshot.model_validate(d["last_quota"])
        if d.get("last_quota_fetch"):
            h.last_quota_fetch = datetime.fromisoformat(d["last_quota_fetch"])
        return h


class RelayManager:
    """渠道健康度与调度。

    线程安全（2026-09-04）：三路并发生图下多个 worker 会同时
    ``record()`` / ``_save()`` / ``pick()``，锁必须加在**本类内部**——
    若指望调用方加锁，各 worker 会各持各的锁，等于没加。
    健康度 JSON 因此曾面临 lost update 与写竞争。
    """

    def __init__(self, channels: Iterable[Channel] | None = None):
        import threading
        self._lock = threading.Lock()
        settings = get_settings()
        chs = [c for c in (list(channels) if channels is not None else settings.channels)
               if c.enabled]
        if not chs:
            raise RuntimeError("没有可用渠道，请检查 .env 中 *_gpt-image-2 密钥")
        self._channels: dict[str, ChannelHealth] = {c.name: ChannelHealth(c) for c in chs}
        self._slots = {c.name: threading.BoundedSemaphore(c.max_concurrency) for c in chs}
        self._load()

    def request_slot(self, name: str):
        """All entry points and processes share one supplier lane.

        Conservative cap of one remains safe during configuration changes.
        Different suppliers still run concurrently.
        """
        from contextlib import contextmanager
        from app.services.process_lock import file_lock
        from app.services.provenance import fingerprint
        import time

        @contextmanager
        def acquire():
            deadline = time.monotonic() + 900
            while True:
                lock = file_lock(STATE_DIR/'provider-slots'/(fingerprint(name.casefold())+'.lock'))
                try:
                    lock.__enter__()
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline: raise RuntimeError('supplier lane wait deadline exceeded')
                    time.sleep(.05)
            try: yield
            finally: lock.__exit__(None, None, None)
        return acquire()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not HEALTH_PATH.exists():
            return
        try:
            data = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        for name, h in self._channels.items():
            if name in data:
                stored = ChannelHealth.from_dict(h.channel, data[name])
                self._channels[name] = stored

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    # ---- 选渠道 ----
    def pick(self) -> Channel:
        """按 score() 排序的健康渠道中选最优；全不健康则强行取 score 最高的。"""
        with self._lock:
            ranked = sorted(self._channels.values(), key=lambda h: h.score(), reverse=True)
            for h in ranked:
                if h.is_healthy:
                    return h.channel
            return ranked[0].channel

    def all(self) -> list[Channel]:
        """全部渠道快照（三路并行用：每个 worker 领一个）。"""
        with self._lock:
            return [h.channel for h in self._channels.values()]

    def health(self, name: str) -> ChannelHealth | None:
        with self._lock:
            return self._channels.get(name)

    def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "score": h.score(),
                "models": h.channel.models or [h.channel.model],
                "max_concurrency": h.channel.max_concurrency,
                "healthy": h.is_healthy,
                "n_history": len(h.history),
                "ok_rate": (sum(1 for x in h.history if x[0]) / len(h.history))
                           if h.history else 1.0,
                "last_quota_usd": h.last_quota.soft_limit_usd if h.last_quota else None,
            }
            for name, h in self._channels.items()
        }

    # ---- 配额同步 ----
    def sync_quota(self, force: bool = False) -> None:
        """拉取每个 key 的配额。TTL 1 小时，force=True 强制刷新。"""
        now = datetime.now()
        for h in self._channels.values():
            if not force and h.last_quota_fetch \
                    and (now - h.last_quota_fetch) < QUOTA_TTL:
                continue
            try:
                r = requests.get(
                    f"{h.channel.base_url}/v1/dashboard/billing/subscription",
                    headers=h.channel.auth_header, timeout=15,
                )
                if r.status_code == 200:
                    d = r.json()
                    h.last_quota = QuotaSnapshot(
                        has_payment_method=d.get("has_payment_method", False),
                        soft_limit_usd=float(d.get("soft_limit_usd", 0)),
                        hard_limit_usd=float(d.get("hard_limit_usd", 0)),
                        system_hard_limit_usd=float(d.get("system_hard_limit_usd", 0)),
                        access_until=int(d.get("access_until", 0)),
                        channel=h.channel.name,
                    )
                    h.last_quota_fetch = now
            except Exception:
                # 配额拉取失败不影响调度
                pass
        self._save()

    # ---- 记录结果（外部调用方使用）----
    def record(self, channel_name: str, ok: bool, latency_s: float) -> None:
        # 读 h 与 h.record 都要在锁内：ChannelHealth.record 内部改 history，
        # 并发下会 lost update（三路并发生图必现）。
        with self._lock:
            h = self._channels.get(channel_name)
            if h:
                h.record(ok, latency_s)
                self._save_locked()

    def _save_locked(self) -> None:
        """内部用：调用方必须已持锁。"""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {name: h.to_dict() for name, h in self._channels.items()}
        from app.services.provenance import atomic_json
        atomic_json(HEALTH_PATH, data)


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_relay: RelayManager | None = None


def get_relay() -> RelayManager:
    global _relay
    if _relay is None:
        _relay = RelayManager()
    return _relay


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "sync", "pick"])
    args = ap.parse_args()

    r = get_relay()
    if args.action == "status":
        print(json.dumps(r.snapshot(), ensure_ascii=False, indent=2))
    elif args.action == "sync":
        r.sync_quota(force=True)
        print(json.dumps(r.snapshot(), ensure_ascii=False, indent=2))
    elif args.action == "pick":
        c = r.pick()
        print(f"picked: {c.name}  base={c.base_url}")


if __name__ == "__main__":
    main()
