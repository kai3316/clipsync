"""Check whether the ClipSync desktop application is actually installed.

Written after a real failure on this machine, and the reason it exists is that the
failure is invisible: every other kind of evidence said the application was fine.
The registry had an install path, a version and an uninstall entry; `%TEMP%` held
three generations of updater downloads; the sidecar was present and 51 MB of Python
runtime sat beside it.  What was missing was `clipsync-desktop.exe` -- the window
itself -- and nothing reported that, because the installer had exited 0.

What went wrong, for the record: a stale `HKCU\\Software\\clipsync\\ClipSync` key
existed while `%LOCALAPPDATA%\\ClipSync` was half-removed (a left-behind sidecar and
uninstall.exe, no main binary).  In that state the installer wrote the sidecar and the
uninstaller and exited successfully without the main executable, and the next run
reported "error opening file for writing" against the mess it had made.  Removing both
the directory and the registry key, then installing to the default location, produced
`clipsync-desktop.exe` (17.7 MB) as it should.

Usage:
    python scripts/check_install.py             # human-readable
    python scripts/check_install.py --json      # machine-readable

Exit code 0 when the application is installed and launchable, 1 otherwise, so it can
be used as a gate in a script.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The Tauri bundle identifier.  The window's WebView data lives under this name, and
# its presence without the application is one of the fingerprints of a half-removed
# install.
BUNDLE_ID = "com.clipsync.desktop"
# The binary the NSIS installer places, and the name the uninstall entry records.
MAIN_BINARY = "clipsync-desktop.exe"

UNINSTALL_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\ClipSync"
INSTALL_PATH_KEY = r"HKCU\Software\clipsync\ClipSync"


def _reg_query(key: str, value: str | None = None) -> str | None:
    """One registry value, or the key's default, or None.  Windows only."""
    if os.name != "nt":
        return None
    args = ["reg", "query", key]
    if value:
        args += ["/v", value]
    else:
        args += ["/ve"]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        # `REG_SZ` / `REG_EXPAND_SZ` followed by the value; the value may contain
        # spaces, so everything after the type is the answer.
        for kind in ("REG_SZ", "REG_EXPAND_SZ", "REG_DWORD"):
            if kind in line:
                return line.split(kind, 1)[1].strip()
    return None


def _running() -> list[str]:
    """Process names matching ClipSync, without a dependency on psutil."""
    if os.name != "nt":
        return []
    try:
        done = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in done.stdout.splitlines():
        first = line.split(",", 1)[0].strip().strip('"')
        if "clipsync" in first.lower():
            names.append(first)
    return names


def diagnose(state: dict) -> list[str]:
    """The failure shapes, as a function of what was found.

    Kept apart from `collect` so it can be tested: this repository's CI has no
    ClipSync installed, so a test that had to run the real checks could only ever
    assert the absence of the application.  The shapes below are the ones actually
    seen on a machine, and the last two are what makes the failure invisible --
    each is, on its own, evidence that the application is fine.
    """
    problems: list[str] = []
    install_dir_present = bool(state["install_dir_present"])

    if not state["main_present"]:
        problems.append(
            f"the application binary is missing: {state['main_binary']}"
            + (
                "  (the installer exits 0 in this state, so its exit code proves nothing)"
                if install_dir_present
                else "  (there is no install directory either)"
            )
        )
    if state["uninstall_entry"] and not install_dir_present:
        problems.append(
            "the registry records an installed version but its directory is gone: "
            "a half-removed install, which is the state that makes the installer "
            "fail with 'error opening file for writing'"
        )
    if not state["main_present"] and state["sidecar_present"]:
        problems.append(
            "the sidecar is installed and the window is not, which is the exact "
            "shape of the failure this script was written for"
        )
    if state["webview_data"] and not state["main_present"]:
        problems.append(
            f"WebView data exists for a window that is not installed: {state['webview_data']}"
        )
    return problems


def collect() -> dict:
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")

    # Where the uninstall entry says it went, falling back to the default the
    # installer uses when it was never told otherwise.
    recorded = _reg_query(INSTALL_PATH_KEY) or ""
    install_dir = Path(recorded) if recorded else Path(local) / "ClipSync"

    main = install_dir / MAIN_BINARY
    sidecar = install_dir / "sidecar" / "clipsync-sidecar.exe"
    webview = Path(local) / BUNDLE_ID

    state = {
        "install_dir": str(install_dir),
        "install_dir_recorded": recorded,
        "install_dir_present": install_dir.is_dir(),
        "main_binary": str(main),
        "main_present": main.is_file(),
        "main_size": main.stat().st_size if main.is_file() else 0,
        "sidecar_present": sidecar.is_file(),
        "sidecar_size": sidecar.stat().st_size if sidecar.is_file() else 0,
        "uninstall_entry": bool(_reg_query(UNINSTALL_KEY, "DisplayVersion")),
        "installed_version": _reg_query(UNINSTALL_KEY, "DisplayVersion") or "",
        # A directory, present or not; the diagnosis only cares whether it is there.
        "webview_data": str(webview) if webview.is_dir() else "",
        "user_data_dir": str(Path(roaming) / "ClipSync"),
        "user_data_present": (Path(roaming) / "ClipSync").is_dir(),
        "running": _running(),
    }
    state["problems"] = diagnose(state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("ClipSync install check")
        print(f"  install directory : {report['install_dir']}")
        if report["install_dir_recorded"]:
            print(f"  recorded path     : {report['install_dir_recorded']}")
        print(
            "  application       : "
            + (
                f"present ({report['main_size'] / 1_048_576:.1f} MB)"
                if report["main_present"]
                else "MISSING"
            )
        )
        print(
            "  sidecar           : "
            + (
                f"present ({report['sidecar_size'] / 1_048_576:.1f} MB)"
                if report["sidecar_present"]
                else "missing"
            )
        )
        print(
            "  uninstall entry   : "
            + (
                f"present (version {report['installed_version'] or '?'})"
                if report["uninstall_entry"]
                else "missing"
            )
        )
        print(f"  user data         : {'present' if report['user_data_present'] else 'missing'}")
        print(f"  running processes : {', '.join(report['running']) or 'none'}")
        if report["problems"]:
            print("\nProblems:")
            for problem in report["problems"]:
                print(f"  - {problem}")
            print(
                "\nTo repair: close ClipSync, delete the install directory and the key\n"
                f"  {INSTALL_PATH_KEY}\n"
                "then run the installer again.  User data is separate and is not touched."
            )
        else:
            print("\nOK: the application is installed.")

    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
