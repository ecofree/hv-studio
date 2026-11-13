# -*- coding: utf-8 -*-
"""Pin the external narration adapter: honest provenance, conserved cuts, real gates.

Why: v2 could only narrate from CosyVoice, so every delivered episode -- all of
them one continuous external recording -- was unreachable from the v2 chain.
Writing a `voice_asset_version` and a `compile_request` fingerprint onto a take
cut out of a recording would make the manifest assert something untrue, which is
precisely what this repo's gates exist to prevent. So external takes are declared
and gated on measurable evidence instead. caomin-02, 2026-10-13.
"""
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from app.services import external_narration as EN
from app.services.narration_pipeline import SAMPLE_RATE

pytestmark = pytest.mark.shared


def _wav(path: Path, seconds: float, rate=SAMPLE_RATE, freq=220.0) -> Path:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = (np.sin(2 * np.pi * freq * t) * 12000).astype("<i2")
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1); h.setsampwidth(2); h.setframerate(rate)
        h.writeframes(x.tobytes())
    return path


def _plan(units):
    """Minimal narration plan: only the fields this module reads."""
    return {"schema_version": "narration-v2.0", "source_sha256": "src",
            "rewrites": [], "delivery_cues": [],
            "units": [{"unit_id": f"narr-{i}", "source_text": t,
                       "approved_spoken_text": t, "display_text": t,
                       "source_map": list(range(len(t))), "delivery": {}}
                      for i, t in enumerate(units)]}


def _rows(count, spans):
    """align_frames-shaped rows: per-unit speech bounds in seconds."""
    return [dict(frame=f"narr-{i}", kind="narration", chars=100, hits=90,
                 start=spans[i][0], end=spans[i][1], coverage=0.9)
            for i in range(count)]


# --- cut_ranges: gap sharing, ordering, bounds -------------------------------

def test_cut_ranges_share_the_silence_and_never_overlap():
    rows = _rows(3, [(1.0, 3.0), (4.0, 6.0), (7.0, 9.0)])
    ranges = EN.cut_ranges(rows, total_samples=10 * SAMPLE_RATE, sample_rate=SAMPLE_RATE)
    assert [ (round(a / SAMPLE_RATE, 3), round(b / SAMPLE_RATE, 3)) for a, b in ranges ] == [
        (0.7, 3.3), (3.7, 6.3), (6.7, 9.3)]


def test_cut_ranges_never_reach_past_the_master():
    rows = _rows(1, [(0.0, 99.0)])
    (start, end), = EN.cut_ranges(rows, total_samples=10 * SAMPLE_RATE, sample_rate=SAMPLE_RATE)
    assert start == 0
    assert end == 10 * SAMPLE_RATE


def test_cut_ranges_reject_overlapping_units():
    """Two units claiming the same audio means the recording really did repeat
    it, or ASR mis-assigned it. Either way the cut cannot be trusted."""
    rows = _rows(2, [(0.0, 5.0), (4.0, 9.0)])
    with pytest.raises(EN.ExternalNarrationError, match="重叠"):
        EN.cut_ranges(rows, total_samples=10 * SAMPLE_RATE, sample_rate=SAMPLE_RATE)


def test_cut_ranges_allow_real_gaps_between_units():
    """Cuts must NOT tile the master: the gaps are recorded pauses, and each
    unit's true offset lives in alignment[].sample_start."""
    rows = _rows(2, [(0.0, 2.0), (8.0, 10.0)])
    (a0, b0), (a1, b1) = EN.cut_ranges(rows, 10 * SAMPLE_RATE, SAMPLE_RATE)
    assert b0 < a1                      # a real hole survives
    assert (b0 / SAMPLE_RATE) == pytest.approx(2.3, abs=0.01)


# --- alignment gate: refuse before cutting ----------------------------------

def test_unaligned_unit_stops_the_build():
    rows = _rows(2, [(0.0, 2.0), (0.0, 0.0)])
    rows[1]["start"] = rows[1]["end"] = None
    with pytest.raises(EN.ExternalNarrationError, match="找不到对应语音"):
        EN._check_alignment({"accuracy": 0.9, "matched": 1, "chars": 1,
                             "chars_asr": 1, "exact": 1, "homophone": 0}, rows)


