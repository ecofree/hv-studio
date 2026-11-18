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


def selected_files(episodes, *, missing=None):
    from app.services.studio_workspace import asset_path
    missing = [] if missing is None else missing
    root=paths.ROOT.resolve();files=set();external={}
    def inventory(path):
        try:
            return read(path)
        except (OSError, ValueError) as exc:
            missing.append(dict(path=str(path), reference=str(path), reason=type(exc).__name__))
            return {}

    def dependency(path):
        if not path.is_file():
            missing.append(dict(path=str(path), reference=str(path), reason='missing'))
        elif path.is_relative_to(root):
            files.add(path)
        else:
            external[str(path)] = file_sha256(path)

    for eid in episodes:
        directory=paths.episode_dir(eid);source=paths.episode_json(eid)
        if not source.is_file(): raise ValueError('episode record missing: '+eid)
        files.add(source)
        journal = paths.state_dir()/'source-imports'/(eid+'.json')
        if journal.is_file(): files.add(journal)
        required=set()
        published=directory/'production/v2/published.json'
        if published.exists():
            pointer=inventory(published)
            required.update(asset_path(pointer[k]) for k in ('candidate_path','receipt','review','listening_review') if pointer.get(k))
        state=directory/'production/v2/run.json'
        if state.exists():
            record=inventory(state)
            if record.get('receipt_path'):
                receipt_path=asset_path(record['receipt_path'])
                required.add(receipt_path)
                if receipt_path.is_file():
                    receipt=inventory(receipt_path)
                    if receipt.get('candidate_path'):
                        required.add(asset_path(receipt['candidate_path']))
                    else:
                        missing.append(dict(reference=str(receipt_path), reason='candidate path missing'))
                    required.update(asset_path(name) for name in receipt.get('inputs', {}))
        for p in directory.rglob('*'):
            if not p.is_file() or p.suffix in ('.lock','.tmp','.pyc'): continue
            if p.name in ('.produce.lock',) or '__pycache__' in p.parts: continue
            if 'renders' in p.relative_to(directory).parts and p.resolve() not in {r.resolve() for r in required}: continue
            files.add(p)
        for path in required:
            dependency(path)
    active_state = paths.state_dir().resolve()
    if not active_state.is_relative_to(root):
        raise ValueError('active state escapes project')
    # Keep shared accounting and rejection records byte-for-byte. Other episodes'
    # evidence and disposable recognition caches are followed only when selected media cites them.
    retained = set()
    selected_queue_rows = []
    for relative in ('production-queue/budgets', 'audio_rejections', 'tts_requests', 'acceptance'):
        retained.update((active_state/relative).rglob('*.json'))
    files.update(retained)
    files.update((active_state/'voice_assets').rglob('*.json'))
    queue = active_state/'production-queue'
    for batch in queue.glob('*.json'):
        if batch.name == 'worker.json': continue
        try:
            job = read(batch)
        except (OSError, ValueError):
            continue
        rows = job.get('episodes') if isinstance(job, dict) else None
        if not isinstance(rows, dict) or not set(episodes).intersection(rows): continue
        files.add(batch)
        retained.add(batch)
        if job.get('budget_path'):
            dependency(asset_path(job['budget_path']))
        for eid in set(episodes).intersection(rows):
            row = rows[eid]
            selected_queue_rows.append(row)
            token = row.get('token') if isinstance(row, dict) else None
            if isinstance(token,str) and len(token)==32 and all(c in '0123456789abcdef' for c in token):
                result = queue/'results'/(token+'.json')
                if result.is_file(): files.add(result)
    from app.services.studio_jobs import _request_owner
    for request in (active_state/'studio-jobs').glob('*/request.json'):
        owner, damaged = _request_owner(request)
        if owner.get('ep_id') in episodes:
            if damaged:
                missing.append(dict(reference=str(request), reason='unreadable job request'))
            files.update(p for p in request.parent.rglob('*.json') if p.name != 'process.json')
    files.update(p for name in ('PAUSE', 'STOP')
                 if (p := active_state/'production-queue'/name).is_file())
    files.update(p for p in active_state.glob('*.json') if p.is_file())
    marker = root/'.studio-project.json'
    if marker.is_file(): files.add(marker)
    for relative in ('app','bin','tests','scripts','.ai-project'):
        files.update(p for p in (root/relative).rglob('*') if p.is_file() and p.suffix in ('.py','.yaml','.yml','.ps1','.json','.toml','.md') and '__pycache__' not in p.parts)
    files.update(p for p in root.glob('requirements*.txt') if p.is_file())
    files.update(p for p in root.glob('*.bat') if p.is_file())
    files.update(p for p in (root/'pyproject.toml',root/'AGENTS.md') if p.is_file())
    from app.services.production_acceptance import FIXED_SOURCE_FILES
    installed = (root/'bin/check_project.py').is_file()
    for relative in FIXED_SOURCE_FILES:
        if installed or (root/relative).is_file():
            dependency(root/relative)
    # Follow local media/receipt dependencies, including approved historical
    # candidates and shared voice/reference assets. Never collect credentials.
    visited=set(retained)
    def visit(value, field=''):
        # Render segments are disposable caches, including after explicit V2
        # cleanup. They are not required to restore the retained candidate.
        if field=='segments' and isinstance(value,list) and all(isinstance(v,dict) and str(v.get('id','')).startswith('seg-') for v in value):return
        if field in ('render_state_path','reused_from'):return
        if isinstance(value,dict):
            for k,v in value.items(): visit(v,k)
        elif isinstance(value,list):
            for v in value: visit(v)
        elif isinstance(value,str) and len(value)<4096 and '\n' not in value:
            try:
                # Provenance records are retained, but retired sources are not
                # dependencies. All actual dependencies use the local resolver.
                if field in ('original_path','original_root'): return
                raw=Path(value)
                if raw.suffix.lower() not in ('.json','.md','.wav','.mp3','.mp4','.png','.jpg','.jpeg','.webp','.ttf','.otf'): return
                if not raw.is_absolute() and '/' not in value and '\\' not in value: return
                p=asset_path(value)
                if not p.is_file():
                    missing.append(dict(path=str(p),reference=value,reason='missing'))
                    return
                if p.is_relative_to(root):files.add(p)
                else:external[str(p)]=file_sha256(p)
            except (OSError,ValueError) as exc:
                missing.append(dict(reference=value,reason=type(exc).__name__))
    for row in selected_queue_rows:
        visit(row)
    while True:
        pending=[p for p in files if p.suffix=='.json' and p not in visited]
        if not pending:break
        for p in pending:
            visited.add(p)
            record = inventory(p)
            visit(record)
    for p in files:
        if not p.resolve().is_relative_to(root): raise ValueError('backup source escapes project')
    return sorted(files),external


