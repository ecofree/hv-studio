"""Maintenance changes refresh acceptance while retaining verified pixels."""
import pytest
from pathlib import Path
from PIL import Image
from app.models.shot import MotionSpec
from app.services import production_v2 as P, video_segment_cache as cache
from app.services.provenance import atomic_json, file_sha256
from app.services.narration_pipeline import read
from test_production_v2 import assembly

pytestmark = pytest.mark.production


def test_projection_tracks_only_affected_pixels_and_render_dependencies(tmp_path):
    for relative in cache.DEPENDENCIES:
        p = tmp_path/relative; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('original')
    font = tmp_path/'font'; font.write_bytes(b'font')
    settings = dict(runtime={'ffmpeg':'test'}, subtitle={'size':72}, encoder=['libx264'], font=font)
    shared = cache.context(tmp_path, **settings)
    worker = tmp_path/'app/services/batch_worker.py'; worker.write_text('maintenance')
    assert shared == cache.context(tmp_path, **settings)
    (tmp_path/'app/services/motion.py').write_text('different camera')
    assert shared != cache.context(tmp_path, **settings)
    images = [tmp_path/'a.png', tmp_path/'b.png']
    specs = [(dict(sample_start=i*48000, sample_end=(i+1)*48000, image_path=str(p)), MotionSpec())
             for i,p in enumerate(images)]
    inputs = {str(p.resolve()):str(i) for i,p in enumerate(images)}
    cues = [dict(text='一',sample_start=0,sample_end=48000), dict(text='二',sample_start=48000,sample_end=96000)]
    def keys():
        return [cache.segment_fingerprint([(i,i*50,(i+1)*50)], specs,cues,24000,inputs,shared) for i in range(2)]
    initial = keys(); inputs[str(images[1].resolve())] = 'changed'
    changed = keys(); assert initial[0] == changed[0] and initial[1] != changed[1]
    cues[0]['text'] = '新字幕'
    assert keys()[0] != changed[0] and keys()[1] == changed[1]
    specs[0][1].end_zoom = 1.3
    assert keys()[0] != changed[0]


@pytest.mark.NEEDS_FFMPEG
def test_maintenance_revalidates_candidate_and_reuses_real_video(tmp_path, monkeypatch):
    from app.config import config
    from app.services.production_identity import artifacts_current
    settings = config.get_settings()
    compose = settings.compose.model_copy(update={'video_encoder':'libx264',
                                                 'fallbacks':[{'preset':'ultrafast','crf':24}]})
    monkeypatch.setattr(config,'get_settings',lambda:settings.model_copy(update={'compose':compose}))
    manifest,*_ = assembly(tmp_path)
    m = read(manifest)
    image = tmp_path/'image.png'; Image.new('RGB',(540,960),(30,60,90)).save(image)
    gate = tmp_path/'gate.py'; gate.write_text('validation version 1')
    timeline = tmp_path/'timeline.json'
    data = dict(shots=[dict(id='one',sample_start=0,sample_end=m['samples'],image_path=str(image))],
                subtitles=[dict(unit_id=u['unit_id'],sample_start=u['sample_start'],sample_end=u['sample_end'],
                                text=u['text'],text_start=0,text_end=len(u['text'])) for u in m['alignment']],
                inputs={str(gate):file_sha256(gate)})
    atomic_json(timeline,data)
    first_path = P.render(manifest,timeline,tmp_path/'renders')
    first = read(first_path)
    gate.write_text('validation version 2')
    data['inputs'][str(gate)] = file_sha256(gate); atomic_json(timeline,data)
    assert not artifacts_current(first['inputs'])
    monkeypatch.setattr(P,'_encode_v2_segment',lambda *a,**k:pytest.fail('maintenance must not re-encode pixels'))
    second_path = P.render(manifest,timeline,tmp_path/'renders')
    second = read(second_path)
    assert second_path != first_path and artifacts_current(second['inputs'])
    assert second['approved'] is False and second['expected_frames'] == first['expected_frames']
    assert second['samples'] == first['samples']
    assert all(s['reused'] and s.get('reused_from') for s in second['segments'])
    assert Path(first['candidate_path']).is_file()
    # A cache hit may not bypass validation of the new timeline.
    data['subtitles'][0]['text'] = 'wrong source text'; atomic_json(timeline,data)
    with pytest.raises(ValueError,match='differs from source'):
        P.render(manifest,timeline,tmp_path/'renders')


def test_one_decode_witness_cannot_be_used_by_a_later_run(tmp_path,monkeypatch):
    from app.services import media_integrity as media
    from app.services.provenance import file_sha256
    path=tmp_path/'fixture.bin';path.write_bytes(b'opaque test bytes')
    contract=media.verification_contract(2,1920,24000)
    receipt=dict(candidate_path=str(path),expected_frames=2,samples=1920,sample_rate=24000,candidate_sha256=file_sha256(path))
    verified=dict(path=str(path),sha256=receipt['candidate_sha256'],status='passed',contract=contract)
    receipt['integrity']=verified
    calls=[]
    monkeypatch.setattr(media,'verify_media',lambda *a,**kw:calls.append(a))
    assert media.verify_for_run(receipt,dict(verified))=='full_decode_in_this_run' and not calls
    assert media.verify_for_run(receipt)=='independent_full_decode' and len(calls)==1
    receipt['samples']+=1
    assert media.verify_for_run(receipt,verified)=='independent_full_decode' and len(calls)==2


def test_highlight_change_invalidates_only_its_own_segment(tmp_path):
    from app.services.karaoke import Index,_Cue
    pic=tmp_path/'plate';inputs={str(pic.resolve()):'hash'}
    specs=[(dict(sample_start=0,sample_end=48000,image_path=str(pic)),MotionSpec())]
    index=Index([_Cue(0,24000,[(10,20,0,1)]),_Cue(24000,48000,[(24010,24020,0,1)])])
    def keys():return [cache.segment_fingerprint([(0,i,i+25)],specs,[],24000,inputs,{},karaoke=index) for i in (0,25)]
    first=keys();index.cues[1].words[0]=(24010,24021,0,1)
    assert keys()[0]==first[0] and keys()[1]!=first[1]
