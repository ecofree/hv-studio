"""Offline ASR worker, executed only by the separately pinned ASR environment."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--audio',required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--threads',type=int,default=4)
    args=parser.parse_args()
    # Even a missing tokenizer must not trigger an implicit download or upload.
    os.environ['HF_HUB_OFFLINE']='1'
    def blocked(*a,**kw):raise RuntimeError('network disabled in local ASR worker')
    socket.socket.connect=blocked
    from faster_whisper import WhisperModel
    started=time.monotonic()
    model=WhisperModel(args.model,device='cpu',compute_type='int8',cpu_threads=args.threads,
                       num_workers=1,local_files_only=True)
    segments,info=model.transcribe(args.audio,language='zh',beam_size=5,temperature=0,
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
        'audio_sha256':hashlib.sha256(Path(args.audio).read_bytes()).hexdigest()}
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
