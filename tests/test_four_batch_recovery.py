"""Audio authority, configuration compatibility and resource waits."""
import copy
import wave
import pytest
from pathlib import Path
from app import paths
from app.services import recording_input as R, production_identity as I, production_storage as S
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.narration_pipeline import read
from app.services.process_lock import file_lock
from test_production_entrypoint import production_episode
from test_task_identity import contract

pytestmark=pytest.mark.production


def wav(p):
    p.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(p),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(24000);f.writeframes(b'\0\0'*240)
    return p


def test_operational_token_rotation_preserves_task_but_model_setting_does_not(tmp_path,monkeypatch):
    monkeypatch.setattr(I,'load_env',lambda:{'HV_API_TOKEN':'old','IMAGE_MODEL':'a'})
    t=contract(tmp_path)
    monkeypatch.setattr(I,'load_env',lambda:{'HV_API_TOKEN':'new','IMAGE_MODEL':'a'})
    I.require_same_task(t,contract(tmp_path))
    with I.task_scope(t,'b'*32): assert I.current_execution()['task_id']==t['task_id']
    monkeypatch.setattr(I,'load_env',lambda:{'HV_API_TOKEN':'new','IMAGE_MODEL':'b'})
    with pytest.raises(I.TaskMismatch): I.require_same_task(t,contract(tmp_path))


def test_machine_contention_is_typed_before_any_source_or_work(monkeypatch):
    from app.services.batch_worker import classify
    monkeypatch.setattr('app.managers.load',lambda *a:pytest.fail('source touched'))
    @S.production_storage
    def work(ep_id):pytest.fail('work ran')
    with file_lock(paths.state_dir()/'production-storage.lock'):
        with pytest.raises(S.ResourceBusy) as exc:work('resource-fixture')
    assert classify(type(exc.value).__name__+': '+str(exc.value))=='WAITING_RESOURCE'


def test_resource_wait_keeps_budget_and_does_not_consume_failure_allowance(tmp_path):
    from app.services.batch_worker import accept_child_result
    from app.services.provider_budget import ProviderBudget
    task=contract(tmp_path);execution=I.execution_ref(task,'a'*32)
    budget=ProviderBudget(task['budget']['path'],**task['budget']['limits'])
    budget.reserve('tts','already-done',2);before=read(budget.path)
    row=dict(task=task,token='a'*32,execution=execution,attempts=1,status='RUNNING')
    result=tmp_path/'result.json'
    atomic_json(result,dict(execution=execution,status='WAITING_RESOURCE',
                           error_type='ResourceBusy',error='machine busy',lease_acquired=True))
    accept_child_result(row,result)
    assert row['attempts']==0 and row['status']=='WAITING_RESOURCE' and row['resource_waits']==1
    assert read(budget.path)==before


def test_explicit_numbered_recordings_are_resolved_without_materializing_or_tts(production_episode,monkeypatch,tmp_path):
    ep,_,_=production_episode
    wav(tmp_path/'parts/1.wav');wav(tmp_path/'parts/2.wav')
    monkeypatch.setattr('app.services.voice_assets.resolve_voice_asset',lambda *a,**k:pytest.fail('TTS lookup'))
    audio=R.episode_source(ep.id,explicit=tmp_path/'parts',mode='parts')
    assert not audio['requires_tts'] and audio['selection']['kind']=='parts'
    assert len(audio['selection']['files'])==2
    assert not list((tmp_path/'parts').glob('master.wav'))


def test_validated_v2_external_source_does_not_need_legacy_sidecar(production_episode,monkeypatch,tmp_path):
    ep,folder,_=production_episode
    master=wav(tmp_path/'original.wav');plan=tmp_path/'plan.json'
    from app.orchestrator import _load_script_md
    from app.managers import load
    source=fingerprint(_load_script_md(load(ep.id)))
    atomic_json(plan,dict(source_sha256=source))
    manifest=tmp_path/'manifest.json'
    atomic_json(manifest,dict(provenance='external_recording',plan_path=str(plan),plan_sha256=file_sha256(plan),
                source_recording=dict(delivered_path=str(master),delivered_sha256=file_sha256(master))))
    atomic_json(folder/'production/v2/run.json',dict(source_sha256=source,manifest_path=str(manifest)))
    called=[]
    monkeypatch.setattr('app.services.narration_pipeline.validate_assembly',lambda p:called.append('assembly'))
    monkeypatch.setattr('app.services.external_narration.validate_external',lambda m:called.append('external'))
    monkeypatch.setattr('app.services.voice_assets.resolve_voice_asset',lambda *a,**k:pytest.fail('TTS lookup'))
    audio=R.episode_source(ep.id)
    assert audio['origin']=='v2_manifest' and not audio['requires_tts']
    assert called==[]  # original source resolution does not certify generated takes
    master.write_bytes(b'changed')
    with pytest.raises(ValueError,match='delivered recording changed'):R.episode_source(ep.id)


def test_tts_mode_remains_explicit_when_no_recording_exists(production_episode):
    audio=R.episode_source(production_episode[0].id)
    assert audio['kind']=='tts' and audio['requires_tts']
