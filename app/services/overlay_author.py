"""Derive a validated ``overlays.json`` from the script plan and quality timings.

Why a generator instead of a hand-written sidecar per episode:

* The act headings already exist in the source as ``kind == "heading"`` blocks.
  They are deliberately excluded from narration (TTS must not read them), so
  nothing downstream turns them into cards unless this step does it.
* An act card's time is the first narration unit after its heading. That mapping
  is exact in the document (``block_ids``) and the quality report
  (``sample_start``); copying it by hand is how cards drift out of sync.
* A missing sidecar is still a no-op for ``overlay.load``. This module is the
  authoring step that makes "every episode opens with a title and marks its
  acts" true in practice, while leaving the JSON editable.

Existing files are never overwritten unless ``force=True``: the generated copy
is a draft, and editorial changes to hooks must survive a refresh.

The legacy pipeline still authors with ``bin/build_overlays.py`` (frame-group
matching). This module is the v2 path: the plan document already carries the
heading -> unit mapping, so no text matching is needed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.services import overlay as O

# Matches 第一幕 / 第十二幕 / 第1幕 and stops before the colon or description.
_ACT_RE = re.compile(r"^第([一二三四五六七八九十百零〇两\d]+)幕")
_COLON_RE = re.compile(r"[：:]")


@dataclass(frozen=True)
class Act:
    label: str
    short: str
    long: str
    start_ms: int
    unit_id: str


def _clean_heading(raw: str) -> str:
    text = (raw or "").strip()
    text = text.lstrip("#").strip()
    text = text.strip("【】[]").strip()
    return text


def _split_heading(text: str) -> tuple[str, str, str]:
    """Return ``(label, short, long)`` for an act heading.

    ``### 【第三幕：五烽箭雨与水囊之空：失落的五升活命水】`` becomes
    ``("第三幕", "五烽箭雨与水囊之空", "失落的五升活命水")``. A heading with
    only one description keeps it in ``long`` and leaves ``short`` empty.
    """
    match = _ACT_RE.match(text)
    if not match:
        return "", "", text
    label = f"第{match.group(1)}幕"
    rest = text[match.end():].lstrip("：: ·—-").strip()
    parts = _COLON_RE.split(rest, maxsplit=1)
    if len(parts) == 2:
        return label, parts[0].strip(), parts[1].strip()
    return label, "", rest


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    raise ValueError(f'字卡文案超过 {limit} 字，请编辑缩写；不会自动截断：{text}')


def _series_from_title(title: str) -> str:
    for sep in ("·", "：", ":"):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if head:
                return head
    return (title or "").strip()


def _takes_by_unit(quality: dict) -> dict[str, tuple[int, int]]:
    result = {}
    for take in quality.get("takes") or []:
        unit_id = take.get("unit_id")
        if unit_id and "sample_start" in take and "sample_end" in take:
            result[unit_id] = (int(take["sample_start"]), int(take["sample_end"]))
    return result


def acts_from_document(document: dict, quality: dict, *, sample_rate: int = 24000) -> list[Act]:
    """Map each act heading to the first narration unit that follows it."""
    blocks = document.get("blocks") or []
    units = document.get("units") or []
    takes = _takes_by_unit(quality)
    acts: list[Act] = []
    seen_units: set[str] = set()

    for index, block in enumerate(blocks):
        if block.get("kind") != "heading":
            continue
        label, short, long = _split_heading(_clean_heading(block.get("raw_text", "")))
        if not label:
            continue
        next_block = None
        for candidate in blocks[index + 1:]:
            kind = candidate.get("kind")
            if kind == "heading":
                break
            if kind == "narration":
                next_block = candidate
                break
        if next_block is None:
            continue
        unit = next(
            (u for u in units if next_block.get("block_id") in (u.get("block_ids") or [])),
            None,
        )
        if unit is None:
            continue
        unit_id = unit.get("unit_id")
        if not unit_id or unit_id in seen_units or unit_id not in takes:
            continue
        seen_units.add(unit_id)
        start_samples = takes[unit_id][0]
        acts.append(
            Act(
                label=label,
                short=short,
                long=long,
                start_ms=int(round(start_samples * 1000 / sample_rate)),
                unit_id=unit_id,
            )
        )

    acts.sort(key=lambda act: act.start_ms)
    return acts


def _act_copy(act: Act) -> tuple[str, str]:
    """Title + subtitle for an act card, within the 18/26 character contract."""
    if act.short and act.long:
        return _clip(act.long, 18), _clip(f"{act.label} · {act.short}", 26)
    description = act.long or act.short
    if description:
        return _clip(description, 18), _clip(act.label, 26)
    return _clip(act.label, 18), ""


def _total_ms(quality: dict, sample_rate: int) -> int:
    ends = [end for _, end in _takes_by_unit(quality).values()]
    if not ends:
        raise ValueError("quality report has no unit sample_end values")
    return int(round(max(ends) * 1000 / sample_rate))


def author(
    ep_id: str,
    document_path,
    quality_path,
    *,
    series: str = "",
    episode_number: int = 0,
    episode_title: str = "",
    title: str | None = None,
    subtitle: str | None = None,
    episode_label: str | None = None,
    total_ms: int | None = None,
    sample_rate: int = 24000,
    force: bool = False,
) -> Path:
    """Write ``episodes/{ep_id}/overlays.json`` and return its path.

    The first act is folded into the title card's subtitle when it starts at
    t=0; later acts get their own 2.8s card. An existing file is returned
    unchanged unless ``force=True``.
    """
    out = O.path_for(ep_id)
    if out.is_file() and not force:
        return out

    document_file = Path(document_path)
    quality_file = Path(quality_path)
    if not document_file.is_file():
        raise FileNotFoundError(f"missing parsed document: {document_file}")
    if not quality_file.is_file():
        raise FileNotFoundError(f"missing quality report: {quality_file}")

    document = json.loads(document_file.read_text(encoding="utf-8"))
    quality = json.loads(quality_file.read_text(encoding="utf-8"))
    acts = acts_from_document(document, quality, sample_rate=sample_rate)
    duration_ms = int(total_ms) if total_ms is not None else _total_ms(quality, sample_rate)
    if duration_ms < O.MIN_MS:
        raise ValueError(f"episode is too short for a title card: {duration_ms}ms")

    title_end = min(3800, duration_ms)
    first_act = acts[0] if acts else None
    # An empty studio brief is an unfilled field, not an instruction to emit
    # an invalid blank title card. Fall back to the series/episode title.
    if not title:
        title = series or _series_from_title(episode_title) or ep_id
    opens_act = bool(first_act and document.get("units") and first_act.unit_id == document["units"][0]["unit_id"])
    if subtitle is None:
        if opens_act:
            subtitle = f"{first_act.label} · {first_act.long or first_act.short}".strip(" ·")
        else:
            subtitle = ""
    if episode_label is None:
        episode_label = f"第{episode_number}集" if episode_number else ""

    specs = [
        O.Spec(
            kind=O.KIND_TITLE,
            start_ms=0,
            end_ms=title_end,
            title=_clip(title, 18),
            subtitle=_clip(subtitle, 26),
            episode=_clip(episode_label, 30),
            fade_ms=400,
            dim=0.32,
            scrim=True,
            # The title must be readable in the very first frame: a 400ms
            # fade-in leaves frame 0 blank, and frame 0 is what the feed and
            # the cover surface show. Keep the fade-out only.
            fade_in_ms=0,
        )
    ]

    # Act 1 is already on the title card when it opens the episode. For a
    # script with a prologue, its card is kept so the act marker is not lost.
    cards = acts[1:] if opens_act else acts
    previous_end = title_end
    for act in cards:
        start = max(act.start_ms, previous_end + 200)
        end = start + 2800
        if end > duration_ms:
            continue
        card_title, card_subtitle = _act_copy(act)
        specs.append(
            O.Spec(
                kind=O.KIND_ACT,
                start_ms=start,
                end_ms=end,
                title=card_title,
                subtitle=card_subtitle,
                fade_ms=400,
                dim=0.22,
                scrim=True,
            )
        )
        previous_end = end

    O.validate(specs, duration_ms)
    payload = {"version": O.SCHEMA_VERSION, "overlays": [spec.payload() for spec in specs]}
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(out)
    print(f"  [overlay] authored {len(specs)} cards for {ep_id}: {out}")
    return out


def ensure(ep_id: str, document_path, quality_path, **kwargs) -> Path:
    """Author only when the sidecar is absent (or ``force=True``)."""
    return author(ep_id, document_path, quality_path, **kwargs)


def check_sidecar(path, *, total_ms=None):
    """An authored v2 episode must open with a readable title at frame zero."""
    from app.services import overlay
    plan=overlay.load_path(path,total_ms=total_ms)
    titles=[s for s in plan.specs if s.kind==overlay.KIND_TITLE]
    if len(titles)!=1 or titles[0].start_ms!=0:
        raise ValueError('本集字卡需要一个从第 0 帧开始的片头标题')
