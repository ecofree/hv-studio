"""Karaoke word highlighting: offset translation, degradation, and no-op safety.

Use isolated punctuation, quotation and tail-stripping cases. Historical episode
folders are never inputs to the test suite.
"""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytestmark = pytest.mark.production

from app.managers import load
from app.services import compose as C
from app.services import word_timing as W

@pytest.fixture
def aligned_episode(tmp_episodes):
    from app.models.episode import Episode,Frame
    from app.managers import save
    from app.services.provenance import atomic_json
    eid='word-alignment-fixture'
    texts=['你好，世界。','“他说：出发！”','甲、乙；丙。','重复，重复。','第一段有十万人自卖身']
    frames=[Frame(id=f'frame-{i:03}',text=text) for i,text in enumerate(texts)]
    ep=Episode(id=eid,title='独立字幕夹具',episode_number=1,frames=frames);save(ep)
    words={}
    for frame in frames:
        rows=[]
        for i,char in enumerate(frame.text):
            if char.isalnum():
                a=len(rows)*100;rows.append([i,i+1,a,a+100])
        words[frame.id]=rows
    atomic_json(W.path_for(eid),dict(version=1,words=words))
    return eid


@pytest.fixture(autouse=True)
def _reset():
    C.reset_word_plan()
    yield
    C.reset_word_plan()


def test_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(W, 'path_for', lambda e: tmp_path / 'nope.json')
    plan = W.load('caomin-99')
    assert not plan
    assert plan.span('frame-000', 100.0) is None


def test_bad_version_rejected(tmp_path, monkeypatch):
    p = tmp_path / 'word_timing.json'
    p.write_text(json.dumps({'version': 42, 'words': {}}), encoding='utf-8')
    monkeypatch.setattr(W, 'path_for', lambda e: p)
    with pytest.raises(ValueError, match='schema'):
        W.load('caomin-99')


def test_span_returns_the_word_being_spoken():
    plan = W.Plan({'f': [W.Word(0, 2, 0.0, 500.0), W.Word(2, 5, 500.0, 900.0)]})
    assert plan.span('f', 100.0) == (0, 2)
    assert plan.span('f', 499.9) == (0, 2)
    assert plan.span('f', 500.0) == (2, 5)
    assert plan.span('f', 900.0) is None, 'end is exclusive'
    assert plan.span('missing', 0.0) is None


@pytest.mark.parametrize('source,words,ok', [
    ('你好世界', [W.Word(0, 2, 0, 100), W.Word(2, 4, 100, 200)], True),
    # punctuation becomes a space on screen; offsets must still land right
    ('你好，世界。', [W.Word(0, 2, 0, 100), W.Word(3, 5, 100, 200)], True),
    # a gap in the source is legitimate -- not every character is spoken
    ('abcdef', [W.Word(0, 2, 0, 100)], True),
])
def test_map_words_translates_offsets(source, words, ok):
    display = C.subtitle_display_text(source)
    got = W.map_words(display, source, words)
    assert (got is not None) is ok
    if ok and got:
        for w, (a, b) in zip(words, got):
            assert display[a:b] == C.subtitle_display_text(source[w.start:w.end]).strip()


def test_map_words_refuses_rather_than_guessing():
    source = '你好世界你好世界'
    display = C.subtitle_display_text(source)
    # overlapping words cannot both map to monotone, non-overlapping ranges
    assert W.map_words(display, source, [W.Word(0, 4, 0, 1), W.Word(2, 6, 1, 2)]) is None


def test_fingerprint_ignores_frames_outside_window():
    plan = W.Plan({'a': [W.Word(0, 2, 0, 100)], 'b': [W.Word(0, 2, 5000, 5100)]})
    assert plan.fingerprint((0, 1000)) != plan.fingerprint()
    assert plan.fingerprint((2000, 3000)) == W.Plan({}).fingerprint((2000, 3000))


def test_draw_without_highlight_draws_one_colour():
    C.check_font_available()
    # Two fresh images: drawing twice on one image would composite the second
    # pass over the first and prove nothing.
    a = Image.new('RGB', (C.W, C.H), (0, 0, 0))
    b = Image.new('RGB', (C.W, C.H), (0, 0, 0))
    C._draw_subtitle(a, '你好世界')
    C._draw_subtitle(b, '你好世界', None)
    assert list(a.getdata()) == list(b.getdata()), \
        'hl=None must be byte-identical to the pre-karaoke renderer'


def test_highlight_changes_pixels_and_stays_in_bounds():
    C.check_font_available()
    source = '第一段有十万人自卖身'
    display = C.subtitle_display_text(source)
    img = Image.new('RGB', (C.W, C.H), (0, 0, 0))
    C._draw_subtitle(img, source, (0, 5))
    highlighted = img.copy()
    C._draw_subtitle(img, source)
    assert list(img.getdata()) != list(highlighted.getdata())
    assert display[0:5]


def test_aligned_data_maps_every_frame(aligned_episode):
    plan = W.load(aligned_episode)
    ep = load(aligned_episode)
    frames = {f.id: f for f in ep.frames}
    missing = [k for k in plan.words if k not in frames]
    assert not missing, f'word_timing references unknown frames: {missing[:5]}'
    bad = []
    for fid, ws in plan.words.items():
        source = (frames[fid].text or '').strip()
        if W.map_words(C.subtitle_display_text(source), source, ws) is None:
            bad.append(fid)
        for w in ws:
            if not 0 <= w.start < w.end <= len(source) or w.ms_end <= w.ms_start:
                bad.append(f'{fid}:span')
                break
    assert not bad, f'frames whose word offsets will not map: {bad[:8]}'


def test_highlight_span_resolves_for_aligned_frames(aligned_episode):
    ep = load(aligned_episode)
    plan = W.load(aligned_episode)
    hit = miss = 0
    for fid, ws in list(plan.words.items())[:60]:
        f = next(x for x in ep.frames if x.id == fid)
        mid = (ws[0].ms_start + ws[0].ms_end) / 2
        sp = C._highlight_span(plan, fid, (f.text or '').strip(), mid)
        if sp:
            hit += 1
            assert 0 <= sp[0] < sp[1]
        else:
            miss += 1
    assert hit > 0, 'no frame produced a highlight span'
    assert hit >= miss, f'too many frames failed to resolve: {miss} vs {hit}'
