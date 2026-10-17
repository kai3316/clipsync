"""The application log: who writes it, and the tail of it that may be read.

Shared by the legacy ``GET /api/logs`` route and the native ``logs.tail``
command so the two cannot drift on either the tail window or the redaction
rules. Log lines are never sent anywhere until they pass
:func:`redact_sensitive_line`.

The record's shape — path, rotation, format, levels — is defined here rather
than at whichever entry point happens to call :func:`setup_file_logging`,
because the reader below parses the level field back out of it.
"""

import contextlib
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time
from pathlib import Path

# Read only the tail (last 256 KB) so an oversized log is not fully loaded
# into memory; the caller's line count is clamped to the same range the web
# route always accepted.
MAX_TAIL_BYTES = 256 * 1024
DEFAULT_LINES = 200
MIN_LINES = 1
MAX_LINES = 1000

LOG_FILE_NAME = "clipsync.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(threadName)-12s "
    "%(name)s:%(lineno)d  %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# A line at one of these levels reports something that went wrong, and is what
# someone reads the log to find; everything else is the running account of what
# the application did. WARNING is the boundary rather than ERROR: a warning is
# already a departure from the expected path, and the alternative is a log view
# whose "problems" tab is silent for the failure that only warned.
PROBLEM_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})

# The level is the first bracketed field, straight after the timestamp
# :data:`LOG_FORMAT` writes. Anchored to that position so a message that
# happens to quote "[ERROR]" cannot promote its own line.
_LEVEL_FIELD = re.compile(r"^\d{4}-\d{2}-\d{2} [\d:.]+\s+\[([A-Z]+)\s*\]")


# The file handler's own default.  ``config.Config.log_level`` defaults to
# INFO, and the settings page offers DEBUG/INFO/WARNING/ERROR; the handler has to
# agree with that default or selecting INFO changes nothing -- the root logger
# stays at DEBUG so a DEBUG selection can still reach it.
DEFAULT_FILE_LEVEL = logging.INFO

_FILE_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def resolve_file_level(level) -> int:
    """Coerce a configured level into a logging level, INFO when it is unknown.

    Strings are the names the config and the settings page use; integers are the
    ``logging`` constants callers pass directly.  Anything else -- including the
    misspelled level a hand-edited config can carry -- is the INFO default that
    ``config.Config.log_level`` itself uses.
    """
    if isinstance(level, bool):
        return DEFAULT_FILE_LEVEL
    if isinstance(level, int):
        return level
    return _FILE_LEVEL_NAMES.get(str(level or "").strip().upper(), DEFAULT_FILE_LEVEL)


def set_file_log_level(level) -> bool:
    """Point the rotating file handler at *level*, falling back to INFO.

    Called by the sidecar once the config is loaded, and again whenever the
    settings page saves ``log_level``: the handler is built before either of
    those happens, at the default, so without this a level picked in the UI only
    took effect on the next restart.

    Returns whether a file handler was found.  An unknown *level* is not a
    failure: it is INFO, the same answer the config default gives.  The stderr
    handler is deliberately not touched.
    """
    resolved = resolve_file_level(level)
    found = False
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            handler.setLevel(resolved)
            found = True
    return found