def test_thin_coverage_is_flagged_not_fatal():
    """A weak whole-track alignment is a review item, not a build failure.

    Measured 2026-10-13 on xlc-01 unit narr-83f2ea92fa177ec9
    (`"朕佳儿佳妇，今以付卿。"`): coverage 0.385 because free ASR heard
    `朕家而家負`, yet `forced_alignment.atoms` returned 13 word rows for that
    exact slice. The cut was correct; rejecting it blocked the episode.
    """
    rows = _rows(2, [(0.0, 2.0), (3.0, 5.0)])
    rows[0]["coverage"] = 0.41
    summary = EN._check_alignment(
        {"accuracy": 0.9, "matched": 1, "chars": 1, "chars_asr": 1,
         "exact": 1, "homophone": 0}, rows)
    assert summary["low_coverage"] == ["narr-0"]
    assert summary["low_coverage_detail"] == [dict(unit_id="narr-0", coverage=0.41)]


def test_alignment_summary_is_measured_not_assumed():
    rows = _rows(3, [(0.0, 2.0), (3.0, 5.0), (6.0, 8.0)])
    for r, c in zip(rows, (0.70, 0.85, 0.95)):
        r["coverage"] = c
    summary = EN._check_alignment({"accuracy": 0.9, "matched": 3, "chars": 3,
                                   "chars_asr": 3, "exact": 3, "homophone": 0}, rows)
    assert summary["min_coverage"] == 0.70
    assert summary["max_coverage"] == 0.95
    assert summary["median_coverage"] == 0.85
    assert summary["units"] == 3


# --- the gate itself: an external manifest must prove what it claims --------

def _manifest(tmp_path, takes=2):
    master = _wav(tmp_path / "master.wav", 10.0)
    audio = _wav(tmp_path / "u0.wav", 2.0)
    return {
        "provenance": EN.PROVENANCE,
        "master_path": str(master), "master_sha256": EN.file_sha256(master),
        "samples": 10 * SAMPLE_RATE, "sample_rate": SAMPLE_RATE,
        "source_recording": {"path": str(master), "sha256": EN.file_sha256(master),
                             "delivered_sha256": "d" * 64},
        "asr": {"accuracy": 0.87},
        "technical_quality": "passed",
        "takes": [{"unit_id": f"narr-{i}", "audio_path": str(audio)} for i in range(takes)],
        "alignment": [{"unit_id": f"narr-{i}", "sample_start": i * 3 * SAMPLE_RATE,
                       "sample_end": (i * 3 + 2) * SAMPLE_RATE, "coverage": 0.87}
                      for i in range(takes)],
    }


def test_validate_external_accepts_conserved_cuts(tmp_path):
    report = EN.validate_external(_manifest(tmp_path))
    assert report["units"] == 2
    assert report["accuracy"] == 0.87


def test_validate_external_rejects_a_tts_manifest(tmp_path):
    """Fail closed: a manifest whose provenance this gate does not recognise is
    not evidence of anything."""
    m = _manifest(tmp_path); m["provenance"] = "tts"
    with pytest.raises(ValueError, match="provenance"):
        EN.validate_external(m)


def test_validate_external_rejects_a_changed_master(tmp_path):
    m = _manifest(tmp_path)
    _wav(Path(m["master_path"]), 11.0)          # same path, different bytes
    with pytest.raises(ValueError, match="母带字节已变"):
        EN.validate_external(m)


def test_validate_external_rejects_a_forged_recording_fingerprint(tmp_path):
    m = _manifest(tmp_path)
    m["source_recording"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="来源指纹"):
        EN.validate_external(m)


def test_validate_external_rejects_out_of_bounds_cuts(tmp_path):
    m = _manifest(tmp_path)
    m["alignment"][1]["sample_end"] = m["samples"] + 1
    with pytest.raises(ValueError, match="越界"):
        EN.validate_external(m)


