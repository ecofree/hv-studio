"""Fetch the pinned public ASR model and verify every file before installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import urlopen

REPOSITORY='Systran/faster-whisper-small'
REVISION='536b0662742c02347bc0e980a01041f333bce120'
FILES={
    'config.json':'b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828',
    'tokenizer.json':'fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab',
    'vocabulary.txt':'34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913',
    'model.bin':'3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671',
}


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',type=Path,default=Path('.cache/asr/faster-whisper-small'))
    parser.add_argument('--mirror',action='store_true',help='Download public bytes via hf-mirror.com; checksums remain pinned to the original model')
    args=parser.parse_args();root=args.model_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    host='hf-mirror.com' if args.mirror else 'huggingface.co'
    for name,expected in FILES.items():
        target=root/name
        if target.exists() and sha(target)==expected:continue
        pending=root/(name+'.download-part');start=last=time.monotonic();size=0
        with urlopen(f'https://{host}/{REPOSITORY}/resolve/{REVISION}/{name}',timeout=30) as response,pending.open('wb') as f:
            while chunk:=response.read(65536):
                f.write(chunk);size+=len(chunk)
                if time.monotonic()-last>25:print(name,round(size/1e6,1),'MB',flush=True);last=time.monotonic()
                if time.monotonic()-start>1200:raise TimeoutError('ASR download deadline exceeded')
            f.flush();os.fsync(f.fileno())
        if sha(pending)!=expected:raise ValueError('model checksum mismatch: '+name)
        os.replace(pending,target)
    manifest=root/'model_manifest.json'
    if not manifest.exists():
        manifest.write_text(json.dumps({'source':{'repository':REPOSITORY,'revision':REVISION},
            'files':{n:{'sha256':h,'bytes':(root/n).stat().st_size} for n,h in FILES.items()}},indent=2),encoding='utf-8')
    print('Verified model:',root)


if __name__=='__main__':main()
