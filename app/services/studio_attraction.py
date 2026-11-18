"""Offline presentation drafts and handoff to the existing directing pipeline.

Drafts never change narration, approvals, image jobs or generation budgets.
Only observed word boundaries may become production timing.
"""
import copy
import json
from pathlib import Path

from app import paths
from app.services.provenance import atomic_json, fingerprint, file_sha256

FRAMINGS = {'保留当前安排': None, '全景': (1., 1.), '动作中景': (1.16, 1.16),
            '局部细节': (1.32, 1.32), '缓推强调': (1., 1.12)}


def framing_motion(name):
    start, end = FRAMINGS[name]
    return dict(type='static' if start == end else 'push', start_zoom=start, end_zoom=end,
                start_xy=[.5,.5], end_xy=[.5,.5], max_zoom=1.6, geometry_version='fit_cover_v2')


def path_for(eid):
    return paths.episode_dir(eid) / 'studio' / 'attraction.json'


def source_key(document):
    return fingerprint([(u['unit_id'], u['source_text']) for u in document['units']])


def locate(document, quote):
    matches = []
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError('请填写能在原旁白中唯一定位的原句')
    for unit in document['units']:
        text = unit['source_text']
        start = 0
        while (index := text.find(quote, start)) >= 0:
            matches.append(dict(unit_id=unit['unit_id'], text_start=index))
            start = index + 1
    if len(matches) != 1:
        raise ValueError('原句不能唯一定位：' + quote)
    return matches[0]


def empty(document):
    return dict(version=1, source_identity=source_key(document), title='',
                title_options=[], cover=dict(text='', composition='', formats=['3:4', '4:3']),
                opening=[], acts=[], visual_beats=[], description='', topics=[],
                ai_disclosure='发布时使用平台提供的 AI 内容声明功能',
                preserve=dict(narration=True, audio=True, style=True, voice=True))


def load(eid):
    p = path_for(eid)
    return json.loads(p.read_text('utf-8')) if p.exists() else None


def validate(plan, document):
    if not isinstance(plan, dict):
        raise ValueError('方案须为对象')
    if plan.get('version') != 1 or plan.get('source_identity') != source_key(document):
        raise ValueError('方案对应另一版旁白；保留旧方案，请重新核对原句位置')
    if not isinstance(plan.get('title'), str) or not isinstance(plan.get('cover'), dict):
        raise ValueError('请保留标题与封面字段')
    if not all(isinstance(plan['cover'].get(k), str) for k in ('text', 'composition')):
        raise ValueError('封面短句与构图须为文字')
    if not isinstance(plan.get('visual_beats'), list):
        raise ValueError('镜头节点须为列表')
    if plan.get('preserve') != dict(narration=True, audio=True, style=True, voice=True):
        raise ValueError('本入口仅编辑画面与发布包装，保留正文、音轨、画风和音色')
    for beat in plan.get('visual_beats', []):
        if not isinstance(beat, dict) or 'quote' not in beat:
            raise ValueError('每个镜头节点须包含原句 quote')
        locate(document, beat['quote'])
    return plan


def save(eid, plan, document, expected):
    from app.services.process_lock import file_lock
    p = path_for(eid)
    validate(plan, document)
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p.with_suffix('.lock')):
        current = file_sha256(p) if p.exists() else None
        if current != expected:
            raise ValueError('方案已更新，请刷新后再保存')
        if p.exists():
            atomic_json(p.parent/'attraction-history'/(current+'.json'), load(eid))
        atomic_json(p, plan)
    return p


def notes(plan):
    result = []
    if len(plan.get('title', '')) > 30:
        result.append('标题较长，请检查发布界面中的截断效果')
    if len(plan.get('cover', {}).get('text', '')) > 10:
        result.append('封面字较多，建议缩小检查是否一眼可读')
    if not plan.get('title') or not plan.get('cover', {}).get('text'):
        result.append('补充标题和封面短句，表达同一个观看理由')
    return result


def compile_direction(plan, document, manifest, evidence, image_assets):
    """Produce a downloadable directing draft; never install it or create assets."""
    from app.services.directing import source_identity, anchor_sample
    from app.models.shot import MotionSpec
    from app.services.motion import resolve_motion
    validate(plan, document)
    if source_identity(manifest) != source_key(document):
        raise ValueError('录音对应的正文与方案不一致')
    beats = plan.get('visual_beats', [])
    if not beats:
        raise ValueError('请先安排画面节点')
    events, starts = [], []
    for beat in beats:
        anchor = locate(document, beat['quote'])
        if not events:
            if anchor != dict(unit_id=manifest['alignment'][0]['unit_id'], text_start=0):
                raise ValueError('首镜必须从原旁白开头开始，不能丢弃开头音频')
            anchor = {'at': 'start'}
        start = anchor_sample(anchor, manifest, evidence)
        key = beat.get('image_id', '')
        if key not in image_assets or not Path(image_assets[key]).is_file():
            raise ValueError('节点尚未指定已有底图：' + beat['quote'])
        motion = copy.deepcopy(beat.get('motion') or dict(type='static', start_zoom=1., end_zoom=1.))
        motion['geometry_version'] = 'fit_cover_v2'
        spec = MotionSpec(**motion)
        resolve_motion(spec)
        starts.append(start)
        events.append(dict(at=anchor, image_id=key, motion=spec.model_dump(mode='json'),
                           purpose=beat.get('purpose', '')))
    if starts != sorted(set(starts)) or starts[-1] >= manifest['samples']:
        raise ValueError('画面节点须按原旁白顺序排列且位于音轨内')
    cards = []
    title = plan.get('cover', {}).get('text', '')
    if title:
        from app.services import overlay
        duration = min(3800, int(manifest['samples']*1000/manifest['sample_rate']))
        spec = overlay.Spec(kind=overlay.KIND_TITLE, start_ms=0, end_ms=duration,
                            title=title, fade_in_ms=0, fade_ms=400)
        overlay.validate([spec], total_ms=manifest['samples']*1000/manifest['sample_rate'])
        card = spec.payload()
        card.pop('start_ms'); card.pop('end_ms')
        cards.append(dict(at={'at':'start'}, duration_ms=duration, card=card))
    return dict(version=1, source_identity=source_identity(manifest), visual_events=events,
                cards=cards, sound={})


def rhythm_points(timeline, sample_rate):
    """Advisory only: distinguish image reuse from shot changes, no max-duration gate."""
    shots = timeline.get('shots', [])
    holds, result = [], []
    for shot in shots:
        a, b = shot['sample_start']/sample_rate, shot['sample_end']/sample_rate
        image = shot['image_path']
        if holds and holds[-1]['image'] == image and abs(holds[-1]['end']-a) < 1/sample_rate:
            holds[-1]['end'] = b
            holds[-1]['shots'] += 1
        else:
            holds.append(dict(image=image, start=a, end=b, shots=1))
    for hold in holds:
        seconds = hold['end']-hold['start']
        threshold = 8 if hold['start'] < 15 else 25
        if seconds > threshold:
            result.append(dict(start_seconds=hold['start'], advisory_only=True,
                label='开场视觉吸引力' if hold['start'] < 15 else '全片画面变化',
                text=f"同底图连续 {seconds:.1f} 秒，含 {hold['shots']} 个镜头。检查构图、动作或信息是否有推进；可复用底图裁切，不要求补图。"))
    return result
