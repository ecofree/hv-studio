"""Text-constrained acoustic timing using a loaded, offline Whisper model."""
import argparse
import hashlib
import json
from pathlib import Path
import time


def align(model, request):
    from faster_whisper.audio import decode_audio, pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    started = time.monotonic()
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
    return result


def main():
    from local_asr_worker import load_model
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    result = align(load_model(request['model'],request['threads']),request)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')


if __name__ == '__main__':
    main()
