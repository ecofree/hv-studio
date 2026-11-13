# -*- coding: utf-8 -*-
"""ASR 回检的判据：PER（读音错误率）而非 CER（字符错误率）。

为什么：ASR 会把「赴京策试」写成「复经测试」—— 读音完全相同，TTS 没读错，
但 CER 记 4 个错字。用它当判据会冤枉大量段落（实测 14 组里多数是同音字）。
PER 把两边转拼音再比，同音 = 无错。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.shared


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


def test_retired_cloud_asr_cannot_dispatch(tmp_path, monkeypatch):
    """An old setting cannot open an unbudgeted cloud request."""
    from app.services import asr_check as ac
    from app.config.config import get_settings
    monkeypatch.setattr(get_settings().get_profile().asr, 'backend', 'dashscope')
    with pytest.raises(ValueError, match='only local-whisper'):
        ac.transcribe(tmp_path/'unused.wav')


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
    (tmp_path / "x.wav").write_bytes(b"audio-fixture")
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


def test_check_unavailable_is_not_passed(tmp_path):
    """ASR 不可用必须返回 unavailable，不能冒充 passed。"""
    from app.services import asr_check as ac
    (tmp_path / "x.wav").write_bytes(b"audio-fixture")
    orig = ac.transcribe
    try:
        ac.transcribe = lambda w: None
        ok, why = ac.check(tmp_path / "x.wav", "随便一句原文。")
        assert not ok and "unavailable" in why
    finally:
        ac.transcribe = orig
