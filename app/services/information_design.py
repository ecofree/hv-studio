"""Native-resolution editorial plates. All values, dates and labels are typeset."""
import math

from PIL import Image, ImageDraw, ImageOps

from app.services.compose import _get_font
from app.services.spec import VideoFormat


BACKGROUND = '#211e1c'
PANEL = '#302b27'
LINE = '#625343'
TEXT = '#faf0dd'
MUTED = '#d1c0a9'
ACCENT = '#f4ba71'
TEAL = '#88b9ad'
HEADER_END = {VideoFormat.LANDSCAPE: 154, VideoFormat.PORTRAIT: 264}


def paste_heading(frame, tile, at, fmt):
    reserved = HEADER_END[fmt]
    if at[1]+tile.height<=reserved-12:
        frame.paste(tile,at,tile)
        return
    factor = min(1.,(reserved-32)/tile.height)
    size = (max(1,round(tile.width*factor)),max(1,round(tile.height*factor)))
    with tile.resize(size,Image.Resampling.LANCZOS) as header:
        frame.paste(header,((fmt.size[0]-header.width)//2,(reserved-header.height)//2),header)


def block(draw, box, value, size, *, color=TEXT, minimum=None):
    """Wrap to measured glyph widths. Never silently clip a cell or source."""
    x, y, right, bottom = box
    for px in range(size, (minimum or size) - 1, -2):
        font = _get_font(px)
        lines = []
        for paragraph in value.split('\n'):
            line = ''
            for char in paragraph:
                if line and font.getlength(line + char) > right - x:
                    lines.append(line.rstrip())
                    line = char.lstrip() if char.isspace() else char
                else:
                    line += char
            lines.append(line.rstrip())
        leading = math.ceil(px * 1.32)
        if len(lines) * leading <= bottom - y and all(font.getlength(s) <= right-x for s in lines):
            for index, line in enumerate(lines):
                draw.text((x, y + index*leading), line, font=font, fill=color, anchor='lt')
            return
    raise ValueError(f'这段文字在当前版式放不下，请缩短或拆行分镜：{value[:36]}')


def _table(d, s, step, box, wide):
    x, y, r, b = box
    headers, rows = s['columns'], s['rows']
    if not wide:
        row = rows[step]
        block(d, (x, y, r, y+110), row[0], 56, color=ACCENT, minimum=40)
        y += 125
        height = (b-y)//(len(row)-1)
        for i, (header, cell) in enumerate(zip(headers[1:], row[1:])):
            top = y+i*height
            d.line((x, top, r, top), fill=LINE, width=2)
            block(d, (x, top+16, r, top+62), header, 30, color=TEAL, minimum=26)
            block(d, (x, top+65, r, top+height-4), cell, 42, minimum=30)
        return
    edges,pages=_table_pages(s,box)
    page=next(page for page in pages if page[-1][0]>=step)
    for i,h in enumerate(headers):
        block(d,(edges[i]+12,y,edges[i+1]-12,y+65),h,32,color=TEAL,minimum=24)
    top=y+70
    for index,h in page:
        timed=s.get('timing')=='span'
        if index>step and not timed: break
        focus=index==step and not timed
        if focus:
            d.rounded_rectangle((x,top,r,top+h-3),radius=10,fill=PANEL)
            d.rectangle((x,top+6,x+5,top+h-9),fill=ACCENT)
        d.line((x,top,r,top),fill=LINE,width=2)
        for j,cell in enumerate(rows[index]):
            block(d,(edges[j]+14,top+13,edges[j+1]-14,top+h-4),cell,30,
                  color=ACCENT if j==0 and (focus or timed) else TEXT,minimum=28)
        top+=h


def _table_pages(s,box):
    x,y,r,b=box
    headers,rows=s['columns'],s['rows']
    weights = [max(5, min(21, max(len(h), max(len(row[i]) for row in rows))**.77))
               for i, h in enumerate(headers)]
    edges = [x]
    for i in range(len(headers)):
        edges.append(x+round((r-x)*sum(weights[:i+1])/sum(weights)))
    height=b-y-70
    pages=[[]]
    font = _get_font(30)
    for index, row in enumerate(rows):
        line_count = 1
        for j, value in enumerate(row):
            width = edges[j+1]-edges[j]-30
            for paragraph in value.split('\n'):
                used, lines = 0., 1
                for char in paragraph:
                    advance = font.getlength(char)
                    if used+advance > width:
                        lines += 1
                        used = 0.
                    used += advance
                line_count = max(line_count, lines)
        need = max(76, line_count*40+30)
        if need > height:
            raise ValueError('横版表格的单行文字太多，请分成两条资料')
        if sum(h for _,h in pages[-1])+need>height: pages.append([])
        pages[-1].append((index,need))
    return edges,pages


def _bars(d, s, step, box, wide):
    x,y,r,b = box
    rows = s['rows']
    maximum = max(float(row[1]) for row in rows) or 1
    block(d, (x,y,r,y+48), s['unit'], 30, color=TEAL)
    y += 66
    per_page = 4
    first = step//per_page*per_page
    height = (b-y)//min(per_page,len(rows)-first)
    for i in range(first,step+1):
        label, number, *note = rows[i]
        value = f'{number} · {note[0]}' if note and note[0] and note[0]!=number else number
        top = y+(i-first)*height
        span = r-x
        if wide:
            bar_x, bar_end = x+int(span*.26), r-int(span*.25)
            block(d,(x,top,bar_x-22,top+height),label,38,minimum=28)
            block(d,(bar_end+25,top,r,top+height),value,36,
                  color=ACCENT if i==step else TEXT, minimum=28)
            bar_y = top+12
        else:
            bar_x, bar_end = x,r
            block(d,(x,top,x+int(span*.52),top+78),label,40,minimum=30)
            block(d,(x+int(span*.56),top,r,top+78),value,38,
                  color=ACCENT, minimum=28)
            bar_y = top+85
        d.rectangle((bar_x,bar_y,bar_end,bar_y+14),fill=LINE)
        width = round((float(number)/maximum)*(bar_end-bar_x))
        if width:
            d.rectangle((bar_x,bar_y,bar_x+width-1,bar_y+14),fill=ACCENT if i==step else TEAL)


def _person(image, d, s, step, box, wide, media):
    x,y,r,b = box
    edge = 276 if wide else 234
    if media is not None:
        tile = ImageOps.fit(media, (edge,edge), method=Image.Resampling.LANCZOS, centering=(.5,.32))
        image.paste(tile,(x,y))
        tile.close()
    else:
        d.rounded_rectangle((x,y,x+edge,y+edge),radius=10,fill=PANEL,outline=LINE,width=2)
        d.text((x+edge//2,y+edge//2),s['title'][0],font=_get_font(98),fill=ACCENT,anchor='mm')
    if wide:
        left = x+edge+80
    else:
        left = x
    info_x = x if wide else x+edge+40
    info_top = y+edge+22 if wide else y+18
    block(d,(info_x,info_top,left-32 if wide else r,b if wide else y+edge),
          s.get('detail',''),34,color=TEAL,minimum=28)
    if not wide:
        y += edge+48
    rows = s['rows']
    first = step//3*3
    height = (b-y)//min(3,len(rows)-first)
    for i in range(first,step+1):
        top = y+(i-first)*height
        label, content = rows[i]
        d.line((left,top,r,top), fill=LINE,width=2)
        block(d,(left+15,top+11,r,top+57),label,30,color=ACCENT if i==step else TEAL,minimum=26)
        block(d,(left+15,top+59,r,top+height-3),content,40 if wide else 42,minimum=30)


def _route(image, d, s, step, box, wide, media):
    x,y,r,b = box
    if wide:
        right, bottom = x+int((r-x)*.65), b
        tx,ty = right+52,y+8
    else:
        right,bottom = r,y+int((b-y)*.65)
        tx,ty = x,bottom+28
    d.rounded_rectangle((x,y,right,bottom),radius=12,fill=PANEL,outline=LINE,width=2)
    if media is not None:
        tile = ImageOps.contain(media,(right-x-4,bottom-y-4),method=Image.Resampling.LANCZOS)
        mw,mh = tile.size
        mx,my = (x+right-mw)//2,(y+bottom-mh)//2
        image.paste(tile,(mx,my))
        tile.close()
    else:
        # Insets leave the outermost schematic markers completely visible.
        mx,my = x+34,y+34
        mw,mh = right-x-68,bottom-y-68
    locations = [(mx+round(float(row[1])*(mw-1)), my+round(float(row[2])*(mh-1)))
                 for row in s['rows'][:step+1]]
    if len(locations)>1:
        d.line(locations,fill='#37281e',width=12,joint='curve')
        d.line(locations,fill=ACCENT,width=6,joint='curve')
        (ax,ay),(bx,by) = locations[-2:]
        length = math.hypot(bx-ax,by-ay)
        if length>70:
            dx,dy = (bx-ax)/length,(by-ay)/length
            cx,cy = ax+.58*(bx-ax),ay+.58*(by-ay)
            d.polygon([(cx+12*dx,cy+12*dy),(cx-9*dx-7*dy,cy-9*dy+7*dx),
                       (cx-9*dx+7*dy,cy-9*dy-7*dx)],fill=ACCENT)
    for i,(cx,cy) in enumerate(locations):
        radius = 17 if i==step else 13
        d.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),fill=ACCENT if i==step else TEAL,
                  outline=BACKGROUND,width=2)
        d.text((cx,cy),str(i+1),font=_get_font(18),fill=BACKGROUND,anchor='mm')
    # Prefer the current label. Leave a numbered marker when space is tight.
    busy = [(cx-19,cy-19,cx+19,cy+19) for cx,cy in locations]
    font = _get_font(26)
    for i in reversed(range(len(locations))):
        cx,cy = locations[i]
        name = s['rows'][i][0]
        width = math.ceil(font.getlength(name))+16
        height = 40
        for lx,ly in [(cx+24,cy-20),(cx-24-width,cy-20),(cx-width//2,cy-64),(cx-width//2,cy+24)]:
            box = (lx,ly,lx+width,ly+height)
            if lx<x+4 or ly<y+4 or box[2]>right-4 or box[3]>bottom-4:
                continue
            if any(lx<v and box[2]>a and ly<z and box[3]>b0 for a,b0,v,z in busy):
                continue
            d.rounded_rectangle(box,radius=6,fill=PANEL)
            d.text((lx+8,ly+6),name,font=font,fill=ACCENT if i==step else TEXT,anchor='lt')
            busy.append(box)
            break
    block(d,(tx,ty,r,ty+58),'路线示意',32,color=TEAL)
    block(d,(tx,ty+62,r,ty+155),s['rows'][step][0],52,color=ACCENT,minimum=38)
    block(d,(tx,ty+155,r,b),s['rows'][step][3],36,minimum=28)


def _box(fmt):
    x=112 if fmt is VideoFormat.LANDSCAPE else 84
    footer=786 if fmt is VideoFormat.LANDSCAPE else 1320
    return x,HEADER_END[fmt]+124,fmt.size[0]-x,footer-22


def page_steps(spec,fmt):
    count=len(spec['rows'])
    if spec.get('timing')=='span':
        if spec['kind']=='table' and fmt is VideoFormat.LANDSCAPE:
            return [page[-1][0] for page in _table_pages(spec,_box(fmt))[1]]
        if spec['kind']=='bars':
            return [min(i+3,count-1) for i in range(0,count,4)]
    return list(range(count))


def render_plate(spec, step, video_format, media=None):
    fmt = VideoFormat(video_format)
    w,h = fmt.size
    wide = fmt is VideoFormat.LANDSCAPE
    image = Image.new('RGB',(w,h),BACKGROUND)
    try:
        d = ImageDraw.Draw(image)
        x,r = (112,w-112) if wide else (84,w-84)
        top = HEADER_END[fmt]
        footer = 786 if wide else 1320
        d.line((x,top+1,r,top+1),fill=LINE,width=2)
        d.line((x,top+1,x+78,top+1),fill=ACCENT,width=5)
        block(d,(x,top+19,r-140,top+105),spec['title'],60 if wide else 56,minimum=38)
        if spec.get('timing')=='span':
            pages=page_steps(spec,fmt)
            page=next(i for i,last in enumerate(pages) if last>=step)
            step=pages[page]
            counter=f'{page+1} / {len(pages)}' if len(pages)>1 else ''
        else:
            counter=f'{step+1} / {len(spec["rows"])}'
        d.text((r,top+30),counter,font=_get_font(28),fill=MUTED,anchor='rt')
        box = _box(fmt)
        credit=spec['source']
        if spec['kind']=='table':
            _table(d,spec,step,box,wide)
        elif spec['kind']=='bars':
            _bars(d,spec,step,box,wide)
        elif spec['kind']=='person':
            _person(image,d,spec,step,box,wide,media)
        elif spec['kind']=='campaign':
            from app.services.historical_atlas import credit as map_credit, draw
            draw(image,d,spec,step,box,wide,media)
            credit=map_credit(spec,step,wide)
        else:
            _route(image,d,spec,step,box,wide,media)
        d.line((x,footer,r,footer),fill=LINE,width=2)
        block(d,(x,footer+10,r,footer+(66 if wide else 86)),credit,28,color=MUTED,minimum=24)
        return image
    except BaseException:
        image.close()
        raise
