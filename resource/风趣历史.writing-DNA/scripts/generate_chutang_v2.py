"""Compatibility entry; historical directions below are retained for reference only."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))

SPEAKER='dylan'

MODE='custom'

UNIFORM_INSTRUCT='沉稳清楚的同一位叙述者，保留自然音高变化；情绪通过句意、重音、节奏和停顿表达，避免喊叫与刻意压嗓。'

def render_act(*args, **kwargs):
    raise ValueError('旧分幕配音入口已合并；请按剧集使用 bin/studio_narrate.py，保留已选声音方案')


def main():
    from bin.studio_narrate import main as narrate
    return narrate()


if __name__ == '__main__': raise SystemExit(main())
