"""Author, preview and freeze source-bound information graphics without a provider."""
import csv
import hashlib
import io
import json
import math
import re
import uuid
from contextlib import nullcontext

from PIL import Image, ImageOps

from app import paths
from app.services import studio_workflow as workflow
from app.services.information_design import render_plate
from app.services.provenance import atomic_json, file_sha256, fingerprint, replace_file
from app.services.spec import VideoFormat
from app.services.studio_attraction import locate, source_key


KINDS = {'table':'对照表', 'bars':'条形对比', 'person':'人物简介', 'route':'历史路线'}


def path_for(eid):
    return workflow.folder(eid)/'graphics.json'


def snapshot(eid):
    path = path_for(eid)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, None
    return json.loads(data), hashlib.sha256(data).hexdigest()


def parse_grid(value):
    """Accept spreadsheet paste, CSV and ordinary Markdown tables."""
    value = value.strip('\r\n')
    lines = value.splitlines()
    if not value.strip():
        raise ValueError('请粘贴数据行')
    if '\t' in lines[0]:
        rows = csv.reader(io.StringIO(value),delimiter='\t',strict=True)
    elif lines[0].lstrip().startswith('|') or ('|' in lines[0] and ',' not in lines[0]):
        rows = [[cell.replace('\\|','|') for cell in re.split(r'(?<!\\)\|',line.strip().strip('|'))]
                for line in lines if line.strip()]
        if len(rows)>1 and all(re.fullmatch(r':?-{2,}:?',s.strip()) for s in rows[1]):
            del rows[1]
    else:
        rows = csv.reader(io.StringIO(value),strict=True)
    try:
        return [[s.strip() for s in row] for row in rows if row]
    except csv.Error as exc:
        raise ValueError('表格分隔符或引号不完整') from exc


def _number(value):
    try:
        n = float(value)
    except (ValueError,TypeError):
        raise ValueError(f'数值须单列填写，并使用相同单位：{value}') from None
    if not math.isfinite(n) or n < 0:
        raise ValueError('数值须为有限的非负数')
    return n


def validate_spec(s):
    kind = s['kind']
    if kind not in KINDS or not s['title'].strip() or not s['source'].strip():
        raise ValueError('请填写图文类型、标题及资料出处')
    rows = s['rows']
    if not 1 <= len(rows) <= 48:
        raise ValueError('一则图文可含 1–48 条，请把更长的资料分为多个镜头')
    if kind=='table':
        cols = s['columns']
        if not 2 <= len(cols) <= 5 or not all(c.strip() for c in cols):
            raise ValueError('表格需要 2–5 个具名列，第一列作为竖版每页的标题')
        sizes = {len(cols)}
    else:
        sizes = {'bars':{2,3},'person':{2},'route':{4}}[kind]
    if any(len(r) not in sizes or not r[0].strip() for r in rows):
        raise ValueError('数据列数与所选类型不符，或有行缺少名称')
    if kind=='bars':
        if not s.get('unit','').strip():
            raise ValueError('请填写条形图共同使用的计量单位')
        for row in rows:
            _number(row[1])
    if kind=='route':
        for row in rows:
            if any(_number(v)>1 for v in row[1:3]):
                raise ValueError('地标位置须在 0 到 1 之间')
    if len(s.get('quotes',[])) != len(rows)-1:
        raise ValueError(f'第一条随画面入场。请为后续 {len(rows)-1} 条各填一条旁白原句，每行一条')


def offsets(document):
    result = {}
    cursor = 0
    for u in document['units']:
        result[u['unit_id']] = cursor
        cursor += sum(c.isalnum() for c in u['source_text'])
    return result,cursor


def quote_anchors(s,units):
    def find(quote,scope):
        return locate({'units':scope},quote) if quote else None
    return ([find(s.get('start_quote'),units[:1])]+[find(q,units) for q in s['quotes']],
            find(s.get('stop_before'),units[-1:]))


def intervals(specs, document):
    units = document['units']
    starts,total = offsets(document)
    ids = list(starts)
    spans = []
    for key,s in specs.items():
        validate_spec(s)
        try:
            a,b = ids.index(s['first_unit']),ids.index(s['last_unit'])
        except ValueError:
            raise ValueError('图文的旁白段已改变，请重新选择') from None
        if b<a:
            raise ValueError('结束段必须在开始段之后或为同一段')
        selected = units[a:b+1]
        points,exit_at = quote_anchors(s,selected)
        def position(anchor):
            u = next(u for u in selected if u['unit_id']==anchor['unit_id'])
            return starts[u['unit_id']] + sum(c.isalnum() for c in u['source_text'][:anchor['text_start']])
        stop = position(exit_at) if exit_at else starts[ids[b+1]] if b+1<len(ids) else total
        beats = [position(p) if p else starts[ids[a]] for p in points]
        start = beats[0]
        if beats != sorted(set(beats)) or beats[-1]>=stop:
            raise ValueError('各条原句须依次出现在所选画面内，并早于退出位置')
        spans.append((start,stop,key,beats,points+[exit_at]))
    spans.sort()
    if any(r[0]<l[1] for l,r in zip(spans,spans[1:])):
        raise ValueError('两则图文的旁白范围重叠，请调整入场或退出原句')
    return spans


