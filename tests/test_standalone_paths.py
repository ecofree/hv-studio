"""No-media regressions for the retired-checkout boundary."""
import builtins
import json
import os
from pathlib import Path

import pytest

from app import paths
from app.services import studio_workspace as S, studio_sources as Sources
from app.services.provenance import atomic_json, file_sha256

pytestmark = pytest.mark.production


@pytest.fixture
def detached(tmp_path, monkeypatch):
    root = tmp_path/'studio'; root.mkdir()
    old = tmp_path/'retired'; old.mkdir()
    atomic_json(root/'.studio-project.json', {'legacy_path_roots': [str(old), 'D:/hv']})
    monkeypatch.setattr(paths, 'ROOT', root)
    monkeypatch.setattr(paths, 'episodes_dir', lambda: root/'workspace/episodes')
    monkeypatch.setattr(paths, 'state_dir', lambda: root/'workspace/state')
    return root, old


def deny_access(monkeypatch, old):
    """Include stat/exists: Python's normal audit hook does not cover those."""
    calls = []
    def wrap(fn):
        def guarded(value, *args, **kwargs):
            if isinstance(value, (str, bytes, os.PathLike)) and S._relative_path(value, old) is not None:
                calls.append(str(value))
                raise AssertionError('retired checkout was accessed')
            return fn(value, *args, **kwargs)
        return guarded
    for name in ('stat', 'lstat', 'scandir', 'listdir', 'open'):
        monkeypatch.setattr(os, name, wrap(getattr(os, name)))
    monkeypatch.setattr(builtins, 'open', wrap(builtins.open))
    import io
    monkeypatch.setattr(io, 'open', wrap(io.open))
    return calls


def test_import_and_update_notice_never_probe_retired_checkout(detached, monkeypatch):
    root, old = detached
    source = root/'author.md'; source.write_text('### 【第一幕：出发】\n\n玄奘走出城门。', encoding='utf-8')
    (old/'author.md').write_text('different text', encoding='utf-8')
    calls = deny_access(monkeypatch, old)
    Sources.import_authoring('independent', '独立原稿', source)
    binding = Sources.contract('independent')
    binding['inputs'][0]['original_path'] = str(old/'author.md')
    atomic_json(paths.episode_dir('independent')/'editorial/source-binding.json', binding)
    assert not Sources.issues('independent')
    assert not Sources.external_updates('independent')
    source.write_text('用户在本项目修改。', encoding='utf-8')
    assert Sources.external_updates('independent') == [str(source)]
    assert not Sources.issues('independent')  # selected snapshot is unchanged
    assert calls == []


def test_existing_original_is_never_a_media_fallback(detached, monkeypatch):
    root, old = detached
    (old/'picture.png').write_bytes(b'original differs')
    (root/'picture.png').write_bytes(b'local selected bytes')
    calls = deny_access(monkeypatch, old)
    local = S.library_path(old/'picture.png')
    assert local.read_bytes() == b'local selected bytes'
    local.unlink()
    assert not S.library_path(old/'picture.png').exists()
    assert calls == []


@pytest.mark.parametrize('address', ['D:/hv/episodes/a.wav', r'd:\HV\episodes\a.wav'])
def test_windows_aliases_are_lexical_and_portable(detached, address):
    root, _ = detached
    assert S.library_path(address) == root/'episodes/a.wav'
    assert not S.is_legacy_path('D:/hv-studio/episodes/a.wav')
    assert not S.is_legacy_path('D:/hv/../hv-other/episodes/a.wav')
    with pytest.raises(ValueError):
        S.library_path(root/'../outside.txt')


