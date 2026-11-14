"""Explicit detached jobs with logs; no scheduler, automatic retries or approvals."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from app import paths
from app.services.provenance import atomic_json,file_sha256,fingerprint
from app.services import studio_workflow as W


def imported_master(ep_id):
    """Offer only the original delivery already bound to this script and plan."""
    from app.services.external_narration import PROVENANCE
    state_path=paths.episode_dir(ep_id)/'production/v2/run.json'
    if not state_path.is_file():
        from app.services.studio_sources import contract,require_usable
        if not contract(ep_id): return None
        require_usable(ep_id)
        episode=json.loads(paths.episode_json(ep_id).read_text('utf-8'))
        if not episode.get('audio_path'): return None
        from app.services.studio_workspace import library_path
        path=library_path(episode['audio_path'])
        if not path.exists():raise ValueError('剧集登记中的原始旁白输入不存在')
        return str(path.resolve())
    state=W.read(state_path)
    if not state.get('manifest_path') or state.get('source_sha256')!=W.document(ep_id)['source_sha256']: return None
    manifest=W.read(state['manifest_path'])
    if manifest.get('provenance')!=PROVENANCE: return None
    plan=Path(manifest['plan_path'])
    if (manifest['plan_sha256']!=file_sha256(plan)
            or W.read(plan)['source_sha256']!=state['source_sha256']):
        raise ValueError('已导入的母带与当前正文计划不一致，请重新选择录音')
    original=manifest['source_recording']; path=Path(original['delivered_path'])
    if not path.is_file() or file_sha256(path)!=original['delivered_sha256']:
        raise ValueError('已导入的原始母带缺失或已改变，请明确选择录音')
    return str(path.resolve())


def launch(ep_id,*,max_requests=0,max_characters=0,max_images=0,external_master=None,mode='video',
           max_image_retries=0,provider_attempts_per_image=None, audio_mode="auto"):
    from app.services.process_lock import file_lock
    paths.validate_episode_id(ep_id)
    from app.services.production_storage import episode_execution
    with episode_execution(ep_id), file_lock(paths.state_dir()/'studio-jobs'/('episode-'+ep_id+'.lock')):
        require_no_active(ep_id)
        return _submit(ep_id,max_requests,max_characters,max_images,external_master,mode,
                       max_image_retries,provider_attempts_per_image,audio_mode)


def _submit(ep_id,max_requests,max_characters,max_images,external_master,mode,max_image_retries,provider_attempts_per_image,audio_mode):
    if mode not in ('video','voice','alignment'):raise ValueError('未知任务类型')
    from app.services.studio_voice_contract import path_for,load
    voice_contract=path_for(ep_id)
    from app.services.studio_narration_handoff import selected
    narration=selected(ep_id) if mode=='video' else None
    from app.services import recording_input
    from app.services.studio_workspace import library_path
    if narration and not external_master: external_master=narration['master']
    elif not external_master and not voice_contract.exists():
        external_master=W.load(ep_id)['voice'].get('master') or imported_master(ep_id)
    if not external_master and audio_mode!='auto':raise ValueError('请指定外部旁白文件或目录')
    selection=None
    if external_master:
        selection=recording_input.bind(library_path(external_master),audio_mode)
        if narration and (selection['kind']!='master' or selection['files'][0]['sha256']!=narration['master_sha256']):
            raise ValueError('已选旁白与传入音轨不同')
        external_master=str(recording_input.materialize(selection,paths.episode_dir(ep_id)/'production/v2/recordings'))
    W.require_production(ep_id, {'voice':['tts'], 'alignment':['alignment'],
                                'video':['images','timeline','compose']}[mode],external_master=external_master)
    values=(max_requests,max_characters,max_images,max_image_retries)
    if any(type(v) is not int or v<0 for v in values): raise ValueError('预算必须是非负整数')
    if (provider_attempts_per_image is not None
            and (type(provider_attempts_per_image) is not int or provider_attempts_per_image<1)):
        raise ValueError('每张图片的尝试次数必须是正整数')
    from app.services.studio_workspace import config
    if (mode=='voice' and not external_master and not voice_contract.exists() and config(paths.ROOT)
            and paths.episode_dir(ep_id).resolve().is_relative_to((paths.ROOT/'workspace').resolve())):
        raise ValueError('请先在样片试听页绑定已确认的系列声音方案')
    if voice_contract.exists():
        load(voice_contract,source_sha256=W.document(ep_id)['source_sha256'],voice=W.load(ep_id)['voice'])
        if mode=='video' and not narration:
            run_path=paths.episode_dir(ep_id)/'production/v2/run.json'
            existing=json.loads(run_path.read_text('utf-8')) if run_path.exists() else {}
            if not existing.get('manifest_path'):raise ValueError('请先制作并选用当前新稿的旁白候选，再制作视频')
    if external_master and (max_requests or max_characters):
        raise ValueError('复用已有录音时，请将合成调用与字符上限设为零')
    if external_master and not voice_contract.exists():
        W.require_voice_binding(ep_id,external_master)
    if mode=='video':W.compile_direction(ep_id)
    root=paths.state_dir()/'studio-jobs'/uuid.uuid4().hex
    request=dict(ep_id=ep_id,max_requests=max_requests,max_characters=max_characters,max_images=max_images,
                 max_image_retries=max_image_retries,
                 budget_id='studio-'+root.name,expected_identity=W.identity(ep_id),job_mode=mode)
    if provider_attempts_per_image is not None:
        request['provider_attempts_per_image']=provider_attempts_per_image
    if mode=='video':
        from app.services.production_inputs import input_files
        request['expected_image_inputs']=input_files(ep_id)
    if mode=='voice':request['expected_narration_identity']=W.narration_identity(ep_id)
    if external_master:
        from app.services.studio_workspace import library_path
        p=library_path(external_master)
        if not p.is_file():raise ValueError('所选旁白文件不存在')
        request['external_master']=str(p)
        request['external_master_sha256']=file_sha256(p)
        request['external_input']=selection
    if voice_contract.exists():request['voice_contract_sha256']=file_sha256(voice_contract)
    if narration:request['narration_selection_sha256']=narration['selection_sha256']
    root.mkdir(parents=True)
    atomic_json(root/'request.json',request)
    atomic_json(root/'submission.json',dict(request=request,request_sha256=file_sha256(root/'request.json')))
    _spawn(root,request)
    return root


def _spawn(root,request):
    import psutil
    import time
    queued = dict(status='QUEUED', updated_at=time.time(), approved=False, production_ready=False)
    atomic_json(root/'checkpoint.json', queued)
    with (root/'job.log').open('ab') as log:
        flags=subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS if os.name=='nt' else 0
        process=subprocess.Popen([sys.executable,str(paths.ROOT/'bin/studio_job.py'),str(root/'request.json')],
                                 cwd=paths.ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                 creationflags=flags,start_new_session=os.name!='nt',
                                 env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1'))
    try:created=psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:created=None
    atomic_json(root/'process.json',dict(pid=process.pid,created_at=created,request=request,
                                       request_sha256=file_sha256(root/'request.json')))


def alive(root):
    import psutil
    path=Path(root)/'process.json'
    if not path.exists():return False
    r=json.loads(path.read_text('utf-8'))
    try:return psutil.Process(r['pid']).create_time()==r.get('created_at')
    except (psutil.Error,ValueError):return False


def resume(root):
    """Explicit resume, same request and budget. Never retries uncertain TTS."""
    from app.services.process_lock import file_lock
    root=Path(root).resolve()
    if root.parent!=(paths.state_dir()/'studio-jobs').resolve():raise ValueError('not a studio job directory')
    with file_lock(root/'resume.lock'):
        request,old=check_resume(root)
        result=root/'result.json'
        if result.exists():
            previous=json.loads(result.read_text('utf-8'))
            if previous.get('status') not in ('BLOCKED','INTERRUPTED','QUEUED','RUNNING'):raise ValueError('completed job does not need resume')
            atomic_json(root/'history'/(fingerprint(previous)+'.json'),previous)
        checkpoint=root/'checkpoint.json'
        if checkpoint.exists():
            previous_checkpoint=json.loads(checkpoint.read_text('utf-8'))
            atomic_json(root/'history'/(fingerprint(previous_checkpoint)+'.json'),previous_checkpoint)
        atomic_json(root/'history'/(fingerprint(old)+'.json'),old)
        if (root/'process.json').exists():
            process_record=json.loads((root/'process.json').read_text('utf-8'))
            atomic_json(root/'history'/(fingerprint(process_record)+'.json'),process_record)
        from app.services.production_storage import episode_execution
        with episode_execution(request['ep_id']), file_lock(paths.state_dir()/'studio-jobs'/('episode-'+request['ep_id']+'.lock')):
            require_no_active(request['ep_id'],exclude=root)
            # The previous terminal result is archived. Until completion only the checkpoint is live.
            result.unlink(missing_ok=True)
            _spawn(root,request)
    return root


def validate_request(request):
    from app.services.studio_sources import require_usable
    require_usable(request['ep_id'])
    if 'expected_image_inputs' in request:
        from app.services.production_inputs import input_files
        if request['expected_image_inputs'] != input_files(request['ep_id']):
            raise ValueError('画面描述或用图安排已改变，请重新提交制作')
    expected=request.get('expected_narration_identity') if request.get('job_mode')=='voice' else None
    actual=W.narration_identity(request['ep_id']) if expected else W.identity(request['ep_id'])
    if actual!=(expected or request['expected_identity']):raise ValueError('方案已改变，请重新提交制作')
    if request.get('external_input'):
        from app.services.recording_input import validate
        validate(request['external_input'])
    if request.get('external_master_sha256') and file_sha256(request['external_master'])!=request['external_master_sha256']:
        raise ValueError('selected narration changed after submission')
    from app.services.studio_voice_contract import path_for,load
    p=path_for(request['ep_id'])
    if request.get('voice_contract_sha256')!=(file_sha256(p) if p.exists() else None):
        raise ValueError('confirmed voice contract changed')
    if p.exists():
        policy=W.folder(request['ep_id'])/'playback-policy.json'
        rate=json.loads(policy.read_text('utf-8'))['target_rate'] if policy.exists() else 1.
        load(p,source_sha256=W.document(request['ep_id'])['source_sha256'],voice=W.load(request['ep_id'])['voice'],rate=rate)
    if request.get('narration_selection_sha256'):
        from app.services.studio_narration_handoff import selected
        value=selected(request['ep_id'])
        if not value or value['selection_sha256']!=request['narration_selection_sha256']:
            raise ValueError('选用旁白已改变，请重新提交制作')


def require_no_active(ep_id,*,exclude=None):
    from app.services.production_storage import episode_available, ProductionBusy
    if not episode_available(ep_id):
        raise ProductionBusy(f'episode {ep_id} already running in another entry point')
    for p in (paths.state_dir()/'studio-jobs').glob('*/request.json'):
        if exclude and p.parent.resolve()==Path(exclude).resolve():continue
        if json.loads(p.read_text('utf-8')).get('ep_id')==ep_id and alive(p.parent):
            raise ValueError('本集已有运行中的任务，请等待或查看原任务')


def check_resume(root):
    """Read-only recovery eligibility. Same source, recipe, selected take and budget."""
    root=Path(root).resolve()
    if root.parent!=(paths.state_dir()/'studio-jobs').resolve():raise ValueError('not a studio job directory')
    if alive(root):raise ValueError('job is still running')
    request=json.loads((root/'request.json').read_text('utf-8'))
    submission=root/'submission.json'
    old=json.loads((submission if submission.exists() else root/'process.json').read_text('utf-8'))
    if old.get('request')!=request or old.get('request_sha256')!=file_sha256(root/'request.json'):
        raise ValueError('original job request changed; cannot resume')
    validate_request(request)
    require_no_active(request['ep_id'],exclude=root)
    if (root/'result.json').exists():
        status=json.loads((root/'result.json').read_text('utf-8')).get('status')
        if status not in ('BLOCKED','INTERRUPTED','QUEUED','RUNNING'):raise ValueError('completed job does not need resume')
    return request,old


def jobs(ep_id):
    rows=[]
    for p in sorted((paths.state_dir()/'studio-jobs').glob('*/request.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        request=json.loads(p.read_text('utf-8'))
        if request['ep_id']!=ep_id:continue
        result=p.parent/'result.json'
        current=json.loads(result.read_text('utf-8')) if result.exists() else None
        live=alive(p.parent)
        checkpoint=p.parent/'checkpoint.json'
        checkpoint=json.loads(checkpoint.read_text('utf-8')) if checkpoint.exists() else {}
        terminal=current and current.get('status') not in ('QUEUED','RUNNING')
        phase={**checkpoint,**current} if terminal and not live else checkpoint or current or {}
        status=phase.get('status','QUEUED' if live else 'INTERRUPTED')
        if not live and status in ('QUEUED','RUNNING'):status='INTERRUPTED'
        row=dict(path=str(p.parent),result=current,running=live,status=status,external_master=bool(request.get('external_master')),
                 stage=phase.get('stage'),progress=phase.get('progress'),stage_timings=phase.get('stage_timings',[]),
                 heartbeat_at=phase.get('heartbeat_at'),updated_at=phase.get('updated_at'),
                 error=phase.get('error'),error_phase=phase.get('error_phase'),
                 recoverable=not live and (current is None or current.get('status') in ('BLOCKED','INTERRUPTED','QUEUED','RUNNING')))
        if status=='INTERRUPTED':row['exit_reason']='进程已退出，未留下完成记录；具体退出原因未记录'
        if phase.get('budget_path') and Path(phase['budget_path']).is_file():
            b=json.loads(Path(phase['budget_path']).read_text('utf-8'))
            row['budget']={k:b[k] for k in ('limits','used')}
        elif current and current.get('budget'):row['budget']=current['budget']
        row['budget_limits']={k:request.get(k,0) for k in ('max_requests','max_characters','max_images')}
        if row['recoverable']:
            try:check_resume(p.parent)
            except (ValueError,OSError,KeyError,RuntimeError) as exc:
                row.update(recoverable=False,recovery_issue=str(exc))
        log=p.parent/'job.log'
        if log.exists():
            with log.open('rb') as f:
                f.seek(max(0,log.stat().st_size-5000))
                lines=[]
                for line in f.read().splitlines():
                    try:lines.append(line.decode('utf-8'))
                    except UnicodeDecodeError:lines.append(line.decode('gb18030',errors='replace'))
                row['log']='\n'.join(lines)
        rows.append(row)
        if len(rows)==5:break
    return rows
