"""Explicit speed lineage and strict audio-only comparisons.

Planning/probing is read-only. Applying a plan is a separate production action.
Filename suffixes and a playable container never prove speed or fair comparison.
"""
from __future__ import annotations

import json
import math
import subprocess
import wave
from fractions import Fraction
from pathlib import Path

from app.services.provenance import atomic_json, file_sha256, fingerprint


def wav_clock(path):
    try:
        with wave.open(str(path), 'rb') as w:
            if w.getnframes()<=0 or w.getframerate()<=0:raise ValueError('音轨没有有效采样')
            return dict(samples=w.getnframes(), sample_rate=w.getframerate(),
                        channels=w.getnchannels(), sample_width=w.getsampwidth())
    except (wave.Error, EOFError) as exc:
        raise ValueError('速度时钟检查需要有效的 PCM WAV 文件') from exc


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .25 <= value <= 4:
        raise ValueError('速度必须是 0.25–4 之间的明确数值')
    return float(value)


def record_speed(audio, receipt, *, applied_rate, evidence, source_identity, parent=None):
    """Explicit provenance registration, never an inference from the filename."""
    if not evidence.strip() or not source_identity:
        raise ValueError('速度记录需要来源依据和文案版本')
    value=dict(version=1,path=str(Path(audio).resolve()),sha256=file_sha256(audio),
               clock=wav_clock(audio),applied_rate=_rate(applied_rate),evidence=evidence,
               source_identity=source_identity,parent=parent,approved=False)
    receipt=Path(receipt)
    if receipt.exists() and json.loads(receipt.read_text('utf-8'))!=value:
        raise ValueError('已有速度记录不覆盖，请使用独立版本')
    atomic_json(receipt,value)
    return receipt


def read_speed(audio, receipt, _seen=None):
    if not receipt or not Path(receipt).is_file():
        raise ValueError('速度来源不明；请先登记可核对的速度记录，不按文件名猜测')
    _seen=set() if _seen is None else _seen
    key=str(Path(receipt).resolve())
    if key in _seen:raise ValueError('速度来源链出现循环')
    _seen.add(key)
    r=json.loads(Path(receipt).read_text('utf-8'))
    if r.get('version')!=1 or r.get('approved') is not False:
        raise ValueError('无效速度记录')
    _rate(r['applied_rate'])
    if (Path(audio).resolve()!=Path(r['path']).resolve() or file_sha256(audio)!=r['sha256']
            or wav_clock(audio)!=r['clock'] or not r.get('source_identity') or not r.get('evidence')):
        raise ValueError('速度记录与实际音轨不一致')
    if r.get('parent'):
        p=r['parent']
        if file_sha256(p['receipt'])!=p['receipt_sha256']:raise ValueError('上游速度记录已改变')
        ancestor=read_speed(p['path'],p['receipt'],_seen)
        if not math.isclose(ancestor['applied_rate']*p['factor'],r['applied_rate'],abs_tol=1e-9):
            raise ValueError('累计速度与上游变换不一致')
        if r['source_identity']!=ancestor['source_identity']:raise ValueError('速度变换不能变更文案版本')
    return r


def speed_plan(audio, receipt, target_rate=1.2, *, subtitles_aligned=False):
    r=read_speed(audio,receipt);target=_rate(target_rate);factor=target/r['applied_rate']
    same=math.isclose(factor,1,abs_tol=1e-9)
    if subtitles_aligned and not same:
        raise ValueError('字幕已对齐；禁止改变音轨时钟。须建立新版本并在最终速度上重新对齐')
    remaining=factor;filters=[]
    while remaining>2:filters.append('atempo=2');remaining/=2
    while remaining<.5:filters.append('atempo=0.5');remaining/=.5
    if not same:filters.append(f'atempo={remaining:.12g}')
    return dict(version=1,input_path=r['path'],input_sha256=r['sha256'],
        receipt_path=str(Path(receipt).resolve()),receipt_sha256=file_sha256(receipt),
        source_identity=r['source_identity'],applied_rate=r['applied_rate'],target_rate=target,
        factor=factor,operation='reuse' if same else 'tempo',audio_filter=','.join(filters) or None,
        align_subtitles_after_speed=not same,expected_seconds=r['clock']['samples']/r['clock']['sample_rate']/factor)


def apply_speed(plan, destination):
    """Production-only transform. Not called by imports, previews, or audit UI."""
    fresh=speed_plan(plan['input_path'],plan['receipt_path'],plan['target_rate'])
    if fresh!=plan:raise ValueError('速度方案已过期，请重新检查')
    source=Path(plan['input_path'])
    if plan['operation']=='reuse':return source,Path(plan['receipt_path'])
    dest=Path(destination)
    if dest.exists():raise ValueError('速度输出已存在，不覆盖旧音轨')
    dest.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['ffmpeg','-nostdin','-v','error','-n','-i',str(source),'-af',plan['audio_filter'],
                    '-c:a','pcm_s16le',str(dest)],check=True,capture_output=True,timeout=300)
    actual=wav_clock(dest)
    if abs(actual['samples']/actual['sample_rate']-plan['expected_seconds'])>max(.1,plan['expected_seconds']*.002):
        raise ValueError('速度处理结果时长不符，保留待查文件，不登记为有效音轨')
    receipt=dest.with_suffix('.speed.json')
    record_speed(dest,receipt,applied_rate=plan['target_rate'],evidence='显式速度方案 '+fingerprint(plan),
        source_identity=plan['source_identity'],parent=dict(path=str(source),receipt=plan['receipt_path'],
            receipt_sha256=plan['receipt_sha256'],factor=plan['factor']))
    return dest,receipt


