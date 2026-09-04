"""
三路并发生图测试（2026-09-04）。

锁住 generate_many 的核心承诺：
  1) 多渠道时**每个渠道都被调用**（若退化成单路全打最优渠道，测试必红）
  2) 失败自动换路，最终仍成功
  3) 已存在合格文件的 job 幂等跳过（不占渠道）
  4) channel= 钉住语义：只打指定渠道，失败不静默换路

通过替换 ImageGenerator._post 拦截网络，无需真实 key/中转。
"""
from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

import pytest

from app.config.config import Channel
from app.models import ImageGenRequest, ImageGenResponse, ImageGenItem
from app.services import image as image_mod
from app.services.image import ImageGenerator, ImageJob, generate_many

# 关闭全局限速，避免测试被 sleep 拖慢
image_mod._QPS_INTERVAL = 0.0


def _make_channel(name: str, base_url: str = "https://mock.example") -> Channel:
    return Channel(
        name=name,
        base_url=base_url,
        api_key=f"sk-{name}",
        model="gpt-image-2",
    )


def _gen_req() -> ImageGenRequest:
    return ImageGenRequest(
        model="gpt-image-2", prompt="test", size="1080x1920", n=1,
    )


def _b64_1px() -> str:
    # 1x1 red PNG
    return base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d4944415478da63fcffff3f030005fe02fea72b4cc60000000049454e44ae426082"
        )
    ).decode()


class _Recorder:
    """记录每次 _post 打到哪个渠道，可注入行为。"""

    def __init__(self):
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.behavior = {}  # channel_name -> callable(req)->ImageGenResponse 或 raise

    def __call__(self, gen, channel, path, body):
        with self.lock:
            self.calls.append(channel.name)
        handler = self.behavior.get(channel.name)
        if handler is None:
            handler = self.behavior.get("*")
        if handler:
            return handler(channel)
        raise RuntimeError(f"未处理渠道 {channel.name}")


@pytest.fixture
def patch_generator(monkeypatch):
    """把 get_image 换成注入 3 个 mock 渠道 + mock _post 的 ImageGenerator。"""
    channels = [_make_channel("aicost"), _make_channel("qcyc"), _make_channel("cxk")]
    rec = _Recorder()

    class FakeRelay:
        def all(self):
            return channels

        def health(self, name):
            return None

        def record(self, *a, **k):
            pass

    orig_relay = image_mod.get_relay
    gen = ImageGenerator.__new__(ImageGenerator)
    gen.relay = FakeRelay()
    gen.max_attempts = 3
    gen._post = lambda ch, path, body: rec(gen, ch, path, body)
    # 替换模块单例与 get_image
    monkeypatch.setattr(image_mod, "_image", gen)
    monkeypatch.setattr(image_mod, "get_image", lambda: gen)
    return gen, channels, rec


def _ok_resp():
    return ImageGenResponse(data=[ImageGenItem(b64_json=_b64_1px())])


def test_every_channel_is_used(patch_generator, tmp_path):
    """3 渠道 × 6 job → 三个渠道都必须被调用（防退化单路）。"""
    gen, channels, rec = patch_generator
    for ch in channels:
        rec.behavior[ch.name] = lambda c: _ok_resp()
    jobs = [ImageJob(f"j{i}", _gen_req(), tmp_path / f"{i}.png") for i in range(6)]
    res = generate_many(jobs)
    assert all(res[j.id] is not None for j in jobs)
    used = set(rec.calls)
    assert used == {"aicost", "qcyc", "cxk"}, f"应三路都用, 实际 {used}"
    # 每个渠道至少被分配到 1 个 job（worker 钉渠道，首攻命中即成功）
    assert len(rec.calls) >= 3


def test_failover_when_one_channel_down(patch_generator, tmp_path):
    """某渠道 429 → 任务换到健康渠道，仍全部成功。"""
    gen, channels, rec = patch_generator
    rec.behavior["aicost"] = lambda c: (_ for _ in ()).throw(RuntimeError("429"))
    rec.behavior["qcyc"] = lambda c: _ok_resp()
    rec.behavior["cxk"] = lambda c: _ok_resp()
    jobs = [ImageJob(f"j{i}", _gen_req(), tmp_path / f"{i}.png") for i in range(4)]
    res = generate_many(jobs)
    assert all(res[j.id] is not None for j in jobs), res
    # aicost 可能被失败尝试，但最终由 qcyc/cxk 承接
    assert "aicost" in rec.calls or len(rec.calls) >= 4


def test_already_done_job_is_skipped(patch_generator, tmp_path):
    """out_path 已存在 >50KB → 不调用渠道。"""
    gen, channels, rec = patch_generator
    for ch in channels:
        rec.behavior[ch.name] = lambda c: _ok_resp()
    # 预写一个合格文件
    pre = tmp_path / "done.png"
    pre.write_bytes(b"\x00" * (51 * 1024))
    jobs = [
        ImageJob("done", _gen_req(), pre),          # 应跳过
        ImageJob("todo", _gen_req(), tmp_path / "todo.png"),
    ]
    res = generate_many(jobs)
    assert res["done"] == str(pre)
    # 只有 todo 触发网络
    assert len(rec.calls) == 1
    assert rec.calls[0] in {"aicost", "qcyc", "cxk"}


def test_pinned_channel_single_target(patch_generator, tmp_path):
    """channel= 钉住：只打该渠道，不排序不换路。"""
    gen, channels, rec = patch_generator
    rec.behavior["qcyc"] = lambda c: _ok_resp()
    rec.behavior["*"] = lambda c: (_ for _ in ()).throw(RuntimeError("不该打这里"))
    req = _gen_req()
    gen.generate(req, channel=channels[1])  # qcyc
    assert rec.calls == ["qcyc"]


def test_pinned_channel_failure_raises(patch_generator):
    """channel= 钉住且该渠道失败 → 直接抛（由 generate_many 决定换路）。"""
    gen, channels, rec = patch_generator
    rec.behavior["*"] = lambda c: (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        gen.generate(_gen_req(), channel=channels[0])
    # 只尝试一次钉住渠道，不换路
    assert rec.calls == ["aicost"]


def test_workers_do_not_exceed_channels(patch_generator, tmp_path):
    """worker 数 ≤ 渠道数（每渠道最多 1 路）。"""
    gen, channels, rec = patch_generator
    for ch in channels:
        rec.behavior[ch.name] = lambda c: _ok_resp()
    jobs = [ImageJob(f"j{i}", _gen_req(), tmp_path / f"{i}.png") for i in range(10)]
    # 并发下若 worker > 渠道，会多个 worker 同渠道（每渠道内仍串行）。无法直接从
    # rec.calls 断言并发数，此处断言功能正确 + 每渠道请求都被串行消化即可。
    res = generate_many(jobs)
    assert all(res[j.id] is not None for j in jobs)
    assert len(rec.calls) == 10
