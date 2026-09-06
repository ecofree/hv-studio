# -*- coding: utf-8 -*-
"""build/ 中间产物清理（整改 P1-8，2026-09-05）。

## 为什么需要它

`episodes/*/build/` 常驻 `narration.wav` / `voice_bgm_mix.wav` / `segments/seg-*.mp4`。
实测（2026-09-05）：

    820M  lsm-15min/build    39M laogongtuan   28M hw-001
     12M  hanwu-ch3         9.1M hw-002      5.6M hanwu-feature   856K hanwu-prologue

其中 `hw-001/build/` 明显是调试残留：`video.mp4` / `test1.mp4` / `concat.txt` /
`subs.ass` / `check` / `check2`。这些**可以安全删**——它们每次 compose 都会重建。
`build/` 在 `.gitignore` 里不会入库，但会一直占盘，且没有任何命令管过它。

## 设计原则（三条硬约束，缺一不可）

1. **白名单**：只认下面列名的中间产物。没在名单里的一律不动，绝不 glob `*` 后 rm。
2. **图片资产绝不删**：底图（`episodes/*/shots/*.png`）是**花钱生成**且跨集复用的
   （hw-002 的底图被 5 集引用），后期其它生产内容还要复用。所以这里加了
   `_NEVER_DELETE_SUFFIX` 硬闸——哪怕白名单写错，图片也删不掉。
3. **默认 dry-run**：不传 `--apply` 只列清单和体积。删之前必须让人看见要删什么。

段（`build/segments/seg-*.mp4`）单独一档：它体积最大，但删了下次 compose 要
**全量重编**（断点续跑的缓存就是这些段）。所以默认不清段，要显式 `--segments`。
"""
from __future__ import annotations

from pathlib import Path

# ---- 白名单：相对 episodes/{id}/build/ 的文件名 / glob ----
# 这些都是 compose / 调试过程的中间产物，删掉后下次跑会原样重建，无信息损失。
BUILD_FILES = (
    "narration.wav",          # 逐帧 wav 拼出来的全片旁白（每次 compose 重建）
    "voice_bgm_mix.wav",      # 混入 BGM 后的音轨（同上）
    "subs.ass",               # 早期烧字幕调试产物（现字幕走逐帧 PIL，非此文件）
    "concat.txt",             # 早期 ffmpeg concat 列表（现 concat 用 .concat.txt 临时文件）
    "video.mp4",              # 早期无音轨中间视频（现产物在 build/segments/）
    "test1.mp4",              # 手工调试产物
)
BUILD_GLOBS = (
    "*.concat.txt",           # _concat_segments 的列表文件（正常会被 finally 删）
    "*.concat_tmp",           # concat 的 .tmp（正常会被 finally 删）
    "*.mp4.tmp",              # mux 的 .tmp（正常会原子改名）
    "*.tmp",
)

# 早期版本的 build 子目录，均已无消费者（真音频目录是 episodes/{id}/audio，
# 不在 build 下）。空目录或纯调试帧。
BUILD_DIRS = ("check", "check2", "audio", "frames", "motion", "subs")

# 段：体积最大，但删了 = 下次全量重编，所以单独一档
SEGMENT_GLOBS = ("seg-*.mp4",)
SEGMENT_STATE_FILES = ("params.json",)   # G 批的参数指纹，随段一起作废

# out/ 下的临时残留（2026-09-06 成片回归实测：concat 的 list_file 若清理失败
# 会留在 out/ 里）。**只认这几个名字，绝不碰 *.mp4**——成片在 out/ 里。
OUT_TMP_GLOBS = ("*.concat.txt", "*.concat_tmp", "*.mp4.tmp", "*.tmp")

# 硬闸：任何情况下都不删的扩展名——**图片资产**。
# 底图是花钱生成、且跨集复用的（hw-002 的底图被 5 集引用，后期其它生产内容
# 还要接着用）。所以这里只拦图片，不拦 .json/.md 之类的数据文件：
# 拦太宽会把白名单里显式点名的 params.json 静默跳过——那种「白名单写了却
# 不生效」正是本项目最忌的失败模式。
_NEVER_DELETE_SUFFIX = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".psd",
})


