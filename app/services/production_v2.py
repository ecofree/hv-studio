"""Render immutable candidates against a sample clock; publish only exact approvals."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import uuid
import time

from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def frame_boundary(sample, sr, fps=25):
    if type(sample) is not int or sample < 0 or type(sr) is not int or sr <= 0:
        raise ValueError("sample boundaries must be nonnegative integers")
    return (sample*fps*2+sr)//(2*sr)


def validate_timeline(manifest, timeline):
    sr, total = manifest["sample_rate"],manifest["samples"]
    shots=timeline["shots"]
    cursor=0
    identifiers=set()
    for shot in shots:
        if shot["id"] in identifiers: raise ValueError("duplicate shot id")
        identifiers.add(shot["id"])
        a,b=shot["sample_start"],shot["sample_end"]
        if a!=cursor or b<=a or b>total: raise ValueError("shot gap, overlap, or out-of-range boundary")
        if frame_boundary(b,sr)<=frame_boundary(a,sr): raise ValueError("shot shorter than one video frame")
        cursor=b
    if cursor!=total: raise ValueError("shots must cover the complete narration")
    units={u["unit_id"]:u for u in manifest["alignment"]}
    covered={u:[] for u in units}
    end=0
    reviewed_alignment=False
    for cue in timeline["subtitles"]:
        unit=units[cue["unit_id"]]
        a,b=cue["sample_start"],cue["sample_end"]
        if type(a)is not int or type(b)is not int or not (unit["sample_start"]<=a<b<=unit["sample_end"]) or a<end:
            raise ValueError("subtitle timeline overlaps or leaves its utterance")
        i,j=cue["text_start"],cue["text_end"]
        if type(i)is not int or type(j)is not int or not (0<=i<j<=len(unit["text"])):
            raise ValueError("invalid subtitle source range")
        if cue["text"]!=unit["text"][i:j]: raise ValueError("subtitle differs from source")
        if (i,j)!=(0,len(unit["text"])) or (a,b)!=(unit["sample_start"],unit["sample_end"]):
            if not reviewed_alignment:
                if timeline.get('alignment_evidence_path'):
                    from app.services.forced_alignment import require_evidence
                    require_evidence(manifest, timeline)
                else:
                    from app.services.subtitle_alignment import require_review
                    require_review(manifest,timeline)
                reviewed_alignment=True
        covered[cue["unit_id"]].append((i,j))
        end=b
    for uid,ranges in covered.items():
        cursor=0
        for i,j in ranges:
            if i!=cursor: raise ValueError("subtitle source omission or duplication")
            cursor=j
        if cursor!=len(units[uid]["text"]): raise ValueError("subtitle source coverage incomplete")
    from app.services.subtitle_layout import validate_cues
    validate_cues(timeline['subtitles'], sr)


def _segment_ranges(timeline, sample_rate, max_seconds=90.0):
    """Split into bounded contiguous frame ranges, including inside long shots.

    The old v2 renderer fed the whole episode to one ffmpeg pipe.  A single
    timeout then discarded every frame already written.  This helper keeps the
    exact frame clock while making each segment independently cacheable.
    """
    max_frames = max(1, int(round(float(max_seconds) * 25))) if max_seconds else 10**18
    ranges, current, frames = [], [], 0
    for index, shot in enumerate(timeline["shots"]):
        first = frame_boundary(shot["sample_start"], sample_rate)
        last = frame_boundary(shot["sample_end"], sample_rate)
        count = last - first
        if count <= 0:
            raise ValueError(f"shot {shot.get('id', index)} has no video frames")
        while first < last:
            if frames == max_frames:
                ranges.append(current)
                current, frames = [], 0
            stop = min(last, first + max_frames - frames)
            current.append((index, first, stop))
            frames += stop - first
            first = stop
    if current:
        ranges.append(current)
    return ranges


def _probe_segment_frames(path):
    """Return a segment's packet count, or -1 for a missing/corrupt file."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not result.stdout.strip():
            return -1
        return int(result.stdout.strip())
    except Exception:
        return -1


