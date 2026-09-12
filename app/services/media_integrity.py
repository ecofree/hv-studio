"""Technical stream integrity, kept separate from content/listening approval."""
import json
import subprocess
from pathlib import Path
from app.services.provenance import file_sha256


def verify_media(path, expected_frames=None, expected_samples=None, sample_rate=None,
                 width=1080, height=1920, timeout=300):
    path = Path(path)
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
    return {"status":"passed","sha256":file_sha256(path),"streams":info["streams"],
            "content_review":"pending"}
