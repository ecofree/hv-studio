"""Compatibility entry; historical directions below are retained for reference only."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))

SPEAKER = "dylan"

MODE = "custom"

ACT_TONES = ['沉稳叙事，带疑问与悬念', '紧张而克制，停顿利落', '凝重，节奏稍缓',
             '坚定，逐渐推进叙事', '克制而坚决', '苍凉而敬重', '昂扬而庄重', '肃穆沉静，克制不煽情']

def chunk_direction(act_index, text, chunk_index, count):
    direction='沉稳有力，咬字清晰，老纪录片旁白；'+ACT_TONES[act_index]+'。'
    if act_index==0 and chunk_index==0:direction+='开篇从容铺陈。'
    if act_index==3 and any(word in text for word in ('宁可','誓','绝不')):
        direction+='只在誓言句表达决心，靠重音和停顿体现。'
    if act_index==4 and '绝食' in text:direction+='涉及绝食日数时停顿清楚。'
    if act_index==6 and '十八' in text:direction+='十八日无人上台这一处略放慢。'
    if chunk_index==count-1:direction+='末句自然收束。'
    return direction+'保持自然稳定的整体说话音量；情绪通过语气、节奏和停顿表达，避免突然喊叫或压低整体音量。'

def render_act(*args, **kwargs):
    raise ValueError('旧分幕配音入口已合并；请按剧集使用 bin/studio_narrate.py，保留已选声音方案')


def main():
    from bin.studio_narrate import main as narrate
    return narrate()


if __name__ == '__main__': raise SystemExit(main())
