"""External narration recording -> a v2 narration manifest.

Why this module exists
----------------------
`narration_pipeline.synthesize` can only produce takes from CosyVoice. Every
delivered episode in this repo is narrated by an **externally recorded master**:
one continuous WAV of the whole script. v2 (`production_v2`) needs that master
split into per-`unit` takes, because `forced_alignment.build` re-derives word
timings by aligning each unit's own audio, and `validate_timeline` binds subtitle
text to `unit['display_text'][i:j]`.

The two segmentation systems are not the same thing and must never be conflated:

  legacy `ep.frames`   295 slices of ~3.5s, from `script.split_script`
  v2 `plan['units']`    47 whole-utterance units of ~21s (caomin-02, measured)

So the cut is *recomputed*, never forwarded. The recording is recognised with
ASR, ASR words are aligned to the units, and each unit is cut at its own bounds.

Provenance is the whole point of this module
--------------------------------------------
A take cut from a recording is NOT a TTS take. It has no voice asset, and its
`request_fp` does not identify a synthesis request that was ever issued. Writing
a CosyVoice `voice_asset_version` and a `compile_request` fingerprint onto an
external take would make the manifest lie in exactly the way this repo's gates
exist to prevent.

So external manifests carry `provenance: external_recording` and are gated on
**observable evidence** instead: the recording's sha256, sample conservation
across every cut, and per-unit transcript QA. Alignment coverage is recorded
per unit and adjudicated by the forced aligner, not re-judged as a threshold
here. `episode_production.current_manifest` validates by provenance and fails
closed on anything it does not recognise.

Where the coverage number comes from, and what it can and cannot prove
---------------------------------------------------------------------
`external_audio.align_frames` is a whole-track banded DP over the *whole*
recording, so a unit's coverage is measured against audio that also contains
its neighbours. It is a cheap pre-filter for "did the DP place this unit in a
plausible place", not a verdict on the slice. The verdict on a slice is
`forced_alignment.atoms`, which re-derives word timings from that unit's own
audio against its own text and raises unless the take covers the text.
"""
from __future__ import annotations

import subprocess
import uuid
import wave
from pathlib import Path

from app.services.provenance import atomic_json, file_sha256

PROVENANCE = "external_recording"

# A cut whose unit matched this little of its own characters cannot be trusted to
# contain exactly that unit's words -- the neighbouring unit may be bleeding in.
# Measured on caomin-02 (2026-10-13): min 0.786 / median 0.873 over 47 units.
MIN_UNIT_COVERAGE = 0.60

# Silence either side of a unit is not part of the unit, but the forced aligner
# needs the take to hold its first and last word whole. Measured inter-unit
# silence on caomin-02: 0.46s..2.35s, so a pad of up to this much always fits.
EDGE_PAD_S = 0.30

# Reporting threshold only, NOT a build gate: units below it are listed in
# `manifest["alignment_review"]` and adjudicated by the forced aligner in the
# `alignment` stage. Making it a hard failure rejected provably-correct cuts on
# classical quotations (xlc-01, 2026-10-13). See `_check_alignment`.
MIN_UNIT_COVERAGE = 0.60


class ExternalNarrationError(RuntimeError):
    """外部录音装配的可执行失败。消息必须给出下一步该做什么。"""


class _UnitText:
    """`external_audio.align_frames` reads only `.id` and `.text`."""

    __slots__ = ("id", "text")

    def __init__(self, unit):
        self.id = unit["unit_id"]
        # Align against what was SPOKEN, never against what the source says.
        # `source_text` may carry unspoken direction cues (`【语气：低沉…】`);
        # aligning those would read them as missed lines.
        self.text = unit["approved_spoken_text"]


def _source_info(src):
    """(rate, channels, samples) for the delivered recording, or an actionable error."""
    src = Path(src)
    try:
        with wave.open(str(src), "rb") as h:
            return h.getframerate(), h.getnchannels(), h.getnframes()
    except (wave.Error, OSError) as exc:
        raise ExternalNarrationError(
            f"{src.name} 不是可读的 WAV（{exc}）。\n"
            "处置：先转成 WAV 再导入，例如\n"
            f'  ffmpeg -y -i "{src.name}" -ac 1 -ar 24000 -c:a pcm_s16le "{src.stem}-24k.wav"'
        ) from exc


