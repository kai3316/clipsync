"""Abstract clipboard monitor, reader, and writer interface."""

import time
from abc import ABC, abstractmethod
from dataclasses import replace

from internal.clipboard.format import ClipboardContent, ContentType, strip_html


class ClipboardReader(ABC):
    """Read clipboard contents."""

    @abstractmethod
    def read(self) -> ClipboardContent:
        """Read all available formats from the clipboard."""


class ClipboardWriter(ABC):
    """Write content to clipboard."""

    @abstractmethod
    def write(self, content: ClipboardContent) -> bool:
        """Write content to the clipboard in the best available format.

        Returns True when the write was applied, False when it could not be
        (e.g. the Win32 clipboard was held open by another app and
        OpenClipboard failed).  Callers use the return value to decide whether
        a remote write actually landed and should count against the receive
        rate limit.
        """


def _html_to_plain_text(data: bytes) -> bytes:
    """Reduce an HTML payload to its visible text (best effort).

    Delegates to :func:`format.strip_html`, which is the single implementation.
    This module used to carry its own, and the two disagreed on the result: it
    left comments in and collapsed only spaces and tabs, so the newlines of the
    source survived — one text here for the sync payload, another in the
    history preview, for the same clip.
    """
    return strip_html(data.decode("utf-8", errors="replace")).encode("utf-8")


def strip_rich_formats(content: ClipboardContent) -> ClipboardContent:
    """Return *content* reduced to its non-rich-text formats.

    Backs the "plain text only" setting: HTML and RTF are dropped so peers
    always receive/paste unformatted text.  Non-text formats survive —
    images, EMF vectors and file lists are content, not formatting.  An
    HTML-only clip (no TEXT payload) is converted to plain text instead of
    being lost; an RTF-only clip (whose body would turn to brace garbage if
    naively stripped) and any clip left empty by the removal pass through
    unchanged.

    Callers apply this to the SYNC MESSAGE, not to the platform write: the
    receiving side hashes/stamps exactly these bytes before writing them to
    its clipboard, so message and written content stay identical and the
    receiver's read-back dedup cannot echo the stripped clip back.
    """
    types = {t: d for t, d in content.types.items() if t not in (ContentType.HTML, ContentType.RTF)}
    if ContentType.TEXT not in types:
        html_data = content.types.get(ContentType.HTML)
        if html_data is not None:
            plain = _html_to_plain_text(html_data)
            if plain:
                types[ContentType.TEXT] = plain
    if not types:
        # Nothing left after stripping (degenerate HTML-only clip whose
        # text extraction came up empty) — keep the original.
        return content
    # ``replace`` rather than a field-by-field rebuild: every field other than
    # ``types`` — the image hint, the transport, and the entry id a file offer
    # names — has to survive, and a rebuild silently drops whichever one was
    # added last.
    return replace(content, types=types)


class ClipboardMonitor(ABC):
    """Monitor clipboard for changes."""

    # Time-based suppression: monitor will not fire callbacks until
    # this timestamp (seconds since epoch).  Set via suppress_for().
    suppress_until: float = 0.0

    # Source app info captured at the moment of the last clipboard change.
    # Set by platform-specific monitors before calling the callback.
    last_source_app: dict | None = None

    # Whether the application that produced the last clipboard change asked not
    # to be recorded (a password manager's "concealed"/"transient" marker).
    #
    # Set by the platform adapters at the same moment as ``last_source_app``,
    # because both describe that one clipboard event and have to be read
    # together; ``SyncManager`` drops the capture when the
    # ``skip_password_manager_clips`` setting is on.  False is the honest answer
    # for a platform that cannot say, and is also what a monitor that has not
    # seen a marked copy reports.
    last_concealed: bool = False

    # Whether source-app tracking is enabled.  When False, get_active_app()
    # returns None and no OS query is made.
    source_tracking_enabled: bool = True

    # Why this monitor is not delivering clipboard changes, or None while it is.
    #
    # Set by the platform adapters that can fail *after* `start()` returned:
    # a Windows monitor builds its message-only window on its own thread, so a
    # refused window class or a refused `AddClipboardFormatListener` happens
    # long after the caller was told the monitor had started.  Recording it
    # here — rather than only in the log — is what lets the rest of the app say
    # "local capture is not running" instead of reporting itself healthy while
    # nothing is ever captured.
    last_error: str | None = None

    # Longest the monitor can go between two clipboard polls, in seconds --
    # the worst case for how long a change can sit unnoticed.  A caller that
    # suppresses the monitor across a write (SyncManager, so a remote write
    # does not read back as a fresh local copy) must outlast this plus its own
    # debounce, so the value belongs on the interface rather than on each
    # adapter as a private field the caller has to know to look for.
    #
    # Poll-based monitors (macOS, Linux) state their own value.  0.0 is the
    # honest answer for an event-driven monitor (Windows): it is notified of
    # every change and has no gap to cover.
    max_poll_interval: float = 0.0

    def suppress_for(self, duration_seconds: float):
        """Suppress monitor callbacks for the given duration."""
        self.suppress_until = time.time() + duration_seconds

    def set_source_tracking(self, enabled: bool):
        """Enable or disable source-app tracking."""
        self.source_tracking_enabled = bool(enabled)
        if not self.source_tracking_enabled:
            self.last_source_app = None

    def get_active_app(self) -> dict | None:
        """Capture and return info about the currently-active application.

        Only queries the OS when ``source_tracking_enabled`` is True.
        The result is cached internally by the source tracker for 500ms.
        """
        if not self.source_tracking_enabled:
            return None
        try:
            from internal.clipboard.source_tracker import get_active_app_info

            return get_active_app_info()
        except Exception:
            return None

    def monitor_error(self) -> str | None:
        """Why no clipboard change will ever arrive, or None while capture works.

        None means "came up and is listening", not merely "was asked to start".
        """
        return self.last_error

    @abstractmethod
    def start(self, callback):
        """
        Start monitoring. Calls `callback()` whenever the clipboard changes.
        The callback is called from a background thread.
        """

    @abstractmethod
    def stop(self):
        """Stop monitoring."""
