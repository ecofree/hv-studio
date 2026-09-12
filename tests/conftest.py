# -*- coding: utf-8 -*-
"""Offline fixtures. Episode writes use tmp_episodes; configured backends are unchanged."""
from __future__ import annotations

import json
import os
import shutil
import wave

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Offline regressions must never call a billable provider, even on silence."""
    import socket
    original = socket.socket.connect
    def connect(sock, address):
        # Windows asyncio implements socketpair using one local connect. Permit
        # only the stdlib function itself, never arbitrary localhost providers.
        import sys
        pair = getattr(socket, "_fallback_socketpair", None)
        if pair is not None and sys._getframe(1).f_code is pair.__code__:
            return original(sock, address)
        raise RuntimeError("external network disabled in offline tests")
    monkeypatch.setattr(socket.socket, "connect", connect)
    from app.services import cosyvoice
    def no_key():
        raise RuntimeError("provider credentials disabled in offline tests")
    monkeypatch.setattr(cosyvoice, "_dashscope_key", no_key)


# D5：真实编解码用例需要本机有 ffmpeg/ffprobe。没装时不该报失败——那不是代码
# 有问题，是环境缺件。给这些用例打上这个 mark，缺件时 skip 而不是 red。
def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


NEEDS_FFMPEG = pytest.mark.skipif(
    not _has_ffmpeg(), reason="需要本机 ffmpeg/ffprobe（未安装 → 跳过真实编解码用例）")


def pytest_collection_modifyitems(session, config, items):
    """Reject ambiguous scope and duplicate declarations, not low test counts."""
    from _case_stats import defined_cases
    for path in {Path(item.path) for item in items}:
        for name, lines in defined_cases(path).items():
            if len(lines) > 1:
                raise pytest.UsageError(f"duplicate test {path.name}::{name}: {lines}")
    for item in items:
        scopes = {m.name for m in item.iter_markers()} & {'production','shared','legacy'}
        if len(scopes) != 1:
            raise pytest.UsageError(f"{item.nodeid}: choose one scope: production/shared/legacy")


def make_small_images(workdir: Path):
    """生成 540×960 源图 + 1080×1920 @2x（upscale() 会复用 @2x，避免大图 Lanczos）。"""
    from PIL import Image, ImageDraw
    img = workdir / "img"
    img.mkdir(parents=True, exist_ok=True)
    src_p = img / "sc.png"
    big_p = img / "sc@2x.png"
    src = Image.new("RGB", (540, 960), (20, 40, 80))
    d = ImageDraw.Draw(src)
    d.rectangle([40, 60, 500, 900], outline=(200, 180, 100), width=6)
    d.text((200, 450), "SMOKE", fill=(255, 255, 255))
    src.save(src_p)
    src.resize((1080, 1920), Image.LANCZOS).save(big_p)
    # 保证 @2x mtime >= 源图，upscale 走复用
    os.utime(big_p, (src_p.stat().st_mtime_ns / 1e9 + 1, src_p.stat().st_mtime_ns / 1e9 + 1))
    return src_p, big_p


def make_small_wav(workdir: Path, dur_s: float = 2.0) -> Path:
    audio = workdir / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    p = audio / "frame-000.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * dur_s))
    return p


def build_episode_dict(ep_id: str, img_abs: str, wav_abs: str, dur_ms: int) -> dict:
    """构造与交付 episode 同构的 dict（单镜 static，2s）。"""
    frame = {
        "id": "frame-000", "text": "冒烟测试合成", "narration_text": "冒烟测试合成",
        "audio_path": wav_abs, "audio_duration_ms": dur_ms,
        "char_align": [], "subtitle": [], "shot_ids": ["shot-000"],
    }
    shot = {
        "id": "shot-000", "frame_id": "frame-000", "index": 0, "type": "ken_burns",
        "is_hero": False, "narration": "", "shot_type": "medium",
        # D2: cast_refs / keywords / scene_ref 已从 Shot 删除，fixture 不再填
        "duration_ms": dur_ms,
        "audio_start_ms": 0, "audio_end_ms": dur_ms, "audio_segment_path": None,
        "prompt": "", "negative_prompt": "", "expression": "", "pose": "",
        "ref_pack_id": None, "ref_pack_paths": [], "image_path": img_abs,
        "subtitle_text": "冒烟测试合成", "face_sim": None, "face_sim_warn": False,
        "retry_count": 0, "last_error": None, "stage": {},
        "motion": {"type": "static", "intensity": 0.0,
                   "start_xy": [0.5, 0.5], "end_xy": [0.5, 0.5],
                   "start_zoom": 1.0, "end_zoom": 1.0, "easing": "linear",
                   "grade": None},
    }
    return {
        "id": ep_id, "title": ep_id, "episode_number": 1, "schema_version": "1.0",
        "status": "composed", "frames": [frame], "shots": {"shot-000": shot},
        "state": {}, "total_seconds": dur_ms / 1000,
    }




@pytest.fixture(autouse=True)
def _isolate_cost_ledger(tmp_path, monkeypatch):
    """把生图台账从 state/ 重定向到临时目录。

    否则跑一次测试就往生产台账里塞几十条 j0/j1/… 假记录 —— 那是拿来给三池
    对账的真数据文件（state/cost_ledger.json），被测试污染就失去意义了。
    （2026-09-04 实测：一次全量跑写入 84 条假记录。）
    """
    from app.services import image as _img
    monkeypatch.setattr(_img, "_COST_PATH", tmp_path / "cost_ledger_test.json")
    monkeypatch.setattr(_img, "_PRICES_PATH", tmp_path / "cost_prices_test.json")


# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_episodes(tmp_path, monkeypatch):
    """把整条链的 episodes 根重定向到 tmp_path（P1-6 根因修复）。

    背景：此前 episodes 根被 4 处 import 期常量写死，测试改不了，只能真写
    D:/hv/episodes/ 再靠「会话末按前缀通配删」兜底 —— 中断即残留、前缀撞车
    即误删。现已收口为 app.paths.episodes_dir()（调用期求值），monkeypatch
    这一个点就能让 managers / compose / orchestrator / shot_builder 全部改道。

    用法：需要落盘 episode 的测试带上本 fixture，然后照常用
    `episode_path(ep_id)` / `episodes_dir()` 取路径，拿到的就是 tmp 下的。
    真实 episodes/ 零写入，也就不需要任何删除兜底。
    """
    import app.paths as _paths
    ep_root = tmp_path / "episodes"
    ep_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "episodes_dir", lambda: ep_root)
    return ep_root


def make_reuse_source(ep_root: Path, src_ep: str = "hw-002",
                      key: str = "SC01") -> Path:
    """在 tmp episodes 根里造一张可复用占位底图，供 reuse_from 命中。

    shot_builder 只校验 reuse 图路径存在（不解码），所以一张 540×960 占位图
    足够——不需要把真实 hw-002 的 49 张图搬进 tmp。
    """
    d = ep_root / src_ep / "shots"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{key}.png"
    if not p.exists():
        from PIL import Image
        Image.new("RGB", (540, 960), (90, 70, 50)).save(p)
    return p


# Tests own only their pytest temporary directories. Never sweep historical episodes.


@pytest.fixture
def smoke_episode(tmp_path, tmp_episodes):
    """构建一个一次性微 episode（**写在 tmp 的 episodes 根下**，P1-6）。

    产出 (ep_id, img, big_img, dur_ms)。因为走 tmp_episodes 重定向，
    真实 D:/hv/episodes/ 一个字节都不写，测后无需任何清理。
    """
    src_p, big_p = make_small_images(tmp_path)
    wav = make_small_wav(tmp_path)
    with wave.open(str(wav), "rb") as w:
        dur_ms = int(w.getnframes() / float(w.getframerate()) * 1000)

    ep_id = f"smoke-{os.getpid()}"
    d = build_episode_dict(ep_id, str(src_p).replace("\\", "/"),
                           str(wav).replace("\\", "/"), dur_ms)
    ep_json = tmp_episodes / f"{ep_id}.json"
    ep_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    yield ep_id, src_p, big_p, dur_ms
    # 无清理：tmp_path 由 pytest 自行回收。
    # 此前这里有一段「通配删 EPISODES/smoke-*」的兜底——那正是 P1-6 的风险源
    # （前缀一旦与交付集相撞就是误删成片）。根改掉之后兜底也就不必存在了。
