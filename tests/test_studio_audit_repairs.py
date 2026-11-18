"""Regressions at Studio entry points. All episodes, jobs, and ledgers are disposable."""
from pathlib import Path
from unittest.mock import patch

import pytest

from app import paths
from app.services import studio_workflow as workflow, studio_workspace as workspace
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, file_sha256, fingerprint
from test_workflow_maintenance import recovery_fixture, studio
from test_studio_attraction import fixture_plan

pytestmark = pytest.mark.production


def test_alternate_script_only_writes_preview(studio):
    from app.services.episode_production import run
    workspace.create_episode('preview', '城门', '城门已开。', scope='full')
    state = paths.episode_dir('preview')/'production/v2/run.json'
    atomic_json(state, {'source_sha256': fingerprint('城门已开。'), 'image_assets': {'a': 'original'}})
    before = state.read_bytes()
    preview = run('preview', stages=['script'], script_md='城门关闭。')
    assert preview['status'] == 'SCRIPT_PREVIEW'
    assert read(preview['plan_path'])['units'][0]['source_text'] == '城门关闭。'
    with pytest.raises(ValueError, match='独立试排'):
        run('preview', stages=['images'], script_md='城门关闭。')
    assert state.read_bytes() == before


def test_qwen_busy_has_no_attempt_or_process(tmp_path, monkeypatch):
    from app.services import model_resources, studio_voice
    from app.services.locked_voice_provider import LockedVoiceProvider
    from app.services.production_storage import ResourceBusy
    from app.services.provider_budget import ProviderBudget
    provider = LockedVoiceProvider(tmp_path/'contract', tmp_path/'worker')
    monkeypatch.setattr(provider, 'validate_request', lambda req: None)
    budget = ProviderBudget(tmp_path/'budget.json', tts_requests=1, characters=12)
    req = studio_voice.request('城门。', speaker='test', mode='custom', instruction='', model='offline', engine_revision='fixture')
    with patch.object(model_resources, 'lease', side_effect=ResourceBusy('local-model')), patch('subprocess.Popen') as spawn:
        for _ in range(2):
            with pytest.raises(ResourceBusy):
                studio_voice.render_take(req,tmp_path/'cache',provider,budget=budget)
        assert not spawn.called
    assert budget.snapshot()['attempts'] == []
    assert not any(budget.snapshot()['used'].values())


def test_old_quality_is_recomputed_but_current_findings_still_block(studio, monkeypatch):
    from app.services import production_admission as admission, production_quality as quality
    from app.services import studio_sources
    monkeypatch.setattr(admission, 'inputs', lambda eid: dict(unsupported_cues=[], source_sha256='source'))
    monkeypatch.setattr(studio_sources, 'require_usable', lambda eid: None)
    monkeypatch.setattr(workflow, 'status', lambda eid: dict(ready_for_generation=True, blockers=[]))
    report = studio/'report.json'
    atomic_json(report, {'fixture': 1})
    atomic_json(paths.episode_dir('one')/'production/v2/run.json', dict(source_sha256='source', quality_path=str(report)))
    with patch.object(quality,'pending',side_effect=quality.StaleQualityReport('policy changed')):
        result=admission.check('one')
    assert result['eligible'] and result['quality_refresh_required'] and result['input_fingerprint']
    monkeypatch.setattr(quality,'pending',lambda p:['unit-with-finding'])
    assert not admission.check('one')['eligible']


def test_bad_job_record_only_affects_its_owner(studio):
    from app.services import studio_jobs as jobs
    job=paths.state_dir()/'studio-jobs'/'old'
    atomic_json(job/'submission.json', {'request': {'ep_id': 'other'}})
    (job/'request.json').write_text('{',encoding='utf-8')
    jobs.require_no_active('one')
    assert jobs.jobs('one') == []
    damaged, = jobs.jobs('other')
    assert damaged['status'] == 'BLOCKED' and not damaged['recoverable']


