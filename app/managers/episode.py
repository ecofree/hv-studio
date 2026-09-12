"""
Episode 状态机持久化管理器。

设计：
  - episodes/{id}.json 是单一可信源
  - 任何 stage 执行前先做 checkpoint 到 state/checkpoints/{id}/{ts}_{label}.json
  - 原子保存：先写 .tmp，再 rename
  - 阶段执行 run_stage(episode, stage_name)：
      幂等：已处于 stage 之后状态 → 跳过
      可恢复：state/checkpoints/ 中有 stage 之后的备份 → 直接恢复
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.config.config import env_get
from app.models import Episode, EpisodeStatus
from app.paths import ROOT, episode_dir, episode_json, episodes_dir, state_dir

ROOT = Path(__file__).resolve().parent.parent.parent
# P1-6（2026-09-05）：EPISODES_DIR 已删。此处改为从 app.paths 调用期求值——
# 否则「测试想重定向 episodes 根」做不到，只能真写 D:/hv/episodes/ 再通配删。
STATE_DIR = state_dir()
CHECKPOINT_DIR = STATE_DIR / "checkpoints"

# D6：checkpoint 无上限增长（每 stage 一份、文件名带微秒时间戳，永不复写）。
# 单集只保留最近 KEEP 份；设 HV_CHECKPOINT_KEEP=0 表示不轮转（保留全部）。
# I 批（2026-09-05）：改走 env_get（.env 优先、真实环境变量兜底）。
# 此前直读 os.environ，这个键写进 .env 是无效的。
CHECKPOINT_KEEP = int(env_get("HV_CHECKPOINT_KEEP", "20"))


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def episode_path(episode_id: str) -> Path:
    return episode_json(episode_id)


def checkpoint_path(episode_id: str, label: str, ts: datetime | None = None) -> Path:
    ts = ts or datetime.now()
    ts_str = ts.strftime("%Y%m%d_%H%M%S_%f")
    return CHECKPOINT_DIR / episode_id / f"{ts_str}_{label}.json"


CURRENT_SCHEMA = "1.0"


def load(episode_id: str) -> Episode:
    """从 episodes/{id}.json 加载。若文件不存在返回 DRAFT 状态的空 Episode。"""
    path = episode_path(episode_id)
    if not path.exists():
        return Episode(id=episode_id, title=episode_id, episode_number=0)
    ep = Episode.model_validate_json(path.read_text(encoding="utf-8"))
    # schema 兼容性检查：旧版无 schema_version → 视为 0.x 自动补 1.0（无破坏字段）
    if not ep.schema_version or ep.schema_version < CURRENT_SCHEMA:
        ep.schema_version = CURRENT_SCHEMA
        save(ep)  # 升版后落盘一次，后续幂等
    _normalize_asset_paths(ep)
    return ep


def validate_schema(ep: Episode, allow_empty: bool = False) -> list[str]:
    """M2 schema 固化：返回结构问题清单（空列表=合格）。

    allow_empty=True 时允许「空集」（无 frames 无 shots）通过——用于
    produce 从零起步（stage_script 会先切脚本填充 frames）的场景，避免
    「官方入口启动官方第一步」的鸡生蛋。默认 False 保持严格。
    """
    problems: list[str] = []
    if ep.schema_version != CURRENT_SCHEMA:
        problems.append(f"schema_version={ep.schema_version} 非当前 {CURRENT_SCHEMA}")
    if not ep.frames and not ep.shots:
        if not allow_empty:
            problems.append("episode 无 frames 也无 shots（空集）")
    # shot 引用一致性（空集无 shots，此检查自动跳过）
    frame_ids = {f.id for f in ep.frames}
    for sid, sh in ep.shots.items():
        if sh.frame_id and sh.frame_id not in frame_ids:
            problems.append(f"{sid}.frame_id={sh.frame_id} 不在 frames 中")
    return problems


def audit_orphan_wavs(
    episode_id: str,
    apply: bool = False,
) -> dict:
    """孤儿 wav 审计/清理（opt-in）。

    判据（安全闸 = 不被流水线任一权威索引引用，防误删交付集音频）：
      盘上 episodes/{id}/audio/*.wav 中，同时不被以下两者引用才认作孤儿：
        1) episode.json 任一 frame.audio_path（compose 只认这个）
        2) _tts_manifest.json 键（TTS 指纹写入的权威记录）
      被任一引用即保留。_orig.wav / frame-NNN-NN.wav(旧切分子帧) 这类既不在
      frames 也不在 manifest 的残留会被判为孤儿。

    返回 {'orphans': [...], 'deleted': n, 'skipped': m}。
    apply=False（默认）只列不删；apply=True 才 os.remove（逐文件，无 rm -rf）。
    """
    ep = load(episode_id)
    audio_dir = episode_dir(episode_id) / "audio"
    if not audio_dir.is_dir():
        return {"orphans": [], "deleted": 0, "skipped": 0}

    wavs = sorted(p.name for p in audio_dir.glob("*.wav"))
    ref = {Path(f.audio_path).name for f in ep.frames if f.audio_path}
    man_keys: set[str] = set()
    man = audio_dir / "_tts_manifest.json"
    if man.exists():
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                man_keys = set(m.keys())
        except Exception:
            pass  # manifest 解析失败 → 该层保险缺省，靠 frames 引用闸
    orphans = [
        w for w in wavs
        if w not in ref and w.replace(".wav", "") not in man_keys
    ]
    deleted = 0
    for w in orphans:
        p = audio_dir / w
        if apply:
            try:
                p.unlink()
                deleted += 1
            except Exception as e:
                print(f"  [!] 删失败 {w}: {e}")
        else:
            print(f"  [orphan] {w}  ({(p.stat().st_size if p.exists() else 0)/1024:.1f}KB)")
    return {"orphans": orphans, "deleted": deleted,
            "skipped": len(orphans) - deleted}


def save(ep: Episode) -> Path:
    """原子保存到 episodes/{id}.json。返回写入路径。"""
    episodes_dir().mkdir(parents=True, exist_ok=True)
    path = episode_path(ep.id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        ep.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# 资产路径可移植归一（2026-09-04：换盘符/换机仍可用）
# ---------------------------------------------------------------------------
def _normalize_asset_paths(ep: Episode) -> None:
    """把 shot.image_path / frame.audio_path / ref_pack_paths 里【指向旧根】的
    绝对路径重映射到当前 ROOT。

    历史数据里这些路径以 `D:/hv/...` 硬编码存储；若仓库整体搬移到其他盘/目录，
    直接 Path() 会失效。这里在 load() 边界统一把「前缀 ≠ 当前 ROOT」的绝对路径
    换头为当前 ROOT，使数据不动、消费方（compose/motion/voice）拿到的恒为有效
    绝对路径。路径已是相对或本就属当前 ROOT 的跳过。
    """
    cur = str(ROOT).replace("\\", "/").rstrip("/") + "/"
    root_s = str(ROOT)

    def remap(p: str | None) -> str | None:
        if not p:
            return p
        norm = p.replace("\\", "/")
        # 已是当前 ROOT 开头 → 不动
        if norm.startswith(cur) or norm.startswith(root_s):
            return p
        # 绝对路径但前缀是【另一个根】（如搬走前的 D:/hv）→ 剥掉旧根头，
        # 以相对段拼到当前 ROOT。相对路径（episodes/...）也在此收敛为绝对。
        if Path(norm).is_absolute():
            if Path(norm).exists():
                return Path(norm).as_posix()
            # 形如 D:/hv/episodes/... → 取旧根之后部分
            idx = norm.find("/episodes/")
            if idx >= 0:
                norm = norm[idx + 1:]  # episodes/...
            else:
                return p  # 非仓库内绝对路径，不动（可能是 C:\ 系统文件）
        return (ROOT / norm).as_posix()

    for sh in ep.shots.values():
        if sh.image_path:
            sh.image_path = remap(sh.image_path)
        if sh.ref_pack_paths:
            sh.ref_pack_paths = [remap(x) for x in sh.ref_pack_paths]
    for f in ep.frames:
        if f.audio_path:
            f.audio_path = remap(f.audio_path)


def rotate_checkpoints(episode_id: str, keep: int | None = None) -> list[Path]:
    """只保留该集最近 keep 个 checkpoint，返回被删掉的那些。

    文件名以 %Y%m%d_%H%M%S_%f 开头，字典序 == 时间序，直接排序即可。
    keep<=0 表示不轮转（保留全部）。
    """
    k = CHECKPOINT_KEEP if keep is None else keep
    if k <= 0:
        return []
    d = CHECKPOINT_DIR / episode_id
    if not d.exists():
        return []
    files = sorted(d.glob("*.json"))
    if len(files) <= k:
        return []
    stale = files[: len(files) - k]
    removed = []
    for p in stale:
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass        # 删不掉就算了，轮转是卫生问题，不该让生产失败
    return removed


def checkpoint(ep: Episode, label: str, keep: int | None = None) -> Path:
    """保存当前状态到 checkpoint 文件（并顺手轮转掉旧的）。"""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    cp = checkpoint_path(ep.id, label)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(ep.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
    rotate_checkpoints(ep.id, keep)
    return cp


def list_checkpoints(episode_id: str) -> list[Path]:
    d = CHECKPOINT_DIR / episode_id
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


# ---------------------------------------------------------------------------
# 阶段编排
# ---------------------------------------------------------------------------
# 状态 → 阶段名 的映射
STAGE_NAMES = {
    EpisodeStatus.DRAFT: "script",
    EpisodeStatus.SCRIPTED: "tts_align",
    EpisodeStatus.TTS_ALIGNED: "image_lock",
    EpisodeStatus.IMAGE_LOCKED: "upscale",
    EpisodeStatus.UPSCALED: "compose",
    EpisodeStatus.COMPOSED: "finalize",
}


def run_stage(
    ep: Episode,
    stage_fn: Callable[[Episode], None] | None = None,
) -> Episode:
    """
    通用阶段运行器。
    - ep: 当前 episode（已加载）
    - stage_fn: 实际干活的回调（M1 填实）

    行为：
      1. 状态机检查：当前状态必须能迁移到下一状态
      2. 幂等：如果 ep 已经在 DONE 之后状态，no-op
      3. 失败回滚：捕获异常 → ep.transition_to(FAILED)，保存，重新抛
    """
    # 幂等：已 DONE 跳过
    if ep.status == EpisodeStatus.DONE:
        return ep

    # 找下一目标
    order = [
        EpisodeStatus.DRAFT, EpisodeStatus.SCRIPTED, EpisodeStatus.TTS_ALIGNED,
        EpisodeStatus.IMAGE_LOCKED, EpisodeStatus.UPSCALED, EpisodeStatus.COMPOSED,
        EpisodeStatus.DONE,
    ]
    if ep.status not in order:
        raise ValueError(f"状态非法: {ep.status}")
    idx = order.index(ep.status)
    if idx + 1 >= len(order):
        return ep
    target = order[idx + 1]

    # P0-3 修复（审计）：禁止"无真实执行函数"的空状态推进。
    # 此前 `run_stage(ep)` 不传 stage_fn 时仍会 transition_to(target)，把 episode
    # 一路空推到 done —— 状态可作伪造，无音频/图/视频也"完成"。此处硬拦：凡要迁入
    # 一个【产生产物】的状态（SCRIPTED 之后全是），必须有真实 stage_fn，否则报错。
    # DRAFT→(script) 也需 stage_fn 才能产出 frames。首个无产物的迁入只可能是
    # 空 episode 到 DRAFT —— 那不经本函数。故凡本函数到达此处都必须有 stage_fn。
    if stage_fn is None:
        raise ValueError(
            f"状态推进 {ep.status}→{target} 缺少真实执行函数(stage_fn)。"
            f"禁止空推状态制造虚假完成。请通过 hv produce / API 编排器调用。")

    # checkpoint 留底
    label = STAGE_NAMES.get(ep.status, str(ep.status))
    cp = checkpoint(ep, f"before_{label}")

    try:
        stage_fn(ep)
        ep.transition_to(target)
        save(ep)
        return ep
    except Exception as e:
        ep.transition_to(EpisodeStatus.FAILED)
        ep.notes = f"[{label}] FAILED: {type(e).__name__}: {e}\ncheckpoint: {cp}"
        save(ep)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["show", "init", "checkpoint"])
    ap.add_argument("episode_id")
    ap.add_argument("--label", default="manual")
    args = ap.parse_args()

    if args.action == "init":
        ep = Episode(id=args.episode_id, title=args.episode_id, episode_number=0)
        p = save(ep)
        print(f"created: {p}")
    elif args.action == "show":
        ep = load(args.episode_id)
        print(json.dumps(ep.model_dump(mode="json"), ensure_ascii=False, indent=2))
    elif args.action == "checkpoint":
        ep = load(args.episode_id)
        cp = checkpoint(ep, args.label)
        print(f"checkpoint: {cp}")


if __name__ == "__main__":
    main()
