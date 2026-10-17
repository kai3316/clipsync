"""Factory reset: delete every user-data file for a clean slate.

The legacy desktop host deletes the same set inline in
``src/main.py::_do_factory_reset`` (``tests/test_sync.py`` pins that list as a
tripwire). The sidecar cannot call a Tk-bound method, so the list lives here —
keep the two in sync when a new user-data file appears.
"""

import contextlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Files holding user state.  The history DB runs in WAL mode with one
# long-lived connection, so its -wal/-shm sidecars MUST go too: deleting only
# the .db lets SQLite replay the stale WAL into the freshly-created empty DB on
# the next start — resurrecting the very history the reset was meant to
# destroy.
DATA_FILES = (
    "config.json",
    "clipboard_history.json",
    "clipboard_history.db",
    "clipboard_history.db-wal",
    "clipboard_history.db-shm",
    "favorites.db",
    # Legacy favorites store: the web API migrates it into an empty
    # favorites.db, so a stale file would resurrect every favorite and group.
    "favorites.json",
    "clipsync.log",
)

# Scratch and quarantine copies hold the OLD identity / private key /
# clipboard rows — a clean slate sweeps them too.
QUARANTINE_GLOBS = (
    ".config_tmp_*.json",
    ".history_tmp_*.json",
    "config.json.corrupt-*",
    "clipboard_history.db.corrupt-*",
)

# One-shot markers the web server turns into a client-side reset: browser state
# (group registry, mutes, theme, onboarding flag) lives in the webview's
# localStorage, which this process cannot reach.  "web_fresh_pending"
# re-surfaces the web onboarding wizard exactly once.
MARKERS = ("factory_reset_pending", "web_fresh_pending")


def reset_data_dir(directory: Path) -> list[str]:
    """Delete the user-data files under *directory*; return the names removed.

    A file the process still holds open (the log handler on Windows) must not
    abort the reset of everything else, so failures are logged rather than
    raised.  Callers are expected to have released their own handles first —
    see ``SidecarApplication.factory_reset``.
    """
    deleted = []
    for name in DATA_FILES:
        path = directory / name
        try:
            if path.exists():
                path.unlink()
                deleted.append(name)
        except OSError as exc:
            logger.warning("Factory reset: failed to delete %s: %s", path, exc)
    for pattern in QUARANTINE_GLOBS:
        for path in list(directory.glob(pattern)):
            with contextlib.suppress(OSError):
                path.unlink()
    for name in MARKERS:
        try:
            (directory / name).write_text("1", encoding="utf-8")
        except OSError:
            logger.debug("Factory reset: could not write %s", name, exc_info=True)
    return deleted
