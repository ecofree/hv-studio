"""
成片输出绑定（P1-1，2026-09-29）

缺口
----
v2 侧有成片的完整哈希链：`published.json` 记 candidate 的 `sha256`，每次
`get_status` 都用 `file_sha256(candidate_path) != candidate_sha256` 复核
（production_status.py:58）。**legacy 路径没有等价物**：

  - 已交付的 mp4 没有 sha256 记录
  - `input-approval.json` 绑的是**输入**指纹，不绑输出

后果：成片文件被替换或重渲，`admission.check()` 依然 `eligible: true`。
2026-09-29 当天就发生过一次——为修字幕错字重渲了 caomin-01 与 lishimin-04，
而当时的审批记录（绑输入指纹）"恰好"没失效，是运气不是设计。

本模块
------
`production/output-binding.json` 记录成片的 sha256 / 帧数 / 时长 / 字节数，
由 `admission.record_approval` 在审批时写入、`check()` 在校验时复核。
于是"审批通过的那一版成片"从一句话变成一个可验证的字节事实。

与 v2 的分工：v2 管的是 **candidate（渲染中间产物）→ published（发布指针）**；
本模块管的是 **legacy-preview 直接成片**。两者都落在"审批必须绑定具体字节"
这条原则上，不合并实现。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

EPISODE_DIR_NAME = "production"
BINDING_NAME = "output-binding.json"


def final_mp4(ep_id: str) -> Path:
    from app.paths import episode_dir
    return episode_dir(ep_id) / "out" / f"{ep_id}_m1.mp4"


def binding_path(ep_id: str) -> Path:
    from app.paths import episode_dir
    return episode_dir(ep_id) / EPISODE_DIR_NAME / BINDING_NAME


def probe(mp4) -> dict:
    """读成片的帧数与时长。ffprobe 不在 PATH 时退化为文件元信息。"""
    import shutil

    mp4 = Path(mp4)
    out = dict(path=str(mp4), bytes=mp4.stat().st_size, frames=None, duration_s=None)
    exe = shutil.which("ffprobe")
    if exe:
        r = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-show_entries", "format=duration", "-of", "json", str(mp4)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        if r.returncode == 0:
            try:
                j = json.loads(r.stdout)
                out["frames"] = int(j["streams"][0]["nb_read_packets"])
                out["duration_s"] = round(float(j["format"]["duration"]), 3)
            except (KeyError, ValueError, TypeError):
                pass
    return out


def bind(ep_id: str, mp4=None) -> dict:
    """把当前成片写成绑定记录。成片不存在时抛错——不能绑一个不存在的字节。"""
    from app.services.provenance import atomic_json, file_sha256

    mp4 = Path(mp4) if mp4 else final_mp4(ep_id)
    if not mp4.is_file():
        raise FileNotFoundError(
            f"{ep_id}: 成片不存在 {mp4}，无法建立输出绑定。"
            "处置：先跑 episode produce --stage compose。")
    info = probe(mp4)
    rec = dict(episode=ep_id, kind="output_binding",
               path=str(mp4), sha256=file_sha256(mp4), bytes=info["bytes"],
               frames=info["frames"], duration_s=info["duration_s"])
    p = binding_path(ep_id)
    atomic_json(p, rec)
    return rec


def verify(ep_id: str) -> dict:
    """复核成片字节是否仍是审批时那一版。"""
    from app.services.narration_pipeline import read
    from app.services.provenance import file_sha256

    p = binding_path(ep_id)
    if not p.is_file():
        return dict(ok=True, skipped=True, reason="该集无输出绑定（未建立或非 legacy 管线）")
    rec = read(p)
    mp4 = Path(rec["path"])
    if not mp4.is_file():
        return dict(ok=False, fails=[f"成片缺失 {mp4}"], path=str(mp4),
                    sha256=rec.get("sha256"))
    cur = file_sha256(mp4)
    if cur != rec["sha256"]:
        return dict(ok=False,
                    fails=[f"成片字节已变（sha256 {cur[:16]} != 审批时 {rec['sha256'][:16]}）"],
                    path=str(mp4), recorded_sha256=rec["sha256"], current_sha256=cur,
                    frames=rec.get("frames"))
    return dict(ok=True, fails=[], path=str(mp4), sha256=cur,
                frames=rec.get("frames"), bytes=rec.get("bytes"),
                duration_s=rec.get("duration_s"))
