"""Tail of the application log, with locally-sensitive strings removed.

Shared by the legacy ``GET /api/logs`` route and the native ``logs.tail``
command so the two cannot drift on either the tail window or the redaction
rules. Log lines are never sent anywhere until they pass
:func:`redact_sensitive_line`.
"""

import os
import shutil
from pathlib import Path

# Read only the tail (last 256 KB) so an oversized log is not fully loaded
# into memory; the caller's line count is clamped to the same range the web
# route always accepted.
MAX_TAIL_BYTES = 256 * 1024
DEFAULT_LINES = 200
MIN_LINES = 1
MAX_LINES = 1000


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
        return _log_dir() / "clipsync.log"
    except Exception:
        return None


# Historic private alias — ``read_log_tail`` and the diagnostics report both
# resolve the path through :func:`log_path`.
_log_path = log_path