def test_restore_opens_frozen_source_after_original_checkout_moves(studio, tmp_path, monkeypatch):
    from app.services import production_backup as backup, studio_sources as sources
    from app.services.production_acceptance import source_fingerprint, FIXED_SOURCE_FILES
    from app.managers import episode
    for relative in FIXED_SOURCE_FILES:
        p=studio/relative
        if not p.exists():
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('# fixture',encoding='utf-8')
    code=studio/'tests/test_one.py'
    code.parent.mkdir();code.write_text('def test_one(): pass',encoding='utf-8')
    author=studio/'story.md'; author.write_text('### 【第一幕：城门】\n\n城门紧闭。',encoding='utf-8')
    sources.import_authoring('selected', '城门', author)
    atomic_json(paths.state_dir()/'studio-jobs/other/request.json', {'ep_id':'other','external_master':str(studio/'absent.wav')})
    before=source_fingerprint()
    snap=backup.create_snapshot(['selected'], tmp_path/'archives')['snapshot']
    restored=tmp_path/'restored'
    backup.restore_snapshot(snap,restored)
    studio.rename(tmp_path/'disconnected')
    monkeypatch.setattr(paths,'ROOT',restored)
    monkeypatch.setattr(paths,'episodes_dir',lambda:restored/'workspace/episodes')
    monkeypatch.setattr(paths,'state_dir',lambda:restored/'workspace/state')
    monkeypatch.setattr(episode,'ROOT',restored)
    assert source_fingerprint() == before
    assert sources.issues('selected') == []
    assert workflow.document('selected')['units'][0]['source_text'] == '城门紧闭。'
    assert (paths.state_dir()/'production-queue/STOP').is_file()


def test_backup_reports_missing_current_receipt(studio,tmp_path):
    from app.services.production_backup import create_snapshot
    receipt=paths.episode_dir('one')/'missing-receipt.json'
    atomic_json(paths.episode_dir('one')/'production/v2/run.json',dict(receipt_path=str(receipt)))
    preview=create_snapshot(['one'],tmp_path/'out',dry_run=True)
    assert not preview['complete_dependencies']
    assert any(r['path']==str(receipt) for r in preview['missing_dependencies'])
    assert not (tmp_path/'out').exists()


def test_identity_resolves_old_addresses_and_ignores_original_path(studio,tmp_path,monkeypatch):
    from app.services import production_identity as identity
    retired=tmp_path/'retired';retired.mkdir()
    atomic_json(studio/'.studio-project.json',{'legacy_path_roots':[str(retired)]})
    old=retired/'material.wav';old.write_bytes(b'old')
    active=studio/'material.wav';active.write_bytes(b'selected')
    atomic_json(paths.episode_dir('one')/'production/v2/registration.json',{'original_path':str(retired/'trace.md'),'audio_path':str(old)})
    monkeypatch.setattr(identity,'implementation_identity',lambda:'fixture')
    first=identity.input_identity('one',['script'])
    old.write_bytes(b'changed old')
    assert identity.input_identity('one',['script']) == first
    active.write_bytes(b'new active')
    assert identity.input_identity('one',['script']) != first


def test_cover_override_retains_checked_act_cards(tmp_path, monkeypatch):
    from app.services import studio_attraction, directing, overlay, forced_alignment
    doc,plan,manifest,evidence=fixture_plan()
    plate=tmp_path/'old.png';plate.write_bytes(b'fixture')
    assets={'old':str(plate)}
    base=[overlay.Spec(overlay.KIND_TITLE,0,3100,title='片头',subtitle='第一幕',fade_in_ms=0),
          overlay.Spec(overlay.KIND_ACT,5400,8200,title='第二幕')]
    atomic_json(tmp_path/'overlays.json',{'version':1,'overlays':[x.payload() for x in base]})
    atomic_json(tmp_path/'directing.json',studio_attraction.compile_direction(plan,doc,manifest,evidence,assets))
    atomic_json(tmp_path/'words.json',evidence)
    monkeypatch.setattr(forced_alignment,'require_evidence',lambda *args:None)
    timeline=directing.apply(manifest,{'alignment_evidence_path':str(tmp_path/'words.json')},tmp_path/'directing.json',assets,tmp_path/'out')
    title,act=overlay.load_path(timeline['overlay_path']).specs
    assert title.title==plan['cover']['text'] and title.subtitle=='第一幕'
    assert act == base[1] and file_sha256(tmp_path/'overlays.json')==timeline['inputs'][str(tmp_path/'overlays.json')]
    from app.services import production_v2 as render
    monkeypatch.setattr(overlay, 'path_for', lambda _: tmp_path/'overlays.json')
    preflight = directing.effective_cards(manifest, read(tmp_path/'directing.json'), evidence, tmp_path/'overlays.json')
    length = round(manifest['samples']*1000/manifest['sample_rate'])
    _, digest = render.timeline_cards('fixture', timeline, length)
    assert digest == fingerprint([card.payload() for card in preflight.specs])
    old_cards = tmp_path/'old-cards.json'
    atomic_json(old_cards, {'version':1, 'overlays':[title.payload()]})
    old_receipt = dict(samples=manifest['samples'], sample_rate=manifest['sample_rate'])
    with pytest.raises(ValueError, match='幕卡'):
        render.check_overlay_receipt('fixture', dict(overlay_path=str(old_cards)), old_receipt)


