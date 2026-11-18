"""Shared settings and revision-bound WAV reuse for the external Charon recorder."""
import hashlib
import json
import os
from pathlib import Path


def api_key():
    name = 'HV_CHARON_API_KEY'
    key = os.environ.get(name)
    if not key and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as env:
                key = winreg.QueryValueEx(env, name)[0]
        except FileNotFoundError:
            pass
    if not key:
        raise ValueError('请在本机配置 HV_CHARON_API_KEY')
    return key


def recording_id(text, model, voice):
    return hashlib.sha256(json.dumps([text, model, voice],ensure_ascii=False).encode()).hexdigest()


def digest(path):
    with Path(path).open('rb') as stream:
        h=hashlib.sha256()
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            h.update(chunk)
        return h.hexdigest()


def recording_path(original, identity):
    """Imported WAVs stay in place. Revised recordings have their own directory."""
    original = Path(original)
    if matches(original,identity) or not original.exists():
        return original
    return original.parent/'revisions'/identity/original.name


def matches(path, identity):
    sidecar = path.with_suffix('.recording.json')
    if not path.is_file() or not sidecar.is_file():
        return False
    receipt=json.loads(sidecar.read_text(encoding='utf-8'))
    return receipt.get('input')==identity and receipt.get('wav_sha256')==digest(path)


def remember(path, identity, *, origin='recorded'):
    from tempfile import NamedTemporaryFile
    path=Path(path)
    with NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
        json.dump(dict(input=identity,wav_sha256=digest(path),origin=origin),f)
    os.replace(f.name,path.with_suffix('.recording.json'))
