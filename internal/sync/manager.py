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
from dataclasses import replace

from internal.clipboard.format import HISTORY_ONLY_TYPES, ContentType, SyncMessage
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
# How long a received clip stays remembered as "its history row was already
# written without the clipboard" (a failed write, or a crossed local copy).
# The sender's delivery budget republishes an unacked frame well inside this,
# so the redelivery applies the clipboard without stacking a second row.
HISTORY_ONLY_TTL = 300.0

# Which per-type sync switch governs which content type.  Two facts are in this
# table rather than in the filter: an image is one switch whether it is a bitmap
# or a vector (IMAGE_PNG / IMAGE_EMF), and a file list is one switch for every
# platform's payload.  FILE_REMOTE is deliberately absent — it is a pointer that
# never leaves the machine it was captured on (see HISTORY_ONLY_TYPES).
_SYNC_TYPE_SWITCHES: tuple[tuple[ContentType, str], ...] = (
    (ContentType.TEXT, "sync_text"),
    (ContentType.URL, "sync_url"),
    (ContentType.HTML, "sync_html"),
    (ContentType.RTF, "sync_rtf"),
    (ContentType.IMAGE_PNG, "sync_image"),
    (ContentType.IMAGE_EMF, "sync_image"),
    (ContentType.FILE, "sync_file"),
)


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
        # Asked about a capture identical to a recent one: does it still have a
        # peer to reach?  Only the transport can answer -- it is the one that
        # knows which peers it delivered the last frame to and which are in scope
        # now -- so the answer arrives as a callback and absence of one means
        # "drop the duplicate", which is what every host did before this existed.
        self._on_needs_resend: Callable | None = None
        self._lock = threading.Lock()
        # Serializes _do_read_and_send executions.  A rich-content capture
        # can take ~1.4s, during which _pending_timer is already cleared, so
        # a second clipboard event would otherwise start a parallel read that
        # broadcasts stale content out of order (and can drop the newest copy).
        self._read_lock = threading.Lock()
        # The last hash this manager handled in either direction, written by the
        # local capture path and by a remote write and read by both as "we just
        # saw this".  There used to be a second field beside it holding the same
        # value at every write, which made one of the two checks that read them
        # unreachable.
        self._last_local_hash: str | None = None
        # Monotonic time it was last set.  Bounds how long it keeps suppressing
        # re-captures (mirrors DEDUP_RING_TTL), so a deliberate re-copy of the
        # same content after the window is treated as new instead of being
        # suppressed forever.
        self._last_hash_ts: float = 0.0
        self._dedup_ring: list[tuple[str, float]] = []  # (hash, monotonic_ts)
        # Hashes whose "copy it again" retry has already been made, and when.
        # Bounded by the same window as the suppression it overrides, so a
        # deliberate re-copy is one more attempt rather than an unbounded stream
        # of them: the retry is for content that never arrived, not a second
        # send path for every capture the platform hands back.
        self._resend_tried: list[tuple[str, float]] = []  # (hash, monotonic_ts)
        self._sync_debounce = sync_debounce
        self._pending_timer: threading.Timer | None = None
        # The longest gap the monitor can leave between polls, asked of the
        # monitor interface: each adapter states its own value there (Linux
        # reports its backed-off idle interval, event-driven Windows reports
        # 0.0 -- no poll, no gap).  The write-suppression window below has to
        # outlast it, or a re-encoded read-back (BMP/TIFF -> PNG on
        # Linux/macOS) lands after suppression expires and is re-broadcast as
        # a fresh local copy.
        #
        # Read from the monitor this object actually ended up with.  It used to
        # be read from the constructor *parameter*, so a caller that passed no
        # monitor — and therefore got ``create_monitor()``, which on Linux and
        # macOS polls — recorded 0.0 instead of that monitor's real interval.
        # The write-suppression window built from it then expired before a
        # re-encoded read-back (BMP/TIFF -> PNG) came off the clipboard, and
        # that read-back was broadcast again as a fresh local copy.
        self._max_poll_interval = float(getattr(self._monitor, "max_poll_interval", 0.0) or 0.0)
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
        # The live Config object, handed over by the runtime that owns it (see
        # set_config).  The recording rules below are settings, so every capture
        # reads them from this object rather than keeping a snapshot taken when
        # the manager was built -- the same way `_app_filter_fn` closes over the
        # runtime's Config, and the reason a settings save takes effect on the
        # next copy instead of on the next launch.
        self._config = None
        # Hash of what was already on the clipboard when start() ran, for the
        # "skip what was copied before launch" rule.  One-shot: the first
        # capture consumes it (see `_do_read_and_send_locked`), so a later copy
        # of the same content is recorded normally.
        self._prelaunch_hash: str | None = None
        # Optional callback invoked when writing remote content to the local
        # clipboard fails.  Set via set_on_write_error().
        self._on_write_error: Callable[[], None] | None = None
        # Hashes whose content was already put into history by a failed receive
        # attempt.  A remote clip whose clipboard write failed is still recorded
        # (the row must not vanish) and reported as retryable; when the sender
        # redelivers it, the retry has to apply the clipboard *without* stacking
        # a second history row for the same content.
        self._history_only: list[tuple[str, float]] = []  # (hash, monotonic_ts)

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

    @property
    def on_needs_resend(self) -> Callable | None:
        return self._on_needs_resend

    @on_needs_resend.setter
    def on_needs_resend(self, callback: Callable | None):
        """Set the predicate asked about a capture that duplicates a recent one.

        Called with the :class:`ClipboardContent` of the duplicate; returns
        whether it still has somewhere to go.  True sends it again — the
        receive side already drops a frame it has seen, so a retry that was not
        needed costs one frame and no side effect — and False, or no callback at
        all, drops it silently, which is what the dedup did before this existed.
        """
        self._on_needs_resend = callback

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

    def set_config(self, cfg):
        """Hand the manager the live Config its recording rules read.

        A reference, not a copy: the settings API mutates this object in place,
        so a rule changed in the UI is in force on the very next capture.
        Passing ``None`` detaches it and leaves the rules at their documented
        defaults.  Callers that never wire a Config (the integration doubles)
        therefore behave exactly as before these rules existed.
        """
        self._config = cfg

    def _recording_setting(self, name: str, default):
        """One recording-rule setting, read live from the Config; *default*
        when no Config is wired."""
        cfg = self._config
        if cfg is None:
            return default
        return getattr(cfg, name, default)

    def _prelaunch_baseline(self) -> str | None:
        """Hash what is already on the clipboard, or None when the rule is off.

        Read *before* the monitor starts.  Once the monitor is listening, the
        clipboard it reports and the clipboard read here are the same object,
        and a capture could win the race: the content the user copied before
        launching would then look like a fresh copy and be recorded.  A read
        failure answers None — a baseline this process cannot take is not a
        reason to fail startup, and the rule simply does not fire.
        """
        if not self._recording_setting("skip_prelaunch_clipboard", True):
            return None
        try:
            content = self._reader.read()
        except Exception:
            logger.debug(
                "Could not read the clipboard for the pre-launch baseline", exc_info=True
            )
            return None
        if not content or content.is_empty():
            return None
        return content.hash_key()

    def _non_file_paste_bytes(self, content) -> int:
        """Total payload size of a capture, counting every non-file format.

        A FILE / FILE_REMOTE payload is a list of paths or an offer, not the
        content itself: a directory of a thousand files is a few kilobytes of
        paths and must not count as one huge paste.  Everything else is the
        bytes that would travel to peers and into history, which is what the
        size cap exists to bound.
        """
        return sum(
            len(data)
            for fmt, data in content.types.items()
            if fmt not in (ContentType.FILE, ContentType.FILE_REMOTE)
        )

    def _content_for_sync(self, content):
        """*content* reduced to the types the user allows to leave this machine.

        The history row is written from the whole capture before this runs, so
        switching a type off never costs the local record of it; only the
        outbound message is filtered, and receiving is untouched.  Each switch
        is read live, like the recording rules above.

        With every switch on — the default — nothing is disabled and the object
        comes back unchanged, so the wire and every existing behaviour are
        exactly what they were before the switches existed.  A ``FILE`` that is
        dropped leaves no offer either: the encoder builds offers out of that
        payload and cannot see it any more.
        """
        disabled = {
            fmt
            for fmt, setting in _SYNC_TYPE_SWITCHES
            if not self._recording_setting(setting, True)
        }
        if not disabled.intersection(content.types):
            return content
        return replace(
            content,
            types={fmt: data for fmt, data in content.types.items() if fmt not in disabled},
        )

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

    def restart_monitor(self) -> None:
        """Restart the clipboard monitor alone, leaving sync state untouched.

        Used when the monitor came up broken: `stop()` here would also turn
        sync off, which is not what a caller trying to *restore* capture
        wants.  Safe to call only while this manager is started — the
        monitor's callback is re-registered from the one already in use.
        """
        self._monitor.stop()
        self._monitor.start(self._on_clipboard_change)

    def clipboard_capture_error(self) -> str:
        """Why local clipboard changes will never reach this manager, or "".

        Asked of the monitor rather than tracked here: whether capture works is
        the monitor's own business, and on Windows it is decided on the
        monitor's thread well after ``start()`` returned.
        """
        reader = getattr(self._monitor, "monitor_error", None)
        if not callable(reader):
            return ""
        try:
            return reader() or ""
        except Exception:
            return ""

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

    def suppress_for_own_write(self) -> None:
        """Suppress the monitor across a clipboard write this app makes.

        A write the app performs for the user (a pulled remote file handed over
        as if it had been copied here) is not a local copy: without this the
        monitor would report it as one and broadcast it straight back to the
        machine it came from.  The window is the receive path's own — its
        debounce plus the monitor's longest poll gap — so a re-encoded read-back
        cannot land after it expires either.

        Public because the paste completion in the runtime needs it too, and the
        two paths writing that window by hand is how they would drift apart.
        """
        self._monitor.suppress_for(self._sync_debounce + self._max_poll_interval + 0.2)

    def reset_dedup_for_restore(self):
        """Clear dedup state so a history-restore write is not suppressed.

        Call before manually writing content to the clipboard (e.g. from
        the history panel) so the ensuing monitor event will be synced to
        peers instead of being filtered as a duplicate.
        """
        with self._lock:
            self._last_local_hash = None
            self._last_hash_ts = 0.0
            self._dedup_ring.clear()
            self._monitor.suppress_until = 0.0

    def start(self):
        # Read the pre-launch clipboard before the monitor is listening: this
        # is the content that was copied before the app started, and the rule
        # exists so that content is not re-recorded as a fresh copy.  One-shot;
        # the first capture consumes it.
        self._prelaunch_hash = self._prelaunch_baseline()
        self._monitor.start(self._on_clipboard_change)
        logger.info("SyncManager started on %s", self._device_name)

    def stop(self):
        with self._lock:
            # Stopping is a pause that does not end: a monitor callback, a
            # debounce timer or a capture already in flight must not keep
            # writing history or broadcasting after the owner let this go.
            self._enabled = False
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
        self._monitor.stop()
        logger.info("SyncManager stopped")

    @staticmethod
    def _outcome(accepted, *, reason="", retryable=False, clipboard=False, history=False):
        """One receive decision, in the shape both callers need.

        *accepted* is the contract the runtime already uses: True means this
        frame is settled — record history, apply the clipboard where possible,
        send the ``relay_ack`` receipt.  *retryable* says the drop was
        transient, so the sender's delivery ledger should keep the frame and
        republish it rather than mark it 未送达.  *clipboard* and *history* say
        which of the two landed, which is what "recorded but not applied"
        reads from.
        """
        return {
            "accepted": bool(accepted),
            "reason": reason,
            "retryable": bool(retryable),
            "clipboard": bool(clipboard),
            "history": bool(history),
        }

    def handle_remote_message(self, msg: SyncMessage, via_relay: bool = False) -> bool:
        """Process a clipboard message received from a peer; True when settled.

        Thin wrapper over :meth:`handle_remote_message_ex` for the runtime's
        receipt decision: only a frame that was accepted earns a ``relay_ack``.
        A frame accepted into history alone (the clipboard was busy, or a local
        copy won a crossed write) still settles the sender's ledger row — the
        content did arrive — while a transitory drop is reported as unsettled
        *and* retryable so the sender keeps it queued.
        """
        return self.handle_remote_message_ex(msg, via_relay)["accepted"]

    def handle_remote_message_ex(
        self, msg: SyncMessage, via_relay: bool = False
    ) -> dict:
        """Process a clipboard message received from a peer.

        Returns a :meth:`_outcome` dict.  ``accepted`` keeps the old bool
        contract; the rest is what the receive path needs to stop losing rows:

        * a clipboard write that fails (Windows clipboard temporarily held by
          another app is routine) still records the clip in history, so the row
          the user is looking for exists, and reports ``retryable`` so the
          sender keeps the frame instead of marking it delivered or dropping
          it;
        * a crossed write (a local copy landed within the debounce window) keeps
          the local clipboard but records the remote clip in history;
        * a frame whose content is already here is *accepted*, because the peer
          delivering it again is exactly what a lost receipt causes.

        ``via_relay`` says which of the two transports delivered the frame: the
        relay router passes True, the LAN router leaves it False.  It is
        recorded on the history row as the route the clip came in on, which a
        reader needs because the same peer can be reachable both ways and the
        device name alone cannot say which one carried this copy.
        """
        with self._lock:
            if not self._enabled:
                return self._outcome(False, reason="disabled")

            # Crossed writes: near-simultaneous copies should resolve by copy
            # time, not arrival order.  If the local clipboard changed very
            # recently, the local copy is probably newer — keep it.  The remote
            # clip is still history: dropping it here used to lose the row
            # entirely, and a user reading the list cannot tell a lost clip
            # from one that never left the peer.
            #
            # Flagged inside the lock and handled outside it: the history-only
            # path takes the lock again, and this lock is not re-entrant.
            crossed = bool(self._local_capture_active) or (
                time.time() - self._last_local_copy_time < self._sync_debounce
            )
        if crossed:
            return self._record_history_only(msg, "crossed-write", via_relay)

        content = msg.content
        if content.is_empty():
            return self._outcome(False, reason="empty")

        # Stamp the sender's device ID so history shows the correct source.
        content.source_device = msg.source_device
        # ...and the route it arrived on, so the row can say "over the relay"
        # or "over the local link" and not just who sent it.
        content.transport = "relay" if via_relay else "lan"

        content_hash = content.hash_key()

        with self._lock:
            # The content is already here (we sent it, or an earlier delivery
            # landed): accept so the sender's receipt settles.  Returning False
            # made a lost ack turn into a retry storm that ended in 未送达.
            if content_hash == self._last_local_hash:
                return self._outcome(True, reason="duplicate", history=True)
            if self._dedup_seen(content_hash):
                return self._outcome(True, reason="duplicate", history=True)

            # Receive-side rate limit ... Retryable: the frame is
            # fine, the budget is the only thing in the way.
            #
            # Measured on the monotonic clock.  A wall-clock window stops
            # being drained the moment the clock steps backwards (NTP
            # correction, sleep resume): the age of the oldest entry goes
            # negative, nothing is ever popped, the deque stays at its cap and
            # every clip from every peer is reported rate-limited from then
            # on — a permanent, silent loss of the remote clipboard.
            now = time.monotonic()
            while (
                self._remote_apply_times and now - self._remote_apply_times[0] > REMOTE_RATE_WINDOW
            ):
                self._remote_apply_times.popleft()
            if len(self._remote_apply_times) >= REMOTE_RATE_MAX:
                return self._outcome(False, reason="rate-limited", retryable=True)

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
                return self._outcome(False, reason="disabled")

        # Write to local clipboard.  Guarded so a clipboard-writer failure
        # drops this one message instead of killing the peer connection.
        #
        # The clipboard comes FIRST and history only follows a successful
        # write.  Recording history first meant a failed write still left a
        # row in the list (and broadcast a history_updated to every web
        # client) for content that never reached the clipboard: the user saw
        # the item arrive, clicked it, and got something else -- a ghost entry
        # with no way to tell it apart from a real one.
        # A clip that only *points at* a file on another device is a history row
        # and nothing else: no clipboard can hold a reference to a file this
        # machine does not have.  Those formats are dropped before the write —
        # handing one to a writer would clear the clipboard and then report
        # success, which is what the Windows URL branch used to do — and a clip
        # left with nothing to write is not written at all, because a platform
        # write is preceded by an empty-the-clipboard step.  History still gets
        # the whole clip, below.
        writable = content.without(HISTORY_ONLY_TYPES)
        if writable.is_empty():
            logger.info(
                "Remote clip from %s holds only history-only formats (%d format(s))"
                " — nothing to put on the clipboard",
                msg.source_device,
                len(content.types),
            )
            wrote = True
        else:
            try:
                logger.info(
                    "Writing remote clipboard from %s: %d format(s)",
                    msg.source_device,
                    len(writable.types),
                )
                wrote = self._writer.write(writable)
            except Exception:
                wrote = False
                logger.exception("Failed to write remote clipboard content")

        if not wrote:
            # Surface the failure (desktop notification) without letting it
            # crash the sync loop — a transient clipboard-busy error on the
            # receiver should not kill the peer connection.  The clip is still
            # recorded in history and the outcome says retryable: the sender
            # keeps the frame (its ledger requeues an unacked send) and the
            # redelivery applies the clipboard without a second history row.
            cb = self._on_write_error
            if cb is not None:
                try:
                    cb()
                except Exception:
                    logger.debug("on_write_error callback failed", exc_info=True)
            return self._record_history_only(
                SyncMessage(content=content, msg_id=msg.msg_id,
                            source_device=msg.source_device),
                "clipboard-write-failed",
                via_relay,
                retryable=True,
            )

        # The write landed — now it is real, so record it in history.  Skip the
        # row when a failed earlier attempt already wrote it (redelivery).
        if not self._history_recorded(content_hash) and self._history is not None:
            try:
                self._history.add(content)
            except Exception:
                logger.debug("Failed to add remote content to history", exc_info=True)

        self._notify_history_change()

        # The message was accepted (clipboard write applied + history).
        # Count this write toward the rate limit AND record loop-prevention
        # state only now that it actually landed.
        with self._lock:
            self._remote_apply_times.append(time.monotonic())
            self._dedup_ring_remember(content_hash)
            self._last_local_hash = content_hash
            self._last_hash_ts = time.monotonic()

        return self._outcome(True, reason="applied", clipboard=True, history=True)

    def _record_history_only(self, msg, reason, via_relay, retryable=False):
        """Record a remote clip in history without touching the clipboard.

        Used where applying it would be wrong (a local copy won a crossed
        write) or impossible right now (the clipboard is held by another
        process).  Either way the row the user is looking for exists and the
        sender is told the right thing: accepted when the content is here, and
        retryable when only the clipboard write is still owed.
        """
        content = msg.content
        content.source_device = msg.source_device
        content.transport = "relay" if via_relay else "lan"
        content_hash = content.hash_key()
        recorded = False
        # One critical section for the whole check-then-record.  These two
        # used to run outside the lock while the transport threads deliver —
        # LAN and relay both, and a redelivery crossing a first arrival is
        # exactly what `_on_needs_resend` produces.  Both helpers rebuild the
        # list they are handed, so two callers interleaving lost one another's
        # entry: the "already recorded" answer went missing and the same clip
        # was written to history twice.
        with self._lock:
            already = self._history_recorded(content_hash)
            if not already and self._history is not None:
                try:
                    self._history.add(content)
                    recorded = True
                except Exception:
                    logger.debug(
                        "Failed to add remote content to history", exc_info=True
                    )
            else:
                recorded = already
            if recorded:
                self._history_only_remember(content_hash)
            # Remembered even when the clipboard write is still owed: the
            # redelivery carries the same content and must apply the clipboard,
            # not stack a second row.
            self._history_only_remember(content_hash)
            if not retryable:
                # A retryable drop is coming back for the clipboard write, so it
                # must not be caught by loop-prevention dedup on the way in.  An
                # accepted (settled) history-only row does enter the ring, so a
                # further duplicate is recognised as already here.
                self._dedup_ring_remember(content_hash)
        if recorded:
            self._notify_history_change()
        return self._outcome(
            not retryable,
            reason=reason,
            retryable=retryable,
            clipboard=False,
            history=recorded,
        )

    def _history_recorded(self, content_hash: str) -> bool:
        """True when *content_hash* already has a history row from a receive."""
        now = time.monotonic()
        self._history_only = [
            (h, ts) for h, ts in self._history_only if now - ts <= HISTORY_ONLY_TTL
        ]
        return any(h == content_hash for h, _ in self._history_only)

    def _history_only_remember(self, content_hash: str) -> None:
        now = time.monotonic()
        self._history_only = [
            (h, ts) for h, ts in self._history_only if now - ts <= HISTORY_ONLY_TTL
        ]
        if not any(h == content_hash for h, _ in self._history_only):
            self._history_only.append((content_hash, now))
            if len(self._history_only) > 64:
                self._history_only = self._history_only[-64:]

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

    def _resend_duplicate(self, content_hash: str, content) -> bool:
        """Whether a capture identical to a recent one should go out anyway.

        The checks that call this exist for two loop cases — the platform handing
        the same clipboard back, and this machine's own write being read back —
        and for both, dropping the capture is right.  It is wrong for the third
        case, which is the one a user reports: content copied while nothing could
        carry it, then copied again once something could.  Suppressing that by
        content suppresses it forever, because the content is the one thing that
        did not change.

        The transport is asked, because only the transport knows: it holds what
        the last frame reached and who is in scope now.  A caller that never sets
        ``on_needs_resend`` — every host before this existed — keeps the old
        behaviour exactly.

        One retry per content per window.  The retry is for a copy that never
        arrived, not a second send path for every capture the platform repeats,
        and a re-sent frame costs the receiver one dedup hit it already knows how
        to drop.
        """
        ask = self._on_needs_resend
        if ask is None:
            return False
        # One attempt per content per window, decided under the lock; the
        # question itself is asked outside it (see the caller).
        with self._lock:
            now = time.monotonic()
            self._resend_tried = [
                (h, ts) for h, ts in self._resend_tried if now - ts <= DEDUP_RING_TTL
            ]
            if any(h == content_hash for h, _ in self._resend_tried):
                return False
            self._resend_tried.append((content_hash, now))
        try:
            allowed = bool(ask(content))
        except Exception:
            logger.debug("on_needs_resend failed", exc_info=True)
            allowed = False
        if not allowed:
            with self._lock:
                self._resend_tried = [
                    (h, ts)
                    for h, ts in self._resend_tried
                    if not (h == content_hash and ts == now)
                ]
            return False
        # Said out loud because it is the difference between "the copy was
        # swallowed" and "the copy went out again": the report that led here was
        # only diagnosable from the code, and this line is what makes the next one
        # answerable from the log.
        logger.info("Copy repeated and nothing had carried it — sending it again")
        return True

    def _reset_dedup_hashes(self) -> None:
        """Clear the loop-prevention hash state.

        Used when content is dropped as noise (empty clipboard, whitespace-only
        text, app-filtered copy) so a later copy of previously-seen content is
        not suppressed against stale hashes.
        """
        with self._lock:
            self._last_local_hash = None
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

        # App filter: drop content from disallowed source applications.  The
        # predicate is the runtime's `is_app_allowed`, which also applies the
        # source-exclusion substrings, so both rules land here as one decision.
        if self._app_filter_fn is not None and not self._app_filter_fn(source_app):
            logger.debug("Clipboard from disallowed app filtered out: %s", source_app)
            self._reset_dedup_hashes()
            return

        # Password-manager "do not record" marker: the application that produced
        # this copy asked the OS/clipboard manager not to keep it, and the user
        # has not turned that request off.  Dropped before history and before
        # the network, so a secret never reaches either.  Read live: a switch
        # saved in the settings page applies to the very next copy.
        if getattr(self._monitor, "last_concealed", False) and self._recording_setting(
            "skip_password_manager_clips", True
        ):
            logger.debug(
                "Clipboard was marked concealed by its source application — "
                "not recorded or synced"
            )
            self._reset_dedup_hashes()
            return

        # Content-based dedup — a single canonical hash for all loop-prevention
        # state (_last_local_hash, dedup ring).
        content_hash = content.hash_key()

        # Pre-launch content: the first capture after start() is compared with
        # what the clipboard held before the monitor was listening; when they
        # are the same this is not a copy the user just made, so it is not
        # recorded.  One-shot — the baseline is consumed here whether it matched
        # or not, and a later copy of the same content is recorded normally.
        # The setting is read again at capture time so turning it off between
        # start and the first copy still takes effect.
        if self._prelaunch_hash is not None:
            started_with = self._prelaunch_hash
            self._prelaunch_hash = None
            if started_with == content_hash and self._recording_setting(
                "skip_prelaunch_clipboard", True
            ):
                logger.debug(
                    "Clipboard content predates this launch — not recorded or synced"
                )
                self._reset_dedup_hashes()
                return

        # Size cap for a single non-file capture.  File lists are path lists
        # rather than payloads and are not counted; 0 disables the cap.  Read
        # live like the rules above.
        limit_mb = self._recording_setting("max_non_file_paste_mb", 8.0)
        try:
            limit_bytes = max(0.0, float(limit_mb)) * 1024 * 1024
        except (TypeError, ValueError):
            limit_bytes = 0.0
        if limit_bytes:
            paste_bytes = self._non_file_paste_bytes(content)
            if paste_bytes > limit_bytes:
                logger.debug(
                    "Clipboard capture holds %d non-file bytes, over the %.1f MB "
                    "cap — not recorded or synced",
                    paste_bytes,
                    limit_bytes / 1024**2,
                )
                self._reset_dedup_hashes()
                return

        with self._lock:
            # A pause requested mid-capture takes effect here: nothing below
            # may enter history or reach the network once the user paused.
            if not self._enabled:
                return
            # One reading, two jobs: a duplicate capture (the same content
            # re-read after the debounce) and this machine's own write coming
            # back off the clipboard.  Bounded by the same TTL as the dedup
            # ring, so a deliberate re-copy of the last content after
            # DEDUP_RING_TTL is treated as new instead of being suppressed
            # forever, and an echoed-back remote write stops suppressing once
            # the ring window has passed.
            #
            # This was two checks over two fields every writer set to the same
            # value, so the second could only be reached when the first was
            # already false -- an unreachable branch whose two comments read as
            # though they meant different things.
            #
            # Inside the window a duplicate is dropped *unless the transport says
            # a peer could take it now* -- see `_resend_duplicate`: the case the
            # window is wrong about is a copy made while nothing could carry it.
            duplicate = (
                content_hash == self._last_local_hash
                and time.monotonic() - self._last_hash_ts <= DEDUP_RING_TTL
            ) or self._dedup_seen(content_hash)

        if duplicate and not self._resend_duplicate(content_hash, content):
            # Asked outside the lock.  The answer comes from the transport,
            # which takes the transport lock on the way: holding this one while
            # asking fixed an ordering between the two that any path holding
            # them the other way round turns into a deadlock, and this lock is
            # not re-entrant, so a callback that reaches back into the manager
            # would wedge it on its own.  Asking costs a network enumeration,
            # which also has no business running inside a critical section.
            return

        with self._lock:
            # Re-read under the lock: the pause check and the dedup state below
            # are only meaningful together, and the lock was released to ask
            # the transport.
            if not self._enabled:
                return
            self._last_local_hash = content_hash
            self._last_hash_ts = time.monotonic()

            self._dedup_ring_remember(content_hash)

        # Record in clipboard history — once per action.  The row's id comes
        # back out because a file entry crosses as an offer naming it: that id is
        # how a peer's request is matched back to a row here, and so how *this*
        # machine, rather than the one asking, decides which paths a request may
        # resolve to.
        if self._history is not None:
            try:
                content.entry_id = self._history.add(content, source_app=source_app) or ""
            except Exception:
                logger.debug("Failed to add to clipboard history", exc_info=True)

        self._notify_history_change()

        # Per-type sync switches: the row above keeps the whole capture, and only
        # what the user allows leaves the machine.  Read live, and a no-op when
        # every switch is on (the default).
        outgoing = self._content_for_sync(content)
        if outgoing.is_empty():
            logger.debug(
                "Clipboard capture holds only content types switched off for sync "
                "— recorded in history, not broadcast"
            )
            return
        if len(outgoing.types) != len(content.types):
            logger.debug(
                "Sync type switches dropped %d of %d format(s) from the outgoing clip",
                len(content.types) - len(outgoing.types),
                len(content.types),
            )

        msg = SyncMessage(
            content=outgoing,
            msg_id=uuid.uuid4().hex,
            source_device=self._device_id,
        )

        logger.info("Local clipboard changed: %d format(s)", len(content.types))

        # Don't broadcast content that carries no encodable formats — a capture
        # whose every format the wire cannot name encodes to an empty frame.
        # FILE and URL are no longer in that set: a file goes out as an offer
        # naming the row above, and a URL as itself, so what is left here is a
        # clip carrying nothing the encoder has a name for.
        from internal.protocol.codec import has_syncable_types

        if not has_syncable_types(outgoing):
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