def _encode_v2_segment(rows, specs, cues, sample_rate, out_path, vcodec, timeout):
    """Encode one v2 shot range and atomically publish its video-only segment."""
    from PIL import Image
    from app.services.compose import (
        _kill_proc, _read_stderr,
        _safe_close, _safe_unlink, _start_stderr_reader, _wait_proc,
    )
    from app.services.motion import apply_grade, plan_frames

    expected = sum(last - first for _, first, last in rows)
    temp = Path(out_path).with_suffix(Path(out_path).suffix + ".tmp")
    _safe_unlink(temp)
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", "1080x1920", "-r", "25", "-i", "-", *vcodec,
        "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(temp),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _start_stderr_reader(proc)
    watchdog = threading.Timer(timeout, _kill_proc, args=(proc,)) if timeout and timeout > 0 else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()
    written = 0
    cue_index = 0
    encoded = False
    subtitle_tile = None
    subtitle_key = None
    try:
        for index, first, last in rows:
            shot = specs[index][0]
            spec = specs[index][1]
            image = Path(shot["image_path"])
            with Image.open(image) as opened:
                original = opened.convert("RGB")
            if spec.grade:
                # The color transform is fixed for this shot. Apply it once to
                # the source, then crop its camera trajectory from that image.
                graded = apply_grade(original, spec.grade)
                if graded is not original: original.close()
                original = graded
            try:
                shot_first = frame_boundary(shot['sample_start'], sample_rate)
                shot_last = frame_boundary(shot['sample_end'], sample_rate)
                # Compute the complete trajectory; splitting a shot must not restart motion.
                boxes = plan_frames(*original.size, (shot_last-shot_first)/25.0, spec, 25)[first-shot_first:last-shot_first]
                previous_key, pixels = None, None
                for offset, box in enumerate(boxes):
                    global_frame = first + offset
                    sample = global_frame * sample_rate // 25
                    while cue_index < len(cues) and sample >= cues[cue_index]["sample_end"]:
                        cue_index += 1
                    cue_text = None
                    if cue_index < len(cues):
                        cue = cues[cue_index]
                        if cue["sample_start"] <= sample < cue["sample_end"]:
                            cue_text = cue['text']
                    key = (tuple(box), cue_text)
                    if key != previous_key:
                        frame = original.crop(box).resize((1080, 1920), Image.Resampling.LANCZOS)
                        if cue_text:
                            if subtitle_key != cue_text:
                                if subtitle_tile is not None: subtitle_tile.close()
                                from app.services.subtitle_layout import overlay
                                subtitle_tile, subtitle_box = overlay(cue_text)
                                subtitle_key = cue_text
                            frame.paste(subtitle_tile, subtitle_box[:2], subtitle_tile)
                        pixels = frame.tobytes()
                        frame.close()
                        previous_key = key
                    proc.stdin.write(pixels)
                    written += 1
            finally:
                original.close()
        proc.stdin.close()
        return_code = _wait_proc(proc, timeout or 0, "v2 segment")
        stderr = _read_stderr(proc)
        if return_code != 0 or written != expected:
            raise RuntimeError(
                f"v2 segment incomplete: wrote {written}/{expected} frames; "
                f"returncode={return_code}; ffmpeg={stderr[-1200:]}")
        os.replace(temp, out_path)
        encoded = True
        return written
    except (BrokenPipeError, OSError) as exc:
        stderr = _read_stderr(proc)
        raise RuntimeError(
            f"v2 segment pipe closed after {written}/{expected} frames: {exc}; "
            f"ffmpeg={stderr[-1200:]}") from exc
    finally:
        if subtitle_tile is not None: subtitle_tile.close()
        if watchdog:
            watchdog.cancel()
        if proc.poll() is None:
            _kill_proc(proc)
        _safe_close(proc.stdin)
        _safe_close(proc.stderr)
        if not encoded:
            _safe_unlink(temp)


