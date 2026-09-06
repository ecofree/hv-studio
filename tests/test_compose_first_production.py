# -*- coding: utf-8 -*-
"""P0-2/P0-3 验收：首次合成（无旧成片）必须成功，且不留 .tmp 残片。

对应 2026-09-04 审计 P0-2 断言：
  对一个从未存在输出文件的三镜头样例执行合成，成功生成 MP4，且无残留 .tmp。
(此处用单镜 static 小 episode 驱动真实 compose 编码路径——避开大图 OOM，
验证点覆盖：无旧 final 的首产 + encode_ok 标志 + .tmp 原子改名 + 无 .tmp 残留。)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import NEEDS_FFMPEG
from app.paths import episode_dir


@NEEDS_FFMPEG
def test_first_production_compose_ok(smoke_episode):
    ep_id, src_p, big_p, dur_ms = smoke_episode
    from app.services.compose import compose_episode

    # P1-6：走 app.paths（调用期求值）。smoke_episode 把根重定向到了 tmp，
    # 这里若写死 ROOT/"episodes" 就会去 probe 一个根本不存在的真实路径，
    # ffprobe 静默返回空串 → 断言 "h264" 落空（2026-09-05 实测踩到）。
    out = episode_dir(ep_id) / "out"
    final = out / f"{ep_id}_m1.mp4"
    assert not final.exists(), "测试前提：首产前不应有旧成片"

    p = compose_episode(ep_id)
    assert Path(p).exists() and Path(p).stat().st_size > 1024

    # 无 .tmp 残留
    tmps = list(out.glob("*.tmp")) + list(out.glob("*.mp4.tmp"))
    assert tmps == [], f"首产残留 .tmp: {tmps}"

    # mp4 可解码、参数正确
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,profile,level,nb_frames",
         "-of", "default=noprint_wrappers=1", str(final)],
        capture_output=True, text=True)
    info = r.stdout
    assert "h264" in info and "High" in info, f"非预期编码: {info}"

    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final),
                          "-f", "null", "-"], capture_output=True, text=True)
    assert dec.returncode == 0, f"decode-to-null 失败: {dec.stderr[-400:]}"
    assert dec.stderr.strip() == "", f"decode 有非零错误输出: {dec.stderr[-400:]}"
