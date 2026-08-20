"""Abstract clipboard monitor, reader, and writer interface."""

import time
from abc import ABC, abstractmethod

from internal.clipboard.format import ClipboardContent


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
