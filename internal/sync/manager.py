"""Sync Manager — central coordinator for clipboard synchronization.

Responsibilities:
- Listen for local clipboard changes and broadcast to peers
- Receive clipboard content from peers and write to local clipboard
- Deduplication (hash-based)
- Loop prevention (don't reflect remote changes back)
- Throttle rapid changes
"""

import logging
import threading
import time
import uuid
from typing import Callable, Optional, Union

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.clipboard.history import ClipboardHistory
from internal.clipboard.history_db import ClipboardHistoryDB
from internal.clipboard.platform import create_monitor, create_reader, create_writer

logger = logging.getLogger(__name__)

# Minimum interval between outgoing syncs (debounce).
# Set to 0.5 s so multi-step clipboard writes (TEXT → HTML → RTF / image)
# are coalesced into a single read.  Applications that write formats
# sequentially typically finish within 200–400 ms; 500 ms covers the
# vast majority of cases without feeling sluggish.
SYNC_DEBOUNCE = 0.5
# Hash ring size for recently-synced content dedup
DEDUP_RING_SIZE = 64


class SyncManager:
    def __init__(self, device_id: str, device_name: str,
                 reader=None, writer=None, monitor=None,
                 history: Optional[Union[ClipboardHistory, ClipboardHistoryDB]] = None,
                 sync_debounce: float = 0.3,
                 retry_enabled: bool = True):
        self._device_id = device_id
        self._device_name = device_name
        self._reader = reader if reader is not None else create_reader()
        self._writer = writer if writer is not None else create_writer()
        self._monitor = monitor if monitor is not None else create_monitor()
        self._history = history
        self._enabled = True
        self._on_send: Callable | None = None
        self._on_history_change: Callable | None = None
        self._lock = threading.Lock()
        self._last_local_hash: str | None = None
        self._last_content_hash: str = ""
        self._dedup_ring: list[str] = []
        self._sync_debounce = sync_debounce
        self._pending_timer: threading.Timer | None = None
        self._suppress_monitor_until: float = 0.0
        self._retry_enabled = retry_enabled
        # Optional predicate: source-app info -> bool (True = allowed).
        # Set via set_app_filter(); used to drop clipboard content that
        # originates from disallowed applications.
        self._app_filter_fn: Callable[[dict | None], bool] | None = None

    @property
    def on_send(self) -> Callable | None:
        return self._on_send

    @on_send.setter
    def on_send(self, callback: Callable):
        self._on_send = callback

    @property
    def on_history_change(self) -> Callable | None:
        return self._on_history_change

    @on_history_change.setter
    def on_history_change(self, callback: Callable):
        self._on_history_change = callback

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._enabled = enabled

    def set_app_filter(self, fn: Callable[[dict | None], bool] | None):
        """Set a predicate that decides whether clipboard content from a
        given source app (dict or None) should be captured.  None disables
        the filter (everything allowed)."""
        self._app_filter_fn = fn

    def _notify_history_change(self) -> None:
        """Invoke the optional history-change callback (e.g. to push a
        `history_updated` event to connected web clients).  Never raises."""
        cb = self._on_history_change
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.debug("History change callback failed", exc_info=True)

    def reset_dedup_for_restore(self):
        """Clear dedup state so a history-restore write is not suppressed.

        Call before manually writing content to the clipboard (e.g. from
        the history panel) so the ensuing monitor event will be synced to
        peers instead of being filtered as a duplicate.
        """
        with self._lock:
            self._last_local_hash = None
            self._last_content_hash = ""
            self._suppress_monitor_until = 0.0
            self._monitor.suppress_until = 0.0

    def start(self):
        self._monitor.start(self._on_clipboard_change)
        logger.info("SyncManager started on %s", self._device_name)

    def stop(self):
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
        self._monitor.stop()
        logger.info("SyncManager stopped")

    def handle_remote_message(self, msg: SyncMessage):
        """Process a clipboard message received from a peer."""
        with self._lock:
            if not self._enabled:
                return

        content = msg.content
        if content.is_empty():
            return

        # Stamp the sender's device ID so history shows the correct source.
        content.source_device = msg.source_device

        content_hash = content.hash_key()

        with self._lock:
            # Skip if we just sent this content (loop prevention)
            if content_hash == self._last_local_hash:
                return

            # Skip if recently processed
            if content_hash in self._dedup_ring:
                return

            self._dedup_ring.append(content_hash)
            if len(self._dedup_ring) > DEDUP_RING_SIZE:
                self._dedup_ring = self._dedup_ring[-DEDUP_RING_SIZE:]

            # Set _last_local_hash so the clipboard monitor ignores the
            # write we're about to make (prevents re-broadcasting remote
            # content).  Also suppress the monitor for a full debounce
            # window after the write, because writers that set formats
            # sequentially will trigger multiple change events.
            self._last_local_hash = content_hash
            # Writer sets formats sequentially (pbcopy → osascript → ...);
            # each step triggers a change event.  Suppress long enough to
            # cover the slowest writer path (~0.3 s).
            self._suppress_monitor_until = time.time() + self._sync_debounce + 0.2
            # Also set monitor-level suppression so the monitor itself
            # drops events during the write window.
            self._monitor.suppress_for(self._sync_debounce + 0.2)

            # Cancel any pending local timer so it doesn't fire with
            # the remote content we're about to write.
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

        # Record in local clipboard history
        if self._history is not None:
            try:
                self._history.add(content)
            except Exception:
                logger.debug("Failed to add remote content to history", exc_info=True)

        self._notify_history_change()

        # Write to local clipboard
        logger.info(
            "Writing remote clipboard from %s: %d format(s)",
            msg.source_device, len(content.types),
        )
        self._writer.write(content)

    def _on_clipboard_change(self):
        """Called by the clipboard monitor when local clipboard changes.

        Defers the actual clipboard read until the debounce window has
        elapsed.  Applications often set clipboard formats in multiple
        steps (each triggering a change event), so reading + hashing on
        every event wastes CPU and creates duplicate history entries.
        By waiting for the clipboard to settle, we read once and produce
        a single history entry per user action.
        """
        with self._lock:
            if not self._enabled:
                return

            # Suppress monitor events caused by our own writes
            # (remote clipboard writes trigger the local monitor).
            if time.time() < self._suppress_monitor_until:
                return

            # Reset the coalescing timer — each new change pushes the
            # read further out until the clipboard is quiet.
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

            self._pending_timer = threading.Timer(
                self._sync_debounce,
                self._do_read_and_send,
            )
            self._pending_timer.daemon = True
            self._pending_timer.start()

    def _do_read_and_send(self):
        """Read clipboard after debounce, then broadcast if content is new."""
        with self._lock:
            self._pending_timer = None
            if not self._enabled:
                return

        # Use multi-round retry capture when enabled
        if self._retry_enabled:
            from internal.clipboard.retry import capture_with_retry
            content = capture_with_retry(self._reader)
        else:
            content = self._reader.read()

        if not content or content.is_empty():
            return

        # Skip accidental clipboard noise: whitespace-only or single-
        # character copies that terminals often emit on click/select.
        if ContentType.TEXT in content.types:
            text = content.types[ContentType.TEXT].decode("utf-8", errors="replace")
            stripped = text.strip()
            if len(stripped) <= 1:
                return

        # Retrieve source-app info once (captured by the monitor before the
        # callback fired) for both app filtering and history attribution.
        source_app = getattr(self._monitor, 'last_source_app', None)

        # App filter: drop content from disallowed source applications.
        if self._app_filter_fn is not None and not self._app_filter_fn(source_app):
            logger.debug("Clipboard from disallowed app filtered out: %s", source_app)
            return

        # SHA256 content-based dedup (catches duplicate captures)
        from internal.clipboard.dedup import content_hash as sha256_content_hash
        sha256_hash = sha256_content_hash(content)
        if sha256_hash == self._last_content_hash:
            return
        self._last_content_hash = sha256_hash

        content_hash = content.hash_key()

        with self._lock:
            if content_hash == self._last_local_hash:
                return  # Already sent, suppress

            self._last_local_hash = content_hash

            self._dedup_ring.append(content_hash)
            if len(self._dedup_ring) > DEDUP_RING_SIZE:
                self._dedup_ring = self._dedup_ring[-DEDUP_RING_SIZE:]

        # Record in clipboard history — once per action
        if self._history is not None:
            try:
                self._history.add(content, source_app=source_app)
            except Exception:
                logger.debug("Failed to add to clipboard history", exc_info=True)

        self._notify_history_change()

        msg = SyncMessage(
            content=content,
            msg_id=uuid.uuid4().hex,
            source_device=self._device_id,
        )

        logger.info("Local clipboard changed: %d format(s)", len(content.types))

        if self._on_send:
            self._on_send(msg)
