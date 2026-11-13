"""Regression cases for the Xuanzang metadata-only repair (no model/encoder calls)."""
import copy
from pathlib import Path
import wave

import pytest
from app.services.provenance import atomic_json,file_sha256,fingerprint
from app.services import production_revision as R
from app.services.narration_continuity import corrected_accounting,audio_binding
from app.services import narration_pipeline as N

pytestmark=pytest.mark.production


def wav(path,pcm):
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(pcm)
    return str(path)


@pytest.fixture
def revision(tmp_path):
    sr=24000
    original=wav(tmp_path/'raw.wav',b'\x01\0'*sr*2)
    patch=wav(tmp_path/'patch.wav',b'\x02\0'*(sr//2))
    output=wav(tmp_path/'master.wav',b'\x01\0'*sr+b'\0\0'*8400+b'\x02\0'*(sr//2))
    total=44400
    old_trim=dict(source_raw_path=original,source_raw_sha256=file_sha256(original),
        output_path=output,output_sha256=file_sha256(output),sample_rate=sr,samples_before=sr*2,samples_after=total,
        chunks=[dict(raw_path=original,raw_sha256=file_sha256(original),samples_before=sr*2,samples_after=42000,
                     repair=dict(repair_raw_path=patch,repair_raw_sha256=file_sha256(patch),
                         repair_speech_start_sample=0,repair_speech_end_sample=12000,original_speech_end_sample=sr))])
    text='保留用户原文。';unit=dict(unit_id='u1',text=text,sample_start=0,sample_end=total,source_map=list(range(len(text))))
    plan=tmp_path/'plan.json';atomic_json(plan,dict(source_sha256='source',units=[dict(unit_id='u1',display_text=text)]))
    m=dict(provenance='continuous_local_tts_candidate',plan_path=str(plan),plan_sha256=file_sha256(plan),
        master_path=output,master_sha256=file_sha256(output),sample_rate=sr,samples=total,
        alignment=[unit],takes=[dict(unit_id='u1',raw_path=output,raw_sha256=file_sha256(output),
          audio_path=output,audio_sha256=file_sha256(output),samples=total,quality={'status':'pending'})],
        continuous_tts=dict(raw_path=output,raw_sha256=file_sha256(original),voice='fixture-voice',source_sha256='source'),
        technical_quality='pending',listening_review='pending',approved=False)
    mp=tmp_path/'manifest.json';atomic_json(mp,m)
    cue=dict(unit_id='u1',text=text,text_start=0,text_end=len(text),sample_start=0,sample_end=total)
    ap=tmp_path/'alignment.json';atomic_json(ap,dict(manifest_path=str(mp),manifest_content_sha256=fingerprint(m),
        inputs={str(mp):file_sha256(mp)},subtitles=[cue],words={},status='machine_validated',listening_approved=False))
    op=tmp_path/'overlays.json';atomic_json(op,{'overlays':[]})
    plate=tmp_path/'image.png';plate.write_bytes(b'fixture plate; never rendered')
    direction=tmp_path/'directing.json';atomic_json(direction,{'fixture':True})
    timeline=dict(shots=[dict(id='s1',image_id='image',image_path=str(plate),sample_start=0,sample_end=total)],
        subtitles=[cue],alignment_evidence_path=str(ap),overlay_path=str(op),inputs={str(op):file_sha256(op)})
    tp=tmp_path/'timeline.json';atomic_json(tp,timeline)
    video=tmp_path/'candidate.mp4';video.write_bytes(b'fixture media; never encoded')
    receipt=dict(candidate_path=str(video),candidate_sha256=file_sha256(video),manifest_path=str(mp),
        samples=total,sample_rate=sr,expected_frames=46,approved=False,
        inputs={str(p):file_sha256(p) for p in [mp,ap,tp,op,plate,direction,plan,Path(output)]})
    rp=tmp_path/'candidate.json';atomic_json(rp,receipt)
    archive=R.archive_candidate(rp,tmp_path/'archive')
    fixed=corrected_accounting(old_trim,[]);fp=R.write_once(tmp_path/'fixed-accounting.json',fixed)
    corrected=copy.deepcopy(m)
    corrected['continuous_tts'].update(lineage_version=1,raw_sha256=file_sha256(output),
        original_raw=audio_binding(original),repaired_raw=audio_binding(output),
        repair_receipt_path=str(fp),repair_receipt_sha256=file_sha256(fp))
    return dict(root=tmp_path,m=m,corrected=corrected,mp=mp,old_trim=old_trim,fixed=fixed,fp=fp,
                rp=rp,archive=archive,tp=tp,timeline=timeline,direction=direction)


def test_wrong_continuous_hash_is_blocked_and_repair_is_pcm_exact(revision):
    f=revision
    with pytest.raises(ValueError,match='continuous raw'):N.validate_assembly(f['mp'])
    fixed=f['fixed'];assert fixed['chunks'][0]['samples_after']==44400
    assert fixed['chunks'][0]['samples_after_silence_trim']==42000
    assert fixed['samples_before']-fixed['removed_source_samples']+fixed['inserted_samples']==fixed['samples_after']
    assert corrected_accounting(fixed,[])==fixed
    rp=R.amend_metadata(f['archive'],f['corrected'],f['root']/'revision')
    assert N.validate_assembly(N.read(rp)['manifest_path'])['samples']==44400


@pytest.mark.parametrize('field,value',[('samples_after',42000),('inserted_samples',0)])
def test_incorrect_accounting_cannot_pass_by_matching_only_top_level(revision,field,value):
    f=revision;wrong=copy.deepcopy(f['fixed']);wrong['chunks'][0][field]=value;atomic_json(f['fp'],wrong)
    m=copy.deepcopy(f['corrected']);m['continuous_tts']['repair_receipt_sha256']=file_sha256(f['fp'])
    p=f['root']/'wrong.json';atomic_json(p,m)
    with pytest.raises(ValueError,match='sample accounting'):N.validate_assembly(p)


def test_archive_recovers_only_exact_old_bytes_and_survives_later_edits(revision):
    f=revision;original=f['tp'].read_bytes();f['tp'].write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='cannot recover'):R.archive_candidate(f['rp'],f['root']/'bad-archive')
    recovered=R.archive_candidate(f['rp'],f['root']/'recovered',recovered={str(f['tp']):original})
    assert R.verify_archive(recovered)['recovered_inputs']==[str(f['tp'])]
    assert R.verify_archive(f['archive'])['inputs'][str(f['tp'])]['sha256']==file_sha256(R.verify_archive(f['archive'])['inputs'][str(f['tp'])]['path'])


def test_visual_edit_is_forked_and_interruption_preserves_previous_inputs(revision):
    f=revision;before=file_sha256(f['tp']);op=Path(f['timeline']['overlay_path']);old=file_sha256(op)
    result=R.fork_visual_revision(f['tp'],{'overlays':[{'title':'新标题'}]},f['root']/'visual')
    # Stop before any pointer switch: the previous input files still match.
    assert result!=f['tp'] and file_sha256(f['tp'])==before and file_sha256(op)==old
    assert R.fork_visual_revision(f['tp'],{'overlays':[{'title':'新标题'}]},f['root']/'visual')==result
    with pytest.raises(ValueError,match='immutable'):R.write_once(result,{'changed':True})


def test_metadata_revision_preserves_every_subtitle_and_candidate_byte(revision):
    f=revision;rp=R.amend_metadata(f['archive'],f['corrected'],f['root']/'revision');r=R.verify_amendment(rp)
    assert r['render_performed'] is False and not r['approved']
    assert r['candidate_sha256']==N.read(f['rp'])['candidate_sha256']
    assert N.read(r['timeline_path'])['subtitles']==f['timeline']['subtitles']
    assert N.read(f['mp'])==f['m']
    assert R.amend_metadata(f['archive'],f['corrected'],f['root']/'revision')==rp


@pytest.mark.parametrize('change',['voice','narration','subtitle'])
def test_metadata_correction_cannot_hide_content_changes(revision,change):
    f=revision;m=copy.deepcopy(f['corrected'])
    if change=='voice':
        m['continuous_tts']['voice']='another speaker'
        with pytest.raises(ValueError,match='voice configuration'):R.amend_metadata(f['archive'],m,f['root']/'bad')
    elif change=='narration':
        m['alignment'][0]['text']='改写了'
        with pytest.raises(ValueError,match='narration or clock'):R.amend_metadata(f['archive'],m,f['root']/'bad')
    else:
        rp=R.amend_metadata(f['archive'],m,f['root']/'revision');r=N.read(rp);t=N.read(r['timeline_path'])
        t['subtitles'][0]['sample_end']-=100;atomic_json(r['timeline_path'],t)
        r['inputs'][r['timeline_path']]=file_sha256(r['timeline_path']);atomic_json(rp,r)
        with pytest.raises(ValueError,match='visuals or subtitle'):R.verify_amendment(rp)


def test_full_registration_is_atomic_idempotent_and_does_not_approve_pilot(revision,tmp_episodes,monkeypatch):
    from app.services import studio_workflow as W,studio_sources as S
    from app import paths
    f=revision;eid='metadata-registration-fixture';ep=paths.episode_dir(eid);ep.mkdir()
    # Registration requires the current directing file, not a historical pathname.
    direction=ep/'directing.json';direction.write_bytes(f['direction'].read_bytes())
    receipt=N.read(f['rp']);receipt['inputs'].pop(str(f['direction']));receipt['inputs'][str(direction)]=file_sha256(direction)
    atomic_json(f['rp'],receipt);archive=R.archive_candidate(f['rp'],f['root']/'register-archive')
    rp=R.amend_metadata(archive,f['corrected'],f['root']/'register-revision')
    monkeypatch.setattr(S,'require_usable',lambda _:None)
    monkeypatch.setattr(W,'require_direction_current',lambda _:None)
    monkeypatch.setattr(W,'identity',lambda _:'design')
    monkeypatch.setattr(R,'probe_existing_media',lambda _:{'status':'passed','fixture_only':True})
    monkeypatch.setattr(W,'document',lambda _:{'source_sha256':'source','units':[{'unit_id':'u1','source_text':'保留用户原文。'}]})
    original={'active_stage':'images','running':False};state_path=ep/'production/v2/run.json';atomic_json(state_path,original)
    with pytest.raises(ValueError,match='episode changed'):
        R.register_existing_full(eid,rp,expected_identity='stale-design')
    assert N.read(state_path)==original
    state=R.register_existing_full(eid,rp,expected_identity='design')
    assert state['active_stage']=='review' and state['last_result']['status']=='NEEDS_REVIEW'
    assert not state['last_result']['approved'] and not state['last_result']['production_ready']
    assert R.register_existing_full(eid,rp,expected_identity='design')==state
    assert len(list((ep/'production/v2/run-history').glob('*.json')))==1
    assert not (ep/'studio/review-pilot.json').exists()


def test_media_probe_rejects_wrong_frame_count_without_encoding(monkeypatch):
    import json
    from types import SimpleNamespace
    info={'streams':[dict(codec_type='video',width=1080,height=1920,nb_read_packets='24',avg_frame_rate='25/1',duration='1.0'),
                     dict(codec_type='audio',sample_rate='24000',duration='1.0')]}
    calls=[]
    def probe(args,**kwargs):
        calls.append(args);return SimpleNamespace(stdout=json.dumps(info))
    monkeypatch.setattr(R.subprocess,'run',probe)
    with pytest.raises(ValueError,match='frame geometry/count'):
        R.probe_existing_media(dict(candidate_path='fixture.mp4',expected_frames=25,samples=24000,sample_rate=24000))
    assert len(calls)==1 and calls[0][0]=='ffprobe'