def setup_file_logging(
    stderr_level: int = logging.WARNING, file_level=DEFAULT_FILE_LEVEL
) -> bool:
    """Log to the rotating file the log view reads, and to stderr above *stderr_level*.

    The root logger is left at DEBUG so records reach the file handler, which is
    what decides how much is kept; *file_level* (INFO by default) is the level it
    starts at, and :func:`set_file_log_level` moves it once the configured level
    is known.  stderr keeps its own higher level so the parent process's pipe is
    not flooded with the running account.

    Idempotent, and never raises: a log directory that cannot be created is not
    a reason for the application to refuse to start.  A second call applies
    *file_level* to the handler already in place rather than ignoring it.
    Returns whether the file handler is in place.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    _quiet_noisy_loggers()
    level = resolve_file_level(file_level)

    if not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    ):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(stderr_level)
        stream.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)-28s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(stream)

    existing = [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]
    if existing:
        for handler in existing:
            handler.setLevel(level)
        return True
    path = log_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not open the log file %s: %s", path, exc)
        return False
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(handler)
    return True


def _quiet_noisy_loggers() -> None:
    """Keep the third-party chatter that is never about this application out."""
    for noisy in ("zeroconf", "PIL", "cryptography", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def split_log_lines(lines) -> tuple[list[str], list[str]]:
    """Return ``(problems, rest)``, each keeping the order it was given.

    A line whose level field is not recognised counts as *rest* — unless it
    directly follows a problem line, which is how a traceback's continuation
    lines stay with the failure that raised them. Those lines are the ones that
    make an error worth reading, and they carry no level of their own. Any wider
    guess is what would put the running account into the one list the user reads
    to find the problem.
    """
    problems: list[str] = []
    rest: list[str] = []
    in_problem = False
    for line in lines:
        match = _LEVEL_FIELD.match(line)
        if match:
            in_problem = match.group(1) in PROBLEM_LEVELS
        (problems if in_problem else rest).append(line)
    return problems, rest


def redact_sensitive_line(line: str, cfg) -> str:
    """Strip locally-sensitive strings (user home, config dir, web token)
    from a log line before it is served to a client.

    The raw log contains absolute user paths (e.g. ``C:\\Users\\<name>
    \\AppData\\Roaming\\ClipSync\\...``), stack traces and config values —
    useful reconnaissance that should not leave the device.
    """
    redact: list[str] = []
    home = os.path.expanduser("~")
    if home:
        redact.append(home)
    try:
        from internal.config.config import _config_dir

        config_dir = str(_config_dir())
        if config_dir and config_dir != home:
            redact.append(config_dir)
    except Exception:
        pass
    token = getattr(cfg, "web_token", "")
    if token:
        redact.append(token)
    # The broker and pairing credentials, for the lines an older build wrote
    # before the settings endpoint stopped naming a new value in the log.  A
    # short one is skipped rather than armed: `str.replace` with a one-character
    # needle would take the rest of the log apart with it, and the strength rule
    # this app enforces on both fields never lets one be that short.
    for field in ("relay_password", "netpair_password"):
        secret = getattr(cfg, field, "") or ""
        if len(secret) >= 8:
            redact.append(secret)
    for r in redact:
        if r:
            line = line.replace(r, "[redacted]")
    return line


def clamp_lines(lines) -> int:
    """Coerce a caller-supplied line count into the accepted range."""
    try:
        count = int(lines)
    except (TypeError, ValueError):
        return DEFAULT_LINES
    return max(MIN_LINES, min(count, MAX_LINES))


def read_log_tail(cfg, lines=DEFAULT_LINES, log_path=None) -> list[str]:
    """Return the last *lines* log lines, newest last, each redacted.

    Missing or unreadable logs return an empty list: a diagnostics view must
    render even when logging never started.
    """
    count = clamp_lines(lines)
    path = Path(log_path) if log_path is not None else _log_path()
    if path is None or not path.exists():
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    return [
        redact_sensitive_line(line, cfg) for line in tail.splitlines()[-count:]
    ]


def export_log(dest: str) -> dict:
    """Copy the log file to *dest*, a path the user picked in a save dialog.

    The raw log is copied rather than the redacted tail: the user is exporting
    their own log, and redacted paths would defeat a support request.  Returns
    ``{"ok": True, "path": dest, "bytes": size}``; a failure comes back as
    ``{"ok": False, "error": code}`` with *code* one of ``LOG_NOT_FOUND`` /
    ``PERMISSION_DENIED`` / ``EXPORT_FAILED`` so the caller can pick an error
    message.  Never raises.
    """
    if not dest:
        return {"ok": False, "error": "EXPORT_FAILED"}
    source = _log_path()
    if source is None or not source.exists():
        return {"ok": False, "error": "LOG_NOT_FOUND"}
    target = Path(dest)
    if target.is_dir():
        return {"ok": False, "error": "EXPORT_FAILED"}
    try:
        shutil.copy2(source, target)
        size = os.path.getsize(target)
    except PermissionError:
        return {"ok": False, "error": "PERMISSION_DENIED"}
    except OSError:
        return {"ok": False, "error": "EXPORT_FAILED"}
    return {"ok": True, "path": str(target), "bytes": size}


def log_path():
    """Path of the application log file, or None when it cannot be resolved."""
    # Resolved per call: tests and callers monkeypatch ``_log_dir`` on the
    # config module, and binding it at import time would ignore that.
    from internal.config.config import _log_dir

    try:
        return _log_dir() / LOG_FILE_NAME
    except Exception:
        return None


# ── logs collected from other devices ────────────────────────────────
#
# A device that has turned log sharing on answers a peer's request with a
# *redacted* copy of its whole current log (``write_share_copy``), and the
# asking side files what arrives under ``collected_dir()`` — the same
# ``~/Downloads`` the received-files and staged-update folders live in, so the
# things a peer can leave on this disk are all in one place.
COLLECTED_DIR_NAME = "ClipSync-logs"
SHARE_DIR_NAME = "share"
# How long a copy made for a peer is kept if its transfer never finished (the
# send runs on a thread of its own, and a peer that disconnects mid-transfer
# leaves the file behind).  An hour is well past any transfer that is still
# going, and the sweep runs when the next copy is written rather than on a
# timer, so a device that never shares again keeps nothing.
SHARE_MAX_AGE = 3600.0

# How many collected logs one device may leave behind, and how old a collected
# log may be before it is dropped even when it is one of that device's newest.
# The folder had no sweep at all: every collection added a file and nothing ever
# removed one, so it grew for as long as log sharing was used.
COLLECTED_KEEP_PER_DEVICE = 5
COLLECTED_MAX_AGE_DAYS = 30
# ``<safe device name>-<YYYYmmdd-HHMMSS>[-N].log``, the shape
# ``stage_collected_log`` writes.  The device is everything before the stamp;
# the optional ``-N`` is the same-second collision counter, and it is stripped
# before grouping so a device's files are compared as one set.  Files that do
# not match are left alone -- they are not ours to delete.
_COLLECTED_NAME = re.compile(r"^(?P<device>.+)-(?P<stamp>\d{8}-\d{6})(?:-\d+)?\.log$")


def collected_dir(home: str | None = None) -> Path:
    """Where logs collected from other devices are filed."""
    return (Path(home) if home else Path.home()) / "Downloads" / COLLECTED_DIR_NAME


def _safe_name(name: str) -> str:
    """One path component's worth of a device name, never empty."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(name or "")).strip(" .-")
    return cleaned[:60] or "device"


