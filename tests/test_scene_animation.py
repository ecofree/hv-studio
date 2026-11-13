"""Animation changes pixels only in authored regions and never resets at cuts."""
import copy
import subprocess

import numpy as np
import pytest
from PIL import Image

from app.services import scene_animation as A, production_v2 as P, video_segment_cache as C
from app.services.provenance import file_sha256
from app.models.shot import MotionSpec
from conftest import NEEDS_FFMPEG

pytestmark = pytest.mark.production


def fixture_scene(tmp_path):
    pixels = np.indices((160, 90)).sum(axis=0) % 2 * 100 + 40
    image = Image.fromarray(np.repeat(pixels[:, :, None], 3, axis=2).astype('uint8'))
    path = tmp_path/'plate.png'; image.save(path)
    plan = dict(version=1, source_sha256=file_sha256(path), seed=8,
                protected=[[[.4,.4],[.6,.4],[.6,.6],[.4,.6]]],
                layers=[dict(kind='parallax', polygon=[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],
                             feather=.01, travel=[.02,0]),
                        dict(kind='rain', polygon=[[.2,.2],[.8,.2],[.8,.8],[.2,.8]],
                             feather=.01, density=40, opacity=.25, speed=.4)])
    return image, path, plan


def test_masks_protect_original_and_temporal_phase_is_random_access(tmp_path):
    image, path, plan = fixture_scene(tmp_path)
    scene = A.Scene(image, plan, 150, 275)
    before = np.asarray(image)
    a = np.asarray(scene.frame(230)); b = np.asarray(scene.frame(250))
    assert np.array_equal(a[65:95,37:53], before[65:95,37:53])
    assert np.array_equal(a[:10], before[:10])
    assert not np.array_equal(a, b)
    assert np.array_equal(a, np.asarray(A.Scene(image, plan,150,275).frame(230)))
    assert np.array_equal(a, np.asarray(scene.frame(230)))
    with pytest.raises(ValueError): scene.frame(275)


@pytest.mark.parametrize('fault', ['hash','nan','overscan','unknown','degenerate','seed','empty'])
def test_invalid_scene_fails_closed(tmp_path, fault):
    _, path, plan = fixture_scene(tmp_path)
    if fault == 'hash': plan['source_sha256']='0'*64
    if fault == 'nan': plan['layers'][0]['travel'][0]=float('nan')
    if fault == 'overscan': plan['layers'][0]['polygon'][0][0]=0
    if fault == 'unknown': plan['layers'][0]['type']='magic'
    if fault == 'degenerate': plan['layers'][0]['polygon']=[[.1,.1]]*3
    if fault == 'seed': plan['seed']=True
    if fault == 'empty': plan['layers']=[]
    with pytest.raises(ValueError): A.validate(plan,file_sha256(path))


def test_animation_parameters_invalidate_only_affected_segment(tmp_path):
    _, path, plan = fixture_scene(tmp_path)
    shot=dict(sample_start=0,sample_end=48000,image_path=str(path),animation=plan)
    specs=[(shot,MotionSpec()),(dict(shot, animation=None),MotionSpec())]
    inputs={str(path.resolve()):file_sha256(path)}
    def keys():
        return [C.segment_fingerprint([(i,0,50)],specs,[],24000,inputs,{}) for i in range(2)]
    before=keys();plan['seed']+=1
    after=keys()
    assert before[0]!=after[0] and before[1]==after[1]
    assert 'app/services/scene_animation.py' in C.DEPENDENCIES


