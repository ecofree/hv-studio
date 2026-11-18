"""Retired unbudgeted cloud demo. Existing audition audio is preserved."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    from bin.studio_narrate import main
    raise SystemExit(main())
