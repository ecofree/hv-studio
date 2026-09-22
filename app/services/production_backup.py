"""Consistent, hash-verified production snapshots; no credentials or auto-resume."""
import json
import shutil
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, file_sha256
from app.services.process_lock import file_lock


def selected_files(episodes):
    root=paths.ROOT.resolve();files=set();external={}
    for eid in episodes:
        directory=paths.episode_dir(eid);source=paths.episode_json(eid)
        if not source.is_file(): raise ValueError('episode record missing: '+eid)
        files.add(source)
        required=set()
        published=directory/'production/v2/published.json'
        if published.exists():
            pointer=read(published)
            required.update(Path(pointer[k]) for k in ('candidate_path','receipt','review','listening_review') if pointer.get(k))
        state=directory/'production/v2/run.json'
        if state.exists():
            record=read(state)
            if record.get('receipt_path'):
                receipt_path=Path(record['receipt_path']);receipt=read(receipt_path)
                required.update([receipt_path,Path(receipt['candidate_path'])])
                for name in receipt.get('inputs',{}):
                    p=Path(name).resolve()
                    if p.is_relative_to(root): required.add(p)
                    else: external[str(p)]=file_sha256(p)
        for p in directory.rglob('*'):
            if not p.is_file() or p.suffix in ('.lock','.tmp','.pyc'): continue
            if p.name in ('.produce.lock',) or '__pycache__' in p.parts: continue
            if 'renders' in p.relative_to(directory).parts and p.resolve() not in {r.resolve() for r in required}: continue
            files.add(p)
        files.update(required)
    for relative in ('state/production-queue','state/voice_assets','state/audio_rejections','state/acceptance'):
        folder=root/relative
        files.update(p for p in folder.rglob('*') if p.is_file() and p.suffix=='.json' and p.name!='worker.json')
    files.update(p for name in ('PAUSE','STOP') if (p := root/'state/production-queue'/name).is_file())
    for relative in ('app','bin','scripts','.ai-project'):
        files.update(p for p in (root/relative).rglob('*') if p.is_file() and p.suffix in ('.py','.yaml','.yml','.ps1','.json','.toml','.md') and '__pycache__' not in p.parts)
    files.update(p for p in root.glob('requirements*.txt') if p.is_file())
    files.update(p for p in (root/'pyproject.toml',root/'AGENTS.md') if p.is_file())
    # Follow local media/receipt dependencies, including approved historical
    # candidates and shared voice/reference assets. Never collect credentials.
    visited=set()
    def visit(value):
        if isinstance(value,dict):
            for v in value.values(): visit(v)
        elif isinstance(value,list):
            for v in value: visit(v)
        elif isinstance(value,str) and len(value)<4096 and '\n' not in value:
            try:
                p=Path(value)
                if p.suffix.lower() not in ('.json','.wav','.mp3','.mp4','.png','.jpg','.webp','.ttf','.otf'): return
                if not p.is_absolute():p=root/p
                if not p.is_file():return
                p=p.resolve()
                if p.is_relative_to(root):files.add(p)
                else:external[str(p)]=file_sha256(p)
            except OSError:pass
    while True:
        pending=[p for p in files if p.suffix=='.json' and p not in visited]
        if not pending:break
        for p in pending:
            visited.add(p);visit(read(p))
    for p in files:
        if not p.resolve().is_relative_to(root): raise ValueError('backup source escapes project')
    return sorted(files),external


def create_snapshot(episodes, destination, *, dry_run=False):
    if not episodes or len(set(episodes))!=len(episodes): raise ValueError('explicit unique episode list required')
    root=paths.ROOT.resolve();destination=Path(destination).resolve()
    if destination.is_relative_to(root): raise ValueError('backup destination must be outside source project')
    with ExitStack() as stack:
        stack.enter_context(file_lock(paths.state_dir()/'production-storage.lock'))
        stack.enter_context(file_lock(paths.state_dir()/'production-queue/worker.lock'))
        files,external=selected_files(episodes)
        total=sum(p.stat().st_size for p in files)
        if dry_run: return dict(files=len(files),bytes=total,episodes=episodes,external_dependencies=external)
        parent=destination
        while not parent.exists(): parent=parent.parent
        if shutil.disk_usage(parent).free < total+512*1024**2: raise ValueError('backup disk space insufficient')
        target=destination/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);target.mkdir(parents=True)
        atomic_json(target/'incomplete.json',dict(episodes=episodes,started_at=time.time()))
        records={}
        for source in files:
            relative=source.relative_to(root).as_posix();sha=file_sha256(source)
            out=target/'payload'/relative;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,out)
            if file_sha256(out)!=sha: raise ValueError('backup copy hash mismatch')
            records[relative]=dict(sha256=sha,bytes=source.stat().st_size)
        current,_=selected_files(episodes)
        if set(current)!=set(files) or any(file_sha256(root/r)!=v['sha256'] for r,v in records.items()):
            raise ValueError('source changed during snapshot; incomplete copy is not a backup')
        atomic_json(target/'manifest.json',dict(schema_version=1,source_root=str(root),episodes=episodes,files=records,
            external_dependencies=external,credentials_included=False,automatic_resume=False,created_at=time.time()))
        (target/'incomplete.json').unlink()
        return dict(snapshot=str(target),files=len(records),bytes=total)


def verify_snapshot(snapshot):
    snapshot=Path(snapshot).resolve()
    if (snapshot/'incomplete.json').exists(): raise ValueError('snapshot is incomplete')
    manifest=read(snapshot/'manifest.json')
    if manifest.get('schema_version')!=1 or not isinstance(manifest.get('files'),dict) or not manifest['files']:
        raise ValueError('invalid snapshot manifest')
    payload=(snapshot/'payload').resolve()
    for relative,record in manifest['files'].items():
        path=(payload/relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(payload): raise ValueError('snapshot path escape')
        if not path.is_file() or path.stat().st_size!=record['bytes'] or file_sha256(path)!=record['sha256']:
            raise ValueError('snapshot file missing or changed: '+relative)
    return manifest


def restore_snapshot(snapshot,destination):
    manifest=verify_snapshot(snapshot);snapshot=Path(snapshot).resolve();destination=Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(paths.ROOT.resolve()) or destination.is_relative_to(snapshot):
        raise ValueError('restore requires a new directory outside the project and snapshot')
    destination.mkdir(parents=True)
    for relative,record in manifest['files'].items():
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(snapshot/'payload'/relative,target)
        if file_sha256(target)!=record['sha256']: raise ValueError('restored file mismatch')
    atomic_json(destination/'RESTORE-INFO.json',dict(snapshot=str(snapshot),source_root=manifest['source_root'],
        automatic_resume=False,requires_path_rebinding=True,credentials_included=False,verified_files=len(manifest['files'])))
    return dict(destination=str(destination),verified_files=len(manifest['files']),automatic_resume=False)
