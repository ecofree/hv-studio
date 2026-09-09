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
def _pin_tts_engine_for_tests(monkeypatch):
    """测试固定走 cosyvoice 引擎（生产配置可能已切到 qwen）。

    2026-09-09 二轮审计回归：config.yaml 的 engine 改成 qwen 后，凡是直接调
    produce() 的用例都会真去打 qwen（额度/网络）→ 12 例失败。测试桩（打桩
    _try_one_cosyvoice / synthesize_dashscope_whole）只在 cosyvoice 路径上，
    所以这里把引擎钉回 cosyvoice；需要测 qwen 的用例用 monkeypatch 显式改。
    """
    from app.config.config import get_settings
    v = get_settings().get_profile().voice
    monkeypatch.setattr(v, "engine", "cosyvoice")


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
# P1-6（2026-09-05）：测试 episodes 隔离 + 交付集硬保护
# ---------------------------------------------------------------------------
# 测试命名空间。会话末兜底清扫只认「{ns}-」这种带连字符的测试名，
# 裸 ns（如恰好有个集叫 "smoke"）不删——那已经不是测试名了。
TEST_NS = ("smoke", "segtest", "imgcfg", "wavaud", "nors", "emptytest",
           "sbguard", "resync", "imgscreen", "imgdup")

# 交付集硬保护名单。**不能只靠 git ls-files**：laogongtuan / lsm-15min 是
# 最新的两部成片（2.2GB out/*.mp4）却都未入库，git 挡不住它们。
PROTECTED_EPISODES = frozenset({
    "hanwu-ch3", "hanwu-feature", "hanwu-prologue", "hw-001", "hw-002",
    "laogongtuan", "lsm-15min",
})


def is_delivery_episode(ep_id: str) -> bool:
    """交付集判定：名单内，或该集目录下已存在成片 out/*.mp4。

    「成片在场」是最强信号——有 mp4 说明这集出过片，删掉就是不可逆损失。
    这条规则对未来新增的集自动生效，不依赖任何人记得更新名单。
    """
    if ep_id in PROTECTED_EPISODES:
        return True
    d = EPISODES / ep_id
    return d.is_dir() and any((d / "out").glob("*.mp4"))


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


@pytest.fixture(autouse=True, scope="session")
def _clean_test_episode_residue():
    """会话结束清扫测试自建 episode 残留（防中断跑次永久残留）。

    ⚠️ 这只是**兜底**。正规路径是测试用 tmp_episodes fixture 根本不写真实
    episodes/；本清扫只为「进程被杀 → teardown 没跑」这种残留兜底，
    所以它带三重闸：① 只认 {ns}- 带连字符的测试名 ② 交付集一律跳过
    ③ 逐个文件 unlink，绝不用 rmtree 通配。
    """
    yield
    _namespaces = TEST_NS
    for jf in EPISODES.glob("*.json"):
        stem = jf.stem
        if not any(stem.startswith(f"{ns}-") for ns in _namespaces):
            continue
        if is_delivery_episode(stem):
            print(f"\n[测试基建] 跳过 {stem}：判定为交付集，任何清扫都不得触碰")
            continue
        try:
            jf.unlink()
        except Exception:
            pass
    for d in EPISODES.iterdir():
        if not d.is_dir():
            continue
        if not any(d.name.startswith(f"{ns}-") for ns in _namespaces):
            continue
        if is_delivery_episode(d.name):
            print(f"\n[测试基建] 跳过目录 {d.name}：判定为交付集")
            continue
        import shutil
        shutil.rmtree(d, ignore_errors=True)


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