def create_snapshot(episodes, destination, *, dry_run=False):
    if not episodes or len(set(episodes))!=len(episodes): raise ValueError('explicit unique episode list required')
    root=paths.ROOT.resolve();destination=Path(destination).resolve()
    if destination.is_relative_to(root): raise ValueError('backup destination must be outside source project')
    with ExitStack() as stack:
        stack.enter_context(file_lock(paths.state_dir()/'production-queue/worker.lock'))
        stack.enter_context(file_lock(paths.state_dir()/'production-storage.lock'))
        from app.services.production_storage import episode_execution
        for eid in sorted(episodes):
            stack.enter_context(episode_execution(eid))
        from app.services.production_storage import require_idle_resources
        require_idle_resources()
        missing=[]
        files,external=selected_files(episodes,missing=missing)
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError as exc:
                missing.append(dict(reference=str(path), path=str(path), reason=type(exc).__name__))
        if dry_run: return dict(files=len(files),bytes=total,episodes=episodes,external_dependencies=external,
                                missing_dependencies=missing,complete_dependencies=not missing and not external)
        if missing:
            raise ValueError('backup dependencies missing or unreadable; use --dry-run for the list: '+
                             '; '.join(r.get('path',r['reference']) for r in missing[:5]))
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
        changed_missing = []
        current, current_external = selected_files(episodes, missing=changed_missing)
        if changed_missing or current_external != external or set(current)!=set(files) or any(file_sha256(root/r)!=v['sha256'] for r,v in records.items()):
            raise ValueError('source changed during snapshot; incomplete copy is not a backup')
        from app.services.production_acceptance import source_fingerprint, FIXED_SOURCE_FILES
        source_sha = source_fingerprint(root) if all((root/name).is_file() for name in FIXED_SOURCE_FILES) else None
        if source_sha and source_fingerprint(target/'payload') != source_sha:
            raise ValueError('backup does not reproduce the project acceptance fingerprint')
        from app.services.production_identity import implementation_identity
        parent_restore = read(root/'RESTORE-INFO.json') if (root/'RESTORE-INFO.json').is_file() else {}
        old_roots = [*parent_restore.get('path_aliases', []), parent_restore.get('source_root')]
        atomic_json(target/'manifest.json',dict(schema_version=2,source_root=str(root),episodes=episodes,files=records,
            snapshot_scope='project_sources_and_episodes' if source_sha else 'episode_data',
            path_aliases=list(dict.fromkeys(p for p in old_roots if p)), project_source_fingerprint=source_sha,
            required_implementation=None if source_sha else implementation_identity(),
            state_relative=paths.state_dir().resolve().relative_to(root).as_posix(),
            episodes_relative=paths.episodes_dir().resolve().relative_to(root).as_posix(),
            external_dependencies=external,credentials_included=False,automatic_resume=False,created_at=time.time()))
        (target/'incomplete.json').unlink()
        return dict(snapshot=str(target),files=len(records),bytes=total)