def _render(manifest_path, timeline_path, output_dir, timeout=300):
    """Render a v2 candidate with resumable, shot-boundary video segments."""
    from PIL import Image
    from app.models.shot import MotionSpec
    from app.services.compose import (
        _build_vcodec, _concat_segments, _run_audio_mux, _sub_font_path,
        check_font_available,
    )
    from app.services.media_integrity import verify_media
    from app.config.config import ROOT, get_settings
    from app.services.audio_disposition import require_usable

    manifest = validate_assembly(manifest_path)
    require_usable(manifest)
    timeline = read(timeline_path)
    validate_timeline(manifest, timeline)
    check_font_available()

    inputs = {
        str(Path(p).resolve()): file_sha256(p)
        for p in [manifest_path, timeline_path, manifest["master_path"], _sub_font_path()]
    }
    for path, sha in timeline.get('inputs', {}).items():
        if file_sha256(path) != sha: raise ValueError('timeline source input changed')
        inputs[str(Path(path).resolve())] = sha
    if timeline.get("alignment_review_path"):
        from app.services.subtitle_alignment import require_review
        inputs.update(require_review(manifest, timeline))
    if timeline.get('alignment_evidence_path'):
        from app.services.forced_alignment import require_evidence
        inputs.update(require_evidence(manifest, timeline))
    bgm_path = timeline.get('bgm_path')
    if bgm_path: inputs[str(Path(bgm_path).resolve())] = file_sha256(bgm_path)
    for dependency in list((ROOT / "app").rglob("*.py")) + list((ROOT / "app/config").glob("*.yaml")):
        inputs[str(dependency.resolve())] = file_sha256(dependency)
    for dependency in [ROOT / "bin/local_asr_worker.py", ROOT / "requirements.asr.lock.txt"]:
        if dependency.is_file():
            inputs[str(dependency.resolve())] = file_sha256(dependency)

    specs = []
    for shot in timeline["shots"]:
        image = Path(shot["image_path"])
        inputs[str(image.resolve())] = file_sha256(image)
        with Image.open(image) as im:
            im.verify()
        spec = MotionSpec(**dict(shot.get("motion", {}), geometry_version="fit_cover_v2"))
        from app.services.motion import resolve_motion
        resolve_motion(spec)
        specs.append((shot, spec))

    sr = manifest["sample_rate"]
    expected_frames = frame_boundary(manifest["samples"], sr)
    import sys
    from app.services.local_asr import runtime_versions
    core_lock = ROOT/'requirements.core.lock.txt'
    runtime = {'python': sys.version.split()[0]}
    if core_lock.is_file():
        inputs[str(core_lock.resolve())] = file_sha256(core_lock)
        runtime['packages'] = runtime_versions(Path(sys.executable), core_lock)
    runtime['ffmpeg'] = subprocess.run(['ffmpeg','-version'],capture_output=True,text=True,timeout=10,check=True).stdout.splitlines()[0]
    render_fp = fingerprint({"inputs": inputs, "runtime":runtime, "renderer": "v2-segmented-2"})
    target = Path(output_dir) / render_fp[:32]
    target.mkdir(parents=True, exist_ok=True)
    state_path = target / "render_state.json"
    state = read(state_path) if state_path.is_file() else {
        "schema_version": "v2-segmented-2",
        "render_fingerprint": render_fp,
        "inputs": inputs,
        "status": "incomplete",
        "segments": [],
    }
    if state.get("render_fingerprint") != render_fp or state.get("inputs") != inputs:
        state = {
            "schema_version": "v2-segmented-2",
            "render_fingerprint": render_fp,
            "inputs": inputs,
            "status": "incomplete",
            "segments": [],
        }

    candidate_receipt = target / "candidate.json"
    if candidate_receipt.is_file():
        receipt = read(candidate_receipt)
        if (receipt.get("inputs") == inputs and Path(receipt.get('candidate_path','')).is_file() and
                file_sha256(receipt["candidate_path"]) == receipt.get("candidate_sha256")):
            return candidate_receipt

    compose_cfg = get_settings().compose
    from app.services import video_segment_cache as cache
    from app.services.compose import _probe_nvenc
    use_nvenc = getattr(compose_cfg, 'video_encoder', None) == 'h264_nvenc' and _probe_nvenc()
    vcodec = _build_vcodec(use_nvenc)
    shared = cache.context(ROOT, runtime, get_settings().get_profile().subtitle.model_dump(mode='json'),
                           vcodec, _sub_font_path())
    reusable = cache.reusable_records(output_dir)
    max_seconds = getattr(compose_cfg, "v2_segment_max_sec", compose_cfg.segment_max_sec)
    ranges = _segment_ranges(timeline, sr, max_seconds)
    seg_dir = target / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    state["segment_max_seconds"] = max_seconds
    state["expected_frames"] = expected_frames
    state['segment_cache_schema'] = cache.SCHEMA
    previous = {s['id']: s for s in state.get('segments', [])}
    state["segments"] = [
        {
            "id": f"seg-{i:03d}",
            "first_frame": rows[0][1],
            "last_frame": rows[-1][2],
            "expected_frames": sum(last - first for _, first, last in rows),
            "input_fingerprint": cache.segment_fingerprint(rows, specs, timeline['subtitles'], sr, inputs, shared),
            "path": str((seg_dir / f"seg-{i:03d}.mp4").resolve()),
            "status": "pending",
        }
        for i, rows in enumerate(ranges)
    ]
    # Keep trusted receipts for later segments if this process dies before it
    # reaches them. Every reused file is still checked immediately before use.
    for record in state['segments']:
        old = previous.get(record['id'], {})
        if old.get('input_fingerprint') == record['input_fingerprint']:
            record.update({k: old[k] for k in ('status', 'sha256', 'encoder', 'elapsed_seconds') if k in old})
    atomic_json(state_path, state)
    cues = timeline["subtitles"]
    state.update(encoder=vcodec, started_at=time.time(), last_error=None)

    try:
        for i, rows in enumerate(ranges):
            record = state["segments"][i]
            path = Path(record["path"])
            expected = record["expected_frames"]
            old = previous.get(record['id'], {})
            if (old.get('status') == 'complete' and old.get('input_fingerprint') == record['input_fingerprint']
                    and path.is_file() and file_sha256(path) == old.get('sha256')
                    and _probe_segment_frames(path) == expected):
                record.update(status="complete", sha256=old['sha256'], reused=True,
                              elapsed_seconds=old.get('elapsed_seconds'))
                atomic_json(state_path, state)
                continue
            restored = next((old for old in reusable.get(record['input_fingerprint'], [])
                             if cache.restore(old, path, expected, _probe_segment_frames)), None)
            if restored is not None:
                record.update(status='complete', sha256=restored['sha256'], reused=True,
                              reused_from=restored['path'], encoder=restored.get('encoder'),
                              elapsed_seconds=restored.get('elapsed_seconds'))
                atomic_json(state_path, state)
                continue
            path.unlink(missing_ok=True)
            record.update(status="running")
            atomic_json(state_path, state)
            started = time.monotonic()
            try:
                _encode_v2_segment(rows, specs, cues, sr, path, vcodec, timeout)
            except RuntimeError:
                if not use_nvenc: raise
                use_nvenc = False
                vcodec = _build_vcodec(False)
                state['encoder'] = vcodec
                state['encoder_fallback'] = 'hardware encode failed; continued with software'
                _encode_v2_segment(rows, specs, cues, sr, path, vcodec, timeout)
            actual = _probe_segment_frames(path)
            if actual != expected:
                raise RuntimeError(f"segment {record['id']} frame count {actual} != {expected}")
            record.update(status="complete", sha256=file_sha256(path), reused=False,
                          elapsed_seconds=round(time.monotonic()-started,3), encoder=vcodec)
            atomic_json(state_path, state)

        if sum(1 for x in state["segments"] if x["status"] == "complete") != len(ranges):
            raise RuntimeError("not all v2 segments completed")
        concat_video = target / "concat.video.mp4"
        _concat_segments([Path(x["path"]) for x in state["segments"]], concat_video)
        candidate = target / "candidate.mp4"
        mux_timeout = max(timeout, 600) if timeout else 0
        mux_audio = Path(manifest['master_path'])
        if bgm_path:
            from app.services.bgm import mix_voice_with_bgm
            bgm_cfg = get_settings().get_profile().bgm
            mux_audio = mix_voice_with_bgm(mux_audio, bgm_path, manifest['samples']*1000/sr,
                volume=bgm_cfg.volume, fade_out_sec=bgm_cfg.fade_out_sec, sample_rate=sr,
                out_wav=target/'mixed.wav')
        _run_audio_mux(concat_video, mux_audio, candidate,
                       "aac", "192k", mux_timeout)
        integrity = verify_media(
            candidate, expected_frames=expected_frames,
            expected_samples=manifest["samples"], sample_rate=sr,
            timeout=mux_timeout,
        )
        for path, sha in inputs.items():
            if file_sha256(path) != sha:
                raise ValueError("input changed while rendering")
        receipt = {
            "candidate_path": str(candidate.resolve()),
            "candidate_sha256": file_sha256(candidate),
            "inputs": inputs,
            "integrity": integrity,
            "manifest_path": str(Path(manifest_path).resolve()),
            "expected_frames": expected_frames,
            "samples": manifest["samples"],
            "sample_rate": sr,
            "runtime": runtime,
            "render_state_path": str(state_path.resolve()),
            "segments": state["segments"],
            "approved": False,
            "quality_report_path": timeline.get('quality_report_path'),
        }
        atomic_json(candidate_receipt, receipt)
        state.update(status="completed", candidate_path=str(candidate.resolve()), candidate_sha256=receipt["candidate_sha256"])
        state['completed_at'] = time.time()
        atomic_json(state_path, state)
        return candidate_receipt
    except Exception as exc:
        state.update(status="incomplete", last_error=str(exc))
        atomic_json(state_path, state)
        raise


