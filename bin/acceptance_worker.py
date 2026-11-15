"""Private owned child of the acceptance supervisor; not a general command hook."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.acceptance_runner import child
if __name__=='__main__':raise SystemExit(child(sys.argv[1]))
