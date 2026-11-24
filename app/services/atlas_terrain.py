"""Build a georeferenced, local backdrop from Terrarium elevation and WGS84 river lines."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
import re
import zipfile

import numpy as np
from PIL import Image, ImageDraw


TERRAIN_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium'
RIVERS_URL = 'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_rivers_lake_centerlines.geojson'
CREDITS = '今地形 Mapzen／USGS · 现代河网 Natural Earth'
SITE_TYPES = ('城址', '关隘', '地点', '河流', '山地')
TILE_LIMIT = 36
TILE_BYTES = 512_000
RIVER_BYTES = 20_000_000
_TILE_NAME = re.compile(r'(?:terrarium/)?(0|[1-9]\d?)/(0|[1-9]\d{0,5})/(0|[1-9]\d{0,5})\.png')


def parse_bounds(values):
    try:
        west,south,east,north = map(float, values)
    except (TypeError, ValueError):
        raise ValueError('范围依次填写西经度、南纬度、东经度、北纬度') from None
    if not (-180<=west<east<=180 and -85.05112878<=south<north<=85.05112878):
        raise ValueError('地图经纬度范围无效，跨日期变更线的区域请拆开')
    return [west,south,east,north]


def _mercator(lon,lat):
    y = (1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2
    return ((lon+180)/360,max(0,min(1,y)))


def _world_box(bounds):
    west,south,east,north = parse_bounds(bounds)
    left,top = _mercator(west,north)
    right,bottom = _mercator(east,south)
    if left>=right or top>=bottom:
        raise ValueError('地图范围太窄，无法定位图层')
    return left,top,right,bottom


def tile_keys(bounds,zoom):
    if type(zoom) is not int or not 1<=zoom<=14:
        raise ValueError('请选择缩放级别在 1–14 之间的高程瓦片')
    scale = 2**zoom
    left,top,right,bottom = (c*scale for c in _world_box(bounds))
    x0,x1 = math.floor(left),math.ceil(right)
    y0,y1 = max(0,math.floor(top)),min(scale,math.ceil(bottom))
    if (x1-x0)*(y1-y0)>TILE_LIMIT:
        raise ValueError(f'此范围在该级别超过 {TILE_LIMIT} 张瓦片，请降低级别或缩小范围')
    return [(zoom,x,y) for x in range(x0,x1) for y in range(y0,y1)]


def _tiles(data,bounds):
    if not data or len(data)>TILE_LIMIT*TILE_BYTES:
        raise ValueError('高程瓦片 ZIP 缺失或过大')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = {}
            for item in archive.infolist():
                if item.is_dir():
                    continue
                name = _TILE_NAME.fullmatch(item.filename)
                if not name or not 0<item.file_size<=TILE_BYTES or item.flag_bits & 1:
                    raise ValueError('瓦片 ZIP 仅接受 z/x/y.png 或 terrarium/z/x/y.png')
                key = tuple(map(int,name.groups()))
                if key in members or len(members)>=TILE_LIMIT:
                    raise ValueError('瓦片重复或太多，请只打包当前地图范围')
                members[key] = item
            zooms = {k[0] for k in members}
            if len(zooms)!=1:
                raise ValueError('请提供同一缩放级别的 Terrarium 高程瓦片')
            keys = tile_keys(bounds,next(iter(zooms)))
            missing = ['%d/%d/%d.png'%k for k in keys if k not in members]
            if missing:
                raise ValueError('高程数据缺少 '+ '、'.join(missing[:6]))
            return {key:archive.read(members[key]) for key in keys}
    except (zipfile.BadZipFile,NotImplementedError,RuntimeError) as exc:
        raise ValueError('高程瓦片 ZIP 无法读取') from exc


def fetch_public(bounds,zoom):
    """Explicitly requested public data only. Respect the runtime's HTTP proxy and timeouts."""
    import requests

    def fetch(url,limit):
        try:
            with requests.get(url,stream=True,timeout=(5,10)) as response:
                response.raise_for_status()
                data = bytearray()
                for piece in response.iter_content(64*1024):
                    data.extend(piece)
                    if len(data)>limit:
                        raise ValueError('公共地图数据体积超限，可以上传裁好的数据文件')
                return bytes(data)
        except requests.RequestException as exc:
            raise ValueError('无法下载公共地图数据，请检查网络，或使用本地瓦片 ZIP 和河网 GeoJSON') from exc

    keys = tile_keys(bounds,zoom)
    urls = [f'{TERRAIN_URL}/%d/%d/%d.png'%k for k in keys]
    with ThreadPoolExecutor(max_workers=6) as workers:
        streams = workers.map(fetch,[RIVERS_URL,*urls],[RIVER_BYTES,*[TILE_BYTES]*len(keys)])
        rivers,*tiles = streams
    with io.BytesIO() as output:
        with zipfile.ZipFile(output,'w') as archive:
            for key,tile in zip(keys,tiles,strict=True):
                archive.writestr('%d/%d/%d.png'%key,tile)
        return output.getvalue(),rivers


