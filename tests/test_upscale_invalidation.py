# -*- coding: utf-8 -*-
"""P0-2 验收：upscale 复用必须检测源图变更。

对应 2026-09-04 审计 P0-2：
  - 旧行为：@2x 存在且尺寸正确就复用，源图被重新生成后仍静默复用旧 @2x。
  - 修复后：复用条件 dst_mtime >= src_mtime；源图变更 → @2x 重做。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import os
from pathlib import Path

from PIL import Image


def test_upscale_reuse_on_unchanged_source(tmp_path):
    from app.services.upscale import upscale
    src = tmp_path / "sc.png"
    Image.new("RGB", (100, 100), (255, 0, 0)).save(src)
    dst = upscale(src)
    t1 = dst.stat().st_mtime_ns
    d2 = upscale(src)  # 无变化 → 复用同一文件
    assert d2 == dst and d2.stat().st_mtime_ns == t1, "未变源图应复用"


def test_upscale_redone_when_source_changed(tmp_path):
    from app.services.upscale import upscale
    src = tmp_path / "sc.png"
    Image.new("RGB", (100, 100), (255, 0, 0)).save(src)
    dst = upscale(src)
    t1 = dst.stat().st_mtime_ns
    # 源图内容/size 相同但重新生成（mtime 变新）
    os.utime(src, None)  # bump mtime to now
    Image.new("RGB", (100, 100), (0, 0, 255)).save(src)
    d3 = upscale(src)
    m3 = d3.stat().st_mtime_ns
    assert m3 > t1, "源图变更 → @2x 必须重做"
    with Image.open(d3) as im:      # with：否则留一个未关闭 fd（C5 后可见）
        assert im.size == (200, 200)


def test_upscale_runtime_engine_from_config(tmp_path):
    """engine 单一真源：config = pil_lanczos，运行时 _engine() 同步。"""
    from app.services.upscale import _engine
    from app.config.config import get_settings
    assert _engine() == get_settings().image.postprocess_upscale.engine == "pil_lanczos"
