import copy
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from app.services.provenance import atomic_json,file_sha256,fingerprint
from app.services import audio_content_qa as Q, studio_voice as V, recognition_cache as C
from app.services.provider_budget import ProviderBudget

pytestmark=pytest.mark.production


def sound(path,seconds=1,silence=0):
    sr=8000;x=(2000*np.sin(np.arange(round(sr*seconds))*2*np.pi*330/sr)).astype('<i2')
    data=x.tobytes()+b'\0\0'*round(sr*silence)
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(data)
    return path


def recognition(path,audio,text,words=None):
    r=dict(audio_sha256=file_sha256(audio),text=text,runtime={},segments=[dict(words=words or [])])
    atomic_json(path,dict(request=dict(adapter='fixture',audio_sha256=file_sha256(audio),threads=4,python='old',options={'beam':5}),
                          result=r,result_sha256=fingerprint(r)))
    return r,path


@pytest.mark.parametrize('heard,kind',[
    ('玄奘没有回头。','possible_omission'),
    ('玄奘穿过沙漠。','possible_omission'),
    ('玄奘穿过沙漠，穿过沙漠，没有回头。','possible_repetition'),
])
def test_missing_and_repeated_spans(heard,kind):
    assert any(f['kind']==kind for f in Q.transcript('玄奘穿过沙漠，没有回头。',heard)['findings'])


def test_homophones_and_script_repetition_are_not_omissions():
    assert not Q.transcript('通关文牒，通关文牒。','通关文碟，通关文碟。')['findings']


def test_silence_detected_before_recognition_and_never_trimmed(tmp_path):
    audio=sound(tmp_path/'long.wav',silence=95);before=file_sha256(audio)
    with pytest.raises(ValueError,match='long silence'):
        Q.require_content(audio,'继续向西走。',tmp_path/'qa',recognizer=lambda p:pytest.fail('model loaded'))
    assert file_sha256(audio)==before
    assert Q.acoustic(audio)['findings'][0]['seconds']>=94.9


def test_wav_and_forced_text_alone_cannot_pass(tmp_path):
    audio=sound(tmp_path/'a.wav')
    r=Q.evaluate(audio,'兵贵神速。')
    assert r['status']=='needs_review' and not r['listening_approved']
    assert r['findings']==[dict(kind='transcript_check_pending')]


def test_existing_transcript_pass_does_not_hide_long_silence(tmp_path):
    from app.services.production_quality import _check_take
    audio=sound(tmp_path/'a.wav',silence=5)
    text='兵贵神速。';sha=file_sha256(audio)
    take=dict(audio_path=str(audio),audio_sha256=sha,quality=dict(status='passed',expected_sha256=fingerprint(text),
                audio_sha256=sha,transcript=text))
    _,_,quality=_check_take((dict(text=text),take,None,fingerprint(text)))
    assert quality['content_checks']['status']=='needs_review'
    assert quality['content_checks']['findings'][0]['kind']=='long_silence'


def test_asr_receipt_must_match_audio(tmp_path):
    a=sound(tmp_path/'a.wav');b=sound(tmp_path/'b.wav',seconds=2)
    _,p=recognition(tmp_path/'r.json',b,'兵贵神速。')
    with pytest.raises(ValueError,match='different audio'):Q.evaluate(a,'兵贵神速。',recognition_path=p)


def test_completed_take_recovers_content_check_without_provider_retry(tmp_path):
    audio=sound(tmp_path/'a.wav');calls=[];bad=[True]
    def provider(*a,**kw):calls.append(1);return audio.read_bytes()
    def recognize(p):return recognition(tmp_path/'asr.json',p,'玄奘' if bad[0] else '玄奘穿过沙漠，没有回头。')
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    opts=dict(speaker='same',mode='custom',model='fixture',engine_revision='1',provider=provider,budget=budget,recognizer=recognize)
    with pytest.raises(ValueError,match='missing/repeated'):
        V.render_act([('玄奘穿过沙漠，没有回头。','自然')],tmp_path/'master.wav',**opts)
    assert not (tmp_path/'master.wav').exists()
    bad[0]=False
    V.render_act([('玄奘穿过沙漠，没有回头。','自然')],tmp_path/'master.wav',**opts)
    assert len(calls)==1 and budget.snapshot()['used']['tts_requests']==1


