"""Read-only executable/config dependency audit; no models or historical jobs."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def inspect(root=ROOT):
    from app.services.studio_workspace import legacy_path_roots, _relative_path
    from app.services.provenance import file_sha256
    retired=legacy_path_roots(root)
    issues=[]; checked={}
    def old_reference(value):
        normal=str(value).replace('\\\\','\\').replace('\\','/').casefold()
        return any(re.search(re.escape(str(p).replace('\\','/').rstrip('/').casefold())+r'(?=/|$)',normal) for p in retired)
    for folder in ('app','bin','scripts','resource/风趣历史.writing-DNA/scripts'):
        for p in (root/folder).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix not in {'.py','.ps1','.bat','.yaml','.yml','.toml'}:continue
            checked[p.relative_to(root).as_posix()]=file_sha256(p)
            text=p.read_text('utf-8-sig')
            if p.suffix=='.py':
                tree=ast.parse(text)
                docs={id(n.value) for n in ast.walk(tree) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant)}
                hits=[n.lineno for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)
                      and id(n) not in docs and old_reference(n.value)]
            else:hits=[i+1 for i,line in enumerate(text.splitlines()) if not line.lstrip().startswith('#') and old_reference(line)]
            if hits:issues.append(dict(file=str(p.relative_to(root)),kind='executable_reference',lines=hits))
    for p in root.glob('*.bat'):
        checked[p.name]=file_sha256(p)
        if old_reference(p.read_text('utf-8-sig')):issues.append(dict(file=p.name,kind='launcher_reference'))
    for name in ('.venv-core','.venv-asr','.venv-qwen3-tts'):
        env=root/name
        if not env.exists():continue
        files=[env/'pyvenv.cfg',*sorted((env/'Lib/site-packages').glob('*.pth')),*sorted((env/'Lib/site-packages').glob('*.egg-link'))]
        for p in files:
            if p.is_file():
                checked[p.relative_to(root).as_posix()]=file_sha256(p)
                if old_reference(p.read_text('utf-8-sig')):issues.append(dict(file=str(p.relative_to(root)),kind='runtime_reference'))
        for p in (env,env/'Scripts',env/'Lib',env/'Lib/site-packages'):
            resolved=p.resolve()
            if any(_relative_path(resolved,old) is not None for old in retired):
                issues.append(dict(file=str(p.relative_to(root)),kind='runtime_link'))
    envfile=root/'.env'
    if envfile.is_file():
        for line in envfile.read_text('utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#') and old_reference(line.split('=',1)[1]):
                issues.append(dict(variable=line.split('=',1)[0].strip(),kind='environment_file_reference'))
    for key in ('HV_PYTHON','PYTHONPATH','PYTHONHOME','HF_HOME','TRANSFORMERS_CACHE'):
        if old_reference(os.environ.get(key,'')):issues.append(dict(variable=key,kind='environment_reference'))
    return dict(status='PASS' if not issues else 'FAIL',issues=issues,checked_files=checked,
                scope='executable_config_and_runtime_paths',models_started=False,
                historical_addresses='preserved as provenance; resolved locally without fallback')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=inspect()
    if args.output:
        from app.services.provenance import atomic_json
        atomic_json(args.output,result)
    print(json.dumps(dict(status=result['status'],checked_files=len(result['checked_files']),issues=result['issues']),ensure_ascii=False))
    return 0 if result['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
