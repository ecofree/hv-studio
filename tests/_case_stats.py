# -*- coding: utf-8 -*-
"""Find duplicate test declarations without relying on parametrized case counts."""
from __future__ import annotations

import ast
from pathlib import Path





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
