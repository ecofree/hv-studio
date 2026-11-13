"""Uniform Dylan narration using the shared immutable, budgeted take service."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
from app.services.provenance import fingerprint,file_sha256,atomic_json
from app.services.provider_budget import ProviderBudget
from app.services.studio_voice import render_act as shared_render
from generate_qwen3_local_audio import parse_acts_from_md,split_text_into_chunks,call_tts_api,concatenate_wavs

SPEAKER='dylan'
MODE='custom'
UNIFORM_INSTRUCT='沉稳清楚的同一位叙述者，保留自然音高变化；情绪通过句意、重音、节奏和停顿表达，避免喊叫与刻意压嗓。'

def render_act(title,text,output_path,cache_dir=None,*,budget=None,retry_uncertain=False):
    from app import paths
    chunks=[(c,UNIFORM_INSTRUCT) for c in split_text_into_chunks(text,max_chars=140)]
    return shared_render(chunks,output_path,speaker=SPEAKER,mode=MODE,model='Qwen3-TTS-12Hz-1.7B-CustomVoice',
        engine_revision=file_sha256(Path(__file__).with_name('generate_qwen3_local_audio.py')),
        provider=call_tts_api,budget=budget,retry_uncertain=retry_uncertain,cache=cache_dir or paths.state_dir()/'voice-takes')

def main():
    p=argparse.ArgumentParser(description='统一声线配音；默认零新调用，保留全部历史素材')
    p.add_argument('script',nargs='?',default=str(Path(__file__).with_name('初唐异人传_第一集_Gemini_TTS专版.md')))
    p.add_argument('--acts',default='');p.add_argument('--budget');p.add_argument('--max-requests',type=int,default=0)
    p.add_argument('--max-characters',type=int,default=0);p.add_argument('--retry-uncertain',action='store_true')
    a=p.parse_args();acts=parse_acts_from_md(a.script)
    ident=fingerprint([Path(a.script).read_text('utf-8'),SPEAKER,UNIFORM_INSTRUCT,file_sha256(__file__)])[:20]
    out=Path(__file__).parent/'audio/studio-uniform'/ident
    budget=ProviderBudget(a.budget,tts_requests=a.max_requests,characters=a.max_characters) if a.budget else None
    selected={int(x) for x in a.acts.split(',') if x.strip()};files=[]
    for i,(title,text) in enumerate(acts,1):
        if selected and i not in selected:continue
        files.append(render_act(title,text,out/f'act-{i:02}.wav',budget=budget,retry_uncertain=a.retry_uncertain))
    if len(files)==len(acts):
        master=out/'master.wav';receipt=master.with_suffix('.inputs.json')
        inputs={str(f):file_sha256(f) for f in files}
        if master.exists():
            from app.services.narration_pipeline import read
            r=read(receipt)
            if r['inputs']!=inputs or r['sha256']!=file_sha256(master):raise ValueError('master changed; original preserved')
        else:
            concatenate_wavs([str(f) for f in files],str(master),silence_sec=.8)
            atomic_json(receipt,dict(inputs=inputs,sha256=file_sha256(master),approved=False,listening_review='pending'))
    print(f'已准备 {len(files)} 段候选，仍需声线、读音、表演和响度试听：{out}')
if __name__=='__main__':main()
