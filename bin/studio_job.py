"""Worker for one user-submitted production request."""
import json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['PATH']=str(ROOT/'tools/ffmpeg/bin')+os.pathsep+os.environ['PATH']
from app.services.provenance import atomic_json

def main():
    path=Path(sys.argv[1]);request=json.loads(path.read_text('utf-8'))
    from app.services.process_lock import file_lock
    with file_lock(path.parent/'execution.lock'):
        execute(path,request)


def execute(path,request):
    import copy,threading,time,traceback
    checkpoint = dict(status='RUNNING', stage='admission', approved=False, production_ready=False)
    guard = threading.Lock()
    stop = threading.Event()

    def update(values=None):
        with guard:
            if values:
                checkpoint.update(copy.deepcopy(values))
                checkpoint['updated_at'] = time.time()
            checkpoint['heartbeat_at'] = time.time()
            atomic_json(path.parent/'checkpoint.json', checkpoint)

    def heartbeat():
        while not stop.wait(5): update()

    started=time.time()
    previous=path.parent/'checkpoint.json'
    queued=json.loads(previous.read_text('utf-8')) if previous.is_file() else {}
    queue_seconds=max(0.,started-queued.get('updated_at',started)) if queued.get('status')=='QUEUED' else 0.
    update(dict(started_at=started,queue_seconds=round(queue_seconds,3)))
    monitor=threading.Thread(target=heartbeat,daemon=True);monitor.start()
    try:
        from app.services.studio_jobs import validate_request
        validate_request(request)
        request=dict(request)
        for key in ('expected_image_inputs','voice_contract_sha256','narration_selection_sha256',
                    'expected_identity','expected_narration_identity','external_master_sha256','external_input'):
            request.pop(key,None)
        mode=request.pop('job_mode','video')
        if mode=='voice' and not request.get('external_master'):
            update(dict(stage='tts',progress=dict(message='制作旁白候选')))
            from studio_narrate import render
            audio=render(request['ep_id'],str(path.parent/'budget.json'),request['max_requests'],request['max_characters'])
            result=dict(status='VOICE_CANDIDATE',audio_path=str(audio),source_map_path=str(audio.parent/'source-map.json'),approved=False,production_ready=False)
        else:
            from app.services.episode_production import run
            stages=['alignment'] if mode=='alignment' else ['tts','quality','alignment'] if mode=='voice' else None
            result=run(**request,stages=stages,progress=update)
            if mode=='voice' and result.get('run_ok'):
                from app.services.narration_pipeline import read
                from app import paths
                state=read(paths.episode_dir(request['ep_id'])/'production/v2/run.json')
                manifest=read(state['manifest_path'])
                result.update(status='VOICE_CANDIDATE',audio_path=manifest['master_path'],
                              voice_reused_without_generation=True)
    except (Exception, KeyboardInterrupt) as exc:
        result=dict(status='INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'BLOCKED',error=str(exc),error_type=type(exc).__name__,
                    error_phase=checkpoint.get('stage'),error_stack=traceback.format_tb(exc.__traceback__),
                    approved=False,production_ready=False)
    finally:
        stop.set();monitor.join(timeout=6)
    result['completed_at']=time.time()
    result['queue_seconds']=round(queue_seconds,3)
    atomic_json(path.parent/'result.json',result)
    update({k:result[k] for k in ('status','error','error_phase','completed_at','final_mp4','audio_path') if k in result})
    # Keep detailed records on disk; the live log remains readable.
    print(json.dumps({k:result[k] for k in ('status','error','error_phase','final_mp4','audio_path') if k in result},ensure_ascii=False),flush=True)
if __name__=='__main__':main()
