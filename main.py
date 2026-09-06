"""ClipSync — entry point redirect.

Run with: python main.py  (or)  python src/main.py
"""

import runpy
import sys
from pathlib import Path

_ENTRY = Path(__file__).resolve().parent / "src" / "main.py"
if not _ENTRY.exists():
    print(f"Error: entry point not found at {_ENTRY}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
runpy.run_path(str(_ENTRY), run_name="__main__")
