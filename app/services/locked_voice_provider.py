"""The only production adapter for the selected local Qwen voice recipe."""
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from app.services.provenance import atomic_json,file_sha256,fingerprint
from app.services import model_resources as R
from app.services.studio_voice_contract import validate
from app.services import qwen_voice as Q


class LockedVoiceProvider:
    def __init__(self,contract_path,root):
        self.contract_path=Q.local_path(contract_path);self.root=Path(root).resolve()
        self.process=None;self.log=None;self.preflight_done=False;self.events=queue.Queue()

    def validate_request(self,req):
        c=validate(req)
        if c is None or Q.local_path(req['voice_contract']['path'])!=self.contract_path:
            raise ValueError('provider requires the confirmed voice contract')
        if req.get('qwen_policy')!=Q.policy():
            raise ValueError('历史配音请求只可复用已有音频；新配音须使用当前统一入口')
        if len(req['text'])>c['max_chunk_characters']:raise ValueError('chunk exceeds confirmed input size')
        engine=Q.runtime(c)
        signature=fingerprint([file_sha256(self.contract_path),engine])
        if self.preflight_done and signature!=self.preflight_signature:
            raise ValueError('声音方案或运行环境在任务中变化，保留已有片段')
        if not self.preflight_done:
            for p,sha in engine['model_files'].items():
                if file_sha256(p)!=sha:raise ValueError('confirmed voice model changed')
            from app.services.local_asr import runtime_versions
            # Check exact installed versions without importing/loading the model.
            lock=self.root/'runtime.lock';lock.parent.mkdir(parents=True,exist_ok=True)
            value='\n'.join(k+'=='+v for k,v in engine['runtime'].items())+'\n'
            if lock.exists() and lock.read_text('utf-8')!=value:raise ValueError('voice runtime revision changed')
            if not lock.exists():lock.write_text(value,encoding='utf-8')
            runtime_versions(Path(engine['runtime_python']),lock)
            self.preflight_signature=signature
            self.preflight_done=True
        # All adapter paths exist before the durable attempt is reserved.
        self.current=req;self.contract=c;self.engine=engine
        self.request_path=self.root/'requests'/(fingerprint(req)+'.json')
        if self.request_path.exists() and json.loads(self.request_path.read_text('utf-8'))!=self._row(req):
            raise ValueError('frozen worker request changed')
        atomic_json(self.request_path,self._row(req))

    def _row(self,req):
        return req

    def _read(self):
        for line in self.process.stdout:
            if line.startswith('@@'):
                try:self.events.put(json.loads(line[2:]))
                except ValueError:self.events.put(dict(state='error',error='invalid worker event'))
        self.events.put(dict(state='error',error='worker exited before receipt'))

    def _event(self,seconds):
        try:return self.events.get(timeout=seconds)
        except queue.Empty:
            self.close()
            raise TimeoutError('local voice worker timed out; outcome uncertain, inspect worker output before retry')

    def __call__(self,text,*,speaker,mode,instruct,ref_audio):
        from app.services.studio_voice import require_dispatch
        require_dispatch(text,speaker,mode,instruct,ref_audio)
        if (text,speaker,mode,instruct or '',ref_audio)!=(self.current['text'],self.current['speaker'],self.current['mode'],self.current['instruction'],None):
            raise ValueError('worker arguments differ from locked request')
        from app.services.provider_budget import current_tts_budget
        budget=current_tts_budget()
        attempts=[a for a in budget.snapshot()['attempts'] if a['kind']=='tts' and a['status']=='reserved'
                  and a['request_fingerprint']==fingerprint(self.current)]
        if len(attempts)!=1:raise ValueError('Qwen dispatch requires one matching reserved attempt')
        import uuid
        output=self.root/'worker-output'/(fingerprint(self.current)+'-'+uuid.uuid4().hex+'.wav')
        message=dict(request_path=str(self.request_path),output_path=str(output),
                     budget_path=str(budget.path.resolve()),attempt_id=attempts[0]['id'])
        dispatch=self.root/'dispatch.json'
        atomic_json(dispatch,dict(request_fingerprint=fingerprint(self.current),**message))
        if self.process is None:
            self.root.mkdir(parents=True,exist_ok=True)
            self.log=(self.root/'worker.log').open('ab')
            self.process=subprocess.Popen([self.engine['runtime_python'],self.engine['worker_path'],'--contract',str(self.contract_path),
                                           '--dispatch',str(dispatch)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',
                env=dict(os.environ,PYTHONUTF8='1'),
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            threading.Thread(target=self._read,daemon=True).start()
            ready=self._event(600)
            if ready.get('state')!='ready' or ready.get('policy')!=Q.policy():
                self.close()
                raise RuntimeError('selected Qwen worker did not become ready with the current policy')
        # Each attempt has a unique output. Interrupted output is preserved.
        self.process.stdin.write(json.dumps(message)+'\n');self.process.stdin.flush()
        event=self._event(1800)
        if event.get('state')!='done' or event.get('request_fingerprint')!=fingerprint(self.current):
            raise RuntimeError('local voice worker failed: '+str(event.get('error',event.get('state'))))
        if event.get('effective_seed')!=self.current['seed'] or event.get('audio_sha256')!=file_sha256(output):
            raise ValueError('Qwen receipt seed/audio differs from dispatched request')
        return output.read_bytes()

    def dispatch_lease(self):
        return R.lease('Qwen', min_available_mb=4096)

    def close(self):
        if self.process is not None:
            R.stop_owned(self.process);self.process=None
        if self.log:self.log.close();self.log=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
