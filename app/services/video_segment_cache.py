"""Reuse verified video-only segments without weakening candidate evidence."""
import shutil
import uuid
from pathlib import Path

from app.services.provenance import file_sha256, fingerprint, replace_file
from app.services.narration_pipeline import read

SCHEMA = 'video-segment-inputs-v2'
# Direct pixel/geometry/encoder implementation dependencies. Full candidate
# receipts still bind every app module and configuration, including validators.
DEPENDENCIES = (
    'app/services/production_v2.py', 'app/services/video_segment_cache.py',
    'app/services/compose.py', 'app/services/motion.py',
    'app/services/subtitle_layout.py', 'app/services/spec.py',
    'app/models/shot.py', 'app/config/config.py',
    # The two v2 visual layers draw pixels inside _encode_v2_segment. Without
    # them here, fixing their drawing code reuses stale segments and no frame
    # count changes, so verify stays green (xlc-01 era gap, 2026-10-14).
    'app/services/overlay.py', 'app/services/karaoke.py',
    'app/services/word_timing.py',
    'app/services/scene_animation.py',
)


def context(root, runtime, subtitle, encoder, font):
    root = Path(root)
    return dict(schema=SCHEMA, runtime=runtime, subtitle=subtitle, encoder=encoder,
                font_sha256=file_sha256(font),
                implementation={p: file_sha256(root/p) for p in DEPENDENCIES})


def segment_fingerprint(rows, specs, cues, sample_rate, inputs, shared,
                        overlay_digest=None,karaoke=None):
    first, last = rows[0][1], rows[-1][2]
    shots = []
    for index, start, stop in rows:
        shot, motion = specs[index]
        shots.append(dict(frames=[start, stop], sample_start=shot['sample_start'],
                          sample_end=shot['sample_end'], motion=motion.model_dump(mode='json'),
                          image_sha256=inputs[str(Path(shot['image_path']).resolve())]))
        if shot.get('animation') is not None:
            shots[-1]['animation'] = shot['animation']
    # Match the renderer's integer sample clock, including a cue beginning
    # inside a frame. Extra boundary cues are harmless conservative invalidation.
    visible = [dict(text=c['text'], sample_start=c['sample_start'], sample_end=c['sample_end'])
               for c in cues if c['sample_end'] > first*sample_rate//25
               and c['sample_start'] <= (last-1)*sample_rate//25]
    # Overlay content is windowed per segment (same principle as cues): a title
    # card edit re-encodes only the segments it touches, instead of the film.
    return fingerprint(dict(context=shared, shots=shots, cues=visible,
                            overlay=overlay_digest, sample_rate=sample_rate,
                            highlights=[c.words for c in karaoke.cues if c.sample_end>first*sample_rate//25
                                        and c.sample_start<=(last-1)*sample_rate//25] if karaoke else []))


def reusable_records(output_dir):
    """Damaged cache metadata is a miss, never an approval or a queue failure."""
    root = Path(output_dir).resolve()
    result = {}
    for path in sorted(root.glob('*/render_state.json')):
        try:
            state = read(path)
            if state.get('segment_cache_schema') != SCHEMA:
                continue  # Legacy records did not bind the video-only contract.
            for record in state['segments']:
                source = Path(record['path']).resolve()
                if record.get('status') == 'complete' and source.is_relative_to(root):
                    result.setdefault(record['input_fingerprint'], []).append(record)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return result


def restore(record, target, expected_frames, probe):
    """Copy a cache candidate only after hashes and actual frame counts agree."""
    source, target = Path(record['path']), Path(target)
    temp = target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        if (record.get('expected_frames') != expected_frames or not source.is_file()
                or file_sha256(source) != record.get('sha256') or probe(source) != expected_frames):
            return False
        shutil.copy2(source, temp)
        if file_sha256(temp) != record['sha256'] or probe(temp) != expected_frames:
            return False
        replace_file(temp, target)
        return True
    except (OSError, ValueError):
        return False
    finally:
        temp.unlink(missing_ok=True)
