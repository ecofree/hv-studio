# -*- coding: utf-8 -*-
"""ASR 回检的判据：PER（读音错误率）而非 CER（字符错误率）。

为什么：ASR 会把「赴京策试」写成「复经测试」—— 读音完全相同，TTS 没读错，
但 CER 记 4 个错字。用它当判据会冤枉大量段落（实测 14 组里多数是同音字）。
PER 把两边转拼音再比，同音 = 无错。
"""
from __future__ import annotations

import pytest


def test_identical_pronunciation_is_not_an_error():
    """同音字选字不同 → 不算错。"""
    from app.services.asr_check import per
    assert per("并听自举，赴京策试。", "并听自举复经测试。") == pytest.approx(0.0)


def test_number_words_are_normalized():
    """「十一月」vs「11月」：数字词表达形式不同，读音一致 → 不算错。"""
    from app.services.asr_check import per
    assert per("武德七年十一月，北风呼啸。", "武德七年11月。北风呼啸。") == pytest.approx(0.0)


def test_real_misread_registered():
    """真读错（"在"→"为"、多出"呢"）PER 必须 > 0；这是判据有效性的下限。

    ⚠️ 不断言具体阈值：阈值尚未标定（见 asr_check.max_per 的说明），
       钉死一个数会让将来标定变成改测试而不是改认知。
    """
    from app.services.asr_check import per
    v = per(
        "尉迟恭在后院擦拭着马槊上的油泥，磨刀石上的白水，泛着刺鼻的铁锈味。"
        "李世民站在案几前，借着跳动的蜡烛，亲笔写下了次日清晨递给父亲的奏折。",
        "尉迟恭为后院擦拭着马硕上的油泥，磨刀石上的白水泛着刺鼻的铁锈味。"
        "李世民呢站在暗记前，借着跳动的蜡烛，亲笔写下了次日清晨递给父亲的奏折。")
    assert v > 0.0, "真读错却算出 0，拼音比对失效"


def test_downgrade_off_by_default():
    """自动降级默认必须关闭 —— 阈值没标定就降级会毁掉大片情感。"""
    from app.services import asr_check as ac
    assert ac.downgrade_enabled() is False
    assert ac.enabled() is True      # 回检本身仍开着（只告警）


def test_per_below_cer_on_homophone_noise():
    """同音字噪声下 PER 应显著低于 CER（这是换 PER 的全部意义）。"""
    from app.services.asr_check import cer, per
    ref = "唐会要卷七十四白纸黑字记着这道命令，诸州学士及闲资，明闲政术，赴京策试。"
    hyp = "唐慧耀卷七十四。白纸黑字记着这道命令。株周学士及贤资，明贤正述，复经测试。"
    assert per(ref, hyp) < cer(ref, hyp) / 2


def test_latin_words_flagged():
    from app.services.asr_check import has_latin
    assert has_latin("九匹战马 No. 嘴里塞着木棍")
    assert not has_latin("九匹战马嘴里塞着木棍")


def test_check_accepts_good_and_rejects_bad(tmp_path):
    """check() 本身：好样本通过、坏样本不通过（用桩替换 transcribe）。"""
    from app import services
    from app.services import asr_check as ac

    ref = "尉迟恭在后院擦拭着马槊上的油泥，磨刀石上的白水，泛着刺鼻的铁锈味。"
    orig = ac.transcribe
    try:
        # 英文串是**不依赖阈值**的硬信号（破折号被念成 No. 那一类）
        ac.transcribe = lambda w: "尉迟恭在后院擦拭着马硕上的油泥。No. 泛着铁锈味。"
        ok, why = ac.check(tmp_path / "x.wav", ref)
        assert not ok and "英文" in why

        ac.transcribe = lambda w: ref            # 完全一致
        ok, why = ac.check(tmp_path / "x.wav", ref)
        assert ok
    finally:
        ac.transcribe = orig


def test_check_never_blocks_when_asr_unavailable(tmp_path):
    """ASR 挂了不能阻断出片。"""
    from app.services import asr_check as ac
    orig = ac.transcribe
    try:
        ac.transcribe = lambda w: None
        ok, why = ac.check(tmp_path / "x.wav", "随便一句原文。")
        assert ok and "跳过" in why
    finally:
        ac.transcribe = orig

def test_spoken_text_has_no_ssml_tags():
    """回检的期望文本不能含 SSML 标签（2026-09-10 P0 回归锁）。

    曾把 build_group_ssml() 输出（含 <speak>/<phoneme>）当期望文本 →
    标签占 ~70% 字符 → 音频全对也 PER 15.8% > 8% → 每组误判失败 →
    无谓重合成（TTS 成本 ×3）+ 刷屏告警。
    """
    from app.services.voice import _group_spoken_text
    spoken = _group_spoken_text(["第一句测试。", "第二句测试。"])
    assert "<speak" not in spoken, "朗读文本里混进了 SSML 标签"
    assert "phoneme" not in spoken
    assert "第一句测试。第二句测试。" in spoken


def test_spoken_text_applies_replace():
    """replace 后的文本才是 TTS 真正念的（晋阳令→晋阳县令）。"""
    from app.services.voice import _group_spoken_text
    spoken = _group_spoken_text(["李渊暗中联络晋阳令刘文静。"])
    assert "晋阳县令" in spoken, (
        "replace 没生效 → ASR 会按『晋阳令』比对，与真实读音对不上")


def test_asr_tempfile_is_unique_per_call():
    """ASR 临时文件必须唯一（4 线程并发踩踏 = 听写串台，2026-09-10 P0）。"""
    import inspect
    from app.services import asr_check as AC
    src = inspect.getsource(AC)
    assert "uuid" in src, "临时文件未加 uuid —— 并发会互相覆盖"
    assert '"hv_asr_16k.wav"' not in src, "仍是固定文件名"
