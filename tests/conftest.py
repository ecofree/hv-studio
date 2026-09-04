# -*- coding: utf-8 -*-
"""共享夹具：可移植的微合成 episode，供首产/缓存失效/编排测试使用。

设计：以最小内存 footprint（1080×1920 @2x，静态 2s 单镜）驱动真实
compose 编码路径，避开低 pagefile 沙箱在 3344×1882 大图上 OOM 的问题。
episode 写入真实 episodes/{id}.json（compose 硬绑定该目录），测试后清理。
"""
from __future__ import annotations

import json
import os
import wave

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODES = ROOT / "episodes"


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
        "is_hero": False, "narration": "", "shot_type": "medium", "cast_refs": [],
        "keywords": [], "scene_ref": None, "duration_ms": dur_ms,
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


@pytest.fixture(autouse=True, scope="session")
def _clean_test_episode_residue():
    """会话结束清扫测试自建 episode 残留（防中断跑次永久残留）。

    各测试在真实 episodes/ 下建 {ns}-{pid} 集（compose 硬绑定该目录），teardown 用
    finally 精确删。但进程被杀/超时中断时 teardown 不跑，会在 episodes/ 留 json+目录。
    这里按已知测试命名空间前缀通配清扫（autouse session，结束时统一兜底）。
    ⚠️ 只清这些前缀，绝不匹配真实交付集（laogongtuan/hanwu-*/hw-* 等）。
    """
    yield
    _namespaces = ("smoke", "segtest", "imgcfg", "wavaud", "nors", "emptytest",
                   "sbguard")
    for jf in EPISODES.glob("*.json"):
        stem = jf.stem
        if any(stem == f"{ns}" or stem.startswith(f"{ns}-") for ns in _namespaces):
            try:
                jf.unlink()
            except Exception:
                pass
    for d in EPISODES.iterdir():
        if d.is_dir() and any(d.name.startswith(f"{ns}-") for ns in _namespaces):
            import shutil
            shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def smoke_episode(tmp_path):
    """构建一个一次性微 episode（写到真实 episodes/），产出 (ep_id, img, wav)。

    测试结束自动清理 json + build/out 目录。
    """
    src_p, big_p = make_small_images(tmp_path)
    wav = make_small_wav(tmp_path)
    with wave.open(str(wav), "rb") as w:
        dur_ms = int(w.getnframes() / float(w.getframerate()) * 1000)

    ep_id = f"smoke-{os.getpid()}"
    d = build_episode_dict(ep_id, str(src_p).replace("\\", "/"),
                           str(wav).replace("\\", "/"), dur_ms)
    ep_json = EPISODES / f"{ep_id}.json"
    ep_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    yield ep_id, src_p, big_p, dur_ms

    # cleanup：先精确删本 pid 集，再清扫历史 smoke-* 残留（防中断跑次永久残留）。
    # 注：单进程跑 pytest 时，残留只可能来自更早失败/被杀的跑次，可安全按通配清；
    # 若启用 xdist 多进程并行则须改回仅按 pid（避免误删他进程在用集）。
    try:
        ep_json.unlink()
    except Exception:
        pass
    ep_dir = EPISODES / ep_id
    if ep_dir.exists():
        import shutil
        shutil.rmtree(ep_dir, ignore_errors=True)
    # 历史残留：只清 smoke-*（测试自建命名空间），绝不动真实交付集
    for jf in EPISODES.glob("smoke-*.json"):
        try:
            jf.unlink()
        except Exception:
            pass
    for d in EPISODES.glob("smoke-*"):
        if d.is_dir():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
