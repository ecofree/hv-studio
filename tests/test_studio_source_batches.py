import json
from pathlib import Path

import pytest

from app import paths
from app.services import studio_sources as S, studio_workflow as W
from app.services.script_handoff import derive, write_revision, verify_revision
from app.services.provenance import atomic_json, file_sha256

pytestmark=pytest.mark.production

AUTHOR='# 写前备注\r\n|史源|短摘|\r\n|一|二|\r\n\r\n### 【第一幕：出发】\r\n* **本幕利害切口**：只能走一条路。\r\n* **视听构图**：观察关门。\r\n\r\n【语气：松弛】\r\n**他出了门。**\r\n\r\n### 【第二幕：归来】\r\n他回来了。\r\n\r\n【片尾字幕】：\r\n**下集预告**\r\n**另一个故事**\r\n'
TTS='### 【第一幕：出发】\n```text\n他出了门。\n```\n### 【第二幕：归来】\n```text\n他回来了。\n```\n'


@pytest.mark.parametrize('original_exists', [False, True])
def test_standalone_import_without_original_checkout(tmp_path, monkeypatch, original_exists):
    from app.services import studio_workspace as workspace
    monkeypatch.setattr(paths, 'ROOT', tmp_path)
    monkeypatch.setattr(paths, 'episodes_dir', lambda: tmp_path/'workspace/episodes')
    monkeypatch.setattr(paths, 'state_dir', lambda: tmp_path/'workspace/state')
    monkeypatch.setattr(workspace, 'config', lambda root=None: {'original_root': str(tmp_path/'missing-original')})
    if original_exists:
        (tmp_path/'missing-original').mkdir()
    source=tmp_path/'author.md'; source.write_text(AUTHOR, encoding='utf-8', newline='')
    revision=S.import_authoring('standalone','独立项目',source)
    binding=S.contract('standalone')
    assert revision.is_dir() and not S.issues('standalone')
    assert binding['inputs'][0]['original_path'] is None
    source.write_text('外部更改',encoding='utf-8')
    assert not S.issues('standalone') and S.external_updates('standalone')


def test_existing_original_still_rejects_mismatch(tmp_path, monkeypatch):
    from app.services import studio_workspace as workspace
    root=tmp_path/'studio';root.mkdir();original=tmp_path/'original';original.mkdir()
    monkeypatch.setattr(paths, 'ROOT', root)
    monkeypatch.setattr(paths, 'episodes_dir', lambda: root/'workspace/episodes')
    monkeypatch.setattr(paths, 'state_dir', lambda: root/'workspace/state')
    monkeypatch.setattr(workspace, 'config', lambda root=None: {'original_root': str(original)})
    source=root/'author.md';source.write_text(AUTHOR,encoding='utf-8')
    (original/'author.md').write_text('不同原文',encoding='utf-8')
    with pytest.raises(ValueError,match='不一致'):
        S.import_authoring('mismatch','校验',source)


def test_authoring_channels_are_lossless_and_never_read_as_narration(tmp_path):
    data=derive('\ufeff'+AUTHOR)
    assert data['source.md']==('\ufeff'+AUTHOR).encode('utf-8')
    assert ''.join(json.loads(data['document.json'])['units'][i]['source_text'] for i in range(2))=='他出了门。他回来了。'
    info=json.loads(data['authoring.json']);source=data['source.md'].decode()
    assert len(info['acts'])==2 and len(info['notes'])==2
    assert '另一个故事' in ''.join(r['raw_text'] for r in info['end_cards'])
    doc=json.loads(data['document.json']);body=data['narration.md'].decode()
    for block in doc['blocks']:
        indices=set(block['source_map'])
        for r in block['excluded']:indices.update(range(r['start'],r['end']))
        assert indices==set(range(block['start'],block['end']))
        assert body[block['start']:block['end']]==block['raw_text']
    for unit,origin in zip(doc['units'],info['unit_sources']):
        assert ''.join(source[i] for i in origin['authoring_source_map'])==unit['source_text']
    for note in info['notes']+info['end_cards']:
        assert source[note['start']:note['end']]==note['raw_text']
    from app.services.narration_pipeline import prepare,_require_writing_handoff
    revision=write_revision('\ufeff'+AUTHOR,tmp_path/'revisions')
    plan=prepare(revision/'narration.md',tmp_path/'plans')
    _require_writing_handoff(json.loads(plan.read_text('utf-8')))


def test_unknown_editorial_field_blocks_without_silently_deleting(tmp_path):
    with pytest.raises(ValueError):
        write_revision(AUTHOR.replace('视听构图','未知指令'),tmp_path/'revisions')
    assert not (tmp_path/'revisions').exists()


def test_unformatted_unknown_list_is_preserved_for_review():
    with pytest.raises(ValueError):derive(AUTHOR.replace('* **视听构图**','* 未知指令'))


@pytest.fixture
def source_files(tmp_path,tmp_episodes,monkeypatch):
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    original=tmp_path/'original';original.mkdir()
    atomic_json(tmp_path/'.studio-project.json',dict(original_root=str(original)))
    for name,content in [('author.md',AUTHOR),('tts.md',TTS),('rules.md','按用户原文。')]:
        (tmp_path/name).write_bytes(content.encode());(original/name).write_bytes(content.encode())
    return tmp_path


