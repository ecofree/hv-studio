"""Timeline overlays: title cards and act cards.

A deliberately separate layer from subtitles (``app.services.subtitle_layout``):

* ``subtitle_fidelity`` requires every cue's text to equal the script text
  verbatim. A title card is **not** script text, so it must never enter the cue
  table -- that gate would correctly reject it.
* Subtitles are bottom-anchored and clamped to 0.8-4s / two lines / a character
  ceiling because they must stay readable at narration speed. Titles want the
  opposite: large, brief, centred, and allowed to sit over the plate.
* Overlays are drawn **after** the shot-change fade and **before** the subtitle
  (see ``compose._encode_video_span_once``), so a title over a fading-in plate
  stays fully legible instead of being dimmed with the plate.

Absence must be a no-op: with no ``overlays.json`` the plan holds no specs, the
encoder's overlay branch never runs, and ``compose._seg_params_fingerprint`` omits
the ``overlays`` key entirely -- so pre-existing segments keep their fingerprint
and are not re-encoded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

KIND_TITLE = "title_card"
KIND_ACT = "act_card"
KINDS = (KIND_TITLE, KIND_ACT)

SCHEMA_VERSION = 1

# A title card is a glance, not a read: past ~6s it has stopped informing.
MIN_MS = 300
MAX_MS = 6000

# Alpha is quantised so the tile is rasterised ALPHA_BUCKETS times per overlay,
# not once per video frame. compose has a history of WinError 1455 on this
# low-pagefile box; per-frame text rasterisation over ~26k frames is exactly the
# pattern that caused it.
ALPHA_BUCKETS = 12

TITLE_SIZE = 132
SUBTITLE_SIZE = 68
EPISODE_SIZE = 54
ACT_TITLE_SIZE = 104

# Vertical centre of the text block, as a fraction of canvas height.
#
# Deliberately HIGH, not centred. The Writing-DNA plate brief forbids big-head
# close-ups and asks for environment, so generated plates are structurally
# consistent: subject in the lower half, sky/ruins up top. Measured on ep02's
# real first frame, an overlay at 0.36 sat squarely on both faces; at 0.20 it
# lands on sky, covers no one, and the sky happens to be the lowest-contrast
# band on the plate. Same reasoning applies to act cards -- they are read, not
# looked past, but there is still no reason to cover a face.
ANCHOR_Y = {KIND_TITLE: 0.20, KIND_ACT: 0.18}

PAD_X = 56
PAD_Y = 48
SCRIM = (0, 0, 0, 150)
EPISODE_FILL = (214, 178, 106)
TEXT_FILL = (255, 255, 255)
TEXT_STROKE = (12, 12, 12)


def _canvas():
    from app.services import compose as C
    return C.W, C.H


@dataclass(frozen=True)
class Spec:
    """One overlay. Times are absolute milliseconds on the episode timeline."""
    kind: str
    start_ms: int
    end_ms: int
    title: str = ""
    subtitle: str = ""
    episode: str = ""
    fade_ms: int = 400
    dim: float = 0.0
    scrim: bool = True
    # Opt-in: legacy cards retain their symmetric fade and exact payload.
    fade_in_ms: int | None = None
    # Per-card vertical anchor override; None keeps the kind default. A plate
    # with two faces high in the frame must not get the sky-anchored default.
    anchor_y: float | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def payload(self) -> dict:
        result = {
            "kind": self.kind, "start_ms": self.start_ms, "end_ms": self.end_ms,
            "title": self.title, "subtitle": self.subtitle, "episode": self.episode,
            "fade_ms": self.fade_ms, "dim": self.dim, "scrim": self.scrim,
        }
        if self.fade_in_ms is not None:
            result['fade_in_ms'] = self.fade_in_ms
        if self.anchor_y is not None:
            result['anchor_y'] = self.anchor_y
        return result


def _f(spec: Spec, ms: float) -> float:
    """Opacity in [0,1] at time ``ms``: linear fade in and fade out."""
    a = 1.0
    fade_in = spec.fade_ms if spec.fade_in_ms is None else spec.fade_in_ms
    if fade_in > 0 and ms < spec.start_ms + fade_in:
        a = (ms - spec.start_ms) / fade_in
    if spec.fade_ms > 0 and ms > spec.end_ms - spec.fade_ms:
        a = min(a, (spec.end_ms - ms) / spec.fade_ms)
    return max(0.0, min(1.0, a))


def _fit(draw, text: str, size: int, max_w: int) -> tuple[int, bool]:
    """Largest size <= ``size`` whose rendered width fits ``max_w``.

    Returns (size, fitted). Without this an 11-character act title at 104px runs
    ~1144px on a 1080px canvas and gets silently cropped by the block width
    clamp -- the viewer sees a truncated title and no error anywhere.
    """
    from app.services import compose as C
    floor = max(48, int(size * 0.6))
    s = size
    while s > floor:
        if draw.textbbox((0, 0), text, font=C._get_font(s))[2] <= max_w:
            return s, s != size
        s -= 4
    return floor, False


def measure(spec: Spec) -> dict:
    """Layout metrics + errors. Mirrors ``subtitle_layout.measure``'s contract."""
    from app.services import compose as C
    W, H = _canvas()
    errors = []
    if spec.kind not in KINDS:
        errors.append(f"unknown overlay kind {spec.kind!r}")
    if not spec.duration_ms >= MIN_MS:
        errors.append(f"overlay shorter than {MIN_MS}ms")
    if spec.duration_ms > MAX_MS:
        errors.append(f"overlay longer than {MAX_MS}ms")
    if spec.start_ms < 0:
        errors.append("overlay starts before timeline")
    fade_in = spec.fade_ms if spec.fade_in_ms is None else spec.fade_in_ms
    if spec.fade_ms < 0 or fade_in < 0 or fade_in + spec.fade_ms > spec.duration_ms:
        errors.append("overlay fade overlaps itself")
    if not (spec.dim == 0.0 or 0.0 <= spec.dim < 1.0):
        errors.append("overlay dim outside [0,1)")
    if not spec.title.strip():
        errors.append("overlay has no title")
    for label, value, limit in (
        ("title", spec.title, 18), ("subtitle", spec.subtitle, 26),
    ):
        if len(value) > limit:
            errors.append(f"overlay {label} exceeds {limit} characters")

    canvas = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(canvas)
    max_w = W - PAD_X * 2
    wanted = [(spec.title, ACT_TITLE_SIZE if spec.kind == KIND_ACT else TITLE_SIZE)]
    if spec.subtitle:
        wanted.append((spec.subtitle, SUBTITLE_SIZE))
    if spec.episode:
        wanted.append((spec.episode, EPISODE_SIZE))
    rows, shrunk = [], False
    for text, size in wanted:
        size, was_shrunk = _fit(draw, text, size, max_w)
        shrunk = shrunk or was_shrunk
        rows.append((text, size))
    canvas.close()
    probe = Image.new("RGB", (1, 1))
    pd = ImageDraw.Draw(probe)
    for text, size in rows:
        if pd.textbbox((0, 0), text, font=C._get_font(size))[2] > max_w:
            errors.append(f"overlay text still overflows at minimum size: {text!r}")
    probe.close()
    height = sum(s for _, s in rows) + PAD_Y * (len(rows) + 1)
    anchor = ANCHOR_Y.get(spec.kind, 0.5) if spec.anchor_y is None else spec.anchor_y
    if not 0.0 <= anchor <= 1.0:
        errors.append("overlay anchor outside [0,1]")
    centre = H * anchor
    top = int(centre - height / 2)
    if top < 0 or top + height > H:
        errors.append("overlay outside vertical safe area")
    return {"rows": rows, "top": top, "height": height, "shrunk": shrunk, "errors": errors}


