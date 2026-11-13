"""Deterministic, source-bound environment animation before camera/text layers.

Parallax uses fixed occlusion windows into a translated background plane. This
deliberately cannot move or invent a person, or expose an unpainted background.
All coordinates are normalized to the original plate; time is the full shot's
absolute frame interval, including when the encoder renders only a segment.
"""
import copy
import math
import random
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def _number(value, lo, hi):
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'animation number must be finite in [{lo}, {hi}]')


def _polygon(points):
    if not isinstance(points, list) or not 3 <= len(points) <= 80:
        raise ValueError('animation polygon needs 3..80 points')
    for p in points:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError('animation polygon needs xy pairs')
        for n in p:
            _number(n, 0, 1)
    area = abs(sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(points, points[1:]+points[:1])))
    if area < .00001:
        raise ValueError('animation polygon is empty')


def _curve(points, monotonic=False):
    if not isinstance(points, list) or not 2 <= len(points) <= 20:
        raise ValueError('animation curve needs 2..20 keys')
    previous = None
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('animation curve needs time/value pairs')
        for value in point:
            _number(value, 0, 1)
        if previous is not None and (point[0] <= previous[0] or (monotonic and point[1] < previous[1])):
            raise ValueError('animation curve keys must increase')
        previous = point
    if points[0][0] != 0 or points[-1][0] != 1:
        raise ValueError('animation curve must cover the full interval')


def _at(points, progress):
    for (a, x), (b, y) in zip(points, points[1:]):
        if progress <= b:
            t = max(0, min(1, (progress-a)/(b-a)))
            return x+(y-x)*t*t*(3-2*t)
    return points[-1][1]


def validate(plan, source_sha256=None):
    if not isinstance(plan, dict) or set(plan) - {'version', 'source_sha256', 'seed', 'protected', 'layers', 'frame_range'}:
        raise ValueError('unknown animation plan fields')
    if plan.get('version') != 1 or not re.fullmatch('[0-9a-f]{64}', str(plan.get('source_sha256', ''))):
        raise ValueError('animation needs version 1 and a source hash')
    if source_sha256 is not None and plan['source_sha256'] != source_sha256:
        raise ValueError('animation source hash mismatch')
    if type(plan.get('seed')) is not int or not 0 <= plan['seed'] <= 2**32-1:
        raise ValueError('animation needs a bounded integer seed')
    if not isinstance(plan.get('protected'), list) or len(plan['protected']) > 20:
        raise ValueError('animation needs explicit protected polygons')
    for p in plan['protected']:
        _polygon(p)
    if 'frame_range' in plan:
        interval = plan['frame_range']
        if (not isinstance(interval, list) or len(interval) != 2
                or any(type(f) is not int for f in interval)
                or not 0 <= interval[0] < interval[1] <= 2**31-1):
            raise ValueError('invalid animation frame range')
    if not isinstance(plan.get('layers'), list) or not 1 <= len(plan['layers']) <= 12:
        raise ValueError('animation needs 1..12 layers')
    fields = {'parallax': {'travel'}, 'rain': {'density', 'opacity', 'speed'}, 'glow': {'strength'}}
    for layer in plan['layers']:
        kind = layer.get('kind')
        if kind not in fields:
            raise ValueError('invalid animation layer fields')
        required = {'kind', 'polygon', 'feather'} | fields[kind]
        optional = {'progress'} if kind == 'parallax' else {'envelope'}
        if not required <= set(layer) or set(layer)-required-optional:
            raise ValueError('invalid animation layer fields')
        if 'progress' in layer:
            _curve(layer['progress'], monotonic=True)
        if 'envelope' in layer:
            _curve(layer['envelope'])
        _polygon(layer['polygon'])
        _number(layer['feather'], 0, .03)
        if kind == 'parallax':
            travel = layer['travel']
            if not isinstance(travel, list) or len(travel) != 2:
                raise ValueError('parallax travel needs xy')
            for axis, delta in enumerate(travel):
                _number(delta, -.02, .02)
                # A translated source must never sample beyond the plate.
                values = [p[axis] for p in layer['polygon']]
                if min(values) < abs(delta) or max(values) > 1-abs(delta):
                    raise ValueError('parallax region lacks source overscan')
        elif kind == 'rain':
            if type(layer['density']) is not int or not 1 <= layer['density'] <= 200:
                raise ValueError('invalid rain density')
            _number(layer['opacity'], 0, .35)
            _number(layer['speed'], .05, 1)
        else:
            _number(layer['strength'], 0, .12)
    return copy.deepcopy(plan)