def imported(root,eid='user-fixture'):
    return S.import_authoring(eid,'出发',root/'author.md',tts_file=root/'tts.md',rule_files=[root/'rules.md'])


def test_import_separates_new_source_from_old_media_and_cannot_rewrite(source_files):
    root=source_files;revision=imported(root)
    assert (revision/'source.md').read_bytes()==(root/'author.md').read_bytes()
    assert verify_revision(revision)['approved'] is False
    assert not S.issues('user-fixture') and not W.review_valid('user-fixture','editorial')
    assert W.load('user-fixture')['voice']['master']==''
    assert S.selectable()==['user-fixture']
    with pytest.raises(ValueError,match='原项目'):
        W.replace_source('user-fixture',AUTHOR,expected_identity=W.identity('user-fixture'))
    with pytest.raises(ValueError,match='旧版本'):
        imported(root)


@pytest.mark.parametrize('file',['author.md','tts.md','rules.md'])
def test_changed_original_prevents_further_use(source_files,file):
    imported(source_files)
    p=source_files/'original'/file;p.write_bytes(p.read_bytes()+b'\n')
    assert S.external_updates('user-fixture')
    S.require_usable('user-fixture')


def test_different_tts_is_not_silently_selected(source_files):
    for p in [source_files/'tts.md',source_files/'original/tts.md']:
        p.write_bytes(TTS.replace('出了','进了').encode())
    with pytest.raises(ValueError,match='正文不一致'):imported(source_files)
    assert not paths.episode_dir('user-fixture').exists()


def test_excluded_version_hidden_and_blocked_in_service(source_files):
    imported(source_files)
    S.set_disposition('user-fixture','excluded','用户明确停用')
    assert not S.selectable() and S.selectable(True)==['user-fixture']
    with pytest.raises(ValueError,match='禁止'):W.require_production('user-fixture',['tts'])


def test_ui_default_is_current_and_source_locked(source_files):
    from streamlit.testing.v1 import AppTest
    imported(source_files)
    app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app/webui/Studio.py'),default_timeout=30).run()
    assert not app.exception
    app.switch_page('app_pages/story.py').run()
    assert not app.exception
    assert next(t for t in app.text_area if t.label=='原稿（只读）').disabled
    assert not any(b.label=='保存为独立文案修订' for b in app.button)


def test_cards_are_verbatim_and_parent_rule_change_invalidates_pilot(source_files):
    from app.services.studio_workspace import create_pilot
    imported(source_files)
    value=W.load('user-fixture')
    value['cards']=[dict(at={'at':'start'},duration_ms=3000,card=dict(kind='title_card',title='出发',fade_in_ms=0))]
    W.save('user-fixture',value,expected_revision=value['revision'])
    child=create_pilot('user-fixture','pilot-fixture',0,1)
    assert W.load(child)['cards'][0]['card']['title']=='出发'
    assert not S.issues(child)
    value=W.load('user-fixture');value['cards'][0]['card']['title']='擅自改写的标题'
    W.save('user-fixture',value,expected_revision=value['revision'])
    p=source_files/'original/rules.md';p.write_bytes(b'updated')
    with pytest.raises(ValueError):S.require_usable(child)


def test_old_media_cannot_be_registered_as_current_script(source_files):
    imported(source_files)
    old=source_files/'old.wav';old.write_bytes(b'preserved old media')
    with pytest.raises(ValueError,match='制作来源记录'):
        W.register_sample('user-fixture',old,'误选旧素材',expected_identity=W.identity('user-fixture'))
    assert not W.samples('user-fixture') and old.read_bytes()==b'preserved old media'


def test_current_audio_proof_checks_source_identity_and_speed(source_files,monkeypatch):
    from app.services import studio_media as M
    imported(source_files)
    wav=source_files/'candidate.wav';wav.write_bytes(b'opaque candidate fixture')
    monkeypatch.setattr(M,'wav_clock',lambda _:dict(samples=100,sample_rate=24000,channels=1,sample_width=2))
    receipt=M.record_speed(wav,wav.with_suffix('.speed.json'),applied_rate=1.,evidence='isolated fixture',
                           source_identity=W.document('user-fixture')['source_sha256'])
    proof=source_files/'source-map.json'
    row=dict(master=str(wav),sha256=file_sha256(wav),design_identity=W.identity('user-fixture'),
             source_sha256=W.document('user-fixture')['source_sha256'],speed_receipt=str(receipt),
             voice_design_identity=W.voice_design_identity('user-fixture'))
    atomic_json(proof,row)
    assert W.sample_source_proof('user-fixture',wav)['sha256']==file_sha256(proof)
    value=W.load('user-fixture');value['voice']['master']=str(wav)
    W.save('user-fixture',value,expected_revision=value['revision'])
    assert W.sample_source_proof('user-fixture',wav)['sha256']==file_sha256(proof)
    row['source_sha256']='old source';atomic_json(proof,row)
    with pytest.raises(ValueError,match='当前文案'):W.sample_source_proof('user-fixture',wav)
