"""Path/URL safety helpers for the web API.

The web API is reachable from any LAN client holding the bearer token, so
filesystem paths supplied by clients must be confined to ClipSync-owned
directories.  Without this, a token holder could point an import/send
handler at any file on the host (e.g. ``~/.ssh/id_rsa``) and read it back.
"""

import os
from pathlib import Path

# URL schemes the /api/nav endpoint is allowed to open locally.  Everything
# else (file://, javascript:, data:, …) is rejected to prevent a token holder
# from driving local desktop behaviour beyond the app's intent.
_ALLOWED_NAV_SCHEMES = {"http", "https"}


def confine_path(path: str, root: Path) -> Path | None:
    """Resolve *path* and return it only if it lies within *root*.

    Returns ``None`` (rejected) for empty input or any path outside *root*,
    including those that escape via ``..`` or symlinks.
    """
    if not path:
        return None
    try:
        resolved = Path(os.path.realpath(os.path.expanduser(path)))
        root_resolved = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return None
    if resolved.is_relative_to(root_resolved):
        return resolved
    return None


def is_safe_nav_url(url: str) -> bool:
    """Return True only for http/https URLs."""
    if not url:
        return False
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    return scheme in _ALLOWED_NAV_SCHEMES
