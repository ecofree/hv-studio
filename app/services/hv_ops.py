# -*- coding: utf-8 -*-
"""运维操作：底图冷备 / 临时目录 gc / 端到端冒烟。

三个都是 2026-09-09 审计第三轮补的生产能力：
  · 底图（episodes/*/shots）是**唯一花钱生成且跨集复用**的资产（hw-002 的
    底图被 5 集引用），既不在 git 也无冷备 → 误删不可逆。
  · state/ 下的 pytest basetemp（_bt_*）会一直堆积（实测 132 个 / 426M）。
  · 没有端到端冒烟时，改动打穿链路无法机械发现（2026-09-09 就发生过：
    补丁锚点串了，reshot() 被截废，全量单测却照样绿）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from app import paths  # 调用期求值：测试用 tmp_episodes 夹具重定向 episodes_dir()


def _human(n: float) -> str:
    for u in ("B", "K", "M", "G"):
        if abs(n) < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}T"


# ---------------------------------------------------------------------------
# ① 底图冷备
# ---------------------------------------------------------------------------
def backup_shots(ep_id: str, dest_root: Path | None = None,
                 dry_run: bool = False) -> dict:
    """把一集的 shots/ 复制到冷备目录（保留 mtime —— upscale 缓存按 mtime 失效）。

    幂等：目标已存在且 size+mtime 一致则跳过。
    """
    src = paths.episodes_dir() / ep_id / "shots"
    if not src.is_dir():
        return {"ep": ep_id, "skipped": "无 shots/ 目录", "copied": 0}

    root = Path(dest_root) if dest_root else (paths.ROOT.parent / "hv_backup" / "shots")
    dst = root / ep_id
    copied, skipped, bytes_copied = 0, 0, 0

    for p in sorted(src.glob("*")):
        if not p.is_file():
            continue
        target = dst / p.name
        if target.exists():
            try:
                if (target.stat().st_size == p.stat().st_size
                        and abs(target.stat().st_mtime - p.stat().st_mtime) < 2):
                    skipped += 1
                    continue
            except OSError:
                pass

        if dry_run:
            copied += 1
            bytes_copied += p.stat().st_size
            continue

        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)          # copy2 保留 mtime
        copied += 1
        bytes_copied += p.stat().st_size

    return {"ep": ep_id, "dest": str(dst), "copied": copied,
            "skipped": skipped, "bytes": bytes_copied,
            "human": _human(bytes_copied), "dry_run": dry_run}


def backup_all(dry_run: bool = False) -> list[dict]:
    ids = sorted(p.stem for p in paths.episodes_dir().glob("*.json"))
    return [backup_shots(i, dry_run=dry_run) for i in ids]


# ---------------------------------------------------------------------------
# ② 临时目录 gc
# ---------------------------------------------------------------------------
def gc_state(older_than_days: int = 7, dry_run: bool = False) -> dict:
    """清理 state/ 下的测试临时目录（_bt_* 与 .tmp/bt-*）。

    只认这两个命名（pytest basetemp / 自建 tmp），绝不 glob 后 rmtree。
    """
    st = paths.state_dir()
    if not st.is_dir():
        return {"freed": 0, "removed": 0, "human": "0B"}

    now = time.time()
    cutoff = older_than_days * 86400
    targets = []
    for pat in ("_bt_*", ".tmp/bt-*"):
        targets.extend(st.glob(pat))

    removed, freed, skipped = 0, 0, 0
    for d in targets:
        if not d.is_dir():
            continue
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < cutoff:
            skipped += 1
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
        removed += 1
        freed += size

    return {"removed": removed, "skipped_recent": skipped, "freed": freed,
            "human": _human(freed), "dry_run": dry_run,
            "older_than_days": older_than_days}


# ---------------------------------------------------------------------------
# ③ 端到端冒烟（全 mock，零请求零成本）
# ---------------------------------------------------------------------------
_SMOKE_SCRIPT = """第一句测试文本。
第二句测试文本，稍长一点点。
第三句收尾。
"""


def run_smoke(keep: bool = False, engine: str = "mock") -> dict:
    """3 帧迷你样本跑通 script→tts→shots→compose→verify 全链路。

    TTS 与生图**全打桩**（写静音 wav / 纯色 png），零请求零成本，秒级完成。
    目的不是验证音质画质，而是验证**链路还活着** —— 能抓住「stage 参数传错」
    「函数体被打穿」这类改动（2026-09-09 reshot 事故就是靠它该发现而没发现的）。
    """
    import json
    import wave

    import numpy as np

    from app.managers import load, save
    from app.models import Episode

    ep_id = f"smoke-{int(time.time())}"
    ep_dir = paths.episodes_dir() / ep_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    script = ep_dir / "script.md"
    script.write_text(_SMOKE_SCRIPT, encoding="utf-8")

    try:
        ep = Episode(id=ep_id, title=ep_id, episode_number=0)
        save(ep)

        # --- stage: script（真实切分）---
        from app.orchestrator import stage_script
        stage_script(ep, md=_SMOKE_SCRIPT, merge_short=False)
        if not ep.frames:                 # stage 内部可能另存了一份
            ep = load(ep_id)
        save(ep)                          # 落盘，供 shots/compose 各自 load
        save(ep)                          # 落盘，供 shots/compose 各自 load
        n_frames = len(ep.frames)
        if not n_frames:
            return {"ok": False, "stage": "script", "error": "切分出 0 帧"}

        # --- TTS 打桩：写静音 wav（约 60ms/字），并回填时长 ---
        audio_dir = ep_dir / "audio"
        audio_dir.mkdir(exist_ok=True)
        for f in ep.frames:
            # 必须落在 [MIN_FRAMES, MAX_FRAMES] = [40, 200] 帧 = [1.6s, 8.0s]
            ms = max(1800, min(7000, len(f.text) * 200))
            wav = audio_dir / f"{f.id}.wav"
            sr = 16000  # 约定采样率（compose 会硬校验，混采样率会导致成片变调）
            n = int(sr * ms / 1000)
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(np.zeros(n, dtype=np.int16).tobytes())
            f.audio_path = str(wav).replace("\\", "/")
            f.audio_duration_ms = ms
        save(ep)

        # --- 生图打桩：1 张纯色 png ---
        shots_dir = ep_dir / "shots"
        shots_dir.mkdir(exist_ok=True)
        # ⚠️ 底图幂等门槛是 >50KB：纯色 png 只有几 K，会被判「缺底图」。
        #    所以桩图必须用噪声（随机像素），保证体积远超门槛。
        from PIL import Image
        rng = np.random.default_rng(0)
        arr = rng.integers(0, 256, (1920, 1080, 3), dtype=np.uint8)
        Image.fromarray(arr).save(shots_dir / "SM01.png")

        # --- scene_plan：1 张底图覆盖全部帧 ---
        from app.services.script import frames_fingerprint
        plan = {
            "episode": ep_id,
            # ⚠️ 相对路径是按 ROOT 解析的（不是 episodes_dir）—— 测试把
            #    episodes 根重定向到 tmp 后必须写绝对路径，否则找不到底图。
            "images": {"SM01": {"path": str(shots_dir / "SM01.png")}},
            "scenes": [{"frames": [0, n_frames - 1], "image": "SM01",
                        "grades": ["noon"]}],
            "frames_fingerprint": frames_fingerprint(
                [f.text for f in ep.frames]),
        }
        (ep_dir / "scene_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (ep_dir / "image_prompts.json").write_text(
            json.dumps({"SM01": {"en": "smoke test image"}}, ensure_ascii=False),
            encoding="utf-8")

        # --- stage: shots（真实装配）---
        from app.services.shot_builder import build_episode_shots
        build_episode_shots(ep_id)
        ep = load(ep_id)
        n_shots = len(ep.shots)

        # --- stage: compose（真实渲染，几秒）---
        from app.services.compose import compose_episode
        final = compose_episode(ep_id)
        ok = Path(final).exists() and Path(final).stat().st_size > 10000
        size = Path(final).stat().st_size if Path(final).exists() else 0

        return {"ok": bool(ok), "episode": ep_id, "frames": n_frames,
                "shots": n_shots, "final": str(final), "bytes": size,
                "human": _human(size)}
    finally:
        if not keep:
            shutil.rmtree(ep_dir, ignore_errors=True)
            sj = paths.episodes_dir() / f"{ep_id}.json"
            if sj.exists():
                sj.unlink(missing_ok=True)
