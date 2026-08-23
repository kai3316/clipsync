"""Abstract clipboard monitor, reader, and writer interface."""

import time
from abc import ABC, abstractmethod

from internal.clipboard.format import ClipboardContent, ContentType


class ClipboardReader(ABC):
    """Read clipboard contents."""

    @abstractmethod
    def read(self) -> ClipboardContent:
        """Read all available formats from the clipboard."""


class ClipboardWriter(ABC):
    """Write content to clipboard."""

    @abstractmethod
    def write(self, content: ClipboardContent):
        """Write content to the clipboard in the best available format."""


def _html_to_plain_text(data: bytes) -> bytes:
    """Reduce an HTML payload to its visible text (best effort)."""
    import html as _html
    import re
    text = data.decode("utf-8", errors="replace")
    # Drop style/script blocks with their content, then remaining tags.
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip().encode("utf-8")


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
    types = {
        t: d for t, d in content.types.items()
        if t not in (ContentType.HTML, ContentType.RTF)
    }
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
    return ClipboardContent(
        types=types,
        source_device=content.source_device,
        timestamp=content.timestamp,
        image_fmt=content.image_fmt,
    )


class ClipboardMonitor(ABC):
    """Monitor clipboard for changes."""

    # Time-based suppression: monitor will not fire callbacks until
    # this timestamp (seconds since epoch).  Set via suppress_for().
    suppress_until: float = 0.0

    # SHA256 hash of the last content captured (used for dedup).
    _last_content_hash: str = ""

    # Source app info captured at the moment of the last clipboard change.
    # Set by platform-specific monitors before calling the callback.
    last_source_app: dict | None = None

    # Whether source-app tracking is enabled.  When False, get_active_app()
    # returns None and no OS query is made.
    source_tracking_enabled: bool = True

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

    @abstractmethod
    def start(self, callback):
        """
        Start monitoring. Calls `callback()` whenever the clipboard changes.
        The callback is called from a background thread.
        """

    @abstractmethod
    def stop(self):
        """Stop monitoring."""
