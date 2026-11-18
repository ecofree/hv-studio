"""Compatibility launcher for the project-owned Qwen production entry.

Existing authored scripts, voice references and generated audio are untouched.
The former unbound HTTP/direct-model fallbacks are no longer production paths.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def validate_voice_direction(mode, instruct):
    if mode == 'clone' and instruct:
        raise ValueError('clone 不支持 instruct；不会替换已选音色')
    if mode not in ('clone', 'custom'):
        raise ValueError('unsupported voice mode')


def call_tts_api(*args, **kwargs):
    validate_voice_direction(kwargs.get('mode', 'clone'), kwargs.get('instruct'))
    raise ValueError('旧 HTTP 配音入口已合并；请使用 bin/studio_narrate.py 的本集声音方案和原有预算')


def call_tts_direct(*args, **kwargs):
    raise ValueError('旧直接加载入口已合并；请使用 bin/studio_narrate.py，不自动切换声音')


def _retired_request(req):
    raise ValueError('旧配音入口已合并；请使用已绑定声音方案的统一入口')


call_tts_api.validate_request = _retired_request
call_tts_direct.validate_request = _retired_request


def render_act(*args, **kwargs):
    validate_voice_direction(kwargs.get('mode', 'clone'), kwargs.get('instruct'))
    raise ValueError('请按剧集使用统一配音入口；旧文案和音频保留')


def main():
    from bin.studio_narrate import main as narrate
    return narrate()


if __name__ == '__main__': raise SystemExit(main())
