"""Generate the bundle config the way CI does, so a local build matches a released one.

`desktop/src-tauri/tauri.conf.json` has no version that tracks the application -- it
says `0.1.0` while `internal/version.py` says 1.0.42 -- and CI never uses it alone.
The Windows job writes `build/tauri-package.json` with the real version and the sidecar
staged as a resource directory, then builds with `--config` pointing at it.  Building
without that gives `ClipSync_0.1.0_x64-setup.exe` and no sidecar, which is what a bare
`npx tauri build` produced here.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from internal.version import __version__  # noqa: E402

# Windows stages the sidecar as a directory (CLIPSYNC_SIDECAR_ONEDIR=1 in CI), because
# a onefile build has nowhere to put the `_internal` tree beside it.
bundle = {"resources": ["sidecar"]}

out = ROOT / "build"
out.mkdir(exist_ok=True)
config = out / "tauri-package.json"
config.write_text(
    json.dumps({"version": __version__, "bundle": bundle}, indent=2),
    encoding="utf-8",
)
print(f"wrote {config}")
print(config.read_text(encoding="utf-8"))
