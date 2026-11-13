r"""Quarantine a genuinely unusable saved image response, preserving evidence.

Size/aspect differences alone are NOT a reason to quarantine or pay again.
Use bin/recover_image_receipt.py first: valid source plates are locally fitted
to the delivery canvas. This command is for corrupt/unrecoverable responses.

This preserves irrecoverable responses for investigation. Quarantine makes the
job missing again, so a later explicitly budgeted run can request a replacement.
Do not use it to work around a normal size/aspect difference; recover and fit
existing image bytes first.

Usage
-----
    .venv-core\Scripts\python.exe bin\quarantine_image_receipt.py \
        --receipt episodes/xlc-01/production/v2/images/<fp>/XL22b1.png.response.json \
        --reason "saved response bytes cannot be decoded; original evidence retained"
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
