"""One measured subtitle contract for planning, rendering and acceptance."""
from PIL import Image, ImageDraw


def measure(text):
    from app.services import compose as C
    from app.config.config import get_settings
    cfg = get_settings().get_profile().subtitle
    font = C._get_font(C._sub_font_size())
    canvas = Image.new('RGB', (1, 1))
    draw = ImageDraw.Draw(canvas)
    lines = C._wrap(draw, C.subtitle_display_text(text) or text, font, C.TEXT_W)
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    height = sum(b[3] for b in boxes) + C.LINE_GAP * (len(lines)-1)
    stroke = C._sub_stroke()
    top = C._sub_bottom()-height-stroke-4
    rectangles=[]; y=top
    for line, box in zip(lines, boxes):
        x=(C.W-box[2])//2
        rectangles.extend([draw.textbbox((x,y),line,font=font,stroke_width=stroke),
                           draw.textbbox((x+3,y+4),line,font=font,stroke_width=stroke)])
        y+=box[3]+C.LINE_GAP
    actual=(min(r[0] for r in rectangles),min(r[1] for r in rectangles),
            max(r[2] for r in rectangles),max(r[3] for r in rectangles)) if rectangles else (0,0,0,0)
    errors = []
    if not text.strip(): errors.append('empty subtitle')
    if len(lines) > 2: errors.append('subtitle exceeds two lines')
    if any(len(line) > cfg.max_chars_per_line for line in lines):
        errors.append('subtitle line exceeds character limit')
    if actual[1] < cfg.margin_top:
        errors.append('subtitle outside top safe area')
    if actual[3] > C._sub_bottom(): errors.append('subtitle outside bottom safe area')
    if actual[0] < 0 or actual[2] > C.W: errors.append('subtitle outline outside horizontal safe area')
    if any(b[2] > C.TEXT_W for b in boxes): errors.append('subtitle outside horizontal safe area')
    canvas.close()
    return {'lines': lines, 'line_count': len(lines), 'height': height,
            'top': top, 'actual_bbox': actual, 'errors': errors}


def validate_cues(cues, sample_rate):
    from app.config.config import get_settings
    cfg = get_settings().get_profile().subtitle
    metrics = []
    for cue in cues:
        row = measure(cue['text'])
        milliseconds = (cue['sample_end']-cue['sample_start'])*1000/sample_rate
        if milliseconds < cfg.min_duration_ms-1 or milliseconds > cfg.max_duration_ms+1:
            row['errors'].append('subtitle duration outside configured limits')
        row.update(unit_id=cue['unit_id'], duration_ms=milliseconds)
        if row['errors']:
            raise ValueError(f"subtitle layout {cue['unit_id']}: {', '.join(row['errors'])}")
        metrics.append(row)
    return metrics


def overlay(text):
    """Rendered once per cue, not measured/drawn for every video frame."""
    from app.services.compose import _draw_subtitle
    canvas = Image.new('RGBA', (1080, 1920), (0, 0, 0, 0))
    _draw_subtitle(canvas, text)
    box = canvas.getbbox()
    tile = canvas.crop(box) if box else Image.new('RGBA', (1, 1))
    canvas.close()
    return tile, box or (0, 0, 1, 1)