def _mask(size, points, feather):
    mask = Image.new('L', size)
    ImageDraw.Draw(mask).polygon([(round(x*(size[0]-1)), round(y*(size[1]-1))) for x, y in points], fill=255)
    if feather:
        # Feather inward only: no rain/glow leaks beyond the authored region.
        blurred = mask.filter(ImageFilter.GaussianBlur(feather*min(size)))
        mask = ImageChops.multiply(mask, blurred)
    return mask


class Scene:
    def __init__(self, original, plan, first_frame, last_frame):
        self.plan = validate(plan)
        if first_frame < 0 or last_frame <= first_frame:
            raise ValueError('invalid animation frame interval')
        self.original, self.first, self.last = original, first_frame, last_frame
        self.clock_first, self.clock_last = self.plan.get('frame_range', [first_frame, last_frame])
        if not self.clock_first <= first_frame < last_frame <= self.clock_last:
            raise ValueError('animation clock must contain the complete shot')
        self.layers = []
        protected = Image.new('L', original.size)
        for points in self.plan['protected']:
            protected = ImageChops.lighter(protected, _mask(original.size, points, 0))
        allowed = ImageChops.invert(protected)
        rng = random.Random(self.plan['seed'])
        for layer in self.plan['layers']:
            mask = ImageChops.multiply(_mask(original.size, layer['polygon'], layer['feather']), allowed)
            box = mask.getbbox()
            if box is None:
                raise ValueError('animation layer fully occluded')
            particles = [(rng.random(), rng.random(), rng.uniform(.65, 1.35))
                         for _ in range(layer.get('density', 0))]
            self.layers.append((layer, box, mask.crop(box), particles))

    def frame(self, absolute_frame):
        if not self.first <= absolute_frame < self.last:
            raise ValueError('animation frame is outside its shot')
        elapsed = absolute_frame-self.clock_first
        seconds = elapsed/25
        progress = elapsed/max(1, self.clock_last-self.clock_first-1)
        ease = progress*progress*(3-2*progress)
        w, h = self.original.size
        frame = self.original.copy()
        for layer, box, mask, particles in self.layers:
            x0, y0, x1, y1 = box
            rw, rh = x1-x0, y1-y0
            if layer['kind'] == 'parallax':
                amount = _at(layer['progress'], progress) if 'progress' in layer else ease
                dx, dy = layer['travel'][0]*w*amount, layer['travel'][1]*h*amount
                tile = self.original.transform((rw, rh), Image.Transform.AFFINE,
                                               (1, 0, x0-dx, 0, 1, y0-dy), Image.Resampling.BICUBIC)
                frame.paste(tile, (x0, y0), mask)
            elif layer['kind'] == 'rain':
                envelope = _at(layer['envelope'], progress) if 'envelope' in layer else 1
                rain = Image.new('L', (rw, rh))
                draw = ImageDraw.Draw(rain)
                for x, y, variation in particles:
                    yy = ((y+seconds*layer['speed']*variation) % 1)*(rh+80)-40
                    xx = (x-.035*seconds*variation) % 1*rw
                    length = h*.012*variation
                    draw.line((xx, yy, xx-length*.2, yy+length),
                              fill=round(255*layer['opacity']*min(1, variation)*envelope), width=max(1, round(w/750)))
                rain = ImageChops.multiply(rain, mask)
                frame.paste((174, 188, 193), box, rain)
            else:
                # Warm, localized light; no global exposure or facial deformation.
                phase = self.plan['seed']*.01
                flicker = .48+.30*math.sin(seconds*5.3+phase)+.14*math.sin(seconds*11.7+phase)
                envelope = _at(layer['envelope'], progress) if 'envelope' in layer else 1
                amount = layer['strength']*flicker*envelope
                glow = mask.point(lambda x: round(x*amount))
                frame.paste((255, 173, 64), box, glow)
        return frame
