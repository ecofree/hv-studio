"""Render source-bound animation excerpts with identical current-version audio.

No episode mutation, provider access, retiming, TTS, or content approval. Run
detached on Windows. The plan's explicit frame windows are excerpts of an
existing validated timeline, not a replacement narration clock.
"""
import argparse
import copy
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['PATH']=str(ROOT/'tools/ffmpeg/bin')+os.pathsep+os.environ.get('PATH','')

from app.services.provenance import atomic_json, file_sha256
from app.services.narration_pipeline import read, validate_assembly
from app.services.production_v2 import validate_timeline, frame_boundary, _encode_v2_segment
from app.services.forced_alignment import require_evidence
from app.services.scene_animation import validate as validate_animation
from app.models.shot import MotionSpec


def run(args):
    subprocess.run(['ffmpeg','-y','-v','error',*map(str,args)],check=True,timeout=300)


def probe(path):
    r=subprocess.run(['ffprobe','-v','error','-count_frames','-show_entries',
                      'stream=codec_type,width,height,r_frame_rate,nb_read_frames,sample_rate,duration',
                      '-of','json',str(path)],capture_output=True,text=True,check=True,timeout=60)
    return json.loads(r.stdout)['streams']


def decoded_pcm(path):
    return subprocess.run(['ffmpeg','-v','error','-i',str(path),'-vn','-f','s16le','-'],
                          capture_output=True,check=True,timeout=60).stdout


