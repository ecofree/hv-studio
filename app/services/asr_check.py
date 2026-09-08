# -*- coding: utf-8 -*-
"""TTS 出片前的发音回检（ASR 听写 vs 原文）。

为什么要这个（2026-09-08 出片实测）：
  CosyVoice 的 `instruction`（自然语言风格指令）能大幅提升表现力
  （响度动态 std 从 5.65 提到 22.17），但**有个可复现的副作用**：
  当「段落首句 + 生僻专名 + 指令」三者同时成立时，模型会在进入角色时
  把首句念糊 —— 实测「尉迟恭在后院」→「尉迟恭**为**后院」、
  「李世民站在案几前」→「李世**宁**站在**案机**前。**接着**跳动的蜡烛」。
  （前面有句子时不触发，普通句段首也不触发 → 是段首效应。）

  同一个文本**重复合结果完全一致**，所以这不是随机噪声，能被稳定检出。

判定：去掉标点后算字符错误率 CER。实测标定：
    好（无指令）3.3%   坏（有指令糊了）8.2%   → 阈值 6% 干净分开。
⚠️ 绝不能因为回检失败就让出片失败 —— 校验失败只降级重合成，再失败就告警接受。
"""
from __future__ import annotations

import difflib
import re
import tempfile
import wave
from pathlib import Path

import numpy as np

_PUNCT = re.compile(r"[，。！？；：、…—\s“”‘’（）《》〈〉·,.!?;:\-]")
_LATIN = re.compile(r"[A-Za-z]{2,}")

# ASR 模型（paraformer 实时版，中文，带标点预测）
_ASR_MODEL = "paraformer-realtime-v2"


def _env(name: str, default: str) -> str:
    try:
        from app.config.config import env_get
        return env_get(name, default)
    except Exception:  # noqa: BLE001
        import os
        return os.environ.get(name, default)


def enabled() -> bool:
    return _env("HV_ASR_CHECK", "1").strip() not in ("0", "", "false", "False")


def downgrade_enabled() -> bool:
    """回检不合格时是否**自动降级重合成**（去掉指令重来）。

    默认 **关闭**：阈值目前没有标注数据可标定（2026-09-08 实测，
    「常何→常和」这种纯同音误判能算出 11.9%，比真读错的 4.9% 还高 ——
    说明离线构造的对照不可信）。贸然自动降级会让 1/4 的段落失去情感，
    代价远大于收益。先只告警，等有真实听感反馈再拿数据标定并开启。
    """
    return _env("HV_ASR_DOWNGRADE", "0").strip() not in ("0", "", "false", "False")


def _to_16k_wav(data: np.ndarray, sr: int) -> Path:
    """read_wav 返回的是 **±32768 整数标度**（不是 [-1,1]）。

    ⚠️ 这里绝不能做任何缩放/裁剪到 ±1 —— 2026-09-08 我正是这么写，
       把波形削成方波，导致后面所有 ASR 结论全错（"模型念出英文 No." 纯属自造）。
    """
    if sr != 16000:
        n = int(round(len(data) * 16000 / sr))
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
        sr = 16000
    p = Path(tempfile.gettempdir()) / "hv_asr_16k.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setframerate(sr)
        w.setsampwidth(2)
        w.writeframes(np.clip(data, -32768, 32767).astype("<i2").tobytes())
    return p


def transcribe(wav: Path) -> str | None:
    """听写一段 wav。失败返回 None（调用方必须容忍）。"""
    try:
        from app.services import tts_group as G
        data, sr = G.read_wav(wav)
        if len(data) < sr // 2:          # 短于 0.5s 不值得听
            return None
        from app.services.cosyvoice import _dashscope_key
        import dashscope
        from dashscope.audio.asr import Recognition, RecognitionCallback

        class _CB(RecognitionCallback):
            def on_open(self): pass
            def on_close(self): pass
            def on_event(self, r): pass
            def on_complete(self): pass
            def on_error(self, r): pass

        dashscope.api_key = _dashscope_key()
        r = Recognition(model=_ASR_MODEL, callback=_CB(),
                        format="wav", sample_rate=16000)
        res = r.call(file=str(_to_16k_wav(data, sr)))
        if res.status_code != 200:
            return None
        return "".join(s.get("text", "")
                       for s in ((res.output or {}).get("sentence") or []))
    except Exception:  # noqa: BLE001
        return None


