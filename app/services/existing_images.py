"""Explicit reuse of historical images, without inventing generation receipts."""
from pathlib import Path
from PIL import Image
from app import paths
from app.services.narration_pipeline import read
from app.services.provenance import atomic_json,file_sha256,fingerprint


def source_binding(eid):
    directory=paths.episode_dir(eid)
    return {str(p.resolve()):file_sha256(p) for p in [directory/'script.md',directory/'scene_plan.json',directory/'image_prompts.json']}


def register(eid):
    """Use the exact existing scene references. This is reuse, not content approval."""
    directory=paths.episode_dir(eid);scene=read(directory/'scene_plan.json')
    used={s['image'] for s in scene.get('scenes',scene.get('unit_scenes',[]))};records={}
    if not used:raise ValueError('no explicit scenes')
    for key in sorted(used):
        paths.validate_episode_id(key)
        spec=scene.get('images',{}).get(key,{})
        if spec.get('path'):
            path=Path(spec['path']);path=path if path.is_absolute() else paths.ROOT/path
        elif spec.get('reuse_from'):
            path=paths.episode_dir(spec['reuse_from'])/'shots'/(key+'.png')
        else:path=directory/'shots'/(key+'.png')
        path=path.resolve()
        if not path.is_relative_to(paths.ROOT.resolve()):raise ValueError('existing image must be inside this project')
        # ⚠️ 2026-09-24：本函数只负责「绑定**已存在的**历史图片」（见 docstring），
        #    待生成的新图不归它管。此前对每个 key 都硬 open()，导致
        #    「部分复用 + 部分新生成」的混合集**死锁**：
        #      生成新图要先过 admission → admission 要 include_registered 生效
        #      → 要有 existing-images.json → register 要求新图已存在 → 循环。
        #    故：本集路径下尚不存在的图 = 待生成，跳过（留给 build_image_jobs）。
        #    复用图（reuse_from）仍然必须存在 —— 不存在说明配置写错了，照旧报错。
        if not path.exists():
            if spec.get('reuse_from'):
                raise ValueError(f'reuse source missing for {key}: {path}')
            continue
        with Image.open(path) as im:
            im.load();size=im.size
            if min(size)<256:raise ValueError('existing image is too small')
        records[key]=dict(path=str(path),sha256=file_sha256(path),size=list(size))
    target=directory/'production/existing-images.json'
    atomic_json(target,dict(kind='explicit_existing_assets',sources=source_binding(eid),images=records,
                           generated_now=False,content_approved=False))
    return target


class ExistingImage:
    imported=True
    def __init__(self,key,record,receipt,binding):
        self.id=key;self.record=record;self.receipt_path=receipt;self.binding=binding
    @property
    def out(self):return Path(self.record['path'])
    def request_fingerprint(self):return fingerprint(dict(kind='existing_asset',record=self.record,sources=self.binding))
    def is_done(self):
        if file_sha256(self.out)!=self.record['sha256']:raise ValueError('imported image changed; review and re-register explicitly')
        return True


def include_registered(eid,jobs):
    receipt=paths.episode_dir(eid)/'production/existing-images.json'
    if not receipt.exists():return jobs
    manifest=read(receipt)
    if manifest.get('kind')!='explicit_existing_assets' or manifest.get('sources')!=source_binding(eid):
        raise ValueError('existing image mapping is stale; review and re-register explicitly')
    imported=[]
    for key,record in manifest['images'].items():
        paths.validate_episode_id(key)
        if not Path(record['path']).resolve().is_relative_to(paths.ROOT.resolve()):
            raise ValueError('existing image must be inside this project')
        image=ExistingImage(key,record,receipt,manifest['sources']);image.is_done();imported.append(image)
    return [j for j in jobs if j.id not in manifest['images']]+imported
