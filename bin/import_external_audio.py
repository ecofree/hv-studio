# -*- coding: utf-8 -*-
r"""外部解说音轨 → 逐帧 wav + 采样守恒绑定（外部音频集的可复现驱动）。

为什么要有这个脚本
------------------
`app/services/external_audio.py` 提供了四个环节（重采样 / 整轨 ASR / 帧对齐 /
逐帧切分 / 写绑定），但项目里**没有任何入口**调用它们：`narration import-voice`
是 per-unit TTS 缓存的搬运，v2 的 `tts` stage 只认 CosyVoice。外部音轨集
（caomin-01 实测）因此只能靠临时脚本驱动，下一个人做新集必须重写一遍。

本脚本只做编排与**更严的守门**，不改 external_audio 的任何算法：

  1. 源音轨 → 16kHz 单声道（external16k/），记 sha256
  2. 整轨 ASR（本地 faster-whisper，分段缓存命中即复用）
  3. 帧对齐（带状 DP，逐帧覆盖率）
  4. 逐帧切分 → episodes/<id>/audio/frame-XXX.wav
  5. 写 production/external-audio-binding.json

**比 caomin-01 更严的一步（重要）**
---------------------------------
caomin-01 的绑定里 238 帧有 116 帧是 0 采样（44 字节的空 wav），而
verify_binding 仍然通过 —— 采样守恒断言允许 0 长度片，于是「字幕跑到无人
说话的区段」这种真故障被绿灯放行（成因是该集 script_path 指向含发布文案的
完整 md，文案与口播正文高度重复，DP 把整条音轨匹配给了文案）。
本脚本在写绑定**之前**做硬断言：

  · 每个有台词的帧切出的时长必须 ≥ --min-frame-s（默认 0.30s）
  · 逐帧覆盖率均值必须 ≥ --min-coverage（默认 0.80）
  · 有台词却对不上语音的帧数必须为 0（external_audio 本身也会拒，这里先报清楚）

任一条不满足就以非零码退出且**不写绑定**，让故障停在可读的证据上。

用法
----
    .venv-core\Scripts\python.exe bin\import_external_audio.py \
        --ep caomin-02 \
        --src "resource/风趣历史.writing-DNA/scripts/audio/大唐草民录_第二集_完整解说音轨.wav" \
        --cache state/_caomin02_asr

    # 只看重采样+ASR+对齐的报告，不切分不落盘
    .venv-core\Scripts\python.exe bin/import_external_audio.py --ep ... --src ... --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.managers import load                                   # noqa: E402
from app.services.external_audio import (                      # noqa: E402
    ExternalAudioError, align_frames, cut_per_frame, recognize_track,
    resample_to_16k, verify_binding, write_binding,
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def repair_short_shots(rows, sr, min_ms=1600.0, pad_s=0.4):
    """把短于镜长下限的帧的时长从相邻帧借回来（总时长分毫不动）。

    为什么需要
    ----------
    真实语音边界切出来的帧不保证都 ≥ MIN_FRAMES（timing_assert：1.6s）。
    本集有 8 帧在 1.42~1.58s 之间（“未给”/“官府不管”这类短促台词），
    compose 的 assert_timing_or_raise 会直接拒绝出片。

    处置不是删帧、也不是拉长音频，而是**滑动帧间共享边界**：
    缺的毫秒从相邻有余量的帧借，还回去，总时长不变、采样守恒不变、
    每帧仍然对应同一段真实语音。借的量以毫秒计（实测 ≤ 660ms）。

    ⚠️ 时长必须按 `cut_per_frame` 的**实际口径**算，不能用 `end - start`：
    切分落片是按累计 end 游标切的（frame i 的落盘长度 = end_i − end_{i−1}，
    首帧从 0 起算），而 rows 里的 start 只用来给下一帧封顶
    （`stop = min(end+pad, next_start)`）。按 end−start 算会在有静音间隙的
    段落凭空多出一截，累计重建就会把整条尾部静音灌进最后一帧——
    实测把末帧撑到 115s。所以这里同样以 end 游标为真源，并把
    `start_{i+1}` 重置为 `end_i + pad`，让封顶不会误剪。
    """
    if not rows:
        return []
    # 复刻 cut_per_frame 的 stop 口径：stop_i = min(end_i + pad, start_{i+1})；
    # 末帧由 cut_per_frame 直接拉到音轨末尾，故按 Σstop 等价处理。
    stops = []
    for i, r in enumerate(rows):
        en = r['end'] + pad_s
        if i + 1 < len(rows):
            en = min(en, rows[i + 1]['start'])
        stops.append(en)
    durs = []
    prev = 0.0
    for s in stops:
        durs.append(s - prev)
        prev = s
    span_before = sum(durs)
    min_s = min_ms / 1000.0
    moves = []
    n = len(durs)
    for i in range(n):
        deficit = min_s - durs[i]
        if deficit <= 1e-9:
            continue
        got = 0.0
        for j in range(i - 1, -1, -1):
            if got >= deficit - 1e-9:
                break
            slack = durs[j] - min_s
            if slack <= 1e-9:
                continue
            take = min(slack, deficit - got)
            durs[j] -= take
            got += take
        k = i
        while got < deficit - 1e-9 and k + 1 < n:
            k += 1
            slack = durs[k] - min_s
            if slack <= 1e-9:
                continue
            take = min(slack, deficit - got)
            durs[k] -= take
            got += take
        # 借到多少都必须记回本帧：donor 已经减过了，不记账会让 Σ 时长缩水，
        # 末尾帧在累计重建时被灌进剩余量（实测把最后一帧撑到 115s）。
        durs[i] += got
        moves.append((rows[i]['frame'], round(durs[i] * 1000), round(got * 1000),
                      got >= deficit - 1e-9))
    cur = 0.0
    for i, (r, d) in enumerate(zip(rows, durs)):
        cur += d
        r['end'] = cur - pad_s
        r['start'] = cur - d if i else 0.0
    span_after = sum(durs)
    if abs(span_after - span_before) > 1e-6:
        raise ExternalAudioError(
            f"短镜修复破坏了总时长：{span_before:.3f}s -> {span_after:.3f}s")
    return moves


def main() -> int:
    ap = argparse.ArgumentParser(description="外部解说音轨 → 逐帧 wav + 绑定")
    ap.add_argument("--ep", required=True, help="episode id")
    ap.add_argument("--src", required=True, help="完整解说音轨 wav（24k/16k mono 16-bit PCM）")
    ap.add_argument("--cache", default="", help="ASR 分段缓存目录（默认 state/_<ep>_asr）")
    ap.add_argument("--min-frame-s", type=float, default=0.30,
                    help="单个旁白帧切出的最小时长（秒），低于此值判定为错位")
    ap.add_argument("--min-coverage", type=float, default=0.80,
                    help="逐帧 token 覆盖率均值下限")
    ap.add_argument("--min-shot-ms", type=float, default=1600.0,
                    help="镜长下限（毫秒），默认取 timing_assert 的 MIN_FRAMES=40@25fps；"
                         "短于此值的帧会向相邻帧借时长")
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不切分不写绑定")
    ap.add_argument("--no-frame-audio", action="store_true",
                    help="不回填 frames 的 audio_path / audio_duration_ms")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    if not src.is_file():
        log(f"[x] 源音轨不存在: {src}")
        return 2

    ep = load(args.ep)
    if not ep.frames:
        log(f"[x] episode {args.ep} 没有 frames，先跑 --stage script")
        return 2
    log(f"episode={args.ep} frames={len(ep.frames)}")

    ep_dir = ROOT / "episodes" / args.ep
    cache = Path(args.cache) if args.cache else (ROOT / "state" / f"_{args.ep}_asr")
    if not cache.is_absolute():
        cache = ROOT / cache

    # 1) 重采样
    res_dir = ep_dir / "production" / "external16k"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_path = res_dir / src.name
    log(f"重采样 {src.name} -> 16kHz 单声道")
    resample = resample_to_16k(src, res_path)
    log(f"  时长 {resample['duration_s']}s  漂移 {resample['drift_ms']}ms  "
        f"源 sha256 {resample['source_sha256'][:12]}")

    # 2) 整轨 ASR
    log(f"整轨 ASR（分段缓存 {cache}）")
    asr = recognize_track(res_path, cache_dir=cache, progress=log)
    log(f"  ASR 完成：{asr['chunk_count']} 段 / {len(asr['words'])} 词 / "
        f"{len(asr['segments'])} 段句 / 语音止于 {asr['speech_end']}s")

    # 3) 帧对齐
    log("帧对齐（带状 DP + 拼音模糊）")
    t0 = time.time()
    alignment = align_frames(ep.frames, asr["words"])
    log(f"  对齐 {time.time()-t0:.1f}s：accuracy={alignment['accuracy']} "
        f"matched={alignment['matched']}/{alignment['chars']} "
        f"旁白帧={alignment['narration_frames']} 空镜帧={alignment['bed_frames']}")

    rows = alignment["rows"]
    cov = [r["coverage"] for r in rows if r["kind"] == "narration"]
    mean_cov = sum(cov) / len(cov) if cov else 0.0
    unaligned = [r["frame"] for r in rows if r["kind"] == "narration" and r["start"] is None]
    log(f"  逐帧覆盖率：均值 {mean_cov:.4f}  最低 {min(cov) if cov else 0:.4f}  "
        f"覆盖率<0.5 的帧 {sum(1 for c in cov if c < 0.5)}")
    if unaligned:
        log(f"  [!] 有台词却对不上语音的帧 {len(unaligned)}：{unaligned[:10]}")

    # 3.5) 短镜修复：滑动帧间边界把 <1.6s 的帧补到下限（总时长不变）
    timed = [r for r in rows if r["start"] is not None]

    def _eff_ms(i):
        """复刻 cut_per_frame 的落盘长度：stop_i − stop_{i−1}，首帧从 0 起。"""
        pad = 0.4
        en = timed[i]['end'] + pad
        if i + 1 < len(timed):
            en = min(en, timed[i + 1]['start'])
        prev = 0.0 if i == 0 else min(timed[i - 1]['end'] + pad, timed[i]['start'])
        return (en - prev) * 1000

    short_before = [timed[i]["frame"] for i in range(len(timed))
                    if _eff_ms(i) < args.min_shot_ms]
    if short_before:
        moves = repair_short_shots(timed, resample["sample_rate"],
                                   args.min_shot_ms, pad_s=0.4)
        log(f"  短镜修复：{len(short_before)} 帧短于 {args.min_shot_ms:.0f}ms → "
            f"{[m[0] for m in moves]}")
        for fid, now_ms, moved_ms, full in moves:
            if full:
                log(f"    {fid}: 向相邻帧借 {moved_ms}ms → {now_ms}ms")
            else:
                log(f"    [x] {fid} 只借到 {moved_ms}ms → {now_ms}ms（相邻帧无余量）")
        still = [timed[i]["frame"] for i in range(len(timed))
                 if _eff_ms(i) < args.min_shot_ms - 1]
        if still:
            log(f"  [!] 修复后仍短于下限的帧：{still}")

    if args.dry_run:
        log("[dry-run] 未切分、未写绑定。逐帧前 8 行：")
        for r in rows[:8]:
            log(f"    {r['frame']} chars={r['chars']} hits={r['hits']} "
                f"cov={r['coverage']} start={r['start']} end={r['end']}")
        return 0

    # 4) 逐帧切分（外部切分会清空目标目录下的 *.wav，符合“旧 take 不复用”）
    log(f"逐帧切分 -> {ep_dir/'audio'}")
    cut = cut_per_frame(res_path, asr, alignment, ep_dir / "audio")

    # 5) 硬断言：任何“有台词但没切到声音”的帧都判定为错位，拒绝写绑定
    by_frame = {r["frame"]: r for r in cut["rows"]}
    short = [(r["frame"], r["duration_ms"]) for r in cut["rows"]
             if by_frame[r["frame"]]["kind"] == "narration" and r["duration_ms"] < args.min_frame_s * 1000]
    total_s = cut["total_samples"] / cut["sample_rate"]
    log(f"  切片 {len(cut['rows'])} 段，合计 {total_s:.2f}s（音轨 {resample['duration_s']}s）")
    fails = []
    if unaligned:
        fails.append(f"{len(unaligned)} 个有台词的帧对不上语音")
    if short:
        fails.append(f"{len(short)} 个旁白帧短于 {args.min_frame_s}s："
                     + ", ".join(f"{f}={ms}ms" for f, ms in short[:8]))
    if mean_cov < args.min_coverage:
        fails.append(f"逐帧覆盖率均值 {mean_cov:.4f} < {args.min_coverage}")
    if fails:
        log("[x] 拒绝写绑定（对齐不可信，先修剧本/音轨对应关系）：")
        for f in fails:
            log("    - " + f)
        return 3

    # 6) 写绑定并立即自校验
    path = write_binding(args.ep, resample=resample, asr=asr, alignment=alignment, cut=cut)
    chk = verify_binding(load(args.ep))
    if not chk["ok"]:
        log(f"[x] 绑定自校验失败：{chk['fails']}")
        return 3
    log(f"[OK] 绑定已写并通过自校验: {path}")
    log(f"     最短旁白帧 {min(r['duration_ms'] for r in cut['rows'] if by_frame[r['frame']]['kind']=='narration')}ms"
        f"  最长 {max(r['duration_ms'] for r in cut['rows'])}ms")

    # 7) 回填 episode frames 的 audio_path / audio_duration_ms
    #
    # 这一步以前是手工的，漏掉它就会踩两个坑：
    #   · shot_builder 对缺 audio_duration_ms 的帧静默按 3000ms 兜底（只告警），
    #     于是 1037.58s 的音轨被排成 885s 的时间轴；
    #   · compose 的 §6#5 会校验「磁盘 wav 真实时长 vs json 记录」，两者必须一致。
    # 时长取**磁盘 wav 实测**（compose 的同一口径，容差 120ms），不取绑定里的采样算术。
    if args.no_frame_audio:
        log("[skip] 未回填 frames 的音频字段（--no-frame-audio）")
        return 0
    import wave as _wave
    from app.managers import save as _save
    ep = load(args.ep)
    filled = 0
    for f in ep.frames:
        w = ep_dir / "audio" / f"{f.id}.wav"
        if not w.is_file():
            log(f"[x] {f.id} 缺逐帧 wav，无法回填时长：{w}")
            return 3
        with _wave.open(str(w), "rb") as h:
            ms = int(h.getnframes() / float(h.getframerate() or 1) * 1000)
        f.audio_path = str(w).replace("\\", "/")
        f.audio_duration_ms = ms
        filled += 1
    _save(ep)
    total = sum(f.audio_duration_ms for f in ep.frames)
    log(f"[OK] 回填 {filled} 帧的 audio_path/audio_duration_ms，合计 {total/1000:.2f}s")
    short = [f.id for f in ep.frames if f.audio_duration_ms < 1600]
    long_ = [f.id for f in ep.frames if f.audio_duration_ms > 8000]
    if short:
        log(f"     注意：{len(short)} 帧短于 1.6s（真实语音边界所致，非捏造）：{short}")
    if long_:
        log(f"     注意：{len(long_)} 帧长于 8s：{long_}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExternalAudioError as exc:
        log(f"[x] {exc}")
        sys.exit(2)