"""Render immutable candidates against a sample clock; publish only exact approvals."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
import uuid
import time

from app.services.spec import VideoFormat, current_video_format, using_video_format
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint


def frame_boundary(sample, sr, fps=25):
    if type(sample) is not int or sample < 0 or type(sr) is not int or sr <= 0:
        raise ValueError("sample boundaries must be nonnegative integers")
    return (sample*fps*2+sr)//(2*sr)


def validate_timeline(manifest, timeline, *, all_declared_proofs=False):
    with using_video_format(VideoFormat.from_record(timeline)):
        return _validate_timeline(manifest, timeline, all_declared_proofs)


def _validate_timeline(manifest, timeline, all_declared_proofs):
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
    proofs={}
    if all_declared_proofs and timeline.get('alignment_review_path'):
        from app.services.subtitle_alignment import require_review
        proofs.update(require_review(manifest,timeline))
    if all_declared_proofs and timeline.get('alignment_evidence_path'):
        from app.services.forced_alignment import require_evidence
        proofs.update(require_evidence(manifest,timeline))
        return proofs  # Includes source coverage, sample clock and subtitle layout.
    units={u["unit_id"]:u for u in manifest["alignment"]}
    covered={u:[] for u in units}
    end=0
    reviewed_alignment=bool(proofs)
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
                    proofs.update(require_evidence(manifest,timeline))
                    return proofs
                from app.services.subtitle_alignment import require_review
                proofs.update(require_review(manifest,timeline))
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
    return proofs


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


def frame_pixels(rows, specs, cues, sample_rate, overlays=None, karaoke=None):
    """Yield final RGB24 frames; overlays and word highlights use the exact sample clock."""
    from PIL import Image
    from app.services.compose import _fade_to_black
    from app.services.motion import apply_grade, plan_frames, camera_frames, camera_crop, inset_backdrop, inset_plate
    from app.services.scene_animation import Scene, validate as validate_animation

    cue_index = 0
    subtitle_tile = None
    subtitle_key = None
    # An 8-bit composite cannot show less than 1/255 of dim, so quantising here
    # costs nothing visually and keeps the frame cache effective while a dim
    # holds steady.
    dim_key = lambda d: round(d * 255)
    try:
        for index, first, last in rows:
            shot = specs[index][0]
            spec = specs[index][1]
            image = Path(shot["image_path"])
            if shot.get('animation') is not None:
                validate_animation(shot['animation'], file_sha256(image))
            with Image.open(image) as opened:
                original = opened.convert("RGB")
            if spec.grade:
                # The color transform is fixed for this shot. Apply it once to
                # the source, then crop its camera trajectory from that image.
                graded = apply_grade(original, spec.grade)
                if graded is not original: original.close()
                original = graded
            frames = backdrop = inset = None
            try:
                shot_first = frame_boundary(shot['sample_start'], sample_rate)
                shot_last = frame_boundary(shot['sample_end'], sample_rate)
                scene = Scene(original, shot['animation'], shot_first, shot_last) if shot.get('animation') is not None else None
                # Compute the complete trajectory; splitting a shot must not restart motion.
                fmt = current_video_format()
                if fmt.uses_inset(original.size):
                    backdrop = inset_backdrop(original, fmt.size)
                    # Keep the complete portrait source visible throughout the shot.
                    boxes = [(0, 0, *fmt.size)] * (last - first)
                    if not scene:
                        inset = inset_plate(original, backdrop)
                else:
                    boxes = plan_frames(*original.size, (shot_last-shot_first)/25.0, spec, 25)[first-shot_first:last-shot_first]
                previous_key, pixels = None, None
                frames = (((b,None,True) for b in boxes) if scene else camera_frames(inset or original,boxes))
                for offset, (box, base, last_use) in enumerate(frames):
                    global_frame = first + offset
                    sample = global_frame * sample_rate // 25
                    while cue_index < len(cues) and sample >= cues[cue_index]["sample_end"]:
                        cue_index += 1
                    cue_text = None
                    if cue_index < len(cues):
                        cue = cues[cue_index]
                        if cue["sample_start"] <= sample < cue["sample_end"]:
                            cue_text = cue['text']
                    hl = karaoke.span_for(cue_index, sample) if (karaoke and cue_text) else None
                    # Overlay tiles are alpha-bucketed internally, so identity is
                    # stable per bucket -- that plus a quantised dim is what makes
                    # the cache usable during a fade without freezing it.
                    tiles, dim = (), 0.0
                    if overlays:
                        g_ms = sample / sample_rate * 1000.0
                        tiles = overlays.layers_at(g_ms)
                        dim = overlays.dim_at(g_ms)
                    ov_key = (tuple(id(t) for t, _b in tiles), dim_key(dim))
                    key = (tuple(box), cue_text, hl, ov_key, global_frame if scene else None)
                    if key != previous_key:
                        if scene:
                            with scene.frame(global_frame) as animated:
                                frame = inset_plate(animated, backdrop) if backdrop is not None else camera_crop(animated, box)
                        else:
                            frame = base.copy() if not last_use and (dim or tiles or cue_text) else base
                        if overlays:
                            if dim > 0.0:
                                faded = _fade_to_black(frame, dim)
                                if frame is not base and frame is not faded: frame.close()
                                frame = faded
                            for tile, at in tiles:
                                frame.paste(tile, at, tile)
                        if cue_text:
                            if subtitle_key != (cue_text, hl):
                                if subtitle_tile is not None: subtitle_tile.close()
                                from app.services.subtitle_layout import overlay
                                subtitle_tile, subtitle_box = overlay(cue_text, hl)
                                subtitle_key = (cue_text, hl)
                            frame.paste(subtitle_tile, subtitle_box[:2], subtitle_tile)
                        pixels = frame.tobytes()
                        if frame is not base: frame.close()
                        previous_key = key
                    yield pixels
            finally:
                if frames is not None: frames.close()
                if inset is not None: inset.close()
                if backdrop is not None: backdrop.close()
                original.close()
    finally:
        if subtitle_tile is not None:subtitle_tile.close()


def _encode_v2_segment(rows, specs, cues, sample_rate, out_path, vcodec, timeout,
                       overlays=None, karaoke=None):
    """Feed one bounded frame stream and atomically install its finished segment."""
    from contextlib import closing
    from app.services.compose import (_kill_proc,_read_stderr,_safe_close,_safe_unlink,
                                      _start_stderr_reader,_wait_proc)
    expected = sum(last - first for _, first, last in rows)
    temp = Path(out_path).with_suffix(Path(out_path).suffix + ".tmp")
    _safe_unlink(temp)
    width, height = current_video_format().size
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", "25", "-i", "-", *vcodec,
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
    encoded = False
    try:
        with closing(frame_pixels(rows,specs,cues,sample_rate,overlays,karaoke)) as frames:
            for pixels in frames:
                proc.stdin.write(pixels)
                written += 1
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
        if watchdog:
            watchdog.cancel()
        if proc.poll() is None:
            _kill_proc(proc)
        _safe_close(proc.stdin)
        _safe_close(proc.stderr)
        if not encoded:
            _safe_unlink(temp)


def timeline_cards(ep_id, timeline, total_ms):
    with using_video_format(VideoFormat.from_record(timeline)):
        return _timeline_cards(ep_id, timeline, total_ms)


def _timeline_cards(ep_id, timeline, total_ms):
    from app.services import overlay, overlay_author
    sidecar = overlay.path_for(ep_id) if ep_id else None
    directed = overlay.load_path(timeline['overlay_path'], total_ms=total_ms).specs if timeline.get('overlay_path') else []
    plan = overlay_author.combined_cards(sidecar, directed, total_ms)
    return plan, fingerprint([c.payload() for c in plan.specs])


def check_overlay_receipt(ep_id, timeline, receipt):
    fmt = VideoFormat.from_record(timeline)
    fmt.require_match(receipt)
    with using_video_format(fmt):
        _check_cards(ep_id, timeline, receipt)


def _check_cards(ep_id, timeline, receipt):
    if not timeline.get('overlay_path'):
        return
    from app.services import overlay
    if not overlay.path_for(ep_id).is_file():
        return
    total_ms = round(receipt['samples']*1000/receipt['sample_rate'])
    _, expected = _timeline_cards(ep_id, timeline, total_ms)
    recorded = receipt.get('effective_overlay_sha256')
    if recorded is None:
        old = overlay.load_path(timeline['overlay_path'], total_ms=total_ms)
        recorded = fingerprint([s.payload() for s in old.specs])
    if expected != recorded:
        raise ValueError('候选采用的字卡缺少当前片头或幕卡，请重新合成')


def _render(manifest_path, timeline_path, output_dir, timeout=300, *, progress=None, on_verified=None):
    """Render and validate against one immutable timeline snapshot."""
    import hashlib
    import json
    from app.services.audio_disposition import require_usable
    manifest = validate_assembly(manifest_path)
    require_usable(manifest)
    data = Path(timeline_path).read_bytes()
    timeline = json.loads(data)
    with using_video_format(VideoFormat.from_record(timeline)):
        return _render_timeline(manifest_path, manifest, timeline_path, timeline, hashlib.sha256(data).hexdigest(),
                                output_dir, timeout, progress, on_verified)


def _render_timeline(manifest_path, manifest, timeline_path, timeline, timeline_sha, output_dir, timeout, progress, on_verified):
    from PIL import Image
    from app.models.shot import MotionSpec
    from app.services.compose import (
        _build_vcodec, _concat_segments, _run_audio_mux, _sub_font_path,
        check_font_available,
    )
    from app.services.media_integrity import verify_media
    from app.config.config import ROOT, get_settings
    proofs=validate_timeline(manifest, timeline, all_declared_proofs=True)
    check_font_available()

    inputs = {
        str(Path(p).resolve()): file_sha256(p)
        for p in [manifest_path, manifest["master_path"], _sub_font_path()]
    }
    inputs[str(Path(timeline_path).resolve())] = timeline_sha
    for path, sha in timeline.get('inputs', {}).items():
        if file_sha256(path) != sha: raise ValueError('timeline source input changed')
        inputs[str(Path(path).resolve())] = sha
    inputs.update(proofs)
    bgm_path = timeline.get('bgm_path')
    if bgm_path and timeline.get('sound_design'):
        raise ValueError('directed sound cannot be combined with legacy bgm')
    if bgm_path: inputs[str(Path(bgm_path).resolve())] = file_sha256(bgm_path)
    # The overlay sidecar changes pixels but lives outside app/, so hashing every
    # module below would not catch an edit to it. Without this, tuning a title
    # card would be declared "pixel-identical" and silently reuse the old render.
    try:
        from app.paths import episodes_dir
        ep_id = Path(manifest_path).resolve().relative_to(episodes_dir().resolve()).parts[0]
    except ValueError:
        ep_id = None
    if ep_id:
        from app.managers import load
        from app.paths import episode_json
        from app.services import overlay as _ov_mod
        if episode_json(ep_id).is_file():
            load(ep_id).video_format.require_match(timeline)
        sidecar = _ov_mod.path_for(ep_id)
        if sidecar.is_file():
            inputs[str(sidecar.resolve())] = file_sha256(sidecar)
    if timeline.get('overlay_path'):
        inputs[str(Path(timeline['overlay_path']).resolve())] = file_sha256(timeline['overlay_path'])
    from app.services.video_segment_cache import DEPENDENCIES
    render_sources = [ROOT/name for name in (*DEPENDENCIES, 'app/services/sound_design.py',
                                              'app/services/bgm.py', 'app/services/upscale.py')]
    for dependency in render_sources + list((ROOT / "app/config").glob("*.yaml")):
        inputs[str(dependency.resolve())] = file_sha256(dependency)
    for dependency in [ROOT / "bin/local_asr_worker.py", ROOT / "requirements.asr.lock.txt"]:
        if dependency.is_file():
            inputs[str(dependency.resolve())] = file_sha256(dependency)

    specs = []
    checked_images = {}
    for shot in timeline["shots"]:
        image = Path(shot["image_path"])
        key = str(image.resolve())
        if key not in checked_images:
            checked_images[key] = file_sha256(image)
            with Image.open(image) as im: im.verify()
            inputs[key] = checked_images[key]
        if shot.get('animation') is not None:
            from app.services.scene_animation import validate as validate_animation
            validate_animation(shot['animation'], checked_images[key])
        spec = MotionSpec(**dict(shot.get("motion", {}), geometry_version="fit_cover_v2"))
        from app.services.motion import resolve_motion
        resolve_motion(spec)
        specs.append((shot, spec))

    sr = manifest["sample_rate"]
    expected_frames = frame_boundary(manifest["samples"], sr)
    import sys
    from app.services.local_asr import runtime_versions
    core_lock = ROOT/'requirements.core.lock.txt'
    fmt = current_video_format()
    width, height = fmt.size
    runtime = {'python': sys.version.split()[0], 'video_format': fmt.value}
    if core_lock.is_file():
        inputs[str(core_lock.resolve())] = file_sha256(core_lock)
        runtime['packages'] = runtime_versions(Path(sys.executable), core_lock)
    runtime['ffmpeg'] = subprocess.run(['ffmpeg','-version'],capture_output=True,text=True,timeout=10,check=True).stdout.splitlines()[0]
    overlay_plan, overlay_sha = timeline_cards(ep_id, timeline, round(manifest['samples']*1000/sr))
    render_fp = fingerprint({"inputs": inputs, "runtime":runtime, "renderer": "v2-segmented-3",
                             "effective_overlays": overlay_sha})
    target = Path(output_dir) / render_fp[:32]
    target.mkdir(parents=True, exist_ok=True)
    state_path = target / "render_state.json"
    state = read(state_path) if state_path.is_file() else {
        "schema_version": "v2-segmented-3",
        "render_fingerprint": render_fp,
        "inputs": inputs,
        "status": "incomplete",
        "segments": [],
    }
    if state.get("render_fingerprint") != render_fp or state.get("inputs") != inputs:
        state = {
            "schema_version": "v2-segmented-3",
            "render_fingerprint": render_fp,
            "inputs": inputs,
            "status": "incomplete",
            "segments": [],
        }

    candidate_receipt = target / "candidate.json"
    if candidate_receipt.is_file():
        receipt = read(candidate_receipt)
        if (VideoFormat.from_record(receipt) == fmt and receipt.get("inputs") == inputs and Path(receipt.get('candidate_path','')).is_file() and
                file_sha256(receipt["candidate_path"]) == receipt.get("candidate_sha256")):
            return candidate_receipt

    compose_cfg = get_settings().compose
    from app.services import video_segment_cache as cache
    from app.services.compose import _probe_nvenc
    use_nvenc = getattr(compose_cfg, 'video_encoder', None) == 'h264_nvenc' and _probe_nvenc('h264_nvenc')
    vcodec = _build_vcodec(use_nvenc)
    shared = cache.context(ROOT, runtime, get_settings().get_profile().subtitle.model_dump(mode='json'),
                           vcodec, _sub_font_path())
    # Both visual layers are optional and absent-means-no-op. The overlay Plan
    # is loaded here (not at encode time) so each segment fingerprint can carry
    # only the overlays intersecting its own time window: editing one title
    # card re-encodes one segment instead of the film.
    from app.services import karaoke as _karaoke
    karaoke_index = _karaoke.EMPTY
    alignment_path = timeline.get('alignment_evidence_path')
    if alignment_path and Path(alignment_path).is_file():
        plan_doc = read(manifest['plan_path'])
        karaoke_index = _karaoke.load(timeline, plan_doc, alignment_path)
    reusable = cache.reusable_records(output_dir)
    max_seconds = getattr(compose_cfg, "v2_segment_max_sec", compose_cfg.segment_max_sec)
    ranges = _segment_ranges(timeline, sr, max_seconds)
    seg_dir = target / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    state["segment_max_seconds"] = max_seconds
    state["expected_frames"] = expected_frames
    state['segment_cache_schema'] = cache.SCHEMA
    previous = {s['id']: s for s in state.get('segments', [])}
    def _overlay_digest(rows):
        if overlay_plan is None:
            return None
        lo = rows[0][1] * 1000.0 / 25
        hi = rows[-1][2] * 1000.0 / 25
        return overlay_plan.fingerprint((lo, hi))
    state["segments"] = [
        {
            "id": f"seg-{i:03d}",
            "first_frame": rows[0][1],
            "last_frame": rows[-1][2],
            "expected_frames": sum(last - first for _, first, last in rows),
            "input_fingerprint": cache.segment_fingerprint(rows, specs, timeline['subtitles'], sr, inputs, shared,
                                                           overlay_digest=_overlay_digest(rows),karaoke=karaoke_index),
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
            if progress: progress(dict(done=i, total=len(ranges), message='画面分段渲染'))
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
                _encode_v2_segment(rows, specs, cues, sr, path, vcodec, timeout,
                                   overlays=overlay_plan, karaoke=karaoke_index)
            except RuntimeError:
                if not use_nvenc: raise
                use_nvenc = False
                vcodec = _build_vcodec(False)
                state['encoder'] = vcodec
                state['encoder_fallback'] = 'hardware encode failed; continued with software'
                # Visibility, not invalidation: already-complete NVENC segments
                # stay as-is and the film concats mixed encoders (like legacy
                # compose.py, which prints the same warning). A mixed film
                # decodes and verifies fine; unifying encoders requires
                # clearing segments and re-running compose.
                print("  [compose] WARNING: NVENC failed mid-render; remaining segments "
                      "use software encoding, the candidate mixes encoders", flush=True)
                _encode_v2_segment(rows, specs, cues, sr, path, vcodec, timeout,
                                   overlays=overlay_plan, karaoke=karaoke_index)
            actual = _probe_segment_frames(path)
            if actual != expected:
                raise RuntimeError(f"segment {record['id']} frame count {actual} != {expected}")
            record.update(status="complete", sha256=file_sha256(path), reused=False,
                          elapsed_seconds=round(time.monotonic()-started,3), encoder=vcodec)
            atomic_json(state_path, state)

        if sum(1 for x in state["segments"] if x["status"] == "complete") != len(ranges):
            raise RuntimeError("not all v2 segments completed")
        if progress: progress(dict(done=len(ranges), total=len(ranges), message='拼接画面、混音与完整性校验'))
        concat_video = target / "concat.video.mp4"
        _concat_segments([Path(x["path"]) for x in state["segments"]], concat_video)
        candidate = target / "candidate.mp4"
        mux_timeout = max(timeout, 600) if timeout else 0
        mux_audio = Path(manifest['master_path'])
        if timeline.get('sound_design'):
            from app.services.sound_design import mix
            mux_audio = mix(mux_audio, timeline['sound_design'], target/'directed-mix.wav')
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
            width=width, height=height, timeout=mux_timeout, output_audio_rate=fmt.output_audio_rate,
        )
        if on_verified is not None:
            on_verified(integrity)
        for path, sha in inputs.items():
            if file_sha256(path) != sha:
                raise ValueError("input changed while rendering")
        receipt = {
            "candidate_path": str(candidate.resolve()),
            "video_format": fmt.value,
            "candidate_sha256": integrity["sha256"],
            "effective_overlay_sha256": overlay_sha,
            "timeline_path": str(Path(timeline_path).resolve()),
            "inputs": inputs,
            # Visibility only: segments keep the exact vcodec argv they were
            # encoded with; a mid-render NVENC->software fallback leaves a
            # mixed film (decodes and verifies fine). Unifying encoders means
            # clearing segments and re-running compose.
            "encoder_mixed": len({tuple(s.get('encoder') or [])
                                  for s in state["segments"]}) > 1,
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


from app.services.production_storage import render_resource


@render_resource
def render(manifest_path, timeline_path, output_dir, timeout=300, *, progress=None, on_verified=None):
    """All callers, including direct narration CLI, share the render-output lock."""
    from app.services.process_lock import file_lock
    key = fingerprint([str(Path(manifest_path).resolve()), str(Path(timeline_path).resolve())])
    with file_lock(Path(output_dir) / ('.render-'+key+'.lock')):
        return _render(manifest_path, timeline_path, output_dir, timeout,
                       **({'on_verified': on_verified} if on_verified else {}),
                       **({'progress': progress} if progress else {}))


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
    if receipt.get('kind') == 'metadata_amendment_v1':
        from app.services.production_revision import verify_amendment
        verify_amendment(receipt_path)
    manifest=validate_assembly(receipt["manifest_path"])
    from app.paths import episodes_dir
    relative_pointer = Path(pointer_path).resolve()
    quality_path = receipt.get('quality_report_path')
    if relative_pointer.is_relative_to(episodes_dir().resolve()):
        eid = relative_pointer.relative_to(episodes_dir().resolve()).parts[0]
        from app.managers import load
        from app.paths import episode_json
        if episode_json(eid).is_file():
            load(eid).video_format.require_match(receipt)
        timeline_path = receipt.get('timeline_path')
        state_path = relative_pointer.parent/'run.json'
        current = read(state_path) if state_path.is_file() else {}
        if current.get('receipt_path') and Path(current['receipt_path']).resolve() == Path(receipt_path).resolve():
            timeline_path = timeline_path or current.get('timeline_path')
            quality_path = current.get('quality_path') or quality_path
        if timeline_path:
            check_overlay_receipt(eid, read(timeline_path), receipt)
    from app.services.audio_disposition import require_usable
    require_usable(manifest)
    if 'delivery_dispositions' in manifest:
        from app.services.delivery import pending as pending_directions
        if pending_directions(receipt['manifest_path']): raise ValueError('delivery directions have unresolved dispositions')
    if quality_path:
        from app.services.production_quality import pending
        if pending(quality_path, manifest_path=receipt['manifest_path']):
            raise ValueError('audio quality findings have unresolved dispositions')
    if review.get("decision")!="approved" or review.get("receipt_sha256")!=file_sha256(receipt_path):
        raise ValueError("candidate has no current approval")
    if review.get("candidate_sha256")!=receipt["candidate_sha256"]:
        raise ValueError("candidate approval hash mismatch")
    if listening.get("decision")!="approved" or listening.get("manifest_sha256")!=file_sha256(receipt["manifest_path"]) or listening.get("master_sha256")!=manifest["master_sha256"]:
        raise ValueError("master audio has no current human listening approval")
    from app.services.production_identity import split_receipt_drift
    content_drift,renderer_drift=split_receipt_drift(
        receipt["inputs"], quality_path=quality_path, manifest_path=receipt['manifest_path'])
    if content_drift: raise ValueError("candidate input changed after review")
    if file_sha256(receipt["candidate_path"])!=receipt["candidate_sha256"]:
        raise ValueError("candidate bytes changed after review")
    fmt = VideoFormat.from_record(receipt)
    width, height = fmt.size
    verified=verify_media(receipt["candidate_path"],expected_frames=receipt["expected_frames"],
                 expected_samples=receipt["samples"],sample_rate=receipt["sample_rate"],
                 width=width, height=height, output_audio_rate=fmt.output_audio_rate)
    if verified["sha256"]!=receipt["candidate_sha256"]:raise ValueError("candidate changed during verification")
    atomic_json(pointer_path,{"candidate_path":receipt["candidate_path"],"sha256":receipt["candidate_sha256"],
                "receipt":str(Path(receipt_path).resolve()),"review":str(Path(review_path).resolve()),
                "listening_review":str(Path(listening_review_path).resolve()),"approved":True,
                "quality_report_path":str(quality_path) if quality_path else None, "renderer_drift":renderer_drift})
    return Path(pointer_path)


def publish(receipt_path, review_path, listening_review_path, pointer_path):
    from app.services.process_lock import file_lock
    with file_lock(Path(pointer_path).with_suffix(".publish.lock")):
        return _publish(receipt_path,review_path,listening_review_path,pointer_path)