def test_all_episode_media_paths_rebase_without_rewriting_record(detached, monkeypatch):
    root, old = detached
    from app.managers import episode as E
    monkeypatch.setattr(E, 'ROOT', root)
    old_media = old/'episodes/old'; old_media.mkdir(parents=True)
    (old_media/'frame.wav').write_bytes(b'old audio')
    fields = ('image_path', 'upscaled_path', 'motion_path', 'i2v_path', 'audio_segment_path')
    shot = dict(id='s',frame_id='f',index=0,duration_ms=100,audio_start_ms=0,audio_end_ms=100,prompt='test',
                ref_pack_paths=[str(old_media/'ref.png')], **{k:str(old_media/k) for k in fields})
    raw = dict(id='history',title='history',script_path=str(old_media/'script.md'),
               frames=[dict(id='f',text='原手工帧',audio_path=str(old_media/'frame.wav'))],shots={'s':shot})
    file = paths.episode_json('history'); atomic_json(file, raw)
    before = file_sha256(file)
    calls = deny_access(monkeypatch, old)
    ep = E.load('history')
    assert ep.script_path == (root/'episodes/old/script.md').as_posix()
    assert ep.frames[0].audio_path == (root/'episodes/old/frame.wav').as_posix()
    for field in fields:
        assert getattr(ep.shots['s'], field) == (root/'episodes/old'/field).as_posix()
    assert ep.shots['s'].ref_pack_paths == [(root/'episodes/old/ref.png').as_posix()]
    assert file_sha256(file) == before and calls == []


def test_history_import_preserves_local_frames_with_old_addresses(detached, monkeypatch):
    root, old = detached
    local = root/'episodes/old'; local.mkdir(parents=True)
    (local/'script.md').write_text('原始正文。',encoding='utf-8')
    raw = dict(id='old',title='old',script_path=str(old/'episodes/old/script.md'),
               frames=[dict(id='hand-edited',text='原始正文。',audio_path=str(old/'episodes/old/audio.wav'))],
               shots={},state={'approved':True})
    source = root/'episodes/old.json'; atomic_json(source, raw); before = source.read_bytes()
    calls = deny_access(monkeypatch, old)
    S.import_episode('old', 'new')
    data = json.loads(paths.episode_json('new').read_text('utf-8'))
    assert data['frames'][0]['id'] == 'hand-edited'
    assert data['frames'][0]['audio_path'] == str(local/'audio.wav')
    assert data['state'] == {} and source.read_bytes() == before and calls == []


def test_retired_guard_covers_read_list_and_write_without_installing_global_hook(detached, monkeypatch):
    import sys
    _, old = detached
    callbacks = []
    monkeypatch.setattr(S, 'config', lambda root=None: {'legacy_path_roots': [str(old)]})
    monkeypatch.setattr(sys, '_hv_studio_guard', False, raising=False)
    monkeypatch.setattr(sys, 'addaudithook', callbacks.append)
    S.install_original_write_guard()
    for event, args in [('open', (str(old/'a'), 'r', 0)), ('open', (str(old/'a'), 'w', 1)),
                        ('os.scandir', (str(old),)), ('os.remove', (str(old/'a'),))]:
        with pytest.raises(PermissionError): callbacks[0](event, args)


@pytest.mark.parametrize('status', ['completed', 'audio_ready'])
def test_external_recording_registration_loads_without_mutation_or_approval(detached, status):
    root, _ = detached
    from app.managers.episode import load
    file = paths.episode_json('imported')
    atomic_json(file,dict(id='imported',title='原题',status=status,script_path=str(root/'author.md'),
                          audio_path=str(root/'master.wav'),notes='原备注',schema_version='0.9'))
    before = file.read_bytes()
    ep = load('imported')
    assert ep.status.value == 'scripted' and ep.notes == '原备注'
    assert file.read_bytes() == before


def test_unknown_completed_video_is_not_silently_downgraded():
    from app.managers.episode import decode_episode
    with pytest.raises(ValueError):
        decode_episode(dict(id='video',title='video',status='completed'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows venv redirector contract')
@pytest.mark.parametrize('changed', ['none', 'pid', 'created', 'parent_exe', 'child_exe', 'command'])
def test_only_exact_venv_redirector_child_is_owned(monkeypatch, tmp_path, changed):
    import sys
    from types import SimpleNamespace
    from app.services import batch_worker as B
    executable = tmp_path/'venv/python.exe'; base = tmp_path/'base/python.exe'
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, '_base_executable', str(base))
    parent = SimpleNamespace(pid=11 if changed=='pid' else 10,
        create_time=lambda:2. if changed=='created' else 1.,
        exe=lambda:str(base if changed=='parent_exe' else executable),
        cmdline=lambda:['python','other.py' if changed=='command' else 'worker.py'])
    child = SimpleNamespace(pid=12,parent=lambda:parent,
        exe=lambda:str(executable if changed=='child_exe' else base),
        cmdline=lambda:['python','worker.py'])
    monkeypatch.setattr(B.psutil, 'Process', lambda:child)
    assert B._owns_current_process(10, 1.) is (changed=='none')
