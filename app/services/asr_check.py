"""ASR is a transcript check, never a listening-quality approval."""
from __future__ import annotations

import difflib
import os
import uuid
from dataclasses import dataclass, asdict, field
from app.services.provenance import file_sha256, fingerprint
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
        import os as _os
        return _os.environ.get(name, default)


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
    # ⚠️ 2026-09-10 P0：原为固定文件名，paragraph 档 4 线程并发
    #    会互相覆盖 → 听写串台。改为 pid+uuid 唯一命名。
    p = (Path(tempfile.gettempdir())
         / f"hv_asr_16k_{os.getpid()}_{uuid.uuid4().hex[:8]}.wav")
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setframerate(sr)
        w.setsampwidth(2)
        w.writeframes(np.clip(data, -32768, 32767).astype("<i2").tobytes())
    return p


class ASRServiceError(RuntimeError):
    def __init__(self, status, request_id, code):
        self.request_id=request_id
        super().__init__(f"ASR status={status} request_id={request_id} code={code}")


class Transcript(str):
    request_id: str | None = None


def transcribe(wav: Path) -> str:
    """Recognize a derived 16k WAV; preserve errors and always remove the copy."""
    from app.config.config import get_settings
    backend=get_settings().get_profile().asr.backend
    if backend == "local-whisper":
        from app.services.local_asr import recognize
        result,path=recognize(wav)
        transcript=Transcript(result['text'])
        transcript.request_id=result['request_id']
        transcript.checker_version=result['checker_version']
        transcript.recognition_path=str(path.resolve())
        return transcript
    if backend != "dashscope":
        raise ValueError('unknown ASR backend: '+backend)
    from app.services import tts_group as G
    from app.services.cosyvoice import _dashscope_key
    import dashscope
    from dashscope.audio.asr import Recognition, RecognitionCallback
    data, sr = G.read_wav(wav)
    if len(data) < sr // 2:
        raise ValueError("audio_too_short")
    derived = _to_16k_wav(data, sr)
    try:
        dashscope.api_key = _dashscope_key()
        res = Recognition(model=_ASR_MODEL, callback=RecognitionCallback(),
                          format="wav", sample_rate=16000).call(file=str(derived))
        if res.status_code != 200:
            raise ASRServiceError(res.status_code,getattr(res,"request_id",None),getattr(res,"code",None))
        text = "".join(s.get("text", "") for s in ((res.output or {}).get("sentence") or []))
        if not text.strip():
            raise ValueError("empty_transcript")
        transcript=Transcript(text)
        transcript.request_id=getattr(res,"request_id",None)
        return transcript
    finally:
        derived.unlink(missing_ok=True)


def normalize_numbers(text: str) -> str:
    """Canonicalize explicit numbers, retaining magnitudes and decimal points."""
    from decimal import Decimal
    text = (text or '').translate(str.maketrans('萬億兩', '万亿两'))
    digits = dict(zip("零〇一二两三四五六七八九", [0,0,1,2,2,3,4,5,6,7,8,9]))
    units = {"十":10,"百":100,"千":1000,"万":10000,"亿":100000000}
    def integer(t):
        if t.isascii() and t.isdigit(): return int(t)
        if not any(c in units for c in t):
            return int("".join(str(digits.get(c,c)) for c in t))
        total=section=number=0
        for c in t:
            if c in digits or c.isdigit(): number=number*10+int(digits.get(c,c))
            elif units[c] < 10000:
                section+=(number or 1)*units[c]; number=0
            else:
                section+=number; number=0
                if c == "亿": total=(total+section)*units[c]; section=0
                else: total+=section*units[c]; section=0
        return total+section+number
    def conv(m):
        t=m.group()
        try:
            if re.fullmatch(r"[0-9.]+[万亿]?",t):
                factor=units.get(t[-1],1)
                return format((Decimal(t[:-1] if factor!=1 else t)*factor).normalize(),'f') if '.' in t else str(int(t[:-1] if factor!=1 else t)*factor)
            parts=re.split('[点.]',t)
            return str(integer(parts[0])) + ('.'+''.join(str(digits.get(c,c)) for c in parts[1]) if len(parts)>1 else '')
        except (ValueError, KeyError): return t
    return re.sub(r"[0-9零〇一二两三四五六七八九十百千万亿]+(?:[点.][0-9零〇一二两三四五六七八九]+)?[万亿]?", conv, text or "")


