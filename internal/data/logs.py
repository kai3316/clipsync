"""The application log: who writes it, and the tail of it that may be read.

Shared by the legacy ``GET /api/logs`` route and the native ``logs.tail``
command so the two cannot drift on either the tail window or the redaction
rules. Log lines are never sent anywhere until they pass
:func:`redact_sensitive_line`.

The record's shape — path, rotation, format, levels — is defined here rather
than at whichever entry point happens to call :func:`setup_file_logging`,
because the reader below parses the level field back out of it.
"""

import logging
import logging.handlers
import os
import re
import shutil
import sys
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


def setup_file_logging(stderr_level: int = logging.WARNING) -> bool:
    """Log to the rotating file the log view reads, and to stderr above *stderr_level*.

    The root logger is left at DEBUG so records reach the file handler, which is
    what decides how much is kept; stderr keeps its own higher level so the
    parent process's pipe is not flooded with the running account.

    Idempotent, and never raises: a log directory that cannot be created is not
    a reason for the application to refuse to start. Returns whether the file
    handler is in place.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    _quiet_noisy_loggers()

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

    if any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
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
    handler.setLevel(logging.DEBUG)
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


# Historic private alias — ``read_log_tail`` and the diagnostics report both
# resolve the path through :func:`log_path`.
_log_path = log_path
