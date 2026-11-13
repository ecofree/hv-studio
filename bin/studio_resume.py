"""Resume a stopped Studio job with its unchanged budget; never auto-retry TTS."""
import argparse,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if __name__=='__main__':
    from app.services.studio_jobs import resume
    parser=argparse.ArgumentParser(description='继续原任务，保留预算、缓存和失败记录')
    parser.add_argument('job_directory')
    args=parser.parse_args()
    print(resume(args.job_directory))
