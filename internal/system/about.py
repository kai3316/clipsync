"""The URLs the app opens on the user's behalf.

Two entry points, and neither lets a caller hand the browser a string the app
did not produce itself.  :func:`open_link` names one of a closed table of the
app's own links.  :func:`open_web_url` is fed the text of an entry in the
user's *own history* — the history use case reads the row and passes what it
found, so a WebView still names a row rather than a URL, and the app can only
open something the user already had on their clipboard.  The version string
stays owned by :mod:`internal.version`.
"""

import webbrowser
from urllib.parse import urlparse

REPOSITORY = "kai3316/clipsync"
HOMEPAGE_URL = f"https://github.com/{REPOSITORY}"
RELEASES_URL = f"{HOMEPAGE_URL}/releases/latest"

#: Target name -> URL.  The native About dialog names one of these keys.
LINKS = {"homepage": HOMEPAGE_URL, "releases": RELEASES_URL}

#: Longest URL that may reach the OS.  A link someone copied is short; a
#: clipboard full of text that merely starts with ``http://`` is not something
#: to launch a browser with.
MAX_URL_CHARS = 2048


def is_openable_url(url: str) -> bool:
    """True only for a short, whitespace-free http(s) URL that has a host."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return False
    # A newline or a control character would let a clip smuggle a second
    # argument past whatever the platform opener hands the shell.
    if any(char.isspace() or ord(char) < 0x20 for char in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def open_link(target: str) -> tuple[bool, str]:
    """Open one of :data:`LINKS` in the default browser.

    Returns ``(True, url)`` on success and ``(False, code)`` on failure, where
    *code* is ``UNKNOWN_TARGET`` or ``OPEN_FAILED``.  Never raises.
    """
    url = LINKS.get(target) if isinstance(target, str) else None
    if url is None:
        return False, "UNKNOWN_TARGET"
    parsed = urlparse(url)
    # Defensive: a bad table entry must never hand the OS a non-web scheme.
    if parsed.scheme != "https" or not parsed.netloc:
        return False, "OPEN_FAILED"
    try:
        webbrowser.open(url)
    except Exception:
        return False, "OPEN_FAILED"
    return True, url


def open_web_url(url: str) -> tuple[bool, str]:
    """Open *url* in the default browser when it is an openable web link.

    Returns ``(True, url)`` on success and ``(False, code)`` on failure, where
    *code* is ``INVALID_URL`` (the text is not a link, so nothing was opened) or
    ``OPEN_FAILED`` (it was a link, and the OS would not take it).  Never raises.
    """
    if not is_openable_url(url):
        return False, "INVALID_URL"
    try:
        webbrowser.open(url)
    except Exception:
        return False, "OPEN_FAILED"
    return True, url
