"""Isolated, fake-model regressions for series voice and independent execution."""
import copy
import importlib.util
import json
import random
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import paths
from app.services import qwen_voice as Q, studio_voice_contract as C, studio_voice as V
from app.services.provenance import atomic_json, file_sha256, fingerprint

pytestmark=pytest.mark.production


@pytest.fixture
def confirmed(tmp_path):
    instruction='同一位 Dylan 男旁白自然讲故事。'+Q.AUDITION_ONLY+'不要喊叫，不模仿角色。'
    model=tmp_path/'model';model.mkdir();weights=model/'config.json';weights.write_text('{}')
    c=dict(version=1,speaker='dylan',model='Qwen3-TTS-12Hz-1.7B-CustomVoice',mode='custom',
        instruction=instruction,engine_revision='audition-engine',source_sha256='fixture-source',
        generation=dict(temperature=.65,top_p=.9,top_k=40,subtalker_temperature=.65),seed=20261022,
        target_rate=1.2,max_chunk_characters=347,selected_variant='fixture',model_path=str(model),request_paths=[])
    docs={'confirmation_path':dict(decision='accepted_for_use',selected_variant='fixture',source_sha256=c['source_sha256'],target_rate=1.2),
        'pilot_receipt_path':dict(request={k:c[k] for k in ('speaker','model','mode','instruction','engine_revision')}),
        'generation_plan_path':{k:c[k] for k in ('generation','seed','source_sha256')},
        'engine_manifest_path':dict(runtime_python='D:/hv/.venv-qwen3-tts/Scripts/python.exe',runtime={'fixture':'1'},model_files={str(weights):file_sha256(weights)}),
        'worker_path':dict(archived_fixture=True)}
    c['evidence']={}
    for name,value in docs.items():
        p=tmp_path/(name+'.json');atomic_json(p,value);c[name]=str(p);c['evidence'][str(p)]=file_sha256(p)
    p=tmp_path/'contract.json';atomic_json(p,c)
    return p,c


def request(contract,text='玄奘走出城门。'):
    return V.request(text,**{k:contract[k] for k in ('speaker','model','mode','instruction','engine_revision')})


def test_effective_recipe_only_removes_known_audition_sentence(confirmed):
    path,c=confirmed;before=path.read_bytes()
    req=C.bind(request(c),path)
    assert Q.AUDITION_ONLY not in req['instruction']
    assert req['instruction']=='同一位 Dylan 男旁白自然讲故事。不要喊叫，不模仿角色。'
    for key in ('speaker','model','mode','generation'):assert req[key]==c[key]
    assert C.validate(req)==c and path.read_bytes()==before
    other=dict(c,speaker='other-selected-voice')
    assert Q.effective(other)==other


def test_seed_depends_on_text_and_recipe_not_order_or_episode(confirmed):
    path,c=confirmed
    one=C.bind(request(c),path);two=C.bind(request(c,'另一段原文。'),path)
    assert one['seed']!=two['seed'] and one['series_seed']==c['seed']
    assert one==C.bind(request(c),path)
    assert Q.segment_seed(dict(c,episode_id='episode-b'),one['text'])==one['seed']
    bad=copy.deepcopy(one);bad['seed']+=1
    with pytest.raises(ValueError,match='settings'):C.validate(bad)
    bad=copy.deepcopy(one);bad['generation']['temperature']=.2
    with pytest.raises(ValueError,match='settings'):C.validate(bad)
    bad=copy.deepcopy(one);bad['language']='english'
    with pytest.raises(ValueError,match='language'):C.validate(bad)


def test_worker_reseeds_for_pickups_after_other_chunks_and_restart(confirmed):
    path,c=confirmed;one=C.bind(request(c),path);two=C.bind(request(c,'第二段。'),path)
    class Torch:
        def __init__(self):self.rng=random.Random();self.cuda=SimpleNamespace(manual_seed_all=self.rng.seed)
        def manual_seed(self,seed):self.rng.seed(seed)
    torch=Torch();seen=[]
    class Model:
        def generate_custom_voice(self,**kw):
            seen.append(kw)
            return [random.random(),float(np.random.random()),torch.rng.random()]
    model=Model();first=Q.infer(model,one,np,torch)
    Q.infer(model,two,np,torch)
    assert Q.infer(model,one,np,torch)==first
    torch=Torch()
    assert Q.infer(model,one,np,torch)==first
    assert all(s['speaker']=='dylan' and s['language']=='chinese' and s['non_streaming_mode'] for s in seen)


def test_historical_receipt_still_verifies_but_cannot_dispatch(confirmed,tmp_path):
    from app.services.locked_voice_provider import LockedVoiceProvider
    path,c=confirmed;old=C.bind(request(c),path,_legacy=True)
    assert old['seed']==c['seed'] and Q.AUDITION_ONLY in old['instruction']
    assert C.validate(old)==c
    with pytest.raises(ValueError,match='历史配音请求'):
        LockedVoiceProvider(path,tmp_path/'worker').validate_request(old)
    assert not (tmp_path/'worker').exists()


def test_runtime_uses_project_interpreter_and_worker_not_archived_executable(confirmed,tmp_path,monkeypatch):
    _,c=confirmed;root=tmp_path/'independent';python=root/'.venv-qwen3-tts/Scripts/python.exe'
    python.parent.mkdir(parents=True);python.write_bytes(b'fixture never executed')
    monkeypatch.setattr(paths,'ROOT',root)
    r=Q.runtime(c)
    assert r['runtime_python']==str(python.resolve())
    assert r['worker_path']==str(root/'bin/qwen_worker.py')
    assert 'D:/hv/' not in json.dumps(r).replace('\\\\','/')
    python.unlink()
    with pytest.raises(ValueError,match='不会借用'):Q.runtime(c)


