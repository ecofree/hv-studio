"""Offline dual-format regressions using generated color plates and a test tone."""
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from app.managers import load
from app.models import Episode
from app.services import overlay, production_v2, subtitle_layout
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.services.spec import VideoFormat, current_video_format, using_video_format

pytestmark = pytest.mark.production


def test_workbench_creates_wide_edition_and_pilot(tmp_episodes):
    from streamlit.testing.v1 import AppTest
    from app.services import studio_workspace

    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app/webui/Studio.py'),
                            default_timeout=30).run()
    app.selectbox(key='new-format').set_value(VideoFormat.LANDSCAPE)
    next(w for w in app.text_input if w.label == '版本名称').set_value('wide-fixture')
    next(w for w in app.text_input if w.label == '作品标题').set_value('测试')
    next(w for w in app.text_area if w.label == '正文').set_value('第一句话。\n\n第二句话。')
    next(b for b in app.button if b.label == '建立创作草稿').click().run()
    assert not app.exception
    assert load('wide-fixture').video_format == VideoFormat.LANDSCAPE
    studio_workspace.create_pilot('wide-fixture', 'wide-pilot', 0, 1)
    assert load('wide-pilot').video_format == VideoFormat.LANDSCAPE
    assert Episode(id='old', title='原版').video_format == VideoFormat.PORTRAIT


def test_plate_requests_and_layout_follow_the_edition(tmp_episodes):
    from app.orchestrator import build_image_jobs
    from app.services import studio_workspace
    from app.services.image import validate_image_params
    from app.services.motion import frame_box

    studio_workspace.create_episode('wide-request', '镜头测试', '城市外的马队穿过沙漠。', video_format='landscape')
    folder = tmp_episodes / 'wide-request'
    atomic_json(folder/'image_prompts.json', {'road': {'en': 'A caravan of horses crosses a broad valley near the city gates at dawn.'}})
    job, = build_image_jobs('wide-request', create_output_dir=False)
    assert job.req.size == '1536x1024' and '16:9' in job.req.prompt
    assert not validate_image_params(job.req.size, job.req.quality, job.req.model)
    assert not job.out.exists()
    card = overlay.Spec(overlay.KIND_TITLE, 0, 1800, title='西出长安', subtitle='穿过大漠', episode='第一集')
    reusable = overlay.Plan([card])
    try:
        for fmt in (VideoFormat.LANDSCAPE, VideoFormat.PORTRAIT):
            with using_video_format(fmt):
                w, h = fmt.size
                (tile, (x, y)), = reusable.layers_at(600)
                assert not overlay.measure(card)['errors']
                text = '长安城外，一支马队正在穿过山谷。'
                assert not subtitle_layout.measure(text)['errors']
                sub, box = subtitle_layout.overlay(text)
                try:
                    assert 0 <= x < x + tile.width <= w and 0 <= y < y + tile.height < box[1]
                    assert 0 < box[0] < box[2] < w and h/2 < box[1] < box[3] < h
                    assert abs(box[0] + box[2] - w) < 12
                finally:
                    sub.close()
                if fmt is VideoFormat.LANDSCAPE:
                    assert frame_box(1536, 1024, 1, .5, .5, 'fit_cover_v2') == (0, 80, 1536, 944)
    finally:
        reusable.close()
    assert current_video_format() is VideoFormat.PORTRAIT
    with pytest.raises(ValueError, match='画幅'):
        production_v2.check_overlay_receipt('wide-request', {'video_format': 'landscape'}, {})


@pytest.mark.NEEDS_FFMPEG
def test_two_formats_build_decode_and_resume_from_same_sources(tmp_path, tmp_episodes, monkeypatch):
    from app.services import production_timeline
    from app.services.media_integrity import verify_for_run
    from test_production_v2 import assembly

    manifest, *_ = assembly(tmp_path, samples=24000)
    m = read(manifest)
    cues = [dict(unit_id=u['unit_id'], sample_start=u['sample_start'], sample_end=u['sample_end'],
                 text=u['text'], text_start=0, text_end=len(u['text'])) for u in m['alignment']]
    evidence = tmp_path/'alignment.json'
    atomic_json(evidence, dict(method='acoustic_source_alignment_v1', status='machine_validated',
                manifest_content_sha256=fingerprint(m), inputs={str(manifest): file_sha256(manifest)},
                subtitles=cues, words={row['unit_id']: [row] for row in cues}))
    scene = tmp_path/'scene.json'
    keys = ['wide', 'vertical']
    atomic_json(scene, {'unit_scenes': [dict(unit_id=u['unit_id'], image=key)
                                        for u, key in zip(m['alignment'], keys)]})
    assets = {}
    for key, size in zip(keys, [(384, 256), (90, 160)]):
        image_path = tmp_path/(key+'.png')
        with Image.new('RGB', size, (32, 72, 110)) as im:
            draw = ImageDraw.Draw(im)
            draw.rectangle((0, 0, im.width-1, 15), fill='red')
            draw.rectangle((0, im.height-16, im.width-1, im.height-1), fill='lime')
            im.save(image_path)
        assets[key] = str(image_path)
    monkeypatch.setattr('app.services.bgm.pick_bgm', lambda profile: None)
    renders = tmp_path/'renders'
    results = []
    for fmt in (VideoFormat.PORTRAIT, VideoFormat.LANDSCAPE):
        ep = Episode(id='test-'+fmt.value, title='测试', video_format=fmt)
        timeline = production_timeline.build(ep, manifest, evidence, assets, scene, tmp_path/'timelines')
        assert VideoFormat.from_record(read(timeline)) is fmt
        receipt = production_v2.render(manifest, timeline, renders)
        result = read(receipt)
        assert verify_for_run(result, result['integrity']) == 'full_decode_in_this_run'
        streams = {s['codec_type']: s for s in result['integrity']['streams']}
        v = streams['video']
        assert (v['width'], v['height']) == fmt.size and v['avg_frame_rate'] == '25/1'
        assert int(v['nb_read_packets']) == 50 and v['codec_name'] == 'h264' and v['pix_fmt'] == 'yuv420p'
        assert streams['audio']['codec_name'] == 'aac' and not result['approved']
        assert int(streams['audio']['sample_rate']) == (48000 if fmt is VideoFormat.LANDSCAPE else 24000)
        from app.services.production_revision import probe_existing_media
        assert probe_existing_media(result)['frames'] == 50
        assert production_v2.render(manifest, timeline, renders) == receipt
        results.append(result)
    assert results[0]['candidate_sha256'] != results[1]['candidate_sha256']
    assert not any(s.get('reused_from') for s in results[1]['segments'])
    assert results[0]['segments'][0]['input_fingerprint'] != results[1]['segments'][0]['input_fingerprint']

    # Sample the actual encoded inset, well inside each colored stripe and outside the subtitles.
    import subprocess
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '1.5', '-i', results[1]['candidate_path'],
        '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
    with Image.frombytes('RGB', VideoFormat.LANDSCAPE.size, raw) as decoded:
        assert decoded.getpixel((800, 20))[0] > 220
        assert decoded.getpixel((800, 1060))[1] > 220
        assert decoded.getpixel((10, 20))[0] < 150
