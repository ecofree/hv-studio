# -*- coding: utf-8 -*-
"""P3 guardrails: per-key image selection, request timeouts, detached task advice.

Locks the three toolchain frictions that cost real time on 2026-09-29:
  1. stage_images ignored which key you wanted ->补单张图时预算被其余缺图抢光
  2. a hung provider connection held a worker for the full 300s request timeout
     and stalled the whole batch (15 min of zero output)
  3. background jobs started through a tool call were killed when that call timed
     out, losing progress at 27/36
"""
import inspect
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.shared
ROOT = Path(__file__).resolve().parents[1]


def test_build_image_jobs_filters_by_only():
    from app.orchestrator import build_image_jobs
    sig = inspect.signature(build_image_jobs)
    assert "only" in sig.parameters
    src = inspect.getsource(build_image_jobs)
    assert "only" in src


def test_cli_exposes_image_key():
    # main() 只做子命令分发，参数定义在 module 顶层；断言整文件更贴近真实契约
    src = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
    assert '"--image-key"' in src
    assert "image_only" in src, "--image-key 未透传到 produce"


def test_image_request_has_split_timeouts():
    """connect 与 read 必须分开给：connect 卡住时长 read 超时救不了。"""
    import app.services.image as I
    src = inspect.getsource(I.ImageGenerator._post_unlocked)
    assert "timeout=(" in src, "requests.post 未使用 connect/read 分离超时"
    assert "HV_IMAGE_CONNECT_TIMEOUT" in src
    assert "HV_IMAGE_READ_TIMEOUT" in src
    # 旧的单一 300s 会让一个挂死连接占满 worker 线程
    assert "timeout=300" not in src


def test_agents_md_records_the_detached_task_rule():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "detached" in low or "分离进程" in text, "AGENTS.md 未记录后台任务约定"
    assert "--image-key" in text, "AGENTS.md 未记录补单张图的正确方式"


def test_env_defaults_are_sane():
    import os
    from app.config.config import env_get
    # 显式设置时按设置走
    os.environ["HV_IMAGE_CONNECT_TIMEOUT"] = "7"
    assert env_get("HV_IMAGE_CONNECT_TIMEOUT", "20") == "7"
    del os.environ["HV_IMAGE_CONNECT_TIMEOUT"]
    assert env_get("HV_IMAGE_CONNECT_TIMEOUT", "20") == "20"
