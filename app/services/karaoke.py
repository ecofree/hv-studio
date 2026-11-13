"""Word-level subtitle highlight for the v2 renderer.

The legacy path carried highlight in ``episodes/{id}/word_timing.json``, keyed by
legacy ``frame-000`` ids. The v2 renderer has no frames -- it walks cues keyed by
``unit_id`` -- so that sidecar cannot be reused as-is.

It does not need to be. ``forced_alignment.build`` already emits, per unit, a
word list carrying **source-char offsets and absolute sample spans**, which is
everything a highlight needs. Building the index from the episode's own evidence
keeps the two renderers from needing a second alignment run, and keeps the v2
path free of the legacy frame vocabulary.

Mapping is deliberately conservative: a word whose display form cannot be
located returns ``None`` and that cue falls back to a plain subtitle. A
plausible-looking highlight on the wrong glyph is worse than no highlight.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _Cue:
    sample_start: int
    sample_end: int
    # (word_sample_start, word_sample_end, local_start, local_end)
    words: list[tuple[int, int, int, int]] = field(default_factory=list)
    _starts: list[int] = field(default_factory=list, repr=False)

    def span_at(self, sample: int) -> tuple[int, int] | None:
        if not self.words:
            return None
        i = bisect.bisect_right(self._starts, sample) - 1
        # words are contiguous in time; a gap between them means silence, and
        # the previous word keeps the highlight lit rather than blinking off.
        if i < 0:
            return None
        _, _, a, b = self.words[i]
        return (a, b)


class Index:
    """Highlight spans per cue, derived from alignment evidence."""

    def __init__(self, cues: list[_Cue]):
        self.cues = cues
        self._starts = [c.sample_start for c in cues]
        self._has_words = any(c.words for c in cues)

    def __bool__(self) -> bool:
        return self._has_words

    def span_for(self, cue_index: int, sample: int) -> tuple[int, int] | None:
        if not (0 <= cue_index < len(self.cues)):
            return None
        return self.cues[cue_index].span_at(sample)

    def close(self) -> None:
        self.cues.clear()
        self._has_words = False


EMPTY = Index([])


def build(timeline: dict, plan: dict, alignment: dict) -> Index:
    """-> Index for one episode's timeline.

    ``timeline`` needs ``subtitles``; ``plan`` supplies each unit's
    ``display_text``; ``alignment`` supplies ``words`` keyed by unit_id.
    display_text is the same string the subtitle gate
    (``validate_timeline``: ``cue["text"]==unit["text"][i:j]``) compares
    against -- indexing any other field would misalign highlight and cue
    the moment the two diverge.
    """
    from app.services import compose as C
    from app.services import word_timing as WT

    sources = {u['unit_id']: u.get('display_text') or u.get('source_text', '')
               for u in plan.get('units', [])}
    by_unit = alignment.get('words') or {}
    cues: list[_Cue] = []

    for cue in timeline.get('subtitles') or []:
        unit_id = cue.get('unit_id')
        source = sources.get(unit_id)
        raw = by_unit.get(unit_id) or []
        entry = _Cue(int(cue['sample_start']), int(cue['sample_end']))
        # Words index the unit's display text (the same string the subtitle
        # gate compares cues against); the cue is a slice of it.
        if source and raw and cue.get('text'):
            unit_display = C.subtitle_display_text(source)
            # Punctuation tokens must go before mapping, not after.
            # ``subtitle_display_text('，')`` returns '，' -- its ``or base``
            # fallback keeps a wholly-punctuation string renderable -- but the
            # unit display has turned every '，' into a space, so the token is
            # absent and ``map_words`` would abandon the *entire* unit on the
            # first one. Every unit in the reference episode has one.
            keep = [w for w in raw
                    if (tok := C.subtitle_display_text(
                        source[int(w['text_start']):int(w['text_end'])]).strip())
                    and tok in unit_display]
            words = [WT.Word(int(w['text_start']), int(w['text_end']), 0.0, 0.0)
                     for w in keep]
            spans = WT.map_words(unit_display, source, words) if words else None
            cue_text = cue['text']
            cue_display = C.subtitle_display_text(cue_text) or cue_text
            cue_range = WT.display_span(source, unit_display,
                                        int(cue['text_start']), int(cue['text_end']), 0)
            if spans and cue_range and cue_display:
                ca, cb = cue_range
                for w, (wa, wb) in zip(keep, spans):
                    lo, hi = max(wa, ca) - ca, min(wb, cb) - ca
                    if hi > lo:
                        entry.words.append((int(w['sample_start']), int(w['sample_end']), lo, hi))
        entry.words.sort(key=lambda t: t[0])
        entry._starts = [t[0] for t in entry.words]
        cues.append(entry)

    return Index(cues)


def load(timeline: dict, plan: dict, alignment_path: str | Path) -> Index:
    p = Path(alignment_path)
    if not p.is_file():
        return EMPTY
    return build(timeline, plan, json.loads(p.read_text(encoding='utf-8')))