def validate(specs: list[Spec], total_ms: int | None = None) -> list[dict]:
    metrics = []
    for i, spec in enumerate(specs):
        row = measure(spec)
        if total_ms is not None and spec.end_ms > total_ms:
            row["errors"].append("overlay ends after timeline")
        if row["errors"]:
            raise ValueError(f"overlay[{i}] {spec.kind}: {', '.join(row['errors'])}")
        row.update(index=i, spec=spec)
        metrics.append(row)
    ordered = sorted(specs, key=lambda s: s.start_ms)
    for a, b in zip(ordered, ordered[1:]):
        if b.start_ms < a.end_ms:
            raise ValueError(f"overlays overlap: {a.kind}@{a.start_ms} and {b.kind}@{b.start_ms}")
    return metrics


def rasterise(spec: Spec, alpha: float = 1.0) -> tuple[Image.Image, tuple[int, int]]:
    """Render one overlay to a tight RGBA tile plus its paste position."""
    from app.services import compose as C
    W, H = _canvas()
    layout = measure(spec)
    rows = layout["rows"]

    font_cache = {}
    for text, size in rows:
        font_cache[(text, size)] = C._get_font(size)
    scratch = Image.new("RGBA", (1, 1))
    sd = ImageDraw.Draw(scratch)
    boxes = [sd.textbbox((0, 0), t, font=font_cache[(t, s)], stroke_width=0)
             for t, s in rows]
    widths = [b[2] - b[0] for b in boxes]
    scratch.close()

    block_w = max(widths) + PAD_X * 2
    block_h = layout["height"]
    block_w = min(block_w, W)
    tile = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)

    # Scrim: a title over a busy plate is unreadable without a backing plane.
    # Optional -- a heavy plate over the middle of the frame hides the subject,
    # which on these plates is often the whole point of the shot.
    if spec.scrim:
        td.rounded_rectangle([0, 0, block_w - 1, block_h - 1], radius=28, fill=SCRIM)
        if spec.episode:
            td.rectangle([PAD_X - 18, PAD_Y - 6, PAD_X - 12, block_h - PAD_Y + 6],
                         fill=EPISODE_FILL)

    y = PAD_Y
    for (text, size), box in zip(rows, boxes):
        font = font_cache[(text, size)]
        x = (block_w - (box[2] - box[0])) // 2 - box[0]
        fill = EPISODE_FILL if (spec.episode and text == spec.episode) else TEXT_FILL
        stroke = max(2, size // 14)
        td.text((x + 3, y + 4), text, font=font, fill=(0, 0, 0, 200),
                stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
        td.text((x, y), text, font=font, fill=fill,
                stroke_width=stroke, stroke_fill=TEXT_STROKE)
        y += size + PAD_Y // 2

    if alpha < 1.0:
        level = max(0, min(255, int(round(alpha * 255))))
        lut = [int(round(i * level / 255)) for i in range(256)]
        tile.putalpha(tile.getchannel("A").point(lut))

    tight = tile.getbbox()
    if not tight:
        out = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        return out, (0, 0, 1, 1)
    out = tile.crop(tight)
    x = (W - out.width) // 2 - tight[0]
    y = layout["top"] - tight[1]
    return out, (x, y)


@dataclass
class Plan:
    specs: list[Spec] = field(default_factory=list)
    _cache: dict = field(default_factory=dict, repr=False)

    def __bool__(self) -> bool:
        return bool(self.specs)

    def layers_at(self, ms: float) -> list[tuple[Image.Image, tuple[int, int]]]:
        """Tiles to composite at ``ms``. Empty list when no overlay is active."""
        live = []
        for i, spec in enumerate(self.specs):
            if not (spec.start_ms <= ms < spec.end_ms):
                continue
            alpha = _f(spec, ms)
            if alpha <= 0.0:
                continue
            key = (i, round(alpha * (ALPHA_BUCKETS - 1)))
            tile = self._cache.get(key)
            if tile is None:
                tile = rasterise(spec, key[1] / (ALPHA_BUCKETS - 1))
                self._cache[key] = tile
            live.append(tile)
        self._evict(ms)
        return live

    def dim_at(self, ms: float) -> float:
        """How much to darken the plate at ``ms`` (0.0 = leave it alone).

        Separate from ``layers_at`` because dimming acts on the **frame** while a
        tile acts on top of it, and because the caller must dim before pasting but
        after the subtitle's own contrast is established -- the subtitle is drawn
        last so it stays fully legible on a dimmed plate.
        """
        best = 0.0
        for spec in self.specs:
            if not (spec.start_ms <= ms < spec.end_ms) or spec.dim <= 0.0:
                continue
            best = max(best, spec.dim * _f(spec, ms))
        return best

    def _evict(self, ms: float) -> None:
        """Drop tiles for overlays that have finished (long episodes add many)."""
        for key in [k for k in self._cache
                    if self.specs[k[0]].end_ms <= ms]:
            tile = self._cache.pop(key)
            tile[0].close()

    def fingerprint(self, window: tuple[float, float] | None = None) -> str:
        """Content identity for ``compose._seg_params_fingerprint``.

        ``window`` restricts the digest to overlays intersecting a span of
        timeline milliseconds. Without it, putting one title card at t=0 would
        invalidate every segment of a segmented render; with it, only the
        segment actually containing the card is re-encoded. Same principle as
        ``transition_frames`` living in the segment fingerprint: invalidate on
        pixel change, no more.
        """
        import hashlib
        import json
        specs = self.specs
        if window is not None:
            lo, hi = window
            specs = [s for s in self.specs if s.start_ms < hi and s.end_ms > lo]
        payload = {"version": SCHEMA_VERSION, "overlays": [s.payload() for s in specs]}
        return hashlib.md5(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def close(self) -> None:
        for tile in self._cache.values():
            tile[0].close()
        self._cache.clear()


EMPTY = Plan()


def path_for(ep_id: str) -> Path:
    from app import paths
    return paths.episode_dir(ep_id) / "overlays.json"


def load(ep_id: str) -> Plan:
    """Read ``episodes/{id}/overlays.json``. Missing file -> empty plan.

    The file is deliberately **optional**: without it every downstream call is a
    no-op and the render is byte-identical to a build that predates overlays.
    """
    p = path_for(ep_id)
    if not p.is_file():
        return EMPTY
    return load_path(p)


def load_path(p, *, total_ms=None) -> Plan:
    """Explicit sidecar for an isolated v2 candidate; a missing path is an error."""
    from pathlib import Path
    p = Path(p)
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported overlay schema version {data.get('version')!r}")
    specs = [Spec(kind=o["kind"], start_ms=int(o["start_ms"]), end_ms=int(o["end_ms"]),
                  title=o.get("title", ""), subtitle=o.get("subtitle", ""),
                  episode=o.get("episode", ""), fade_ms=int(o.get("fade_ms", 400)),
                  dim=float(o.get("dim", 0.0)), scrim=bool(o.get("scrim", True)),
                  fade_in_ms=o.get('fade_in_ms'),
                  anchor_y=(float(o['anchor_y']) if o.get('anchor_y') is not None else None))
             for o in data.get("overlays", [])]
    validate(specs,total_ms=total_ms)
    return Plan(specs)