def render(manifest_path, timeline_path, output_dir, timeout=300):
    """All callers, including direct narration CLI, share the render-output lock."""
    from app.services.process_lock import file_lock
    key = fingerprint([str(Path(manifest_path).resolve()), str(Path(timeline_path).resolve())])
    with file_lock(Path(output_dir) / ('.render-'+key+'.lock')):
        return _render(manifest_path, timeline_path, output_dir, timeout)


def review_candidate(receipt_path, reviewer, findings, decision):
    receipt=read(receipt_path)
    from datetime import datetime, timezone
    if not reviewer.strip() or not findings.strip() or decision not in ("approved","rejected"):
        raise ValueError("review requires reviewer, findings and decision")
    if file_sha256(receipt["candidate_path"])!=receipt["candidate_sha256"]: raise ValueError("candidate changed")
    record={"receipt_sha256":file_sha256(receipt_path),"candidate_sha256":receipt["candidate_sha256"],
            "reviewer":reviewer,"findings":findings,"decision":decision,
            "reviewed_at":datetime.now(timezone.utc).isoformat(),
            "scope":"visual composition, narrative, source facts, subtitles and audio"}
    target=Path(receipt_path).parent/("review-"+uuid.uuid4().hex+".json")
    atomic_json(target,record)
    return target