def media_path(eid, digest, folder='graphics-media'):
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise ValueError('图文素材指纹无效')
    return paths.contained_path(workflow.folder(eid),f'{folder}/{digest}.png')


def _store_png(eid, image, folder):
    directory = paths.contained_path(workflow.folder(eid),folder)
    directory.mkdir(parents=True,exist_ok=True)
    temp = directory/(uuid.uuid4().hex+'.tmp')
    try:
        image.save(temp,format='PNG')
        digest = file_sha256(temp)
        target = media_path(eid,digest,folder)
        if not target.is_file() or file_sha256(target)!=digest:
            replace_file(temp,target)
        return digest
    finally:
        temp.unlink(missing_ok=True)


def open_upload(data):
    with Image.open(io.BytesIO(data)) as raw:
        if raw.format not in {'PNG','JPEG','WEBP'} or raw.width*raw.height>30_000_000:
            raise ValueError('请选择不超过 3000 万像素的 PNG、JPEG 或 WebP 素材')
        oriented = ImageOps.exif_transpose(raw)
        try:
            rgba = oriented.convert('RGBA')
            try:
                result = Image.new('RGB',rgba.size,'#302b27')
                result.paste(rgba,mask=rgba.getchannel('A'))
                return result
            finally:
                rgba.close()
        finally:
            oriented.close()


def open_media(eid, s, upload=None):
    if upload is not None:
        return open_upload(upload)
    if not s.get('media'):
        return nullcontext(None)
    path = media_path(eid,s['media'])
    if file_sha256(path)!=s['media']:
        raise ValueError('头像或地图底图已变化，请重新选用素材')
    return Image.open(path)


def current(eid, document):
    plan,digest = snapshot(eid)
    if plan:
        identity = source_key(document)
        if plan.get('version')!=1 or any(s['bound_to']!=identity for s in plan['graphics'].values()):
            raise ValueError('图文属于另一版旁白，请在制作页重新绑定')
    return plan,digest


def _plates(eid,entry,media,previous):
    from app.services import information_design
    from app.services.compose import _sub_font_path
    fields = ('kind','title','source','rows','columns','unit','detail','media')
    identity = fingerprint([[entry.get(k) for k in fields],file_sha256(information_design.__file__),
                            file_sha256(_sub_font_path())])
    if previous.get('layout_identity')==identity:
        old = previous['plates']
        files = [media_path(eid,sha,'graphics-plates') for values in old.values() for sha in values]
        if all(p.is_file() and file_sha256(p)==p.stem for p in files):
            return identity,old
    plates = {}
    for fmt in VideoFormat:
        images = []
        for step in range(len(entry['rows'])):
            with render_plate(entry,step,fmt,media) as im:
                images.append(_store_png(eid,im,'graphics-plates'))
        plates[fmt.value] = images
    return identity,plates


def save(eid, key, spec, document, expected, *, upload=None):
    """Build both layouts, then atomically replace one binding. Readers see one revision."""
    from app.services.process_lock import file_lock
    key = paths.validate_episode_id(key)
    path = path_for(eid)
    path.parent.mkdir(parents=True,exist_ok=True)
    with file_lock(path.with_suffix('.lock')):
        before,actual = snapshot(eid)
        identity = source_key(document)
        if actual!=expected or identity!=source_key(workflow.document(eid)):
            raise ValueError('旁白或图文已更新，请刷新后再保存')
        entries = dict((before or {}).get('graphics',{}))
        if spec is None:
            entries.pop(key,None)
        else:
            entries[key] = dict(spec)
        if spec is not None:
            entries[key]['bound_to'] = identity
        intervals({k:s for k,s in entries.items() if s['bound_to']==identity},document)
        if spec is not None:
            entry = entries[key]
            with open_media(eid,entry,upload) as media:
                if upload is not None:
                    entry['media'] = _store_png(eid,media,'graphics-media')
                previous = (before or {}).get('graphics',{}).get(key,{})
                entry['layout_identity'],entry['plates'] = _plates(eid,entry,media,previous)
        if source_key(workflow.document(eid))!=identity:
            raise ValueError('保存期间旁白已更新，请重新绑定')
        if before:
            atomic_json(path.parent/'graphics-history'/(actual+'.json'),before)
        if entries:
            atomic_json(path,dict(version=1,graphics=entries))
        else:
            path.unlink(missing_ok=True)
