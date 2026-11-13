"""Overlay layer: layout rules, alpha bucketing, and the no-op-when-absent invariant.

The last one is the load-bearing guarantee: an episode without overlays.json must
produce exactly the pre-change segment fingerprint, or every already-approved
episode gets needlessly re-encoded.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# production, not shared: this layer changes the live renderer's pixel output and
# its segment fingerprint, so it belongs in the default acceptance suite.
pytestmark = pytest.mark.production

from app.services import compose as C
from app.services import overlay as O


@pytest.fixture(autouse=True)
def _reset():
    C.reset_overlay_plan()
    yield
    C.reset_overlay_plan()


def _spec(**kw):
    base = dict(kind=O.KIND_TITLE, start_ms=0, end_ms=3000, title='均田制')
    base.update(kw)
    return O.Spec(**base)


def test_missing_file_is_empty_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(O, 'path_for', lambda ep_id: tmp_path / 'nope.json')
    plan = O.load('caomin-99')
    assert not plan
    assert plan.layers_at(0) == []
    assert plan.dim_at(0) == 0.0


def test_layout_is_measured_not_guessed():
    m = O.measure(_spec(subtitle='大唐草民的三件小事', episode='第 2 集'))
    assert not m['errors']
    assert [s for _, s in m['rows']] == [O.TITLE_SIZE, O.SUBTITLE_SIZE, O.EPISODE_SIZE]
    top, height = m['top'], m['height']
    assert 0 <= top and top + height <= C.H


def test_long_title_autofits_instead_of_being_cropped():
    m = O.measure(_spec(title='第一幕  均田制的公摊与崩溃', kind=O.KIND_ACT))
    assert not m['errors']
    assert m['shrunk'], 'an 11-character act title must shrink to fit'
    text, size = m['rows'][0]
    assert C._get_font(size).getbbox(text)[2] <= C.W - O.PAD_X * 2


def test_unsalvageable_overflow_is_rejected_loudly():
    with pytest.raises(ValueError, match='exceeds 18 characters'):
        O.validate([_spec(title='标' * 25)])


def test_anchor_y_override_moves_the_block():
    """A two-shot at an act boundary needs a lower card than the sky default."""
    default = O.measure(_spec(kind=O.KIND_ACT, title='第二幕', subtitle='荒原深处的试探'))
    lower = O.measure(_spec(kind=O.KIND_ACT, title='第二幕', subtitle='荒原深处的试探', anchor_y=0.66))
    assert not lower['errors']
    assert lower['top'] > default['top']


def test_anchor_y_outside_range_rejected():
    with pytest.raises(ValueError, match='anchor'):
        O.validate([_spec(anchor_y=1.2)])


@pytest.mark.parametrize('bad,match', [
    (dict(start_ms=0, end_ms=100), f'shorter than {O.MIN_MS}ms'),
    (dict(start_ms=0, end_ms=9000), f'longer than {O.MAX_MS}ms'),
    (dict(start_ms=-5, end_ms=3000), 'starts before timeline'),
    (dict(fade_ms=2000), 'fade overlaps itself'),
    (dict(title='   '), 'no title'),
])
def test_invalid_specs_raise(bad, match):
    with pytest.raises(ValueError, match=match):
        O.validate([_spec(**bad)])


def test_overlapping_overlays_rejected():
    with pytest.raises(ValueError, match='overlap'):
        O.validate([_spec(start_ms=0, end_ms=3000), _spec(start_ms=2000, end_ms=5000)])


def test_alpha_is_bucketed_not_per_frame():
    spec = _spec(fade_ms=400)
    plan = O.Plan([spec])
    seen = []
    for ms in range(0, 3000, 40):
        for _t, _b in plan.layers_at(ms):
            seen.append(ms)
    assert len(seen) > 50, 'overlay should be visible across its span'
    assert len(plan._cache) <= O.ALPHA_BUCKETS + 1, (
        f'tile cache grew to {len(plan._cache)}; rasterisation is not bucketed')
    plan.close()
    assert not plan._cache


def test_tiles_are_evicted_once_they_end():
    plan = O.Plan([_spec(start_ms=0, end_ms=1000)])
    # ms=0 is the very first frame of the fade, where alpha is 0 and the overlay
    # is deliberately invisible -- sample mid-fade instead.
    assert plan.layers_at(0) == []
    plan.layers_at(500)
    assert plan._cache
    plan.layers_at(2000)
    assert not plan._cache


def test_dim_follows_the_fade_curve():
    plan = O.Plan([_spec(start_ms=0, end_ms=2000, fade_ms=400, dim=0.5)])
    assert plan.dim_at(0) == 0.0
    assert plan.dim_at(200) == pytest.approx(0.25, abs=0.02)
    assert plan.dim_at(1000) == pytest.approx(0.5, abs=0.02)
    assert plan.dim_at(2000) == 0.0


def test_scrim_can_be_dropped():
    """Without the backing plane the tile crops tight to the glyphs.

    Smaller is better here: less paste area per frame, and the subject behind
    the card stays visible.
    """
    on, _ = O.rasterise(_spec(), 1.0)
    off, _ = O.rasterise(_spec(scrim=False), 1.0)
    assert off.width < on.width and off.height < on.height
    assert sum(off.getchannel('A').getdata()) < sum(on.getchannel('A').getdata())
    on.close(); off.close()


def test_fingerprint_ignores_out_of_window_overlays():
    plan = O.Plan([_spec(start_ms=50000, end_ms=53000)])
    assert plan.fingerprint((0, 2000)) != plan.fingerprint()
    assert plan.fingerprint((0, 2000)) == O.Plan([]).fingerprint((0, 2000))
    assert plan.fingerprint((50000, 53000)) != O.Plan([]).fingerprint((50000, 53000))


class _Sh:
    def __init__(self, i):
        self.id = f'shot{i}'; self.duration_ms = 1000; self.image_path = None
        self.upscaled_path = None; self.subtitle_text = f'第{i}句'; self.motion = None


def test_absent_plan_leaves_segment_fingerprint_untouched():
    """The regression guard: no overlays.json -> byte-identical fingerprint."""
    tl = [(_Sh(0), 0, 1000), (_Sh(1), 1000, 2000)]
    args = (tl, ['-c:v', 'libx264'], 'segment', [tl], 0)
    base = C._seg_params_fingerprint(*args)

    C._OVERLAY_PLAN = O.Plan([_spec(start_ms=50000, end_ms=53000)])
    assert C._seg_params_fingerprint(*args) == base, \
        'an overlay outside this segment must not invalidate it'

    C._OVERLAY_PLAN = O.Plan([_spec(start_ms=0, end_ms=3000)])
    assert C._seg_params_fingerprint(*args) != base, \
        'an overlay inside this segment must invalidate it'


def test_roundtrip_through_json(tmp_path, monkeypatch):
    import json
    p = tmp_path / 'overlays.json'
    p.write_text(json.dumps({
        'version': O.SCHEMA_VERSION,
        'overlays': [{'kind': 'title_card', 'start_ms': 0, 'end_ms': 3400,
                      'title': '均田制', 'subtitle': '大唐草民的三件小事',
                      'episode': '第 2 集', 'dim': 0.35, 'scrim': False,
                      'anchor_y': 0.62}],
    }, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(O, 'path_for', lambda ep_id: p)
    plan = O.load('caomin-02')
    assert len(plan.specs) == 1
    s = plan.specs[0]
    assert s.dim == 0.35 and s.scrim is False and s.title == '均田制'
    assert s.anchor_y == 0.62
    plan.close()


def test_unsupported_schema_version_rejected(tmp_path, monkeypatch):
    import json
    p = tmp_path / 'overlays.json'
    p.write_text(json.dumps({'version': 99, 'overlays': []}), encoding='utf-8')
    monkeypatch.setattr(O, 'path_for', lambda ep_id: p)
    with pytest.raises(ValueError, match='schema version'):
        O.load('caomin-02')


def test_corrupt_file_surfaces_rather_than_silently_skipping(tmp_path, monkeypatch):
    p = tmp_path / 'overlays.json'
    p.write_text('{"version": 1, "overlays": [{"kind": "title_card"}]}', encoding='utf-8')
    monkeypatch.setattr(O, 'path_for', lambda ep_id: p)
    with pytest.raises(Exception):
        C.overlay_plan('caomin-02')

# ------------------------------------------------- the caching trap that bit us
def test_layer_source_hash_changes_the_segment_fingerprint():
    """Editing a layer's drawing code must invalidate cached segments.

    Regression guard, originally written for the now-removed kinetic layer. It
    earns its keep the moment a layer is *added*: adding the sidecar JSON
    changes the fingerprint and forces one re-encode, but if that first version
    has a rendering bug and draws nothing, fixing the bug changes no
    fingerprinted value -- every later run calls the segments "pixel-identical"
    and reuses them, so the fix can never reach the film. Only a source hash
    catches that.
    """
    from app.services import compose as C

    class _Sh:
        def __init__(self, i):
            self.id = f'shot{i}'
            self.duration_ms = 1000
            self.image_path = None
            self.upscaled_path = None
            self.subtitle_text = f'第{i}句'
            self.motion = None

    tl = [(_Sh(0), 0, 1000), (_Sh(1), 1000, 2000)]
    args = (tl, ['-c:v', 'libx264'], 'segment', [tl], 0)
    before = C._seg_params_fingerprint(*args)

    import inspect
    src = inspect.getsource(C._seg_params_fingerprint)
    assert 'layer_src' in src, 'layer source hashes must be in the fingerprint payload'

    real = C._layer_sha
    try:
        C._layer_sha = lambda name: 'deadbeef' if name == 'overlay' else real(name)
        after = C._seg_params_fingerprint(*args)
    finally:
        C._layer_sha = real
    assert before != after, 'a changed layer source must invalidate the segment'


def test_absent_layer_reports_none_instead_of_raising():
    """A layer that does not exist must not perturb the fingerprint."""
    from app.services import compose as C
    assert C._layer_sha('a_module_that_does_not_exist') is None
    for name in ('overlay', 'word_timing'):
        assert C._layer_sha(name) is not None, f'{name} is installed but unreadable'