def contract(tmp_path):
    p=tmp_path/'contract.json'
    c=dict(version=1,speaker='dylan',model='same-model',mode='custom',instruction='保持已确认说书人',engine_revision='same-engine',
           source_sha256='source',generation={'temperature':.65},seed=42,evidence={},target_rate=1.2)
    c['selected_variant']='fixture'
    rows={'confirmation_path':dict(decision='accepted_for_use',selected_variant='fixture',source_sha256='source',target_rate=1.2),
          'pilot_receipt_path':dict(request={k:c[k] for k in ('speaker','model','mode','instruction','engine_revision')}),
          'generation_plan_path':{k:c[k] for k in ('generation','seed','source_sha256')},
          'engine_manifest_path':{},'worker_path':{}}
    for key,value in rows.items():
        f=tmp_path/(key+'.json');atomic_json(f,value);c[key]=str(f);c['evidence'][str(f)]=file_sha256(f)
    c['request_paths']=[]
    atomic_json(p,c)
    req=V.request('玄奘穿过沙漠。',**{k:c[k] for k in ('speaker','model','mode','instruction','engine_revision')})
    from app.services.studio_voice_contract import bind
    return p,bind(req,p)


@pytest.mark.parametrize('field,value',[
    ('instruction','添加角色戏剧化指令'),('speaker','other'),('model','other'),
    ('generation',{'temperature':1.}),('seed',43)])
def test_voice_contract_changes_refused_before_budget(tmp_path,field,value):
    _,req=contract(tmp_path);req[field]=value
    b=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    with pytest.raises(ValueError):V.render_take(req,tmp_path/'takes',lambda *a,**k:pytest.fail('called'),budget=b)
    assert b.snapshot()['used']['tts_requests']==0


def test_voice_backend_must_enforce_contract_and_validate_before_charge(tmp_path):
    _,req=contract(tmp_path);b=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    with pytest.raises(ValueError,match='cannot enforce'):V.render_take(req,tmp_path/'takes',lambda *a,**k:None,budget=b)
    class Broken:
        def validate_request(self,r):raise ValueError('worker output metadata missing')
        def __call__(self,*a,**k):pytest.fail('dispatch')
    with pytest.raises(ValueError,match='metadata missing'):V.render_take(req,tmp_path/'takes',Broken(),budget=b)
    assert b.snapshot()['used']['tts_requests']==0


def test_cache_excludes_threads_but_not_model_options(tmp_path,monkeypatch):
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path)
    audio=sound(tmp_path/'a.wav');r,p=recognition(tmp_path/'r.json',audio,'兵贵神速。')
    req=json.loads(p.read_text('utf-8'))['request'];C.promote(p)
    tuned=dict(req,threads=1,python='moved-runtime')
    assert C.lookup(tuned,tmp_path/'missing.json')[0]==r
    tuned['options']={'beam':1}
    assert C.lookup(tuned,tmp_path/'missing.json') is None


def test_corrupted_recognition_cache_never_reused(tmp_path,monkeypatch):
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path)
    a=sound(tmp_path/'a.wav');_,p=recognition(tmp_path/'r.json',a,'兵贵神速。')
    r=json.loads(p.read_text('utf-8'));C.promote(p);r['result']['text']='被换了';atomic_json(p,r)
    with pytest.raises(ValueError,match='changed'):C.lookup(r['request'],tmp_path/'miss.json')