def _is_image(p: Path) -> bool:
    return p.suffix.lower() in _NEVER_DELETE_SUFFIX


def scan_episode_build(ep_dir: Path, include_segments: bool = False,
                       _skipped: list[dict] | None = None) -> list[dict]:
    """扫一个集的 build/，返回待清项 [{path, size, tier}]。**只读，不删。**

    只扫描 `episodes/{id}/build/` 一层及其直接子目录；绝不触碰 `out/`（成片）
    与 `shots/`（底图）——它们在扫描范围之外，这是结构性的，不是靠判断。
    """
    build = ep_dir / "build"
    if not build.is_dir():
        return []

    found: list[dict] = []
    seen: set[Path] = set()

    def add(p: Path, tier: str) -> None:
        if p in seen or not p.is_file():
            return
        # 硬闸：白名单写错也不许删图片资产。被闸掉的一律**记账**——
        # 静默跳过会让人以为白名单生效了、实际没有，这正是本项目最忌的失败模式。
        if _is_image(p):
            if _skipped is not None:
                _skipped.append({"path": str(p), "reason": "图片资产，永不删除"})
            return
        seen.add(p)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        found.append({"path": p, "size": size, "tier": tier})

    for name in BUILD_FILES:
        add(build / name, "build")
    for g in BUILD_GLOBS:
        for p in build.glob(g):
            add(p, "build")
    for d in BUILD_DIRS:
        sub = build / d
        if sub.is_dir():
            for p in sub.rglob("*"):
                add(p, "build")

    # out/ 下的临时残留（不含成片：只有上面 4 个 glob 能命中，*.mp4 不在其中）
    out_dir = ep_dir / "out"
    if out_dir.is_dir():
        for g in OUT_TMP_GLOBS:
            for p in out_dir.glob(g):
                add(p, "build")

    if include_segments:
        seg = build / "segments"
        if seg.is_dir():
            for g in SEGMENT_GLOBS:
                for p in seg.glob(g):
                    add(p, "segments")
            for name in SEGMENT_STATE_FILES:
                add(seg / name, "segments")

    return found


def clean_episodes(
    ep_dirs: dict[str, Path],
    include_build: bool = True,
    include_segments: bool = False,
    apply: bool = False,
) -> dict:
    """清理若干集的 build/ 中间产物。

    返回 {'items': [...], 'total_bytes': n, 'deleted_bytes': n, 'deleted': n,
          'skipped': [{'path','reason'}]}
    apply=False（默认）只扫描不删。
    """
    items: list[dict] = []
    skipped: list[dict] = []
    for _ep_id, ep_dir in ep_dirs.items():
        # 一次扫描同时收 build 与 segments（只扫一次，避免 tier 过滤漏项）
        got = scan_episode_build(ep_dir, include_segments=include_segments,
                                 _skipped=skipped)
        for it in got:
            if it["tier"] == "segments" and not include_segments:
                continue
            if it["tier"] == "build" and not include_build:
                continue
            items.append(it)

    deleted = 0
    deleted_bytes = 0
    for it in items:
        p: Path = it["path"]
        if not apply:
            continue
        try:
            p.unlink()
            deleted += 1
            deleted_bytes += it["size"]
        except OSError as e:
            skipped.append({"path": str(p), "reason": str(e)})

    # 删空后清理 build 下变成空的遗留子目录（只删已知白名单目录，且必须为空）
    if apply:
        for ep_dir in ep_dirs.values():
            for d in BUILD_DIRS:
                sub = ep_dir / "build" / d
                if sub.is_dir() and not any(sub.iterdir()):
                    try:
                        sub.rmdir()
                    except OSError:
                        pass

    return {
        "episodes": sorted(ep_dirs),
        "items": [{**it, "path": str(it["path"])} for it in items],
        "total_bytes": sum(i["size"] for i in items),
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "skipped": skipped,
        "apply": apply,
    }


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
