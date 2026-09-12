"""Capture installed SDK call() transport without any network or generated audio.

Run with --output PATH. The WAV bytes below are a transport fixture, not a voice
quality sample. No credentials, voice enrollment or live provider calls occur.
"""
import argparse
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.provenance import atomic_json


def probe(output):
    from app.services import cosyvoice as C
    from dashscope.audio.tts_v2 import SpeechSynthesizer
    messages=[]
    def connect(synth,timeout_seconds=5):
        def send(message):
            payload=json.loads(message)
            messages.append(payload)
            if payload['header']['action']=='run-task':synth.start_event.set()
            if payload['header']['action']=='finish-task':
                synth._audio_data=b'test-transport-audio'
                synth.complete_event.set()
        synth.ws=SimpleNamespace(send=send,close=lambda:None,sock=SimpleNamespace(connected=True))
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with patch.object(C,'ROOT',output),patch.object(C,'_env_get',lambda name:''),patch.object(C,'_resolve_synth_ws_url',lambda:None),patch.object(SpeechSynthesizer,'_SpeechSynthesizer__connect',connect):
        for rate in (1.0,1.2):
            svc=C.DashScopeCosyVoiceSynthesizer(api_key='test-key',model='cosyvoice-v3.5-flash',
                voice='fixture-voice',response_format='wav',speech_rate=rate,enable_ssml=False)
            assert svc._speech_bytes('十一月，十万大军出发。','fixture-voice')==b'test-transport-audio'
    result={'sdk_version':importlib.metadata.version('dashscope'),'transport':'mock',
            'paid_calls':0,'observed_messages':messages,
            'capabilities':{k:'unverified' for k in ('voice_instruction','voice_ssml','voice_timestamp','voice_wav')},
            'sdk_call_auto_enable_ssml':all(m['payload']['parameters']['enable_ssml'] is True
                for m in messages if m['header']['action']=='run-task')}
    atomic_json(output/'sdk_probe.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    args=parser.parse_args();r=probe(args.output)
    print(json.dumps({'sdk_version':r['sdk_version'],'auto_enable_ssml':r['sdk_call_auto_enable_ssml'],
                      'transport':'mock','paid_calls':0}))
