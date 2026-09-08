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


def test_stage_tts_defaults_to_paragraph():
    from app import orchestrator as O

    assert inspect.signature(O.stage_tts).parameters["granularity"].default == "paragraph"


def test_stage_assets_defaults_to_paragraph():
    from app import orchestrator as O

    sig = inspect.signature(O.stage_assets)
    assert sig.parameters["tts_granularity"].default == "paragraph"


def test_produce_defaults_to_paragraph():
    from app import orchestrator as O

    sig = inspect.signature(O.produce)
    assert sig.parameters["tts_granularity"].default == "paragraph"


def test_voice_synthesize_defaults_to_paragraph():
    from app.services import voice as V

    sig = inspect.signature(V.synthesize)
    assert sig.parameters["granularity"].default == "paragraph"


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


def test_voice_id_matches_model():
    """voice_id 与模型必须匹配 —— 拿 flash 的 id 去跑 plus → HTTP 418。

    2026-09-08 实测：测试脚本漏传 voice_id，回退取了 flash 的 id 配 plus，
    四个用例全部 418。这里钉死：不同模型必须给出不同的 voice_id，
    且 id 里带模型名（便于一眼看出是否串了）。
    """
    from app.services.cosyvoice import get_voice_id

    v_plus = get_voice_id("cosyvoice-v3.5-plus")
    v_flash = get_voice_id("cosyvoice-v3.5-flash")
    assert v_plus != v_flash
    assert "plus" in v_plus and "flash" in v_flash


def test_default_model_is_plus():
    """2026-09-08 起默认升 plus（¥0.54 vs ¥0.29，绝对值可忽略，韵律更好）。"""
    from app.services.cosyvoice import _cosyvoice_model_from_env
    assert _cosyvoice_model_from_env() == "cosyvoice-v3.5-plus"


def test_instruction_is_configured_and_content_agnostic():
    """指令必须已配置，且不能点名具体词（它作用于每一组）。"""
    from app.config.config import get_settings
    ins = (get_settings().get_profile().voice.instruction or "").strip()
    assert ins, "instruction 未配置 → 出片又是平的"
    for q in ("「", "」", "“", "”"):
        assert q not in ins, (
            f"instruction 里出现了引号 {q}：点名具体词的指令只适用于某一段，"
            "但它会作用于每一组")


def test_voice_prompt_is_not_news_anchor():
    """旧 prompt 里的「语速平稳」「新闻播报」＝直接指示产出播报腔。"""
    from app.services.cosyvoice import DEFAULT_VOICE_PROMPT as P
    # 「语速平稳」是纯负面指示，任何位置都不该出现
    assert "语速平稳" not in P, "音色描述里又出现「语速平稳」→ 又是平的"
    # 「新闻播报」只能出现在否定语境（如"避免…播音腔和新闻播报腔"），
    # 不能是描述目标（旧版写的是"适合历史纪录片旁白或新闻播报"）
    if "新闻播报" in P:
        assert "避免" in P or "不要" in P or "非" in P, (
            "音色描述把「新闻播报」当成了目标风格（应是否定语境）")


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


def test_cli_default_is_paragraph():
    kw = _cli_granularity_kw()
    assert kw, "没抓到 --tts-granularity 的定义，测试 spy 失效了"
    assert kw.get("default") == "paragraph", \
        f"CLI --tts-granularity default = {kw.get('default')!r}，应为 paragraph"


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