def test_preflight_limits_prompts_and_checks_full_motion(studio):
    from app.services.production_inputs import prepare
    from app.services.studio_image_reuse import coverage_report
    from app.managers import load
    eid='shots';workspace.create_episode(eid,'城门','城门紧闭。',scope='full')
    folder=paths.episode_dir(eid)
    doc=workflow.document(eid)
    scene={'unit_scenes':[dict(unit_id=doc['units'][0]['unit_id'],image='A')]}
    atomic_json(folder/'scene_plan.json',scene)
    atomic_json(folder/'image_prompts.json',{'A':{'en':'A mounted guard watches the distant city gate in morning sunlight.'}, 'unused':{'en':''}})
    jobs,_=prepare(load(eid),doc,folder/'production/v2',preview=True)
    assert [j.id for j in jobs]==['A']
    scene['unit_scenes'][0]['motion']=dict(type='push',start_zoom=1.,end_zoom=1.8,max_zoom=1.6)
    atomic_json(folder/'scene_plan.json',scene)
    with pytest.raises(ValueError,match='zoom'):
        prepare(load(eid),doc,folder/'production/v2',preview=True)
    assert coverage_report(eid, document=doc,scene_path=folder/'scene_plan.json')['status']=='invalid'


def test_ranges_are_versioned_and_stale_session_cannot_overwrite(tmp_path):
    from app.services import recording_alignment_recovery as recovery
    plan,recording,rows,report=recovery_fixture(tmp_path)
    first=file_sha256(report)
    path=recovery.save_range(report,'1',3,6,expected_sha256=first)
    with pytest.raises(ValueError,match='重新载入'):
        recovery.save_range(report,'1',4,5,expected_sha256=first)
    assert read(report)['rows'][1]['start']==3 and read(path)['ranges']['1']==[3,6]
    master=tmp_path/'ext-current/master.wav'
    source=tmp_path/'current-plan.json';atomic_json(source,dict(source_sha256='current'))
    manifest=dict(master_path=str(master),plan_path=str(source),source_recording=recording,
                  alignment_range_inputs={path.name:file_sha256(path)})
    recovery.validate_range_inputs(manifest)
    atomic_json(source,dict(source_sha256=plan['source_sha256']))
    with pytest.raises(ValueError,match='定位已变化'):
        recovery.validate_range_inputs(dict(manifest,alignment_range_inputs={}))


def test_import_failure_resumes_same_version(studio,monkeypatch):
    from app.services import studio_sources as sources
    author=studio/'story.md';author.write_text('### 【第一幕：城门】\n\n城门紧闭。',encoding='utf-8')
    with patch.object(workflow,'save',side_effect=OSError('interrupted')):
        with pytest.raises(OSError):
            sources.import_authoring('imported','城门',author)
    assert 'imported' not in sources.selectable()
    with pytest.raises(ValueError):
        sources.import_authoring('imported','其他标题',author)
    sources.import_authoring('imported','城门',author)
    assert 'imported' in sources.selectable() and sources.issues('imported') == []
    assert read(workflow.folder('imported')/'workflow.json')['revision']==1
    assert not sources.contract('imported')['approved']


def test_status_hashes_reuse_only_display_reads(tmp_path,monkeypatch):
    from app.services import provenance,production_identity as identity
    monkeypatch.setattr(identity,'ROOT',tmp_path)
    ui=tmp_path/'app/webui/screen.py';ui.parent.mkdir(parents=True);ui.write_bytes(b'new label')
    media=tmp_path/'voice.wav';media.write_bytes(b'first')
    original=provenance._hash_file
    with patch.object(provenance,'_hash_file',wraps=original) as hashes:
        for display in (True,True,False):
            with provenance.status_hashes(display=display):
                assert file_sha256(media)==file_sha256(media)
        assert hashes.call_count==2
        media.write_bytes(b'second')
        with provenance.status_hashes(display=True):
            assert file_sha256(media)==original(media)
        assert hashes.call_count==3
    assert identity.split_receipt_drift({str(ui):'old',str(media):'old'})==([str(media)],[])


