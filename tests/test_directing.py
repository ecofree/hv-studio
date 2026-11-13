"""Source-bound direction and sample-preserving mixing, using isolated fixtures."""
import copy
import wave
import numpy as np
import pytest
from app.services import directing as D, overlay as O, sound_design as S
from app.services.provenance import atomic_json

pytestmark = pytest.mark.production


@pytest.fixture
def direction_fixture(tmp_path, monkeypatch):
    m={'samples':48000,'sample_rate':24000,'alignment':[
        {'unit_id':'u','text':'甲乙。','sample_start':100,'sample_end':40000}]}
    e={'words':{'u':[{'text_start':0,'sample_start':100},{'text_start':1,'sample_start':24000}]}}
    ep=tmp_path/'evidence.json'; atomic_json(ep,e)
    t={'alignment_evidence_path':str(ep)}
    monkeypatch.setattr('app.services.forced_alignment.require_evidence',lambda m,t:None)
    design={'version':1,'source_identity':D.source_identity(m),'visual_events':[
        {'at':{'at':'start'},'image_id':'A','motion':{}},
        {'at':{'unit_id':'u','text_start':1},'image_id':'A','motion':{}}]}
    return m,e,t,design,tmp_path


def test_direction_follows_actual_word_clock_and_preserves_text(direction_fixture):
    m,e,t,p,root=direction_fixture; original=copy.deepcopy(m)
    path=root/'direction.json'; atomic_json(path,p)
    out=D.apply(m,t,path,{'A':'fixture.png'},root/'compiled')
    assert [(s['sample_start'],s['sample_end']) for s in out['shots']]==[(0,24000),(24000,48000)]
    assert m==original and t=={'alignment_evidence_path':str(root/'evidence.json')}
    assert out['direction']['listening_approved'] is False
    assert str(path.resolve()) in out['inputs']


@pytest.mark.parametrize('fault,match', [('source','bound'),('image','declared'),('boundary','boundary'),('order','increase')])
def test_invalid_direction_fails_closed(direction_fixture,fault,match):
    m,e,t,p,root=direction_fixture
    if fault=='source': p['source_identity']='stale'
    if fault=='image': p['visual_events'][0]['image_id']='missing'
    if fault=='boundary': p['visual_events'][1]['at']['text_start']=2
    if fault=='order': p['visual_events'][1]['at']={'at':'start'}
    path=root/'direction.json'; atomic_json(path,p)
    with pytest.raises(ValueError,match=match): D.apply(m,t,path,{'A':'fixture.png'},root/'compiled')


def test_first_frame_is_visible_without_changing_existing_cards(tmp_path):
    old=O.Spec(kind=O.KIND_TITLE,start_ms=0,end_ms=3000,title='托孤')
    new=O.Spec(kind=O.KIND_TITLE,start_ms=0,end_ms=3000,title='托孤',fade_in_ms=0)
    assert O._f(old,0)==0 and O._f(new,0)==1
    assert 'fade_in_ms' not in old.payload()
    O.validate([new],total_ms=3000)
    path=tmp_path/'cards.json'; atomic_json(path,{'version':1,'overlays':[new.payload()]})
    assert O.load_path(path).layers_at(0)
    with pytest.raises(ValueError): O.validate([O.Spec(kind=O.KIND_TITLE,start_ms=0,end_ms=3000,fade_in_ms=3000)])


def test_mix_preserves_clock_and_speech_pitch(tmp_path):
    sr=24000; t=np.arange(sr*2)/sr
    pcm=np.rint(np.sin(2*np.pi*330*t)*8000).astype('<i2')
    source=tmp_path/'source.wav'
    with wave.open(str(source),'wb') as w:
        w.setparams((1,2,sr,0,'NONE','not compressed'));w.writeframes(pcm.tobytes())
    original=source.read_bytes()
    plan={'version':1,'voice_gains':[{'sample_start':12000,'sample_end':36000,'db':1.5}],
          'beds':[{'sample_start':0,'sample_end':12000,'tone':'warm','db':-36}]}
    target=S.mix(source,plan,tmp_path/'mixed.wav')
    with wave.open(str(target),'rb') as w:
        assert w.getnframes()==len(pcm) and w.getframerate()==sr
        output=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2')
    assert source.read_bytes()==original
    # After the bed ends, voiced zero crossings remain at the exact source samples.
    assert np.array_equal(np.signbit(output[14000:34000]),np.signbit(pcm[14000:34000]))
    assert np.max(np.abs(output.astype(float)))<32767
    assert np.sqrt(np.mean(output[14000:34000].astype(float)**2))>np.sqrt(np.mean(pcm[14000:34000].astype(float)**2))
    from app.services.narration_pipeline import read
    assert read(target.with_suffix('.json'))['listening_approved'] is False


@pytest.mark.parametrize('row',[
    {'sample_start':0,'sample_end':110,'db':1},
    {'sample_start':0,'sample_end':100,'db':4},
    {'sample_start':0,'sample_end':100,'db':float('nan')}])
def test_unsafe_sound_events_rejected(row):
    with pytest.raises(ValueError): S.validate({'version':1,'voice_gains':[row]},100)
