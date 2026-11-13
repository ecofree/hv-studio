"""Offline Whisper operations in the pinned ASR interpreter."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import time


def load_model(path, threads):
    os.environ['HF_HUB_OFFLINE'] = '1'
    def blocked(*args, **kwargs):
        raise RuntimeError('network disabled in local acoustic worker')
    socket.socket.connect = blocked
    from faster_whisper import WhisperModel
    return WhisperModel(path, device='cpu', compute_type='int8', cpu_threads=threads,
                        num_workers=1, local_files_only=True)


def recognize(model, audio):
    started = time.monotonic()
    segments,info=model.transcribe(audio,language='zh',beam_size=5,temperature=0,
        word_timestamps=True,vad_filter=True,condition_on_previous_text=False)
    rows=[]
    for segment in segments:
        rows.append({'start':segment.start,'end':segment.end,'text':segment.text,
            'avg_logprob':segment.avg_logprob,'no_speech_prob':segment.no_speech_prob,
            'words':[{'word':w.word,'start':w.start,'end':w.end,'probability':w.probability}
                     for w in segment.words or []]})
    result={'text':''.join(s['text'] for s in rows),'segments':rows,'language':info.language,
        'duration':info.duration,'elapsed_seconds':time.monotonic()-started,
        'runtime':{name:importlib.metadata.version(name) for name in
                   ('faster-whisper','ctranslate2','onnxruntime','av')},
        'timestamp_source':'whisper_word_estimates','alignment_review':'pending',
        'audio_sha256':hashlib.sha256(Path(audio).read_bytes()).hexdigest()}
    return result


def serve(model):
    from local_alignment_worker import align
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request['operation'] == 'recognize':
                result = recognize(model, request['audio'])
            elif request['operation'] == 'align':
                result = align(model, request)
            else:
                raise ValueError('unknown acoustic operation')
            response = {'result': result}
        except Exception as exc:
            response = {'error': f'{type(exc).__name__}: {exc}'}
        print(json.dumps(response, ensure_ascii=True, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--audio')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not args.serve and not (args.audio and args.output):
        parser.error('--audio and --output are required for one recording')
    model = load_model(args.model, args.threads)
    if args.serve:
        serve(model)
    else:
        Path(args.output).write_text(json.dumps(recognize(model,args.audio),ensure_ascii=False),encoding='utf-8')


if __name__ == '__main__':
    main()