def write_share_copy(cfg, path: Path | str | None = None) -> Path | None:
    """Write the whole current log, redacted, as a file to hand to a peer.

    The same redaction the log viewer gets, applied to the whole file rather
    than the tail: this copy leaves the machine, and the reason the tail is
    capped (not loading an oversized log into memory) does not apply to a
    reader that writes as it goes.

    ``None`` when there is no log to share — a machine that has never written
    one has nothing to answer with, which is a different answer from a device
    that has sharing switched off (see ``_serve_peer_log``).
    """
    source = Path(path) if path is not None else log_path()
    if source is None or not source.exists():
        return None
    target_dir = source.parent / SHARE_DIR_NAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        _prune_shares(target_dir)
        target = target_dir / f"clipsync-{int(time.time())}.log"
        with (
            open(source, encoding="utf-8", errors="replace") as reader,
            open(target, "w", encoding="utf-8", newline="\n") as writer,
        ):
            for line in reader:
                writer.write(redact_sensitive_line(line.rstrip("\n"), cfg) + "\n")
    except OSError:
        logging.getLogger(__name__).warning("Could not build a log copy to share", exc_info=True)
        return None
    return target


def _prune_shares(target_dir: Path) -> None:
    """Drop copies left by transfers that never finished."""
    cutoff = time.time() - SHARE_MAX_AGE
    for stale in target_dir.glob("clipsync-*.log"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue


def _prune_collected(target_dir: Path, incoming_stem: str = "") -> None:
    """Keep the collected-log folder bounded: newest five per device, 30 days.

    Files are grouped by the device their ``<device>-<stamp>[-N].log`` name
    names, and recency is the file's own mtime (a staged copy keeps the mtime it
    arrived with), so two copies from the same second keep a stable order.  A
    file older than :data:`COLLECTED_MAX_AGE_DAYS` goes even when it is one of a
    device's newest.

    *incoming_stem* is the file ``stage_collected_log`` is about to move in.
    When it is given, the device it belongs to keeps one slot free, so the
    folder ends at :data:`COLLECTED_KEEP_PER_DEVICE` after the write rather than
    one over it.  Called without it, the function simply keeps the five newest.
    """
    cutoff = time.time() - COLLECTED_MAX_AGE_DAYS * 86400
    incoming = _COLLECTED_NAME.match(incoming_stem) if incoming_stem else None
    incoming_device = incoming.group("device") if incoming is not None else ""
    grouped: dict[str, list[tuple[int, float, str, Path]]] = {}
    for entry in target_dir.glob("*.log"):
        match = _COLLECTED_NAME.match(entry.name)
        if match is None or not entry.is_file():
            continue
        try:
            info = entry.stat()
        except OSError:
            continue
        grouped.setdefault(match.group("device"), []).append(
            (info.st_mtime_ns, info.st_mtime, entry.name, entry)
        )
    for device, entries in grouped.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        keep = COLLECTED_KEEP_PER_DEVICE
        if device == incoming_device:
            keep -= 1
        for index, (_mtime_ns, mtime, _name, path) in enumerate(entries):
            if index >= keep or mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()


def stage_collected_log(saved_path: str, device_name: str, home: str | None = None) -> str:
    """File a peer's log where the user can find it; returns its new path.

    Named ``<device>-<time>.log`` in ``collected_dir()``: several devices are
    collected at once, and what the user is looking for afterwards is whose log
    this is and when it was taken.  Raises on any filesystem failure, so the
    caller can report a collection that did not land rather than a folder that
    is quietly missing one device.

    The folder is swept on the way in (see :func:`_prune_collected`), so a device
    cannot leave an unbounded pile of logs here.
    """
    target_dir = collected_dir(home)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{_safe_name(device_name)}-{stamp}"
    target = target_dir / f"{stem}.log"
    suffix = 2
    while target.exists():
        target = target_dir / f"{stem}-{suffix}.log"
        suffix += 1
    # Before the move, and counting the file that is about to land: keeping five
    # of the existing files and then adding one would end at six.
    _prune_collected(target_dir, incoming_stem=target.name)
    shutil.move(str(saved_path), str(target))
    return str(target)


# Historic private alias — ``read_log_tail`` and the diagnostics report both
# resolve the path through :func:`log_path`.
_log_path = log_path
