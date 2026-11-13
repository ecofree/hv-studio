"""Lossless excerpts of explicitly selected external WAV masters.

Source lineage is not a listening approval. Transcript/alignment checks still
run on the selected excerpt, and neither the parent master nor its reviews move.
"""
from pathlib import Path
import os
import wave

from app import paths
from app.services import studio_workflow as W
from app.services.provenance import atomic_json, file_sha256, fingerprint


def selected_source(ep_id):
    from app.services.studio_workspace import library_path
    value = W.load(ep_id)['voice'].get('master')
    parent = W.folder(ep_id) / 'parent.json'
    if not value and parent.exists():
        value = W.load(W.read(parent)['parent_episode'])['voice'].get('master')
    if not value:
        raise ValueError('请先选定外部母带，或在母集登记母带')
    source = library_path(value)
    # Adjusting a selection must use the original clock, not trim the previous
    # excerpt again (which would silently shift start/end seconds).
    seen = set()
    while receipt_path(source).exists():
        key = str(source.resolve())
        if key in seen:
            raise ValueError('音频片段来源存在循环，请检查登记记录')
        seen.add(key)
        source = library_path(validate(ep_id, source)['parent_master'])
    return source


def receipt_path(master):
    return Path(master).with_suffix('.excerpt.json')


def _reader(path):
    try:
        return wave.open(str(path), 'rb')
    except (wave.Error, EOFError) as exc:
        raise ValueError('无损截取需要可读取的 PCM WAV 母带') from exc


def _pcm(path, start=0, stop=None):
    with _reader(path) as src:
        if src.getcomptype() != 'NONE':
            raise ValueError('无损截取需要 PCM WAV 母带')
        total = src.getnframes()
        stop = total if stop is None else stop
        if type(start) is not int or type(stop) is not int or not 0 <= start < stop <= total:
            raise ValueError('截取范围超出母带，或没有完整采样')
        params = (src.getnchannels(), src.getsampwidth(), src.getframerate())
        src.setpos(start)
        data = src.readframes(stop-start)
        if len(data) != (stop-start)*params[0]*params[1]:
            raise ValueError('母带采样不完整')
        return params, data


def validate(ep_id, master):
    row = W.read(receipt_path(master))
    if row.get('version') != 1 or row.get('episode_id') != ep_id:
        raise ValueError('音频片段不属于当前剧集')
    if row['source_sha256'] != W.document(ep_id)['source_sha256'] or row['narration_identity'] != W.narration_identity(ep_id):
        raise ValueError('音频片段对应的正文或声音方案已改变，请重新选择范围')
    source = Path(row['parent_master'])
    if file_sha256(source) != row['parent_master_sha256'] or file_sha256(master) != row['excerpt_sha256']:
        raise ValueError('母带或片段内容已改变')
    original = _pcm(source, row['start_sample'], row['end_sample'])
    if _pcm(master) != original:
        raise ValueError('片段与登记的母带范围不一致')
    return row


def create(ep_id, start_seconds, end_seconds, *, expected_identity):
    """Explicit local trim, no provider/encoder/ASR. Writes a new file only."""
    import math
    from app.orchestrator import _episode_lock
    with _episode_lock(ep_id):
        if W.identity(ep_id) != expected_identity:
            raise ValueError('方案已改变，请刷新后重新选择范围')
        for value in (start_seconds, end_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('截取起止时间必须为有限秒数')
        source = selected_source(ep_id).resolve()
        source_hash = file_sha256(source)
        with _reader(source) as src:
            rate = src.getframerate()
            total = src.getnframes()
        start, end = round(start_seconds*rate), round(end_seconds*rate)
        if start == 0 and end == total:
            reject_uncut_parent(ep_id, source)
        params, data = _pcm(source, start, end)
        row = dict(version=1, episode_id=ep_id, parent_master=str(source),
                   parent_master_sha256=source_hash, start_sample=start, end_sample=end,
                   sample_rate=rate, source_sha256=W.document(ep_id)['source_sha256'],
                   narration_identity=W.narration_identity(ep_id), listening_review='pending')
        out = paths.episode_dir(ep_id)/'production/audio-excerpts'/fingerprint(row)/'master.wav'
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            validate(ep_id, out)
        else:
            pending = out.with_suffix('.pending.wav')
            with wave.open(str(pending), 'wb') as dst:
                dst.setnchannels(params[0]); dst.setsampwidth(params[1]); dst.setframerate(params[2])
                dst.writeframes(data)
            if file_sha256(source) != source_hash:
                raise ValueError('截取期间母带被修改，未选用片段')
            os.replace(pending, out)
            row['excerpt_sha256'] = file_sha256(out)
            atomic_json(receipt_path(out), row)
        validate(ep_id, out)
        value = W.load(ep_id)
        value['voice']['master'] = str(out.resolve())
        W.save(ep_id, value, expected_revision=value['revision'])
        validate(ep_id, out)
        return out


def reject_uncut_parent(ep_id, master):
    """A subset script must not be globally aligned against the entire parent."""
    from app.services.studio_workspace import library_path
    parent = W.folder(ep_id)/'parent.json'
    if not parent.exists():
        return
    parent_id = W.read(parent)['parent_episode']
    source = W.load(parent_id)['voice'].get('master')
    if source and library_path(source).is_file() and file_sha256(master) == file_sha256(library_path(source)):
        if W.document(ep_id)['source_sha256'] != W.document(parent_id)['source_sha256']:
            raise ValueError('短样片不能用整集母带直接对齐；请在制作页选择对应起止时间，无损截取并登记音源关系，无需重新配音')
