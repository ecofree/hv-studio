# -*- coding: utf-8 -*-
"""路径单一真源（整改 P1-6，2026-09-05）。

## 为什么要有这个文件

此前 `episodes/` 根目录被 4 处**各自在 import 期**推导：

    app/managers/episode.py:25    EPISODES_DIR = ROOT / "episodes"
    app/services/compose.py:46    BASE         = ROOT / "episodes"
    app/orchestrator.py:28        _EPISODES    = <pkg parent> / "episodes"
    app/services/shot_builder.py:70/84          ROOT / "episodes" / ...

后果不是「重复定义了 4 次」这么轻：
- import 期求值 → 测试无法重定向，只能把测试 episode 真写进 `D:/hv/episodes/`；
- 于是必须配一套「会话末按前缀通配删」来兜底 → 进程被杀就永久残留；
- 更糟的是**前缀一旦与交付集相撞就是误删成片**（`laogongtuan`、
  `lsm-15min` 还是未入库的集，`git ls-files` 挡不住）。

## 收敛方式

全部改成**调用期求值**的函数。生产行为一字不变（仍是 `ROOT/"episodes"`），
但测试只要 monkeypatch `app.paths.episodes_dir` 一个点，整条链
（managers / compose / orchestrator / shot_builder）一起改道，
真实 `episodes/` 一个字节都不写、也就不必再删。

⚠️ 新增代码一律走这里的函数，不要再造 `ROOT / "episodes"`。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # D:/hv


def episodes_dir() -> Path:
    """episodes/ 根目录。调用期求值——测试可 monkeypatch 本函数整体重定向。"""
    return ROOT / "episodes"


def episode_dir(ep_id: str) -> Path:
    """episodes/{ep_id} —— 单集资产目录。"""
    return episodes_dir() / ep_id


def episode_json(ep_id: str) -> Path:
    """episodes/{ep_id}.json —— 单集单一可信源。"""
    return episodes_dir() / f"{ep_id}.json"


def state_dir() -> Path:
    """state/ —— 健康度、台账、checkpoint 等运行态。"""
    return ROOT / "state"