def test_validate_external_reports_low_coverage_without_blocking(tmp_path):
    """Downstream must not re-judge with a threshold: the verdict belongs to the
    forced aligner in the alignment stage, which is consumed by the gate."""
    m = _manifest(tmp_path)
    m["alignment"][0]["coverage"] = 0.10
    report = EN.validate_external(m)
    assert report["low_coverage"] == ["narr-0"]
    assert report["units"] == 2


def test_validate_external_requires_a_delivered_recording(tmp_path):
    m = _manifest(tmp_path)
    m["source_recording"]["delivered_sha256"] = ""
    with pytest.raises(ValueError, match="交付来源"):
        EN.validate_external(m)


# --- master canonicalisation -------------------------------------------------

@pytest.mark.NEEDS_FFMPEG
def test_canonical_master_normalises_rate_and_rejects_drift(tmp_path):
    src = _wav(tmp_path / "raw.wav", 3.0, rate=44100)
    out = tmp_path / "master.wav"
    info = EN.canonical_master(src, out)
    assert info["sample_rate"] == SAMPLE_RATE
    assert info["source_rate"] == 44100
    assert info["total_samples"] == pytest.approx(3.0 * SAMPLE_RATE, abs=SAMPLE_RATE * 0.01)
    with wave.open(str(out), "rb") as h:
        assert (h.getnchannels(), h.getsampwidth(), h.getframerate()) == (1, 2, SAMPLE_RATE)


def test_canonical_master_reports_a_missing_file(tmp_path):
    with pytest.raises(EN.ExternalNarrationError, match="找不到录音文件"):
        EN.canonical_master(tmp_path / "nope.wav", tmp_path / "out.wav")


def test_canonical_master_rejects_an_unreadable_source(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    with pytest.raises(EN.ExternalNarrationError, match="不是可读的 WAV"):
        EN.canonical_master(bad, tmp_path / "out.wav")


def test_choose_whole_or_numbered_delivery_once_and_reject_changed_inputs(tmp_path):
    import struct,wave
    from app.services import recording_input as inputs
    def clip(name,sample):
        p=tmp_path/name
        with wave.open(str(p),'wb') as w:
            w.setparams((1,2,24000,0,'NONE','not compressed'))
            w.writeframes(struct.pack('<h',sample)*12)
        return p
    for i in range(1,11):clip(f'act{i}_幕.wav',i)
    master=clip('完整解说母带.wav',99)
    assert inputs.select(tmp_path)==('master',[master])
    current=inputs.bind(tmp_path,'parts')
    assert [Path(r['path']).name for r in current['files']]==[f'act{i}_幕.wav' for i in range(1,11)]
    dest=tmp_path/'derived'
    result=inputs.materialize(current,dest)
    with wave.open(str(result),'rb') as w:
        assert w.getnframes()==120
        assert w.readframes(120)==b''.join(struct.pack('<h',i)*12 for i in range(1,11))
    assert inputs.parts_receipt(result)['spans'][-1]['end_sample']==120
    assert inputs.materialize(current,dest)==result
    selected_via_master=inputs.bind(master,'parts')
    master.unlink()
    inputs.validate(selected_via_master)
    assert inputs.select(tmp_path)[0]=='parts'
    clip('act2_幕.wav',77)
    with pytest.raises(ValueError,match='源文件'):inputs.parts_receipt(result)
    (tmp_path/'act3_幕.wav').unlink()
    with pytest.raises(ValueError,match='不连续'):inputs.bind(tmp_path)

def test_sample_edit_does_not_touch_adjacent_words_or_shift_earlier_chunks():
    import struct
    from app.services.recording_revision import SampleEdit
    raw=struct.pack('<7h',10,20,30,40,50,60,70)
    changed=raw[:4]+raw[8:]
    edit=SampleEdit.compare(raw,changed)
    assert edit.interval(0,2)==(0,2) and edit.interval(4,7)==(2,5)
    assert edit.interval(1,3) is None and edit.boundary(3) is None
