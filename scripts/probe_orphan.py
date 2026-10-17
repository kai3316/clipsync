"""Does a force-killed app leave an orphan sidecar holding the install directory?

That is the failure the user reports -- write errors during install, more often since the
sidecar became a directory -- and it decides what else is needed beyond the reorder:

  * if a normal close always takes the sidecar with it, and only a force-kill orphans it,
    the reorder covers the update path and the force-kill case wants a parent watchdog;
  * if the sidecar survives even an orderly close, the reorder is not enough on its own.

Run against the installed application, one scenario per invocation, so the harness itself
cannot confuse the two cases:

    python scripts/probe_orphan.py graceful
    python scripts/probe_orphan.py force
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

INSTALL = Path.home() / "AppData" / "Local" / "ClipSync"
EXE = INSTALL / "clipsync-desktop.exe"
SIDECAR = "clipsync-sidecar.exe"
APP = "clipsync-desktop.exe"


def processes(name: str) -> set[int]:
    """Pids of `name`, by image name, without a PowerShell round trip's quoting."""
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def wait_for(predicate, seconds: float = 30.0, interval: float = 0.5) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def stop_all() -> None:
    for name in (APP, SIDECAR):
        for pid in processes(name):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "graceful").lower()
    if not EXE.exists():
        print(f"not installed: {EXE}")
        return 2
    stop_all()
    time.sleep(2)

    print(f"mode: {mode}")
    subprocess.Popen([str(EXE)], close_fds=True)
    if not wait_for(lambda: processes(APP) and processes(SIDECAR), seconds=60):
        print("  the app or its sidecar never appeared")
        stop_all()
        return 2
    app_pids = processes(APP)
    side_pids = processes(SIDECAR)
    print(f"  started: app={sorted(app_pids)} sidecar={sorted(side_pids)}")
    # A sidecar only locks the directory once it is up; give it a moment past appearing.
    time.sleep(6)

    if mode == "graceful":
        # An orderly close: the same thing the window's close button sends.
        subprocess.run(
            ["taskkill", "/PID", str(sorted(processes(APP))[0])],
            capture_output=True,
        )
        print("  asked the app to close (no /F)")
    else:
        for pid in processes(APP):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        print("  force-killed the app (/F)")

    gone = wait_for(lambda: not processes(SIDECAR), seconds=30)
    leftover = processes(SIDECAR)
    if gone:
        print("  sidecar exited with the app")
    else:
        print(f"  ORPHAN: sidecar still running as {sorted(leftover)}")
        # Try to overwrite the file it holds, which is what an installer does.
        target = INSTALL / "sidecar" / "clipsync-sidecar.exe"
        try:
            with open(target, "r+b"):
                pass
            print("  and the install directory is still writable")
        except OSError as exc:
            print(f"  and the install directory is locked: {exc}")
    stop_all()
    return 0 if gone else 1


if __name__ == "__main__":
    raise SystemExit(main())
