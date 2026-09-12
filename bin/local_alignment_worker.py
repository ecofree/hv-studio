"""Offline, text-constrained acoustic alignment in the pinned ASR environment.

Recognition is kept independent: forcing a transcript is timing evidence, never
proof that the speaker said the right words.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    os.environ['HF_HUB_OFFLINE'] = '1'
    def blocked(*a, **kw): raise RuntimeError('network disabled in local alignment')
    socket.socket.connect = blocked
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio, pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    started = time.monotonic()
    model = WhisperModel(request['model'], device='cpu', compute_type='int8',
                         cpu_threads=request['threads'], num_workers=1, local_files_only=True)
    tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task='transcribe', language='zh')
    audio = decode_audio(request['audio'], sampling_rate=16000)
    rows = []
    for window in request['windows']:
        a, b = window['start'], window['end']
        if not 0 <= a < b <= len(audio)/16000 + .001 or b-a > 30:
            raise ValueError('alignment window must be an observed audio span of at most 30 seconds')
        chunk = audio[round(a*16000):round(b*16000)]
        features = model.feature_extractor(chunk)
        encoded = model.encode(pad_or_trim(features, model.feature_extractor.nb_max_frames))
        tokens = tokenizer.encode(window['text'])
        if len(tokens) > 440: raise ValueError('too many text tokens for alignment window')
        aligned = model.find_alignment(tokenizer, [tokens], encoded, round(len(chunk)/160))[0]
        # Preserve the exact model token boundaries and probability for review.
        for word in aligned:
            if not word['word']: continue
            rows.append({'word': word['word'], 'start': a+float(word['start']),
                         'end': a+float(word['end']), 'probability': float(word['probability'])})
    result = {'words': rows, 'windows': request['windows'],
              'audio_sha256': hashlib.sha256(Path(request['audio']).read_bytes()).hexdigest(),
              'elapsed_seconds': time.monotonic()-started,
              'method': 'whisper_text_constrained_acoustic_dtw', 'listening_approval': False}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__': main()
