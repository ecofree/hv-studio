"""Reusable historical base maps. An episode owns a frozen copy and its own events."""
import hashlib
import io
import json
import math
import re
import uuid
import zipfile

from PIL import Image, ImageOps

from app import paths
from app.services.provenance import fingerprint, replace_file


TANG = dict(name='唐末两京与潼关',period='唐 · 广明元年',source='事件据《资治通鉴》卷二五四，地形与相对位置为示意',
    places=[['长安','.16','.64','都城'],['潼关','.46','.47','黄河沿岸的关隘'],['洛阳','.84','.53','东都']],
    features=[['黄河','河流','.45,.04 .44,.22 .46,.39 .58,.41 .72,.35 .98,.34'],
              ['秦岭','山地','.03,.83 .18,.78 .30,.84 .43,.72 .55,.76']])
EVENTS = [
    ['东都','洛阳','','黄巢军','','黄巢军进入东都。'],
    ['潼关之战','潼关','洛阳','黄巢军','唐军','双方在潼关交战。'],
    ['进入长安','长安','潼关','黄巢军','','黄巢军进入长安。']]


def _coordinate(value):
    try:
        v = float(value)
    except (ValueError,TypeError):
        raise ValueError('地标位置须为 0 至 1 之间的数') from None
    if not math.isfinite(v) or not 0<=v<=1:
        raise ValueError('地标位置超出底图')
    return v


def points(line):
    result=[]
    for pair in line.split():
        coords = pair.replace('，',',').split(',')
        if len(coords)!=2: raise ValueError('每个地形节点须写成 x,y，各节点以空格分开')
        result.append(tuple(map(_coordinate,coords)))
    if not 2<=len(result)<=80: raise ValueError('每条地形线需要 2–80 个节点')
    return result


def validate(atlas,events=None):
    if not isinstance(atlas,dict) or not all(isinstance(atlas.get(key),str) and atlas[key].strip()
                                             for key in ('name','period','source')):
        raise ValueError('请填写底图名称、年代和地理资料出处')
    if not isinstance(atlas.get('places'),list) or not 1<=len(atlas['places'])<=100:
        raise ValueError('底图需要 1–100 个地标')
    names=set()
    for row in atlas['places']:
        if not isinstance(row,list) or len(row)!=4 or not all(isinstance(v,str) for v in row):
            raise ValueError('地标列为地名、横向位置、纵向位置、地理说明')
        name,x,y,_=row
        if not name.strip() or name in names: raise ValueError('地名须填写且不能重复')
        _coordinate(x)
        _coordinate(y)
        names.add(name)
    if 'geography' in atlas:
        from app.services.atlas_terrain import map_sites, tile_keys
        geo = atlas['geography']
        if not isinstance(geo,dict) or geo.get('projection')!='EPSG:3857':
            raise ValueError('地图投影与高程底图不匹配')
        if geo.get('tile_count')!=len(tile_keys(geo.get('bounds'),geo.get('zoom'))):
            raise ValueError('高程瓦片与地图范围记录不一致')
        if type(geo.get('river_lines')) is not int or geo['river_lines']<0:
            raise ValueError('河网记录无效')
        for k in ('terrain_sha256','rivers_sha256','raster_sha256'):
            if not isinstance(geo.get(k),str) or not re.fullmatch(r'[0-9a-f]{64}',geo[k]):
                raise ValueError('地图数据指纹无效')
        if map_sites(atlas.get('sites'),geo['bounds'])!=atlas['places']:
            raise ValueError('经纬度地标与底图标记不一致')
        names = {row[0] for row in atlas['sites'] if row[3] not in ('山地','河流')}
    features=atlas.get('features',[])
    if not isinstance(features,list) or len(features)>40: raise ValueError('地形线格式无效')
    for f in features:
        if not isinstance(f,list) or len(f)!=3 or not all(isinstance(v,str) for v in f) or f[1] not in ('河流','山地'):
            raise ValueError('地形线请填写名称、河流或山地、节点')
        points(f[2])
    if events is not None and (not isinstance(events,list) or not 1<=len(events)<=48):
        raise ValueError('每个战役模板需要 1–48 个阶段')
    for row in events or []:
        if not isinstance(row,list) or len(row) not in (6,8) or not all(isinstance(v,str) for v in row):
            raise ValueError('事件依次填写阶段、地点、来处、行动方、守方、事件，可另加时间与简短史料出处')
        title,where,origin,actor,defender,event = row[:6]
        if where not in names or (origin and origin not in names):
            raise ValueError('事件引用了底图里没有的地点')
        if not title.strip() or not event.strip(): raise ValueError('请填写阶段与事件说明')
        if actor and actor==defender: raise ValueError('行动方与守方相同，请核对本阶段记载')
        if (len(row)==8 and not (row[6].strip() and row[7].strip())) or ('geography' in atlas and len(row)!=8):
            raise ValueError(f'请填写阶段「{title}」的时间与简短史料出处')


