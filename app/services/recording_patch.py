"""Preview a local pick-up against the selected external recording."""
from pathlib import Path
import hashlib
import tempfile
import wave

from app import paths
from app.orchestrator import _episode_lock
from app.services import studio_workflow as W
from app.services.external_narration import canonical_master
from app.services.narration_pipeline import read, validate_assembly
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.studio_audio_excerpt import _pcm


def _wav(path, rate, pcm):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(pcm)


def preview(eid, manifest_path, unit_id, start, stop, pickup, *, expected_identity):
    """Use sample offsets in the canonical master; preserve both sides verbatim."""
    with _episode_lock(eid):
        root = paths.episode_dir(eid)/'production/v2'
        if read(root/'run.json').get('manifest_path') != str(Path(manifest_path).resolve()) or W.identity(eid) != expected_identity:
            raise ValueError('录音或方案已更新，请重新选择片段')
        manifest = validate_assembly(manifest_path)
        if manifest.get('provenance') != 'external_recording':
            raise ValueError('请选择外部录音')
        plan = read(manifest['plan_path'])
        chosen = Path(W.load(eid)['voice'].get('master') or '.')
        if chosen.parent.joinpath('edit.json').is_file() and chosen.is_file() and file_sha256(chosen) != manifest['source_recording']['delivered_sha256']:
            raise ValueError('请先对齐已选用的新版母带，再继续补录')
        if W.document(eid)['source_sha256'] != plan['source_sha256']:
            raise ValueError('正文已更新，请先对齐当前录音')
        unit = next((u for u in manifest['alignment'] if u['unit_id'] == unit_id), None)
        if not unit or not (unit['sample_start'] <= start < stop <= unit['sample_end']):
            raise ValueError('补录范围须在所选旁白段内')
        rate = manifest['sample_rate']
        parent = Path(manifest['master_path'])
        params, original = _pcm(parent)
        if file_sha256(parent)!=manifest['master_sha256']:
            raise ValueError('读取时母带发生变化，请重新载入')
        base = root/'audio-edits'
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            take = Path(tmp)/'pickup.wav'
            canonical_master(pickup, take)
            fmt, new = _pcm(take)
        if fmt != params:
            raise ValueError('补录采样格式与当前录音不同')
        record = dict(kind='external_pickup_v1', episode=eid, manifest_path=str(manifest_path),
                      manifest_sha256=file_sha256(manifest_path), source_master_sha256=manifest['source_recording']['delivered_sha256'],
                      old_source_sha256=plan['source_sha256'], parent_sha256=manifest['master_sha256'],
                      selection_identity=expected_identity, narration_identity=W.narration_identity(eid),
                      unit_id=unit_id, start=start, stop=stop,
                      pickup_pcm_sha256=hashlib.sha256(new).hexdigest(), inserted_samples=len(new)//2)
        dest = base/fingerprint(record)
        dest.mkdir(exist_ok=True)
        before, after = original[:2*start], original[2*stop:]
        output = dest/'master.wav'
        # A published receipt always refers to a fully written master.
        temporary = dest/'master.pending.wav'
        _wav(temporary, rate, before+new+after)
        temporary.replace(output)
        _wav(dest/'preview.wav', rate, before[-4*rate:]+new+after[:4*rate])
        atomic_json(dest/'edit.json',dict(record, derived_master_sha256=file_sha256(output)))
        return output


def select(eid, master):
    with _episode_lock(eid):
        root = paths.episode_dir(eid)/'production/v2'
        master = Path(master).resolve()
        if not master.is_relative_to((root/'audio-edits').resolve()):
            raise ValueError('请选择本集生成的补录预览')
        edit = read(master.parent/'edit.json')
        value = W.load(eid)
        if (edit.get('kind') != 'external_pickup_v1' or edit['episode'] != eid
                or W.identity(eid) != edit['selection_identity']
                or read(root/'run.json').get('manifest_path') != edit['manifest_path']
                or file_sha256(edit['manifest_path']) != edit['manifest_sha256']
                or file_sha256(master) != edit['derived_master_sha256']):
            raise ValueError('预览对应的输入已变化，请重新试听补录')
        value['voice']['master'] = str(master)
        value['binding']=W.binding(eid)
        W.save(eid,value,expected_revision=value['revision'])
