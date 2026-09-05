# -*- coding: utf-8 -*-
"""共享夹具：可移植的微合成 episode，供首产/缓存失效/编排测试使用。

设计：以最小内存 footprint（1080×1920 @2x，静态 2s 单镜）驱动真实
compose 编码路径，避开低 pagefile 沙箱在 3344×1882 大图上 OOM 的问题。
episode 写入真实 episodes/{id}.json（compose 硬绑定该目录），测试后清理。
"""
from __future__ import annotations

import json
import os
import shutil
import wave

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODES = ROOT / "episodes"


# D5：真实编解码用例需要本机有 ffmpeg/ffprobe。没装时不该报失败——那不是代码
# 有问题，是环境缺件。给这些用例打上这个 mark，缺件时 skip 而不是 red。
def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


NEEDS_FFMPEG = pytest.mark.skipif(
    not _has_ffmpeg(), reason="需要本机 ffmpeg/ffprobe（未安装 → 跳过真实编解码用例）")


def pytest_collection_modifyitems(session, config, items):
    """收集完钉一条：AST 里数到的用例定义数 必须 ≤ 实际收集数。

    收集数 < 定义数 = 有用例「定义了却没跑」。最常见原因是同一文件里后定义
    覆盖了前定义——2026-09-05 实测就这么丢了 4 条 NVENC 回归用例，而
    "114 passed" 看着完全正常。**这类问题不报错、不变红，只能靠这个口径发现。**

    收集数 > 定义数 属正常（parametrize 会把一个定义展开成多条），放行。
    """
    # 只做部分收集时（-k 过滤 / 命令行指定了具体路径）本守卫不适用
    if getattr(config.option, "keyword", "") or config.args:
        return
    try:
        from _case_stats import count_defined_cases
    except ImportError:  # rootdir 未把 tests/ 加进 sys.path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _case_stats import count_defined_cases

    defined = count_defined_cases()
    collected = len(items)
    if collected < defined:
        raise pytest.Exit(
            f"\n[测试基建] 用例定义数 {defined} > 实际收集数 {collected}，"
            f"少了 {defined - collected} 条。\n"
            "最常见原因：同一测试文件里出现了同名的 test 用例，后定义静默覆盖前定义\n"
            "（被覆盖的那份永不执行）。请检查 tests/ 下是否有重复定义。",
            returncode=3)


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
                   "sbguard", "resync", "imgscreen", "imgdup")
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
