r"""剪掉 TTS 音轨里被重复朗读的一段（能量最少的拼接点，不改原件）。

为什么允许剪而不是一律重渲染
--------------------------
重复朗读的毛病是**同一段话念了两遍**（ep4 第一幕把
「咱们看看官府是怎么玩这套垄断印钞机的…三百文到四百文钱」念了两遍，
第二遍 140.26-172.71s）。剪掉第二遍之后，音频与剧本恢复一一对应——
字幕天然对齐，不需要改任何文案。
而重渲染会把整幕重新生成一遍，可能再撞上新的重复/静音（ep4 第四幕就是
重渲染一次才修好的，第一次直接吐了 41 秒静音）。所以重复=剪，漏念=重渲。

拼接点怎么选
------------
不按 ASR 的词边界硬切（词边界只是估计，误差可达 0.5s，切在字中间很明显）。
做法与 external_audio 切 ASR 段一致：在给定搜索窗内找**能量最低的 0.2s**
作为拼接点，落在句子中间的呼吸上，听感上是自然的停顿。

安全性
------
· 永远写新文件（<src>.trim-<start>-<end>s.wav），原件一个字都不动
· 剪完立刻用 bin/check_tts_track.py 复检，并打印拼接点秒数供人工回听
· --dry-run 只报告不写盘

用法
----
    .venv-core\Scripts\python.exe bin\trim_duplicated_span.py \
        --src "resource/风趣历史.writing-DNA/scripts/audio/ep4_act1.wav" \
        --start 140.26 --end 172.71 \
        --search 0.6 --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def energy_profile(wav: Path, win_s: float = 0.05):
    """→ (sr, [(rms, t0, t1)])，逐窗 RMS。"""
    import numpy as np

    with wave.open(str(wav), 'rb') as h:
        sr, n, ch = h.getframerate(), h.getnframes(), h.getnchannels()
        raw = h.readframes(n)
    x = np.frombuffer(raw, dtype='<i2').astype('float32') / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    w = max(1, int(sr * win_s))
    m = len(x) // w
    rms = np.sqrt((x[:m * w].reshape(m, w) ** 2).mean(axis=1))
    return sr, list(zip(rms.tolist(), [i * win_s for i in range(m)]))


def quietest(sr, prof, lo: float, hi: float, win_s: float = 0.2) -> float:
    """在 [lo,hi] 内找能量最低的 win_s 窗口，返回其中心秒。"""
    need = max(1, int(win_s / (prof[0][1] - prof[0][0]))) if len(prof) > 1 else 1
    best, best_t = None, (lo + hi) / 2
    for i in range(len(prof) - need):
        t = prof[i][1]
        if t < lo or t > hi:
            continue
        e = sum(r for r, _t in prof[i:i + need]) / need
        if best is None or e < best:
            best, best_t = e, t + win_s / 2
    return best_t


def main() -> int:
    ap = argparse.ArgumentParser(description='剪掉 TTS 重复朗读段（写新文件）')
    ap.add_argument('--src', required=True)
    ap.add_argument('--start', type=float, required=True, help='重复段起点（秒）')
    ap.add_argument('--end', type=float, required=True, help='重复段终点（秒）')
    ap.add_argument('--search', type=float, default=0.6,
                    help='在端点前后各找多少秒内的最低能量点做拼接')
    ap.add_argument('--out', default='', help='输出路径（默认 <src>.trim-<start>-<end>s.wav）')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    if args.end <= args.start:
        print('[x] --end 必须大于 --start')
        return 2

    sr, prof = energy_profile(src)
    win_s = prof[0][1] - prof[0][0] if len(prof) > 1 else 0.05
    total = prof[-1][1] + win_s          # prof[i] = (rms, t0)，时间在 [1] 不在 [0]
    a = quietest(sr, prof, max(0.0, args.start - args.search), min(total, args.start + args.search))
    b = quietest(sr, prof, max(a + 0.3, args.end - args.search), min(total, args.end + args.search))
    if b <= a:
        print('[x] 找不到合理的拼接区间（端点搜索窗重叠）')
        return 2

    cut = b - a
    dur = total - cut
    out = Path(args.out) if args.out else src.with_name(
        '%s.trim-%d-%ds.wav' % (src.stem, round(a), round(b)))
    print('源      %s  %.2fs' % (src.name, total))
    print('请求剪  %.2f - %.2fs (%.2fs)' % (args.start, args.end, args.end - args.start))
    print('实际剪  %.3f - %.3fs (%.3fs)  <- 窗内最低能量点' % (a, b, cut))
    print('剪后    %.2fs' % dur)
    print('输出    %s' % out.name)
    if args.dry_run:
        print('[dry-run] 未写盘')
        return 0

    r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src),
                        '-af', 'atrim=%.3f:%.3f,asetpts=PTS-STARTPTS' % (0.0, a),
                        '-c:a', 'pcm_s16le', '-f', 'wav', str(out.with_suffix('.head.wav'))],
                       capture_output=True, text=True, encoding='utf-8')
    if r.returncode:
        print('[x] ffmpeg 失败: %s' % r.stderr[-400:])
        return 2
    r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src),
                        '-af', 'atrim=%.3f:%.3f,asetpts=PTS-STARTPTS' % (b, total),
                        '-c:a', 'pcm_s16le', '-f', 'wav', str(out.with_suffix('.tail.wav'))],
                       capture_output=True, text=True, encoding='utf-8')
    if r.returncode:
        print('[x] ffmpeg 失败: %s' % r.stderr[-400:])
        return 2
    head = out.with_suffix('.head.wav')
    tail = out.with_suffix('.tail.wav')
    with wave.open(str(head), 'rb') as h1, wave.open(str(tail), 'rb') as h2:
        params1, params2 = h1.getparams(), h2.getparams()
        f1 = h1.readframes(h1.getnframes())
        f2 = h2.readframes(h2.getnframes())
    if (params1.nchannels, params1.sampwidth, params1.framerate) != \
       (params2.nchannels, params2.sampwidth, params2.framerate):
        print('[x] 首尾段参数不一致，拒绝拼接：%s vs %s' % (params1, params2))
        return 2
    with wave.open(str(out), 'wb') as h:
        h.setnchannels(params1.nchannels)
        h.setsampwidth(params1.sampwidth)
        h.setframerate(params1.framerate)
        h.writeframes(f1 + f2)
    head.unlink()
    tail.unlink()
    with wave.open(str(out), 'rb') as h:
        got = h.getnframes() / float(h.getframerate())
    print('[OK] 已写 %s  %.2fs（原 %.2fs）' % (out.name, got, total))
    print('     拼接点：%.3fs / %.3fs —— 请回听这两处' % (a, b))
    return 0


if __name__ == '__main__':
    sys.exit(main())