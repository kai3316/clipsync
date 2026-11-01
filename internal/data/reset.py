"""Factory reset: delete every user-data file for a clean slate.

The legacy desktop host deletes the same set inline in
``src/main.py::_do_factory_reset``; that host is gone, so this module is now the
only list — keep it complete when a new user-data file appears.

"Every" is the hard part, and it is not a list a reader can audit: the reset
claimed a clean slate while ``phone_chat.json`` (the phone conversation),
``relay_pending.json`` (frames waiting for an offline peer, republished on the
next start) and ``aiconfig_trash/`` (deleted AI config files, with their API
keys) stayed behind.  So the set lives in one place, the reset reports what it
could not remove instead of logging it, and
``tests/test_factory_reset_scope.py`` walks the filesystem finders for new
user-data paths so the next one cannot be forgotten quietly.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Files holding user state, in the config/data directory.
#
# The history DB runs in WAL mode with one long-lived connection, so its
# -wal/-shm sidecars MUST go too: deleting only the .db lets SQLite replay the
# stale WAL into the freshly-created empty DB on the next start — resurrecting
# the very history the reset was meant to destroy.
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
    # The phone's conversation with this machine.  Read back by the companion
    # on the next launch, so a surviving file is a conversation the user
    # believed they had deleted.
    "phone_chat.json",
    # A crashed write's leftovers: each is a full copy of the state above.
    "phone_chat.json.tmp",
    # Frames queued for a peer that was offline.  The queue is reloaded at
    # startup and retried once the relay is reachable again, so a surviving
    # file republishes the clipboard contents and chat messages the reset was
    # meant to destroy — to the peers the reset just forgot.
    "relay_pending.json",
    "relay_pending.json.tmp",
    "clipsync.log",
)

# Directories, relative to the data directory, that hold user data.
#
# ``aiconfig_trash`` keeps the copies of AI config files the user deleted —
# frequently containing the API keys that made them worth deleting.
# ``.scratch`` holds temporary key material from the transport.
DATA_DIRS = (
    "aiconfig_trash",
    ".scratch",
)

# Scratch and quarantine copies hold the OLD identity / private key /
# clipboard rows — a clean slate sweeps them too.
QUARANTINE_GLOBS = (
    ".config_tmp_*.json",
    ".history_tmp_*.json",
    "config.json.corrupt-*",
    "clipboard_history.db.corrupt-*",
    # An unreadable phone conversation is copied aside rather than overwritten;
    # that copy is the whole conversation, so it goes with the rest.
    "phone_chat.json.corrupt-*",
)

# One-shot markers the web server turns into a client-side reset: browser state
# (group registry, mutes, theme, onboarding flag) lives in the webview's
# localStorage, which this process cannot reach.  "web_fresh_pending"
# re-surfaces the web onboarding wizard exactly once.
MARKERS = ("factory_reset_pending", "web_fresh_pending")

# Directories the reset deliberately keeps.  Spelled out, with the reason, so
# "kept" is a decision on the record rather than an omission — the review's
# complaint was precisely that the omissions were indistinguishable from
# choices.
KEPT_DIRS = {
    # The user asked for these explicitly, in the reset dialog itself
    # ("先创建备份" / create a backup first).  Deleting the archive somebody
    # just made would be the one thing worse than keeping an old one.
    "backups": "archives the user asked for, offered from the reset dialog itself",
}

# The one entry whose survival is reported but does not make the reset a
# failure: the logging handler may still hold it open on Windows, and a log
# tail is not user content.  Everything else carries something a user would
# call theirs.
_NON_CRITICAL = frozenset({"clipsync.log", *MARKERS})


@dataclass(frozen=True)
class ResetReport:
    """What the reset removed, and what it could not.

    ``ok`` is the reset's own claim: everything that holds user data is gone.
    ``failed`` carries every entry that survived, critical or not, so the
    caller can say what is left instead of returning success over a file it
    could not delete.
    """

    deleted: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    kept: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(name not in _NON_CRITICAL for name, _reason in self.failed)

    @property
    def failures(self) -> list[str]:
        """The surviving names, in the order they were attempted."""
        return [name for name, _reason in self.failed]

    def as_dict(self) -> dict:
        """The shape the RPC/HTTP response carries."""
        return {
            "ok": self.ok,
            "deleted": len(self.deleted),
            "failed": [{"name": name, "reason": reason} for name, reason in self.failed],
            "kept": list(self.kept),
        }


def reset_data_dir(directory: Path, *, data_dir: Path | None = None) -> ResetReport:
    """Delete the user-data files under *directory*; report what survived.

    A file the process still holds open (the log handler on Windows) must not
    abort the reset of everything else, so a failure is collected rather than
    raised — but it is *returned*, because a caller that logs it and answers
    ``ok: True`` is how the user ended up with a "clean slate" that still held
    their chat history.

    *data_dir* is the user's custom data directory (``cfg.data_dir``) when one
    is set; ``aiconfig_trash`` lives under it rather than beside config.json.
    Only that app-owned subdirectory is touched — never the directory itself,
    which the user may share with something else.

    Callers are expected to have released their own handles first — see
    ``SidecarApplication.factory_reset``.
    """
    base = Path(directory)
    data_base = Path(data_dir) if data_dir else base
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []

    def remove_file(path: Path, name: str) -> None:
        try:
            if path.exists():
                path.unlink()
                deleted.append(name)
        except OSError as exc:
            logger.warning("Factory reset: failed to delete %s: %s", path, exc)
            failed.append((name, str(exc)))

    for name in DATA_FILES:
        remove_file(base / name, name)

    for pattern in QUARANTINE_GLOBS:
        for path in list(base.glob(pattern)):
            remove_file(path, path.name)

    for name in DATA_DIRS:
        target = data_base / name
        try:
            if not target.is_dir():
                continue
            shutil.rmtree(target)
            deleted.append(name + "/")
        except OSError as exc:
            logger.warning("Factory reset: failed to delete %s: %s", target, exc)
            failed.append((name + "/", str(exc)))

    for name in MARKERS:
        try:
            (base / name).write_text("1", encoding="utf-8")
        except OSError as exc:
            logger.debug("Factory reset: could not write %s", name, exc_info=True)
            failed.append((name, str(exc)))

    return ResetReport(
        deleted=tuple(deleted),
        failed=tuple(failed),
        kept=tuple(KEPT_DIRS),
    )


def retry_failed(report: ResetReport, directory: Path, *, data_dir: Path | None = None
                 ) -> ResetReport:
    """One more pass over what the first pass could not delete.

    A handle released a moment ago on Windows stays locked for a few
    milliseconds after ``stop()`` returns; the retry is what turns "the log
    file survived" into a warning instead of a failed reset.  Only the failed
    entries are re-attempted — re-walking deleted files would report them as
    missing successes.
    """
    if not report.failed:
        return report
    base = Path(directory)
    data_base = Path(data_dir) if data_dir else base
    still_failing: list[tuple[str, str]] = []
    recovered: list[str] = []
    for name, reason in report.failed:
        try:
            if name in MARKERS:
                # A marker is created, not removed: retrying means writing it.
                (base / name).write_text("1", encoding="utf-8")
                recovered.append(name)
            elif name.endswith("/"):
                target = data_base / name.rstrip("/")
                if target.is_dir():
                    shutil.rmtree(target)
                recovered.append(name)
            else:
                path = base / name
                if path.exists():
                    path.unlink()
                recovered.append(name)
        except OSError as exc:
            still_failing.append((name, str(exc) or reason))
    if not recovered:
        return report
    return ResetReport(
        deleted=report.deleted + tuple(recovered),
        failed=tuple(still_failing),
        kept=report.kept,
    )
