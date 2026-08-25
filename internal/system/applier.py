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

import errno
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


def _build_windows_bat(staged: Path, cur: Path) -> str:
    """Return the helper script text for the Windows self-replace.

    Kept a pure function so tests can assert on the generated steps (notably
    the ``.old`` fallback copy) without spawning cmd.exe.  Before the replace
    loop the helper copies the CURRENT exe to ``<exe>.old`` (copy, not move —
    the running exe stays in place) so the user can manually roll back a bad
    update; no automatic health check is done on it.
    """
    old = Path(str(cur) + ".old")
    return (
        "@echo off\n"
        # Fallback copy: keep the current binary as .old before anything is
        # replaced.  A copy of the locked (running) exe is allowed.
        f'copy /y "{cur}" "{old}" >nul 2>&1\n'
        "set /a tries=0\n"
        ":retry\n"
        "timeout /t 1 /nobreak >nul\n"
        f'move /y "{staged}" "{cur}" >nul 2>&1\n'
        f'if not exist "{staged}" goto launch\n'
        "set /a tries+=1\n"
        "if %tries% geq 300 goto launch\n"
        "goto retry\n"
        ":launch\n"
        # Run the new exe DIRECTLY (not via `start ""`).  PyInstaller onefile
        # bootloaders validate their parent process at startup; `start` hands
        # the child to cmd and then this shell exits immediately, so the
        # relaunched exe's parent is already gone and it dies with
        # "Security validation failure: failed to obtain executable path for
        # parent process".  Running it here keeps THIS cmd alive as its parent
        # for the whole app lifetime, so the validation always resolves.
        f'"{cur}"\n'
        'del "%~f0"\n'
    )


def _apply_windows(staged: Path) -> bool:
    cur = _current_exe()
    bat = cur.with_name("clipsync-update.bat")
    # Retry the replace for up to 5 minutes while the running exe exits, then
    # give up and relaunch the CURRENT binary (better a working old version
    # than an immortal helper looping forever -- e.g. if antivirus quarantined
    # the staged file).  ``del "%~f0"`` removes the helper itself afterwards
    # instead of leaving clipsync-update.bat next to the app forever.
    script = _build_windows_bat(staged, cur)
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


def _backup_current_binary(cur: Path) -> Path | None:
    """Copy the current Linux binary to ``<name>.old`` before replacing it.

    Purely a manual-rollback convenience for the user (no automatic health
    check reads it back).  Best-effort: a failed copy logs and returns None
    but must never block the update itself.
    """
    old = cur.with_name(cur.name + ".old")
    try:
        shutil.copyfile(cur, old)
        logger.info("Backed up current binary to %s", old)
        return old
    except OSError as exc:
        logger.warning("Could not back up current binary to %s: %s", old, exc)
        return None


def _apply_linux(staged: Path) -> bool:
    cur = _current_exe()
    # Keep the running binary as .old for manual rollback.  os.replace below
    # swaps the directory entry, so the copy must happen first.
    _backup_current_binary(cur)
    try:
        os.replace(staged, cur)
    except OSError as exc:
        # The staged dir may live on a different filesystem (a tmpfs /tmp or a
        # separate /home), and os.replace can't move across filesystems.  Copy
        # into a temp name next to the target, then swap within that same
        # directory.  The .old backup and the chmod below still apply.
        if exc.errno != errno.EXDEV:
            raise
        logger.warning(
            "Cross-filesystem staged update %s → %s (%s); copying instead",
            staged, cur, exc,
        )
        tmp = cur.with_name(cur.name + ".new")
        shutil.copy2(staged, tmp)
        os.replace(tmp, cur)
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
