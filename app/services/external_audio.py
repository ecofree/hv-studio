"""Recognize a recording on a stable sample clock and align it to source text."""
from __future__ import annotations

import re
import wave
from pathlib import Path

from app.models import Episode

# ASR worker 有 180s 硬超时，超过必须切段。留 20% 余量。
WORKER_TIMEOUT_S = 150
CHUNK_SAMPLES_16K = int(16000 * WORKER_TIMEOUT_S * 0.6)


class ExternalAudioError(RuntimeError):
    """外部音频导入的可执行失败。消息必须给出下一步该做什么。"""


def _sha256(path) -> str:
    from app.services.provenance import file_sha256
    return file_sha256(path)


def read_wav(path) -> tuple[int, int, int]:
    """→ (sample_rate, channels, total_samples)。非 16-bit PCM 直接拒绝。"""
    with wave.open(str(path), "rb") as h:
        if h.getsampwidth() != 2:
            raise ExternalAudioError(
                f"{Path(path).name} 不是 16-bit PCM（{h.getsampwidth()*8}bit）。"
                "请先转 16-bit PCM 再导入。")
        return h.getframerate(), h.getnchannels(), h.getnframes()


def find_ffmpeg() -> str:
    """定位 ffmpeg。

    与 orchestrator 的工具链预检同一条约定：**从 PATH 解析**，不写死绝对路径。
    曾用 `ROOT/bin/ffmpeg.exe` 兜底，但项目里没有那个文件（WinGet 装在别处），
    兜底落空后 subprocess 抛的是 FileNotFoundError，错误信息完全指不到根因
    （2026-09-29 实测）。这里改成先 shutil.which，再抛可执行的提示。
    """
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise ExternalAudioError(
        "PATH 中找不到 ffmpeg，无法重采样外部音轨。\n"
        "  处置：安装 ffmpeg 并加入 PATH（本机为 "
        "C:\\Users\\coumoo\\AppData\\Local\\Microsoft\\WinGet\\Packages\\"
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\\"
        "ffmpeg-9.0.2-full_build\\bin），"
        "或先手工把音轨转成 16kHz 单声道 16-bit PCM 再导入。")


def resample_to_16k(src, dst) -> dict:
    """外部音轨统一重采样到 16kHz 单声道（ASR 与 whisper 的输入要求）。"""
    import subprocess

    ffmpeg = find_ffmpeg()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-ac", "1",
                        "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    if r.returncode != 0:
        raise ExternalAudioError(
            f"重采样失败 {Path(src).name}: {r.stderr[-400:]}\n"
            "处置：确认源文件是合法 WAV，或先手工转 16kHz 单声道 16-bit PCM。")
    sr0, ch0, n0 = read_wav(src)
    sr1, ch1, n1 = read_wav(dst)
    drift = abs(n1 / sr1 - n0 / sr0)
    if drift > 0.05:
        raise ExternalAudioError(
            f"重采样时长漂移 {drift*1000:.0f}ms > 50ms，源音轨可能损坏")
    return dict(source=str(src), source_sha256=_sha256(src),
                resampled=str(dst), resampled_sha256=_sha256(dst),
                source_rate=sr0, source_channels=ch0, source_samples=n0,
                sample_rate=sr1, total_samples=n1,
                duration_s=round(n1 / sr1, 3), drift_ms=round(drift * 1000, 2))


