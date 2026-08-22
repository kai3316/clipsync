"""Source application tracking for clipboard content.

Determines which application was active when clipboard content was
captured, using platform-specific APIs.  Results are cached for 500ms
to avoid expensive OS queries on every clipboard poll.

Platform support:
  - Windows: ctypes + Win32 API (GetForegroundWindow, GetWindowTextW,
             GetWindowThreadProcessId, QueryFullProcessImageNameW)
  - macOS:   subprocess + osascript (System Events)
  - Linux:   subprocess + xdotool + /proc/PID/comm
"""

import fnmatch
import logging
import platform
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()

# ---- cache ---------------------------------------------------------

_cache_lock = threading.Lock()
_cache_expires: float = 0.0
_cache_value: dict | None = None
_CACHE_TTL = 0.5  # 500 ms


def get_active_app_info() -> dict | None:
    """Return info about the currently-active (foreground) application.

    Returns a dict with keys ``name``, ``process``, and ``title``, or
    ``None`` when the active application cannot be determined.

    The result is cached for 500 ms so that rapid successive calls
    (e.g., from the clipboard monitor polling loop) do not hammer the
    OS with repeated queries.
    """
    global _cache_expires, _cache_value
    now = time.time()
    with _cache_lock:
        if _cache_value is not None and now < _cache_expires:
            return _cache_value

    info = _get_active_app_info_impl()
    with _cache_lock:
        _cache_value = info
        _cache_expires = now + _CACHE_TTL

    return info


def is_app_allowed(app_info: dict | None, cfg) -> bool:
    """Check whether clipboard content from *app_info* should be
    captured, based on the app-filter configuration in *cfg*.

    Returns ``True`` when the content is allowed, ``False`` when it
    should be filtered out.

    *cfg* must have these attributes (matches ``Config``):
      - ``app_filter_enabled`` (bool)
      - ``app_filter_mode`` ("blacklist" | "whitelist")
      - ``app_filter_list`` (list[str] of process-name patterns)
    """
    if not cfg.app_filter_enabled:
        return True

    if not app_info:
        # Can't determine the app — allow by default (don't block user)
        return True

    process = (app_info.get("process") or "").lower()
    app_filter_list = [p.lower() for p in cfg.app_filter_list]

    def _matches(pattern: str) -> bool:
        # fnmatch supports glob patterns (*, ?) in addition to exact names.
        return fnmatch.fnmatch(process, pattern)

    if cfg.app_filter_mode == "whitelist":
        return any(_matches(p) for p in app_filter_list)

    # blacklist mode (default)
    return not any(_matches(p) for p in app_filter_list)


# ---- platform implementations --------------------------------------

def _get_active_app_info_impl() -> dict | None:
    if _SYSTEM == "Windows":
        return _get_active_app_info_windows()
    elif _SYSTEM == "Darwin":
        return _get_active_app_info_darwin()
    else:
        return _get_active_app_info_linux()


# -- Windows ---------------------------------------------------------

def _get_active_app_info_windows() -> dict | None:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # GetForegroundWindow
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        # GetWindowTextW — window title
        buf_title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf_title, 512)
        title = buf_title.value

        # GetWindowThreadProcessId
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        # OpenProcess + QueryFullProcessImageNameW
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_process = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not h_process:
            return {"name": title or "", "process": "", "title": title or ""}

        buf_path = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        success = kernel32.QueryFullProcessImageNameW(
            h_process, 0, buf_path, ctypes.byref(size),
        )
        kernel32.CloseHandle(h_process)

        exe_path = buf_path.value if success else ""
        process = exe_path.split("\\")[-1] if exe_path else ""

        # Derive a friendly name from the process
        name = _friendly_name(process, title)
        return {"name": name, "process": process, "title": title or ""}
    except Exception:
        logger.debug("Failed to get active app info on Windows", exc_info=True)
        return None


# -- macOS -----------------------------------------------------------

def _get_active_app_info_darwin() -> dict | None:
    try:
        script = (
            'tell application "System Events" to '
            "get {name, unix id, title} of "
            "first application process whose frontmost is true"
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=2,
        )
        if result.returncode != 0:
            return None

        # Output is like "Finder, 123, " (name, pid, title separated by comma-space)
        parts = result.stdout.decode("utf-8").strip().split(", ")
        if len(parts) < 2:
            return None

        app_name = parts[0]
        # pid = parts[1] (unused for now)
        title = parts[2] if len(parts) > 2 else ""
        process = app_name

        return {"name": app_name, "process": process, "title": title}
    except Exception:
        logger.debug("Failed to get active app info on macOS", exc_info=True)
        return None


# -- Linux -----------------------------------------------------------

def _get_active_app_info_linux() -> dict | None:
    try:
        # Get active window PID
        pid_result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True, timeout=2,
        )
        if pid_result.returncode != 0:
            return None

        pid = pid_result.stdout.decode("utf-8").strip()

        # Read process name from /proc/PID/comm
        try:
            proc_comm = open(f"/proc/{pid}/comm").read().strip()
        except (OSError, FileNotFoundError):
            return None

        # Try to get window title
        title_result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, timeout=2,
        )
        title = title_result.stdout.decode("utf-8").strip() if title_result.returncode == 0 else ""

        name = _friendly_name(proc_comm, title)
        return {"name": name, "process": proc_comm, "title": title}
    except Exception:
        logger.debug("Failed to get active app info on Linux", exc_info=True)
        return None


# ---- helpers --------------------------------------------------------

def _friendly_name(process: str, title: str = "") -> str:
    """Derive a human-friendly app name from the process name."""
    if not process:
        return title or ""
    # Strip common extensions
    for ext in (".exe", ".app", ".App"):
        if process.lower().endswith(ext):
            process = process[: -len(ext)]
    # Title-case
    if "_" in process or "-" in process:
        parts = process.replace("_", " ").replace("-", " ").split()
        process = " ".join(p.capitalize() for p in parts)
    elif process.islower() or process.isupper():
        process = process[0].upper() + process[1:] if process else ""
    return process
