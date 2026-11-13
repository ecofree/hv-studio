"""Lossless writing → narration → direction handoff; no provider/episode writes.

Only source.md is authored; the other payloads are deterministically derived and
checked before preparing narration. Existing recordings stay bound to old text.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.services.script_document import parse_document

VERSION = "writing-handoff-3"
PREVIOUS_VERSION = "writing-handoff-2"
LEGACY_VERSION = "writing-handoff-1"
PLAIN_VERSION = "writing-handoff-plain-1"
PLAIN_APPENDIX_VERSION = "writing-handoff-plain-2"
ACT_HEAD = re.compile(r"^#{1,6}\s*【第[^\n】]{1,30}幕[^\n】]*[：:]", re.M)
QA_APPENDIX = re.compile(r"^#{1,6}[ \t]+本集完成度与质量审查指标[ \t]*\r?$", re.M)


def narration_end(source):
    acts = list(ACT_HEAD.finditer(source))
    appendix = QA_APPENDIX.search(source, acts[-1].end() if acts else 0)
    return appendix.start() if appendix else len(source)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def derive(source: str, *, version=None) -> dict[str, bytes]:
    if version is None:
        version = VERSION if narration_end(source) < len(source) else PREVIOUS_VERSION
    if version not in (VERSION, PREVIOUS_VERSION, LEGACY_VERSION, PLAIN_VERSION, PLAIN_APPENDIX_VERSION):
        raise ValueError("Unsupported writing handoff version")
    match = ACT_HEAD.search(source)
    if not match and version not in (PLAIN_VERSION, PLAIN_APPENDIX_VERSION):
        raise ValueError("找不到幕标题；保留原稿，未生成旁白。请使用 ### 【第一幕：标题】")
    start = match.start() if match else 0
    end = narration_end(source) if version in (VERSION, PLAIN_APPENDIX_VERSION) else len(source)
    body = source[start:end]
    # Keep authored bytes (including BOM/CRLF) untouched. The derived Markdown
    # uses LF, with an explicit map back to the authored character coordinates.
    author_map = list(range(start, end))
    if version != LEGACY_VERSION:
        chars, author_map = [], []
        for pos in range(start, end):
            if source[pos] == '\r' and pos + 1 < len(source) and source[pos + 1] == '\n':
                continue
            chars.append('\n' if source[pos] == '\r' else source[pos])
            author_map.append(pos)
        body = ''.join(chars)
    document = parse_document(body, "narration.md", authoring_layout=version != LEGACY_VERSION)
    problems = [f"{c['cue_id']}: 未识别标注 {c['raw_text'][:80]}"
                for c in document["delivery_cues"] if not c["recognized"]]
    problems += [f"{b['block_id']}: 非正文区需要明确标记或移回正文"
                 for b in document["blocks"] if b.get("review_required")]
    spoken = "\n\n".join(u["source_text"] for u in document["units"]) + "\n"
    if any(c in spoken for c in "【】[]［］"):
        problems.append("正文残留方括号；请闭合导演标注或明确改成普通括注")
    if not document["units"]:
        problems.append("没有可朗读的正文")
    if problems:
        raise ValueError("交接未通过：\n" + "\n".join(problems))
    directions = []
    for cue in document["delivery_cues"]:
        cue = dict(cue)
        cue["authoring_start"] = author_map[cue["start"]]
        cue["authoring_end"] = author_map[cue["end"] - 1] + 1
        if version != LEGACY_VERSION:
            cue['authoring_raw_text'] = source[cue['authoring_start']:cue['authoring_end']]
        cue["execution_status"] = "pending"
        cue["required_disposition"] = (
            "acoustic_pause_after_alignment" if cue["intent"] == "pause" else
            "voice_capability_check_and_listening_review" if cue["channel"] == "performance" else
            "bind_to_shot_and_review_result")
        directions.append(cue)
    payloads = {
        "source.md": source.encode("utf-8"),
        "narration.md": body.encode("utf-8"),
        "spoken.txt": spoken.encode("utf-8"),
        "document.json": _json(document),
        "directions.json": _json({"schema_version": version, "cues": directions,
                                 "status": "pending_execution", "approved": False}),
        "extraction.json": _json({"body_start": start, "body_end": end,
                                 "excluded_prefix": {"start": 0, "end": start,
                                                     "reason": "authoring_front_matter"}}),
    }
    if version != LEGACY_VERSION:
        acts, notes, cards = [], [], []
        for block in document['blocks']:
            if block['kind'] == 'heading' and ACT_HEAD.match(block['raw_text']):
                acts.append(dict(id=f'act-{len(acts)+1:02}', heading=block['raw_text'].strip(),
                                 body_start=block['start'], unit_ids=[]))
            if block['kind'] in ('editorial_direction', 'end_card'):
                a=author_map[block['start']]
                z=author_map[block['end']-1]+1
                row=dict(kind=block['kind'],label=block['label'],start=a,end=z,
                         raw_text=source[a:z],act_id=acts[-1]['id'] if acts else None,
                         execution_status='pending',approved=False)
                (cards if block['kind']=='end_card' else notes).append(row)
        mapped=[]
        for unit in document['units']:
            act=next((a for a in reversed(acts) if a['body_start']<=unit['source_map'][0]),None)
            if act: act['unit_ids'].append(unit['unit_id'])
            positions=[author_map[i] for i in unit['source_map']]
            original=''.join(source[i] for i in positions)
            if any(a!=b and not (a.isspace() and b.isspace()) for a,b in zip(original,unit['source_text'])):
                raise ValueError('Narration character map differs from authored source')
            mapped.append(dict(unit_id=unit['unit_id'],act_id=act['id'] if act else None,
                               authoring_source_map=positions))
        payloads['authoring.json']=_json(dict(schema_version=version,
            coordinate_system='unicode_character_offsets_in_source.md',
            research_notes=dict(start=0,end=start,raw_text=source[:start],fact_checked=False),
            acts=acts,notes=notes,end_cards=cards,unit_sources=mapped,approved=False))
    return payloads


def write_revision(source: str, revisions: Path, context: dict | None = None, *, allow_plain=False) -> Path:
    """Validate before writing; identical imports are idempotent."""
    version=PLAIN_VERSION if allow_plain and not ACT_HEAD.search(source) else VERSION
    if version == VERSION and narration_end(source) == len(source):
        version = PREVIOUS_VERSION
    elif version == PLAIN_VERSION and narration_end(source) < len(source):
        version = PLAIN_APPENDIX_VERSION
    payloads = derive(source,version=version)
    context = context or {}
    revision_id = _sha(version.encode() + payloads["source.md"] + _json(context))[:20]
    target = Path(revisions) / revision_id
    manifest = {"schema_version": version, "revision_id": revision_id,
                "authoritative_source": "source.md", "status": "editorial_candidate",
                "approved": False, "audio_binding": "new_recording_required",
                "context": context,
                "files": {name: _sha(data) for name, data in payloads.items()}}
    if target.exists():
        current = verify_revision(target)
        if current != manifest:
            raise ValueError("Revision identity collision; original revision preserved")
        return target
    target.mkdir(parents=True, exist_ok=False)
    # A partially written directory is unimportable because it has no manifest.
    for name, data in payloads.items():
        (target / name).write_bytes(data)
    (target / "manifest.json").write_bytes(_json(manifest))
    return target


def verify_revision(target: Path) -> dict:
    target = Path(target)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("schema_version")
    if version not in (VERSION, PREVIOUS_VERSION, LEGACY_VERSION, PLAIN_VERSION, PLAIN_APPENDIX_VERSION):
        raise ValueError("Unsupported writing handoff version")
    expected = derive((target / "source.md").read_bytes().decode("utf-8"), version=version)
    expected_hashes = {name: _sha(data) for name, data in expected.items()}
    if manifest.get("files") != expected_hashes:
        raise ValueError("Handoff source/manifest drift; import a new revision")
    identity = _sha(version.encode() + expected["source.md"] + _json(manifest.get("context", {})))[:20]
    if (manifest.get("revision_id") != identity or manifest.get("approved") is not False
            or manifest.get("status") != "editorial_candidate"
            or manifest.get("audio_binding") != "new_recording_required"
            or manifest.get("authoritative_source") != "source.md"):
        raise ValueError("Handoff identity/status drift")
    for name, data in expected.items():
        if (target / name).read_bytes() != data:
            raise ValueError(f"Derived handoff drift: {name}; edit source.md and import a new revision")
    return manifest
