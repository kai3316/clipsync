"""Ask a sidecar binary what version it reports.

The window's version comes from the sidecar's status, not from the bundle, so a sidecar
built from an older checkout makes a current application show an old version -- measured
here: a 1.0.45 host displayed "版本 1.0.33", because the sidecar staged beside it had been
built two weeks earlier.

Three things are easy to get wrong, and each cost a failed attempt while writing this:

  * frames are **newline-delimited JSON**, not length-prefixed -- `encode_frame` appends a
    newline -- so reading a 4-byte length first finds nothing;
  * a request needs `"type": "request"`, or the sidecar answers `PROTOCOL_ERROR`;
  * the sidecar **refuses to start while the application is running**, because the data
    directory is locked; it answers `DATA_IN_USE` and exits 2.

The version cannot be read out of the file: PyInstaller compresses its archive, so a byte
search finds neither the old number nor the new one.  Running it is the only way.

Usage:
    python scripts/probe_sidecar_version.py [path-to-sidecar]
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "desktop" / "src-tauri" / "sidecar" / "clipsync-sidecar.exe"


def read_version(binary: Path, seconds: float = 25.0) -> tuple[str | None, str]:
    """The version the sidecar reports, and a sentence about what happened.

    Returns `(version, note)`.  `version` is None when the sidecar refused or never
    answered, and `note` says which so the caller does not have to guess.
    """
    process = subprocess.Popen(
        [str(binary), "--history-only"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = process.stdout.readline().decode("utf-8", "replace").strip()
        if not ready:
            return None, "the sidecar wrote nothing on startup"
        with contextlib.suppress(ValueError):
            frame = json.loads(ready)
            if frame.get("type") == "fatal":
                error = frame.get("error") or {}
                if error.get("code") == "DATA_IN_USE":
                    return None, "the data directory is in use; stop ClipSync first"
                return None, f"refused: {error.get('code')} - {error.get('message')}"

        request = {"type": "request", "id": "1", "method": "app.status", "params": {}}
        process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        process.stdin.flush()

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            raw = process.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if frame.get("id") == "1":
                result = frame.get("result") or {}
                return result.get("version"), "answered app.status"
        return None, f"no answer within {seconds:.0f}s"
    finally:
        process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    binary = Path(args[0]) if args else DEFAULT_BINARY
    if not binary.is_file():
        print(f"no such binary: {binary}")
        return 2
    version, note = read_version(binary)
    print(f"binary:  {binary}")
    print(f"note:    {note}")
    if version:
        print(f"reports: {version}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
