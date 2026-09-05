# -*- coding: utf-8 -*-
"""用例定义统计——给「用例悄悄消失」守卫共用（conftest 与测试文件都 import 它）。

放独立模块（下划线开头，pytest 不收集）而不是塞进 conftest，
因为 conftest 是 pytest 专用、不能被普通测试文件 import。
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def iter_test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def defined_cases(path: Path) -> dict[str, list[int]]:
    """返回 {用例限定名: [行号, ...]}，限定名 = 顶层函数名 或 ClassName::method。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, list[int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                found.setdefault(node.name, []).append(node.lineno)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name.startswith("test_")):
                    found.setdefault(f"{node.name}::{sub.name}", []).append(sub.lineno)
    return found


def count_defined_cases() -> int:
    """全 tests/ 目录 AST 能数到的用例定义总数（含被同名覆盖的重复定义）。"""
    return sum(len(v) for f in iter_test_files() for v in defined_cases(f).values())