def canonical_master(src, destination):
    """Resample the delivered recording into the v2 master format.

    `narration_pipeline.wave_info` rejects any master that is not mono PCM16 at
    `SAMPLE_RATE`, so a recording at any other rate has to be rendered before it
    can enter the chain. The duration drift guard is inherited from
    `external_audio.resample_to_16k`: a source that changes length while being
    resampled is damaged, not merely formatted differently.
    """
    from app.services import external_audio as EA
    from app.services.narration_pipeline import SAMPLE_RATE

    src = Path(src)
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        raise ExternalNarrationError(
            f"找不到录音文件：{src}\n"
            "处置：确认路径，或用 --external-master 显式指定这条解说母带。")
    # Read the source first: ffmpeg's own "Invalid data found" is accurate but
    # does not tell the operator what format to hand over.
    sr0, ch0, n0 = _source_info(src)

    ffmpeg = EA.find_ffmpeg()
    run = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(src), "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if run.returncode != 0:
        raise ExternalNarrationError(
            f"母带规范化失败 {src.name}: {run.stderr[-400:]}\n"
            "处置：确认源文件是合法音频，或先手工转 24kHz 单声道 16-bit PCM。")

    samples, _ = _read_pcm16(dst)
    if samples <= 0:
        raise ExternalNarrationError(f"{src.name} 规范化后没有采样。")
    drift = abs(samples / SAMPLE_RATE - n0 / sr0)
    if drift > 0.05:
        raise ExternalNarrationError(
            f"母带时长漂移 {drift * 1000:.0f}ms > 50ms，源音轨可能损坏。\n"
            "处置：重新导出原始录音，不要在已有剪辑/变速的母带上叠加。")
    return dict(
        path=str(dst.resolve()), sha256=file_sha256(dst),
        delivered_path=str(src.resolve()), delivered_sha256=file_sha256(src),
        source_rate=sr0, source_channels=ch0, source_samples=n0,
        sample_rate=SAMPLE_RATE, total_samples=samples,
        duration_s=round(samples / SAMPLE_RATE, 3), drift_ms=round(drift * 1000, 2))


def _read_pcm16(path):
    """(samples, raw bytes) for a mono PCM16 WAV. Mirrors `wave_info` checks."""
    from app.services.narration_pipeline import SAMPLE_RATE

    with wave.open(str(path), "rb") as h:
        if h.getnchannels() != 1 or h.getsampwidth() != 2 or h.getcomptype() != "NONE":
            raise ExternalNarrationError(
                f"{Path(path).name} 不是单声道 16-bit PCM WAV。")
        if h.getframerate() != SAMPLE_RATE:
            raise ExternalNarrationError(
                f"{Path(path).name} 采样率 {h.getframerate()} != {SAMPLE_RATE}。")
        n = h.getnframes()
        data = h.readframes(n)
    if len(data) != n * 2:
        raise ExternalNarrationError(f"{Path(path).name} 数据块被截断。")
    return n, data


def _align_units(plan, words):
    """Align ASR words to whole-utterance units; return rows + global accuracy."""
    from app.services.external_audio import align_frames

    spoken = [(u, _UnitText(u)) for u in plan["units"]]
    empty = [u["unit_id"] for u, shim in spoken if not shim.text.strip()]
    if empty:
        raise ExternalNarrationError(
            f"{len(empty)} 个 unit 没有可朗读文本，无法与录音对齐："
            + ", ".join(empty[:5])
            + "\n处置：这些 unit 的 approved_spoken_text 为空，先修 plan 再导入录音。")

    result = align_frames([shim for _, shim in spoken], words)
    rows = result["rows"]
    if len(rows) != len(plan["units"]):
        raise ExternalNarrationError(
            f"对齐结果行数 {len(rows)} 与 unit 数 {len(plan['units'])} 不符。")
    return result, rows


