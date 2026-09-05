# -*- coding: utf-8 -*-
"""测试基建自检：防止「用例悄悄消失」。

背景（2026-09-05 二轮复查 P0-1）：
`tests/test_production_regressions.py` 曾把 `_install_fake_encoder` + 4 个 NVENC
用例整段重复定义了一遍。Python 后定义覆盖前定义，pytest 收集数看着也正常，
**其中一整组用例其实永不执行**——NVENC 降级这条最贵的回归守卫等于没有。
这类问题完全静默：不报错、不变红。

两条守卫：
  1. 同文件内不得有同名用例（直接抓「覆盖」）
  2. 收集数必须 == AST 定义数（在 conftest 的 pytest_collection_modifyitems 里，
     能抓到本条抓不到的一类）
"""
from __future__ import annotations

from _case_stats import count_defined_cases, defined_cases, iter_test_files


def test_no_duplicate_case_names_within_a_file():
    """同一测试文件内不得有同名用例——后定义会静默覆盖前定义。"""
    dups: list[str] = []
    for f in iter_test_files():
        for name, linenos in defined_cases(f).items():
            if len(linenos) > 1:
                dups.append(f"{f.name}:{name} @ 行 {linenos}")
    assert not dups, (
        "以下用例被重复定义，后面的会静默覆盖前面的（被覆盖的那份永不执行）：\n  "
        + "\n  ".join(dups))


def test_every_test_file_has_at_least_one_case():
    """每个 test_*.py 至少含一个用例——防「整文件被遗忘」。

    例：文件名拼错成 `tets_x.py`、或文件里只剩 helper，都会被抓到。
    """
    empty = [f.name for f in iter_test_files() if not defined_cases(f)]
    assert not empty, f"以下测试文件里一个用例都没有（命名或内容有误）：{empty}"


def test_defined_case_count_is_sane():
    """AST 口径的定义数必须 > 0（守卫自身别被写坏）。"""
    assert count_defined_cases() > 0