def cer(ref: str, hyp: str) -> float:
    """字符错误率（去标点后）。

    ⚠️ 只用于**展示**：它分不清「TTS 读错」和「ASR 选了同音字」。
       例：「赴京策试」被听写成「复经测试」—— 读音完全相同，TTS 没错，
       但 CER 记 4 个错字。所以判定请用 :func:`per`（读音错误率）。
    """
    a, b = _PUNCT.sub("", ref or ""), _PUNCT.sub("", hyp or "")
    if not a:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    err = sum(max(i2 - i1, j2 - j1)
              for t, i1, i2, j1, j2 in sm.get_opcodes() if t != "equal")
    return err / len(a)


# 数字词：ASR 会把「十一月」写成「11月」、「三千五百」写成「3500」，
# 读音其实一致但字符/音节数都不同 → 比较前两边都剥掉，避免误判。
_NUM_TOKEN = re.compile(r"[0-9]+|[〇零一二三四五六七八九十百千万亿两0-9]+")


def _pinyin(text: str) -> list[str]:
    """汉字 → 拼音（含声调）。pypinyin 缺失时退化成「按字符比较」。

    数字词先剥掉：它们的读音在 ref/hyp 里表达形式不同（十一月 vs 11月），
    留着会制造大量假阳性（实测把「武德七年十一月」判成 4.7% 的错）。
    """
    clean = _NUM_TOKEN.sub("", _PUNCT.sub("", text or ""))
    try:
        from pypinyin import lazy_pinyin, Style
        return lazy_pinyin(clean, style=Style.TONE)
    except Exception:  # noqa: BLE001
        return list(clean)


def per(ref: str, hyp: str) -> float:
    """**读音**错误率（Pronunciation Error Rate）—— 真正的判据。

    把两边都转成拼音再比。好处：
      · 「赴京策试」vs「复经测试」→ 拼音相同 → 0 错（TTS 没错，只是 ASR 选字不同）
      · 「在(zài)」vs「为(wèi)」→ 拼音不同 → 真错（模型把首句念糊了）

    实测（2026-09-08）：坏样本 PER 4.9%，而「赴京策试→复经测试」这类
    纯同音误判被吸收成 0.0% —— 比 CER 判别力强得多。
    ⚠️ 但阈值**尚未标定**（见 max_per 的说明）。
    """
    a, b = _pinyin(ref), _pinyin(hyp)
    if not a:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    err = sum(max(i2 - i1, j2 - j1)
              for t, i1, i2, j1, j2 in sm.get_opcodes() if t != "equal")
    return err / len(a)


def has_latin(hyp: str) -> bool:
    """中文旁白里出现英文单词 = 明显异常（如破折号被念成 "No."）。"""
    return bool(_LATIN.search(hyp or ""))


def max_per() -> float:
    """PER 阈值。用于告警；只有开启 HV_ASR_DOWNGRADE 时才用于降级。

    ⚠️ 尚未标定：离线构造的对照文本不可信 —— 实测「常何→常和」这种
       纯同音误判能算出 11.9%，比真读错的 4.9% 还高。
       等有真实听感反馈（哪一段读错了）再拿数据重新标定，别照抄这个数。
    """
    try:
        return float(_env("HV_ASR_MAX_PER", "0.08"))
    except ValueError:
        return 0.08


def suspicious(hyp: str) -> bool:
    """**不依赖阈值**的硬信号：中文旁白里出现英文单词。

    破折号被念成 "No." 那一类靠它抓。
    """
    return has_latin(hyp)


def check(wav: Path, expected: str) -> tuple[bool, str]:
    """回检一段音频。返回 (是否合格, 说明)。

    回检自身失败（ASR 挂了/没网）时返回 True —— 校验不能阻断出片。
    """
    if not enabled():
        return True, "回检已关闭"
    hyp = transcribe(wav)
    if hyp is None:
        return True, "ASR 不可用，跳过回检"
    c = cer(expected, hyp)
    p = per(expected, hyp)
    if has_latin(hyp):
        return False, f"听写出现英文串（PER {p:.1%}）：{hyp[:60]}"
    # 判据用 PER（读音），不用 CER（字符）—— CER 会把 ASR 的同音字选字
    # 误判成 TTS 读错，导致大量不必要的降级重合成（实测 14 组里多数是冤枉的）。
    if p > max_per():
        return False, (f"PER {p:.1%} > {max_per():.1%}"
                       f"（CER {c:.1%}）：{hyp[:60]}")
    return True, f"PER {p:.1%}（CER {c:.1%}）"