def test_quality_refresh_reuses_media_only_for_the_same_manifest(tmp_path, monkeypatch):
    from app.services import production_identity as identity, production_quality as quality
    from app.services.production_timeline import select_after_quality_refresh
    monkeypatch.setattr(identity, 'ROOT', tmp_path)
    manifest, other = tmp_path/'manifest.json', tmp_path/'other.json'
    atomic_json(manifest, {'recording':'original'})
    atomic_json(other, {'recording':'other'})
    checker = Path(quality.__file__).with_name('asr_check.py')
    row = dict(manifest_path=str(manifest), manifest_sha256=file_sha256(manifest),
               decision_sha256=quality.decision_identity(), checker_sha256=file_sha256(checker), takes=[])
    old, new = tmp_path/'old.json', tmp_path/'new.json'
    atomic_json(old, dict(row, decision_sha256='superseded'))
    atomic_json(new, row)
    qa_source = tmp_path/'app/services/asr_check.py'
    qa_source.parent.mkdir(parents=True); qa_source.write_text('# revised quality rules')
    receipt = {str(qa_source):'previous rules'}
    assert identity.split_receipt_drift(receipt, quality_path=old, manifest_path=manifest)[0] == [str(qa_source)]
    assert identity.split_receipt_drift(receipt, quality_path=new, manifest_path=other)[0] == [str(qa_source)]
    assert identity.split_receipt_drift(receipt, quality_path=new, manifest_path=manifest) == ([], [])
    def timeline(report, caption):
        return dict(shots=['plate'], subtitles=[caption], quality_report_path=str(report),
                    inputs={str(report):file_sha256(report), str(manifest):file_sha256(manifest)})
    first, second = tmp_path/'timeline-1.json', tmp_path/'timeline-2.json'
    atomic_json(first, timeline(old, '城门紧闭。'))
    atomic_json(second, timeline(new, '城门紧闭。'))
    assert select_after_quality_refresh(first, second) == first
    atomic_json(second, timeline(new, '城门打开。'))
    assert select_after_quality_refresh(first, second) == second


def test_empty_and_revised_attraction_plans_can_add_beats(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    from app.services import studio_attraction as attraction
    eid='add-beat';workspace.create_episode(eid,'城门','城门紧闭。\n\n将军等待。',scope='full')
    code="from app.webui.attraction_panel import render\nfrom app.services.studio_workflow import document\nrender('add-beat',document('add-beat'))"
    app=AppTest.from_string(code,default_timeout=20).run()
    for quote in ('城门紧闭。','将军等待。'):
        next(s for s in app.selectbox if s.label=='添加镜头所用的旁白').select(quote)
        next(b for b in app.button if b.label=='保存吸引力方案').click().run()
        assert not app.exception
    assert len(attraction.load(eid)['visual_beats'])==2
    from app.managers import load
    Path(load(eid).script_path).write_text('有人来了。',encoding='utf-8')
    app.run()
    next(s for s in app.selectbox if s.label=='添加镜头所用的旁白').select('有人来了。')
    next(b for b in app.button if b.label=='保存吸引力方案').click().run()
    assert [x['quote'] for x in attraction.load(eid)['visual_beats']]==['有人来了。']
    assert any(len(read(p)['visual_beats'])==2 for p in (attraction.path_for(eid).parent/'attraction-history').glob('*.json'))


def test_acceptance_terminates_a_stalled_check():
    import subprocess
    import sys
    import time
    from bin.check_project import run_bounded
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-c', 'import time; time.sleep(45)'], .4)
    assert time.monotonic()-start < 30
    assert run_bounded([sys.executable, '-c', 'pass'], 15) == 0


def test_read_routes_distinguish_unknown_episode_from_draft(tmp_episodes):
    from fastapi import HTTPException
    from app.controllers.v1.episode import get_episode,get_production
    from app.managers import save
    from app.models import Episode
    for route in (get_episode,get_production):
        with pytest.raises(HTTPException) as failure:
            route('unknown')
        assert failure.value.status_code==404
    save(Episode(id='draft',title='草稿',episode_number=0))
    assert get_episode('draft')['status']=='draft' and get_production('draft')['status']=='NOT_STARTED'
