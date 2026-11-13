"""Technical stream integrity, kept separate from content/listening approval."""
import json
import subprocess
import shutil
from functools import lru_cache
from pathlib import Path
from app.services.provenance import file_sha256


@lru_cache(maxsize=8)
def _tool_hash(path, size, changed, modified):
    return file_sha256(path)


def verification_contract(frames, samples, rate, width=1080, height=1920):
    tools={}
    for name in ('ffprobe','ffmpeg'):
        path=shutil.which(name)
        if not path:raise ValueError('missing verification tool: '+name)
        stat=Path(path).stat()
        tools[name]=_tool_hash(path,stat.st_size,stat.st_ctime_ns,stat.st_mtime_ns)
    return dict(tools=tools,validator_sha256=file_sha256(__file__), expected_frames=frames,
                expected_samples=samples, sample_rate=rate, width=width, height=height)


def verify_media(path, expected_frames=None, expected_samples=None, sample_rate=None,
                 width=1080, height=1920, timeout=300):
    path = Path(path)
    contract=verification_contract(expected_frames,expected_samples,sample_rate,width,height)
    before = file_sha256(path)
    probe = subprocess.run(["ffprobe","-v","error","-count_packets","-show_streams",
                            "-show_format","-of","json",str(path)],
                           check=True,capture_output=True,text=True,timeout=timeout)
    info = json.loads(probe.stdout)
    videos=[s for s in info["streams"] if s["codec_type"]=="video"]
    audios=[s for s in info["streams"] if s["codec_type"]=="audio"]
    if len(videos)!=1 or len(audios)!=1:
        raise ValueError("candidate requires exactly one video and one audio stream")
    video,audio=videos[0],audios[0]
    if (video["width"],video["height"])!=(width,height):
        raise ValueError("candidate dimensions mismatch")
    if expected_frames is not None and int(video["nb_read_packets"])!=expected_frames:
        raise ValueError("candidate frame count mismatch")
    if expected_samples is not None:
        expected = expected_samples/sample_rate
        if abs(float(video["duration"])-expected) > 1/25 + .005:
            raise ValueError("video drifts from sample timeline")
        if abs(float(audio["duration"])-expected) > .08:
            raise ValueError("audio stream duration mismatch")
    subprocess.run(["ffmpeg","-v","error","-xerror","-i",str(path),
                    "-map","0:v:0","-map","0:a:0","-f","null","-"],
                   check=True,capture_output=True,timeout=timeout)
    if file_sha256(path) != before:
        raise ValueError("candidate changed during full decode")
    if contract!=verification_contract(expected_frames,expected_samples,sample_rate,width,height):
        raise ValueError("verification tool changed during full decode")
    return {"status":"passed","sha256":before,"path":str(path.resolve()),"streams":info["streams"],
            "contract":contract,
            "content_review":"pending"}


def verify_for_run(receipt, decoded_here=None, *, timeout=300):
    """Caller has validated the current input hashes, candidate bytes and timeline."""
    frames,samples,rate=(receipt[k] for k in ('expected_frames','samples','sample_rate'))
    integrity=receipt.get('integrity',{})
    if (decoded_here and integrity==decoded_here and integrity.get('status')=='passed'
            and integrity.get('path')==str(Path(receipt['candidate_path']).resolve())
            and integrity.get('sha256')==receipt['candidate_sha256']
            and integrity.get('contract')==verification_contract(frames,samples,rate)):
        return 'full_decode_in_this_run'
    verify_media(receipt['candidate_path'],expected_frames=frames,expected_samples=samples,sample_rate=rate,timeout=timeout)
    return 'independent_full_decode'
