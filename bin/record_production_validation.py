"""Compile real scenario run files into production-validation.json.

This command does not execute providers, inject faults, or manufacture events.
Each scenario directory must already contain run.json, events.json and
artifacts.json written by the corresponding real test/production harness.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--episodes", nargs="+", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--output", default="state/acceptance/production-validation.json")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.git_revision):
        raise SystemExit("git revision must be a full 40-character SHA")
    from app.paths import validate_episode_id
    for episode in args.episodes:
        validate_episode_id(episode)
    from app.services.production_acceptance import collect_validation
    from app.services.provenance import atomic_json
    record = collect_validation(args.source_fingerprint, args.git_revision,
                                args.episodes, args.evidence_root)
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = ROOT/destination
    atomic_json(destination, record)
    print(json.dumps({"status":"PASS", "output":str(destination.resolve()),
                      "scenarios":sorted(record["scenario_evidence"]),
                      "episodes":record["episodes"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
