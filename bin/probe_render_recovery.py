"""Real-media kill/resume and same-frame replacement probe; synthetic inputs only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def worker(folder, interrupt):
    from app.services import production_v2 as P
    from app.config.config import get_settings
    cfg=get_settings().compose; cfg.v2_segment_max_sec=2
    original=P._encode_v2_segment
    def encode(rows,specs,cues,sr,path,vcodec,timeout):
        if interrupt and Path(path).name=='seg-001.mp4':
            Path(path).with_suffix('.mp4.tmp').write_bytes(b'interrupted synthetic partial output')
            (folder/'interrupt-ready').write_text(str(os.getpid()))
            time.sleep(30)  # Controlled failpoint; the parent kills this process.
        return original(rows,specs,cues,sr,path,vcodec,timeout)
    P._encode_v2_segment=encode
    request=json.loads((folder/'request.json').read_text())
    receipt=P.render(request['manifest'],folder/'timeline.json',folder/'renders',timeout=60)
    (folder/'receipt-path').write_text(str(receipt.resolve()))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--worker');parser.add_argument('--interrupt',action='store_true')
    parser.add_argument('--output',default='state/remediation-20260912/recovery')
    args=parser.parse_args()
    if args.worker:return worker(Path(args.worker),args.interrupt)
    from app.services import narration_pipeline as N
    from app.services.provenance import atomic_json,file_sha256,fingerprint
    from PIL import Image
    folder=(ROOT/args.output/uuid.uuid4().hex);folder.mkdir(parents=True)
    source=folder/'source.md';source.write_text('甲军抵达。\n\n乙军出发。\n\n大桥安全。',encoding='utf-8')
    plan=N.prepare(source,folder/'plans')
    asset=folder/'voice.json';atomic_json(asset,dict(model='cosyvoice-v3.5-flash',voice_id='synthetic-only',version='fixture',capabilities={}))
    def provider(req,path):
        with wave.open(str(path),'wb') as out:
            out.setparams((1,2,24000,0,'NONE','not compressed'));out.writeframes(b'\0\0'*48000)
        return dict(status='succeeded',audio_sha256=file_sha256(path))
    def checker(path,text):return dict(status='passed',audio_sha256=file_sha256(path),expected_sha256=fingerprint(text))
    manifest=N.synthesize(plan,asset,max_requests=3,max_characters=100,provider=provider,checker=checker)
    m=N.read(manifest);image=folder/'image.png';Image.new('RGB',(1080,1920),(50,100,150)).save(image)
    atomic_json(folder/'timeline.json',dict(shots=[dict(id='long',sample_start=0,sample_end=m['samples'],image_path=str(image),
                motion=dict(type='push',start_zoom=1,end_zoom=1.08))],
        subtitles=[dict(unit_id=u['unit_id'],text=u['text'],text_start=0,text_end=len(u['text']),sample_start=u['sample_start'],sample_end=u['sample_end']) for u in m['alignment']]))
    atomic_json(folder/'request.json',{'manifest':str(manifest.resolve())})
    command=[sys.executable,str(Path(__file__).resolve()),'--worker',str(folder)]
    with (folder/'worker.log').open('wb') as log:
        proc=subprocess.Popen(command+['--interrupt'],stdout=log,stderr=log,cwd=ROOT)
        deadline=time.monotonic()+90
        while not (folder/'interrupt-ready').exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.1)
        if not (folder/'interrupt-ready').exists():
            proc.kill();proc.wait();raise RuntimeError('render failpoint not reached; see worker log')
        states=list((folder/'renders').rglob('render_state.json'));state=N.read(states[0])
        first=state['segments'][0];old_sha=file_sha256(first['path'])
        import psutil
        killed=[p.pid for p in psutil.Process(proc.pid).children(recursive=True)]+[proc.pid]
        for pid in killed:
            try:psutil.Process(pid).kill()
            except psutil.NoSuchProcess:pass
        proc.wait(timeout=10)
        resumed=subprocess.run(command,stdout=log,stderr=log,cwd=ROOT,timeout=90)
        if resumed.returncode:raise RuntimeError('resume failed; see worker log')
    receipt=N.read(Path((folder/'receipt-path').read_text()));state=N.read(receipt['render_state_path'])
    assert state['segments'][0]['reused'] and file_sha256(first['path'])==old_sha
    assert all(r['status']=='complete' and r['expected_frames']<=50 for r in state['segments'])
    # Keep frame count/codec valid while replacing one segment's actual content.
    replacement=Path(state['segments'][1]['path'])
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=red:s=1080x1920:r=25:d=2',
                    '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(replacement)],check=True,timeout=30)
    Path((folder/'receipt-path').read_text()).unlink()
    with (folder/'worker.log').open('ab') as log:subprocess.run(command,stdout=log,stderr=log,cwd=ROOT,check=True,timeout=90)
    final=N.read(N.read(Path((folder/'receipt-path').read_text()))['render_state_path'])
    assert [r['reused'] for r in final['segments']]==[True,False,True]
    report=dict(status='passed',synthetic_only=True,paid_calls=0,kill_exit=proc.returncode,
                killed_pids=killed,first_segment_preserved=True,replacement_reused=False,expected_frames=150,
                actual_integrity=receipt['integrity'],final_segment_reuse=[r['reused'] for r in final['segments']],folder=str(folder))
    atomic_json(folder/'result.json',report);print(json.dumps({k:v for k,v in report.items() if k!='actual_integrity'},ensure_ascii=False))


if __name__=='__main__':main()
