import importlib.util
import json
from pathlib import Path

import pytest

from app.services.script_document import parse_document
from app.services.script_handoff import derive, write_revision, verify_revision
from app.services.narration_pipeline import prepare

pytestmark = pytest.mark.production
HEAD = "# 编剧资料\n发布文案不朗读。\n\n### 【第一幕：决定】\n"


def test_new_appendix_is_excluded_without_changing_legacy_derivation(tmp_path):
    source=(HEAD+"正文照录。\n\n## 本集完成度与质量审查指标\n1. 编务备注。\n").replace('\n','\r\n')
    fresh=write_revision(source,tmp_path)
    assert verify_revision(fresh)['schema_version']=='writing-handoff-3'
    assert (fresh/'spoken.txt').read_text(encoding='utf-8').strip()=='正文照录。'
    assert (fresh/'source.md').read_bytes()==source.encode()
    assert '编务备注' in derive(source,version='writing-handoff-2')['spoken.txt'].decode()


def test_multiline_cues_and_parenthetical_prose_accounted_for():
    source = HEAD + "【画面：保持旧人物\n只用宫门空镜】\n（语气：缓慢但不煽情，结尾留出空间）李勣举起笏。\n\n**没有一口气解决所有问题。**[pause 1s]（参见《旧唐书》，这里仍是普通括注。）\n"
    payloads = derive(source)
    spoken = payloads['spoken.txt'].decode()
    assert '保持旧人物' not in spoken and '缓慢' not in spoken and 'pause' not in spoken
    assert '李勣举起笏。' in spoken and '没有一口气解决所有问题。' in spoken
    assert '（参见《旧唐书》，这里仍是普通括注。）' in spoken
    doc = json.loads(payloads['document.json'])
    assert len(doc['delivery_cues']) == 3
    assert doc['delivery_cues'][-1]['pause_seconds'] == 1
    body = payloads['narration.md'].decode()
    for block in doc['blocks']:
        accounted = set(block['source_map'])
        for exclusion in block['excluded']:
            accounted.update(range(exclusion['start'], exclusion['end']))
        assert accounted == set(range(block['start'], block['end']))
        assert body[block['start']:block['end']] == block['raw_text']
    for cue in json.loads(payloads['directions.json'])['cues']:
        assert source[cue['authoring_start']:cue['authoring_end']] == cue['raw_text']
        assert cue['execution_status'] == 'pending'


@pytest.mark.parametrize('bad', ['[不明魔法]', '[pause 0s]', '[pause 100s]', '【语气：未闭合', '> 这句话是正文。', '（语音压低10%，带对讲机混响：好家伙。）'])
def test_ambiguous_import_writes_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        write_revision(HEAD + '正文。' + '\n' + bad, tmp_path/'revisions')
    assert not (tmp_path/'revisions').exists()


def test_english_equal_pause_retains_semantics():
    payloads = derive(HEAD + '李治继位。[pause=0.5s]朝臣入殿。')
    cues = json.loads(payloads['directions.json'])['cues']
    assert cues[0]['pause_seconds'] == .5
    assert cues[0]['raw_text'] == '[pause=0.5s]'


def test_visual_cue_cannot_borrow_voice_instruction_execution():
    from app.services.delivery import effective_unit, dispositions
    doc = parse_document('[语气：平稳]【画面：宫门远景】【音效：风】正文。')
    asset = {'capabilities': {'instruction': {'status': 'supported'}}}
    assert effective_unit(doc['units'][0], doc, asset)['delivery']['instruction'] == '语气：平稳'
    rows = dispositions(doc, asset)
    assert rows[0]['status'] == 'request_compiled_audio_review_pending'
    assert all(r['status'] == 'unsupported_needs_disposition' and r['instruction'] is None for r in rows[1:])


def test_split_unit_cue_has_exactly_one_target():
    text = '前情' * 40 + '。' + '[语气：平静]' + '后情' * 40 + '。' + '尾声' * 40 + '。'
    doc = parse_document(text)
    cue = doc['delivery_cues'][0]
    owners = [u for u in doc['units'] if cue['cue_id'] in u['cue_ids']]
    assert len(owners) == 1 and owners[0]['unit_id'] == cue['unit_id']
    assert owners[0]['source_text'].startswith('后情') and cue['text_offset'] == 0


@pytest.mark.parametrize('file', ['source.md', 'spoken.txt', 'narration.md', 'directions.json'])
def test_derived_or_source_tamper_stops_plan(tmp_path, file):
    target = write_revision(HEAD + '保留可汗、李勣、仆射的字形。', tmp_path/'revisions')
    path = target/file
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError):
        prepare(target/'narration.md', tmp_path/'plans')
    assert not (tmp_path/'plans').exists()


def test_import_is_idempotent_and_does_not_overwrite_old_media(tmp_path):
    spec = importlib.util.spec_from_file_location('dna_import', Path(__file__).parents[1]/'bin/setup_episode_from_writing_dna.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    episode = tmp_path/'episodes'/'sandbox-only'
    episode.mkdir(parents=True)
    old = {'script.md': b'old source', 'script_narration.md': b'old narration', 'old.wav': b'old take'}
    for name, data in old.items():
        (episode/name).write_bytes(data)
    src = tmp_path/'author.md'
    src.write_text(HEAD + '【语气：平静】新的正文。[停顿0.5秒]', encoding='utf-8')
    first = module.import_revision('sandbox-only', src, tmp_path)
    second = module.import_revision('sandbox-only', src, tmp_path)
    assert first == second
    manifest = verify_revision(Path(first['revision']))
    assert manifest['approved'] is False
    assert manifest['audio_binding'] == 'new_recording_required'
    assert all((episode/name).read_bytes() == data for name, data in old.items())
    plan = json.loads(Path(first['plan']).read_text(encoding='utf-8'))
    assert ''.join(u['source_text'] for u in plan['units']) == '新的正文。'
    assert all(u['source_text'] == u['approved_spoken_text'] == u['display_text'] for u in plan['units'])


@pytest.mark.parametrize('mutation', ['source_after_prepare', 'deleted_manifest', 'edited_cue'])
def test_prepared_handoff_is_rechecked_before_quote_or_dispatch(tmp_path, mutation):
    from app.services import narration_pipeline as N
    target = write_revision(HEAD + '【语气：平静】新的正文。', tmp_path/'revisions')
    plan_path = prepare(target/'narration.md', tmp_path/'plans')
    if mutation == 'source_after_prepare':
        with (target/'source.md').open('ab') as f:
            f.write(b'\n')
    elif mutation == 'deleted_manifest':
        (target/'manifest.json').unlink()
    else:
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        plan['delivery_cues'][0]['raw_text'] = '【语气：狂怒】'
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    asset = tmp_path/'asset.json'
    asset.write_text('{}', encoding='utf-8')
    calls = []
    with pytest.raises(ValueError, match='[Hh]andoff'):
        N.generation_quote(plan_path, asset)
    with pytest.raises(ValueError, match='[Hh]andoff'):
        N._synthesize(plan_path, asset, max_requests=1, max_characters=100,
                      provider=lambda *args: calls.append(args))
    assert not calls and not (plan_path.parent/'takes').exists()