def map_sites(rows,bounds):
    """Map the editor's signed decimal degrees to locations inside the rendered plate."""
    left,top,right,bottom = _world_box(bounds)
    west,south,east,north = parse_bounds(bounds)
    if not isinstance(rows,list) or not 1<=len(rows)<=100:
        raise ValueError('经纬度地标需要 1–100 行')
    places = []
    for row in rows:
        if not isinstance(row,list) or len(row)!=7 or not all(isinstance(c,str) for c in row):
            raise ValueError('地标依次填写名称、经度、纬度、类型、年代、出处、说明')
        name,lon,lat,kind,period,source,note = row
        if kind not in SITE_TYPES or not (name.strip() and period.strip() and source.strip()):
            raise ValueError('地标须有名称、年代、出处及类别（城址、关隘、地点、河流或山地）')
        try:
            lon,lat = float(lon),float(lat)
        except ValueError:
            raise ValueError(f'「{name}」请填写十进制经纬度') from None
        if not (west<=lon<=east and south<=lat<=north):
            raise ValueError(f'「{name}」位于地图范围外，请核对经纬度或重选范围')
        x,y = _mercator(lon,lat)
        places.append([name,f'{(x-left)/(right-left):.8f}',f'{(y-top)/(bottom-top):.8f}',note])
    return places


def _elevation(tiles,box,size):
    zoom = next(iter(tiles))[0]
    x0,y0 = min(k[1] for k in tiles),min(k[2] for k in tiles)
    x1,y1 = max(k[1] for k in tiles),max(k[2] for k in tiles)
    mosaic = np.empty(((y1-y0+1)*256,(x1-x0+1)*256),dtype=np.float32)
    for (_,x,y),data in tiles.items():
        try:
            with Image.open(io.BytesIO(data)) as tile:
                if tile.format!='PNG' or tile.size!=(256,256):
                    raise ValueError('此入口需要 256×256 的 Terrarium PNG')
                with tile.convert('RGB') as rgb_tile:
                    rgb = np.asarray(rgb_tile,dtype=np.float32)
        except OSError as exc:
            raise ValueError(f'高程瓦片 {zoom}/{x}/{y}.png 无法解码') from exc
        heights = rgb[:,:,0]*256 + rgb[:,:,1] + rgb[:,:,2]/256 - 32768
        row,column = (y-y0)*256,(x-x0)*256
        mosaic[row:row+256,column:column+256] = heights
    if float(mosaic.min())<-12000 or float(mosaic.max())>10000:
        raise ValueError('瓦片高程超出范围，请确认上传了 Terrarium 编码的高程图')
    left,top,right,bottom = (value*(2**zoom)*256 for value in box)
    crop = (left-x0*256,top-y0*256,right-x0*256,bottom-y0*256)
    with Image.fromarray(mosaic) as source, source.resize(size,Image.Resampling.BICUBIC,box=crop) as raster:
        return np.array(raster)


