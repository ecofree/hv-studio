"""Bounded adapter for the exact frozen offline Qwen worker already auditioned."""
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from app.services.provenance import atomic_json,file_sha256,fingerprint
from app.services import model_resources as R
from app.services.studio_voice_contract import validate


class LockedVoiceProvider:
    def __init__(self,contract_path,root):
        self.contract_path=Path(contract_path).resolve();self.root=Path(root).resolve()
        self.process=None;self.log=None;self.preflight_done=False;self.events=queue.Queue()

    def validate_request(self,req):
        c=validate(req)
        if c is None or Path(req['voice_contract']['path']).resolve()!=self.contract_path:
            raise ValueError('provider requires the confirmed voice contract')
        if len(req['text'])>c['max_chunk_characters']:raise ValueError('chunk exceeds confirmed input size')
        engine=json.loads(Path(c['engine_manifest_path']).read_text('utf-8'))
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
            self.preflight_done=True
        # All adapter paths exist before the durable attempt is reserved.
        self.current=req;self.contract=c;self.engine=engine
        self.request_path=self.root/'requests'/(fingerprint(req)+'.json')
        if self.request_path.exists() and json.loads(self.request_path.read_text('utf-8'))!=self._row(req):
            raise ValueError('frozen worker request changed')
        atomic_json(self.request_path,self._row(req))

    def _row(self,req):
        return dict(text=req['text'],speaker=req['speaker'],mode=req['mode'],instruction=req['instruction'],
                    generation=req['generation'],seed=req['seed'],request_fingerprint=fingerprint(req))

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
        with R.lease('Qwen',min_available_mb=4096):
            if self.process is None:
                self.root.mkdir(parents=True,exist_ok=True)
                self.log=(self.root/'worker.log').open('ab')
                self.process=subprocess.Popen([self.engine['runtime_python'],self.contract['worker_path'],str(self.contract['seed']),self.contract['model_path']],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                threading.Thread(target=self._read,daemon=True).start()
                if self._event(600).get('state')!='ready':raise RuntimeError('confirmed Qwen worker did not become ready')
            # Each attempt has a unique output. Interrupted output is preserved.
            import uuid
            output=self.root/'worker-output'/(fingerprint(self.current)+'-'+uuid.uuid4().hex+'.wav')
            message=dict(request_path=str(self.request_path),output_path=str(output))
            atomic_json(self.root/'dispatch.json',dict(request_fingerprint=fingerprint(self.current),**message))
            self.process.stdin.write(json.dumps(message)+'\n');self.process.stdin.flush()
            event=self._event(1800)
            if event.get('state')!='done' or event.get('request_fingerprint')!=fingerprint(self.current):
                raise RuntimeError('local voice worker failed: '+str(event.get('error',event.get('state'))))
            return output.read_bytes()

    def close(self):
        if self.process is not None:
            R.stop_owned(self.process);self.process=None
        if self.log:self.log.close();self.log=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
