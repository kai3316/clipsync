"""Stage and apply a downloaded ClipSync update (per platform).

M1 of auto-update: after ``updater.download_latest_release()`` saves and
verifies the release asset, stage it (extract the binary/bundle) and apply it
(replace the running binary/bundle, then relaunch).

Per-platform reality:
- Windows (one-file ``clipsync.exe``) — a running exe is locked, so a detached
  helper retries ``move`` until the process exits, then relaunches.
- Linux (single binary) — ``os.replace`` works over a running binary (the old
  inode stays until the process exits), so no helper is needed.
- macOS (``clipsync.app``, adhoc-signed) — without Developer ID / notarization
  a silent in-place replace triggers Gatekeeper; hand the extracted bundle to
  the user instead.

The callers are the update-check/download flows in ``src/main.py``; in a
non-frozen (source) run everything is a no-op.
"""

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _current_exe() -> Path:
    return Path(sys.executable)


def stage_update(asset_path: str) -> Path | None:
    """Extract *asset_path* and return the staged binary/bundle path.

    Windows zip → ``clipsync.exe.new`` (next to the running exe); macOS zip →
    ``clipsync.app`` in a ``staged`` dir; Linux tar.gz → the ``clipsync``
    binary in a ``staged`` dir. Returns None on any failure (never raises).
    """
    if not _is_frozen():
        logger.info("Not a frozen build — update staging is a no-op")
        return None

    ap = Path(asset_path)
    try:
        if sys.platform == "win32":
            with zipfile.ZipFile(ap) as z:
                member = next(
                    n for n in z.namelist() if n.lower().endswith("clipsync.exe")
                )
                dest = _current_exe().with_name("clipsync.exe.new")
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            return dest

        if sys.platform == "darwin":
            staged_dir = ap.parent / "staged"
            with zipfile.ZipFile(ap) as z:
                z.extractall(staged_dir)
            app = next((staged_dir).glob("*.app"))
            return app

        # Linux
        staged_dir = ap.parent / "staged"
        with tarfile.open(ap, "r:gz") as t:
            t.extractall(staged_dir)
        return staged_dir / "clipsync"
    except Exception as exc:
        logger.warning("stage_update failed for %s: %s", asset_path, exc)
        return None


def apply_and_restart(staged: Path) -> bool:
    """Replace the running binary/bundle with *staged* and relaunch.

    Returns True if the apply was launched (the caller should exit the app);
    False on failure.
    """
    if not _is_frozen():
        logger.info("Not a frozen build — update apply is a no-op")
        return False

    try:
        if sys.platform == "win32":
            return _apply_windows(staged)
        if sys.platform == "darwin":
            return _apply_macos(staged)
        return _apply_linux(staged)
    except Exception as exc:
        logger.warning("apply_and_restart failed for %s: %s", staged, exc)
        return False


def _apply_windows(staged: Path) -> bool:
    cur = _current_exe()
    bat = cur.with_name("clipsync-update.bat")
    script = (
        "@echo off\n"
        ":retry\n"
        "timeout /t 1 /nobreak >nul\n"
        f'move /y "{staged}" "{cur}" >nul 2>&1\n'
        f'if exist "{staged}" goto retry\n'
        f'start "" "{cur}"\n'
    )
    try:
        bat.write_text(script)
    except OSError as exc:
        logger.warning("Failed to write update helper: %s", exc)
        return False
    subprocess.Popen(
        ["cmd", "/c", str(bat)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
    logger.info("Windows update helper launched; waiting to replace %s", cur)
    return True


def _apply_linux(staged: Path) -> bool:
    cur = _current_exe()
    os.replace(staged, cur)
    os.chmod(cur, 0o755)
    subprocess.Popen([str(cur)], close_fds=True, start_new_session=True)
    logger.info("Linux update applied; relaunched %s", cur)
    return True


def _apply_macos(staged: Path) -> bool:
    # No Developer ID / notarization → a silent replace would re-trigger
    # Gatekeeper. Hand the extracted bundle's folder to the user instead.
    subprocess.Popen(["open", str(staged.parent)])
    logger.info("macOS update staged at %s; opened for manual install", staged.parent)
    return True
