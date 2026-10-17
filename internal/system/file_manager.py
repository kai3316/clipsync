"""Open a path with the OS default app, or reveal its folder.

The platform commands live here so the legacy Tk application and the native
sidecar open/reveal the same path the same way.  Callers map the returned code
to their own localized message; nothing is logged or shown from this module.
"""

import os
import subprocess
import sys

FILE_NOT_FOUND = "file_not_found"
FOLDER_NOT_FOUND = "folder_not_found"
OPEN_FAILED = "open_failed"


def open_file(file_path: str) -> tuple[bool, str]:
    """Open *file_path* with the OS default application.

    Returns ``(True, resolved)`` on success and ``(False, code)`` on failure,
    where *code* is one of ``FILE_NOT_FOUND`` / ``OPEN_FAILED``.  Never raises.
    """
    resolved = os.path.abspath(file_path) if file_path else ""
    if not os.path.isfile(resolved):
        return False, FILE_NOT_FOUND
    try:
        if sys.platform == "win32":
            os.startfile(resolved)
        elif sys.platform == "darwin":
            subprocess.run(["open", resolved], check=True)
        else:
            subprocess.run(["xdg-open", resolved], check=True)
    except Exception:
        return False, OPEN_FAILED
    return True, resolved


def reveal_folder(file_path: str) -> tuple[bool, str]:
    """Reveal *file_path* (a file or a directory) in the OS file manager.

    Returns ``(True, folder)`` on success and ``(False, code)`` on failure,
    where *code* is one of ``FILE_NOT_FOUND`` / ``FOLDER_NOT_FOUND`` /
    ``OPEN_FAILED``.  Never raises.
    """
    resolved = os.path.abspath(file_path) if file_path else ""
    if os.path.isfile(resolved):
        folder = os.path.dirname(resolved)
    elif os.path.isdir(resolved):
        folder = resolved
    else:
        return False, FILE_NOT_FOUND
    if not os.path.isdir(folder):
        return False, FOLDER_NOT_FOUND
    try:
        if sys.platform == "win32":
            # explorer.exe directly instead of os.startfile(): a file
            # association could otherwise launch a second app instance.
            subprocess.Popen(["explorer", folder])
        elif sys.platform == "darwin":
            subprocess.run(["open", folder], check=True)
        else:
            subprocess.run(["xdg-open", folder], check=True)
    except Exception:
        return False, OPEN_FAILED
    return True, folder