def _water(data,bounds):
    if not data or len(data)>RIVER_BYTES:
        raise ValueError('请提供 20 MB 以内的 WGS84 河网 GeoJSON')
    record = json.loads(data)
    features = record.get('features') if isinstance(record,dict) else None
    if not isinstance(features,list) or record.get('type')!='FeatureCollection' or len(features)>40_000:
        raise ValueError('河网需要使用 GeoJSON FeatureCollection')
    west,south,east,north = bounds
    for feature in features:
        if not isinstance(feature,dict):
            raise ValueError('河网中存在无效的 GeoJSON 图层')
        shape = feature.get('geometry') or {}
        if not isinstance(shape,dict) or shape.get('type') not in ('LineString','MultiLineString'):
            continue
        lines = shape.get('coordinates')
        if shape['type']=='LineString':
            lines = [lines]
        if not isinstance(lines,list):
            raise ValueError('河网坐标字段无效')
        for line in lines:
            try:
                xy = np.asarray(line,dtype=float)
                if len(xy)==0:
                    continue
                if xy.ndim!=2 or xy.shape[1]<2 or len(xy)>100_000:
                    raise ValueError
                xy = xy[:,:2]
                lower,upper = np.min(xy,axis=0),np.max(xy,axis=0)
                if not (np.isfinite(xy).all() and np.all(lower>=(-180,-90)) and np.all(upper<=(180,90))):
                    raise ValueError
            except (TypeError,ValueError):
                raise ValueError('河网坐标超出 WGS84 经度、纬度范围') from None
            if len(xy)<2 or upper[0]<west or lower[0]>east or upper[1]<south or lower[1]>north:
                continue
            meta = feature.get('properties') or {}
            rank = meta.get('scalerank',9) if isinstance(meta,dict) else 9
            for part in np.split(xy,np.flatnonzero(np.abs(np.diff(xy[:,0]))>180)+1):
                if len(part)>=2:
                    yield part, isinstance(rank,(float,int)) and rank<=5


def _relief(elevation,metres_per_pixel):
    dy,dx = np.gradient(np.maximum(elevation,0))
    dy *= 4.8/metres_per_pixel
    dx *= 4.8/metres_per_pixel
    light = np.clip((.46*dx+.49*dy+.74)/np.sqrt(1+dx*dx+dy*dy),0,1)
    bands = [0,300,520,850,1350,2200,4500]
    tint = [(44,53,49),(53,63,51),(65,72,55),(77,77,58),(93,86,65),(126,108,78),(161,140,106)]
    colour = np.stack([np.interp(elevation,bands,values) for values in zip(*tint)],axis=2)
    return Image.fromarray(np.uint8(np.clip(colour*(.47+.80*light[:,:,None])+3,0,255)))


def raster_identity(image):
    with image.convert('RGB') as pixels:
        return hashlib.sha256(f'{image.width}x{image.height}:'.encode()+pixels.tobytes()).hexdigest()


def build(bounds,terrain,rivers):
    """Return one PNG and a portable receipt. Video rendering never fetches data."""
    from app.services.compose import _get_font

    bounds = parse_bounds(bounds)
    box = _world_box(bounds)
    aspect = (box[2]-box[0])/(box[3]-box[1])
    if not .25<=aspect<=4:
        raise ValueError('底图太狭长，请选择宽高比在 1:4 到 4:1 之间的范围')
    width,height = (1728,round(1728/aspect)) if aspect>=1 else (round(1728*aspect),1728)
    west,south,east,north = bounds
    metres_per_pixel = (east-west)*111_319.5*math.cos(math.radians((south+north)/2))/width
    tiles = _tiles(terrain,bounds)
    zoom = next(iter(tiles))[0]
    if min(box[2]-box[0],box[3]-box[1])*(2**zoom)*256<24:
        raise ValueError('高程瓦片精度不足，请提高缩放级别或扩大地图范围')
    elevation = _elevation(tiles,box,(width,height))
    with _relief(elevation,metres_per_pixel) as image:
        draw = ImageDraw.Draw(image)
        left,top,right,bottom = box
        count = 0
        for line,major in _water(rivers,bounds):
            count += 1
            xy = []
            for lon,lat in line:
                x,y = _mercator(lon,max(-85.05112878,min(85.05112878,lat)))
                xy.append(((x-left)/(right-left)*(width-1),(y-top)/(bottom-top)*(height-1)))
            draw.line(xy,fill='#293d40',width=10 if major else 5,joint='curve')
            draw.line(xy,fill='#83a9ad' if major else '#587879',width=5 if major else 2,joint='curve')
        x,y = width-48,42
        draw.text((x,y),'北',anchor='mm',font=_get_font(32),fill='#e7ddc4',stroke_width=1,stroke_fill='#263128')
        draw.polygon([(x,y+21),(x-10,y+49),(x,y+42),(x+10,y+49)],fill='#e7ddc4')
        with io.BytesIO() as buf:
            image.save(buf,format='PNG')
            png = buf.getvalue()
        identity = raster_identity(image)
    return png,dict(bounds=bounds,projection='EPSG:3857',zoom=zoom,tile_count=len(tiles),
                    river_lines=count,terrain_sha256=hashlib.sha256(terrain).hexdigest(),
                    rivers_sha256=hashlib.sha256(rivers).hexdigest(),raster_sha256=identity)