def verify_snapshot(snapshot):
    snapshot=Path(snapshot).resolve()
    if (snapshot/'incomplete.json').exists(): raise ValueError('snapshot is incomplete')
    manifest=read(snapshot/'manifest.json')
    if manifest.get('schema_version') not in (1,2) or not isinstance(manifest.get('files'),dict) or not manifest['files']:
        raise ValueError('invalid snapshot manifest')
    payload=(snapshot/'payload').resolve()
    for relative,record in manifest['files'].items():
        path=(payload/relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(payload): raise ValueError('snapshot path escape')
        if not path.is_file() or path.stat().st_size!=record['bytes'] or file_sha256(path)!=record['sha256']:
            raise ValueError('snapshot file missing or changed: '+relative)
    if manifest.get('schema_version') == 2:
        for key in ('state_relative', 'episodes_relative'):
            relative = manifest.get(key)
            if not isinstance(relative, str) or Path(relative).is_absolute() or not (payload/relative).resolve().is_relative_to(payload):
                raise ValueError('invalid snapshot workspace layout')
    if (payload/'bin/check_project.py').is_file() and (payload/'.studio-project.json').is_file():
        from app.services.production_acceptance import FIXED_SOURCE_FILES
        absent = [p for p in FIXED_SOURCE_FILES if p not in manifest['files']]
        if absent:
            raise ValueError('snapshot is missing required project files: '+', '.join(absent))
    if manifest.get('project_source_fingerprint'):
        from app.services.production_acceptance import source_fingerprint
        if source_fingerprint(payload) != manifest['project_source_fingerprint']:
            raise ValueError('backup project acceptance fingerprint differs')
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
    # Restored queue/process identities still point to the original machine.
    # Never let a newly launched worker run them before explicit rebinding.
    state_relative = manifest.get('state_relative', 'workspace/state'
        if (destination/'.studio-project.json').is_file() else 'state')
    stop = destination/state_relative/'production-queue/STOP'
    if not stop.exists():
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text('restored workspace: rebind and review before resuming', encoding='utf-8')
    from app.services.production_identity import implementation_identity
    required = manifest.get('required_implementation')
    info = dict(snapshot=str(snapshot), source_root=manifest['source_root'], path_aliases=manifest.get('path_aliases', []),
                snapshot_scope=manifest.get('snapshot_scope','historical_archive'),
                project_source_fingerprint=manifest.get('project_source_fingerprint'), required_implementation=required,
                runtime_matches_creator=implementation_identity()==required if required else None,
                external_dependencies=manifest.get('external_dependencies', {}), automatic_resume=False,
                requires_path_rebinding=True, credentials_included=False, verified_files=len(manifest['files']))
    atomic_json(destination/'RESTORE-INFO.json', info)
    return dict(destination=str(destination), **info)
