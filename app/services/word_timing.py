"""Per-word (karaoke) subtitle highlighting.

Consumes ``episodes/{id}/word_timing.json`` (written by ``bin/align_episode.py``)
and answers one question for the render loop: *at this millisecond into this
frame's audio, which word is being spoken?*

Two things this module deliberately does **not** do:

* It does not touch the cue table. ``subtitle_fidelity`` requires cue text to
  equal the script verbatim; highlighting is a display concern layered on top.
* It does not re-derive the subtitle's display text. Offsets arrive in **source**
  character coordinates, and translating them to display coordinates is done by
  replaying ``compose.subtitle_display_text`` on each word's own substring and
  walking a monotone cursor. Reimplementing the punctuation-to-space and
  tail-stripping rules here would be a second copy of logic that already decides
  what viewers see.

Absent file, unsupported version, or a coverage mismatch -> no highlighting. The
failure mode is "subtitles look exactly as before", never "wrong word glows".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Word:
    start: int          # source char offset, inclusive
    end: int            # source char offset, exclusive
    ms_start: float     # ms into this frame's own audio
    ms_end: float


@dataclass
class Plan:
    words: dict[str, list[Word]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.words)

    def span(self, frame_id: str, ms: float) -> tuple[int, int] | None:
        """Source char range of the word being spoken at ``ms`` into the audio."""
        for w in self.words.get(frame_id, ()):
            if w.ms_start <= ms < w.ms_end:
                return w.start, w.end
        return None

    def duration_ms(self, frame_id: str) -> float:
        ws = self.words.get(frame_id)
        return ws[-1].ms_end if ws else 0.0

    def fingerprint(self, window: tuple[float, float] | None = None) -> str:
        """Content identity for ``compose._seg_params_fingerprint``.

        Same window trick as overlays: highlighting only changes pixels inside a
        cue's own span, so segments without a word overlap keep their old
        fingerprint and are not re-encoded.
        """
        import hashlib
        import json
        items = {k: [[w.start, w.end, w.ms_start, w.ms_end] for w in v]
                 for k, v in self.words.items()}
        if window is not None:
            lo, hi = window
            items = {k: v for k, v in items.items()
                     if v and lo <= v[-1][3] and v[0][2] < hi}
        payload = {"version": SCHEMA_VERSION, "words": items}
        return hashlib.md5(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def close(self) -> None:
        self.words.clear()


EMPTY = Plan()


def path_for(ep_id: str) -> Path:
    from app import paths
    return paths.episode_dir(ep_id) / 'word_timing.json'


def load(ep_id: str) -> Plan:
    p = path_for(ep_id)
    if not p.is_file():
        return EMPTY
    data = p.read_text(encoding='utf-8')
    import json
    rec = json.loads(data)
    if rec.get('version') != SCHEMA_VERSION:
        raise ValueError(f'unsupported word_timing schema {rec.get("version")!r}')
    return Plan({fid: [Word(int(a), int(b), float(c), float(d)) for a, b, c, d in rows]
                 for fid, rows in (rec.get('words') or {}).items()})


def display_span(source: str, display: str, start: int, end: int,
                 cursor: int = 0) -> tuple[int, int] | None:
    """Translate a source-char range into display-char indices.

    ``display`` must be ``compose.subtitle_display_text(source)``. Returns
    ``None`` when the word's display form cannot be located at or after
    ``cursor`` -- the caller then skips highlighting for that word rather than
    guessing at an offset.
    """
    from app.services import compose as C
    token = C.subtitle_display_text(source[start:end]).strip()
    if not token:
        return None
    idx = display.find(token, cursor)
    if idx < 0:
        return None
    return idx, idx + len(token)


def map_words(display: str, source: str, words) -> list[tuple[int, int]] | None:
    """Display range per word, or None if a monotone mapping cannot be built.

    The invariant is **monotone, non-overlapping, in bounds** -- not a tiling of
    ``display``. Gaps are expected and correct: punctuation becomes spaces on
    screen and punctuation tokens are dropped upstream, so consecutive words are
    separated by characters that belong to no word.

    Returning None (rather than a partial mapping) is deliberate: if one word
    cannot be located, the whole cue falls back to plain subtitles instead of
    lighting a plausible-looking but wrong glyph.
    """
    spans, cursor = [], 0
    for w in words:
        got = display_span(source, display, w.start, w.end, cursor)
        if got is None or got[0] < cursor or got[1] > len(display):
            return None
        spans.append(got)
        cursor = got[1]
    return spans or None