@NEEDS_FFMPEG
@pytest.mark.NEEDS_FFMPEG
def test_static_camera_animation_does_not_freeze_and_split_is_identical(tmp_path):
    image,path,plan=fixture_scene(tmp_path)
    shot=dict(sample_start=0,sample_end=9600,image_path=str(path),animation=plan)
    specs=[(shot,MotionSpec(type='static',intensity=0,start_zoom=1,end_zoom=1))]
    codec=['-c:v','libx264','-preset','ultrafast','-crf','0']
    whole=tmp_path/'whole.mp4'; tail=tmp_path/'tail.mp4'
    P._encode_v2_segment([(0,0,10)],specs,[],24000,whole,codec,30)
    P._encode_v2_segment([(0,5,10)],specs,[],24000,tail,codec,30)
    def decode(p):
        return subprocess.run(['ffmpeg','-v','error','-i',str(p),'-f','rawvideo','-pix_fmt','rgb24','-'],
                              capture_output=True,check=True,timeout=30).stdout
    a,b=decode(whole),decode(tail); stride=1080*1920*3
    assert a[5*stride:]==b
    assert a[:stride]!=a[stride:2*stride]
    # The inactive renderer must remain byte-identical at the raw pixel level.
    specs[0][0].pop('animation')
    plain=tmp_path/'plain.mp4'; P._encode_v2_segment([(0,0,10)],specs,[],24000,plain,codec,30)
    raw=decode(plain)
    assert raw[:stride]==raw[stride:2*stride]


def test_direction_preserves_and_binds_animation(tmp_path,monkeypatch):
    from app.services import directing as D
    from app.services.provenance import atomic_json
    image,path,plan=fixture_scene(tmp_path)
    manifest={'samples':48000,'sample_rate':24000,'alignment':[{'unit_id':'u','text':'字'}]}
    evidence=tmp_path/'evidence.json';atomic_json(evidence,{'words':{}})
    timeline={'alignment_evidence_path':str(evidence)}
    design={'version':1,'source_identity':D.source_identity(manifest),'visual_events':[
        {'at':{'at':'start'},'image_id':'p','motion':{},'animation':plan}]}
    design_path=tmp_path/'direction.json';atomic_json(design_path,design)
    monkeypatch.setattr('app.services.forced_alignment.require_evidence',lambda m,t:None)
    result=D.apply(manifest,timeline,design_path,{'p':str(path)},tmp_path)
    assert result['shots'][0]['animation']==plan
    path.write_bytes(b'changed plate')
    with pytest.raises(ValueError,match='hash mismatch'):
        D.apply(manifest,timeline,design_path,{'p':str(path)},tmp_path)


def test_shared_clock_keeps_consecutive_shots_continuous(tmp_path):
    image,path,plan=fixture_scene(tmp_path)
    plan['frame_range']=[0,100]
    whole=A.Scene(image,plan,0,100)
    first=A.Scene(image,plan,0,50);second=A.Scene(image,plan,50,100)
    for frame,scene in [(0,first),(49,first),(50,second),(99,second)]:
        assert np.array_equal(np.asarray(whole.frame(frame)),np.asarray(scene.frame(frame)))
    with pytest.raises(ValueError,match='contain'):A.Scene(image,plan,90,110)


def test_authored_pause_stops_parallax_and_environment(tmp_path):
    image,path,plan=fixture_scene(tmp_path)
    plan['layers'][0]['progress']=[[0,0],[.45,1],[1,1]]
    plan['layers'][1]['envelope']=[[0,1],[.45,0],[1,0]]
    scene=A.Scene(image,plan,0,100)
    assert np.array_equal(np.asarray(scene.frame(50)),np.asarray(scene.frame(99)))
    assert not np.array_equal(np.asarray(scene.frame(20)),np.asarray(scene.frame(50)))


@pytest.mark.parametrize('curve',[[[0,0],[0,1],[1,1]],[[.1,0],[1,1]],[[0,0],[1,2]],[[0,.8],[1,.2]]])
def test_invalid_motion_curves_rejected(tmp_path,curve):
    image,path,plan=fixture_scene(tmp_path);plan['layers'][0]['progress']=curve
    with pytest.raises(ValueError):A.validate(plan)


def test_shared_clock_and_pause_invalidate_cached_pixels(tmp_path):
    image,path,plan=fixture_scene(tmp_path)
    shot=dict(sample_start=0,sample_end=48000,image_path=str(path),animation=plan)
    def key():return C.segment_fingerprint([(0,0,50)],[(shot,MotionSpec())],[],24000,{str(path.resolve()):file_sha256(path)},{})
    old=key();plan['frame_range']=[0,100]
    clock=key();assert clock!=old
    plan['layers'][1]['envelope']=[[0,1],[1,0]]
    assert key()!=clock