def inspect_video(path):
    """Read encoded packets without decoding/rendering. Stream-copy A/B proof."""
    try:
        result=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_streams',
            '-show_packets','-show_data_hash','sha256','-show_entries',
            'stream=codec_name,width,height,pix_fmt,nb_frames,time_base,r_frame_rate,extradata_hash:packet=pts,dts,duration,data_hash',
            '-of','json',str(path)],capture_output=True,text=True,check=True,timeout=30)
    except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
        raise ValueError('视频无法完成内容与时间检查，请检查文件或稍后重试') from exc
    data=json.loads(result.stdout)
    if len(data.get('streams',[]))!=1 or not data.get('packets'):raise ValueError('缺少可核对的视频流')
    stream=data['streams'][0];tb=Fraction(stream['time_base']);packets=[]
    for row in data['packets']:
        if not all(k in row for k in ('pts','dts','duration','data_hash')):raise ValueError('视频包时钟或内容指纹缺失')
        packets.append(dict(pts=str(int(row['pts'])*tb),dts=str(int(row['dts'])*tb),
                            duration=str(int(row['duration'])*tb),data_hash=row['data_hash']))
    return dict(path=str(Path(path).resolve()),sha256=file_sha256(path),
        geometry={k:stream[k] for k in ('codec_name','width','height','pix_fmt')},
        frames=int(stream['nb_frames']) if str(stream.get('nb_frames','')).isdigit() else None,
        packets=len(packets),content_sha256=fingerprint([p['data_hash'] for p in packets]),
        timing_sha256=fingerprint([{k:p[k] for k in ('pts','dts','duration')} for p in packets]),
        start=str(min(Fraction(p['pts']) for p in packets)),
        end=str(max(Fraction(p['pts'])+Fraction(p['duration']) for p in packets)),
        extradata_hash=stream.get('extradata_hash'))


def compare_video_probes(before,after):
    errors=[]
    for key,label in [('geometry','编码/画面尺寸'),('frames','帧数'),('packets','视频包数'),
                      ('content_sha256','视频内容'),('timing_sha256','逐包时间'),('start','起点'),
                      ('end','结束点'),('extradata_hash','编码参数')]:
        if before.get(key) is None or after.get(key) is None or before[key]!=after[key]:errors.append(label+'不一致或缺少证据')
    return dict(ok=not errors,scope='audio_only_same_video',issues=errors,before=before,after=after,approved=False)


def validate_audio_pair(before_wav,after_wav,before_speed,after_speed,subtitle_before,subtitle_after):
    a=read_speed(before_wav,before_speed);b=read_speed(after_wav,after_speed)
    errors=[]
    if a['clock']!=b['clock']:errors.append('音频采样时钟不一致')
    if a['source_identity']!=b['source_identity']:errors.append('文案不同，只能标记为历史参照，不能用于单变量声音对照')
    if not math.isclose(a['applied_rate'],b['applied_rate'],abs_tol=1e-9):errors.append('累计速度不一致')
    if file_sha256(subtitle_before)!=file_sha256(subtitle_after):errors.append('字幕内容或时间不同')
    return dict(ok=not errors,issues=errors,approved=False,listening_review='pending',
                scope='source_speed_subtitle_and_sample_clock_only',audio_content_alignment='not_verified')


def remux_plan(video, audio, output):
    """Preserve every encoded video packet; pad silence only, never -shortest."""
    video=Path(video);audio=Path(audio);output=Path(output)
    if output.resolve() in (video.resolve(),audio.resolve()) or output.exists():raise ValueError('对照输出不能覆盖已有素材')
    probe=inspect_video(video);clock=wav_clock(audio)
    if Fraction(probe['start'])!=0:raise ValueError('视频起点不是零，需要先明确对照时钟')
    n=math.ceil(Fraction(probe['end'])*clock['sample_rate'])
    if clock['samples']>n:raise ValueError('音频比固定画面长；禁止截掉声音或裁短画面凑对照')
    args=['ffmpeg','-nostdin','-v','error','-n','-i',str(video),'-i',str(audio),'-map','0:v:0','-map','1:a:0',
          '-c:v','copy','-c:a','aac','-af',f'apad=whole_len={n}',str(output)]
    return dict(video_sha256=probe['sha256'],audio_sha256=file_sha256(audio),
                pad_silence_samples=n-clock['samples'],argv=args,
                requires_postcheck='compare_video_probes + validate_audio_pair',approved=False)
