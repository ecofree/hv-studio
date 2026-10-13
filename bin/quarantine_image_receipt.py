r"""Quarantine a paid-but-unusable saved image response.

Why this script exists
---------------------
A provider response is paid evidence: once `*.response.json` exists, the image
pipeline refuses to issue another request for that fingerprint. When the saved
response itself is bad (xlc-01 XL22b1, 2026-10-13: provider returned 1086x1448
for a 1080x1920 request), the job is stuck forever -- retrying is impossible
and deleting the receipt would destroy the proof that money was spent.

This moves the whole candidate directory to `images/_quarantine/<fp>/` with an
event record. Nothing is deleted. After quarantine the job is simply missing
again, so the next run re-requests it against the retry allowance
(`--max-image-retries`).

Usage
-----
    .venv-core\Scripts\python.exe bin\quarantine_image_receipt.py \
        --receipt episodes/xlc-01/production/v2/images/<fp>/XL22b1.png.response.json \
        --reason "provider returned 1086x1448 for 1080x1920; dimensions rejected"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.image import quarantine_saved_response  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Quarantine a paid-but-unusable saved image response")
    ap.add_argument("--receipt", required=True, type=Path,
                    help="The *.response.json that fails to install")
    ap.add_argument("--reason", required=True,
                    help="Why it is unusable; recorded in quarantine.json")
    args = ap.parse_args()
    dst = quarantine_saved_response(args.receipt, args.reason)
    print(f"quarantined -> {dst}")
    print("The job is now missing and will be re-requested on the next run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