def test_archived_addresses_resolve_locally_without_touching_original(tmp_path,monkeypatch):
    from test_standalone_paths import deny_access
    root=tmp_path/'independent';old=tmp_path/'retired';root.mkdir();old.mkdir()
    atomic_json(root/'.studio-project.json',dict(legacy_path_roots=[str(old),'D:/hv'],project_path_aliases=['D:/hv-studio']))
    local=root/'evidence.json';atomic_json(local,dict(retained=True))
    monkeypatch.setattr(paths,'ROOT',root)
    calls=deny_access(monkeypatch,old)
    assert Q.read(old/'evidence.json')==dict(retained=True)
    assert Q.local_path('D:/hv-studio/evidence.json')==local
    assert Q.local_path('D:/hv/evidence.json')==local
    assert calls==[]


def test_entry_plan_is_read_only_and_has_no_budget_or_model(confirmed,tmp_path,monkeypatch,capsys):
    from bin import studio_narrate as N
    path,c=confirmed
    monkeypatch.setattr(N,'requests',lambda eid:({},[dict(text='原文。',instruction=Q.effective(c)['instruction'])]))
    monkeypatch.setattr(C,'path_for',lambda eid:path)
    monkeypatch.setattr(N,'render',lambda *a:pytest.fail('plan must not generate'))
    assert N.main(['fixture','--plan'])==0
    data=json.loads(capsys.readouterr().out)
    assert not data['model_started'] and data['speaker']=='dylan' and data['segments'][0]['seed']>=0
    with pytest.raises(SystemExit):N.main(['fixture'])


def test_no_local_launcher_calls_shared_http_service():
    root=Q.CODE_ROOT
    bat=(root/'start_qwen3_tts.bat').read_text('utf-8-sig')
    assert 'studio_narrate.py' in bat and 'start_service.bat' not in bat
    for name in ('generate_qwen3_local_audio.py','run_marcus_demo.py'):
        text=(root/'resource/风趣历史.writing-DNA/scripts'/name).read_text('utf-8-sig')
        assert 'studio_narrate' in text and 'urlopen' not in text and 'Qwen3TTSModel' not in text


def test_worker_import_never_loads_model(monkeypatch):
    import builtins
    original=builtins.__import__
    def guarded(name,*a,**kw):
        if name.split('.')[0] in {'torch','qwen_tts','soundfile','safetensors'}:pytest.fail('heavy import on worker import')
        return original(name,*a,**kw)
    monkeypatch.setattr(builtins,'__import__',guarded)
    spec=importlib.util.spec_from_file_location('qwen_worker_fixture',Q.CODE_ROOT/'bin/qwen_worker.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert callable(module.main)


def test_unified_adapter_receipt_and_cache_use_one_reserved_attempt(confirmed,tmp_path,monkeypatch):
    import contextlib
    from app.services import locked_voice_provider as L
    from app.services.provider_budget import ProviderBudget
    from bin.qwen_worker import validate_dispatch
    from test_studio_voice import sound
    path,c=confirmed;req=C.bind(request(c),path);messages=[];launches=[]
    runtime=dict(runtime_python='fixture-python',worker_path='fixture-worker',runtime={},model_files={})
    monkeypatch.setattr(Q,'runtime',lambda c:runtime)
    monkeypatch.setattr('app.services.local_asr.runtime_versions',lambda *a:{})
    monkeypatch.setattr(L.R,'lease',lambda *a,**k:contextlib.nullcontext())
    class Input:
        def write(self,message):
            job=json.loads(message);assert validate_dispatch(job,path)==req
            Path(job['output_path']).parent.mkdir(parents=True,exist_ok=True)
            Path(job['output_path']).write_bytes(sound());messages.append(job)
        def flush(self):pass
    class Process:
        def __init__(self,*a,**kw):launches.append(a);self.stdin=Input();self.stdout=[]
        def poll(self):return 0
    monkeypatch.setattr(L.subprocess,'Popen',Process)
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    with L.LockedVoiceProvider(path,tmp_path/'worker') as provider:
        monkeypatch.setattr(provider,'_read',lambda:None)
        def event(seconds):
            if seconds==600:return dict(state='ready',policy=Q.policy())
            return dict(state='done',request_fingerprint=fingerprint(req),effective_seed=req['seed'],
                        audio_sha256=file_sha256(messages[-1]['output_path']))
        monkeypatch.setattr(provider,'_event',event)
        raw,receipt=V.render_take(req,tmp_path/'takes',provider,budget=budget)
        cached,_=V.render_take(req,tmp_path/'takes',provider)
    assert cached==raw and len(launches)==len(messages)==1
    assert budget.snapshot()['used']['tts_requests']==1
    assert receipt['request']['seed']==Q.segment_seed(c,req['text'])
    with pytest.raises(ValueError,match='reserved budget'):validate_dispatch(messages[0],path)


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows launcher path contract')
def test_retired_runtime_override_is_rejected_before_access(tmp_path):
    import subprocess
    root=tmp_path/'studio';root.mkdir()
    atomic_json(root/'.studio-project.json',dict(name='历史创作工作台',legacy_path_roots=['D:/hv']))
    script=Q.CODE_ROOT/'scripts/check-runtime-path.ps1'
    result=subprocess.run(['powershell','-NoProfile','-File',str(script),'-ProjectRoot',str(root),
                           '-RuntimePath','D:/hv/.venv-core/Scripts/python.exe'],capture_output=True,text=True,errors='replace')
    assert result.returncode!=0 and 'retired checkout' in result.stderr
