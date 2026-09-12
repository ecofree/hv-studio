"""Build a pinned offline wheelhouse from the already installed core closure.

No optional GPU/face dependencies and no network. Each wheel records its origin
as the currently installed distribution; this is not a PyPI origin attestation.
"""
import argparse
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
from pathlib import Path
import re
import zipfile
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def require_module_payload(entries, modules, name):
    names = [str(entry).replace('\\','/') for entry in entries]
    missing = [module for module in modules if module.isidentifier() and not any(
        entry.startswith(module+'/') or entry==module+'.py' or
        (entry.startswith(module+'.') and entry.endswith('.pyd')) for entry in names)]
    if missing: raise RuntimeError(f'{name}: module payload missing: {missing}; metadata-only snapshot is unusable')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = Path(args.output); destination.mkdir(parents=True, exist_ok=True)
    roots = [line.strip() for line in (root/'requirements.lock.txt').read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.startswith('#')]+['pytest']
    queue, selected = [Requirement(line) for line in roots], {}
    while queue:
        requirement = queue.pop()
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}): continue
        name = canonicalize_name(requirement.name); dist = metadata.distribution(name)
        if requirement.specifier and dist.version not in requirement.specifier:
            raise RuntimeError(f'installed core dependency conflict: {name} {dist.version} requires {requirement.specifier}')
        if name in selected: continue
        selected[name] = dist
        queue.extend(Requirement(line) for line in dist.requires or [])
    lock = '# Complete Windows / Python 3.13 core + acceptance closure; optional ASR is separate.\n'
    lock += '\n'.join(f'{name}=={dist.version}' for name, dist in sorted(selected.items()))+'\n'
    (root/'requirements.core.lock.txt').write_text(lock, encoding='utf-8')
    records = []
    for name, dist in sorted(selected.items()):
        files = [p for p in dist.files or [] if '..' not in Path(str(p)).parts and '__pycache__' not in str(p)
                 and not str(p).endswith(('.pyc', '/RECORD', '\\RECORD'))]
        modules = (dist.read_text('top_level.txt') or '').splitlines()
        require_module_payload(files,modules,name)
        wheels = [p for p in files if str(p).endswith('.dist-info/WHEEL')]
        if len(wheels) != 1: raise RuntimeError('installed distribution lacks wheel metadata: '+name)
        info = dist.locate_file(wheels[0]).read_text(encoding='utf-8')
        from packaging.tags import sys_tags
        compatible = {str(t) for t in sys_tags()}
        tag = next(t.strip() for t in re.findall(r'^Tag: (.+)$', info, re.MULTILINE) if t.strip() in compatible)
        path = destination/f'{name.replace("-", "_")}-{dist.version}-{tag}.whl'
        if path.exists():
            with zipfile.ZipFile(path) as existing:
                require_module_payload(existing.namelist(),modules,name)
            records.append(dict(name=name, version=dist.version, wheel=path.name,
                                sha256=hashlib.sha256(path.read_bytes()).hexdigest(), origin='installed_runtime_snapshot'))
            continue
        record_name = str(wheels[0]).replace('WHEEL', 'RECORD')
        rows = []
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in files:
                source = Path(dist.locate_file(entry))
                if not source.is_file(): raise RuntimeError('missing installed core file: '+str(entry))
                archive.write(source, str(entry).replace('\\', '/'))
                rows.append([str(entry).replace('\\', '/'), '', str(source.stat().st_size)])
            buf = io.StringIO(newline=''); writer = csv.writer(buf); writer.writerows(rows+[[record_name, '', '']])
            archive.writestr(record_name, buf.getvalue())
        records.append(dict(name=name, version=dist.version, wheel=path.name,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), origin='installed_runtime_snapshot'))
    (destination/'manifest.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    print(json.dumps({'distributions': len(records), 'wheelhouse': str(destination.resolve()), 'network_calls': 0}))


if __name__ == '__main__': main()
