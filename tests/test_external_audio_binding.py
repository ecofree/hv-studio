# -*- coding: utf-8 -*-
"""Pin the external audio binding: conservation, tamper detection, episode wiring.

Why: legacy/external-audio episodes previously had no machine guarantee binding
the track to the frames -- only Path(f.audio_path).exists(). Swapping the audio
or reordering frames was undetectable. lishimin-04/caomin-01 (2026-09-29).
"""
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, r"D:\hv")
from app.managers import load, save
from app.services.external_audio import (ExternalAudioError, align_frames,
                                         cut_per_frame, read_wav,
                                         verify_binding, write_binding)

pytestmark = pytest.mark.shared
ROOT = Path(r"D:\hv")
SR = 16000


def _tone(path: Path, seconds: float, freq: float = 220.0) -> Path:
    """A deterministic mono 16-bit wav (no TTS, no network)."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = (np.sin(2 * np.pi * freq * t) * 12000).astype("<i2")
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1); h.setsampwidth(2); h.setframerate(SR)
        h.writeframes(x.tobytes())
    return path


def _fake_words(texts):
    """Synthesise ASR-like words so alignment is testable without a model."""
    words, t = [], 0.0
    for txt in texts:
        d = max(0.3, len(txt) / 4.0)
        words.append(dict(word=txt, start=round(t, 3), end=round(t + d, 3)))
        t += d + 0.25
    return words


def test_read_wav_rejects_non_16bit(tmp_path):
    p = tmp_path / "w8.wav"
    with wave.open(str(p), "wb") as h:
        h.setnchannels(1); h.setsampwidth(1); h.setframerate(SR)
        h.writeframes(b"\x80" * 100)
    with pytest.raises(ExternalAudioError, match="16-bit"):
        read_wav(p)


def test_align_frames_keeps_per_frame_coverage():
    from app.models import Episode, Frame
    frames = [Frame(id="frame-000", text="东北亚那边就是高句丽。", narration_text=""),
              Frame(id="frame-001", text="他自称天可汗。", narration_text=""),
              Frame(id="frame-002", text="", narration_text="")]   # 空镜
    r = align_frames(frames, _fake_words(["东北亚那边就是高句丽。", "他自称天可汗。"]))
    by = {x["frame"]: x for x in r["rows"]}
    assert by["frame-000"]["coverage"] > 0.9
    assert by["frame-001"]["coverage"] > 0.9
    # bed frame: no chars, no invented timing
    assert by["frame-002"]["kind"] == "bed"
    assert by["frame-002"]["start"] is None
    assert r["bed_frames"] == 1 and r["narration_frames"] == 2


def test_cut_per_frame_conserves_every_sample(tmp_path):
    from app.models import Episode, Frame
    wav = _tone(tmp_path / "t.wav", 12.0)
    texts = ["第一句话在这里。", "第二句话稍微长一点。", "第三句结束。"]
    frames = [Frame(id=f"frame-{i:03d}", text=t, narration_text="")
              for i, t in enumerate(texts)]
    al = align_frames(frames, _fake_words(texts))
    cut = cut_per_frame(wav, dict(words=_fake_words(texts)), al,
                        tmp_path / "audio", pad_s=0.4, bed_s=7.5, min_frame_s=1.65)
    assert sum(r["samples"] for r in cut["rows"]) == cut["total_samples"]
    assert len(cut["rows"]) == len(frames)
    cur = 0
    for r in cut["rows"]:
        assert r["start_sample"] == cur
        cur += r["samples"]
    assert cur == cut["total_samples"]


def test_cut_rejects_narrated_frame_without_speech(tmp_path):
    from app.models import Episode, Frame
    wav = _tone(tmp_path / "t.wav", 6.0)
    frames = [Frame(id="frame-000", text="有台词。", narration_text=""),
              Frame(id="frame-001", text="也有台词但音频里没有。", narration_text="")]
    words = _fake_words(["有台词。"])
    al = align_frames(frames, words)
    bad = [r for r in al["rows"] if r["chars"] and r["start"] is None]
    assert bad, "有台词却对不上语音的帧必须被识别出来"
    with pytest.raises(ExternalAudioError, match="拒绝切分"):
        cut_per_frame(wav, dict(words=words), al, tmp_path / "audio")


def test_write_binding_record_shape(tmp_path):
    """write_binding must persist the same field names verify_binding reads.

    An earlier draft wrote start= while verify_binding read start_sample=, which
    only blew up later as a KeyError (2026-09-29). Assert the shape directly.
    """
    from app.models import Frame
    wav = _tone(tmp_path / "t.wav", 9.0)
    texts = ["第一句话在这里。", "第二句话稍微长一点。"]
    frames = [Frame(id=f"frame-{i:03d}", text=t, narration_text="")
              for i, t in enumerate(texts)]
    words = _fake_words(texts)
    al = align_frames(frames, words)
    cut = cut_per_frame(wav, dict(words=words), al, tmp_path / "audio")
    resample = dict(source=str(wav), source_sha256="s" * 64,
                    resampled_sha256="r" * 64, sample_rate=SR,
                    total_samples=cut["total_samples"], duration_s=9.0, drift_ms=0.0)
    # recognize_track 的返回形状：words/segments 是**列表**，write_binding 会对
    # 它们取 len()。传计数（int）是调用方的形状错误，模块应当明确报错而不是
    # 到深一层才炸（2026-09-29 踩过：int has no len）。
    p = write_binding("bind_probe", resample=resample,
                      asr=dict(speech_end=9.0, chunk_count=1,
                               words=words, segments=[{"start": 0.0}]),
                      alignment=al, cut=cut)
    with pytest.raises(ExternalAudioError, match="asr\\['words'\\]"):
        write_binding("bind_probe", resample=resample,
                      asr=dict(speech_end=9.0, chunk_count=1,
                               words=len(words), segments=[{"start": 0.0}]),
                      alignment=al, cut=cut)
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["kind"] == "external_audio_binding"
    for o in rec["frame_offsets"]:
        assert {"frame", "kind", "start_sample", "samples", "sha256"} <= set(o), o
    assert sum(o["samples"] for o in rec["frame_offsets"]) == rec["total_samples"]
    cur = 0
    for o in rec["frame_offsets"]:
        assert o["start_sample"] == cur
        cur += o["samples"]


def test_verify_binding_detects_tampering():
    for ep_id in ("caomin-01", "lishimin-04"):
        f = ROOT / "episodes" / f"{ep_id}.json"
        if not f.is_file():
            continue
        ep = load(ep_id)
        assert verify_binding(ep)["ok"], ep_id
        tgt = next((x for x in ep.frames
                    if x.audio_path and (ROOT / "episodes" / ep_id / "audio" /
                                         f"{x.id}.wav").is_file()), None)
        if tgt is None:
            continue
        p = ROOT / "episodes" / ep_id / "audio" / f"{tgt.id}.wav"
        bak = p.read_bytes()
        try:
            p.write_bytes(bak[:-1000] + b"\x00" * 1000)
            bad = verify_binding(load(ep_id))
            assert not bad["ok"], f"{ep_id}: tampered audio must fail the binding"
            assert any("sha256" in x for x in bad["fails"]), bad["fails"]
        finally:
            p.write_bytes(bak)
        assert verify_binding(load(ep_id))["ok"]


def test_delivered_episodes_bind_to_existing_audio():
    for ep_id in ("caomin-01", "lishimin-04"):
        b = ROOT / "episodes" / ep_id / "production" / "external-audio-binding.json"
        if not b.is_file():
            pytest.skip(f"{ep_id} has no binding yet")
        chk = verify_binding(load(ep_id))
        assert chk["ok"], chk["fails"]
        assert chk["frames"] == len(load(ep_id).frames)


def test_admission_fingerprint_includes_binding():
    """Changing a frame audio must invalidate the recorded input approval.

    Fixture is lishimin-04 rather than caomin-01: both are hash-bound legacy
    deliveries with an external-audio binding, so the property under test is
    identical, but caomin-01's admission is legitimately refused since 2026-10-01
    (see test_output_binding_and_legacy_status.caomin01_known_deviation) and the
    test needs a currently-eligible episode to tamper with.
    """
    from app.services.production_admission import check
    ep_id = "lishimin-04"
    if not (ROOT / "episodes" / ep_id / "production" / "input-approval.json").is_file():
        pytest.skip("no recorded approval")
    before = check(ep_id)["input_fingerprint"]
    assert before, "admission must resolve a fingerprint with a binding present"
    assert check(ep_id)["eligible"], check(ep_id)["blockers"]
    tgt = next(x for x in load(ep_id).frames
               if x.audio_path and (ROOT / "episodes" / ep_id / "audio" /
                                    f"{x.id}.wav").is_file())
    p = ROOT / "episodes" / ep_id / "audio" / f"{tgt.id}.wav"
    bak = p.read_bytes()
    try:
        p.write_bytes(bak[:-500] + b"\x00" * 500)
        after = check(ep_id)
        assert not after["eligible"], "tampered audio must break admission"
        assert any("binding" in x for x in after["blockers"]), after["blockers"]
    finally:
        p.write_bytes(bak)
    assert check(ep_id)["eligible"]


def test_verify_stage_rejects_tampered_audio():
    ep_id = "caomin-01"
    out = ROOT / "episodes" / ep_id / "out" / f"{ep_id}_m1.mp4"
    if not out.is_file():
        pytest.skip("no composed mp4")
    tgt = next(x for x in load(ep_id).frames
               if x.audio_path and (ROOT / "episodes" / ep_id / "audio" /
                                    f"{x.id}.wav").is_file())
    p = ROOT / "episodes" / ep_id / "audio" / f"{tgt.id}.wav"
    bak = p.read_bytes()
    try:
        p.write_bytes(bak[:-800] + b"\x00" * 800)
        r = subprocess.run(
            [r"D:\hv\.venv-core\Scripts\python.exe", "-m", "app.cli", "episode",
             "produce", ep_id, "--stage", "verify", "--pipeline", "legacy-preview"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
                 "PYTHONIOENCODING": "utf-8",
                 "PATH": (r"C:\Users\coumoo\AppData\Local\Microsoft\WinGet\Packages"
                          r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
                          r"\ffmpeg-9.0.2-full_build\bin")
                        + os.pathsep + os.environ["PATH"]},
            timeout=1800)
        assert r.returncode != 0
        assert "绑定" in (r.stdout + r.stderr)
    finally:
        p.write_bytes(bak)