def verify_media(atlas,media):
    if 'geography' in atlas:
        from app.services.atlas_terrain import raster_identity
        if media is None or raster_identity(media)!=atlas['geography']['raster_sha256']:
            raise ValueError('底图图片与经纬度范围的校验记录不一致，请重新导入或生成')


def _story(atlas,story):
    if story is None:
        validate(atlas)
        return None
    if not isinstance(story,dict) or not all(isinstance(story.get(k),str) and story[k].strip() for k in ('title','source')):
        raise ValueError('地图阶段模板需要标题及事件出处')
    rows=story.get('rows')
    if not rows: raise ValueError('请填写要共享的事件阶段')
    validate(atlas,rows)
    return dict(title=story['title'],source=story['source'],rows=rows)


def bundle(atlas,media,story=None):
    story=_story(atlas,story)
    verify_media(atlas,media)
    # A package can travel between episodes. Omit episode IDs, timings and approvals.
    base={k:atlas[k] for k in ('name','period','source','places','features','geography','sites') if k in atlas}
    expanded='geography' in base or (story and any(len(row)==8 for row in story['rows']))
    record=json.dumps(dict(version=2 if expanded else 1,atlas=base,story=story),ensure_ascii=False).encode()
    if len(record)>256_000: raise ValueError('地图文字资料过大，请分成多个模板')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('atlas.json',record)
        if media is not None:
            with io.BytesIO() as buf:
                media.save(buf,format='PNG')
                if buf.tell()>50_000_000: raise ValueError('地图底图文件过大')
                archive.writestr('base.png',buf.getvalue())
    return out.getvalue()


def unpack(data):
    from app.services.studio_graphics import open_upload
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=sorted(z.namelist())
            if names not in (['atlas.json'],['atlas.json','base.png']) or z.getinfo('atlas.json').file_size>256_000:
                raise ValueError('请选择由工作台导出的历史底图包')
            record=json.loads(z.read('atlas.json'))
            if record.get('version') not in (1,2): raise ValueError('未知的历史底图版本')
            atlas=record.get('atlas')
            story=_story(atlas,record.get('story'))
            if 'base.png' not in names:
                verify_media(atlas,None)
                return atlas,None,story
            if z.getinfo('base.png').file_size>50_000_000: raise ValueError('地图底图文件过大')
            raw=z.read('base.png')
            with open_upload(raw) as media: verify_media(atlas,media)
            return atlas,raw,story
    except (zipfile.BadZipFile,KeyError,UnicodeError,AttributeError,RuntimeError) as exc:
        raise ValueError('历史底图包内容不完整') from exc


def templates():
    root=paths.state_dir()/'historical-atlases'
    return sorted((p for p in root.glob('*/*.zip') if re.fullmatch(r'[a-f0-9]{64}',p.stem)),
                  key=lambda p:(p.parent.name,p.name))