def test_whole_asr_reuse_requires_exact_pcm_and_whole_words(tmp_path):
    master=sound(tmp_path/'m.wav',seconds=2)
    clip=tmp_path/'c.wav'
    with wave.open(str(master),'rb') as m,wave.open(str(clip),'wb') as c:
        c.setparams(m.getparams());m.setpos(8000);c.writeframes(m.readframes(8000))
    _,p=recognition(tmp_path/'asr.json',master,'玄奘向西走',[
        dict(word='玄奘',start=.1,end=.8),dict(word='向西走',start=1.2,end=1.9)])
    r,path=C.slice_cached(master,clip,8000,16000,p,tmp_path/'derived')
    assert r['text']=='向西走' and abs(r['segments'][0]['words'][0]['start']-.2)<1e-6
    data=bytearray(clip.read_bytes());data[-4]^=1;clip.write_bytes(data)
    with pytest.raises(ValueError,match='exact master PCM'):C.slice_cached(master,clip,8000,16000,p,tmp_path/'derived')
    with pytest.raises(ValueError,match='input changed'):C.validated(path)


def test_resource_limits_are_bounded_and_busy_does_not_start_worker(tmp_path,monkeypatch):
    from app.services import model_resources as R
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path)
    assert R.threads(100)<=4
    audio=sound(tmp_path/'long.wav',seconds=60)
    assert R.timeout_for(audio,180)==360
    with R.lease('first',min_available_mb=0):
        with pytest.raises(RuntimeError,match='busy'):
            with R.lease('second',wait_seconds=0,min_available_mb=0):pytest.fail('second worker')
    with R.lease('recovered',wait_seconds=0,min_available_mb=0):pass


def test_low_memory_refuses_before_launch(tmp_path,monkeypatch):
    from app.services import model_resources as R
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path)
    monkeypatch.setattr('psutil.virtual_memory',lambda:SimpleNamespace(available=1))
    with pytest.raises(RuntimeError,match='RAM'):
        with R.lease('ASR'):pytest.fail('worker launched')


def test_worker_timeout_cleans_up_its_own_process(tmp_path):
    import sys,subprocess,psutil
    from app.services import model_resources as R
    pid_file=tmp_path/'worker.pid'
    code="import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        R.run([sys.executable,'-c',code,str(pid_file)],timeout=1,text=True)
    assert pid_file.exists() and not psutil.pid_exists(int(pid_file.read_text()))


def test_contract_cannot_relabel_new_instruction_as_confirmed(tmp_path):
    from app.services.studio_voice_contract import load
    path,_=contract(tmp_path);value=json.loads(path.read_text('utf-8'))
    value['instruction']='擅自加上另一套表演指令';atomic_json(path,value)
    with pytest.raises(ValueError,match='confirmed pilot'):load(path)


def test_resume_keeps_budget_and_archives_previous_failure(tmp_path,monkeypatch):
    from app.services import studio_jobs as J
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path)
    root=tmp_path/'studio-jobs/job';root.mkdir(parents=True)
    req=dict(ep_id='fixture',expected_identity='same',max_requests=2,max_characters=100)
    atomic_json(root/'request.json',req)
    atomic_json(root/'process.json',dict(request=req,request_sha256=file_sha256(root/'request.json')))
    atomic_json(root/'result.json',dict(status='BLOCKED',error='ASR timed out'))
    budget=ProviderBudget(root/'budget.json',tts_requests=2,characters=100);budget.reserve('tts','old',10)
    sha=file_sha256(budget.path);spawned=[]
    monkeypatch.setattr(J,'alive',lambda root:False)
    monkeypatch.setattr(J.W,'identity',lambda ep:'same')
    monkeypatch.setattr(J,'_spawn',lambda r,q:spawned.append(q))
    J.resume(root)
    assert spawned==[req] and file_sha256(budget.path)==sha
    assert len(list((root/'history').glob('*.json')))==2
    req['max_requests']=3;atomic_json(root/'request.json',req)
    with pytest.raises(ValueError,match='request changed'):J.resume(root)
    assert len(spawned)==1
