"""Verbatim source import only; no provider, audio, timeline or rendering."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.studio_sources import import_authoring


def main():
    p=argparse.ArgumentParser(description='逐字导入用户原稿，建立独立创作版本')
    p.add_argument('episode');p.add_argument('--title',required=True)
    p.add_argument('--source',required=True);p.add_argument('--tts')
    p.add_argument('--rule',action='append',default=[])
    a=p.parse_args()
    print(import_authoring(a.episode,a.title,a.source,tts_file=a.tts,rule_files=a.rule))


if __name__=='__main__':main()