def phonetic_equivalent(reference, hypothesis):
    """Exact full-utterance syllable+tone equality only; no threshold approval.

    Arabic numerals remain canonical number tokens. This deliberately leaves
    ambiguous number pronunciations for listening review.
    """
    from pypinyin import lazy_pinyin, Style
    def key(text):
        return lazy_pinyin(_PUNCT.sub('', text), style=Style.TONE3, neutral_tone_with_five=True)
    return bool(reference.strip()) and key(reference) == key(hypothesis)


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
    clean = _PUNCT.sub("", normalize_numbers(text))
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


@dataclass
class QualityResult:
    status: str
    audio_sha256: str
    expected_sha256: str
    checker_version: str = "asr-content-v2"
    request_id: str | None = None
    findings: list = field(default_factory=list)
    error: str | None = None
    transcript: str | None = None
    note: str = ""
    recognition_path: str | None = None

    def __iter__(self):
        # Compatibility for old callers, with fail-closed semantics.
        yield self.status == "passed"
        yield self.note or self.error or self.status

    def to_dict(self): return asdict(self)

    def valid_for(self, wav, expected):
        return self.audio_sha256 == file_sha256(wav) and self.expected_sha256 == fingerprint(expected)


def check(wav: Path, expected: str, entities=(), *, prepared_transcript=None) -> QualityResult:
    result = QualityResult("unavailable", "", fingerprint(expected))
    try:
        result.audio_sha256 = file_sha256(wav)
        if not enabled():
            result.status = "disabled"
            return result
        hyp = transcribe(wav) if prepared_transcript is None else prepared_transcript
        if not hyp:
            raise RuntimeError("empty_or_unavailable_transcript")
        result.transcript = hyp
        result.request_id = getattr(hyp,"request_id",None)
        result.checker_version = getattr(hyp,"checker_version",result.checker_version)
        result.recognition_path = getattr(hyp,"recognition_path",None)
        refn, hypn = normalize_numbers(expected), normalize_numbers(hyp)
        if re.findall(r"\d+(?:\.\d+)?",refn) != re.findall(r"\d+(?:\.\d+)?",hypn):
            result.findings.append({"kind":"number", "expected":refn, "heard":hypn})
        for entity in entities:
            if expected.count(entity) != hyp.count(entity):
                result.findings.append({"kind":"entity", "text":entity, "review_required":True})
        units = r"(?:\d+(?:\.\d+)?)\s*(公里|千米|厘米|毫米|公斤|毫升|千克|平方公里|秒|分钟|小时|年|月|日|里|米|斤|克|升|岁|人|名|万|亿|%|％)"
        if re.findall(units,refn) != re.findall(units,hypn):
            result.findings.append({"kind":"unit"})
        if set(_LATIN.findall(hyp.lower())) - set(_LATIN.findall(expected.lower())):
            result.findings.append({"kind":"unexpected_latin", "message":"听写出现意外英文"})
        sm = difflib.SequenceMatcher(None, _PUNCT.sub('',refn), _PUNCT.sub('',hypn), autojunk=False)
        for tag,i,j,k,l in sm.get_opcodes():
            if tag in ("delete", "insert"):
                result.findings.append({"kind":"omission" if tag=="delete" else "insertion", "reference_range":[i,j], "heard_range":[k,l]})
        c,p = cer(refn,hypn), per(expected,hyp)
        if p > max_per(): result.findings.append({"kind":"pronunciation", "per":p})
        if phonetic_equivalent(expected, hyp):
            # A homophone in independent ASR is not a numeric omission by TTS.
            # This remains machine transcript QA, never a listening approval.
            result.findings = [f for f in result.findings if f['kind'] == 'entity']
        result.status = "failed" if result.findings else "passed"
        result.note = f"PER {p:.1%} (CER {c:.1%}); " + ', '.join(f.get('message',f['kind']) for f in result.findings)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.request_id = getattr(exc,"request_id",None)
    return result
