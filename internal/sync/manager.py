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
from collections import deque
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
# A local clipboard change within this window (s) counts as "newer" than an
# incoming remote message, so near-simultaneous copies resolve by copy time
# rather than by arrival order (crossed writes).
CROSSED_WRITE_WINDOW = 0.1
# Receive-side rate limit: cap distinct remote clipboard writes per window so
# a peer cannot flood the local clipboard.  A small burst is allowed (covers
# out-of-order network delivery of a few rapid messages) but the sustained
# rate stays bounded to roughly REMOTE_RATE_MAX / REMOTE_RATE_WINDOW writes/s.
REMOTE_RATE_WINDOW = 1.0
REMOTE_RATE_MAX = 5


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
        # Platform monitors (macOS/Linux) store their poll interval on the
        # instance; fall back to 0.0 for event-driven (Windows) / mocked
        # monitors, where there is no poll to cover.
        self._poll_interval = getattr(monitor, "_poll_interval", 0.0)
        self._last_local_copy_time: float = 0.0
        self._remote_apply_times: deque = deque(maxlen=REMOTE_RATE_MAX)
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
            # A disable must also cancel any pending debounced read so a
            # queued local change is not broadcast after sync is paused.
            if not enabled and self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

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

            # Crossed writes: near-simultaneous copies should resolve by copy
            # time, not arrival order.  If the local clipboard changed very
            # recently, the local copy is probably newer — drop this message.
            if time.time() - self._last_local_copy_time < CROSSED_WRITE_WINDOW:
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

            # Receive-side rate limit: a peer must not be able to flood the
            # local clipboard with writes.  Drop messages once the per-window
            # budget of distinct writes is exhausted.
            now = time.time()
            while (self._remote_apply_times
                   and now - self._remote_apply_times[0] > REMOTE_RATE_WINDOW):
                self._remote_apply_times.popleft()
            if len(self._remote_apply_times) >= REMOTE_RATE_MAX:
                return

            self._dedup_ring.append(content_hash)
            if len(self._dedup_ring) > DEDUP_RING_SIZE:
                self._dedup_ring = self._dedup_ring[-DEDUP_RING_SIZE:]

            # Set _last_local_hash/_last_content_hash so the clipboard monitor
            # ignores the write we're about to make (prevents re-broadcasting
            # remote content).  Suppress the platform monitor for at least one
            # poll interval so the write — and any re-encoded read-back
            # (e.g. BMP/TIFF -> PNG on Linux/macOS) — is not re-detected.
            self._last_local_hash = content_hash
            self._last_content_hash = content_hash
            self._monitor.suppress_for(self._sync_debounce + self._poll_interval + 0.2)

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

        # Re-check enabled immediately before writing so a disable that
        # happened while we were processing is honored, and count this write
        # toward the rate limit only if it actually lands.
        with self._lock:
            if not self._enabled:
                return
            self._remote_apply_times.append(time.time())

        # Write to local clipboard.  Guarded so a clipboard-writer failure
        # drops this one message instead of killing the peer connection.
        try:
            logger.info(
                "Writing remote clipboard from %s: %d format(s)",
                msg.source_device, len(content.types),
            )
            self._writer.write(content)
        except Exception:
            logger.exception("Failed to write remote clipboard content")

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

            # A genuine local clipboard change (our own remote-write events
            # are absorbed at the platform monitor via suppress_for).  Track
            # when it happened so a crossed remote write doesn't overwrite it.
            self._last_local_copy_time = time.time()

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

        # Content-based dedup — a single canonical hash for all loop-prevention
        # state (_last_local_hash, _last_content_hash, dedup ring).
        content_hash = content.hash_key()

        with self._lock:
            # Catches duplicate captures (same content re-read after debounce)
            if content_hash == self._last_content_hash:
                return
            # Skip if we just sent this content (loop prevention)
            if content_hash == self._last_local_hash:
                return
            # Skip if recently seen (e.g. a remote write reflected back whose
            # read-back was not re-encoded)
            if content_hash in self._dedup_ring:
                return

            self._last_content_hash = content_hash
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

        # Don't broadcast content that carries no encodable formats (e.g. a
        # FILE/URL-only capture) — it would produce an empty frame on the wire.
        from internal.protocol.codec import has_syncable_types
        if not has_syncable_types(content):
            logger.debug("Clipboard content has no syncable formats — not broadcasting")
            return

        if self._on_send:
            self._on_send(msg)