def build(plan_path, destination):
    from app.services import karaoke, overlay
    from app.services.compose import _sub_font_path
    from app.services.scene_animation import Scene
    from PIL import Image
    plan=read(plan_path); dest=Path(destination).resolve();dest.mkdir(parents=True,exist_ok=True)
    review=dest/'review';review.mkdir(exist_ok=True)
    manifest=validate_assembly(plan['manifest_path']);timeline=read(plan['timeline_path'])
    validate_timeline(manifest,timeline);require_evidence(manifest,timeline)
    sr=manifest['sample_rate']
    if sr%25:
        raise ValueError('sample excerpts require integral samples per frame')
    identifiers=[s['id'] for s in plan['samples']]
    if plan.get('version') != 1 or len(set(identifiers)) != len(identifiers) or any(
            not re.fullmatch('[a-z0-9-]{1,60}', name) for name in identifiers):
        raise ValueError('invalid or duplicate sample identifiers')
    # Validate masks/occlusion before any costly rendering, including layers
    # hidden completely by a person-protection region.
    for item in plan['samples']:
        for key, animation in item['animation_by_image'].items():
            for shot in timeline['shots']:
                first=frame_boundary(shot['sample_start'],sr);last=frame_boundary(shot['sample_end'],sr)
                if shot['image_id']!=key or first>=item['frames'][1] or last<=item['frames'][0]:
                    continue
                validate_animation(animation,file_sha256(shot['image_path']))
                with Image.open(shot['image_path']) as original:
                    Scene(original,animation,first,last)
    inputs={str(Path(p).resolve()):h for p,h in timeline.get('inputs',{}).items()}
    paths=[Path(__file__),Path(plan_path),Path(plan['manifest_path']),Path(plan['timeline_path']),
           Path(plan['current_video']),Path(plan['current_audio']),Path(manifest['master_path']),
           Path(timeline['alignment_evidence_path']),Path(manifest['plan_path'])]
    paths += [Path(_sub_font_path()), ROOT/'requirements.core.lock.txt']
    paths+=list((ROOT/'app').rglob('*.py'))+list((ROOT/'app/config').glob('*.yaml'))
    paths+=[Path(s['image_path']) for s in timeline['shots']]
    if timeline.get('overlay_path'):paths.append(Path(timeline['overlay_path']))
    for p in paths:inputs[str(p.resolve())]=file_sha256(p)
    def check_inputs():
        drift=[p for p,h in inputs.items() if file_sha256(p)!=h]
        if drift:raise ValueError(f'input drift: {drift}')
    check_inputs()
    atomic_json(dest/'render-inputs.json',inputs)
    overlays=overlay.load_path(timeline['overlay_path']) if timeline.get('overlay_path') else None
    highlights=karaoke.load(timeline,read(manifest['plan_path']),timeline['alignment_evidence_path'])
    with wave.open(plan['current_audio'],'rb') as w:
        params=w.getparams();pcm=w.readframes(w.getnframes())
        if params.framerate!=sr or params.nframes!=manifest['samples']:
            raise ValueError('current mix is not the full source clock')
    codec=['-c:v','libx264','-preset','fast','-crf','18']
    results=[]
    for item in plan['samples']:
        first,last=item['frames'];count=last-first
        if not 100<=count<=150 or first<0 or last>frame_boundary(manifest['samples'],sr):
            raise ValueError('each sample must be 4..6 seconds inside the source')
        modified=copy.deepcopy(timeline)
        rows=[]
        for i,shot in enumerate(modified['shots']):
            a=max(first,frame_boundary(shot['sample_start'],sr))
            b=min(last,frame_boundary(shot['sample_end'],sr))
            if a<b:
                shot['animation']=validate_animation(item['animation_by_image'][shot['image_id']],file_sha256(shot['image_path']))
                rows.append((i,a,b))
        if sum(b-a for _,a,b in rows)!=count:raise ValueError('sample shot coverage mismatch')
        specs=[(s,MotionSpec(**dict(s.get('motion',{}),geometry_version='fit_cover_v2'))) for s in modified['shots']]
        stem=item['id'];print(f'{stem}: encoding {count} frames',flush=True)
        silent=dest/(stem+'-video.mp4')
        _encode_v2_segment(rows,specs,modified['subtitles'],sr,silent,codec,300,overlays,highlights)
        audio=dest/(stem+'.wav');a=first*sr//25;b=last*sr//25
        stride=params.nchannels*params.sampwidth
        excerpt=pcm[a*stride:b*stride]
        with wave.open(str(audio),'wb') as w:
            w.setparams(params);w.writeframes(excerpt)
        baseline=review/(stem+'-current.mp4');animated=review/(stem+'-animated.mp4')
        # Decode/trim the actual current candidate; both sides get the exact same
        # PCM excerpt and AAC encoder settings, with no fades or time stretching.
        run(['-i',plan['current_video'],'-i',audio,'-filter:v',
             f'trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS',
             '-map','0:v:0','-map','1:a:0',*codec,'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k',
             '-movflags','+faststart',baseline])
        run(['-i',silent,'-i',audio,'-map','0:v:0','-map','1:a:0','-c:v','copy',
             '-c:a','aac','-b:a','192k','-movflags','+faststart',animated])
        for p in (baseline,animated):
            streams=probe(p);video=next(s for s in streams if s['codec_type']=='video')
            if (int(video['nb_read_frames']),video['width'],video['height'],video['r_frame_rate'])!=(count,1080,1920,'25/1'):
                raise ValueError('encoded frame geometry mismatch')
            run(['-i',p,'-f','null','-'])
        if decoded_pcm(baseline)!=decoded_pcm(animated):raise ValueError('A/B decoded audio differs')
        with wave.open(str(audio),'rb') as w:
            assert w.getnframes()==b-a and w.readframes(w.getnframes())==excerpt
        pair=review/(stem+'-comparison.mp4')
        run(['-i',baseline,'-i',animated,'-filter_complex',
             '[0:v]scale=540:960[a];[1:v]scale=540:960[b];[a][b]hstack=inputs=2[v]',
             '-map','[v]','-map','1:a:0',*codec,'-c:a','copy','-movflags','+faststart',pair])
        for name,p in [('current',baseline),('animated',animated)]:
            run(['-i',p,'-frames:v','1','-vf','scale=360:640',review/(stem+'-'+name+'.jpg')])
        results.append(dict(id=stem,title=item['title'],frames=count,duration=count/25,
                            source_frames=[first,last],source_seconds=[first/25,last/25],
                            pcm_samples=b-a,pcm_matches_current_mix=True,decoded_ab_audio_equal=True,
                            files={p.name:file_sha256(p) for p in (baseline,animated,pair,audio)},
                            animation=item['animation_by_image']))
        atomic_json(dest/'progress.json',{'completed':results})
        print(f'{stem}: video/audio verified',flush=True)
    check_inputs()
    receipt=dict(status='TECHNICALLY_VALID_SAMPLE',approved=False,art_approved=False,
                 provider_calls=0,inputs=inputs,samples=results,
                 runtime={'python':sys.version.split()[0], 'ffmpeg':subprocess.run(
                     ['ffmpeg','-version'],capture_output=True,text=True,check=True).stdout.splitlines()[0]},
                 subtitles='original full-timeline text, geometry and word clock validated',
                 current_version=plan['current_video'],audio_source=plan['current_audio'])
    atomic_json(dest/'samples.json',receipt)
    print('DONE: 3 samples, source inputs unchanged, original mix retained',flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--plan',required=True);ap.add_argument('--output',required=True)
    args=ap.parse_args();build(args.plan,args.output)