def keep(atlas,media,story=None):
    data=bundle(atlas,media,story)
    restored,raw,story=unpack(data)
    identity=fingerprint([restored,hashlib.sha256(raw).hexdigest() if raw else None,story])
    name=re.sub(r'[^\w-]+','-',atlas['name']).strip('-')[:52] or 'atlas'
    directory=paths.contained_path(paths.state_dir()/'historical-atlases','atlas-'+name)
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/(identity+'.zip')
    if target.is_file(): return target
    temp=directory/(uuid.uuid4().hex+'.tmp')
    try:
        temp.write_bytes(data)
        replace_file(temp,target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def draw(image,d,s,step,box,wide,media):
    from app.services.compose import _get_font
    from app.services.information_design import ACCENT,TEAL,TEXT,PANEL,MUTED,LINE,block
    x,y,r,b=box
    right,bottom = (x+round((r-x)*.66),b) if wide else (r,y+round((b-y)*.55))
    region=(x,y,right,bottom)
    d.rounded_rectangle(region,12,fill='#322f28',outline=LINE,width=2)
    if media is None:
        mx,my=x+36,y+35
        mw,mh=right-x-72,bottom-y-70
    else:
        with ImageOps.contain(media,(right-x-4,bottom-y-4),method=Image.Resampling.LANCZOS) as tile:
            mw,mh=tile.size
            mx,my=(x+right-mw)//2,(y+bottom-mh)//2
            image.paste(tile,(mx,my))
    def xy(a,b):
        return mx+round(float(a)*(mw-1)),my+round(float(b)*(mh-1))
    atlas=s['atlas']
    geography=atlas.get('geography')
    for name,kind,line in atlas.get('features',[]):
        coords=[xy(*p) for p in points(line)]
        color='#678f9c' if kind=='河流' else '#86795f'
        d.line(coords,fill=color,width=6 if kind=='河流' else 9,joint='curve')
        if name:
            px,py=coords[len(coords)//2]
            d.text((px,py-13),name,font=_get_font(23),fill=color,anchor='mb',stroke_width=1,stroke_fill=PANEL)
    physical = {row[0]:row[3] for row in atlas.get('sites',[]) if row[3] in ('河流','山地')}
    places={name:(xy(a,b0),note) for name,a,b0,note in atlas['places'] if name not in physical}
    for name,a,b0,_ in atlas['places']:
        if name in physical:
            px,py=xy(a,b0)
            font=_get_font(28)
            inset=round(font.getlength(name)/2)+8
            px=max(x+inset,min(px,right-inset))
            py=max(y+20,min(py,bottom-20))
            color='#c2d8d7' if physical[name]=='河流' else '#ddccac'
            d.text((px,py),name,font=font,fill=color,anchor='mm',stroke_width=2,stroke_fill='#263128')
    rows=s['rows'][:step+1]
    for i,row in enumerate(rows):
        _,place,origin,*_=row
        if not origin or place==origin: continue
        start,end=places[origin][0],places[place][0]
        dx,dy=end[0]-start[0],end[1]-start[1]
        length=math.hypot(dx,dy)
        if length<1: continue
        color=ACCENT if i==step else '#a28e6e'
        dx,dy=dx/length,dy/length
        width=7 if i==step else 4
        if geography:
            # A connection between attested places describes direction, not a surveyed route.
            for offset in range(0,math.ceil(length),22):
                stop=min(offset+13,length)
                segment=[(start[0]+dx*k,start[1]+dy*k) for k in (offset,stop)]
                d.line(segment,fill='#382e27',width=width+4)
                d.line(segment,fill=color,width=width)
        else:
            d.line((start,end),fill=color,width=width)
        ax,ay=start[0]+.56*(end[0]-start[0]),start[1]+.56*(end[1]-start[1])
        d.polygon([(ax+12*dx,ay+12*dy),(ax-8*dx-7*dy,ay-8*dy+7*dx),
                   (ax-8*dx+7*dy,ay-8*dy-7*dx)],fill=color)
    title,focus,_,actor,defender,event=rows[-1][:6]
    active=places[focus][0]
    for name,(pos,_) in places.items():
        cx,cy=pos
        d.ellipse((cx-7,cy-7,cx+7,cy+7),fill=TEXT,outline=PANEL,width=2)
    cx,cy=active
    if defender: d.rectangle((cx-15,cy-15,cx+15,cy+15),outline=TEAL,width=4)
    if actor: d.ellipse((cx-9,cy-9,cx+9,cy+9),fill=ACCENT)
    if not actor and not defender: d.ellipse((cx-15,cy-15,cx+15,cy+15),outline=ACCENT,width=4)
    font=_get_font(28)
    occupied=[(px-16,py-16,px+16,py+16) for (px,py),_ in places.values()]
    for name in [focus,*[n for n in places if n!=focus]]:
        (px,py),_=places[name]
        tw=math.ceil(font.getlength(name))+12
        for lx,ly in [(px-tw//2,py+19),(px-tw//2,py-53),(px+20,py-17),(px-20-tw,py-17)]:
            if not (x+3<=lx and lx+tw<=right-3 and y+3<=ly and ly+38<=bottom-3): continue
            if any(lx<c and lx+tw>a and ly<e and ly+38>b0 for a,b0,c,e in occupied): continue
            d.rounded_rectangle((lx,ly,lx+tw,ly+38),5,fill=PANEL)
            d.text((lx+6,ly+4),name,font=font,anchor='lt',fill=ACCENT if name==focus else TEXT)
            occupied.append((lx,ly,lx+tw,ly+38))
            break
    tx,ty=(right+45,y+3) if wide else (x,bottom+20)
    period=rows[-1][6] if len(rows[-1])==8 else atlas['period']
    block(d,(tx,ty,r,ty+51),period,32,color=MUTED,minimum=26)
    block(d,(tx,ty+53,r,ty+122),title+' · '+focus,43,color=TEXT,minimum=30)
    pos=ty+128
    for label,value,color in [('攻方' if defender else '行动方',actor,ACCENT),('守方',defender,TEAL)]:
        if value:
            if label!='守方': d.ellipse((tx+2,pos+9,tx+20,pos+27),fill=color)
            else: d.rectangle((tx+2,pos+9,tx+20,pos+27),outline=color,width=3)
            block(d,(tx+31,pos,r,pos+43),label+'  '+value,29,color=color,minimum=25)
            pos+=45
    detail=places[focus][1]
    block(d,(tx,pos+6,r,b),event+('  '+detail if detail else ''),34,minimum=26)


def credit(spec,step,wide):
    row=spec['rows'][step]
    text=row[7] if len(row)==8 else spec['source']
    if 'geography' in spec['atlas']:
        from app.services.atlas_terrain import CREDITS
        # Include geography credits in the video as well as the portable reference record.
        text+=(' · ' if wide else '\n')+CREDITS+' · 路线示意'
    return text
