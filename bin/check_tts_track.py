r"""TTS 音轨体检：在交给流水线之前，先找出「模型重复念了」的段落。

为什么要有这个脚本
------------------
Gemini TTS 在长文本上会**重复朗读一整段**（ep4 第一幕把
「咱们看看官府是怎么玩这套垄断印钞机的…三百文到四百文钱」整段念了两遍，
第二遍在 142.1-168.8s；ep4 第四幕更严重，直接吐了 41 秒静音当代替）。
这两种故障**都不报错、不缺文件、时长也接近合理值**，靠「看时长对不对」
抓不住；等进了流水线，只能靠逐帧对齐反推，而那已经是在浪费一轮生图预算。

本脚本做三件事（全部本地、零成本）
----------------------------------
1. 静音扫描：列出 > --min-silence 的静音段（漏念/返回静音）
2. 重复检测：对 ASR 文本做自相似扫描，列出 >= --min-dup 字的重复朗读段
   （按时间给出两处的起止，方便直接回听）
3. 字数核对：把 ASR 字数与剧本字数对比（差太多 = 漏念或多念）

用法
----
    .venv-core\Scripts\python.exe bin\check_tts_track.py \
        --src "resource/风趣历史.writing-DNA/scripts/audio/ep4_act1.wav" \
        --text "resource/风趣历史.writing-DNA/scripts/大唐草民录_第四集_Gemini_TTS专版.md" \
        --act 1
    # 合并轨（整集）也要能查：
    .venv-core\Scripts\python.exe bin\check_tts_track.py --src ..._完整解说音轨.wav --acts 4

退出码：0 = 无重复、无长静音；2 = 发现问题（把清单打出来）。
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.external_audio import recognize_track, resample_to_16k  # noqa: E402


def act_texts(md: Path) -> list[str]:
    t = md.read_text(encoding='utf-8')
    pat = re.compile(r'###\s*【(第.幕[：:][^】]+)】.*?```text\s*\n(.*?)\n```', re.S)
    return [re.sub(r'\s+', '', re.sub(r'\[[^\]]*\]', '', b)) for _t, b in pat.findall(t)]


def silences(wav: Path, min_s: float, noise_db: float = -45.0) -> list[tuple[float, float]]:
    import subprocess

    ff = 'ffmpeg'
    r = subprocess.run([ff, '-v', 'info', '-i', str(wav), '-af',
                        f'silencedetect=noise={noise_db}dB:d={min_s}', '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = []
    cur = None
    for line in (r.stderr or '').splitlines():
        if 'silence_start' in line:
            cur = float(re.search(r'silence_start:\s*([\d.]+)', line).group(1))
        elif 'silence_end' in line and cur is not None:
            out.append((cur, float(re.search(r'silence_end:\s*([-\d.]+)', line).group(1))))
            cur = None
    return out


def duplicates(words: list[dict], min_chars: int) -> list[tuple[float, float, float, str]]:
    """在词序列上找重复朗读：同一段文字在两个不同时间各出现一次。

    用 k-gram 哈希定位，不走 difflib.find_longest_match —— 后者在
    ``alo=0, blo=n`` 这类边界上会 IndexError（b[n]），而重复段恰好常常
    一头一尾贴着边界。
    """
    txt = ''.join(w['word'] for w in words)
    starts = []
    for w in words:
        starts.extend([w['start']] * len(w['word']))
    n = len(txt)
    if n < min_chars * 2:
        return []
    k = min_chars
    first: dict[str, int] = {}
    hits: list[tuple[int, int]] = []
    for i in range(n - k + 1):
        g = txt[i:i + k]
        j = first.get(g)
        if j is None:
            first[g] = i
        elif i - j >= k:
            hits.append((j, i))
    # 合并相邻命中，向两侧贪心延长
    merged: list[list[int]] = []
    for j, i in hits:
        if merged and j <= merged[-1][1] + 8:
            merged[-1][1] = i
        else:
            merged.append([j, i])
    out = []
    for j, i in merged:
        a, b = j, i
        while a > 0 and b > 0 and txt[a - 1] == txt[b - 1]:
            a -= 1
            b -= 1
        end_a, end_b = i + k, i + k
        while end_a < n and end_b < n and txt[end_a] == txt[end_b]:
            end_a += 1
            end_b += 1
        out.append((starts[a], starts[min(end_a, n) - 1], starts[b], txt[a:end_a]))
    out.sort(key=lambda x: -len(x[3]))
    return out[:5]


def main() -> int:
    ap = argparse.ArgumentParser(description='TTS 音轨体检：静音 / 重复朗读 / 字数')
    ap.add_argument('--src', required=True, help='要检查的 wav（分幕或合并轨）')
    ap.add_argument('--text', default='', help='Gemini TTS 专版 md（提供则做字数核对）')
    ap.add_argument('--act', type=int, default=0, help='--text 里第几幕（1 起）')
    ap.add_argument('--cache', default='', help='ASR 分段缓存目录')
    ap.add_argument('--min-silence', type=float, default=2.0)
    ap.add_argument('--min-dup', type=int, default=30, help='重复判定的最小字数')
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    problems = 0

    sil = silences(src, args.min_silence)
    if sil:
        problems += len(sil)
        print('[静音] %d 段（>%.1fs）:' % (len(sil), args.min_silence))
        for a, b in sil:
            print('    %.1f - %.1fs  (%.1fs)' % (a, b, b - a))
    else:
        print('[静音] 无 >%.1fs 的静音段' % args.min_silence)

    cache = Path(args.cache) if args.cache else (ROOT / 'state' / '_tts_check')
    cache.mkdir(parents=True, exist_ok=True)
    res = ROOT / 'state' / '_tts_check' / (src.stem + '_16k.wav')
    resample_to_16k(src, res)
    asr = recognize_track(res, cache_dir=cache)
    with wave.open(str(res), 'rb') as h:
        dur = h.getnframes() / float(h.getframerate())
    heard = ''.join(w['word'] for w in asr['words'])
    print('[时长] %.1fs | ASR 识别 %d 字（%.0f 字/分钟）'
          % (dur, len(heard), len(heard) / dur * 60))

    if args.text:
        md = Path(args.text)
        if not md.is_absolute():
            md = ROOT / md
        texts = act_texts(md)
        want = texts[args.act - 1] if args.act else ''.join(texts)
        ratio = len(heard) / len(want) if want else 0
        print('[字数] 剧本 %d 字 / 音轨 %d 字 = %.2f' % (len(want), len(heard), ratio))
        # ⚠️ ASR 本身会丢字（本地 faster-whisper small 尤其爱输出繁体与近音错字），
        # 所以字数比只是**提示**，不能单独定罪：ep4 第二/三幕实测 0.90-0.91 却内容完整。
        # 真正的判据是下面两项——长静音（漏念）与重复朗读（多念）。
        if ratio and (ratio < 0.85 or ratio > 1.15):
            problems += 1
            print('    [!] 字数比偏离 ±15%%，疑似漏念/多念（需结合下面两项判断）')

    dup = duplicates(asr['words'], args.min_dup)
    if dup:
        problems += len(dup)
        print('[重复] %d 处（>=%d 字）：' % (len(dup), args.min_dup))
        for t0, t1, u0, text in dup:
            print('    %.1f-%.1fs 与 %.1fs 起重复：%s' % (t0, t1, u0, text[:60]))
    else:
        print('[重复] 无 >=%d 字的重复朗读' % args.min_dup)

    print()
    print('结论：%s' % ('发现问题，需重渲染' if problems else '音轨可用'))
    return 2 if problems else 0


if __name__ == '__main__':
    sys.exit(main())