def _publish(receipt_path, review_path, listening_review_path, pointer_path):
    from app.services.media_integrity import verify_media
    receipt,review,listening=read(receipt_path),read(review_path),read(listening_review_path)
    manifest=validate_assembly(receipt["manifest_path"])
    from app.services.audio_disposition import require_usable
    require_usable(manifest)
    if 'delivery_dispositions' in manifest:
        from app.services.delivery import pending as pending_directions
        if pending_directions(receipt['manifest_path']): raise ValueError('delivery directions have unresolved dispositions')
    if receipt.get('quality_report_path'):
        from app.services.production_quality import pending
        if pending(receipt['quality_report_path']): raise ValueError('audio quality findings have unresolved dispositions')
    if review.get("decision")!="approved" or review.get("receipt_sha256")!=file_sha256(receipt_path):
        raise ValueError("candidate has no current approval")
    if review.get("candidate_sha256")!=receipt["candidate_sha256"]:
        raise ValueError("candidate approval hash mismatch")
    if listening.get("decision")!="approved" or listening.get("manifest_sha256")!=file_sha256(receipt["manifest_path"]) or listening.get("master_sha256")!=manifest["master_sha256"]:
        raise ValueError("master audio has no current human listening approval")
    for path,sha in receipt["inputs"].items():
        if file_sha256(path)!=sha: raise ValueError("candidate input changed after review")
    if file_sha256(receipt["candidate_path"])!=receipt["candidate_sha256"]:
        raise ValueError("candidate bytes changed after review")
    verified=verify_media(receipt["candidate_path"],expected_frames=receipt["expected_frames"],
                 expected_samples=receipt["samples"],sample_rate=receipt["sample_rate"])
    if verified["sha256"]!=receipt["candidate_sha256"]:raise ValueError("candidate changed during verification")
    atomic_json(pointer_path,{"candidate_path":receipt["candidate_path"],"sha256":receipt["candidate_sha256"],
                "receipt":str(Path(receipt_path).resolve()),"review":str(Path(review_path).resolve()),
                "listening_review":str(Path(listening_review_path).resolve()),"approved":True})
    return Path(pointer_path)


def publish(receipt_path, review_path, listening_review_path, pointer_path):
    from app.services.process_lock import file_lock
    with file_lock(Path(pointer_path).with_suffix(".publish.lock")):
        return _publish(receipt_path,review_path,listening_review_path,pointer_path)