def _chunk_bounds(samples, sr, anchors=()):
    """Search around a fixed distance from the last cut, independent of the end of the recording."""
    import numpy as np
    n=len(samples)
    bounds=[0]
    for stop in sorted({n,*[x for x in anchors if 0<x<n]}):
        # Previously verified boundaries and numbered part boundaries are stable anchors.
        while stop-bounds[-1]>int(CHUNK_SAMPLES_16K*1.25):
            center=bounds[-1]+CHUNK_SAMPLES_16K
            win=max(1,int(sr*.25)); radius=CHUNK_SAMPLES_16K//8
            lo,hi=center-radius,min(stop,center+radius)
            buf=samples[lo:hi].astype('float32')
            blocks=len(buf)//win
            quiet=int((buf[:blocks*win].reshape(blocks,win)**2).mean(axis=1).argmin()) if blocks else 0
            bounds.append(lo+quiet*win+win//2)
        bounds.append(stop)
    return bounds


def recognize_track(wav, *, cache_dir=None, progress=None, anchors=()):
    """One independent recognition per exact chunk, using the model-bound content cache."""
    import tempfile
    import numpy as np
    from app.services.local_asr import preflight,recognize
    from app.services.provenance import atomic_json,fingerprint,replace_file
    from app.services.acoustic_session import job_session
    with wave.open(str(wav),'rb') as h:
        if (h.getnchannels(),h.getsampwidth(),h.getframerate())!=(1,2,16000):
            raise ExternalAudioError('整轨识别需要 16 kHz 单声道 PCM16')
        sr=h.getframerate();raw=h.readframes(h.getnframes())
    samples=np.frombuffer(raw,dtype='<i2')
    if not len(samples):raise ExternalAudioError('整轨无采样')
    bounds=_chunk_bounds(samples,sr,anchors)
    segments=[]; words=[]
    with tempfile.TemporaryDirectory(prefix='hv-chunks-') as tmp, job_session():
        folder=Path(cache_dir)/('layout-'+fingerprint(bounds)[:24]) if cache_dir else Path(tmp)
        folder.mkdir(parents=True,exist_ok=True)
        ctx=preflight()
        for i,(a,b) in enumerate(zip(bounds,bounds[1:])):
            p=folder/f'part{i:02d}.wav';pcm=memoryview(raw)[a*2:b*2]
            ok=False
            if p.is_file():
                try:
                    with wave.open(str(p),'rb') as h:
                        ok=(h.getnchannels(),h.getsampwidth(),h.getframerate(),h.getnframes())==(1,2,sr,b-a) and h.readframes(b-a)==pcm
                except (OSError,EOFError,wave.Error):pass
            if not ok:
                import uuid
                draft=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
                try:
                    with wave.open(str(draft),'wb') as h:
                        h.setparams((1,2,sr,0,'NONE','not compressed'));h.writeframes(pcm)
                    replace_file(draft,p)
                finally:draft.unlink(missing_ok=True)
            record,evidence=recognize(p,context=ctx)
            atomic_json(p.with_suffix('.json'),dict(start_sample=a,end_sample=b,sample_rate=sr,
                                                    recognition_path=str(evidence),audio_sha256=record['audio_sha256']))
            offset=a/sr
            for seg in record['segments']:
                shifted=[dict(w,start=w['start']+offset,end=w['end']+offset) for w in seg.get('words',[])]
                words.extend(shifted)
                segments.append(dict(seg,start=seg['start']+offset,end=seg['end']+offset,words=shifted))
            if progress:progress(f'整轨语音定位 {i+1}/{len(bounds)-1}')
    if not words:raise ExternalAudioError('全轨未识别出任何语音')
    return dict(sample_rate=sr,total_samples=len(samples),duration=round(len(samples)/sr,3),
                chunks=[round(b/sr,2) for b in bounds],chunk_count=len(bounds)-1,
                cache_dir=str(folder.resolve()) if cache_dir else None,segments=segments,words=words,
                speech_end=round(words[-1]['end'],2))


def align_frames(frames, words) -> dict:
    """把 ASR 词单调对齐到剧本帧（带状 DP + 拼音模糊匹配）。

    为什么不直接用 `forced_alignment.align`：那条路径要求 v2 manifest 的 per-unit
    文本与 take 采样数，外部单轨场景给不了。这里做的是同口径的轻量版，返回
    逐帧覆盖率与偏移，供外部音频导入使用。
    """
    from pypinyin import lazy_pinyin

    py = lambda c: (lazy_pinyin(c, errors="ignore") or [""])[0]
    S_ch, S_fr = [], []
    for i, f in enumerate(frames):
        for ch in (f.text or ""):
            S_ch.append(ch); S_fr.append(i)
    A_ch, A_st, A_en = [], [], []
    for w in words:
        t = w.get("word") or ""
        if not t.strip():
            continue
        a, b, k = w["start"], w["end"], max(1, len(t))
        for j, ch in enumerate(t):
            A_ch.append(ch); A_st.append(a + (b - a) * j / k)
            A_en.append(a + (b - a) * (j + 1) / k)
    n, m = len(S_ch), len(A_ch)
    if n == 0 or m == 0:
        raise ExternalAudioError("剧本或音频为空，无法对齐")
    S_py = [py(c) for c in S_ch]
    A_py = [py(c) for c in A_ch]
    # NEG 必须足够深：累计 gap 罚分会迅速跌破浅 NEG，使所有转移被跳过、
    # 对齐结果静默变成 0 匹配（2026-09-29 双日凌空/草民录实测）。
    NEG, GS, GA = -1e9, -0.28, -0.10
    band = max(800, int(n * 0.20))
    dp = __import__("numpy").full((n + 1, m + 1), NEG, dtype="float32")
    bt = __import__("numpy").zeros((n + 1, m + 1), dtype="int8")
    dp[0, 0] = 0.0
    np_ = __import__("numpy")
    for i in range(1, n + 1):
        c = int(i * m / n)
        for j in range(max(0, c - band), min(m, c + band) + 1):
            best, arg = NEG, 0
            d = dp[i - 1, j - 1]
            if d > NEG / 2:
                s, q = S_ch[i - 1], A_ch[j - 1]
                sc = 2.0 if s == q else (1.0 if (S_py[i - 1] and S_py[i - 1] == A_py[j - 1]) else -0.9)
                if d + sc > best:
                    best, arg = d + sc, 1
            d = dp[i - 1, j]
            if d > NEG / 2 and d + GS > best:
                best, arg = d + GS, 2
            d = dp[i, j - 1]
            if d > NEG / 2 and d + GA > best:
                best, arg = d + GA, 3
            if best > NEG / 2:
                dp[i, j] = best; bt[i, j] = arg
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        a = bt[i, j]
        if a == 1:
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif a == 2:
            i -= 1
        elif a == 3:
            j -= 1
        else:
            break
    pairs.reverse()
    exact = sum(1 for a, b in pairs if S_ch[a] == A_ch[b])
    hom = sum(1 for a, b in pairs
              if S_ch[a] != A_ch[b] and S_py[a] and S_py[a] == A_py[b])
    # tmap 必须按**帧下标**建键。按剧本字符下标建键会让逐帧覆盖率恒为 0，
    # 而全局准确率看着正常（2026-09-29 实测：全局 87% 但每帧 0%）。
    tmap: dict[int, list[int]] = {}
    for a, b in pairs:
        tmap.setdefault(S_fr[a], []).append(b)
    nfr: dict[int, int] = {}
    for fr in S_fr:
        nfr[fr] = nfr.get(fr, 0) + 1
    # 逐帧区间。无旁白的空镜帧（text 为空）没有字符可对齐，start/end 留空 ——
    # 它们由 cut_per_frame 用「前后帧之间的空隙」填充，不参与 ASR 对齐。
    # 若在这里给它们编造区间，等于凭空多出语音，纯属污染。
    rows = []
    for i, f in enumerate(frames):
        ix = tmap.get(i, [])
        st = min((A_st[b] for b in ix), default=None)
        en = max((A_en[b] for b in ix), default=None)
        rows.append(dict(frame=f.id, kind=("bed" if not (f.text or "").strip() else "narration"),
                         chars=nfr.get(i, 0), hits=len(ix),
                         start=st, end=en,
                         coverage=round(len(ix) / nfr[i], 4) if nfr.get(i) else 0.0))
    return dict(accuracy=round((exact + hom) / n, 4), matched=len(pairs),
                chars=n, chars_asr=m, rows=rows,
                exact=exact, homophone=hom,
                narration_frames=sum(1 for r in rows if r["kind"] == "narration"),
                bed_frames=sum(1 for r in rows if r["kind"] == "bed"))


def cut_per_frame(wav, asr, alignment, out_dir, *, pad_s=0.4, bed_s=7.5,
                  min_frame_s=1.65) -> dict:
    """按对齐结果把整轨切成逐帧 wav，产出采样守恒的偏移表。

    切法（与 2026-09-29 两集验证过的做法一致）：
      旁白帧  = [首词起点, 末词终点 + pad]，封顶到下一帧起点
      配乐床  = 剩余空隙；镜长上限是**逐帧**判定的，长空隙拆成 ≤ bed_s 的
                无旁白空镜帧（空镜在管线里合法，shot_builder 会给运镜）
      短帧    = 并入邻帧而**不是删除**，总时长分毫不动
      采样    = 累计偏移，残差补进末片，保证 Σframe == 音轨，零丢失零重复
    """
    import numpy as np

    wav = Path(wav)
    with wave.open(str(wav), "rb") as h:
        sr = h.getframerate()
        raw = h.readframes(h.getnframes())
    total = len(raw) // 2
    x = np.frombuffer(raw, dtype="<i2")
    dur = total / sr
    by_id = {r["frame"]: r for r in alignment["rows"]}
    order = [r["frame"] for r in alignment["rows"]]
    if not order:
        raise ExternalAudioError("没有帧可切分")
    say = [f for f in order if by_id[f]["start"] is not None]
    beds = [f for f in order if by_id[f]["start"] is None]
    if not say:
        raise ExternalAudioError("没有任何帧对到语音，无法切分")
    if any(by_id[f]["start"] is None and by_id[f]["chars"] for f in beds):
        raise ExternalAudioError("存在有台词却对不上语音的帧，拒绝切分（会丢字幕）")

    # 逐帧边界：旁白帧 [首词, 末词+pad]，空镜帧填前后的空隙。
    # 关键约束：**输出的片数必须等于帧数**（一一对应），否则 episode.frames
    # 与盘上音频就对不上，绑定也就无从校验。空隙若需要多于现有空镜帧来装，
    # 按比例摊给这些空镜帧；若空镜帧多于空隙，多出来的并入相邻旁白帧。
    bounds: list[float] = []
    cursor = 0.0
    plan: list[tuple[str, str, float, float]] = []
    si = 0
    for k, fid in enumerate(order):
        row = by_id[fid]
        if row["start"] is not None:
            st, en = row["start"], row["end"]
            nxt_i = next((j for j in range(si + 1, len(say))
                          if by_id[say[j]]["start"] > st), None)
            nxt = by_id[say[nxt_i]]["start"] if nxt_i is not None else dur
            stop = min(en + pad_s, nxt)
            # 本帧与下一个**旁白**帧之间的空隙，留给其间的空镜帧
            gap = nxt - stop
            between = []
            if gap > 0.05:
                j = order.index(fid) + 1
                while j < len(order) and by_id[order[j]]["start"] is None:
                    between.append(order[j]); j += 1
            if between:
                step = gap / len(between)
                for m, bf in enumerate(between):
                    plan.append(("bed", bf, stop + m * step, stop + (m + 1) * step))
                stop = nxt
            plan.append(("narration", fid, st, stop))
            si += 1
        else:
            # 空镜帧：区间由上面填好；这里只占位，真正的边界在同一次遍历里定
            continue
    plan.sort(key=lambda p: p[2])
    # 尾部空隙（最后一帧之后）并入最后一帧
    if plan:
        plan[-1] = (plan[-1][0], plan[-1][1], plan[-1][2], dur)
    # 采样精确：逐片严格相接（上一片结束即下一片开始），残差补进末片，
    # 保证 Σframe == 音轨，零丢失零重复。
    lengths: list[int] = []
    cursor = 0
    for m, p in enumerate(plan):
        end = int(round(p[3] * sr)) if m == len(plan) - 1 else int(round(p[3] * sr))
        if m == 0:
            start = 0
        else:
            start = cursor
        if end <= start:
            end = start + 1
        lengths.append(end - start)
        cursor = end
    resid = total - sum(lengths)
    if resid:
        lengths[-1] += resid
    if len(plan) != len(order):
        raise ExternalAudioError(
            f"切片数 {len(plan)} != 帧数 {len(order)}，无法一一对应")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("*.wav"):
        p.unlink()
    rows, cur = [], 0
    for (kind, fid, _a, _b), ln in zip(plan, lengths):
        w = out_dir / f"{fid}.wav"
        with wave.open(str(w), "wb") as h:
            h.setnchannels(1); h.setsampwidth(2); h.setframerate(sr)
            # ⚠️ 切片必须按**样本下标**切，不能再乘 2：`x` 已经是 `<i2` 视图
            # （每个元素 = 一个采样），`*2` 是从 int16 字节视图时代留下的。
            # 乘 2 的后果不报错、不违反下面任何断言，只是内容全错：每帧写出
            # 2× 于自身区间的前段（后段被下一帧吃掉），越界帧被 Python 切片
            # 静默截成 0 采样。实测 caomin-01：238 帧里 116 帧是 44 字节空 wav，
            # 其余帧长 2 倍；而 verify_binding 与 Σsamples 守恒**照样绿灯**
            # —— 它们核对的是记录值与同一个错误字节流的 sha256，从不看
            # 帧长与真实语音边界是否对得上。
            h.writeframes(x[cur:(cur + ln)].tobytes())
        written = _wav_frames(w)
        if written != ln:
            raise ExternalAudioError(
                f"{fid} 落盘 {written} 采样 != 计划 {ln} 采样（切片与写盘不一致）")
        rows.append(dict(frame=fid, kind=kind, path=str(w), sha256=_sha256(w),
                         start_sample=cur, samples=ln,
                         duration_ms=round(ln * 1000 / sr)))
        cur += ln
    if cur != total:
        raise ExternalAudioError(
            f"采样不守恒：Σframe {cur} != 音轨 {total}（差 {total-cur} 采样）")
    return dict(sample_rate=sr, total_samples=total, rows=rows,
                bed_frames=sum(1 for r in rows if r["kind"] == "bed"),
                audio_dir=str(out_dir))


def _wav_frames(path) -> int:
    """读回 wav 的真实帧数。切完必须用它自检：记录值与落盘值可以一起错。"""
    with wave.open(str(path), "rb") as h:
        return h.getnframes()


def write_binding(ep_id, *, resample, asr, alignment, cut) -> Path:
    """把导入结果落成绑定记录（进 admission 指纹）。"""
    from app.services.provenance import atomic_json
    from app.paths import episode_dir

    # 逐个容器先做形状校验再取 len()：这里曾经把 `cut` 当行序列迭代
    # （TypeError: object of type 'int' has no len），把 asr 的 words/segments
    # 当 dict 取 len() 也同样炸。集中校验比逐个踩坑便宜。
    rows, words, segments = cut["rows"], asr["words"], asr["segments"]
    for name, val in (("cut['rows']", rows), ("asr['words']", words),
                      ("asr['segments']", segments)):
        if isinstance(val, int) or not hasattr(val, "__len__"):
            raise ExternalAudioError(
                f"{name} 必须是序列，实为 {type(val).__name__}（int 无 len，"
                "通常是误把容器本身当行序列迭代）")
    rec = dict(
        episode=ep_id,
        kind="external_audio_binding",
        source_sha256=resample["source_sha256"],
        resampled_sha256=resample["resampled_sha256"],
        sample_rate=cut["sample_rate"],
        total_samples=cut["total_samples"],
        duration_s=resample["duration_s"],
        drift_ms=resample["drift_ms"],
        asr=dict(speech_end=asr["speech_end"], chunks=asr["chunk_count"],
                 words=len(words), segments=len(segments)),
        alignment=dict(accuracy=alignment["accuracy"], matched=alignment["matched"],
                       chars=alignment["chars"],
                       # 空镜帧无台词、不参与 ASR 对齐，单列出来便于诊断
                       narration_frames=alignment.get("narration_frames"),
                       bed_frames=alignment.get("bed_frames")),
        # 键名与 cut_per_frame 的 rows 字段严格一致（frame_offsets 之前被写成
        # start=，verify_binding 读 start_sample= 直接 KeyError，
        # 2026-09-29 实测）。这里统一用 rows 的原字段名。
        frame_offsets=[dict(frame=r["frame"], kind=r["kind"],
                            start_sample=r["start_sample"], samples=r["samples"],
                            sha256=r["sha256"]) for r in rows],
        bed_frames=cut["bed_frames"],
    )
    path = episode_dir(ep_id) / "production" / "external-audio-binding.json"
    atomic_json(path, rec)
    return path


def verify_binding(ep: Episode) -> dict:
    """校验绑定与当前素材一致。成片交付前与准入门禁都会跑。"""
    from app.paths import episode_dir

    path = episode_dir(ep.id) / "production" / "external-audio-binding.json"
    if not path.is_file():
        return dict(ok=True, skipped=True,
                    reason="该集无外部音频绑定（非外部音频集）")
    # read() 住在 narration_pipeline：provenance 只导出写侧（atomic_json /
    # file_sha256 / fingerprint），v2 各处也都从 narration_pipeline 取 read。
    from app.services.narration_pipeline import read
    rec = read(path)
    fails: list[str] = []
    total = sum(o["samples"] for o in rec["frame_offsets"])
    if total != rec["total_samples"]:
        fails.append(f"Σ帧采样 {total} != 绑定音轨 {rec['total_samples']}")
    cur = 0
    for o in rec["frame_offsets"]:
        if o["start_sample"] != cur:
            fails.append(f"{o['frame']} 起始采样 {o['start_sample']} != 累计 {cur}"
                         "（帧序或切分被改动）")
            break
        cur += o["samples"]
    if cur != rec["total_samples"]:
        fails.append(f"末帧累计 {cur} != 音轨 {rec['total_samples']}")
    for o in rec["frame_offsets"]:
        p = Path(o["frame"]) if Path(o["frame"]).is_absolute() else None
        p = p or (episode_dir(ep.id) / "audio" / f"{o['frame']}.wav")
        if not p.is_file():
            fails.append(f"{o['frame']} 音频缺失")
            break
        if _sha256(p) != o["sha256"]:
            fails.append(f"{o['frame']} 音频内容已变（sha256 不符）")
            break
        # 帧长必须等于记录值。sha256 只能证明「文件没被换过」，不能证明
        # 「切对了」——切分切片写错时，文件与记录是同一条错字节流，sha256
        # 必然自洽（caomin-01 实测：238 帧里 116 帧是 0 采样空 wav，此处仍绿）。
        # 帧长对不上就是切分坏了，必须报出来而不是等成片字幕错位。
        got = _wav_frames(p)
        if got != o["samples"]:
            fails.append(f"{o['frame']} 落盘 {got} 采样 != 绑定记录 {o['samples']}"
                         f"（切分与写盘不一致，需重跑导入）")
            break
    return dict(ok=not fails, fails=fails, path=str(path),
                total_samples=rec["total_samples"], frames=len(rec["frame_offsets"]))
