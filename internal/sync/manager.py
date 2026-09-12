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
from collections.abc import Callable

from internal.clipboard.format import ContentType, SyncMessage
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
# How long (seconds) a hash keeps suppressing re-captures.  The ring exists
# to stop sync loops (a broadcast reflecting back, a debounce re-read), not
# to remember history: past this window a repeated hash is treated as a
# deliberate new copy and flows into history/broadcast normally.
DEDUP_RING_TTL = 90.0
# Receive-side rate limit: cap distinct remote clipboard writes per window so
# a peer cannot flood the local clipboard.  A small burst is allowed (covers
# out-of-order network delivery of a few rapid messages) but the sustained
# rate stays bounded to roughly REMOTE_RATE_MAX / REMOTE_RATE_WINDOW writes/s.
REMOTE_RATE_WINDOW = 1.0
REMOTE_RATE_MAX = 5


class SyncManager:
    def __init__(
        self,
        device_id: str,
        device_name: str,
        reader=None,
        writer=None,
        monitor=None,
        history: ClipboardHistoryDB | None = None,
        sync_debounce: float = 0.3,
        retry_enabled: bool = True,
    ):
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
        # Serializes _do_read_and_send executions.  A rich-content capture
        # can take ~1.4s, during which _pending_timer is already cleared, so
        # a second clipboard event would otherwise start a parallel read that
        # broadcasts stale content out of order (and can drop the newest copy).
        self._read_lock = threading.Lock()
        self._last_local_hash: str | None = None
        self._last_content_hash: str = ""
        # Monotonic time the hash fields above were last set.  Bounds how long
        # they keep suppressing re-captures (mirrors DEDUP_RING_TTL), so a
        # deliberate re-copy of the same content after the window is treated
        # as new instead of being suppressed forever.
        self._last_hash_ts: float = 0.0
        self._dedup_ring: list[tuple[str, float]] = []  # (hash, monotonic_ts)
        self._sync_debounce = sync_debounce
        self._pending_timer: threading.Timer | None = None
        # Platform monitors (macOS/Linux) store their poll interval on the
        # instance; fall back to 0.0 for event-driven (Windows) / mocked
        # monitors, where there is no poll to cover.
        self._poll_interval = getattr(monitor, "_poll_interval", 0.0)
        # The monitor may back off to a longer idle poll interval (Linux does:
        # _idle_poll_interval = max(poll*2, 2.0s)).  The write-suppression
        # window must cover that too, or a re-encoded read-back (BMP/TIFF ->
        # PNG on Linux) lands after suppression expires and is re-broadcast as
        # a fresh local copy.  Fall back to the active interval for event-driven
        # (Windows) / mocked monitors that have no idle backoff.
        self._max_poll_interval = getattr(
            monitor,
            "_idle_poll_interval",
            self._poll_interval,
        )
        self._last_local_copy_time: float = 0.0
        # Non-zero while a local capture is in flight.  The crossed-write
        # check below reads it: the debounce window is measured from the
        # change event, so it has already expired by the time the capture
        # actually runs -- and capture_with_retry can spend ~1.4s on rich
        # content.  For that whole stretch a remote message won the tie and
        # overwrote a local copy the user had just made, which then vanished
        # from under them.  The flag extends the window over exactly the
        # capture, and not a millisecond past it (a legitimate remote message
        # arriving right after must still land).
        self._local_capture_active: int = 0
        self._remote_apply_times: deque = deque(maxlen=REMOTE_RATE_MAX)
        self._retry_enabled = retry_enabled
        # Optional predicate: source-app info -> bool (True = allowed).
        # Set via set_app_filter(); used to drop clipboard content that
        # originates from disallowed applications.
        self._app_filter_fn: Callable[[dict | None], bool] | None = None
        # Optional callback invoked when writing remote content to the local
        # clipboard fails.  Set via set_on_write_error().
        self._on_write_error: Callable[[], None] | None = None

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

    def set_on_write_error(self, callback: Callable[[], None] | None):
        """Set a callback invoked when writing remote clipboard content to
        the local clipboard fails.

        The callback takes no arguments and is responsible for surfacing the
        failure to the user (e.g. a desktop notification).  Passing ``None``
        disables the callback.  The sync loop still swallows the underlying
        error either way, so a clipboard-writer failure never crashes the
        receive path.
        """
        self._on_write_error = callback

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
            self._last_hash_ts = 0.0
            self._dedup_ring.clear()
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

    def handle_remote_message(self, msg: SyncMessage, via_relay: bool = False) -> bool:
        """Process a clipboard message received from a peer.

        Returns True when the message was ACCEPTED (recorded into history and
        applied to the clipboard), False when it was dropped for any reason
        (sync disabled, crossed write, empty content, loop-prevention dedup,
        rate limit) or when the clipboard write failed to land.  Round 17: the
        caller uses the return value to decide whether to send an internet
        ``relay_ack`` receipt — only a message that actually landed on the
        clipboard earns a "已送达" confirmation.

        ``via_relay`` says which of the two transports delivered the frame: the
        relay router passes True, the LAN router leaves it False.  It is
        recorded on the history row as the route the clip came in on, which a
        reader needs because the same peer can be reachable both ways and the
        device name alone cannot say which one carried this copy.
        """
        with self._lock:
            if not self._enabled:
                return False

            # Crossed writes: near-simultaneous copies should resolve by copy
            # time, not arrival order.  If the local clipboard changed very
            # recently, the local copy is probably newer — drop this message.
            # The window must cover the debounce period (a local copy is still
            # "pending" until its timer fires), otherwise a remote message
            # arriving mid-debounce overwrites the newer local copy.
            if self._local_capture_active:
                # ...and the debounce alone does not cover the capture that
                # follows it, which is the slowest part of the local path.
                return False
            if time.time() - self._last_local_copy_time < self._sync_debounce:
                return False

        content = msg.content
        if content.is_empty():
            return False

        # Stamp the sender's device ID so history shows the correct source.
        content.source_device = msg.source_device
        # ...and the route it arrived on, so the row can say "over the relay"
        # or "over the local link" and not just who sent it.
        content.transport = "relay" if via_relay else "lan"

        content_hash = content.hash_key()

        with self._lock:
            # Skip if we just sent this content (loop prevention)
            if content_hash == self._last_local_hash:
                return False

            # Skip if recently processed (loop prevention, TTL-bounded)
            if self._dedup_seen(content_hash):
                return False

            # Receive-side rate limit: a peer must not be able to flood the
            # local clipboard with writes.  Drop messages once the per-window
            # budget of distinct writes is exhausted.
            now = time.time()
            while (
                self._remote_apply_times and now - self._remote_apply_times[0] > REMOTE_RATE_WINDOW
            ):
                self._remote_apply_times.popleft()
            if len(self._remote_apply_times) >= REMOTE_RATE_MAX:
                return False

            # Suppress the platform monitor for its longest (idle) poll interval
            # so the write — and any re-encoded read-back (e.g. BMP/TIFF -> PNG
            # on Linux/macOS) — is not re-detected.  The loop-prevention
            # bookkeeping (_dedup_ring_remember / _last_*_hash) happens AFTER
            # the write lands (see below), so a failed write cannot poison the
            # dedup state and silently drop the sender's retry.
            self._monitor.suppress_for(self._sync_debounce + self._max_poll_interval + 0.2)

            # Cancel any pending local timer so it doesn't fire with
            # the remote content we're about to write.
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

        # Re-check enabled immediately before writing so a disable that
        # happened while we were processing is honored.
        with self._lock:
            if not self._enabled:
                return False

        # Write to local clipboard.  Guarded so a clipboard-writer failure
        # drops this one message instead of killing the peer connection.
        #
        # The clipboard comes FIRST and history only follows a successful
        # write.  Recording history first meant a failed write still left a
        # row in the list (and broadcast a history_updated to every web
        # client) for content that never reached the clipboard: the user saw
        # the item arrive, clicked it, and got something else -- a ghost entry
        # with no way to tell it apart from a real one.
        try:
            logger.info(
                "Writing remote clipboard from %s: %d format(s)",
                msg.source_device,
                len(content.types),
            )
            wrote = self._writer.write(content)
        except Exception:
            wrote = False
            logger.exception("Failed to write remote clipboard content")

        if not wrote:
            # Surface the failure (desktop notification) without letting it
            # crash the sync loop — a transient clipboard-busy error on the
            # receiver should not kill the peer connection.
            cb = self._on_write_error
            if cb is not None:
                try:
                    cb()
                except Exception:
                    logger.debug("on_write_error callback failed", exc_info=True)
            # The write did not land — do not count it toward the rate limit
            # and do not acknowledge the message to the peer.
            return False

        # The write landed — now it is real, so record it in history.
        if self._history is not None:
            try:
                self._history.add(content)
            except Exception:
                logger.debug("Failed to add remote content to history", exc_info=True)

        self._notify_history_change()

        # The message was accepted (clipboard write applied + history).
        # Count this write toward the rate limit AND record loop-prevention
        # state only now that it actually landed.
        with self._lock:
            self._remote_apply_times.append(time.time())
            self._dedup_ring_remember(content_hash)
            self._last_local_hash = content_hash
            self._last_content_hash = content_hash
            self._last_hash_ts = time.monotonic()

        return True

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
        """Read clipboard after debounce, then broadcast if content is new.

        Serialized by ``_read_lock``: a capture can take ~1.4s for rich
        content while ``_pending_timer`` is already None, so without the lock
        a second clipboard event could start a parallel capture that
        broadcasts stale content out of order (or drops the newest copy).
        """
        with self._read_lock:
            with self._lock:
                self._local_capture_active += 1
            try:
                self._do_read_and_send_locked()
            finally:
                with self._lock:
                    self._local_capture_active -= 1

    def _dedup_seen(self, content_hash: str) -> bool:
        """TTL-bounded ring lookup: True if *content_hash* was processed
        within ``DEDUP_RING_TTL``.  Expired entries are pruned here so the
        ring stays bounded in time, not only in count."""
        now = time.monotonic()
        self._dedup_ring = [(h, ts) for h, ts in self._dedup_ring if now - ts <= DEDUP_RING_TTL]
        return any(h == content_hash for h, _ in self._dedup_ring)

    def _dedup_ring_remember(self, content_hash: str) -> None:
        """Record a hash in the dedup ring together with its capture time."""
        self._dedup_ring.append((content_hash, time.monotonic()))
        if len(self._dedup_ring) > DEDUP_RING_SIZE:
            self._dedup_ring = self._dedup_ring[-DEDUP_RING_SIZE:]

    def _reset_dedup_hashes(self) -> None:
        """Clear the loop-prevention hash state.

        Used when content is dropped as noise (empty clipboard, whitespace-only
        text, app-filtered copy) so a later copy of previously-seen content is
        not suppressed against stale hashes.
        """
        with self._lock:
            self._last_local_hash = None
            self._last_content_hash = ""
            self._last_hash_ts = 0.0

    def _do_read_and_send_locked(self):
        """Capture + broadcast. Called while holding ``_read_lock``."""
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
            # Clipboard was cleared — clear the loop-prevention hashes so a
            # later copy of previously-seen content is not suppressed.
            self._reset_dedup_hashes()
            return

        # Skip accidental clipboard noise: whitespace-only copies that
        # terminals often emit on click/select.  A single character is a
        # legitimate copy (a digit or letter) and must not be dropped.
        # Only drop whitespace-only TEXT when nothing else is on the
        # clipboard — a whitespace TEXT alongside an image or file list is
        # part of a legitimate rich copy and must survive.
        if ContentType.TEXT in content.types:
            text = content.types[ContentType.TEXT].decode("utf-8", errors="replace")
            stripped = text.strip()
            if not stripped and not any(
                data for fmt, data in content.types.items() if fmt != ContentType.TEXT
            ):
                self._reset_dedup_hashes()
                return

        # Retrieve source-app info once (captured by the monitor before the
        # callback fired) for both app filtering and history attribution.
        source_app = getattr(self._monitor, "last_source_app", None)

        # App filter: drop content from disallowed source applications.
        if self._app_filter_fn is not None and not self._app_filter_fn(source_app):
            logger.debug("Clipboard from disallowed app filtered out: %s", source_app)
            self._reset_dedup_hashes()
            return

        # Content-based dedup — a single canonical hash for all loop-prevention
        # state (_last_local_hash, _last_content_hash, dedup ring).
        content_hash = content.hash_key()

        with self._lock:
            # A pause requested mid-capture takes effect here: nothing below
            # may enter history or reach the network once the user paused.
            if not self._enabled:
                return
            # Catches duplicate captures (same content re-read after debounce).
            # Bounded by the same TTL as the dedup ring, so a deliberate
            # re-copy of the last content after DEDUP_RING_TTL is treated as
            # new instead of being suppressed forever.
            if (
                content_hash == self._last_content_hash
                and time.monotonic() - self._last_hash_ts <= DEDUP_RING_TTL
            ):
                return
            # Skip if we just sent this content (loop prevention) — same TTL
            # bound, so an echoed-back remote write stops suppressing once the
            # ring window has passed.
            if (
                content_hash == self._last_local_hash
                and time.monotonic() - self._last_hash_ts <= DEDUP_RING_TTL
            ):
                return
            # Skip if recently seen (e.g. a remote write reflected back whose
            # read-back was not re-encoded)
            if self._dedup_seen(content_hash):
                return

            self._last_content_hash = content_hash
            self._last_local_hash = content_hash
            self._last_hash_ts = time.monotonic()

            self._dedup_ring_remember(content_hash)

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

        # Final pause check: the dedup block released the lock a moment ago;
        # honour a pause that landed since then before going to the network.
        with self._lock:
            if not self._enabled:
                logger.debug("Sync paused during send prep — dropping local capture")
                return

        if self._on_send:
            try:
                self._on_send(msg)
            except Exception:
                # A failing send callback must not kill the daemon Timer
                # thread (which would drop future captures) nor lose the
                # capture already recorded in history above.
                logger.exception("on_send callback failed")
