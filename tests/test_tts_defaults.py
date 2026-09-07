"""TTS 合成粒度「默认 = 逐帧」的防回归。

背景（2026-09-07）：段落级在长上下文下会把「晋阳令刘文静」断成
「晋阳 / 令刘文静」，且 ≥45 字必错，而段落级中位长度正是 65 字 ——
结构性错误。所以默认档位退回逐帧（正确性优先）。

这里钉两件事：
1. 四处默认值都是 frame（有人手滑改回 paragraph 会立刻红）
2. CLI 帮助能正常渲染（argparse 对 help 字符串做 % 格式化，
   写「+8%」会抛 ValueError —— 2026-09-07 本人亲手踩过）
"""
from __future__ import annotations

import importlib
import inspect
import sys

import pytest


def test_stage_tts_defaults_to_frame():
    from app import orchestrator as O

    assert inspect.signature(O.stage_tts).parameters["granularity"].default == "frame"


def test_stage_assets_defaults_to_frame():
    from app import orchestrator as O

    sig = inspect.signature(O.stage_assets)
    assert sig.parameters["tts_granularity"].default == "frame"


def test_produce_defaults_to_frame():
    from app import orchestrator as O

    sig = inspect.signature(O.produce)
    assert sig.parameters["tts_granularity"].default == "frame"


def test_voice_synthesize_defaults_to_frame():
    from app.services import voice as V

    sig = inspect.signature(V.synthesize)
    assert sig.parameters["granularity"].default == "frame"


def test_workers_default_is_auto():
    """workers 默认 None = 按粒度自动（2026-09-07 前写死 4 且无开关）。"""
    from app.services import voice as V

    for fn in (V.synthesize, ):
        p = inspect.signature(fn).parameters.get("workers")
        assert p is not None, f"{fn.__name__} 没有 workers 参数"
        assert p.default is None


def test_resolve_workers_by_granularity():
    """逐帧 2（请求数 20 倍，4 路实测撞限流）；段落 4；显式值优先。"""
    from app.services.voice import _resolve_workers as R

    assert R(None, "frame") == 2
    assert R(None, "paragraph") == 4
    assert R(8, "frame") == 8          # 显式覆盖
    assert R(1, "paragraph") == 1
    assert R(0, "frame") == 1          # 兜底：至少 1 路


def test_orchestrator_threads_workers():
    """workers 要能一路传到 stage_tts —— 闭包漏传是本项目踩过三次的坑（#38）。"""
    from app import orchestrator as O

    for fn, key in ((O.stage_tts, "workers"),
                    (O.stage_assets, "tts_workers"),
                    (O.produce, "tts_workers")):
        p = inspect.signature(fn).parameters.get(key)
        assert p is not None, f"{fn.__name__} 缺 {key} 参数"
        assert p.default is None


def _cli_granularity_kw() -> dict:
    """跑一次 `app.cli.main(["episode", "--help"])`，用 spy 抓
    --tts-granularity 的 add_argument 参数。

    ⚠️ app.cli.main() 不收参数，读 sys.argv —— 必须 patch sys.argv。
    """
    import argparse

    mod = importlib.import_module("app.cli")
    captured: dict = {}
    orig = argparse.ArgumentParser.add_argument

    def spy(self, *a, **kw):
        if "--tts-granularity" in a:
            captured.update(kw)
        return orig(self, *a, **kw)

    argv = sys.argv
    sys.argv = ["hv", "episode", "--help"]
    argparse.ArgumentParser.add_argument = spy
    try:
        mod.main()
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.add_argument = orig
        sys.argv = argv
    return captured


def test_cli_default_is_frame():
    kw = _cli_granularity_kw()
    assert kw, "没抓到 --tts-granularity 的定义，测试 spy 失效了"
    assert kw.get("default") == "frame", \
        f"CLI --tts-granularity default = {kw.get('default')!r}，应为 frame"


def test_cli_help_renders_without_percent_crash():
    """argparse 会对 help 字符串做 % 展开 —— 写「+8%」会抛 ValueError。

    2026-09-07 我改帮助文案时真的踩了这个坑：`hv episode --help` 直接崩，
    报错 ValueError: unsupported format character。帮助文案也是代码。
    """
    import contextlib
    import io

    mod = importlib.import_module("app.cli")
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["hv", "episode", "--help"]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.main()
            except SystemExit:
                pass
    finally:
        sys.argv = argv
    out = buf.getvalue()
    assert "ValueError" not in out, "帮助文案里有未转义的 %"
    assert "Traceback" not in out
    assert "--tts-granularity" in out
