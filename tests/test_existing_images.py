"""Imported assets remain distinct from paid generation and content approval."""
import pytest
from PIL import Image
from app import paths
from app.services.existing_images import register,include_registered
from app.services.provenance import atomic_json,file_sha256
from app.services.narration_pipeline import read

pytestmark=pytest.mark.production


def test_cross_episode_reuse_is_bound_without_fake_generation(tmp_path,monkeypatch):
    monkeypatch.setattr(paths,'ROOT',tmp_path)
    source=paths.episode_dir('donor')/'shots/SC01.png';source.parent.mkdir(parents=True)
    Image.new('RGB',(400,700),'red').save(source)
    directory=paths.episode_dir('new');directory.mkdir()
    (directory/'script.md').write_text('原稿',encoding='utf-8')
    atomic_json(directory/'scene_plan.json',dict(images={'SC01':{'reuse_from':'donor'}},unit_scenes=[dict(image='SC01',unit_id='one')]))
    atomic_json(directory/'image_prompts.json',{})
    before=file_sha256(source);receipt=register('new');jobs=include_registered('new',[])
    assert len(jobs)==1 and jobs[0].is_done() and jobs[0].out==source
    record=read(receipt)
    assert record['generated_now'] is False and record['content_approved'] is False
    assert file_sha256(source)==before and not source.with_suffix('.png.request.json').exists()
    Image.new('RGB',(400,700),'blue').save(source)
    with pytest.raises(ValueError,match='changed'):include_registered('new',[])
    (directory/'script.md').write_text('修改原稿',encoding='utf-8')
    with pytest.raises(ValueError,match='stale'):include_registered('new',[])
