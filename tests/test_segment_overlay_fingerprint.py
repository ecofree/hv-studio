# -*- coding: utf-8 -*-
"""Pin the v2 segment fingerprint policy for the two visual layers.

Why: overlay/karaoke/word_timing draw pixels inside _encode_v2_segment, but
video_segment_cache.DEPENDENCIES did not list them -- fixing their drawing
code reused stale segments with no frame-count change, so verify stayed green
(2026-10-14 audit P0-1). And overlay *content* invalidated the whole film
instead of the touched segment.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.production

from app.services import overlay as O
from app.services import video_segment_cache as cache


def _spec(**kw):
    base = dict(kind=O.KIND_TITLE, start_ms=0, end_ms=3000, title="均田赋")
    base.update(kw)
    return O.Spec(**base)


def test_drawing_code_is_a_segment_dependency():
    """Editing overlay/karaoke/word_timing must invalidate cached segments."""
    for module in ("app/services/overlay.py", "app/services/karaoke.py",
                   "app/services/word_timing.py"):
        assert module in cache.DEPENDENCIES, module


def test_overlay_digest_is_windowed_per_segment():
    """A title card edit re-encodes only the segments it touches."""
    plan = O.Plan([_spec(start_ms=50000, end_ms=53000)])
    seg_hit = plan.fingerprint((0.0, 60000.0))
    seg_miss = plan.fingerprint((0.0, 40000.0))
    assert seg_hit != seg_miss
    # Same window, unchanged plan -> stable digest (no spurious re-encode).
    assert plan.fingerprint((0.0, 60000.0)) == seg_hit
    # Absent plan -> the empty digest, stable across runs.
    assert O.Plan([]).fingerprint((0.0, 60000.0)) == O.EMPTY.fingerprint((0.0, 60000.0))


def test_segment_fingerprint_carries_the_overlay_window():
    """The v2 segment fingerprint includes the per-segment overlay digest."""
    from app.models.shot import MotionSpec
    from PIL import Image
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    img = tmp / "a.png"
    Image.new("RGB", (540, 960), (30, 60, 90)).save(img)
    specs = [(dict(sample_start=0, sample_end=48000, image_path=str(img)), MotionSpec())]
    inputs = {str(img.resolve()): "img"}
    cues: list = []
    shared = {"unit": "test"}
    rows = [(0, 0, 50)]
    base = cache.segment_fingerprint(rows, specs, cues, 24000, inputs, shared)
    plan = O.Plan([_spec(start_ms=0, end_ms=3000)])
    with_digest = cache.segment_fingerprint(
        rows, specs, cues, 24000, inputs, shared,
        overlay_digest=plan.fingerprint((0.0, 2000.0)))
    assert with_digest != base
    # An overlay outside this segment's window must not invalidate it.
    far = O.Plan([_spec(start_ms=50000, end_ms=53000)])
    assert cache.segment_fingerprint(
        rows, specs, cues, 24000, inputs, shared,
        overlay_digest=far.fingerprint((0.0, 2000.0))) == cache.segment_fingerprint(
        rows, specs, cues, 24000, inputs, shared,
        overlay_digest=O.EMPTY.fingerprint((0.0, 2000.0)))
    # Backward compatibility: old callers without the digest keep working.
    assert cache.segment_fingerprint(rows, specs, cues, 24000, inputs, shared) == base