def _check_alignment(result, rows):
    """Classify alignment before cutting. Only the impossible case fails here.

    `start is None` is fatal: that unit has no audio to cut at all, so continuing
    would fabricate a slice. Low coverage is **not** fatal -- see below.

    Why coverage is a flag, not a gate
    ---------------------------------
    Coverage comes from `external_audio.align_frames`, a whole-track banded DP
    that aligns ASR characters to script characters. It measures two different
    failures with one number:

      a) the cut ran into the neighbouring unit   -> the slice really is wrong
      b) ASR could not recognise the script text  -> the slice can be perfect

    (b) is common on classical quotations. Measured 2026-10-13 on xlc-01 unit
    narr-83f2ea92fa177ec9 (`"朕佳儿佳妇，今以付卿。"`): whole-track coverage
    0.385 because free ASR heard `朕家而家負`, yet the cut was provably correct --
    `forced_alignment.atoms` returned 13 word rows by aligning the *source text*
    back onto that exact audio slice. Treating that as a build failure rejected a
    good cut and blocked the whole episode.

    The authority on "is this slice the right audio" is the forced aligner in the
    downstream `alignment` stage, which re-derives word timings per take and
    raises if the take does not cover its text. So low coverage is recorded per
    unit and adjudicated there; only the truly unalignable unit stops the build.
    """
    unaligned = [r["frame"] for r in rows if r["start"] is None]
    if unaligned:
        raise ExternalNarrationError(
            f"{len(unaligned)}/{len(rows)} 个 unit 在录音里找不到对应语音："
            + ", ".join(unaligned[:5])
            + "\n处置：这段剧本可能没有录，或被剪掉了。重录该段后重新导入。")
    thin = [(r["frame"], r["coverage"]) for r in rows
            if r["coverage"] < MIN_UNIT_COVERAGE]
    coverage = sorted(r["coverage"] for r in rows)
    summary = dict(
        accuracy=result["accuracy"], matched=result["matched"],
        chars=result["chars"], chars_asr=result["chars_asr"],
        exact=result["exact"], homophone=result["homophone"],
        min_coverage=coverage[0], max_coverage=coverage[-1],
        median_coverage=coverage[len(coverage) // 2], units=len(rows),
        low_coverage=[u for u, _ in thin])
    if thin:
        # Not an error. The manifest carries these ids, `alignment` adjudicates
        # them with the forced aligner, and publication requires that verdict.
        summary["low_coverage_detail"] = [
            dict(unit_id=u, coverage=round(c, 4)) for u, c in thin]
    return summary


def cut_ranges(rows, total_samples, sample_rate):
    """Turn per-unit speech bounds into non-overlapping in-bounds cut ranges.

    The space between two consecutive units is recorded silence, so it can be
    shared: each side keeps what it needs to hold its edge word whole, and
    neither side may claim audio that belongs to the other. Leading and trailing
    edges are clamped to the master for the same reason.

    Cuts need not tile the master -- the gaps are real pauses, and
    `alignment[i].sample_start` carries each unit's true offset, which is what
    `forced_alignment` and the timeline use.
    """
    pad = round(EDGE_PAD_S * sample_rate)
    spans = [[r["start"] * sample_rate, r["end"] * sample_rate] for r in rows]
    for k in range(len(spans) - 1):
        gap = spans[k + 1][0] - spans[k][1]
        if gap < 0:
            raise ExternalNarrationError(
                f"{rows[k]['frame']} 与 {rows[k + 1]['frame']} 的语音区间在 "
                f"{spans[k][1] / sample_rate:.2f}s 处重叠："
                "这一段在录音里被读了两次，或 ASR 把邻段认成了本段。\n"
                "处置：重录这两段之间的过渡后重新导入。")
        share = min(pad, gap / 2)
        spans[k][1] += share
        spans[k + 1][0] -= share
    spans[0][0] = max(0, spans[0][0] - min(pad, spans[0][0]))
    spans[-1][1] = min(total_samples, spans[-1][1] + min(pad, total_samples - spans[-1][1]))

    ranges, cursor = [], 0
    for r, (lo, hi) in zip(rows, spans):
        start = max(cursor, round(lo))
        end = min(total_samples, round(hi))
        if end <= start:
            raise ExternalNarrationError(
                f"{r['frame']} 的切片区间为空（{start}..{end}），无法切出这一段。\n"
                "处置：该 unit 在录音里的语音短于一个采样周期，通常意味着对齐错位。")
        ranges.append((start, end))
        cursor = end
    return ranges


def _write_cut(data, start, end, path, sample_rate):
    """Write one cut to its own file. Deliberately not atomic: a cut is derived
    data, and a half-written file fails loudly on the next sha256 read."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(data[start * 2:end * 2])
    return end - start


def build(plan_path, master, destination, *, cache_dir=None, progress=None):
    """Assemble one external recording into a v2 narration manifest.

    Returns the manifest path. Everything it writes is bound by sha256, so the
    manifest becomes invalid the moment the recording or any cut changes.
    """
    from app.services import external_audio as EA
    from app.services import narration_pipeline as N
    from app.services.asr_check import check

    plan = N.read(plan_path)
    assembly = Path(destination) / ("ext-" + uuid.uuid4().hex)
    takes_dir = assembly / "takes"
    takes_dir.mkdir(parents=True)

    say = progress or (lambda message: None)

    say("规范化母带到 v2 母带格式 …")
    recording = canonical_master(master, assembly / "master.wav")
    rate = recording["sample_rate"]
    total = recording["total_samples"]
    say(f"母带 {recording['duration_s']}s @ {rate}Hz，{total} 采样")

    say("整轨 ASR（用于定位每个 unit 的语音区间） …")
    asr_source = assembly / "asr16k.wav"
    EA.resample_to_16k(master, asr_source)
    cache = Path(cache_dir) if cache_dir else assembly / "asr-cache"
    asr = EA.recognize_track(str(asr_source), cache_dir=cache, progress=say)
    say(f"ASR {len(asr['words'])} 词，语音止于 {asr['speech_end']}s")

    say("把 ASR 词对齐到 v2 unit …")
    result, rows = _align_units(plan, asr["words"])
    summary = _check_alignment(result, rows)
    say(f"对齐准确率 {summary['accuracy']:.1%}，覆盖率 {summary['min_coverage']:.1%}"
        f"..{summary['max_coverage']:.1%}")

    ranges = cut_ranges(rows, total, rate)
    _, data = _read_pcm16(assembly / "master.wav")

    takes, alignment, failed = [], [], []
    for unit, row, (start, end) in zip(plan["units"], rows, ranges):
        take_path = takes_dir / f"{unit['unit_id']}.wav"
        samples = _write_cut(data, start, end, take_path, rate)
        audio_sha = file_sha256(take_path)
        quality = check(take_path, unit["approved_spoken_text"]).to_dict()
        if quality.get("status") != "passed":
            failed.append(unit["unit_id"])
        takes.append({
            "unit_id": unit["unit_id"],
            "provenance": PROVENANCE,
            # raw == audio, and that is honest: there is no provider stage, the
            # cut file *is* the delivered recording. See module docstring.
            "raw_path": str(take_path.resolve()), "raw_sha256": audio_sha,
            "audio_path": str(take_path.resolve()), "audio_sha256": audio_sha,
            "samples": samples,
            "quality": quality,
            "listening_review": "pending",
        })
        alignment.append({
            "unit_id": unit["unit_id"],
            "sample_start": start, "sample_end": start + samples,
            "text": unit["display_text"], "source_map": unit["source_map"],
            "source": "external_recording_bounds",
            "word_alignment": "deferred_to_forced_alignment",
            "audio_sha256": audio_sha,
            "coverage": row["coverage"],
        })
        say(f"  {unit['unit_id']}  {samples / rate:6.2f}s  coverage={row['coverage']:.1%}")

    if any(t["samples"] <= 0 for t in takes):
        raise ExternalNarrationError("存在零长度切片，母带或对齐结果不可用。")
    ordered = [a["sample_start"] for a in alignment]
    if ordered != sorted(ordered):
        raise ExternalNarrationError("切片在母带上的顺序与 unit 顺序不一致。")

    manifest = {
        "schema_version": "2.1-external",
        "provenance": PROVENANCE,
        "plan_sha256": file_sha256(plan_path),
        "plan_path": str(Path(plan_path).resolve()),
        "master_path": recording["path"],
        "master_sha256": recording["sha256"],
        "samples": total,
        "sample_rate": rate,
        "source_recording": recording,
        "asr": dict(method="local-whisper-words+banded-dp",
                    sample_rate=asr["sample_rate"],
                    duration_s=asr["duration"],
                    speech_end_s=asr["speech_end"],
                    words=len(asr["words"]),
                    cache_dir=str(cache.resolve()),
                    **summary),
        "cut": dict(pad_s=EDGE_PAD_S,
                    min_unit_coverage=MIN_UNIT_COVERAGE,
                    speech_samples=sum(t["samples"] for t in takes),
                    covered_ratio=round(sum(t["samples"] for t in takes) / total, 4)),
        "takes": takes,
        "alignment": alignment,
        # Units the whole-track DP could not convincingly align. They are cut and
        # adjudicated downstream by the forced aligner; publication is blocked
        # until each one has a verdict (see production_status).
        "alignment_review": sorted(summary.get("low_coverage") or []),
        "technical_quality": "passed" if not failed else "pending",
        "listening_review": "pending",
        "approved": False,
    }
    path = assembly / "manifest.json"
    atomic_json(path, manifest)
    return path


def validate_external(manifest, *, min_coverage=MIN_UNIT_COVERAGE):
    """Evidence a v2 stage must still hold before an external manifest is usable.

    Kept separate from `narration_pipeline.validate_assembly` because that one
    verifies byte integrity for both provenances; this one verifies only what is
    specific to a recorded master. It never returns a truth it did not measure.
    """
    if manifest.get("provenance") != PROVENANCE:
        raise ValueError(
            f"manifest provenance {manifest.get('provenance')!r} 不是 {PROVENANCE!r}")
    recording = manifest.get("source_recording")
    if not isinstance(recording, dict) or not recording.get("delivered_sha256"):
        raise ValueError("外部母带缺少交付来源记录，无法核验是哪一条录音")
    master = Path(manifest["master_path"])
    if file_sha256(master) != manifest["master_sha256"]:
        raise ValueError("母带字节已变")
    if recording.get("path") and str(Path(recording["path"]).resolve()) != str(master.resolve()):
        raise ValueError("母带路径与来源记录不一致")
    if recording.get("sha256") != manifest["master_sha256"]:
        raise ValueError("母带来源指纹与实际母带不一致")

    alignment = manifest.get("alignment") or []
    if len(alignment) != len(manifest["takes"]):
        raise ValueError("alignment 与 takes 数量不一致")
    for a in alignment:
        if not isinstance(a.get("coverage"), (int, float)):
            raise ValueError(
                f"{a['unit_id']} 缺少对齐覆盖率：无法判断这个切片凭什么被信任")
    cursor = 0
    for a in alignment:
        if a["sample_start"] < cursor:
            raise ValueError(f"{a['unit_id']} 的切片与上一个重叠")
        if not 0 <= a["sample_start"] < a["sample_end"] <= manifest["samples"]:
            raise ValueError(f"{a['unit_id']} 的切片区间越界")
        cursor = a["sample_end"]
    # Low coverage is reported, never re-judged here. The verdict comes from the
    # `alignment` stage's forced aligner (atoms) and is consumed by the
    # publication gate; a threshold test at this point would repeat the exact
    # mistake that rejected a correct cut (see _check_alignment).
    low = sorted(a["unit_id"] for a in alignment
                 if a["coverage"] < float(min_coverage))
    return {
        "units": len(alignment),
        "min_coverage": min(a["coverage"] for a in alignment),
        "max_coverage": max(a["coverage"] for a in alignment),
        "accuracy": manifest.get("asr", {}).get("accuracy"),
        "technical_quality": manifest.get("technical_quality"),
        "low_coverage": low,
    }