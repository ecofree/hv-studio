"""Provision pinned media dependencies on disposable Windows CI runners only.

Downloads are setup operations, never test/provider calls. No font binaries are
committed or uploaded. Keep production settings and historical materials intact.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import zipfile

if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_OS') != 'Windows':
    raise SystemExit('This provisioner is restricted to disposable Windows Actions runners')

FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip'
FFMPEG_SHA256 = '60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
FONT_URL = 'https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Serif/Variable/TTF/Subset/NotoSerifSC-VF.ttf'
FONT_BLOB = '914d51db3ef9841a98ccb8f48db9e8bddb0b0d72'


def download(url, limit):
    with urllib.request.urlopen(url, timeout=180) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError('Dependency exceeds its bounded download size')
    return content


archive = download(FFMPEG_URL, 200 * 1024 * 1024)
if hashlib.sha256(archive).hexdigest() != FFMPEG_SHA256:
    raise ValueError('FFmpeg archive differs from the publisher checksum')
root = (Path(os.environ['RUNNER_TEMP']) / 'hv-media-tools').resolve()
root.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(archive)) as package:
    for item in package.infolist():
        if not (root / item.filename).resolve().is_relative_to(root):
            raise ValueError('Unsafe dependency archive path')
    package.extractall(root)
executables = list(root.rglob('ffmpeg.exe'))
if len(executables) != 1 or not executables[0].with_name('ffprobe.exe').is_file():
    raise ValueError('Missing or ambiguous FFmpeg/ffprobe installation')
binary = executables[0]
with open(os.environ['GITHUB_PATH'], 'a', encoding='utf-8') as stream:
    stream.write(str(binary.parent) + '\n')
version = subprocess.check_output([str(binary), '-version'], text=True).splitlines()[0]

font = download(FONT_URL, 40 * 1024 * 1024)
if hashlib.sha1(b'blob ' + str(len(font)).encode() + b'\0' + font).hexdigest() != FONT_BLOB:
    raise ValueError('Noto Serif SC differs from the pinned upstream Git blob')
target = Path(os.environ['WINDIR']) / 'Fonts' / 'NotoSerifSC-VF.ttf'
if target.exists() and target.read_bytes() != font:
    raise ValueError('Refuse to overwrite a different installed font')
target.write_bytes(font)
from PIL import ImageFont
ImageFont.truetype(str(target), 72)
report = dict(ffmpeg_url=FFMPEG_URL, ffmpeg_sha256=FFMPEG_SHA256, ffmpeg_version=version,
              font_url=FONT_URL, font_git_blob=FONT_BLOB,
              font_sha256=hashlib.sha256(font).hexdigest())
Path('ci-evidence').mkdir(exist_ok=True)
Path('ci-evidence/media-environment.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
