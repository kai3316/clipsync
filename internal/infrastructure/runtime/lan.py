"""Owned LAN discovery, two-sided pairing and clipboard sync without a GUI."""

import inspect
import logging
import os
import platform
import random
import secrets
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from internal.application.errors import ApplicationError
from internal.application.use_cases.transfers import format_eta
from internal.clipboard import file_ref, format
from internal.clipboard.clipboard import strip_rich_formats
from internal.clipboard.file_ref import MAX_OFFER_ENTRIES
from internal.clipboard.file_ref import summary as file_summary
from internal.clipboard.filter import ContentFilter
from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.clipboard.platform import create_monitor, create_reader, create_writer
from internal.clipboard.source_tracker import is_app_allowed
from internal.config.config import (
    PeerInfo,
    chosen_device_name,
    config_dir,
    config_lock,
)
from internal.data.logs import stage_collected_log, write_share_copy
from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.internet_pairing import (
    InternetPairingService,
    is_provisional_key,
)
from internal.infrastructure.runtime.relay_delivery import RelayDelivery
from internal.platform.notify import notification_mgr
from internal.protocol.codec import (
    BINARY_HEADER_SIZE,
    CLIP_FILE_MSG_TYPES,
    ENDING_MSG_TYPES,
    PAIRING_MSG_TYPES,
    UNPAIRED_FILE_MSG_TYPES,
    decode_message,
    encode_frame,
    encode_message,
    has_syncable_types,
)
from internal.security.fingerprint import sas_code
from internal.security.pairing import PAIRING_STATUS_PAIRED, fingerprint_short
from internal.sync.ai_config import AIConfigManager
from internal.sync.file_transfer import MAX_FILE_SIZE, FileTransferManager
from internal.sync.manager import SyncManager
from internal.sync.nearby_chat import CHAT_MSG_TYPES, ChatManager
from internal.system import updater
from internal.system.archive import ArchiveEmptyError, create_archive
from internal.system.update_service import _verdict_is_rejection
from internal.transport import relay_chunks
from internal.transport.connection import (
    _REJECT_REASONS,
    MAX_FRAME_SIZE,
    NO_PAIRING_MARKER_SINCE,
    TransportManager,
)
from internal.transport.discovery import Discovery
from internal.transport.ids import peer_id_hash
from internal.transport.peer_id import expand_id_forms
from internal.transport.relay import (
    MAX_RELAY_PAYLOAD,
    RelayTransport,
    build_paho_client,
    frame_limit_for,
    netpair_device_tag,
)
from internal.transport.relay_chunks import (
    ClipChunkAssembler,
    ClipChunkSends,
    is_clip_transfer_id,
    mint_clip_transfer_id,
    split_frame,
)
from internal.version import __version__

logger = logging.getLogger(__name__)

# How long a 下载 request licenses the peer to send unprompted.  The window is
# not a guess at transfer time: what it bounds is how long the *receiver* will
# keep believing a `clip_file` frame is one it asked for.  Long enough that a
# peer preparing a folder archive is still inside it, short enough that a
# request abandoned by a crash stops being an open door.
CLIP_FILE_WINDOW = 300.0

# How long an update request licenses the answering peer to send the blob.  The
# same idea one step tighter: an update asset is tens of megabytes, so the
# window has to outlast the transfer rather than only its preparation, and it is
# still bounded because what it leaves open is the one transfer kind that never
# asks the user anything.
UPDATE_WINDOW = 1800.0

# A failed automatic fetch is retried once for a peer that is coming back up.  These
# are the codes that mean "not this second"; every other refusal is an answer.
_UPDATE_RETRY_CODES = frozenset({"update.peer_unreachable", "update.fetch_failed"})
_UPDATE_RETRY_SECONDS = 4.0

# How long the "send this device the update" click waits for the peer to say
# whether it wants the asset.  The answer is one frame, and the peer answers
# before it does anything else with the offer, so this only has to outlast a
# round trip on a slow link — but it is a wait in front of a person who just
# clicked a button, so it stays short.  A peer from a build with no answer frame
# at all never sends one, and that is what the timeout reads as.
OFFER_ANSWER_TIMEOUT = 4.0

# How long after an automatic attempt at one peer before it is tried again.
#
# 自动更新 runs on its own, so the failure it has to survive is the ordinary
# one: a peer advertising a build it cannot actually hand over — a machine that
# upgraded by hand, a dev build of a newer version, an asset the receiving end
# ends up refusing.  None of those is a reason to stop asking, and all of them
# are a reason not to ask four times a second, which is what the maintenance
# tick would do.  Long enough to be nothing like a loop, short enough that a
# peer which fetches the release in the meantime is picked up the same session.
AUTO_UPDATE_RETRY = 1800.0

# How long after an automatic request of ours a peer's refusal is read as the
# answer to it.  The peer answers "nothing to send" in one frame, so this only
# has to outlast a slow link -- and it exists so that the automatic attempt's
# own failure is not reported to somebody who never asked for it, while a
# refusal arriving outside it (a click, at a peer that happens to have nothing)
# still is.
AUTO_UPDATE_ANSWER_WINDOW = 30.0

# How long a log request licenses the peer to send the file.  Generous for the
# update window's reason — a log can be several megabytes and the answering side
# builds the redacted copy before it sends — and bounded for the same one: what
# a live entry permits is an unprompted transfer.
LOG_WINDOW = 1800.0


def _local_platform() -> tuple[str, str]:
    """This machine's os/arch, spelled the way the mDNS TXT records spell them.

    One spelling for both ends of the comparison: the values here are read
    against a peer's advertised ``os``/``arch``, which come off its own TXT
    records -- lowercased in one place and compared in the other, so the two
    have to be produced the same way or every peer looks like a different
    platform.
    """
    return platform.system().lower(), (platform.machine() or "").lower()


def _reject_reason_name(reason) -> str:
    """The wire reason as a word the app layer and the UI can switch on.

    A name rather than the number, because the number would have to be duplicated
    in the TypeScript and in the phone client, and a duplicated constant is one
    that drifts.  Unknown codes come back spelled as the code, so a newer peer's
    reason is still reported rather than silently reported as "unspecified".
    """
    try:
        code = int(reason or 0)
    except (TypeError, ValueError):
        return "unspecified"
    if code in _REJECT_REASONS:
        return _REJECT_REASONS[code]
    return f"code-{code}"


def _short_fingerprint(certificate_pem):
    """Short fingerprint of a pinned certificate; '' when absent or malformed."""
    if not (certificate_pem or "").strip():
        return ""
    try:
        return fingerprint_short(certificate_pem)
    except Exception:
        return ""


class _OwnedSyncManager(SyncManager):
    """Keep existing debounce/dedup algorithms, but account for their callbacks."""

    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        super().__init__(*args, **kwargs)

    def _on_clipboard_change(self):
        return self._owner._background(super()._on_clipboard_change)

    def _do_read_and_send(self):
        return self._owner._background(super()._do_read_and_send)


class _OwnedDiscovery(Discovery):
    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        # The registration runs on its own thread (see Discovery.start), so a
        # failure has no caller to raise to.  Report it the way this runtime
        # reports every other background failure, or a device that is on the
        # LAN but not advertising looks like a device nobody is running.
        kwargs.setdefault("on_error", self._registration_failed)
        super().__init__(*args, **kwargs)

    def _registration_failed(self):
        self._owner._publish(
            "runtime.error",
            {"code": "MDNS_REGISTER_FAILED", "message": "Could not advertise on the LAN"},
        )

    def _network_watch_loop(self):
        return self._owner._background(super()._network_watch_loop)

    def _presence_loop(self):
        # Accounted for like the network watcher: it is a loop of this runtime's
        # own, and a stop waits for the threads it started (see _cleanup).
        return self._owner._background(super()._presence_loop)

    def _wake_recovery(self):
        return self._owner._background(super()._wake_recovery)


class _Reader:
    def __init__(self, owner, reader):
        self._owner, self._reader = owner, reader

    def read(self):
        try:
            return self._reader.read()
        except Exception:
            self._owner._error("CLIPBOARD_READ_FAILED")
            return None


class _Writer:
    def __init__(self, owner, writer):
        self._owner, self._writer = owner, writer

    def write(self, content):
        try:
            return self._writer.write(content)
        except Exception:
            # SyncManager otherwise logs the platform exception, which may
            # include clipboard text or local paths.
            return False


class _History:
    def __init__(self, owner, history):
        self._owner, self._history = owner, history

    def add(self, content, **kwargs):
        try:
            return self._history.add(content, **kwargs)
        except Exception:
            self._owner._error("HISTORY_WRITE_FAILED")
            return None


class LanRuntime:
    """Compose existing LAN engines with explicit resource ownership.

    Injected dependencies are instances, not factories. ``history`` is the
    repository (its ``add`` API), ``events`` is an EventJournal, and
    ``save_config`` takes no arguments and saves the shared config securely.
    A stopped runtime is single-use. ``stop()`` has a five-second wait budget;
    False requires retaining this runtime, history and identity and retrying.
    Snapshots are cached, so EventJournal.snapshot never performs network I/O,
    persistence or publication while holding the journal lock.
    """

    STOP_TIMEOUT = 5.0
    REFRESH_INTERVAL = 0.25
    # How short the gap between two progress events may be.  Progress is
    # reported once per 256 KB chunk and every event costs the desktop a whole
    # snapshot refresh plus a WebSocket frame to the phone, so the events are
    # spaced to what a bar can show rather than to what the disk can report.
    PROGRESS_INTERVAL = 0.25
    PAIRING_SEND_WAIT = 12.0
    # How long a fragmented clipboard frame may sit with chunks missing before
    # this side asks its sender for them again (see ``_sweep_clip_chunks``).
    # The value is ``relay_chunks.STALL_GRACE`` narrowed into a class attribute
    # so a test can shrink it the way it already shrinks ``REFRESH_INTERVAL``.
    CLIP_CHUNK_STALL_GRACE = relay_chunks.STALL_GRACE
    # How long a device keeps its row after its mDNS record lapses.
    #
    # An mDNS announcement is the advertiser's to schedule and the network's to
    # drop, and this one comes and goes while the device has not moved: the log
    # of one working day holds the same Mac found and lost four seconds apart,
    # then found again two seconds later, four times over -- and, twice, gone
    # for fifty minutes on a machine that was on the network throughout.  The
    # row was deleted on the first missed goodbye, so a device that never left
    # flickered in and out of the list, and the one the reader was looking for
    # was usually not on screen.  A sighting that is stale is not a device that
    # has gone; the transport is what decides *that*, from the socket, and a
    # peer that really left is dropped there.  This is only how long the last
    # thing a device said about itself stays on its row.
    SIGHTING_GRACE = 300.0
    # How long a lapsed sighting still counts as *here* -- as opposed to what it
    # is still allowed to answer about.  Short, because "the record lapsed" is no
    # longer a guess about a network that drops packets: the presence round has
    # established that the device went quiet for twenty seconds and stopped
    # answering questions.  One round of grace on top of that, so a goodbye, or
    # an answer that missed the round's window, cannot flicker the row — and the
    # round after it is what proves the device was never gone.
    #
    # Only membership reads this: it decides whether an unpaired device is on
    # the list.  Everything a row is *made of* — the name a peer published, the
    # address a click would dial, the version and platform the update buttons
    # are decided from — keeps reading SIGHTING_GRACE, because a device that is
    # merely quiet is still the device this machine knows how to reach.
    PRESENCE_GRACE = 10.0
    # How often a deferred ending notice re-dials a peer it cannot reach.  The
    # maintenance loop runs four times a second, which is far more often than a
    # connection attempt needs and would keep several in flight at once.
    ENDING_DIAL_RETRY = 1.0
    DEVICE_PING_TIMEOUT = 4.0
    # The shortest gap between two rounds of "what are you?" asked of one peer.
    # The entry point is the devices page's refresh button, and the answer it
    # asks for cannot have changed in less than this — mDNS re-announces on a
    # sixty-second round, so a shorter floor would only put frames on the wire
    # for a reader who presses the button twice.
    FACTS_ASK_INTERVAL = 5.0
    # Legacy waited this long for a chat peer's connection to come up.
    CHAT_CONNECT_TIMEOUT = 15.0
    # Legacy held a pairing notice back for ~1.2s so a chat invite arriving on
    # the same connection could suppress it; the refresh loop supplies the wait.
    PAIRING_NOTICE_DELAY = 1.2
    # Bounds one pushed snippet. Legacy /api/push had no limit; a frame over
    # MAX_FRAME_SIZE is refused by the broadcast path anyway, so this only
    # keeps an absurd request from being read, stripped and stored first.
    MAX_PUSH_CHARS = 100_000
    # Asked at most this often per peer: a peer that keeps reconnecting with a
    # changed certificate would otherwise stack prompts.  Legacy throttled the
    # same way (its ``_cert_alert_throttle``).
    CERT_ALERT_THROTTLE = 30.0

    def __init__(
        self,
        config,
        pairing,
        encryption,
        history,
        events,
        save_config,
        *,
        monitor=None,
        reader=None,
        writer=None,
        discovery=None,
        transport=None,
        open_url=None,
    ):
        self.config = config
        self.pairing = pairing
        self.events = events
        self._save_config = save_config
        # Injected so tests never launch a real browser for a peer's nav_url.
        self._open_url = open_url if open_url is not None else webbrowser.open
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._active = {}
        self._state = "created"
        self._stop_event = threading.Event()
        self._start_done = threading.Event()
        self._maintenance = None
        self._cleanup_thread = None
        self._cleanup_ok = False
        self._started = set()
        self._discovered = {}
        # Sightings whose mDNS record has lapsed, and when they lapsed: see
        # SIGHTING_GRACE.  Kept apart from ``_discovered`` so the live set stays
        # exactly that -- what is being announced now.
        self._lost_sightings = {}
        self._connecting = {}
        self._deferred = {}
        self._ending_dial_at = {}
        self._snapshot = {"items": []}
        self._pairing_ops = threading.RLock()
        self._probes = {}
        self._probes_lock = threading.Lock()
        # What a peer answered when asked directly about itself (see
        # `_handle_device_probe`): its own account of its version, platform,
        # architecture, application and name, stamped with when this machine
        # heard it.  A second source beside the mDNS sighting — the two disagree
        # exactly when a peer has upgraded and one of the two is lagging — and
        # the rule between them is "the newer observation wins", never "this
        # kind beats that kind": a broadcast read ten minutes ago must not
        # outrank a reply to a question asked ten seconds ago, and a late
        # announcement from before an upgrade must not outrank either.
        self._answers = {}
        # When each peer's sighting was last *heard*, keyed by the resolved id on
        # the same clock as an answer's own stamp.  The two sources are ranked by
        # it (`_answer_overlay`), and without it there is no way to say which of
        # them is the stale one.
        self._sighted_at = {}
        # The ping_ids already taken.  A pong that arrives twice (a retransmit)
        # would otherwise restamp the answer and so outrank a newer sighting.
        self._answered_pings = set()
        # When each peer was last asked, so opening the devices page twice in a
        # row puts one round of frames on the wire rather than two.
        self._asked_at = {}
        self._dirty = threading.Event()
        self._persist_dirty = False
        # When a progress event was last published (see _progress_ready).
        self._progress_at = 0.0
        # Presence and pairing transitions the legacy host detected by polling:
        # the last connected set, and per peer the pending code plus whether it
        # was already announced (``(code, first_seen, announced)``).
        self._connected_seen = set()
        self._pairing_seen = {}
        # A peer whose presented certificate no longer matches its pin is
        # rejected, and the certificate it presented is held here until the user
        # answers: "trust again" has to pin what was presented, and the dial side
        # can only report the mismatch — it never sees the certificate.
        self._pending_certs = {}
        self._cert_alert_seen = {}
        self._last_error = ""
        self._last_error_detail = ""
        # A clipboard frame larger than one relay message crosses as binary
        # chunks (see ``internal/transport/relay_chunks.py``).  These two hold
        # the receiving and the sending half of that exchange: the assembler
        # rebuilds what a peer cut, and the send registry answers a peer's
        # request for the chunks it is missing.
        self._clip_chunks = ClipChunkAssembler()
        self._clip_sends = ClipChunkSends()
        # The pairing panel reports the relay's own state beside the peers', so
        # it is given the accessor that already answers it: a peer list that
        # reads 离线 throughout means one thing when this machine is on the
        # relay and quite another when it is not, and only that fact separates
        # the two.
        self.internet_pairing = InternetPairingService(
            config, save_config, relay=None, relay_state_fn=self.relay_state
        )
        self.relay = None
        self.delivery_queue = RelayDeliveryQueue(config_dir() / "relay_pending.json")
        # Ledger + retry policy over the persisted queue: rows loaded from disk
        # re-enter it as queued, so the legacy "已送达" view survives a restart.
        self.delivery = RelayDelivery(
            self.delivery_queue, self._relay_publish_to_peer, self._delivery_changed
        )
        self.internet_pairing.on_unpair = self._internet_unpaired
        self.internet_pairing.send_enroll = self._send_relay_enroll
        self._chat_muted = set(getattr(config, "chat_muted_peers", []) or [])
        self.content_filter = ContentFilter(config.filter_enabled_categories)
        if monitor is None:
            interval = config.clipboard_poll_interval
            if config.low_memory_mode:
                interval = max(interval, 2.0)
            monitor = create_monitor(poll_interval=interval)
        monitor.set_source_tracking(config.source_tracking_enabled)
        # Shared with SyncManager: push_text writes through the same wrapper so
        # a platform write failure is reported identically on both paths.
        self._clipboard = _Writer(
            self, writer if writer is not None else create_writer()
        )
        self.sync = _OwnedSyncManager(
            self,
            config.device_id,
            config.device_name,
            reader=_Reader(self, reader if reader is not None else create_reader()),
            writer=self._clipboard,
            monitor=monitor,
            history=_History(self, history) if history is not None else None,
            sync_debounce=config.sync_debounce,
            retry_enabled=config.retry_capture_enabled,
        )
        self.sync.set_enabled(False)
        self.sync.set_app_filter(lambda app: is_app_allowed(app, self.config))
        self.sync.on_send = lambda msg: self._background(self._on_local_sync, msg)
        # Asked about a copy identical to a recent one: the manager drops those,
        # which is right for the two loop cases it exists for and wrong for the
        # copy that was made while nothing could carry it.  This side is the one
        # that knows whether anybody is in scope now.
        self.sync.on_needs_resend = lambda content: bool(self._clip_targets())
        self.sync.on_history_change = lambda: self._publish("history.changed", {})
        self.sync.set_on_write_error(lambda: self._error("CLIPBOARD_WRITE_FAILED"))
        self.file_transfer = FileTransferManager(
            config.device_id,
            output_dir=getattr(config, "file_receive_dir", "") or None,
            transfer_timeout=config.transfer_timeout,
        )
        self.file_transfer.set_on_transfer_progress(
            lambda tid, progress: self._publish_progress(
                "transfer.progress", {"transfer_id": tid, "progress": progress}, progress
            )
        )
        self.file_transfer.set_on_transfer_complete(
            lambda tid, success, cancelled, status: self._on_transfer_complete(
                tid, success, cancelled, status
            )
        )
        self.file_transfer.set_on_transfer_request(
            lambda tid, name, size, mime, send_fn, *, relay=False: self._publish(
                "transfer.request",
                {
                    "transfer_id": tid,
                    "filename": name,
                    "size": size,
                    "mime": mime,
                    # The route the offer was cut for, decided by the manager and never on the
                    # wire: a relay-sized offer is that sender's own signal that the file is
                    # arriving over the internet, and the reader is told before they accept it.
                    # False is the LAN default, so nothing a LAN arrival carries changes.
                    "relay": bool(relay),
                },
            )
        )
        # Peer-sent update blobs arrive as ordinary transfers (kind="update") and
        # are handed to the update service instead of the file-received flow.
        self.file_transfer.set_on_file_received(self._on_file_received)
        # Whether an inbound `clip_file` may skip the consent prompt is not the
        # sender's to decide; the manager asks this machine's own ledger.
        self.file_transfer.set_clip_file_guard(self._clip_file_outstanding_for)
        # And an `update` blob is one *this* device asked a peer for.  That
        # request is the whole consent gate on an unpaired push: without it the
        # kind alone would let any device on the network drop a file on this
        # disk with nobody asked.
        self.file_transfer.set_update_guard(self._update_outstanding_for)
        # A `log` blob is the same bargain: this machine asked that peer for its
        # log, and the asking is the only thing that lets the answer in.
        self.file_transfer.set_log_guard(self._log_outstanding_for)
        # An ordinary file is the one kind with nothing written down on this
        # side to license it, so the setting decides: on, it is taken on arrival
        # like a chat attachment; off, it raises the accept prompt and the
        # reader answers.  Pairing is deliberately not part of that question --
        # the transfer page refused an unpaired sender outright, which made it
        # unusable between two devices that simply had not paired, and left the
        # rule different from chat's for no reason a reader could see.
        self.file_transfer.set_file_open_to_all(bool(getattr(config, "file_open_to_all", True)))
        # The temp archives of folder sends, by transfer id, waiting to be
        # unlinked when their transfer reaches a terminal state.  Its own lock
        # rather than the runtime's: the completion callback runs on the
        # transfer's own thread and must not queue behind a command.
        self._outgoing_archives = {}
        self._archive_lock = threading.Lock()
        self._update_sink = None
        self._update_verdict_reporter = None
        # peer device_id -> monotonic deadline, the ledger of peers this machine
        # has actually asked for a cached update asset.  Consulted by the update
        # guard above; a peer's answer that arrives inside the window is the only
        # update blob this side accepts.
        self._update_expectations: dict[str, float] = {}
        # device_id -> (event, answer) for an update_offer this machine has sent
        # and is waiting on.  The offer's answer is either the peer asking for
        # the asset or its refusal, and the click that sent the offer reports
        # what came back rather than the fact that a frame left.
        self._offer_answers: dict[str, tuple[threading.Event, dict]] = {}
        # (path, size, mtime) and the digest computed for it: the cached asset is
        # one file, served unchanged to every peer that asks, and re-hashing ten
        # megabytes per request is work with the same answer every time.
        self._cached_digest_key: tuple | None = None
        self._cached_digest_value = ""
        # 自动更新: peer device_id -> (monotonic deadline, the version that was
        # asked for).  One entry per peer this machine has tried to update itself
        # *from*, written before the attempt rather than after it, because the
        # attempt does not end at a moment this runtime can see -- it ends when
        # the blob arrives, which is a different thread.  The version travels
        # with the deadline so a peer that upgrades again is asked again without
        # waiting the window out.
        self._auto_update_tried: dict[str, tuple[float, str]] = {}
        # peer device_id -> the version whose asset actually arrived from it.
        # Without this a host that has no silent installer (and so stays on the
        # old build after a successful fetch) would pull the same ten megabytes
        # off the network every window, forever.
        self._auto_update_served: dict[str, str] = {}
        # One automatic fetch at a time -- the tick runs several times a second
        # and the dial this starts is a real one -- and, per peer, how long a
        # refusal of it counts as an answer to a question nobody asked.
        self._auto_update_busy = False
        self._auto_update_quiet: dict[str, float] = {}
        # peer device_id -> monotonic deadline, the same ledger for logs: this
        # machine asked that peer for its log, and an answer is welcome until
        # the deadline.  Consulted by the log guard above.
        self._log_expectations: dict[str, float] = {}
        # Resolves an entry id a peer asked for into this machine's own paths —
        # set by the application layer, which is what owns the history store.
        # A callback rather than a history reference because the runtime is
        # built before the store is guaranteed to exist, and because the answer
        # ("these paths, or this reason") is a policy the store should own.
        self._clip_file_source = None
        # device_id -> (entry_id asked for, monotonic deadline), the ledger of
        # what this machine has actually requested.  The manager asks rather
        # than tracks because the sender writes the `clip_file` label on its own
        # frames — see `FileTransferManager.set_clip_file_guard`.
        self._clip_file_outstanding: dict[str, tuple[str, float]] = {}
        self.chat = ChatManager(
            config.device_id,
            config.device_name,
            receive_dir=getattr(config, "file_receive_dir", "") or "",
        )
        self.chat.set_own_fingerprint(pairing.get_identity().fingerprint)
        self.chat.set_on_sessions_changed(lambda: self._publish("chat.sessions.changed", {}))
        self.chat.set_on_message(
            lambda sid, entry: self._publish("chat.message", self._chat_entry(sid, entry))
        )
        # Chat file transfers report their own progress/outcome; without these
        # the desktop progress row and the phone's chat file bubble sat at 0%
        # until the transfer finished.
        self.chat.set_on_file_progress(
            lambda sid, tid, fraction: self._publish_progress(
                "chat.file.progress",
                {"session_id": sid, "transfer_id": tid, "fraction": fraction},
                fraction,
            )
        )
        self.chat.set_on_file_done(
            lambda sid, tid, success, saved_path, status: self._publish(
                "chat.file.done",
                {
                    "session_id": sid,
                    "transfer_id": tid,
                    "success": success,
                    "saved_path": saved_path,
                    "status": status,
                },
            )
        )
        # Only ever raised while `chat_open_to_all` is off: both are what turns
        # "somebody wants to talk to you" into a prompt the user can answer.
        self.chat.set_on_incoming_invite(
            lambda invite: self._publish("chat.invite", self._as_named_here(invite))
        )
        self.chat.set_on_invite_response(
            lambda sid, pid, accepted: self._publish(
                "chat.invite.response",
                {"session_id": sid, "peer_id": pid, "accepted": accepted},
            )
        )
        self.chat.set_open_to_all(getattr(config, "chat_open_to_all", True))
        self.transport = (
            transport
            if transport is not None
            else TransportManager(
                config.device_id,
                config.device_name,
                config.port,
                pairing,
                max_reconnect_attempts=config.max_reconnect_attempts,
            )
        )
        self.ai_config = AIConfigManager(
            config,
            lambda peer_id, frame: self.transport.send_to_peer(peer_id, frame),
            connected_fn=lambda: self.transport.get_connected_peers(),
            event_fn=lambda event: self._publish("aiconfig.file", event),
            save_fn=save_config,
        )
        if config.encryption_enabled:
            if encryption is None:
                raise ApplicationError("APP_LOCKED", "Unlock encryption before starting LAN")
            self.transport.set_encryption_manager(encryption)
        self.discovery = (
            discovery
            if discovery is not None
            else _OwnedDiscovery(
                self, config.device_id, config.device_name, config.port, config.service_type
            )
        )
        self._restore_peers()
        self._restore_timed_pause()
        self._refresh(publish=False)

    def _restore_timed_pause(self):
        deadline = float(getattr(self.config, "timed_pause_until", 0.0) or 0.0)
        if deadline <= time.time():
            if deadline:
                self.config.timed_pause_until = 0.0
            return
        self.sync.set_enabled(False)
        def resume_when_due():
            delay = max(0.0, deadline - time.time())
            if self._stop_event.wait(delay):
                return
            if float(getattr(self.config, "timed_pause_until", 0.0) or 0.0) != deadline:
                return
            self.resume_sync()
        self._maintenance = threading.Thread(
            target=resume_when_due, daemon=True, name="clipsync-pause-resume"
        )
        self._maintenance.start()

    def _restore_peers(self):
        # Hash aliases are identified only by the protocol hash, never by a
        # shared hostname or IP address. Never promote trust from an alias.
        with config_lock:
            for real_id in list(self.config.peers):
                alias = peer_id_hash(real_id)
                if alias != real_id and alias in self.config.peers:
                    old = self.config.peers.pop(alias)
                    peer = self.config.peers[real_id]
                    peer.notes = peer.notes or old.notes
                    if not peer.last_ip:
                        peer.last_ip, peer.last_port = old.last_ip, old.last_port
                    self.pairing.remove_peer(alias)
                    self._persist_dirty = True
            known = {peer.device_id for peer in self.pairing.get_known_peers()}
            for peer in self.config.peers.values():
                if peer.device_id not in known:
                    self.pairing.add_peer(
                        peer.device_id, peer.device_name, peer.public_key_pem, peer.paired
                    )

    @property
    def sync_state(self):
        with self._lock:
            if self._state != "running":
                return {"created": "not_started", "stopped": "stopped"}.get(
                    self._state, self._state
                )
            return "running" if self.config.sync_enabled else "paused"

    def devices(self):
        with self._lock:
            snapshot = deepcopy(self._snapshot)
        # Each row says whether this machine sends clipboard content to it, so the
        # list that already renders one control per device can render this one
        # without a second call per row -- and so the switch is visible on the page
        # where a user counts their devices, which is the moment the question
        # ("which of these gets what I copy?") actually occurs to them.
        for row in snapshot.get("items", []):
            row["syncs_to"] = self.syncs_to(row.get("device_id") or row.get("id") or "")
        return snapshot

    def set_device_sync(self, peer_id: str, enabled: bool) -> dict:
        """Include or exclude one device from what this machine copies.

        Writes the *exception* list, so a device that is enabled simply stops
        appearing: the setting stays the size of the user's exclusions rather
        than the size of their device list, and a device paired later is in scope
        without anything having to add it.
        """

        def run():
            paused = {p for p in (getattr(self.config, "sync_paused_peers", None) or ()) if p}
            # Both names for one device, so the switch is not undone by the id
            # resolving: the user may be looking at the hashed row.
            forms = {peer_id, self._resolve(peer_id), peer_id_hash(self._resolve(peer_id))}
            if enabled:
                paused -= forms
            else:
                paused |= forms
            with config_lock:
                self.config.sync_paused_peers = sorted(paused)
            self._save_config()
            self._publish("devices.changed", {})
            return {"ok": True, "peer_id": peer_id, "enabled": enabled}

        return self._command(run)

    def transfers(self):
        def map_item(item):
            outgoing = item.get("type") == "outgoing" or item.get("direction") == "up"
            success = bool(item.get("success")) or item.get("state") in ("completed", "success")
            status = "completed" if success else (
                "cancelled" if item.get("cancelled") or item.get("state") == "cancelled" else (
                    "failed" if item.get("status", "").startswith(("error", "peer_", "rejected"))
                    else item.get("state", "pending")
                )
            )
            return {
                **item,
                "id": item.get("transfer_id", ""),
                # Unnamed rows are up to the shell to label in its own language
                # -- an English default here would show through a Chinese UI.
                "filename": item.get("file_name", ""),
                "size": item.get("file_size", 0),
                "direction": "up" if outgoing else "down",
                "status": "paused" if item.get("paused") else status,
                "progress": round(max(0.0, min(float(item.get("progress", 0) or 0), 1.0)) * 100, 1),
                # Active rows only: the live rate and what is left of the
                # transfer, in the user's language. A history row has neither.
                "speed": item.get("speed_bytes_per_sec", 0),
                "eta": format_eta(item.get("eta_seconds", 0)),
                "path": item.get("saved_path") or item.get("source_path") or "",
                "reason": item.get("status", "") if not success else "",
            }
        raw_speed = self.file_transfer.get_speed_test() or {}
        sent = int(raw_speed.get("chunks_sent", 0) or 0)
        total = int(raw_speed.get("total_chunks", 0) or 0)
        done = raw_speed.get("state") == "done"
        mbps = float(raw_speed.get("result_mbps", 0) or 0)
        speed = {
            **raw_speed,
            "done": done,
            "mbps": round(mbps, 2) if done else None,
            "progress": (sent / total) if total else (1.0 if done else 0.0),
            "status": raw_speed.get("state", ""),
        }
        return {
            "active": [map_item(item) for item in self.file_transfer.get_transfers()],
            "history": [map_item(item) for item in self.file_transfer.get_history()],
            "speed_test": speed,
        }

    def start_speed_test(self):
        return self._command(
            self.file_transfer.start_speed_test,
            self.transport.broadcast,
            lambda: bool(self.transport.get_connected_peers()),
        )

    def send_files(self, paths, device_id=""):
        """Send one or more picked paths to one named peer, or to every peer.

        Legacy's transfers page made the target mandatory -- it refused to
        start an upload until a device was picked (``transfer.select_target``)
        -- and the host sent it through ``send_to_peer``, so a chosen file
        reached exactly one machine. Broadcasting stays the answer when no
        target is named: a file sent without a page to ask on has no peer to
        pick, and the phone upload forward (``forward_file``) already names
        its own.

        A named target that is not connected is refused rather than falling
        back to a broadcast -- sending to everyone when one machine was asked
        for is the one outcome the user could not have meant. Raising here
        reports it; ``send_to_peer`` on a gone peer would silently drop the
        frames and leave a transfer that looks alive.

        A *directory* is archived first and the archive is what goes: the
        legacy panel's 发送文件夹 button zipped the folder into a temp file,
        sent that, and unlinked the archive when the transfer finished.  The
        archive is remembered here so the same unlink can happen -- see
        ``_reclaim_archive`` -- because the receiver must be able to read it
        for as long as the transfer runs.

        *paths* is a list, and more than one of them is the legacy file
        picker's multi-select: several picks are zipped into one archive and
        arrive as one transfer, not as N.  A single pick keeps the old shape --
        a file goes as itself, a folder goes as its own archive -- so the wire
        and the receiving row read the same as they always did.

        The selection also rides along on the transfer (``send_file``'s
        ``origin_paths``) so a retry can rebuild the archive: the one this call
        made is unlinked when the transfer ends, and a row whose only path is
        that archive can never be sent again.  See ``transfer_action``.
        """
        picked = list(paths)
        if not picked or not all(isinstance(path, str) and path for path in picked):
            raise ApplicationError("INVALID_ARGUMENT", "No files to send")
        # Blocking: the archive a folder or multi-file pick is sent as is built
        # in here, which is as long as the selection is big.
        return self._command(lambda: self._send_files(picked, device_id), blocking=True)

    def _send_files(self, picked, device_id):
        """Start the send *picked* describes.  The caller holds the runtime.

        A file's path is passed through exactly as it was handed in: this
        runtime is in no position to rewrite a caller's path, and a round trip
        through Path would do just that on Windows.
        """
        archive = ""
        source = picked[0] if len(picked) == 1 else picked
        if len(picked) > 1 or os.path.isdir(picked[0]):
            try:
                archive_path, count = create_archive(source)
            except ArchiveEmptyError:
                raise ApplicationError(
                    "INVALID_ARGUMENT", "That folder has no files to send"
                ) from None
            except OSError as error:
                # The pick can be a folder or several files, so the message
                # names the selection rather than a folder: a path that is
                # gone between the pick and the send lands here too.
                raise ApplicationError(
                    "INVALID_ARGUMENT", f"Could not archive the selection: {error}"
                ) from error
            archive = str(archive_path)
            logger.info("Archived %s for sending (%d files)", archive_path.name, count)
        subject = archive or picked[0]
        # Only an archived send carries its selection: a plain file's own path
        # is its source, so the row can be sent again from it as it stands, and
        # the call stays exactly what every caller already made.
        extra = {"origin_paths": picked} if archive else {}
        if not device_id:
            transfer_id = self.file_transfer.send_file(
                subject, self.transport.broadcast, **extra
            )
        else:
            pid = self._resolve(device_id)
            # This protocol stays LAN-only: its chunks are 256 KiB before the
            # envelope's base64 and would not fit the relay, and pause/resume
            # has no way to pick a transfer back up off a public broker.  Files
            # cross the internet as chat attachments instead, which chunk to the
            # relay's size and cap the file — a device paired by code therefore
            # arrives here as NOT_CONNECTED, and the transfers page is where it
            # is told so (see `TransfersView.vue::targets`).
            if pid not in (self.transport.get_connected_peers() or []):
                if archive:
                    Path(archive).unlink(missing_ok=True)
                raise ApplicationError("NOT_CONNECTED", "Device is not connected")
            transfer_id = self.file_transfer.send_file(
                subject,
                lambda data: self.transport.send_to_peer(pid, data),
                **extra,
            )
        if archive:
            if transfer_id:
                with self._archive_lock:
                    self._outgoing_archives[transfer_id] = archive
            else:
                # No transfer was started, so no completion event will come
                # to reclaim it: the panel dropped the archive here too.
                Path(archive).unlink(missing_ok=True)
        return transfer_id

    def transfer_action(self, action, transfer_id):
        def run():
            if action in ("open", "reveal"):
                return self._open_transfer_path(action, transfer_id)
            send_fn = (
                self.file_transfer.get_transfer_send_fn(transfer_id) or self.transport.broadcast
            )
            if action == "cancel":
                return self.file_transfer.cancel_transfer(transfer_id, send_fn)
            if action == "pause":
                return self.file_transfer.pause_transfer(transfer_id, send_fn)
            if action == "resume":
                return self.file_transfer.resume_transfer(transfer_id, send_fn)
            if action == "accept":
                return self.file_transfer.accept_transfer(transfer_id, send_fn)
            if action == "reject":
                return self.file_transfer.reject_transfer(transfer_id, send_fn)
            if action == "delete":
                return self.file_transfer.delete_history_by_id(transfer_id)
            if action == "retry":
                entry = next((item for item in self.file_transfer.get_history()
                              if item.get("transfer_id") == transfer_id), None)
                if not entry or entry.get("direction") != "up" or not entry.get("source_path"):
                    raise ApplicationError("INVALID_ARGUMENT", "Transfer cannot be retried")
                peer_id = entry.get("peer_id") or ""
                # A folder or a multi-file pick travelled as a temp archive that
                # was unlinked the moment the transfer ended, so the row's own
                # path is gone and re-sending it could only ever fail with a
                # message about a file the user never chose.  The selection the
                # send was made from is carried on the row instead, and the send
                # is rebuilt from it -- archiving again, so a retry sends what is
                # on disk now, exactly as a retried single file does.
                origin = entry.get("origin_paths") or []
                if origin:
                    return self._send_files(list(origin), peer_id)
                send_fn = (
                    (lambda data: self.transport.send_to_peer(peer_id, data))
                    if peer_id else self.transport.broadcast
                )
                return self.file_transfer.send_file(entry["source_path"], send_fn)
            raise ApplicationError("INVALID_ARGUMENT", "Unknown transfer action")
        # Blocking: a retry re-archives its selection, and that is the slow half
        # of this callback -- the other actions answer immediately.
        return self._command(run, blocking=True)

    def cancel_all_transfers(self):
        """Cancel every active transfer through the same path a single ✕ takes.

        The list is read here, under the runtime lock, rather than being handed
        in by the window: a transfer that arrived since the window last refreshed
        would otherwise be left running behind a "cancel all" the user has
        already confirmed.  Each cancel still goes through
        ``cancel_transfer`` with the row's own send function, so peer
        notification, history and the once-guarded callbacks are identical to
        cancelling that row by hand.
        """
        def run():
            cancelled = 0
            for item in self.file_transfer.get_transfers():
                transfer_id = item.get("transfer_id", "")
                if not transfer_id:
                    continue
                send_fn = (
                    self.file_transfer.get_transfer_send_fn(transfer_id)
                    or self.transport.broadcast
                )
                if self.file_transfer.cancel_transfer(transfer_id, send_fn):
                    cancelled += 1
            return cancelled
        return self._command(run)

    def clear_transfer_history(self):
        """Delete every finished transfer's record, reporting how many went.

        The count is read here rather than handed in by the window for the same
        reason "cancel all" reads its own list: a transfer that finished between
        the window's last refresh and the click is one a count from the window
        would have missed, and the window reports what happened rather than what
        it expected.

        Only records go.  A running transfer is not a record, so this cannot
        disturb one — which is why the legacy panel's clear button sat on the
        history card rather than over both lists.

        The count is what the user could see, because that is the number the
        window says out loud; the background records it is counted past (an
        update asset, a collected log) go with the rest of them, since the
        question the button answers is "clear the history".
        """
        def run():
            cleared = len(self.file_transfer.get_history())
            self.file_transfer.clear_history()
            return cleared
        return self._command(run)

    def _open_transfer_path(self, action, transfer_id):
        """Open or reveal a received file, resolving its path from history.

        The client never supplies the path — it is looked up by transfer id, so
        a compromised WebView cannot ask the host to launch an arbitrary file,
        and only received files are eligible (the legacy panel's rule).
        """
        from internal.system.file_manager import (
            FILE_NOT_FOUND,
            FOLDER_NOT_FOUND,
            open_file,
            reveal_folder,
        )

        entry = next(
            (
                item
                for item in self.file_transfer.get_history()
                if item.get("transfer_id") == transfer_id
            ),
            None,
        )
        if entry is None:
            raise ApplicationError("NOT_FOUND", "Unknown transfer")
        if entry.get("direction") == "up":
            raise ApplicationError("INVALID_ARGUMENT", "Only received files can be opened")
        path = entry.get("saved_path") or ""
        if not path:
            raise ApplicationError("INVALID_ARGUMENT", "Transfer has no saved file")
        ok, detail = (open_file(path) if action == "open" else reveal_folder(path))
        if not ok:
            if detail in (FILE_NOT_FOUND, FOLDER_NOT_FOUND):
                raise ApplicationError("NOT_FOUND", "The file is no longer on disk")
            raise ApplicationError("OPEN_FAILED", "Could not open the file")
        return {"ok": True, "path": path}

    def transfer_lists(self):
        """Raw ``(active, history)`` rows for the phone panel's transfers view.

        The web API remaps these to its own field names, so it wants the
        manager dicts rather than this class's ``transfers()`` projection.
        """
        return self.file_transfer.get_transfers(), self.file_transfer.get_history()

    def speed_test_state(self):
        """Raw speed-test state (the web API does its own field mapping)."""
        return self.file_transfer.get_speed_test() or {}

    def web_transfer_action(self, action, transfer_id):
        """Apply a phone-panel transfer action; True when it took effect.

        The panel speaks the legacy action names, so ``history_delete`` maps to
        the manager's ``delete``.  Like the legacy host callback this reports a
        bool and never raises: the HTTP layer only renders success/failure.
        """
        action = {"history_delete": "delete"}.get(action, action)
        if action not in ("cancel", "pause", "resume", "accept", "reject",
                          "delete", "retry"):
            return False
        try:
            self.transfer_action(action, transfer_id)
        except Exception:
            logger.warning("Web transfer action %s failed", action, exc_info=True)
            return False
        return True

    def forward_file(self, path, device_id):
        """Send a file the phone uploaded to one connected peer.

        Returns False when the target is not connected, so the upload handler
        can report "peer offline" instead of a success the file never reached
        (``send_to_peer`` silently drops frames for a gone peer).
        """
        try:
            if device_id not in (self.transport.get_connected_peers() or []):
                logger.warning("Web forward target %s is not connected", device_id[:12])
                return False
        except Exception:
            logger.debug("get_connected_peers failed during web forward", exc_info=True)
            return False
        try:
            transfer_id = self.file_transfer.send_file(
                path, lambda data: self.transport.send_to_peer(device_id, data)
            )
        except Exception:
            logger.exception("Failed to forward an uploaded file to a peer")
            return False
        logger.info("Web upload forwarded to peer %s", device_id[:12])
        return bool(transfer_id)

    def record_web_upload(self, file_name, file_size, saved_path):
        """Record a phone upload as a completed incoming transfer.

        The legacy host beeped for every upload even when recording it failed,
        so the sound sits outside the recording step.
        """
        self._play_transfer_sound()
        return self.file_transfer.record_web_upload(file_name, file_size, saved_path)

    def _play_transfer_sound(self) -> None:
        """Play the received-file beep, mirroring legacy ``_play_transfer_sound``.

        ``sound_enabled`` is the pure sound toggle and the master
        ``notifications_enabled`` switch silences sound too: both must be on.
        Neither gates whether a notification appears. A missing sound tool must
        never break the transfer flow, so failures are logged and swallowed.
        """
        if not getattr(self.config, "sound_enabled", False):
            return
        if not getattr(self.config, "notifications_enabled", True):
            return
        try:
            notification_mgr.play_sound()
        except Exception:
            logger.debug("play_sound failed", exc_info=True)

    # ── peer-to-peer update exchange (M2) ────────────────────────────────
    def set_update_sink(self, sink) -> None:
        """Where a peer-sent update blob goes (the update service's stage step).

        Called as ``sink(path, sha256="", signature="")``, where *sha256* is the
        digest the sending device declared for the blob and *signature* is the
        release's minisign signature when the sender had one cached.  The digest
        matters because the receiver may have no route to the release server --
        that is what the peer's copy is for -- and the signature is what lets
        the host verify the bytes offline instead of trusting the sender.

        Wrapped rather than assigned so the peer and the transfer travel into the
        service with the blob: without them a refusal cannot be reported back, and
        the device holding the stale file never learns it is being refused.
        """

        def carry_origin(path, sha256="", peer_id="", transfer_id="", signature=""):
            try:
                return sink(
                    path,
                    sha256=sha256,
                    peer_id=peer_id,
                    transfer_id=transfer_id,
                    signature=signature,
                )
            except TypeError:
                # A sink written before the signature (or before the origin) rode
                # along: retry with the fields it did declare, then with the
                # documented two-argument shape.  A sink that accepts the origin
                # but not the signature must still receive the origin, because
                # without it a refusal cannot be reported to the right device.
                try:
                    return sink(path, sha256=sha256, peer_id=peer_id, transfer_id=transfer_id)
                except TypeError:
                    return sink(path, sha256=sha256)

        self._update_sink = carry_origin

    def set_update_verdict_reporter(self, reporter) -> None:
        """Where a decided verdict about a peer-sent blob is published.

        Set by `bootstrap` rather than reached for here: this class does not hold
        the update service, and a lookup of something a caller never gave it would
        work in the application and fail in every test that builds a runtime alone.
        """
        self._update_verdict_reporter = reporter

    # ── files pulled from a peer's history ───────────────────────────────
    def set_clip_file_source(self, source) -> None:
        """Set the ``entry_id -> (paths, reason)`` resolver for file offers.

        The runtime knows the wire and the history store knows the row, so the
        question "may this peer have these files, and where are they" is
        answered by the store and asked by the runtime.  Passing ``None``
        disables serving, and every request is then answered with a refusal.
        """
        self._clip_file_source = source

    def _clip_file_outstanding_for(self, device_id: str, entry_id: str) -> bool:
        """Whether a file from *device_id* really is the one we asked for.

        Read by the transfer manager on every inbound `clip_file` frame, which is
        what makes the label an answer to a request rather than a claim a peer
        can make on its own.

        Matched on the entry as well as the device: one ask yields many files, so
        the entry cannot be consumed on first use, and matching only the device
        would leave a window in which anything that peer chose to push went
        through unprompted.  The deadline is what ends it — a peer that refuses
        answers with a denial instead, and one that simply drops the request
        would otherwise license the next push from it forever.
        """
        if not device_id or not entry_id:
            return False
        now = time.monotonic()
        with self._archive_lock:
            # Pruned here rather than on a timer: this is the only reader, and a
            # window that has run out is indistinguishable from no window.
            for peer, (_entry, deadline) in list(self._clip_file_outstanding.items()):
                if deadline <= now:
                    del self._clip_file_outstanding[peer]
            asked = self._clip_file_outstanding.get(device_id, ("", 0.0))
        return asked[1] > now and asked[0] == entry_id

    def request_entry_files(self, device_id: str, entry_id: str) -> dict:
        """Ask one paired device to send the files behind a history entry.

        This is the only pull in the protocol: every other transfer is started
        by whoever holds the file.  The request carries the entry id and
        nothing else that matters — the names and sizes the user is looking at
        came from the peer's own offer, and the paths are resolved on the
        peer's side, because a path is only meaningful on the machine it names.

        A paired peer that is here on this network *or* reachable over the relay
        can be asked.  This used to be LAN-only, on the reasoning that the frames
        are 256 KiB and a relay cannot carry them -- which was true of the
        frames, and is why the request is now answered with chunks cut to fit a
        broker message instead of refusing the download.  The ask itself is
        small, so it travels either way; the route for the answer is chosen when
        the answer is built (``_file_route_for``).

        A peer that is neither is refused here so the caller learns immediately
        rather than watching a button do nothing.  The request still travels over
        the LAN when there is one, because that path is the faster of the two and
        is the one whose certificate is pinned.
        """
        if not entry_id or not isinstance(entry_id, str):
            raise ApplicationError("INVALID_ARGUMENT", "No entry to download")

        def run():
            pid = self._resolve(device_id)
            if not pid:
                raise ApplicationError("NOT_CONNECTED", "Device is not connected")
            if not (
                self.pairing.is_peer_paired(pid) or self._peer_is_internet_reachable(pid)
            ):
                # Either pairing is enough.  A pinned certificate is the LAN
                # side's answer to "is this device trusted"; an internet
                # pairing's channel key is the same trust reached a different
                # way, and this gate used to require the first alone — which is
                # what made a device paired by code answer its own 下载 with
                # "已与那台设备解除配对" while the receiver below, the route
                # chooser in `_file_route_for` and this method's own docstring
                # all already treated the relay as a route a download may take.
                raise ApplicationError("NOT_PAIRED", "That device is not paired")
            local = pid in (self.transport.get_connected_peers() or [])
            if not local and not self._peer_is_internet_reachable(pid):
                raise ApplicationError("NOT_CONNECTED", "Device is not connected")
            frame = encode_frame(
                {
                    "msg_type": "clip_file_request",
                    "entry": entry_id,
                    "ts": time.time(),
                },
                source_device=self.config.device_id,
            )
            if not self.transport.send_to_peer(pid, frame):
                # No LAN link, so the ask goes over the relay.  The outstanding-request
                # ledger is armed either way: it is what lets the *answer* be accepted
                # without a prompt, and the answer arrives on whichever route works.
                self._relay_publish_to_peer(frame, pid)
            with self._archive_lock:
                self._clip_file_outstanding[pid] = (entry_id, time.monotonic() + CLIP_FILE_WINDOW)
            logger.info("Asked %s for the files behind a history entry", pid[:8])
            return {"requested": True}

        return self._command(run)

    def _file_route_for(self, pid: str) -> tuple:
        """How to send a file's frames to *pid*, and at what chunk size.

        Two questions with one answer each, and they are coupled: the chunk size
        is fixed when the offer is made while the route is chosen per frame, so a
        transfer that *might* cross the relay has to be cut small enough for it
        from the start.  Sizing by the live LAN link alone was the bug chat
        already fixed for itself -- a link that drops halfway through a file
        hands every remaining 256 KiB frame to a broker that refuses them,
        failing a transfer that had already delivered most of itself.

        The LAN case keeps the LAN wire format exactly as it was: the peer's own
        send, and no ``chunk_size`` on the offer, so both that order and older
        builds read it as they always did.

        The relay case borrows the chat closure, which is LAN-first with a relay
        fallback and carries the chunk-ack tag the public broker needs to be
        trusted with a burst.  Sharing that closure rather than writing a second
        one is the point: it is where "a dual-connected peer is sent to exactly
        once" already lives, and a second copy of that rule is a second place for
        it to be wrong.
        """
        if self._peer_is_internet_reachable(pid):
            size = ChatManager.relay_chunk_for(self.config.relay_max_message_bytes)
            return self._chat_send_fn(pid), size
        return (lambda data: self.transport.send_to_peer(pid, data)), 0

    def _serve_clip_file(self, pid: str, payload: dict) -> None:
        """Answer a peer's request for the files behind an entry we published.

        Every step is a refusal the requester is told about, because the
        alternative is a 下载 button that appears to work and produces nothing:
        an entry we cannot resolve, a selection too large for one click, a file
        past the transfer size cap, or an archive that will not build.  The
        refusal carries a reason code rather than a sentence — the requester's
        window is in the requester's language, and this machine does not know
        which that is.
        """
        entry_id = str(payload.get("entry") or "")
        paths, reason = ([], "not_found")
        if entry_id and self._clip_file_source is not None:
            try:
                paths, reason = self._clip_file_source(entry_id)
            except Exception:
                logger.exception("Could not resolve a file offer for a peer")
                paths, reason = [], "not_found"
        if not paths:
            self._deny_clip_file(pid, entry_id, reason or "not_found")
            return
        # Cut to what the offer itself could describe rather than refusing past
        # it.  The two limits are different — the offer's is the size of one JSON
        # frame, this one is how many transfers a single click may start — but
        # they meet at the same number: a peer can only ask for an entry it holds
        # an offer for, and every path here past that offer was never shown to
        # anybody.  A refusal would name no remedy (there is no way to ask for
        # "the next batch"), where a partial delivery is visible in the transfers
        # list; the row's own count already said how many there were.
        if len(paths) > MAX_OFFER_ENTRIES:
            logger.info(
                "Serving the first %d of %d files behind an entry",
                MAX_OFFER_ENTRIES,
                len(paths),
            )
            paths = paths[:MAX_OFFER_ENTRIES]

        # Everything is prepared before anything is sent.  Preparing inside the
        # send loop would mean a selection whose fourth file is too large leaves
        # three transfers already running behind a refusal — the requester would
        # see files arrive *and* an error, which is worse than either alone.
        prepared: list[tuple[str, str]] = []
        for path in paths:
            try:
                subject, archive = self._clip_file_subject(path)
                if os.path.getsize(subject) > MAX_FILE_SIZE:
                    self._discard_archives(prepared)
                    self._deny_clip_file(pid, entry_id, "too_large")
                    return
            except ArchiveEmptyError:
                self._discard_archives(prepared)
                self._deny_clip_file(pid, entry_id, "empty")
                return
            except OSError as error:
                logger.info("Could not prepare a file for a peer: %s", error)
                self._discard_archives(prepared)
                self._deny_clip_file(pid, entry_id, "gone")
                return
            prepared.append((subject, archive))

        # Asked for here rather than at the send: a peer that drops off the LAN mid-click is still
        # the same peer on the relay, and this is the one place that decides which frames go where.
        send_fn, chunk_size = self._file_route_for(pid)
        sent = 0
        orphaned: list[tuple[str, str]] = []
        # walk the same order ``prepared`` was built in, so each entry keeps the
        # path it came from: a directory's own archive is reclaimed when the
        # transfer ends, and that path is what a retry rebuilds from.
        for path, (subject, archive) in zip(paths, prepared, strict=False):
            try:
                # One transfer per file, so what lands is the file the user saw
                # rather than an archive they have to open; a *directory* has no
                # other shape to travel in, so it goes as its own archive,
                # exactly as a folder sent from the transfers page does.
                transfer_id = self.file_transfer.send_file(
                    subject,
                    send_fn,
                    kind="clip_file",
                    entry_id=entry_id,
                    origin_paths=[path] if archive else None,
                    chunk_size=chunk_size,
                )
            except OSError as error:
                logger.info("Could not send a file to a peer: %s", error)
                transfer_id = ""
            if transfer_id:
                if archive:
                    # Reclaimed when the transfer ends, like any folder send:
                    # the receiver has to read it for as long as it runs, so it
                    # must outlive this call.
                    with self._archive_lock:
                        self._outgoing_archives[transfer_id] = archive
                sent += 1
            elif archive:
                # Nothing will ever reclaim this one.
                orphaned.append((subject, archive))
        # An archive nothing reclaimed would sit in the temp dir forever.
        self._discard_archives(orphaned)
        logger.info("Serving %d file(s) behind a history entry to %s", sent, pid[:8])
        if not sent:
            self._deny_clip_file(pid, entry_id, "failed")

    @staticmethod
    def _clip_file_subject(path: str) -> tuple[str, str]:
        """What actually goes on the wire for one pulled path: (subject, archive).

        ``archive`` is "" for a plain file and the temp archive's path for a
        directory, which the caller remembers so it can be unlinked when the
        transfer ends.  A file is sent as itself: archiving a single file would
        hand the user a zip where they expected the document.
        """
        if not os.path.isdir(path):
            return path, ""
        archive_path, _count = create_archive(path)
        return str(archive_path), str(archive_path)

    @staticmethod
    def _discard_archives(prepared: list[tuple[str, str]]) -> None:
        """Unlink the temp archives of a request that will not be served."""
        for _subject, archive in prepared:
            if archive:
                Path(archive).unlink(missing_ok=True)

    def _deny_clip_file(self, pid: str, entry_id: str, reason: str) -> None:
        """Tell a peer its file request will not be served, and why (a code).

        A code rather than a sentence: the requester's window is in the
        requester's language, and this machine does not know which that is.
        Nothing is published here — a refusal is the requester's to see, and
        on this side nothing happened to report.
        """
        try:
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "clip_file_denied",
                        "entry": entry_id,
                        "reason": reason,
                        "ts": time.time(),
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.debug("Could not send a clip file refusal", exc_info=True)

    def _on_clip_file_denied(self, pid: str, payload: dict) -> None:
        """Surface a peer's refusal to this machine's window."""
        self._publish(
            "clip.file.denied",
            {
                "device_id": pid,
                "entry_id": str(payload.get("entry") or ""),
                "reason": str(payload.get("reason") or "failed"),
            },
        )

    def _expect_update(self, pid: str) -> None:
        """Record that this machine has asked *pid* for its cached asset.

        The answer is a ``kind="update"`` transfer, the one kind that skips the
        consent prompt -- so this ledger, not the frame's label, is what makes
        such a blob acceptable.  See ``set_update_guard``."""
        with self._lock:
            self._update_expectations[pid] = time.monotonic() + UPDATE_WINDOW

    def _update_outstanding_for(self, pid: str) -> bool:
        """Whether *pid* was asked for an update and has not answered yet."""
        with self._lock:
            deadline = self._update_expectations.get(pid)
            if deadline is None:
                return False
            if deadline <= time.monotonic():
                # Expired rather than answered: forget it so the table does not
                # grow one dead entry per peer per attempt.
                self._update_expectations.pop(pid, None)
                return False
        return True

    def request_update_from_peers(self) -> None:
        """Ask connected peers for their cached release asset.

        A peer that already downloaded the release answers with it
        (``kind="update"``); the release-server download runs in parallel, so
        nothing ever waits on a peer.
        """
        try:
            peers = list(self.transport.get_connected_peers())
        except Exception:
            peers = []
        # Asked and answered over the same link, so the expectation is per
        # connected peer: an answer from anybody else is not one of ours.
        for pid in peers:
            self._expect_update(pid)
        try:
            self.transport.broadcast(
                encode_frame(
                    {
                        "msg_type": "update_request",
                        # The asker's own build, so the answering side can tell a
                        # peer of its own platform from one the asset is useless
                        # to -- and can serve the latter without pairing.
                        "version": __version__,
                        "os": _local_platform()[0],
                        "arch": _local_platform()[1],
                    },
                    source_device=self.config.device_id,
                )
            )
            logger.info("Broadcast update_request to peers")
        except Exception:
            logger.warning("Failed to broadcast update_request", exc_info=True)

    def offer_device_update(self, device_id: str) -> dict:
        """Tell one peer a newer build is out, and offer our cached asset.

        The half of the 免配对 update path that starts on the *newer* device:
        this machine has the release, the peer is on an older build of the same
        platform, and the person looking at the device list is the one who says
        so.  The peer answers with an ``update_request`` it would otherwise have
        had to pair to make, and the rest of the exchange is the M2 one.

        Raises :class:`ApplicationError` when the peer cannot be reached, so the
        click that produced no traffic cannot look like one that did -- and when
        there is no installer here to send, which is a click this machine cannot
        keep however long it waits.
        """
        # Blocking: offering to an idle device dials it first, and that wait is
        # the runtime's to hold, not the runtime's lock.
        return self._command(self._offer_device_update, device_id, blocking=True)

    def _offer_device_update(self, device_id: str) -> dict:
        pid = self._resolve(device_id)
        if not self._address(pid) and pid not in {
            p.device_id for p in self.pairing.get_known_peers()
        }:
            raise ApplicationError("NOT_FOUND", "Device not found")
        cached = updater.get_cached_asset()
        if not cached:
            # The click means "share this machine's installer", and this machine
            # has none: it has not upgraded through here since this feature
            # existed, so there is nothing to send and nothing to wait for.  The
            # answer is a sentence rather than a download -- fetching the release
            # now would be this machine spending its own bandwidth on a copy of a
            # file the device being offered can fetch itself, which is the same
            # file from the same place, once instead of twice.
            #
            # Asked before the dial, which is the other half of the point: a
            # click that cannot be kept must not reach across the network, least
            # of all to a device that has not agreed to a pairing.
            raise ApplicationError(
                "update.no_asset", "This device has no installer to send yet"
            )
        if pid not in self.transport.get_connected_peers():
            # Not a paired peer, so nothing here will dial it on its own -- and
            # the offer is the one thing this side wants to send.  The dial
            # carries no pairing request (`no_auto_pairing`): an update is not a
            # reason to ask for a pairing code, on either device.
            connected = self._connect_and_wait(pid, no_auto_pairing=True)
            if connected is None:
                raise ApplicationError(
                    "update.peer_unreachable",
                    f"Could not reach {self._peer_name(pid) or 'the device'}",
                )
            pid = connected
        try:
            answer = self._await_offer_answer(
                pid,
                lambda: self.transport.send_to_peer(
                    pid,
                    encode_frame(
                        {
                            "msg_type": "update_offer",
                            "version": __version__,
                            "os": _local_platform()[0],
                            "arch": _local_platform()[1],
                            # Whether an answer would carry bytes.  Always true from
                            # here -- the cache above is the only source of an offer
                            # -- but an older build sent this frame without one, and
                            # the field is what its own receiver reads.
                            "has_asset": True,
                            "asset": os.path.basename(cached),
                        },
                        source_device=self.config.device_id,
                    ),
                ),
            )
        except Exception as exc:
            logger.warning("Failed to offer an update to %s", str(pid)[:12], exc_info=True)
            raise ApplicationError(
                "update.offer_failed", "The update could not be offered to that device"
            ) from exc
        reason = answer.get("reason", "no_answer")
        logger.info(
            "Offered update %s to peer %s: %s", __version__, str(pid)[:12], reason
        )
        # *reason* rather than a bare boolean: "sent" was true of a no-op too,
        # and the click that produced the offer is a person who expects the
        # other device to move.  "accepted" is the peer asking for the asset
        # (the transfer follows on its own); every other value is that peer
        # saying why it will not, and is reported as itself.
        return {"sent": True, "reason": reason}

    def _await_offer_answer(self, pid: str, send) -> dict:
        """Send an update offer and wait, briefly, for the peer's answer.

        The answer is either an ``update_request`` (the peer wants the build,
        and the transfer starts on its own) or an ``update_unavailable`` with a
        reason.  Both arrive on the receive thread, so the wait must not hold
        the runtime lock -- and it is bounded, because a peer from a build with
        no answer frame at all will never send one and the caller's thread is
        the window's.
        """
        answer: dict = {}
        event = threading.Event()
        with self._lock:
            self._offer_answers[pid] = (event, answer)
        try:
            send()
            event.wait(OFFER_ANSWER_TIMEOUT)
        finally:
            with self._lock:
                self._offer_answers.pop(pid, None)
        return answer

    def _resolve_offer_answer(self, pid: str, reason: str) -> None:
        """Record what *pid* did with the offer this machine just sent it."""
        with self._lock:
            entry = self._offer_answers.get(pid)
        if entry is None:
            return
        event, answer = entry
        answer["reason"] = reason
        event.set()

    def _offer_answer_pending_for(self, pid: str) -> bool:
        """Whether an offer of ours to *pid* is still waiting for its answer."""
        with self._lock:
            return pid in self._offer_answers

    def fetch_device_update(self, device_id: str) -> dict:
        """Ask one peer for its cached build; the answer is staged for install.

        The primary direction of the 免配对 update path: the device on the older
        build is the one that has a reason to act, so this is the button its own
        device list carries.  The exchange is the offer's, started from the other
        end -- this side asks, the peer serves, and the bytes are checked against
        the published release digest on arrival before anything is staged.

        Raises :class:`ApplicationError` when the peer cannot be reached, so a
        click that produced no traffic cannot look like one that did.
        """
        # Blocking for the offer's reason: an idle device is dialed first, and
        # that wait is the runtime's to hold, not the runtime's lock.
        return self._command(self._fetch_device_update, device_id, blocking=True)

    def _fetch_device_update(self, device_id: str) -> dict:
        pid = self._resolve(device_id)
        if not self._address(pid) and pid not in {
            p.device_id for p in self.pairing.get_known_peers()
        }:
            raise ApplicationError("NOT_FOUND", "Device not found")
        if pid not in self.transport.get_connected_peers():
            # The dial carries no pairing request, exactly as the offer's does:
            # an update is not a reason to ask for a pairing code on either
            # device, in either direction.
            connected = self._connect_and_wait(pid, no_auto_pairing=True)
            if connected is None:
                raise ApplicationError(
                    "update.peer_unreachable",
                    f"Could not reach {self._peer_name(pid) or 'the device'}",
                )
            pid = connected
        mine_os, mine_arch = _local_platform()
        try:
            delivered = self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "update_request",
                        "version": __version__,
                        "os": mine_os,
                        "arch": mine_arch,
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to ask %s for its update", str(pid)[:12], exc_info=True)
            raise ApplicationError(
                "update.fetch_failed", "The update could not be requested from that device"
            ) from exc
        if not delivered:
            logger.warning("Peer %s was not reachable for the update request", str(pid)[:12])
            raise ApplicationError(
                "update.fetch_failed", "The update could not be requested from that device"
            )
        # The ledger entry is what licenses the blob that comes back: an incoming
        # ``kind="update"`` transfer skips the consent prompt, and this is the
        # record that it was asked for.  Written once the request is away, never
        # before -- an entry armed for a request that never left is a licence for
        # a blob nobody asked for, standing for the whole window.
        self._expect_update(pid)
        logger.info("Asked peer %s for its update", str(pid)[:12])
        return {"sent": True}

    def _on_update_offer(self, pid: str, payload: dict) -> None:
        """A peer says it has a newer build; decide whether we want it.

        Three things have to hold, and all three are read off the *receiving*
        device rather than taken as the sender's word: the peer has to be
        running what it claims, the build has to be newer than this one, and it
        has to be for this platform.  Only then is anything asked for -- and
        what is asked for is one frame, which is what licenses the blob.

        A refusal is answered rather than swallowed.  The offering device's user
        clicked a button and is owed a reason: silence there is the same to them
        as a transfer that is about to start, so the offer page would sit saying
        it had been sent while nothing on this side ever moved.
        """
        version = str(payload.get("version") or "")
        peer_os = str(payload.get("os") or "")
        peer_arch = str(payload.get("arch") or "")
        mine_os, mine_arch = _local_platform()
        # An empty platform is a peer too old to say, not a match.
        same_platform = bool(peer_os) and peer_os == mine_os and peer_arch == mine_arch
        if not same_platform:
            logger.info(
                "Refusing update offer %r from %s: platform %s/%s vs %s/%s",
                version, str(pid)[:12], peer_os, peer_arch, mine_os, mine_arch,
            )
            self._say_no_update_here(pid, "other_platform")
            return
        if not updater.is_newer(version, __version__):
            logger.info(
                "Refusing update offer %r from %s: not newer than %s",
                version, str(pid)[:12], __version__,
            )
            self._say_no_update_here(pid, "not_newer")
            return
        # The offer is only ever sent by a device whose device list was clicked,
        # and the dial behind it is the same one a chat invite makes: it carries
        # no pairing request, so the code this end generated for it has to go or
        # the offer reads as "this device wants to pair" on the way in.
        self._discard_pairing_for_chat(pid)
        name = self._peer_name(pid) or ""
        if not payload.get("has_asset"):
            # Nothing to fetch from the peer itself, so the offer is a notice:
            # this device's own release check is the way to get the build.
            logger.info("Peer %s has no cached asset to serve", str(pid)[:12])
            self._publish(
                "update.peer_notice",
                {"device_id": pid, "name": name, "version": version, "has_asset": False},
            )
            return
        try:
            delivered = self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "update_request",
                        "version": __version__,
                        "os": mine_os,
                        "arch": mine_arch,
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.warning("Failed to ask %s for its update", str(pid)[:12], exc_info=True)
            return
        if not delivered:
            logger.warning("Peer %s was not reachable for the update request", str(pid)[:12])
            return
        # Armed only now the request is away, for the reason ``_fetch_device_update``
        # gives: the entry is what lets the peer's blob in without a prompt.
        self._expect_update(pid)
        self._publish(
            "update.peer_notice",
            {"device_id": pid, "name": name, "version": version, "has_asset": True},
        )

    def _serve_cached_update(self, pid: str, peer_version: str = "") -> None:
        """Answer a peer's update_request: the cached asset, or why not.

        The request can be a background broadcast, which nobody is waiting on,
        or a device-list click, which somebody is.  Both get the same answer --
        silence is indistinguishable from a transfer that is about to start, and
        the click would sit on a spinner forever.

        The cache is the whole of what this machine can send.  It is filled by
        this machine's own upgrade -- the installer it downloaded to install
        itself is kept -- so having none means this build has never been
        upgraded here, and the answer to the peer is that there is nothing to
        hand over.  What this side must *not* do is download the release in order
        to serve it: that is fetching a file from the same place the asking
        device can fetch it from, on a machine that does not need it, and the
        device that is behind is the one with a reason to spend the bandwidth.

        The version the peer reported is what keeps a stray request from pulling
        an installer across the network for nothing: what this machine would send
        is the build it runs, so a peer at or past that version is asking for a
        file it will refuse on arrival.  An empty version is a peer too old to
        say -- not newer, so it is served like any other.
        """
        # A request is also how a peer accepts an offer this machine sent, and
        # it is answered before anything is decided about serving: whether the
        # asset goes out is this side's business, but the asking is the peer's
        # answer either way, and the click that sent the offer is waiting.
        self._resolve_offer_answer(pid, "accepted")
        if peer_version and not updater.is_newer(__version__, peer_version):
            logger.info(
                "Peer %s asked for an update but runs %s", str(pid)[:12], peer_version
            )
            self._say_no_update_here(pid, "not_newer")
            return
        cached = updater.get_cached_asset()
        if not cached:
            logger.info("Peer asked for an update, but none is cached")
            self._say_no_update_here(pid, "no_asset")
            return
        # The cached file's own version, not this build's.  The guard above asks whether *this
        # machine* is ahead, which it can be while the cache still holds an older installer --
        # measured: a 1.0.54 host served its stale 1.0.33 .dmg to a 1.0.33 peer, which discarded
        # it as "not newer than the running version" while both ends logged a successful
        # transfer.  Read with the receiver's own parser, so the two ends agree about a name.
        cached_version = updater.version_in_asset_name(os.path.basename(cached))
        if cached_version and peer_version and not updater.is_newer(cached_version, peer_version):
            logger.info(
                "Not serving cached update %s to %s: it runs %s",
                os.path.basename(cached),
                str(pid)[:12],
                cached_version,
            )
            self._say_no_update_here(pid, "cached_not_newer", os.path.basename(cached))
            return
        # The digest travels with the transfer because the asking device may have
        # no route to the release server at all -- that is the whole reason it is
        # asking a peer.  Without it the receiver has nothing to check the bytes
        # against, and an unchecked installer is one it must refuse.
        digest = self._cached_digest(cached)
        # The release's own signature travels beside the bytes when this machine
        # kept one -- the host caches it while downloading through the plugin.
        # It is what lets a receiver with no route to the manifest verify the
        # bytes offline instead of having to fall back to a manual install.
        # Empty is served as-is (the old sender shape), and the receiver keeps
        # the manual gate for it.
        signature = updater.get_cached_signature(os.path.basename(cached))
        try:
            transfer_id = self.file_transfer.send_file(
                cached,
                lambda data: self.transport.send_to_peer(pid, data),
                kind="update",
                sha256=digest,
                signature=signature,
            )
        except Exception:
            logger.exception("Failed to serve the cached update to a peer")
            self._say_no_update_here(pid, "send_failed")
            return
        logger.info(
            "Serving cached update %s to %s (transfer %s, digest %s)",
            os.path.basename(cached),
            str(pid)[:12],
            str(transfer_id or "")[:8],
            digest[:12] or "none",
        )

    def _cached_digest(self, path: str) -> str:
        """The SHA-256 of *path*, memoized on ``(path, size, mtime)``.

        Hashing a ten-megabyte installer costs a moment, and the cache is the
        same file for every peer asked in a session, so the answer is kept until
        the file it describes changes.  A failure is not fatal: the transfer
        goes out without a digest, and the receiver refuses it for the reason it
        would anyway rather than this side staying silent.
        """
        try:
            stat = os.stat(path)
        except OSError:
            logger.debug("Cannot stat the cached asset %s", path, exc_info=True)
            return ""
        key = (path, stat.st_size, stat.st_mtime)
        with self._lock:
            if self._cached_digest_key == key:
                return self._cached_digest_value
        try:
            digest = updater.sha256_file(path)
        except Exception:
            logger.warning("Cannot hash the cached asset %s", path, exc_info=True)
            return ""
        with self._lock:
            self._cached_digest_key = key
            self._cached_digest_value = digest
        return digest

    def _on_update_verdict(self, pid: str, payload: dict) -> None:
        """What the device we sent an installer to did with it.

        This is the message that was missing.  Measured, a 1.0.54 Mac served its stale 1.0.33
        `.dmg` to a 1.0.33 peer four times in ten minutes; the peer discarded every one as "not
        newer than the running version" and the sender's log said only "Serving cached update".

        Logged at WARNING for a refusal because it means the exchange achieved nothing, and the
        file is named so the reader can go and look at the cache it came from.  Nothing is retried
        here: the version guard in `_serve_cached_update` is what stops the stale file going out
        again, and this line is how anyone finds out it was going out at all.
        """
        verdict = str(payload.get("verdict") or "")
        filename = str(payload.get("file_name") or "?")
        if _verdict_is_rejection(verdict):
            logger.warning(
                "Peer %s refused the update we sent: %s (%s)",
                str(pid)[:12],
                verdict or "unknown",
                filename,
            )
        else:
            logger.info(
                "Peer %s accepted the update we sent: %s (%s)",
                str(pid)[:12],
                verdict or "ok",
                filename,
            )

    def _report_verdict_to_sender(
        self, peer_id: str, transfer_id: str, verdict: str, filename: str
    ) -> None:
        """Tell the sending device what became of the file, and log it here as well.

        The message is what lets the other machine act: it is the one holding the
        stale cache, so a refusal is only actionable there.  The log line is for
        this side's own log, because a reader looking at the receiving machine
        should not have to open the sender's to find out a file was refused.

        A refusal is a WARNING and an acceptance is INFO -- only one of the two
        means the exchange did not do what it set out to do.
        """
        rejected = _verdict_is_rejection(verdict)
        log = logger.warning if rejected else logger.info
        log(
            "Update %s from %s: %s (%s)",
            "refused" if rejected else "accepted",
            str(peer_id)[:12],
            verdict,
            filename or "?",
        )
        try:
            self.transport.send_to_peer(
                peer_id,
                encode_frame(
                    {
                        "msg_type": "update_verdict",
                        "transfer_id": transfer_id,
                        "verdict": verdict,
                        "file_name": filename,
                        "version": __version__,
                        "os": _local_platform()[0],
                        "arch": _local_platform()[1],
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.debug("Could not report an update verdict", exc_info=True)

    def _say_no_update_here(self, pid: str, reason: str = "", asset: str = "") -> None:
        """Tell a peer that asked for an update that this machine has none.

        *reason* is a short code rather than a sentence: the asking device is
        the one that knows what to say to its own user, and it may be running a
        different build with its own wording for each case.

        *asset* names the cached file that was refused, and travels only for
        ``cached_not_newer``.  That case is the one where the reader needs to see
        *what* was held back -- a build they already run -- because the other
        reasons all point at a different action.
        """
        try:
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "update_unavailable",
                        "version": __version__,
                        "os": _local_platform()[0],
                        "arch": _local_platform()[1],
                        "reason": reason,
                        "asset": asset,
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.debug("Could not answer %s about the update", str(pid)[:12], exc_info=True)

    def _on_update_unavailable(self, pid: str, payload: dict) -> None:
        """A peer we asked -- or offered to -- has no installer to send.

        The refusal is the peer's word, and it is acted on as nothing more than
        news: no transfer is expected any more, so the ledger entry goes, and the
        window is told the click is over rather than left waiting on a file that
        is not coming.

        *reason* says which answer this is.  "not_newer" and "other_platform" are
        the peer declining an offer -- it is not behind, or the build is for a
        different machine -- and neither is a failure on this side, so the window
        can say what happened instead of showing it as one.
        """
        reason = str(payload.get("reason") or "")
        with self._lock:
            self._update_expectations.pop(pid, None)
        # A refusal is also the answer to an offer, when this is that exchange
        # rather than an answer to a request; resolving an offer nobody sent is
        # a no-op.  A peer from a build that sends no reason still answered, so
        # an offer waiting on it ends either way.
        self._resolve_offer_answer(pid, reason or "unavailable")
        if self._auto_update_quiet_for(pid):
            # 自动更新 asks on its own, so this refusal is an answer to a
            # question the person did not ask: a device with nothing to hand
            # over is not news about a button they pressed.  The next window
            # tries again; the log is where the asking is recorded.
            logger.info(
                "Peer %s has no update to send: %s", str(pid)[:12], reason or "unavailable"
            )
            return
        self._publish(
            "update.peer_unavailable",
            {
                "device_id": pid,
                "name": self._peer_name(pid) or "",
                "version": str(payload.get("version") or ""),
                # Empty for a peer too old to say why; the window reads that as
                # "no installer to send", which is what it used to be the only
                # case of.
                "reason": reason,
            },
        )

    def _update_dialable(self, seen: dict) -> bool:
        """Whether this machine may call *seen* without asking its user to pair.

        Every dial this build makes on a user's behalf carries the no-pairing
        marker, and only a peer from
        :data:`~internal.transport.connection.NO_PAIRING_MARKER_SINCE` reads
        one: an older build parses the certificate and ignores the tail, so the
        call arrives as a pairing request and puts a pairing card in front of
        someone who asked for nothing.

        This is a version, not a capability the peer advertises -- there is no
        field for it.  The peer's ``app`` used to stand in for one, because only
        builds that have the field know the marker, which is the coincidence the
        update gates rested on while they also required the applications to
        match.  They no longer ask about the application, so the question is
        asked directly.
        """
        version = str(seen.get("version") or "")
        return bool(version) and not updater.is_newer(NO_PAIRING_MARKER_SINCE, version)

    def _update_offerable(self, seen: dict) -> bool:
        """Whether *seen* (one discovery sighting) is a device this build updates.

        Two claims of the peer's, and both have to hold: the same platform,
        because the asset we would send is the one for this one; and a strictly
        older version, because an offer to a device that is already current is a
        transfer nobody wants.

        Which application the peer runs is not one of them.  It used to be, on
        the argument that an installer for one of the two applications published
        from this repository cannot be installed by the other -- which is true,
        and is also a question that machine settles by itself: the blob is
        checked against its own release digest before it can be installed, so a
        wrong offer is refused there rather than mis-installed here.  Requiring
        it cost every peer older than the field that answers it (``app``, 1.0.13
        and later) the ability to be updated from this side at all, and the
        legacy shell it was guarding against is being retired.

        What the question still costs is the dial, so the peer has to be one
        this machine may call -- see :meth:`_update_dialable`.  An empty version
        means a peer too old to advertise: unknown, never "behind".
        """
        version = str(seen.get("version") or "")
        if not version:
            return False
        if not self._update_dialable(seen):
            return False
        if (str(seen.get("os") or ""), str(seen.get("arch") or "")) != _local_platform():
            return False
        return updater.is_newer(__version__, version)

    def _update_fetchable(self, seen: dict) -> bool:
        """Whether *seen* is a device this build can be updated *from*.

        The mirror of :meth:`_update_offerable`, and the direction the update
        path is meant to run in: the device that is behind is the one with a
        reason to act, so its own list is where the button belongs.  Same two
        claims, read the other way -- same platform, and the peer's version
        strictly newer than this one -- and the same reason for leaving the
        application out of them: what the peer would send is an installer for
        whatever shell *it* runs, and the digest check that refuses the wrong
        one at this end is the same check either way.

        Nothing is trusted on the sighting: the blob it sends is checked against
        the published release digest before it can be installed, exactly as a
        GitHub download is.  This only decides whether offering the button is
        truthful at the moment the list is drawn.
        """
        version = str(seen.get("version") or "")
        if not version:
            return False
        if not self._update_dialable(seen):
            return False
        if (str(seen.get("os") or ""), str(seen.get("arch") or "")) != _local_platform():
            return False
        return updater.is_newer(version, __version__)

    # ── 自动更新: the same exchange, started by this device on its own ──────
    def _auto_update_from_peers(self) -> None:
        """Update this machine from a peer that runs a newer build of it.

        The same request :meth:`fetch_device_update` makes on a click, made
        without one: a device on this network, on this platform, running a
        newer build than this one is the whole of what 自动更新 is for, and a
        person who has to notice that in a device list and ask for it is a
        person doing the version check by hand.

        Off with ``auto_update_check``, which is the same switch the release
        lookup reads -- "check for updates automatically" is one decision, and
        it would be a strange reading of it to keep polling GitHub while
        refusing the copy sitting on the LAN.

        One attempt at a time, and one per peer per :data:`AUTO_UPDATE_RETRY`
        window (:meth:`_auto_update_candidate` is what holds that ledger).  The
        tick runs several times a second, and the dial this starts is a real
        one.
        """
        if not getattr(self.config, "auto_update_check", True):
            return
        if self._auto_update_busy:
            return
        candidate = self._auto_update_candidate(self._sightings())
        if candidate is None:
            return
        pid, version = candidate
        with self._lock:
            self._auto_update_tried[pid] = (
                time.monotonic() + AUTO_UPDATE_RETRY,
                version,
            )
            self._auto_update_quiet[pid] = (
                time.monotonic() + AUTO_UPDATE_ANSWER_WINDOW
            )
            self._auto_update_busy = True
        logger.info(
            "Peer %s runs %s, newer than %s here; asking it for the update",
            str(pid)[:12], version, __version__,
        )
        # Its own thread: the fetch dials, and the tick holds the pairing lock.
        threading.Thread(
            target=self._background,
            args=(self._auto_update_fetch, pid),
            name="auto-update-fetch",
            daemon=True,
        ).start()

    def _auto_update_candidate(self, sightings: dict) -> tuple[str, str] | None:
        """The peer to try next, as ``(device_id, version)``, or None.

        *sightings* is every device this machine still trusts, so the candidate
        is one it could also have drawn as a row; :meth:`_update_fetchable`
        is what makes a row offer the action, reused here for the same reason.

        Of several, the newest -- a device two builds behind that reaches the
        one three ahead in a single jump has less to do, and no peer is asked
        anything until the fetch that is running finishes.

        The sighting keys are the ids peers announce, which are not always the
        ids this machine files them under, so each is resolved before it is
        remembered: the ledger has to agree with the id the receive path
        reports, or the record of what a peer has already handed over would
        never match the question asked of it.
        """
        best: tuple[str, str] | None = None
        now = time.monotonic()
        for key, seen in sightings.items():
            pid = self._resolve(key)
            if not pid or pid == self.config.device_id:
                continue
            if not self._update_fetchable(seen):
                continue
            version = str(seen.get("version") or "")
            if self._auto_update_served.get(pid) == version:
                # Already fetched this exact build from that device.  It is here,
                # staged or installed; asking again buys the same bytes.
                continue
            deadline, tried = self._auto_update_tried.get(pid, (0.0, ""))
            if tried == version and now < deadline:
                continue
            if best is None or updater.is_newer(version, best[1]):
                best = (pid, version)
        return best

    def _auto_update_fetch(self, pid: str) -> None:
        """Ask *pid* for its build, quietly.  Runs on the fetch's own thread.

        Quiet in both directions: the automatic attempt is not something the
        person asked for, so a peer that turns out to have nothing to hand over
        is a line in the log rather than a notice about a device they never
        named.  What does reach them is the update card, once there are bytes
        to show.
        """
        try:
            # One retry for a peer that is coming back up.  Measured: a device that was restarting
            # timed out the dial, the attempt was abandoned, and the machine went to a release
            # server it could not reach -- the update arrived two and a half minutes late from the
            # peer that had it all along.
            attempts = 2
            for attempt in range(attempts):
                try:
                    # Through ``_command``, not straight to the callback: the fetch dials and
                    # waits, so it takes the same ``blocking=True`` route the click does -- the
                    # runtime held, the stop event checked, a failure wrapped -- and it is that
                    # path rather than a bare call because a runtime that started stopping between
                    # the tick and this thread should not dial anybody.
                    self._command(self._fetch_device_update, pid, blocking=True)
                    return
                except ApplicationError as exc:
                    # Only the failures that mean "not this second" are retried.  Anything else
                    # (`no_asset`, `not_newer`, `other_platform`) is a real answer, and asking again
                    # would spend another dial to be told the same thing.
                    if attempt + 1 < attempts and exc.code in _UPDATE_RETRY_CODES:
                        logger.info(
                            "Automatic update from %s: %s; trying once more",
                            str(pid)[:12],
                            exc.code,
                        )
                        # Jittered because several devices notice the same new peer at the same
                        # moment, and retrying in lockstep would hammer a machine still coming up.
                        self._stop_event.wait(_UPDATE_RETRY_SECONDS * (0.5 + random.random()))
                        continue
                    logger.info(
                        "Automatic update from %s did not start: %s", str(pid)[:12], exc.code
                    )
                    return
                except Exception:
                    logger.warning(
                        "Automatic update from %s failed", str(pid)[:12], exc_info=True
                    )
                    return
        finally:
            # Released when the request is away, not when the answer comes: the
            # answer is what the quiet window above is for, and the next
            # candidate should not have to wait for it.
            with self._lock:
                self._auto_update_busy = False

    def _auto_update_quiet_for(self, pid: str) -> bool:
        """Whether a refusal from *pid* answers an automatic attempt of ours."""
        with self._lock:
            deadline = self._auto_update_quiet.get(pid)
            if deadline is None:
                return False
            if deadline <= time.monotonic():
                self._auto_update_quiet.pop(pid, None)
                return False
        return True

    def _update_blocked(self, seen: dict) -> str:
        """Why this row offers no update action, as a code the window renders.

        Neither flag above being set has more than one cause, and the row could
        only show all of them the same way: as a button that is not there.  That
        reads identically whether this machine has nothing to send, the peer is
        too old to be called at all, the two are on different platforms, or the
        builds are simply level -- so this says which, and the window says it in
        words.

        A code rather than a sentence, like every other per-device word the
        window draws (``connection_state``, ``pairing_status``): the shell is
        where the two languages live, and a sentence composed here could not be
        translated there.  A code makes the entry un-actionable; the reason is
        what tells the reader the difference between "nothing to do" and "not
        from this machine".

        Form is ``<direction>:<cause>``, the direction being the entry the
        window dims and explains -- ``send`` when the peer is the one behind,
        ``fetch`` when it is the one ahead.  Empty when an action *is* offered
        (the flags say so, and nothing is being withheld), and empty for a peer
        that advertised no version at all: a device that has said nothing about
        itself has no answer here, exactly as it has no version chip.

        The causes are read in the order that decides the question: whether the
        peer can be dialled at all -- a build from before the no-pairing marker
        is one this machine will not call -- then the platform the asset would
        have to fit.  What is left, with both agreeing, is the versions.
        """
        version = str(seen.get("version") or "")
        if not version:
            return ""
        if self._update_offerable(seen) or self._update_fetchable(seen):
            return ""
        behind = updater.is_newer(__version__, version)
        if not self._update_dialable(seen):
            cause = "too_old"
        elif (str(seen.get("os") or ""), str(seen.get("arch") or "")) != _local_platform():
            cause = "other_platform"
        else:
            return "level"
        return f"{'send' if behind else 'fetch'}:{cause}"

    def _same_platform_peer(self, payload: dict) -> bool:
        """Whether a request's sender says it is on this build's own platform.

        Consulted only for a peer this machine has *not* paired with: the asset
        we would answer with is the one for this platform, so a Windows zip sent
        to a Mac is a transfer neither side can use.  The claim is the sender's,
        but nothing is trusted on it -- the bytes still have to match the
        published release digest on arrival before they can be installed."""
        peer_os = str(payload.get("os") or "")
        peer_arch = str(payload.get("arch") or "")
        if not peer_os:
            # A peer too old to say, which is not the same as a match.
            return False
        return (peer_os, peer_arch) == _local_platform()

    # ── peer-to-peer log collection (debug) ─────────────────────────────
    def _expect_log(self, pid: str) -> None:
        """Record that this machine has asked *pid* for its log.

        The answer is a ``kind="log"`` transfer, which — like an update blob —
        skips the consent prompt, so this ledger rather than the frame's label
        is what makes one acceptable.  See ``set_log_guard``."""
        with self._lock:
            self._log_expectations[pid] = time.monotonic() + LOG_WINDOW

    def _log_outstanding_for(self, pid: str) -> bool:
        """Whether *pid* was asked for its log and has not answered yet."""
        with self._lock:
            deadline = self._log_expectations.get(pid)
            if deadline is None:
                return False
            if deadline <= time.monotonic():
                self._log_expectations.pop(pid, None)
                return False
        return True

    def collect_device_log(self, device_id: str) -> dict:
        """Ask one device for its log; the file arrives as a transfer.

        The same shape as :meth:`fetch_device_update`, and for the same reason:
        a debug tool that answers "sent" without reaching anybody is worse than
        one that says it could not, so an unreachable peer is an error here
        rather than a spinner that ends in nothing.
        """
        return self._command(self._collect_device_log, device_id, blocking=True)

    def _collect_device_log(self, device_id: str) -> dict:
        pid = self._resolve(device_id)
        if not self._known_device(pid):
            raise ApplicationError("NOT_FOUND", "Device not found")
        if not self._ask_for_log(pid):
            raise ApplicationError(
                "log.peer_unreachable",
                f"Could not reach {self._peer_name(pid) or 'the device'}",
            )
        return {"sent": True}

    def collect_all_logs(self) -> dict:
        """Ask every device on the network for its log.

        Returns when the requests are away, not when the logs arrive: each
        answer is a transfer of its own that lands when it lands, and holding
        the window's click open for however long the slowest device takes would
        make a working feature look like a hung one.  Every arrival is announced
        on its own (``log.collected``), which is where the user learns what came
        back.
        """
        asked: list[str] = []
        for row in self.devices().get("items", []):
            pid = self._resolve(str(row.get("id") or ""))
            if not pid or pid == self.config.device_id or row.get("archived"):
                continue
            if not self._known_device(pid):
                continue
            asked.append(pid)
        # Dialing is what takes the time, and each peer is dialed on its own
        # thread: one device that has gone quiet must not hold up the rest.
        for pid in asked:
            threading.Thread(
                target=self._background,
                args=(self._ask_for_log, pid),
                name="log-ask",
                daemon=True,
            ).start()
        logger.info("Asked %d device(s) for their logs", len(asked))
        return {
            "requested": len(asked),
            "devices": [
                {"device_id": pid, "name": self._peer_name(pid) or ""} for pid in asked
            ],
        }

    def _known_device(self, pid: str) -> bool:
        """Whether a device id names something this machine can try to reach.

        Either form of the id is accepted, and so is either kind of evidence:
        a sighting on the network (which is what an unpaired device has) or a
        record in the pairing repository (which is what a paired one has, and
        what a peer that is currently away still keeps)."""
        if not pid:
            return False
        if self._address(pid):
            return True
        return pid in {p.device_id for p in self.pairing.get_known_peers()}

    def _ask_for_log(self, pid: str) -> bool:
        """Dial if needed and ask one peer for its log; True when the ask is away.

        ``no_auto_pairing`` exactly as the update path has it: reading a log is
        not a reason to ask anyone for a pairing code, and the request is a
        single frame that the peer answers or refuses.
        """
        if pid not in self.transport.get_connected_peers():
            connected = self._connect_and_wait(pid, no_auto_pairing=True)
            if connected is None:
                return False
            pid = connected
        try:
            delivered = self.transport.send_to_peer(
                pid,
                encode_frame(
                    {"msg_type": "log_request", "version": __version__},
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.warning("Failed to ask %s for its log", str(pid)[:12], exc_info=True)
            return False
        if not delivered:
            logger.warning("Peer %s was not reachable for the log request", str(pid)[:12])
            return False
        # Armed only now the request is away, for ``_fetch_device_update``'s
        # reason: an entry standing for a request that never left is a licence
        # for a blob nobody asked for.
        self._expect_log(pid)
        logger.info("Asked peer %s for its log", str(pid)[:12])
        return True

    def _on_log_request(self, pid: str, payload: dict) -> None:
        """A peer asks for this machine's log; share it if the user said so.

        Both answers are sent, and the refusal is a frame rather than silence:
        the requester's window is showing a request it cannot see the end of,
        and "this device keeps its log to itself" is a different thing to tell
        the user than "that device never answered".
        """
        if not getattr(self.config, "log_sharing", False):
            logger.info("Refusing a log request from %s: sharing is off", str(pid)[:12])
            self._answer_log_request(pid, "log_denied", "disabled")
            return
        # Building the copy reads and rewrites the whole log; it runs on its own
        # thread so the connection's receive loop is not held for it.
        threading.Thread(
            target=self._background,
            args=(self._serve_peer_log, pid),
            name="log-serve",
            daemon=True,
        ).start()

    def _serve_peer_log(self, pid: str) -> None:
        """Answer a log request with a redacted copy of this machine's log."""
        copy = write_share_copy(self.config)
        if copy is None:
            self._answer_log_request(pid, "log_denied", "no_log")
            return
        try:
            self.file_transfer.send_file(
                str(copy),
                lambda data: self.transport.send_to_peer(pid, data),
                kind="log",
            )
        except Exception:
            logger.warning("Failed to serve the log to %s", str(pid)[:12], exc_info=True)
            self._answer_log_request(pid, "log_denied", "failed")

    def _answer_log_request(self, pid: str, msg_type: str, reason: str) -> None:
        try:
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {"msg_type": msg_type, "version": __version__, "reason": reason},
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.debug("Could not answer %s about the log", str(pid)[:12], exc_info=True)

    def _on_log_denied(self, pid: str, payload: dict) -> None:
        """A device this machine asked keeps its log to itself.

        The entry goes: nothing is coming, and leaving it standing would let a
        transfer this side never asked for in for the rest of the window.
        """
        with self._lock:
            self._log_expectations.pop(pid, None)
        self._publish(
            "log.unavailable",
            {
                "device_id": pid,
                "name": self._peer_name(pid) or "",
                "reason": str(payload.get("reason") or ""),
            },
        )

    def _on_file_received(self, transfer_id, saved_path, _file_name):
        kind, sender, sha256, signature = self.file_transfer.take_received_info(transfer_id)
        if kind == "log":
            # A log this machine asked a peer for.  It is filed rather than
            # handed to the received-files flow, and filing it means a move on
            # disk, which is not the receive thread's to do.
            threading.Thread(
                target=self._file_peer_log,
                args=(saved_path, sender),
                name="log-blob",
                daemon=True,
            ).start()
            return
        if kind != "update":
            # Legacy beeped once per received file. The notification itself is
            # the host's job; only the sound is played here.
            self._play_transfer_sound()
            return
        sink = self._update_sink
        if sink is None:
            return
        # The one thing 自动更新 cannot tell from its own ledger: that the attempt
        # it started has ended in bytes rather than in a sentence.  Recorded
        # against the version that was asked for, so this peer is not asked for
        # the same build again -- a host with no silent installer may well stay
        # on it, and that is no reason to pull the file across every window.
        pid = self._resolve(sender) if sender else ""
        if pid:
            with self._lock:
                served = self._auto_update_tried.get(pid, (0.0, ""))[1]
                if served:
                    self._auto_update_served[pid] = served
        logger.info(
            "Received update blob %s from %s (digest %s, signature %s)",
            os.path.basename(saved_path),
            str(sender or "")[:12],
            (sha256 or "")[:12] or "none",
            "present" if signature else "none",
        )
        # Verification looks up the published digest (up to ~30s), so it must
        # not run on the transfer receive thread that called this.
        threading.Thread(
            target=self._deliver_update_blob,
            args=(sink, saved_path, sha256, pid or sender, transfer_id, signature),
            name="update-blob",
            daemon=True,
        ).start()

    def _file_peer_log(self, saved_path: str, sender: str) -> None:
        """File a peer's log under ``~/Downloads/ClipSync-logs`` and say so.

        Named for the device it came from, because several arrive at once and
        the folder is the only thing the user sees afterwards.  A failure here
        is published rather than raised: this runs on its own thread, and the
        window is waiting for news either way.
        """
        pid = self._resolve(sender) if sender else ""
        name = self._peer_name(pid) if pid else ""
        try:
            path = stage_collected_log(saved_path, name or str(pid or "")[:12])
        except Exception as exc:
            logger.warning("Could not file a log from %s: %s", str(pid)[:12], exc)
            self._publish(
                "log.failed",
                {"device_id": pid, "name": name, "reason": "save_failed"},
            )
            return
        logger.info("Filed a log from %s at %s", str(pid)[:12], path)
        self._publish(
            "log.collected",
            {"device_id": pid, "name": name, "path": path},
        )

    @staticmethod
    def _deliver_update_blob(
        sink, saved_path, sha256: str = "", peer_id="", transfer_id="", signature=""
    ):
        try:
            sink(
                saved_path,
                sha256=sha256,
                peer_id=peer_id,
                transfer_id=transfer_id,
                signature=signature,
            )
        except Exception:
            logger.exception("Could not stage a peer-sent update blob")

    def chat_devices(self):
        """The devices this machine could open a conversation with.

        The same list the devices page draws, minus the removals: an archived
        row is a device the user took away, and it was still being offered as a
        chat target — greyed out only because a removed row's connection state is
        offline, which reads as "away" rather than as "gone".  The archive is
        managed on the devices page, not from the chat rail.
        """
        return {"devices": [row for row in self.devices()["items"] if not row.get("archived")]}

    def chat_sessions(self):
        # `open_to_all` rides along so a front end can tell an offer that is
        # waiting on this user from one that was taken on arrival: an entry
        # at `await_accept` exists for an instant in both modes, and only the
        # setting says whether the prompt that follows it is real.
        return {
            "sessions": [self._as_named_here(s) for s in self.chat.get_sessions()],
            "muted": sorted(self._chat_muted),
            "open_to_all": self.chat.open_to_all,
        }

    def _as_named_here(self, session):
        """*session*, with the name **this machine** calls that peer.

        A session's ``peer_name`` is the label the conversation was opened with.
        A conversation the *peer* started carries the name that peer reports
        about itself, so the chat list, the invitation prompt and the notice that
        words an arriving message all named a device the user had renamed by the
        name its owner publishes — the one family of surfaces the rename did not
        reach.  The device list resolved it; the chat did not.

        Resolved once, here, because every chat surface on all three fronts reads
        this field, and each of them resolving it again is the same defect with
        three more chances to disagree.  A peer this machine has no name for
        keeps the label the conversation was opened with.
        """
        pid = str(session.get("peer_id") or "")
        resolved = self._peer_name(pid) if pid else ""
        if not resolved or resolved == session.get("peer_name"):
            return session
        return {**session, "peer_name": resolved}

    def set_chat_muted(self, peer_id, muted):
        def run():
            if muted:
                self._chat_muted.add(peer_id)
            else:
                self._chat_muted.discard(peer_id)
            with config_lock:
                self.config.chat_muted_peers = sorted(self._chat_muted)
            self._save_config()
            # The list travels in the chat sessions payload, and this is the one
            # way it changes — so it has to be announced the way every other
            # change to that payload is, or the surface that did not make the
            # change keeps the old list: the phone is pushed the sessions
            # without it, and a second window reads it only when something else
            # moves a conversation.
            self._publish("chat.sessions.changed", {})
            return {"ok": True, "muted": sorted(self._chat_muted)}
        return self._command(run)

    def _chat_entry(self, session_id, entry):
        """One conversation entry as an event, with the device it belongs to.

        The entry carries the session it went into and nothing about who is on
        the other end of it, and a message is the one chat event a surface has
        to word for a reader who is not looking at it — the desktop's notice
        names the sender — so the peer is added here, from the session.  An
        entry whose session has already gone is published without it; the entry
        is the news either way, and the notice can fall back to the device list
        or to the id.
        """
        peer = self.chat.session_peer(session_id)
        named = self._as_named_here(peer)
        return {
            "session_id": session_id,
            "entry": entry,
            "peer_id": named.get("peer_id", ""),
            # The sender's name for the notice a reader who is not looking at the
            # window gets: the same resolution every chat surface uses, so a
            # renamed device is named by that name there too.
            "peer_name": named.get("peer_name", ""),
        }

    def chat_messages(self, session_id):
        return {"messages": self.chat.get_messages(session_id)}

    def chat_saved_file(self, session_id, transfer_id):
        for entry in self.chat.get_messages(session_id):
            if entry.get("transfer_id") == transfer_id and entry.get("saved_path"):
                path = entry["saved_path"]
                if os.path.isfile(path):
                    return {"path": path}
        raise ApplicationError("NOT_FOUND", "Received file is not available")

    def chat_reveal_file(self, session_id, transfer_id):
        """Show a received chat file in the OS file manager.

        The legacy chat panel put 打开所在文件夹 beside 打开 on a saved
        attachment (`chat-panel.js`, `revealFile(m.saved_path)`), and the new
        page had only the first of the two.  The lookup is `chat_saved_file`'s,
        so a file can only be revealed from a message that actually carries a
        saved path — the caller names a session and a transfer, never a path.

        Revealing goes through the same `reveal_folder` the transfer list uses
        rather than a second implementation: one place that knows how each
        platform's file manager is asked, and the same answer to "打开所在
        文件夹" wherever it is clicked.  A file whose folder has since been
        moved or deleted comes back as its own error instead of a silent
        success.
        """
        from internal.system.file_manager import reveal_folder

        path = self.chat_saved_file(session_id, transfer_id)["path"]
        ok, detail = reveal_folder(path)
        if not ok:
            raise ApplicationError("REVEAL_FAILED", "The file's folder could not be opened")
        return {"ok": True, "folder": detail}

    def _chat_send_fn(self, peer_id):
        """A send closure for one chat peer: LAN first, relay only as fallback.

        Chat has no content dedup (unlike clipboard history), so the relay is
        used only when the LAN send did not deliver — an unconditional mirror
        would append every message twice to a dual-connected peer.  The closure
        returns whether EITHER path delivered the frame, which is what chat's
        frame sender tests, so an internet-only peer (LAN send fails, relay
        succeeds) still counts as delivered.  Broadcast (no peer) stays
        LAN-only: internet peers are always paired.
        """
        if not peer_id:
            return self.transport.broadcast

        def send(data):
            if self.transport.send_to_peer(peer_id, data):
                self._note_chat_sent(peer_id, self._decode_frame(data))
                return True
            return self._relay_publish_to_peer(data, peer_id)

        if self._peer_is_internet_reachable(peer_id):
            # A peer that *can* be reached over the relay is chunked relay-safe
            # even while its LAN link is up, because the chunk size is fixed
            # when the offer is made and the route is chosen per frame.  Sizing
            # by the LAN alone meant a link that dropped halfway through a file
            # handed every remaining 256 KiB frame to a relay that refuses
            # anything past MAX_RELAY_FRAME (``pack_envelope``), failing a
            # transfer that had already delivered most of itself.
            #
            # Only the *message* has a relay limit; the file's total size is
            # the app's own MAX_FILE_SIZE and nothing narrower, and the
            # configured relay limit picks the chunk, since a broker that
            # carries less than this app assumes needs smaller chunks rather
            # than dropped ones.
            #
            # This does not narrow what a peer may be: the condition is
            # ``_peer_is_internet_reachable``, which needs a netpair or an
            # enrolled secret, and a build that predates the relay has neither
            # -- so the peers the LAN wire format exists to keep interoperating
            # keep it, and the ones that lose it are exactly the ones already
            # parsing ``chunk_size`` out of the offer.
            send.chunk_size = ChatManager.relay_chunk_for(self.config.relay_max_message_bytes)
            # The same closure says the relay is not to be trusted with a burst
            # and asks the receiver to acknowledge every chunk it writes, which
            # is what turns "the broker took it" into "the peer has it" (see
            # ChatManager's CHUNK_ACK_* block).  A LAN peer carries neither tag,
            # so its transfers keep the blunt loop and its wire format.
            send.chunk_ack = True
            # The pace the transfer may hold, in bytes/s: a policy setting, not
            # a property the broker publishes, and the measured difference
            # between a burst it drops and a rate it carries.
            send.relay_rate = self.config.relay_max_bytes_per_second
        return send

    def _note_chat_sent(self, peer_id, msg):
        """Ledger a chat frame that reached an internet-reachable peer.

        The peer acks it over the relay like any other accepted frame, and a
        ledger row is what that receipt resolves against — without one the ack
        is dropped as an unknown msg_id and the message would sit "sent" in the
        send list forever.  No offline queue for chat (clipboard only).
        """
        if msg is None or not self._peer_is_internet_reachable(peer_id):
            return
        kind = getattr(msg, "msg_type", "")
        if kind not in CHAT_MSG_TYPES:
            return
        payload = getattr(msg, "_raw_payload", {}) or {}
        session_id = payload.get("session_id", "")
        self.delivery.note_sent(
            peer_id,
            getattr(msg, "msg_id", "") or "",
            "",
            self._delivery_chat_preview(kind, payload),
            kind=kind,
            session_id=session_id if isinstance(session_id, str) else "",
        )

    @staticmethod
    def _delivery_chat_preview(msg_type, payload):
        """Short preview of a chat frame for the send list (legacy shape)."""
        if msg_type == "chat_text":
            text = str(payload.get("text", "") or "").strip()
        elif msg_type == "chat_file_offer":
            text = str(payload.get("file_name", "") or "").strip()
        else:
            text = ""
        return text[:40] if text else (msg_type or "chat")

    def chat_invite(self, peer_id, peer_name):
        """Open a conversation, dialing first when there is no link yet.

        **Never waits for that dial.**  The wait is up to
        ``CHAT_CONNECT_TIMEOUT``, and this call runs on the sidecar's one request
        thread: ``RpcServer._serve_requests`` reads, dispatches and answers one
        frame at a time — its reader thread is held back by ``_read_next``, so it
        never reads ahead — and it only flushes the event journal when it finds
        the input queue empty.  A dial waited out inside this call therefore
        delayed every later request *and* every ``devices.changed`` /
        ``chat.sessions.changed`` behind it, for as long as the peer took to
        answer or the timeout to lapse.  That is the whole of "附近聊天按钮有时卡住",
        including the device list looking frozen around it.

        What the caller gets instead is the answer `rpc.py` and the web API
        already document ("No session yet means the link is still being dialed,
        not that the invite was refused"): a session id when the link is already
        up, and nothing while the dial runs.  The session opens when the dial
        lands and announces itself through the chat manager's own hook
        (``set_on_sessions_changed``), which is how the window, the web panel and
        the phone learn that it arrived.
        """
        return self._command(self._chat_invite, peer_id, peer_name)

    def _chat_invite(self, peer_id, peer_name):
        """The invite's own half: dial if one is owed, otherwise open it now.

        Legacy dialed and waited up to 15s for the connection before starting
        the session, and told the user when it never came up; without the wait
        an invite to a discovered-but-idle device would be sent into a link that
        does not exist yet and silently go nowhere.  The dial is still made — it
        is moved off this thread, not dropped; see :meth:`_invite_after_dial`.

        An internet-only peer has no LAN address to dial, but its send closure
        falls back to the relay, so a pairing code alone is enough to open the
        conversation.
        """
        pid = self._resolve(peer_id)
        if pid in self.transport.get_connected_peers() or self._address(pid) is None:
            # Nothing to wait for: either the link is already up, or there is no
            # address to dial and the relay carries the frames.
            return self._open_chat(pid, peer_name)
        # Dialing is what takes the time, so it happens on a thread of its own —
        # the same way every collected-log request is dialed (see
        # `request_logs`) — and the answer is "connecting" at once.
        threading.Thread(
            target=self._background,
            args=(self._invite_after_dial, pid, peer_name),
            name="chat-invite-dial",
            daemon=True,
        ).start()
        return None

    def _open_chat(self, pid, peer_name):
        """Start the conversation and hand back its id, or None if suppressed."""
        return self.chat.start_session(
            pid, peer_name,
            self.chat.shorten_fingerprint(self.pairing.get_peer_fingerprint(pid)),
            self._chat_send_fn(pid),
        )

    def _invite_after_dial(self, pid, peer_name):
        """Dial *pid*, then open the conversation the invite asked for.

        Runs on its own thread (``chat-invite-dial``), so the wait — the dialer's,
        up to ``CHAT_CONNECT_TIMEOUT`` — is nobody else's.  ``_connect_and_wait``
        checks the stop event on every pass, so a shutdown is not held up by a
        peer that never answers.

        The dial suppresses the automatic pairing offer, as legacy's two
        chat-start paths both did: opening a conversation is not a request to
        pair, and the shared code the ordinary dial puts on both screens is
        consent neither side gave.
        """
        # The dial may hand back a different id than the one asked for — the hash
        # this device is discovered by becomes the real device_id once the
        # handshake lands — and the session has to be keyed by the one the
        # transport delivers to, or every message is sent to an address nothing
        # is connected at.
        connected = self._connect_and_wait(pid, no_auto_pairing=True)
        if connected is None:
            # A dial that never lands is the end of the conversation only for a
            # peer the relay cannot carry either.  A dual-paired peer answers on
            # the relay whatever its LAN link is doing, and the send closure
            # falls back there per frame — so giving up here reported a device as
            # unreachable while the very next thing the user clicked would have
            # worked.
            #
            # The address that just failed is often not the peer's anyway:
            # ``_address`` falls back to a cached, then a persisted, address when
            # no live sighting exists, and a dial that fails does not clear
            # either — so a device that changed network keeps being dialed where
            # it used to be.  Clearing them is not the answer: the transport's
            # saved address is the same table ``_schedule_reconnect`` dials from,
            # and a peer that comes back without answering mDNS has nothing else
            # (see ``connection.py``'s reconnect ladder).
            if not self._peer_is_internet_reachable(pid):
                self._publish(
                    "chat.connect_timeout",
                    {"peer_id": pid, "name": self._peer_name(pid) or peer_name},
                )
                return
            real = pid
        else:
            real = connected
        self._open_chat(real, peer_name)

    def _connect_and_wait(self, pid, timeout=None, no_auto_pairing=False):
        """Dial one peer and wait for the link; the connected id, or None.

        The id returned is not always the one passed in, and callers must key
        their work by it.  A device this machine has never handshaked is known
        only by its discovery hash, and ``_resolve`` cannot map that hash back
        to a device_id until a handshake names it — while the transport keys
        ``_peers`` by the real device_id throughout.  Polling for the hash
        therefore waited out the whole timeout on a link that was up the entire
        time and then reported a failure that never happened, which is how a
        chat invite to a freshly discovered device went nowhere: no invite was
        ever sent, so the peer's only way to learn the conversation was not a
        pairing request never ran either.
        """
        if pid in self.transport.get_connected_peers():
            return pid
        if not self._connect(pid, no_auto_pairing=no_auto_pairing):
            return None
        deadline = time.monotonic() + (
            self.CHAT_CONNECT_TIMEOUT if timeout is None else timeout
        )
        while True:
            # Re-resolved every pass: the handshake that completes mid-wait is
            # what makes the mapping exist, so this is the moment it appears.
            real = self._resolve(pid)
            if real in self.transport.get_connected_peers():
                return real
            if time.monotonic() >= deadline or self._stop_event.is_set():
                return None
            time.sleep(0.1)

    def chat_action(self, action, session_id, text=""):
        def run():
            session = next(
                (s for s in self.chat.get_sessions() if s["session_id"] == session_id), None
            )
            if not session:
                return False
            send_fn = self._chat_send_fn(session["peer_id"])
            if action == "send":
                return self.chat.send_text(session_id, text, send_fn)
            if action == "accept":
                return self.chat.accept_invitation(session_id, send_fn)
            if action == "decline":
                return self.chat.decline_invitation(session_id, send_fn, text)
            if action == "read":
                self.chat.mark_session_read(session_id)
                return True
            if action == "close":
                return self.chat.close_session(session_id)
            if action == "resend":
                return self.chat.resend_text(session_id, text, send_fn)
            raise ApplicationError("INVALID_ARGUMENT", "Unknown chat action")
        return self._command(run)

    def chat_typing(self, session_id, typing):
        def run():
            session = next(
                (s for s in self.chat.get_sessions() if s["session_id"] == session_id), None
            )
            if not session:
                return False
            return self.chat.report_typing(
                session_id, bool(typing), self._chat_send_fn(session["peer_id"])
            )
        return self._command(run)

    def chat_file(self, action, session_id, transfer_id="", path=""):
        def run():
            session = next(
                (s for s in self.chat.get_sessions() if s["session_id"] == session_id), None
            )
            if not session:
                return False
            send_fn = self._chat_send_fn(session["peer_id"])
            if action == "send":
                return self.chat.send_file(session_id, path, send_fn)
            if action == "accept":
                return self.chat.accept_file(session_id, transfer_id, send_fn)
            if action == "decline":
                return self.chat.decline_file(session_id, transfer_id, send_fn)
            if action == "cancel":
                return self.chat.cancel_file(session_id, transfer_id)
            raise ApplicationError("INVALID_ARGUMENT", "Unknown chat file action")
        return self._command(run)

    def _enter(self):
        with self._lock:
            if self._state not in ("starting", "running"):
                return False
            thread = threading.current_thread()
            self._active[thread] = self._active.get(thread, 0) + 1
            return True

    def _leave(self):
        with self._idle:
            thread = threading.current_thread()
            self._active[thread] -= 1
            if not self._active[thread]:
                del self._active[thread]
            self._idle.notify_all()

    def _background(self, callback, *args):
        if not self._enter():
            return None
        try:
            return callback(*args)
        except Exception:
            self._error("LAN_CALLBACK_FAILED")
            return None
        finally:
            self._leave()

    def _command(self, callback, *args, blocking=False):
        """Run one runtime operation: accounted for, serialized, wrapped.

        ``_pairing_ops`` is held across the whole callback, which is what every
        operation that reads or moves trust state needs -- and what an operation
        that *blocks for a long time* must not have.  The receive path and the
        refresh tick take the same lock, so a callback that dials a peer and
        waits for the link (``_connect_and_wait``, up to
        ``CHAT_CONNECT_TIMEOUT``) froze every other command the window was
        asking for, and every frame the peer sent while it waited, for as long
        as it waited.  A callback that blocks on the disk -- the zip a folder
        send is built from -- holds it just as long, for no better reason.

        Those few pass ``blocking=True``.  They are marked, not unguarded: the
        runtime is still held, the stop event is still checked and a failure is
        still wrapped exactly as below.  ``test_device`` makes the same choice
        by hand, from before this had a name.
        """
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            if blocking:
                return self._checked_call(callback, *args)
            with self._pairing_ops:
                return self._checked_call(callback, *args)
        except ApplicationError:
            raise
        except Exception:
            # Logged here because nothing downstream will: the RPC layer answers
            # an ApplicationError with its message and records nothing, and that
            # message is deliberately one sentence for every cause.  The traceback
            # is the only place the failing operation and its reason exist, so it
            # goes to the log before ``from None`` drops it from the raise.
            logger.error("LAN command failed", exc_info=True)
            raise ApplicationError(
                "LAN_OPERATION_FAILED", "LAN operation failed", retryable=True
            ) from None
        finally:
            self._leave()

    def _checked_call(self, callback, *args):
        """Refuse a stopped runtime, then run the callback."""
        if self._stop_event.is_set():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
        return callback(*args)

    def _on_transfer_complete(self, transfer_id, success, cancelled, status):
        """Report a finished transfer, then reclaim its archive if it had one.

        The archive of a folder send is a temp file this runtime made, and the
        legacy panel unlinked its own when the transfer finished so folder sends
        did not leak zip copies.  Nothing has to be unlinked when the transfer
        is *still* running: the receiver reads the archive for as long as the
        transfer lasts, which is why this waits for the terminal event rather
        than deleting sooner.
        """
        self._publish(
            "transfer.complete",
            {
                "transfer_id": transfer_id,
                "success": success,
                "cancelled": cancelled,
                "status": status,
            },
        )
        self._reclaim_archive(transfer_id)

    def _reclaim_archive(self, transfer_id):
        """Drop the temp archive a folder send was made from, if there was one."""
        with self._archive_lock:
            archive = self._outgoing_archives.pop(transfer_id, "")
        if not archive:
            return
        try:
            os.unlink(archive)
        except OSError:
            # A temp file left behind is a tidier outcome than a completion
            # event that never went out: report the transfer, not the litter.
            logger.warning("Could not reclaim the archive for %s", transfer_id[:8])

    def _publish(self, name, data):
        with self._lock:
            admitted = self._state in ("starting", "running")
        if admitted:
            # Never publish with _lock held: snapshot takes journal -> _lock.
            try:
                self.events.publish(name, data)
            except Exception:
                logger.warning("LAN runtime: EVENT_PUBLISH_FAILED")

    def _publish_progress(self, name, data, fraction):
        """Publish one progress event, unless one has just gone out.

        Progress arrives once per 256 KB chunk, so a 250 MB file is a thousand
        events — and every one of them is answered by the desktop with a
        whole-snapshot refresh (status, devices and history) and by the phone
        with a WebSocket frame.  Those refreshes are what fill the bars, so the
        events cannot be dropped; they can be spaced, because neither surface
        shows more than the eye can read.  One gate for the process is enough
        to space them: the desktop's refresh is not per-transfer, so whichever
        event arrives refreshes every bar on screen.

        The last event of a transfer is never held back — 1.0 is what fills the
        bar, and it lands inside the window whenever the last chunk flushes
        quickly after the one before it.
        """
        if fraction < 1.0 and not self._progress_ready():
            return
        self._publish(name, data)

    def _progress_ready(self):
        """Whether PROGRESS_INTERVAL has passed since the last progress event.

        The timestamp is read and written without the lock on purpose: it is a
        float under a gate that decides nothing but how often a bar is redrawn,
        so two threads reaching it at once cost at most one extra event, and
        taking _lock on every chunk of every transfer is the cost this exists to
        avoid.
        """
        now = time.monotonic()
        if now - self._progress_at < self.PROGRESS_INTERVAL:
            return False
        self._progress_at = now
        return True

    def _error(self, code, detail: str = ""):
        """Report one runtime failure: a code the front ends localize, and the
        facts of *this* one beside it.

        *detail* is what a code cannot carry on its own -- which limit was hit,
        which peer did not get the frame -- so the log line and the event say
        something a reader can act on.  It is part of the change test as well as
        the code: two oversized clips to two different peers are two reports,
        because the second one's peer is the whole point of it.
        """
        logger.warning("LAN runtime: %s%s", code, f" ({detail})" if detail else "")
        with self._lock:
            changed = self._last_error != code or self._last_error_detail != detail
            self._last_error = code
            self._last_error_detail = detail
        if changed:
            payload = {"code": code, "message": "LAN operation failed"}
            if detail:
                payload["detail"] = detail
            self._publish("runtime.error", payload)

    def start(self):
        with self._lock:
            if self._state == "running":
                return
            if self._state != "created":
                raise ApplicationError("LAN_NOT_READY", "LAN runtime cannot be restarted")
            self._state = "starting"
        try:
            # Seed the local inventory before peers connect so the first
            # inventory request never receives an empty pre-start snapshot.
            self.ai_config.collect()
            self.transport.set_on_peer_message(
                lambda msg, peer_id=None: self._background(self._receive, msg, peer_id)
            )
            self.transport.set_on_security_alert(
                lambda *args: self._background(self._security_alert, *args)
            )
            self.transport.set_on_connect_rejected(
                lambda name, pid: self._background(self._connect_rejected, name, pid)
            )
            self.transport.set_on_wake(lambda: self._background(self.discovery._wake_recovery))
            self.pairing.set_on_new_pairing(
                lambda *args: self._background(self._new_pairing, *args)
            )
            self.discovery.set_callbacks(
                lambda *args: self._background(self._peer_found, *args),
                lambda pid: self._background(self._peer_lost, pid),
            )
            for name, start in (
                ("transport", self.transport.start_server),
                ("discovery", self.discovery.start),
                ("sync", self.sync.start),
            ):
                if self._stop_event.is_set():
                    raise RuntimeError("Start interrupted")
                self._started.add(name)
                start()
                if name == "transport" and self.config.port == 0:
                    # Ephemeral ports are useful for isolated loopback runtimes.
                    self.discovery._port = self.transport._server_sock.getsockname()[1]
            if self.config.internet_sync_enabled:
                self._open_relay()
            if isinstance(self.discovery, Discovery) and not self.discovery.is_browsing:
                raise RuntimeError("Discovery did not start")
            with self._lock:
                if self._state != "starting":
                    raise RuntimeError("Start interrupted")
                self._state = "running"
                self.sync.set_enabled(self.config.sync_enabled)
                self._maintenance = threading.Thread(
                    target=self._maintenance_loop, daemon=True, name="clipsync-lan-state"
                )
                self._maintenance.start()
            for peer in self.pairing.get_paired_peers():
                self._connect(peer.device_id)
            self._refresh()
        except Exception:
            self._start_done.set()
            self.stop()
            raise ApplicationError(
                "LAN_START_FAILED", "Could not start LAN services", retryable=True
            ) from None
        finally:
            self._start_done.set()
        self._publish("sync.state.changed", {"sync_state": self.sync_state})

    def stop(self) -> bool:
        with self._lock:
            if self._state == "stopped":
                return True
            if self._state == "created":
                self._start_done.set()
            self._state = "stopping"
            self._stop_event.set()
            self._dirty.set()
            inside = threading.current_thread() in self._active
            if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
                self._cleanup_thread = threading.Thread(
                    target=self._cleanup, daemon=True, name="clipsync-lan-stop"
                )
                self._cleanup_thread.start()
            worker = self._cleanup_thread
        if inside:
            return False
        worker.join(self.STOP_TIMEOUT)
        with self._lock:
            if worker.is_alive() or not self._cleanup_ok or self._active:
                return False
            self._state = "stopped"
            return True

    def _open_relay(self):
        """Build and start the relay from the live config; returns it.

        One definition, because the internet-sync switch can now be turned on
        while running as well as at startup, and both have to build the same
        client — brokers, credentials and the runtime's own receive callback
        included.
        """
        relay = RelayTransport(
            list(self.config.relay_brokers),
            self.internet_pairing.channels,
            lambda frame, *args: self._background(self._receive_relay, frame, *args),
            self._relay_state_changed,
            client_factory=build_paho_client,
            username=getattr(self.config, "relay_username", ""),
            password=getattr(self.config, "relay_password", ""),
            private_brokers=list(getattr(self.config, "relay_private_brokers", []) or []),
            max_payload=int(
                getattr(self.config, "relay_max_message_bytes", MAX_RELAY_PAYLOAD)
                or MAX_RELAY_PAYLOAD
            ),
        )
        self.relay = relay
        self.internet_pairing.attach_relay(relay)
        relay.start()
        return relay

    def _close_relay(self):
        """Detach and stop the relay; False only when stopping it failed."""
        relay, self.relay = self.relay, None
        self.internet_pairing.attach_relay(None)
        if relay is None:
            return True
        try:
            relay.stop()
            return True
        except Exception:
            logger.debug("Relay stop failed", exc_info=True)
            return False

    def _apply_internet_sync_enabled(self, enabled):
        """Bring the relay up or down for a switch flipped while running.

        The relay used to be built in the startup path and nowhere else, so this
        setting was one only a restart honoured: the page went on showing
        whatever state the process started in.  Turning it *off* is the half
        that matters — the relay carries the same clipboard frames the LAN
        carries, over a public broker, and "off" has to mean they stop going
        there rather than that they stop after the next launch.
        """
        if enabled:
            if self.relay is None and not self._stop_event.is_set():
                self._open_relay()
        elif self.relay is not None:
            self._close_relay()

    @staticmethod
    def _stop_stage(stop, budget: float):
        """Call one teardown stage, handing it what is left of the deadline.

        A stage that takes a timeout gets the remainder rather than a budget of
        its own; one that does not is bounded by the sockets it closes.  Decided
        by signature instead of by `except TypeError`, because swallowing a
        TypeError from inside a stage would hide the stage's own bug -- and it is
        the only reason the fakes in the tests can keep a no-argument
        `stop_server()`.
        """
        try:
            parameters = inspect.signature(stop).parameters
        except (TypeError, ValueError):
            return stop()
        if not parameters:
            return stop()
        return stop(budget)

    def _cleanup(self):
        # One deadline for the whole teardown, not one per stage.
        #
        # The outer budget is `STOP_TIMEOUT` -- the caller joins this thread for
        # exactly that long and reports "did not release ownership within its
        # budget" if it is still running.  Each stage below has a natural
        # 5-second budget of its own (a socket accept loop, a drain of admitted
        # callbacks), and giving each of them the full five seconds is how the
        # outer one came to be exceeded with no indication of which stage did it.
        # Measured on this machine, the transport drain was the stage that lost:
        # `runtime.stop()` returned False at 5.01 s with `clipsync-lan-stop`
        # still inside `stop_server`.
        #
        # So every wait from here on is against this deadline, and a stage that
        # cannot finish says what it was waiting for.
        deadline = time.monotonic() + self.STOP_TIMEOUT

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        self._start_done.wait()
        ok = True
        # Disable first, so a capture already reading cannot record or send.
        self.sync.set_enabled(False)
        try:
            self.chat.shutdown()
        except Exception:
            ok = False
        if not self._close_relay():
            ok = False
        for name, stop in (
            ("sync", self.sync.stop),
            ("discovery", self.discovery.stop),
            ("transport", self.transport.stop_server),
        ):
            if name not in self._started:
                continue
            if remaining() <= 0:
                logger.warning("LAN runtime teardown gave up before %s", name)
                ok = False
                break
            started = time.monotonic()
            try:
                complete = self._stop_stage(stop, remaining())
                if complete is False:
                    ok = False
                else:
                    self._started.discard(name)
            except Exception:
                ok = False
                logger.warning("LAN runtime: RESOURCE_STOP_FAILED (%s)", name)
            elapsed = time.monotonic() - started
            if elapsed > 1.0:
                logger.warning(
                    "LAN runtime teardown: %s took %.1fs (%s)",
                    name, elapsed, "ok" if ok else "incomplete",
                )
        if self._maintenance is not None and self._maintenance.ident is not None:
            # Bounded, and the one wait here that used to be unbounded: the loop
            # parks on a refresh interval, so it should exit promptly -- and if it
            # does not, the join was what held the outer budget open with nothing
            # to show for it.
            self._maintenance.join(remaining())
            if self._maintenance.is_alive():
                logger.warning(
                    "LAN runtime teardown: the state loop did not stop within the budget"
                )
                ok = False
        with self._idle:
            # A straggling background task holds ownership: say so rather than
            # let the caller report a bare timeout.
            if self._active and remaining() > 0:
                self._idle.wait(remaining())
            if self._active:
                logger.warning(
                    "LAN runtime teardown: %d background task(s) still running: %s",
                    len(self._active),
                    sorted(thread.name for thread in self._active),
                )
                ok = False
            self._cleanup_ok = ok

    def _maintenance_loop(self):
        while not self._stop_event.is_set():
            self._background(self._tick)
            self._dirty.wait(self.REFRESH_INTERVAL)
            self._dirty.clear()

    def _tick(self):
        with self._pairing_ops:
            self._tick_locked()

    def _tick_locked(self):
        self._refresh()
        # After the refresh, so a peer that has just announced itself is a
        # candidate on this tick rather than the next one.  The fetch it may
        # start runs on its own thread -- see _auto_update_from_peers.
        self._auto_update_from_peers()
        self.delivery.tick()
        # Fragmented clipboard frames: ask for the gaps in one that has gone
        # quiet, and forget the ones nobody is going to finish.
        self._sweep_clip_chunks()
        # From the clock, not from the next pull: the panel waits for one event
        # per request before it stops saying "waiting to receive files", and a
        # reply that never arrives has no other moment to be reported at.
        self.ai_config.expire_pulls()
        with self._lock:
            deferred = dict(self._deferred)
        connected = set(self.transport.get_connected_peers())
        pending = {p[0] for p in self.pairing.get_pending_pairings()}
        for pid, (kind, deadline) in deferred.items():
            # A notice that *ends* a pairing is valid on the opposite rule from
            # one that concludes it: a confirmation matters while the pairing is
            # still live (pending or paired), an unpair/reject matters while it
            # is not.  Reading both off the confirmation rule is how a severed
            # pairing stayed severed on one device and intact on the other.
            if kind in ENDING_MSG_TYPES:
                # Only a completed pairing supersedes an ending notice.  A
                # *request* does not, because reconnecting to a peer this
                # device no longer trusts is what puts the shared code back on
                # screen: both connection paths re-offer it for any known
                # unpaired peer (`generate_shared_pairing_code`), which is how
                # two machines on this network come to pair at all.  Reading
                # that as "the pairing is live again" discarded the notice in
                # the one case it exists for -- the link came back after the
                # unpair -- and left the other device paired forever.
                valid = not self.pairing.is_peer_paired(pid)
            else:
                # Cancel stale confirmations after reject/unpair/expiry.
                valid = pid in pending or self.pairing.is_peer_paired(pid)
            expired = time.monotonic() >= deadline
            sent = valid and pid in connected and self._send_pairing(pid, kind)
            if valid and not sent and not expired and pid not in connected:
                # Nothing to send down yet.  The single dial `_end_pairing`
                # made is an attempt, not a delivery: it fails on its own when
                # the peer is mid-reconnect or the TLS handshake does not
                # finish, and when it does the notice has nowhere to go and
                # nobody tries again.  That is the whole of the divergence --
                # one device unpaired, the other still showing the pairing as
                # live -- so the window is spent reaching for the peer.
                self._redial_for_notice(pid)
            if sent or not valid or expired:
                with self._lock:
                    if self._deferred.get(pid) == (kind, deadline):
                        self._deferred.pop(pid, None)
                if expired and valid and not sent:
                    # An ending notice that ran out of window is logged rather
                    # than raised: the user asked to break the pairing and this
                    # device did, so the operation they performed succeeded.  The
                    # peer only failed to hear about it, and a toast here would
                    # have to say that in a sentence none of the three fronts has
                    # a catalog entry for -- what it actually renders today is
                    # the runtime's untranslated "LAN operation failed".  It is
                    # a deliberate divergence from the previous panel, and the
                    # warning below is the record of it.
                    if kind in ENDING_MSG_TYPES:
                        logger.warning(
                            "LAN runtime: %s to %s never landed; that device may "
                            "still show this pairing as live",
                            kind,
                            pid[:12],
                        )
                    else:
                        self._error("PAIRING_SEND_FAILED")

    def _redial_for_notice(self, pid):
        """Dial a peer again so a deferred ending notice has a link to travel on.

        `_end_pairing` dials once, immediately, which is the right first move
        and not a guarantee: the dial is asynchronous and fails on its own if
        the handshake does not finish or the peer is already reconnecting to
        us.  Throttled, because the tick runs four times a second and each
        attempt starts a connection of its own.  The stale entry this leaves
        for a peer that is never dialled again costs one delayed attempt, which
        is cheaper than tracking the dict's lifetime in the six places a
        deferred notice can be dropped.
        """
        now = time.monotonic()
        with self._lock:
            if now - self._ending_dial_at.get(pid, 0.0) < self.ENDING_DIAL_RETRY:
                return
            self._ending_dial_at[pid] = now
        self._connect(pid)

    def _internet_unpaired(self, peer_id):
        """An internet pair was severed — drop its sends and tell the UIs.

        Legacy pushed ``netpair_peer`` {status:"unpaired"} from the REST route
        that did the unpairing; publishing it from the runtime instead means
        every caller (the phone, the desktop settings panel) reaches the same
        listeners, and the phone's other tabs can drop the row.
        """
        self.delivery.clear_peer(peer_id)
        self._publish("netpair.peer.changed", {"peer_id": peer_id, "status": "unpaired"})

    def _drop_internet_pairing(self, peer_id):
        """Break this device's code pairing, when trust with it is broken.

        A device can hold two pairings at once — a pinned LAN certificate and a
        code pairing over the relay — and both are trust in the same machine, so
        an action that ends trust has to end it on both.  Leaving the code
        pairing standing after 移除设备 left a device the user had removed still
        able to publish clipboard, chat and files into this machine (a relay
        frame's gate is reachability, and reachability was the secret that was
        never dropped), still listed as paired on the internet pairing card, and
        still reachable by a chat invite this side sent.  The removal notice
        promises the peer has to pair again; this is what makes that true.
        """
        if peer_id not in (getattr(self.config, "netpair_secrets", {}) or {}):
            return
        try:
            self.internet_pairing.unpair(peer_id)
        except Exception:
            # Nothing here is worth failing the removal for: the reachability
            # gate refuses a removed device on its own, and this is the state
            # cleanup that keeps the pairing card honest.
            logger.debug(
                "Could not drop the internet pairing for %s", str(peer_id)[:12], exc_info=True
            )

    def _drop_stranded_internet_pairings(self):
        """Finish a removal that a build older than this one left half done.

        Removal ends both of a device's routes, and the code pairing is dropped
        with the rest of it (``_drop_internet_pairing``).  A config written
        before that was so keeps the secret: the device is archived, the list
        says 已移除, and the relay pairing is still on disk — one restore away
        from coming back to life, which is the opposite of what the removal
        notice promised the user.  Repairing it here rather than filtering it at
        every read means the secret is gone rather than merely unspoken, and it
        runs in the same slot as the other repair over the archive
        (``_rekey_archived``) — before anything reads the state it fixes.
        """
        with config_lock:
            stranded = sorted(
                set(self.config.removed_peers or {}) & set(self.config.netpair_secrets or {})
            )
        for pid in stranded:
            logger.info("Dropping the internet pairing of removed device %s", str(pid)[:12])
            self._drop_internet_pairing(pid)

    def _delivery_changed(self, peer_id, msg_id, status, content_hash, kind, session_id):
        """Publish one send-ledger transition (legacy ``internet_delivery``)."""
        self._publish("relay.delivery.changed", {
            "peer_id": peer_id,
            "msg_id": msg_id,
            "status": status,
            "content_hash": content_hash,
            "kind": kind,
            "session_id": session_id,
        })

    def _relay_state_changed(self, state):
        self._publish("relay.state.changed", {"state": state})
        if state == "online":
            # Coming online is a retransmission trigger: flush every peer that
            # queued frames while the relay was down.
            self.delivery.retry_all()
            # ...and the moment to offer every paired peer this machine's relay
            # secret, which is what legacy did when its relay started.  Doing it
            # on the state change rather than once at startup covers a relay that
            # reconnects, and reaches the peers that were not up the first time.
            self.internet_pairing.enroll_peers()

    def _send_relay_enroll(self, peer_id, payload):
        """Hand one ``relay_enroll`` payload to a peer over its LAN link.

        The LAN transport alone, and it reports whether the frame went out so the
        service knows whether to remember the offer — the peer's own answer is
        what the exchange is for, and a send to a peer that is not connected must
        not count as having asked.
        """
        try:
            return bool(
                self.transport.send_to_peer(
                    peer_id, encode_frame(payload, source_device=self.config.device_id)
                )
            )
        except Exception:
            logger.warning(
                "relay enroll to %s failed", str(peer_id)[:12], exc_info=True
            )
            return False

    def relay_state(self):
        """Internet-sync relay state for diagnostics: off/connecting/online/error.

        Mirrors the legacy accessor: enabled but no transport yet reports
        ``connecting`` rather than a misleading ``off``.
        """
        if not getattr(self.config, "internet_sync_enabled", False):
            return "off"
        if self.relay is None:
            return "connecting"
        try:
            return self.relay.state
        except Exception:
            return "connecting"

    def delivery_counts(self):
        """Per-peer queued relay sends: ``{"peers": {peer_id: count}}``."""
        return self.delivery.counts()

    def relay_delivery_status(self, peer_id=""):
        """Send ledger for the UI: ``{"pending": N, "items": [...]}``.

        Rows carry the legacy fields (status sent/delivered/failed/queued,
        preview, content_hash, kind, session_id) so a relayed send can be told
        apart from a chat frame and stamped in the right chat session.
        """
        return self.delivery.status(peer_id)

    def _resolve(self, device_id):
        resolved = self.transport.get_resolved_hashes()
        if device_id in resolved:
            return resolved[device_id]
        for peer in self.pairing.get_known_peers():
            if device_id in (peer.device_id, peer_id_hash(peer.device_id)):
                return peer.device_id
        # A third way this machine knows a device: by internet pairing code.  A
        # device paired that way is not in the local repository, so its hashed
        # sighting used to stay unresolved — and an unresolved sighting is a row
        # keyed by the hash, while the pairing it holds is keyed by the real id.
        # The same device was then drawn twice, as a device with no internet
        # pairing and as an internet peer this machine has never seen here, with
        # neither row carrying what the other knew.  Resolving it here is what
        # makes one row answer for both routes.  Nothing is relaxed by it: a
        # frame that is not from the relay still has to come from a peer in the
        # repository (see ``_on_peer_message``), and the relay has its own gate
        # (``_peer_is_internet_reachable``).
        for pid in (getattr(self.config, "netpair_secrets", {}) or {}):
            if pid != device_id and peer_id_hash(pid) == device_id:
                return pid
        return device_id

    def _chosen_name(self, pid, fallback=""):
        """The best name on record for *pid*, or *fallback* when there is none.

        Better than the sighting's own name, which is only the peer's answer
        when it published one: a peer that named itself is listed by that name,
        one that did not keeps whatever name this machine already learned — the
        relay's handshake, or a dial from a build that did publish one — rather
        than being renamed to a truncation of its hostname by the next sighting.

        Read from :meth:`_published_name` and not :meth:`_peer_name`, because
        this string is handed to the transport as the dial's own label for the
        peer (see :meth:`_address`), where it is recorded as that peer's name.
        The user's local alias belongs on this machine's screens, not in that
        record — the alias is a local-only view the peer never learns.
        """
        with config_lock:
            peer = self.config.peers.get(pid)
            known = getattr(peer, "device_name", "") or ""
        if not known:
            known = self._published_name(pid)
        return known or fallback

    def _sightings(self, grace=None):
        """Every sighting this runtime still trusts: live ones, and lapsed ones
        still inside ``grace`` (``SIGHTING_GRACE`` by default).

        A lapsed sighting is a real answer to every question a live one answers
        -- where the device is, what it calls itself, what version it runs --
        and the only thing that has changed is that its record is not being
        announced this second.  Entries past the grace are dropped here, on the
        way out, so the map cannot grow without a reader (``_refresh`` runs
        four times a second).

        Live sightings win over lapsed ones for the same id: a device that moved
        (DHCP renewal, Wi-Fi reconnect) announces from its new address, and the
        entry it replaces is the stale one.  Callers get a copy and walk it
        outside ``_lock``, which is what keeps ``_resolve`` -- the transport and
        the pairing repository -- out of the lock.
        """
        limit = self.SIGHTING_GRACE if grace is None else grace
        now = time.monotonic()
        with self._lock:
            # Always pruned at SIGHTING_GRACE, never at the caller's limit: a
            # caller asking the shorter question (who is *here*) must not be
            # able to throw away the longer answer (what we know about them).
            for pid in [
                pid
                for pid, (_, when) in self._lost_sightings.items()
                if now - when > self.SIGHTING_GRACE
            ]:
                del self._lost_sightings[pid]
            sightings = {
                pid: info
                for pid, (info, when) in self._lost_sightings.items()
                # Strictly inside the window, so a grace of 0.0 means *gone*.  It
                # used to be `<=`, and that made the zero case depend on the
                # clock: `lost()` records ``time.monotonic()`` and this runs
                # later, so an entry is at exactly age 0.0 whenever the clock did
                # not tick in between.  On a machine with a coarse monotonic
                # clock that happens nearly always and the entry survived a
                # zero grace; on one with a fine clock it never did.  A test that
                # sets ``PRESENCE_GRACE = 0.0`` therefore passed on the machine
                # it was written on and failed on a CI runner.
                if now - when < limit
            }
            sightings.update(self._discovered)
        return sightings

    def _address(self, pid):
        sightings = self._sightings()
        for key, info in sightings.items():
            if self._resolve(key) == pid:
                # The three the dialer takes, and no more: every caller unpacks
                # them into ``connect_to_peer(pid, name, address, port)``.
                #
                # The name is handed to the transport, which records it as this
                # peer's name — so a fallback label here becomes the peer's
                # stored name on the far side of the dial.  Ask for a better one
                # first; the address and port still come from the sighting,
                # which is the only thing that knows where the peer is now.
                name = info["name"] if info.get("named") else self._chosen_name(pid, info["name"])
                return (name, info["address"], info["port"])
        saved = self.transport.get_saved_address(pid)
        if saved:
            return saved
        with config_lock:
            peer = self.config.peers.get(pid)
            if peer and peer.last_ip:
                return peer.device_name, peer.last_ip, peer.last_port or self.config.port
        return None

    def _connect(self, pid, no_auto_pairing=False):
        """Dial one peer; True once the dial was started.

        ``no_auto_pairing`` is the nearby-chat flow's, and it is the default
        that matters here: a dial made to sync or to pair *should* offer the
        shared pairing code, because that is how two machines on this network
        come to trust each other.  A dial made only to open a conversation must
        not, because chat has its own invite-and-fingerprint consent and an
        unpaired peer has not agreed to anything yet — dialing it the ordinary
        way used to raise a pairing code on both screens the moment someone
        clicked a device in the chat list.  See
        ``TransportManager.connect_to_peer``.
        """
        if self._stop_event.is_set():
            return False
        if pid in self.transport.get_connected_peers():
            return True
        address = self._address(pid)
        if address is None:
            return False
        with self._lock:
            self._connecting[pid] = time.monotonic() + self.PAIRING_SEND_WAIT
        self.transport.connect_to_peer(pid, *address, no_auto_pairing=no_auto_pairing)
        return True

    def _receive_relay(self, frame, topic="", key_index=0):
        if not frame or self._stop_event.is_set():
            return
        msg = decode_message(frame)
        if msg is None:
            self._error("RELAY_FRAME_INVALID")
            return
        # Which key of the channel's list opened this frame: 0 is the channel's
        # current one, and a higher index means the frame rode the code-derived
        # key that a netpair channel keeps only so the handshake can be read
        # before the key agreement has happened (see
        # ``InternetPairingService.netpair_keys_for_topic``).  Once both ends
        # know each other's public key, that older key must buy nothing but the
        # handshake itself: it is derived from the 35-bit pairing code, so
        # anyone who has enumerated the code — the exact attack the agreement
        # exists to defeat — can produce frames under it.  Accepting them would
        # leave the channel as forgeable as it was before.
        if key_index and getattr(msg, "msg_type", "") != "netpair_hello":
            logger.warning(
                "Dropping relay frame: it rode the pairing-code key on a channel "
                "that has moved to key agreement"
            )
            return
        source = getattr(msg, "source_device", "") or ""
        if source == self.config.device_id:
            return
        kind = getattr(msg, "msg_type", "")
        # The identity the channel itself is bound to: a topic is derived from a
        # shared secret, so it — never a frame's self-declared source — says who
        # may speak on that channel.
        ident = self.internet_pairing.topic_identity(topic)
        if not source:
            # Chat file BYTES ride the compact binary frame, whose 46-byte
            # header is transfer_id + indices and has nowhere to put a device
            # id.  Landing one used to end on the line above — ``not source``
            # returned with nothing logged — so over the internet every chunk of
            # a chat attachment was dropped (the offer/accept/complete frames
            # are JSON and routed fine, which is why the transfer reached the
            # receiver's finalize check with 0 bytes on disk and died there as
            # a size mismatch).  The LAN path was unaffected: there the peer
            # comes from the pinned connection, not from the frame.  The
            # channel resolves the identity either way, so attribute the frame
            # to the peer whose secret derives the topic it arrived on; a
            # provisional netpair tag names no device yet, so those still drop.
            if kind != "file_chunk" or ident is None or is_provisional_key(ident):
                return
            source = ident
        # A frame is proof of two things before it is routed: that its sender is
        # alive, and — when it is a peer whose confirmation hello was lost — who
        # that sender really is.  The second re-keys the entry the code was
        # stored under, which is what the lost hello would have done.
        self.internet_pairing.note_relay_source(source)
        # ...and it proves the peer reachable, so retry whatever is still queued
        # for it (a cheap no-op when nothing is queued).
        self.delivery.retry_peer(source)
        if kind == "netpair_hello":
            payload = getattr(msg, "_raw_payload", {})
            # Which half of the handshake this frame is, and what it changes, is
            # the pairing service's to decide: it owns the secrets, and the
            # topic→secret lookup that tells a frame of ours from a stranger's
            # has to see the codes we generated as well as the pairs we hold.
            # This branch used to resolve the secret against the persisted map
            # alone, so the hello answering a generated code — the one frame the
            # whole exchange exists to deliver — arrived on a topic we were
            # listening to and was discarded here, silently, on both machines.
            result = self.internet_pairing.handle_hello(
                source, payload.get("peer_id", ""), payload.get("device_name", "") or "", topic,
                dh_pub=payload.get("dh_pub", ""),
            )
            if not result["accepted"]:
                return
            if result["reply"]:
                # Only the generator owes one, and the enterer has nothing else
                # to learn our real device id from: without this the pair stays
                # keyed by a 4-char tag that will never become a device.
                self.internet_pairing.confirm_hello(result["peer_id"], result["secret"])
            # A confirmed handshake is proof the peer is alive, so the pairing
            # card can show it online immediately rather than waiting for its
            # next status pull (legacy pushed the same liveness fields).
            self._publish("netpair.peer.changed", {
                "peer_id": result["peer_id"],
                "name": payload.get("device_name", ""),
                "status": "paired",
                "online": True,
                "last_seen": int(time.time()),
            })
            self._publish("devices.changed", self.devices())
            return
        # Bind the frame's self-declared source to the channel it arrived on.
        # A topic is derived from a shared secret, so holding that secret
        # entitles a peer to speak *as* the identity bound to that channel and
        # as nothing else.  Without this, one paired peer could publish with a
        # second paired device's id in ``source_device`` and have everything it
        # sent — clipboard content, chat, receipts — attributed to that second
        # device, which holds a pairing it never used.  Legacy bound the frame
        # in the same place, in the same three cases.  (``ident`` was resolved
        # above, where a source-less binary frame takes it as its source.)
        if ident is not None and source:
            if is_provisional_key(ident):
                # The peer's real id is not known until its hello confirms it,
                # but the code carried its 4-char tag, so the source it claims
                # has to hash to that tag.
                if netpair_device_tag(source) != ident:
                    logger.warning(
                        "Dropping relay frame: source %s does not match the channel's device tag",
                        source[:12],
                    )
                    return
            elif source != ident:
                logger.warning(
                    "Relay frame claims source %s but its channel belongs to "
                    "%s — attributing it to the channel owner",
                    source[:12],
                    ident[:12],
                )
                source = ident
        if kind == "relay_ack":
            # Routed after the bind, so a receipt resolves against the owner of
            # the channel it came in on rather than against whatever device the
            # frame named.
            msg_id = getattr(msg, "_raw_payload", {}).get("msg_id", "")
            if isinstance(msg_id, str) and msg_id:
                self.delivery.note_ack(source, msg_id)
            return
        if kind in ("device_ping", "device_pong"):
            if self._peer_is_internet_reachable(source):
                self._handle_device_probe(kind, getattr(msg, "_raw_payload", {}), source, True)
            return
        # Everything else is an ordinary ClipSync frame — clipboard content,
        # chat text, chat file chunks, aiconfig — so it goes through the very
        # same router LAN frames use, with ``via_relay`` selecting the gate that
        # fits an un-certificated transport.  This is what lets a conversation
        # with an internet-only paired device work at all.
        self._receive(msg, source, via_relay=True)

    def _relay_channels(self):
        return self.internet_pairing.channels()

    def _archived_ids(self) -> set[str]:
        """Every id form of the devices the user has removed.

        Both forms because a sighting arrives hashed while the archive is keyed
        by the real device id (see ``internal.transport.peer_id``).
        """
        return expand_id_forms(self.config.removed_peers)

    def _peer_found(
        self, pid, name, address, port, version="", os_name="", arch="", app="", named=False
    ):
        """One mDNS sighting, with whatever the peer advertised about itself.

        The row is a dict rather than the ``(name, address, port)`` tuple it used
        to be: the advertised version and platform belong to the same sighting,
        and a second structure beside this one would be a second thing to keep
        in step with it.

        ``named`` says whether ``name`` is the peer's own answer — the name its
        user set — or this machine's fallback reading of its instance label.
        Only the first may outrank a name already on record; see ``_refresh``.

        ``app`` is which of the two applications published from this repository
        the peer runs, and is empty for any build that predates the field.
        """
        if pid in (self.config.device_id, peer_id_hash(self.config.device_id)):
            return
        if pid in self._archived_ids():
            # A removed device, heard again.  Removal is a decision the user
            # made, and nothing tells the peer to stop announcing itself — so
            # this is a sighting of a device that is deliberately not on the
            # list, and keeping it draws a second row beside the archive entry
            # it belongs to.  The archive row is what the list shows; restoring
            # is the way back, and it drops this cache on the way in, so the
            # sighting after that one counts again.
            #
            # The refresh is not for a row: a sighting is how a device the user
            # removed under its bare mDNS id gets a real one, and the archive
            # key follows it there (see _rekey_archived) — which is the id the
            # row's own restore and purge buttons are handed.
            self._refresh()
            return
        row = {
            "name": name,
            "named": named,
            "address": address,
            "port": port,
            "version": version,
            "os": os_name,
            "arch": arch,
            "app": app,
        }
        with self._lock:
            # Announced again: whatever it said before it went quiet is replaced
            # by what it is saying now, and it is no longer a lapsed sighting.
            previous = self._discovered.get(pid)
            self._lost_sightings.pop(pid, None)
            self._discovered[pid] = row
        real = self._resolve(pid)
        with self._lock:
            # When this machine heard it, on the same clock as an answer's own
            # stamp (`_answer_overlay`).  Kept beside the row rather than inside
            # it: this row is compared against the previous one to decide whether
            # a paired peer needs dialing again, and a timestamp that changed on
            # every announcement would make every announcement look like news.
            self._sighted_at[real] = time.monotonic()
        if named:
            # The peer's own answer, written into the record every surface falls
            # back to when the peer is not advertising — and the sighting this
            # arrived on is the only word this machine ever gets that its user
            # renamed it.  Kept in the sighting alone, the name lasted exactly as
            # long as the announcement did: the row went back to the one written
            # when the two last dialed, which is the name frozen on that peer's
            # certificate, and the web companion — which reads the record and
            # never the advertisement — showed that older name throughout.
            self.pairing.set_peer_name(real, name)
        with self._lock:
            dialing = self._connecting.get(real, 0) > time.monotonic()
        if self.pairing.is_peer_paired(real) and (not dialing or previous != row):
            self._connect(real)
        self._refresh()

    def _peer_lost(self, pid):
        """The peer stopped advertising itself over mDNS: drop the sighting.

        The record is all that goes.  An mDNS announcement is unauthenticated
        and expires on whatever schedule the advertiser chose, so it is not a
        statement about the connection underneath it — and this used to read it
        as one, tearing down a live link and the retry loop behind it whenever a
        record lapsed.  A device still answering a probe while its record had
        gone was listed 离线 on a working connection, and anyone on the LAN
        could force the same by forging a goodbye packet for that service name.

        Liveness is the transport's to decide, from the socket: a peer that
        really left is noticed there (a closed socket at once, an unreachable
        one within the keepalive budget) and the retry scheduler starts from
        that, which is also where a peer that comes back is picked up.
        """
        real = self._resolve(pid)
        with self._lock:
            info = self._discovered.pop(pid, None)
            if info is not None:
                # Kept as a lapsed sighting rather than dropped: see
                # SIGHTING_GRACE.  The row outlives the record, which is the
                # difference between a device that is here and one that blinks.
                self._lost_sightings[pid] = (info, time.monotonic())
            self._connecting.pop(real, None)
        self._refresh()

    def _new_pairing(self, *_args):
        # Transport invokes this before it registers the accepted connection.
        # Refresh/deferred sends run on the maintenance thread after handoff.
        self._dirty.set()

    def _security_alert(self, name, pid, _expected="", _received="", new_cert=""):
        """A peer presented a certificate that no longer matches its pin.

        The connection has already been refused by the transport; all this does
        is tell the user, so the device does not sit "paired but unreachable"
        with no visible reason, and hold the presented certificate so the answer
        to that prompt has something to pin.  Throttled per peer, the certificate
        included — a peer that keeps reconnecting is answered once with the
        certificate from the alert the user was actually shown.
        """
        real = self._resolve(pid)
        if not real:
            # Nothing to key the prompt on: legacy declined to prompt either.
            logger.warning("Certificate alert without a device id")
            return
        now = time.monotonic()
        with self._lock:
            self._connecting.pop(real, None)
            seen = self._cert_alert_seen.get(real)
            if seen is not None and now - seen < self.CERT_ALERT_THROTTLE:
                logger.debug("Certificate prompt for %s suppressed (throttled)", real[:12])
                return
            self._cert_alert_seen[real] = now
            peer_name = name or self._peer_name(real)
            self._pending_certs[real] = (peer_name, new_cert or "")
        self._publish(
            "device.security_alert",
            {
                "device_id": real,
                "name": peer_name,
                "code": "CERTIFICATE_CHANGED",
                # The dial side knows only the pin it refused, so the window has
                # to be able to say "connect once more before trusting" instead
                # of offering a button that cannot work yet.
                "can_trust": bool(new_cert),
            },
        )
        self._refresh()

    def retrust_device(self, device_id):
        return self._command(self._retrust_device, device_id)

    def _retrust_device(self, device_id):
        """Answer a certificate change with "trust again" (legacy's retrust).

        The user has decided the change is their own reinstall, so the
        certificate the device presented replaces the pin and the pairing stands.
        Re-pinning is also what lifts the transport's pin-mismatch block — it
        resumes reconnecting on its own once the pinned fingerprint changes — and
        the dial is asked for now rather than waiting for that tick.
        """
        pid = self._resolve(device_id)
        with self._lock:
            pending = self._pending_certs.pop(pid, None)
        if pending is None:
            raise ApplicationError("NOT_FOUND", "No certificate change is pending")
        name, new_cert = pending
        if not new_cert:
            # The alert carried no certificate, so there is nothing to pin yet;
            # the device is still paired and pinned as before, and the next
            # connection from it (the accept side) alerts with the real one.
            raise ApplicationError(
                "VALIDATION_ERROR", "The device must connect again before it can be trusted"
            )
        if not self.pairing.update_peer_certificate(pid, new_cert):
            # Unknown to the pairing manager (e.g. restored from config with an
            # empty pin) — (re)add it now, keeping it paired.
            self.pairing.add_peer(pid, name or pid, new_cert, paired=True)
        with config_lock:
            peer = self.config.peers.get(pid)
            if peer is not None:
                peer.public_key_pem = new_cert
                peer.paired = True
        try:
            self._save_config()
        except Exception:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save device state", retryable=True
            ) from None
        logger.info("Re-trusted peer %s (%s)", name or pid, pid[:12])
        self._connect(pid)
        self._refresh()
        return {"trusted": True}

    def _connect_rejected(self, name, pid, reason=0):
        real = self._resolve(pid)
        with self._lock:
            self._connecting.pop(real, None)
        # The peer's name rides along so the phone can toast "«name» refused"
        # instead of a bare device id (legacy read it from the pairing repo).
        #
        # The reason rides along too, because the two refusals need different
        # words and only one of them has an action: "it removed this device" is
        # final until the user re-pairs *there*, while "it will not trust this
        # certificate" is fixed by re-pairing here.  A single vague "refused"
        # leaves the user with a device that stopped working and nothing to do.
        self._publish(
            "device.connection_rejected",
            {
                "device_id": pid,
                "name": name or "",
                "reason": _reject_reason_name(reason),
            },
        )
        self._refresh()

    def _persist(self):
        known = self.pairing.get_known_peers()
        changed = self._persist_dirty
        with config_lock:
            for peer in known:
                existing = self.config.peers.get(peer.device_id)
                value = (
                    replace(existing) if existing else PeerInfo(peer.device_id, peer.device_name)
                )
                value.device_name = peer.device_name
                value.public_key_pem = peer.certificate_pem
                value.paired = peer.paired
                address = self._address(peer.device_id)
                if address:
                    _, value.last_ip, value.last_port = address
                if value != existing:
                    self.config.peers[peer.device_id] = value
                    changed = True
        if changed:
            self._persist_dirty = True
            try:
                self._save_config()
            except Exception:
                raise ApplicationError(
                    "SAVE_FAILED", "Could not save peer state", retryable=True
                ) from None
            self._persist_dirty = False

    def _rekey_archived(self):
        """Move an archive entry to the id its device is really known by.

        A device is listed under a hashed mDNS id until something resolves it
        to the real one, and the user can remove it while it is still listed
        that way.  The archive entry then keeps the hash as its key while the
        device itself comes back under its real id — the removed-device block
        suppressed itself on "this id is already listed", which it never was,
        so the same device was drawn twice: once as a device, once as its own
        archive.  Renaming the entry is the fix rather than comparing resolved
        ids at read time, because restore and purge are given the id from the
        row, and a key that changes underneath them is a key they cannot use.
        """
        with config_lock:
            archived = dict(self.config.removed_peers)
        moves = {
            pid: real
            for pid in archived
            if (real := self._resolve(pid)) != pid
        }
        if not moves:
            return
        with config_lock:
            for pid, real in moves.items():
                peer = self.config.removed_peers.pop(pid, None)
                if peer is None:
                    continue
                peer.device_id = real
                existing = self.config.removed_peers.get(real)
                if existing is None:
                    self.config.removed_peers[real] = peer
                else:
                    # Both describe one device. The canonical entry is the one
                    # the list drew, so it keeps its identity and its place;
                    # only what the alias carried and it lacks is merged in.
                    existing.notes = existing.notes or peer.notes
                    if not existing.last_ip:
                        existing.last_ip, existing.last_port = peer.last_ip, peer.last_port
                    existing.removed_at = max(existing.removed_at, peer.removed_at)
            self._persist_dirty = True

    def _refresh(self, publish=True):
        with self._pairing_ops:
            self._rekey_archived()
            self._drop_stranded_internet_pairings()
            pending = {p[0]: p for p in self.pairing.get_pending_pairings()}
            self.pairing.drain_expiry_rollbacks()
            if publish:
                self._persist()
            known = {p.device_id: p for p in self.pairing.get_known_peers()}
            archived_ids = set(self.config.removed_peers)
            connected = {self._resolve(pid) for pid in self.transport.get_connected_peers()}
            # Live sightings and lapsed ones still inside the grace (see
            # ``_sightings``): a device whose record has just lapsed is still
            # this machine's picture of that device, and dropping it here is
            # what emptied a row of its version and platform, and took it off
            # the list entirely when it was not paired.
            sightings = self._sightings()
            # The same sightings, asked the shorter question: which of these
            # devices is *here*, as opposed to which of them this machine can
            # still describe (see PRESENCE_GRACE).  Membership reads this one and
            # nothing else does, so a device that has gone quiet keeps its name,
            # its address and the version on its row for five minutes while
            # stopping being a device that is on the network.
            present = self._sightings(self.PRESENCE_GRACE)
            with self._lock:
                connecting = dict(self._connecting)
            # What each peer advertises about itself, keyed the way the rows
            # are: a device seen only over mDNS has no other source for these,
            # and a paired one that is currently away keeps the last sighting's
            # answer -- which is the version it will still be running when it
            # comes back.
            # A sighting, with a newer direct answer laid over it: the two
            # sources carry the same five facts and can disagree (see
            # `_answer_overlay`).
            advertised = {}
            for pid, info in sightings.items():
                real = self._resolve(pid)
                advertised[real] = self._answer_overlay(real, info)
            # Which row gets which name:
            #
            # 1. The name the peer published about itself.  This is the peer's
            #    own answer to "what is this device called", and it outranks the
            #    name on record — a device renamed on the other side has to be
            #    renamed here too, and a name written down when it was last
            #    dialed can never learn that the peer's settings changed.
            # 2. The name already on record (the relay handshake, an earlier
            #    dial from a build that did publish one, the pairing prompt).
            # 3. The truncated instance label, which fills in a peer that has
            #    no name of its own yet — and which is all a peer running a
            #    build older than the published-name field can offer.
            #
            # A label never replaces a name, though: it is a truncation of a
            # hostname, and letting it write over a real one is how a listed
            # device gets renamed to something nobody chose.
            names = {pid: p.device_name for pid, p in known.items()}
            for pid, info in advertised.items():
                if info.get("named") or not names.get(pid):
                    names[pid] = info["name"]
            # Keyed the way `advertised` is -- through `_resolve` -- because the
            # two are compared against each other and against the ids the
            # pairing repository holds: a sighting can live under a hashed mDNS
            # id while every row, and every `pid in ...` test below, is the
            # resolved device id.
            discovered_ids = {self._resolve(pid) for pid in present}
            # This machine's internet pairings, by device id.  Every row reads
            # them, not only the ones the pairing below draws: a device can hold
            # both routes at once — a pairing code is how two machines that have
            # never met are introduced, and the same two can then meet on a
            # network — and there is one row per device, so whichever producer
            # draws it has to carry the other join's facts.  Read once per pass
            # rather than per row; it is two config tables.
            internet = {
                net["peer_id"]: net for net in self.internet_pairing.paired_peers()
            }
            rows = []
            # Which ids the loop below actually drew a row for, as opposed to
            # which ones it looked at.  The internet pairs are added after it
            # and need the difference: a device this machine also knows on the
            # LAN is already a row, and drawing it twice is the one thing a
            # device list cannot survive.
            listed = set()
            for pid, name in sorted(names.items()):
                peer = known.get(pid)
                paired = bool(peer and peer.paired)
                request = pending.get(pid)
                status = "paired" if paired else self.pairing.get_pairing_status(pid)
                # A device earns a row by being someone the user has a
                # relationship with, or by being here now.  `connect_to_peer`
                # records the certificate of every peer it dials, with
                # `paired=was_paired`, so without this an unpaired device that
                # was merely seen once keeps a row for good -- it reads as a
                # device that will not leave, and the list stops being a
                # picture of the network.
                #
                # Kept: paired (that is what pairing bought, and
                # `connection_state` already reports it offline), a pairing
                # prompt in flight or a pairing that ended in a status the
                # user has not seen (`cancelled`, `expired`), a note the user
                # wrote, an archive entry (the recovery path), and anything
                # connected or visible right now.
                #
                # `names` itself is deliberately left whole: the removed-peer
                # block below reads it to keep an archived row from repeating
                # one that is already listed.
                #
                # Nothing here touches the store.  `pairing._peers` holds the
                # pinned fingerprints the certificate-change alarm compares
                # against, and dropping a pin because its device went quiet
                # would silence that alarm for good -- the list is what this
                # filter is for, not the memory.
                if not (
                    status
                    or request
                    or pid in connected
                    or pid in discovered_ids
                    or pid in archived_ids
                    # The note lives on the saved peer, not on the pinned
                    # identity `peer` is: `PeerIdentity` carries no notes.
                    or getattr(self.config.peers.get(pid), "notes", "")
                ):
                    continue
                fingerprint = self.pairing.get_peer_fingerprint(pid)
                mine = self.pairing.get_identity().fingerprint
                seen = advertised.get(pid) or {}
                net = internet.get(pid)
                # Read the same address a click would dial with, once, rather
                # than per front end.  A sighting answers first, then the
                # transport's own memory of the peer — which now includes the
                # address the peer's own call taught it — and last the address
                # written down the last time this machine could reach it.
                dialable = pid in connected or self._address(pid) is not None
                listed.add(pid)
                rows.append(
                    {
                        "id": pid,
                        "name": name,
                        "note": (getattr(peer, "notes", "") or "") if peer else "",
                        "paired": paired,
                        # What the peer said about itself in its mDNS records.
                        # Empty for a peer that predates those fields, which is
                        # why nothing may read them as "up to date".
                        "version": seen.get("version", ""),
                        "platform": seen.get("os", ""),
                        "arch": seen.get("arch", ""),
                        # Whether this build could update that device: same
                        # platform, and a version this one is ahead of.  Decided
                        # here rather than in a front end because the comparison
                        # and the platform spelling are the sidecar's, and a
                        # second implementation of either is a second answer to
                        # the same question.
                        "update_available": self._update_offerable(seen),
                        # The same question the other way round: whether *that*
                        # device could update this one.  Both can be false, and
                        # both true is impossible -- they are opposite readings
                        # of one version comparison.
                        "update_fetchable": self._update_fetchable(seen),
                        # Whether the offer would carry the installer or only
                        # the news.  A peer told "no" has its own release check
                        # to fall back on.
                        "update_cached": bool(updater.get_cached_asset()),
                        # And, when neither flag is set, why not -- a code the
                        # window turns into a sentence on the row's own update
                        # entry.  Without it the only way this row could report
                        # "the peer is behind and nothing will happen" was by
                        # showing no button, which is what a level pair, a peer
                        # too old to call and another platform all look like.
                        "update_blocked": self._update_blocked(seen),
                        # This device's internet pairing, on this row because
                        # the row is the device's and this is one of its two
                        # routes.  A front end that could only see the join its
                        # own producer knew about read the other one as absent:
                        # a device paired by code, met on this network, kept the
                        # LAN card -- whose chat, test and send-URL actions are
                        # each gated on the LAN pairing it does not have -- and
                        # lost every one of them, while the 互联网 chip beside
                        # them said the peer was online.  `relay` stays False:
                        # that flag marks the row that exists *only* for the
                        # relay join, which decides what a row may offer.
                        "relay": False,
                        "relay_paired": net is not None,
                        "relay_online": bool(net.get("online")) if net else False,
                        # The user's own name for it, and when the relay last
                        # heard from it — the two facts an internet-paired peer
                        # contributes to a row it does not own.
                        "alias": (net.get("alias") or "") if net else "",
                        "last_seen": float(net.get("last_seen") or 0.0) if net else 0.0,
                        "connection_state": (
                            "online"
                            if pid in connected
                            else "connecting"
                            if connecting.get(pid, 0) > time.monotonic()
                            else "discovered"
                            if not paired and pid in discovered_ids
                            else "offline"
                        ),
                        # Whether a click can place a call.  This is not
                        # `connection_state`, and the difference is the whole
                        # of a device that reads 离线 while the one button on
                        # its row is the way back to it: that field describes
                        # the connection that exists, not the one that could be
                        # made, and a peer that went quiet but left an address
                        # behind is dialable throughout.
                        "dialable": dialable,
                        "pairing_status": status,
                        "pairing_code": request[1] if request else "",
                        "sas": sas_code(mine, fingerprint) if mine and fingerprint else "",
                        "archived": False,
                        "removed_at": 0.0,
                    }
                )
            # The devices paired by code, which until now were the one kind of
            # device this list did not show.  A pairing code is a pairing: it is
            # how two machines that have never met on a network are introduced,
            # and it left the user a card in one tab and nothing in the one
            # place devices are managed — so an internet peer could be renamed
            # and unpaired and otherwise not touched: no chat, no test, no send.
            #
            # Their rows cannot come from the pairing repository.  That
            # repository is the TLS trust store, and the LAN handshake reads "is
            # this id paired" as "was this certificate pinned here"; a peer
            # whose identity is bound by a relay channel's shared secret has no
            # certificate to pin, so writing it in would let anyone on this
            # network who claims that device id be trusted as it.  The rows come
            # from the relay pairing itself instead, carrying the route they
            # have (`relay`) so a front end can offer what the relay can carry
            # and withhold what it cannot.
            for net in internet.values():
                pid = net["peer_id"]
                if pid in listed or pid in archived_ids or pid in names:
                    # A device this machine also knows on the LAN is already a
                    # row, and its 互联网 chip is this same join read from the
                    # other side — the `relay_paired`/`relay_online`/`last_seen`
                    # fields every row above carries for exactly this reason.
                    continue
                last_seen = net.get("last_seen")
                rows.append(
                    {
                        "id": pid,
                        # The name the peer goes by, and — separately — the name
                        # the user gave it, exactly as a LAN row carries them.
                        # This row is the only one that had no `note` at all
                        # ("Notes live on a saved LAN peer"), so a front end
                        # reading `note or name` — which is the rule `deviceLabel`
                        # states and the rule the web panel's card applies — had
                        # no way to see a rename made from the pairing card on a
                        # relay-only row.  For that device the alias *is* the
                        # note: same fact, and `rename` below keeps the two in
                        # step so neither can go stale.
                        "name": net.get("name") or pid,
                        "note": net.get("alias") or "",
                        "paired": True,
                        # A device with no local route advertises nothing here:
                        # an internet peer has not announced these, and an empty
                        # version means unknown, never "up to date".
                        "version": "",
                        "platform": "",
                        "arch": "",
                        "update_available": False,
                        "update_fetchable": False,
                        "update_cached": False,
                        # For a device whose only route is the relay, the relay's
                        # own view of it *is* its connection state — there is no
                        # second link for this field to describe.  `relay` is what
                        # marks that reading, so the consumers that would
                        # otherwise misread it (dialing the peer, offering it as
                        # a transfer target) can tell the two apart.
                        "connection_state": "online" if net.get("online") else "offline",
                        # No LAN route, so no LAN dial to offer.  Said in the
                        # same field the other rows answer with, so a front end
                        # asking "can this be dialed" of any row gets the truth
                        # for a relay-only device too.
                        "dialable": False,
                        "pairing_status": "paired",
                        "pairing_code": "",
                        "sas": "",
                        "archived": False,
                        "removed_at": 0.0,
                        # This row's route, and what it is worth naming: the LAN
                        # handshake never established it, so every LAN-only
                        # action is one the card must not offer.
                        "relay": True,
                        # The same two facts the rows above carry, so a consumer
                        # can ask "is this device internet-paired" of any row
                        # rather than of the rows the pairing drew.
                        "relay_paired": True,
                        "relay_online": bool(net.get("online")),
                        # The name the user chose, on its own, because it is what
                        # the rename writes and a field that could only be read
                        # back off `name` would make "no alias" and "alias equal
                        # to the peer's own name" the same string.
                        "alias": net.get("alias") or "",
                        "last_seen": float(last_seen or 0.0),
                    }
                )
            with config_lock:
                removed = [
                    (pid, peer)
                    for pid, peer in self.config.removed_peers.items()
                    if pid not in names
                ]
            for pid, peer in sorted(removed, key=lambda item: item[1].removed_at, reverse=True):
                rows.append(
                    {
                        "id": pid,
                        "name": peer.device_name or pid,
                        "note": peer.notes or "",
                        "paired": False,
                        "connection_state": "offline",
                        # Removed is removed: the address the archive kept is
                        # for the recovery path to restore, not for a dial from
                        # the row that says the device is gone.
                        "dialable": False,
                        "pairing_status": "",
                        "pairing_code": "",
                        "sas": "",
                        "archived": True,
                        "removed_at": float(peer.removed_at or 0.0),
                    }
                )
            snapshot = {"items": rows}
            with self._lock:
                changed = snapshot != self._snapshot
                self._snapshot = snapshot
                for pid in connected:
                    self._connecting.pop(pid, None)
        if changed and publish:
            self._publish("devices.changed", snapshot)
        if publish:
            self._publish_presence(connected, pending, names)

    def _publish_presence(self, connected, pending, names):
        """Turn presence transitions into events.

        The legacy host ran this as a 3s poll of the connected set plus its
        ``notify_device_connect`` notices; this refresh is the equivalent hook.
        Pairing notices are held for :attr:`PAIRING_NOTICE_DELAY` so a chat
        invite on the same connection — chatting with an unpaired device must
        not look like a pairing request — can suppress them, exactly like the
        legacy debounce.
        """
        # Only a live conversation suppresses the notice: a closed session
        # must not silence this device's pairing notices forever.
        chat_peers = {
            session["peer_id"]
            for session in self.chat.get_sessions()
            if session["status"] == "active"
        }
        now = time.monotonic()
        events = []
        arrived = set()
        with self._lock:
            # A peer carrying an ending notice is on this link for the notice's
            # sake: the runtime dials it itself so the retry has somewhere to
            # land.  Both fronts render `device.connected` as a "connected"
            # notice, and saying that a second after the user broke the pairing
            # would contradict the thing they just did.  The transition is still
            # recorded below, so it is not announced a tick later instead.
            ending = {
                pid
                for pid, (kind, _) in self._deferred.items()
                if kind in ENDING_MSG_TYPES
            }
            for pid in sorted(connected - self._connected_seen):
                arrived.add(pid)
                if pid not in ending:
                    events.append(
                        ("device.connected", {"device_id": pid, "name": names.get(pid) or pid[:12]})
                    )
            for pid in sorted(self._connected_seen - connected):
                events.append(
                    ("device.disconnected", {"device_id": pid, "name": names.get(pid) or pid[:12]})
                )
            self._connected_seen = set(connected)
            for pid, entry in pending.items():
                code, name = entry[1], entry[2]
                seen = self._pairing_seen.get(pid)
                if seen is None or seen[0] != code:
                    # First sighting of this code: start the debounce window.
                    self._pairing_seen[pid] = (code, now, pid in chat_peers)
                    continue
                if seen[2] or pid in chat_peers or now - seen[1] < self.PAIRING_NOTICE_DELAY:
                    self._pairing_seen[pid] = (seen[0], seen[1], seen[2] or pid in chat_peers)
                    continue
                self._pairing_seen[pid] = (seen[0], seen[1], True)
                events.append(
                    (
                        "pairing.request",
                        {
                            "device_id": pid,
                            "name": name,
                            "code": code,
                            # SAS shown on BOTH devices during pairing — the user
                            # compares them before confirming (defeats a
                            # pairing-code MITM).  Legacy sent it with the push.
                            "sas": self._sas_for(pid),
                        },
                    )
                )
            # A resolved pairing must be able to notify again: the code is
            # derived from the two fingerprints, so the next genuine request
            # carries the same one and would otherwise be swallowed forever.
            # Never on expiry — an ignored request should not re-prompt.
            for pid in list(self._pairing_seen):
                if self.pairing.is_peer_paired(pid):
                    self._pairing_seen.pop(pid, None)
        for name, data in events:
            self._publish(name, data)
        # A peer whose LAN link has just come up gets the enroll offer now, if
        # the relay is up to carry the answer: it is the only moment the runtime
        # can tell a peer it had been waiting for is reachable, and without it a
        # peer that starts after this machine's relay did is never enrolled at
        # all — legacy's start-only offer reached whoever happened to be
        # connected at that instant and never retried.
        for pid in sorted(arrived):
            self.internet_pairing.offer_enroll(pid)

    def set_device_note(self, device_id, note):
        def run():
            pid = self._resolve(device_id)
            with config_lock:
                peer = self.config.peers.get(pid)
                if peer is None:
                    raise ApplicationError("NOT_FOUND", "Device not found")
                peer.notes = note
                # One name, written to one place.  The alias is the same fact
                # under its other spelling, and a reader that consults only one
                # of the two keeps showing the name the user has just replaced:
                # the relay row carries the alias, the LAN row the note, and
                # `chosen_device_name` reads either.  A device paired on this
                # network and by code at once is renamed from both of its rows,
                # so both spellings have to move together or which name the user
                # sees depends on which row they last touched.
                aliases = getattr(self.config, "netpair_aliases", None)
                if aliases is not None:
                    if note:
                        aliases[pid] = note
                    else:
                        aliases.pop(pid, None)
            self._save_config()
            self._refresh()
            return {"ok": True}
        return self._command(run)

    def connect_device(self, device_id):
        return self._command(self._connect_device, device_id)

    def _connect_device(self, device_id):
        pid = self._resolve(device_id)
        if pid not in {p.device_id for p in self.pairing.get_known_peers()}:
            raise ApplicationError("NOT_FOUND", "Device not found")
        accepted = self._connect(pid)
        if not accepted and not self._stop_event.is_set():
            # The click landed on a peer that is neither advertising nor saved
            # with an address, so there is nowhere to dial — and the route only
            # answers {accepted: false}, which every UI renders as a bare
            # failure.  The reason rides out as an event instead, so the phone
            # can toast the real cause (legacy: connect_unreachable).
            self._publish(
                "device.connection_unreachable",
                {"device_id": pid, "name": self._peer_name(pid)},
            )
        self._refresh()
        return {"accepted": accepted}

    def _published_name(self, pid):
        """The name the **peer** says it goes by, empty when nothing knows it.

        The name the peer publishes about itself comes first: it is the peer's
        own answer, and it is the one that changes when its user renames it —
        every other source here is a copy of what that answer used to be.

        Deliberately *not* the name this user gave it; that is :meth:`_peer_name`.
        This value is the one handed to the transport as a dial's label (see
        ``_chosen_name``), where it is written down as the peer's own name — so a
        local alias must not reach it.
        """
        # Resolved outside the lock: _resolve reads the transport and the
        # pairing manager, and holding _lock across those invites the reverse
        # order from whichever thread walks them the other way round.  A hash
        # nothing has resolved yet is dropped rather than keyed under "", so an
        # unresolved sighting cannot answer for an unnamed peer.  Lapsed
        # sightings count: the name a peer published is the answer this exists
        # for, and it is the one thing that must not go back to being unknown
        # the moment its record stops being announced.
        published = {
            real: info["name"]
            for key, info in self._sightings().items()
            if info.get("named") and (real := self._resolve(key))
        }
        if published.get(pid):
            return published[pid]
        peer = (getattr(self.config, "peers", None) or {}).get(pid)
        name = getattr(peer, "device_name", "") or ""
        if name:
            return name
        try:
            return next(
                (p.device_name for p in self.pairing.get_known_peers() if p.device_id == pid),
                "",
            )
        except Exception:
            logger.debug("Pairing lookup failed while naming %s", pid[:12], exc_info=True)
            return ""

    def _peer_name(self, pid):
        """What this machine calls a peer: the user's name for it, else its own.

        The user's own name comes first.  It is the one the rename dialog wrote
        and the one the device list already shows (its `deviceLabel` is
        ``note or name``), while this function read the published name alone —
        so a device renamed here was called one thing on its row and another in
        every notice, chat header and collected-log filename.  Reported as
        "设备名有的时候会变成'试试'……历史记录里下面显示的名字不对".

        A name this device has no record of is still empty rather than the id:
        every caller already decides for itself what to print when nothing knows
        the peer.
        """
        chosen = chosen_device_name(self.config, pid)
        if chosen:
            return chosen
        return self._published_name(pid)

    def disconnect_device(self, device_id):
        return self._command(self._disconnect_device, device_id)

    def _disconnect_device(self, device_id):
        pid = self._resolve(device_id)
        if pid not in {p.device_id for p in self.pairing.get_known_peers()}:
            raise ApplicationError("NOT_FOUND", "Device not found")
        # Reject reconnection until the user connects again, and abandon any
        # work aimed at the peer instead of letting it run into a timeout.
        self.transport.disconnect_peer(pid, reject=True)
        self.file_transfer.fail_peer_transfers(pid)
        try:
            self.chat.mark_peer_disconnected(pid)
        except Exception:
            logger.debug("Chat disconnect marking failed")
        with self._lock:
            self._connecting.pop(pid, None)
        self._refresh()
        return {"disconnected": True}

    def forget_device(self, device_id):
        return self._command(self._forget_device, device_id)

    def _discard_pairing_for_chat(self, peer_id):
        """An incoming chat invite or update offer cancels the pairing prompt.

        Every unpaired connection auto-generates a shared code, so a peer that
        dials us for something that is not pairing — a chat invite, an update
        offer — would otherwise surface as "wants to pair" as well.  Each of
        those is its own consent flow (the invite banner, the update card), so
        the pending pairing is dropped and the notice de-duplication forgotten —
        the legacy ``_chat_handle_incoming_invite`` did the same before
        answering. The deferred pairing frames are left to :meth:`_tick_locked`,
        which drops them for a peer that is neither pending nor paired.
        """
        if self.pairing.is_peer_paired(peer_id):
            return
        self.pairing.discard_pending_pairing(peer_id)
        with self._lock:
            self._pairing_seen.pop(peer_id, None)
        # The pending request is gone, so a UI still showing the prompt card
        # has to be told to drop it (legacy pushed pairing_resolved here).
        self._publish("pairing.resolved", {"device_id": peer_id, "status": ""})
        self._refresh()

    def _adopt_peer_pairing(self, pid):
        """Take a confirm from a peer this side never showed a request for.

        A pairing can only be started from the machine it is started on: the
        shared code is generated by whichever side opened the link, and only at
        the moment it opens, so a request made on a link that is already up — a
        declined prompt leaves the link standing, and a nearby chat opens one —
        puts the code on one screen alone.  The peer's confirm is then the first
        thing this side hears about the pairing, and reading it as an answer to
        a request that does not exist here is how the machine that asked ended
        up waiting for a confirmation nobody had been asked to give.

        Deriving the code here is what that half of the handshake needs, and it
        is the same code: the derivation is over both fingerprints, and the
        peer's certificate was pinned by the handshake this frame arrived on.  So
        what this raises is ``mark_peer_confirmed``'s own case — the peer
        confirmed first — with the code on both screens for the user to compare.
        """
        if self.pairing.is_peer_paired(pid):
            return
        try:
            code = self.pairing.generate_shared_pairing_code(pid)
        except Exception:
            # No certificate on record for that id: a peer that never handshaked
            # cannot have a code derived for it, so it is not asking.  Nothing is
            # recorded either, which is what keeps the devices page from drawing
            # a confirm row for a device nobody has asked to pair with.
            logger.debug("No pairing to adopt from %s", str(pid)[:12], exc_info=True)
            return
        self.pairing.mark_peer_confirmed(pid)
        # Announced here rather than left to the poll, which announces requests
        # nobody asked for and holds them back while a chat with that peer is
        # live — the very state a pairing started from chat is in.  This request
        # was asked for, so it goes out now, wherever in the window the reader
        # is.  Saying so here means telling the poll as well, or it announces the
        # same request again on its next tick.
        with self._lock:
            self._pairing_seen[pid] = (code, time.monotonic(), True)
        self._publish(
            "pairing.request",
            {
                "device_id": pid,
                "name": self._peer_name(pid),
                "code": code,
                "sas": self._sas_for(pid),
            },
        )

    def _forget_device(self, device_id):
        pid = self._resolve(device_id)
        with config_lock:
            peer = self.config.peers.get(pid)
            if pid not in self.config.removed_peers:
                # Archive whatever is known so the device can be restored even
                # when it was never a saved peer (a discovered-only card).
                name, address, port = self._removal_info(pid)
                self.config.removed_peers[pid] = PeerInfo(
                    device_id=pid,
                    device_name=(peer.device_name if peer else "") or name or pid,
                    public_key_pem=peer.public_key_pem if peer else "",
                    paired=False,
                    notes=peer.notes if peer else "",
                    last_ip=(peer.last_ip if peer else "") or address or "",
                    last_port=(peer.last_port if peer else 0) or port or 0,
                    removed_at=time.time(),
                )
            self.config.peers.pop(pid, None)
            alias = peer_id_hash(pid)
            if alias != pid:
                self.config.peers.pop(alias, None)
        # Tell the peer it is no longer trusted before tearing the link down,
        # otherwise its side keeps auto-reconnecting to a device that forgot it.
        self._send_pairing(pid, "pairing_unpair")
        with self._pairing_ops:
            self.pairing.unpair_peer(pid)
            self.pairing.reject_pairing(pid)
            self.pairing.remove_peer(pid)
        with self._lock:
            self._deferred.pop(pid, None)
            self._connecting.pop(pid, None)
            self._discovered.pop(pid, None)
            self._discovered.pop(peer_id_hash(pid), None)
            # A lapsed sighting is a sighting: removing the device has to take
            # it too, or the row the user just removed comes back from the
            # grace period with nothing behind it.
            self._lost_sightings.pop(pid, None)
            self._lost_sightings.pop(peer_id_hash(pid), None)
            # Removed is terminal: forget the pairing notice de-duplication.
            self._pairing_seen.pop(pid, None)
            # A forgotten device has no pin to replace, and its prompt must not
            # outlive it in the window either.
            self._pending_certs.pop(pid, None)
            self._cert_alert_seen.pop(pid, None)
        self.transport.forget_peer(pid)
        # And this side's own memory of having seen it.  Discovery caches the
        # record a peer announces (see Discovery.forget_peer), so a device that
        # is restored comes back announcing the record it left with — which
        # reads as "nothing changed, nothing to report" — and the sighting that
        # is supposed to put its row back never reaches this runtime at all.
        # Dropping the cache makes the next announcement news again.
        try:
            self.discovery.forget_peer(pid)
        except Exception:
            logger.debug("Could not drop the discovery record for %s", pid)
        # Removed is removed on every route: the code pairing carries no LAN pin,
        # so nothing else in this method would end it, and the peer would go on
        # reaching this machine over the relay while the list called it removed.
        self._drop_internet_pairing(pid)
        try:
            # An unpaired peer must stop burning relay retries, and its base64
            # payloads must not linger on disk in relay_pending.json.
            self.delivery.clear_peer(pid)
        except Exception:
            logger.debug("Relay queue cleanup failed")
        try:
            self.chat.mark_peer_disconnected(pid)
        except Exception:
            logger.debug("Chat disconnect marking failed")
        try:
            self._save_config()
        except Exception:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save device state", retryable=True
            ) from None
        self._refresh()
        return {"forgotten": True}

    def restore_device(self, device_id):
        return self._command(self._restore_device, device_id)

    def _restore_device(self, device_id):
        pid = self._resolve(device_id)
        with config_lock:
            archived = self.config.removed_peers.pop(pid, None)
            if archived is None:
                raise ApplicationError("NOT_FOUND", "Removed device not found")
            if pid in self.config.peers:
                # Already known again — the archive row is a stale duplicate.
                saved = True
            else:
                # Restore as known but unpaired: the forget told the peer to
                # un-pair, so replaying trust would create one-sided consent.
                self.config.peers[pid] = PeerInfo(
                    device_id=pid,
                    device_name=archived.device_name,
                    public_key_pem=archived.public_key_pem,
                    paired=False,
                    notes=archived.notes,
                    last_ip=archived.last_ip,
                    last_port=archived.last_port,
                    removed_at=0.0,
                )
                saved = False
        if not saved:
            self.pairing.restore_peer(pid, archived.device_name, paired=False)
            # forget_peer parked this peer in the transport's reject set;
            # without lifting it a pairing the peer initiates is refused.
            try:
                self.transport.allow_peer(pid)
            except Exception:
                logger.debug("Restore could not lift rejection")
        # A restored device is one this runtime has to be able to *see* again:
        # the peer never stopped announcing, so the announcement it makes next
        # is the one already in discovery's cache and would be read as "no
        # change".  Dropping the cache makes it news (see Discovery.forget_peer),
        # and the scan asks for it now rather than at the next presence round —
        # on a worker, because a scan waits out the settle window and this call
        # is the window's own.
        try:
            self.discovery.forget_peer(pid)
            threading.Thread(
                target=self._background,
                args=(self.discovery.scan,),
                name="clipsync-restore-scan",
                daemon=True,
            ).start()
        except Exception:
            logger.debug("Restore could not refresh discovery for %s", pid)
        try:
            self._save_config()
        except Exception:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save device state", retryable=True
            ) from None
        self._refresh()
        return {"restored": True}

    def purge_device(self, device_id):
        return self._command(self._purge_device, device_id)

    def _purge_device(self, device_id):
        pid = self._resolve(device_id)
        with config_lock:
            removed = self.config.removed_peers.pop(pid, None)
        if removed is None:
            raise ApplicationError("NOT_FOUND", "Removed device not found")
        try:
            self._save_config()
        except Exception:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save device state", retryable=True
            ) from None
        self._refresh()
        return {"purged": True}

    def _removal_info(self, pid):
        """Best known name/address for a device, for archiving on forget."""
        for key, info in self._sightings().items():
            if self._resolve(key) == pid:
                return (info["name"], info["address"], info["port"])
        address = self._address(pid)
        if address:
            return address
        return "", "", 0

    def device_action(self, action, device_id, *args):
        """Apply a phone-panel device action; True when it took effect.

        The panel speaks the legacy action names (``edit_note``, ``pair``,
        ``reject`` …), mapped here rather than changed in the panel.  Like the
        legacy host callback this reports a bool and never raises, because the
        HTTP layer renders only success/failure.
        """
        try:
            if action == "edit_note":
                self.set_device_note(device_id, args[0] if args else "")
            elif action == "pair":
                self.confirm_pairing(device_id, args[0] if args else "")
            elif action == "reject":
                self.reject_pairing(device_id)
            elif action == "unpair":
                self.unpair_device(device_id)
            elif action == "connect":
                self.connect_device(device_id)
            elif action == "disconnect":
                self.disconnect_device(device_id)
            elif action == "forget":
                self.forget_device(device_id)
            elif action == "restore":
                self.restore_device(device_id)
            elif action == "purge":
                self.purge_device(device_id)
            else:
                return False
        except Exception:
            logger.warning("Web device action %s failed", action, exc_info=True)
            return False
        return True

    def certs(self):
        """Pinned certificate fingerprints for every known peer."""
        try:
            peers = self.pairing.get_known_peers()
        except Exception:
            logger.debug("Certificate listing failed", exc_info=True)
            peers = []
        return {
            "devices": [
                {
                    "device_id": getattr(peer, "device_id", ""),
                    "device_name": getattr(peer, "device_name", ""),
                    # add_peer pins the certificate but leaves the short form
                    # unset, so derive it from the pinned PEM when missing.
                    "fingerprint_short": getattr(peer, "fingerprint_short", "")
                    or _short_fingerprint(getattr(peer, "certificate_pem", "")),
                    "fingerprint": getattr(peer, "fingerprint", ""),
                    "paired": bool(getattr(peer, "paired", False)),
                }
                for peer in peers
            ]
        }

    def discovered_peers(self):
        """Live mDNS sightings for the phone panel's Discovered section.

        Discovery keys peers by their hashed id and stores a
        row; the web devices API expects ``{id: {"name", "address", "port"}}``
        exactly like the legacy ``Application._snapshot_discovered_peers``.

        "Discovered" means here now, so this reads the presence grace rather
        than the sighting grace: the section this feeds is a picture of the
        network, and a device that has stopped answering for a minute is not on
        it.  (The device list's own rows are built in ``_refresh``, which needs
        both answers — who is here, and what this machine knows about them.)
        """
        return {
            pid: {"name": info["name"], "address": info["address"], "port": info["port"]}
            for pid, info in self._sightings(self.PRESENCE_GRACE).items()
        }

    def resolved_hashes(self):
        """Hashed-mDNS-id → real device id map (transport bookkeeping)."""
        return self.transport.get_resolved_hashes()

    def reconnect_states(self):
        """Auto-reconnect progress per peer, for the offline device cards."""
        return self.transport.get_reconnect_states()

    def pending_pairings(self):
        """Pending requests as ``(peer_id, code, name, status, sas)`` tuples.

        The SAS is appended the way the legacy host did it, so the phone's
        confirmation card can show the same short code the desktop shows.
        """
        return [
            tuple(entry) + (self._sas_for(entry[0]),)
            for entry in self.pairing.get_pending_pairings()
        ]

    def _sas_for(self, pid):
        """Short authentication string for one peer ("" when it has no pin)."""
        try:
            mine = self.pairing.get_identity().fingerprint
        except Exception:
            logger.debug("Own fingerprint unavailable for SAS", exc_info=True)
            return ""
        fingerprint = self.pairing.get_peer_fingerprint(pid)
        return sas_code(mine, fingerprint) if mine and fingerprint else ""

    def current_relay_broker(self):
        """Endpoint URL of the broker the relay is connected to ("" when off)."""
        if not getattr(self.config, "internet_sync_enabled", False):
            return ""
        relay = self.relay
        if relay is None:
            return ""
        try:
            return relay.current_broker
        except Exception:
            return ""

    def discovery_state(self):
        """Whether mDNS browsing and advertising are currently active."""
        with self._lock:
            discovery = self.discovery
        if discovery is None:
            return {"enabled": False, "visible": False}
        return {
            "enabled": bool(discovery.is_browsing),
            "visible": bool(discovery.is_advertising),
        }

    def set_discovery_enabled(self, enabled):
        """Start or stop mDNS browsing (legacy ``/api/discovery/toggle``)."""
        return self._toggle_discovery(
            enabled,
            lambda: self.discovery.start_browsing(),
            lambda: self.discovery.stop_browsing(),
            "enabled",
            "DISCOVERY_TOGGLE_FAILED",
        )

    def set_discovery_visible(self, enabled):
        """Start or stop advertising this device on the LAN."""
        return self._toggle_discovery(
            enabled,
            lambda: self.discovery.start_advertising(),
            lambda: self.discovery.stop_advertising(),
            "visible",
            "VISIBILITY_TOGGLE_FAILED",
        )

    def scan_devices(self):
        """Ask the LAN who is here, and read the answers back.

        What the window's refresh button calls.  The round is the discovery
        layer's (``Discovery.scan``): it announces this device, asks the
        network, and waits for the answers — so the device list the window reads
        immediately afterwards is the one that was just asked for, rather than
        the one the last background round happened to leave behind.

        ``blocking=True`` because it waits for those answers: ``_pairing_ops``
        is held across a non-blocking command, and holding it while a multicast
        round completes would stall the receive path for as long as the round
        takes.  It is still accounted for, so a stop waits for it.

        Answers with the discovery flags, not with a device list: nothing about
        the flags changes, so there is nothing to publish, and the list arrives
        through the ``devices.changed`` the round's own sightings fire.  A scan
        that could not run (browsing switched off) is then legible to the caller
        instead of looking like a network with no devices on it.
        """
        self._command(self.discovery.scan, blocking=True)
        # And then ask every device this machine can reach what it is.  The mDNS
        # round above answers "who is here"; this answers "what are they
        # running", now, rather than whenever that peer's next broadcast lands —
        # which is the wait a refresh is supposed to end.  Off the caller's
        # thread (see `_ask_for_facts`), so the button still returns at the speed
        # of the round rather than of the slowest peer.
        self._ask_for_facts(self._facts_targets())
        return self.discovery_state()

    def _facts_targets(self):
        """The devices worth asking: the ones a row can show facts about.

        Archived devices are left out — the user took them away — and a device is
        asked over whichever route this machine has to it, decided per send in
        `_ask_for_facts`.  Read from the same sighting/peer tables `_refresh`
        builds rows from, so a device the page lists is a device the page asks.
        """
        with config_lock:
            known = set(self.config.peers)
        with self._lock:
            sighted = {self._resolve(pid) for pid in self._discovered}
        return sorted(
            (known | sighted) - self._archived_ids() - {self.config.device_id}
        )

    def _toggle_discovery(self, enabled, start, stop, key, code):
        # Deliberately outside ``_command``: the toggle only touches mDNS, and
        # holding the pairing lock across a zeroconf call would stall the
        # receive path for no reason.
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            if self._stop_event.is_set():
                raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
            (start if enabled else stop)()
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(
                code, "Could not change the LAN setting", retryable=True
            ) from None
        finally:
            self._leave()
        state = self.discovery_state()
        if state[key] is not enabled:
            raise ApplicationError(
                code, "Could not change the LAN setting", retryable=True
            )
        self._publish("discovery.changed", state)
        return state

    def send_url(self, device_id, url):
        """Send an http(s) URL to one paired peer as a ``nav_url`` frame.

        Deliberately outside ``_command``: the send is fire-and-forget, so
        holding the pairing lock across it would only stall the receive path.
        """
        if not isinstance(url, str) or not 0 < len(url) <= 2048:
            raise ApplicationError("INVALID_ARGUMENT", "Invalid URL")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApplicationError("INVALID_ARGUMENT", "Only http(s) URLs can be sent")
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            if self._stop_event.is_set():
                raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
            pid = self._resolve(device_id)
            reachable = self._peer_is_internet_reachable(pid)
            known = {peer.device_id for peer in self.pairing.get_known_peers()}
            if pid not in known and not reachable:
                raise ApplicationError("NOT_FOUND", "Device not found")
            if not self.pairing.is_peer_paired(pid) and not reachable:
                raise ApplicationError("NOT_PAIRED", "Device is not paired")
            data = encode_frame(
                {"msg_type": "nav_url", "url": url}, source_device=self.config.device_id
            )
            try:
                sent = bool(self.transport.send_to_peer(pid, data))
            except Exception:
                logger.debug("URL send failed", exc_info=True)
                sent = False
            if not sent:
                # The receiving side has read a `nav_url` off the relay since the
                # relay learned to route one (``_on_peer_message`` trusts it for
                # any internet-reachable peer); the send side was the half that
                # never used it, so a URL to an internet-paired device came back
                # as "not sent" while the same device took clipboard fine.
                sent = self._relay_publish_to_peer(data, pid)
            if not sent:
                raise ApplicationError("SEND_FAILED", "URL was not sent", retryable=True)
            self._publish("url.sent", {"device_id": pid, "url": url})
            return {"sent": True, "device_id": pid}
        finally:
            self._leave()

    def push_text(self, text):
        """Write text to the local clipboard and broadcast it to peers.

        Native counterpart of the legacy ``/api/push`` (the phone Companion's
        send-text box): the text lands on this machine's clipboard *and* is
        synced, so a desktop can send one snippet to every device at once.

        The monitor is suppressed for the write so the capture path does not
        re-broadcast the same text as a second message; ``_on_local_sync``
        below performs the single send. The returned ``sent`` reports whether
        sync was on and the frame actually left — the clipboard write already
        succeeded at that point, so a filtered or oversized payload is not an
        error for the caller.
        """
        if not isinstance(text, str) or not text.strip():
            raise ApplicationError("INVALID_ARGUMENT", "Text is required")
        text = text.strip()
        if len(text) > self.MAX_PUSH_CHARS:
            raise ApplicationError("INVALID_ARGUMENT", "Text is too long")
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            if self._stop_event.is_set():
                raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
            content = ClipboardContent(types={ContentType.TEXT: text.encode("utf-8")})
            if self._clipboard.write(content) is not True:
                self._error("CLIPBOARD_WRITE_FAILED")
                raise ApplicationError(
                    "CLIPBOARD_WRITE_FAILED",
                    "Could not write to the clipboard",
                    retryable=True,
                )
            try:
                self.sync._monitor.suppress_for(2.0)
            except Exception:
                logger.debug("Clipboard monitor suppression failed", exc_info=True)
            history = getattr(self.sync, "_history", None)
            if history is not None:
                history.add(content)
                self._publish("history.changed", {})
            sent = False
            if self.sync_state == "running" and self.config.sync_enabled:
                msg = SyncMessage(content, uuid.uuid4().hex, self.config.device_id)
                sent = bool(self._background(self._on_local_sync, msg))
            return {"ok": True, "len": len(text), "sent": sent}
        finally:
            self._leave()

    def _handle_nav_url(self, payload, pid):
        """Open a peer's URL in the default browser (http/https only)."""
        url = payload.get("url", "") if isinstance(payload, dict) else ""
        parsed = urlparse(url) if isinstance(url, str) else None
        # Anything else (file://, custom OS schemes) would let a peer launch
        # local handlers, so the scheme check is not optional.
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
            logger.warning("Ignoring unsafe nav_url from peer")
            return
        self._publish("url.received", {"device_id": pid, "url": url})
        try:
            self._open_url(url)
        except Exception:
            logger.debug("Opening a received URL failed", exc_info=True)

    def test_device(self, device_id):
        """Probe every reachable channel to *device_id* and measure RTT.

        Deliberately not wrapped in ``_command``: that wrapper holds
        ``_pairing_ops`` for the whole callback, and the reply is recorded by
        the receive path which needs the same lock — waiting under it would
        guarantee every probe timed out.
        """
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            if self._stop_event.is_set():
                raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
            pid = self._resolve(device_id)
            # A device paired by code is not in the pairing repository and is
            # still a device this machine can reach: the probe's relay channel
            # exists for exactly this case, and refusing the request before it
            # could answer left the row's 测试连接 button failing on a pairing
            # that works.
            if (
                pid not in {p.device_id for p in self.pairing.get_known_peers()}
                and not self._peer_is_internet_reachable(pid)
            ):
                raise ApplicationError("NOT_FOUND", "Device not found")
            return self._probe_device(pid)
        finally:
            self._leave()

    def _probe_device(self, pid):
        channels = []
        try:
            if pid in set(self.transport.get_connected_peers() or ()):
                channels.append("lan")
        except Exception:
            logger.debug("Device probe: LAN connectivity check failed", exc_info=True)
        try:
            if self._peer_is_internet_reachable(pid):
                channels.append("relay")
        except Exception:
            logger.debug("Device probe: relay reachability check failed", exc_info=True)
        if not channels:
            return {"ok": False, "results": [], "error": "no_channel"}
        ping_id = secrets.token_hex(6)
        entry = {
            "results": {
                channel: {
                    "channel": channel,
                    "ok": False,
                    "send_ts": time.monotonic(),
                    "latency_ms": None,
                    "error": "timeout",
                }
                for channel in channels
            },
            "replied": set(),
            "event": threading.Event(),
        }
        with self._probes_lock:
            self._probes[ping_id] = entry
        frame = encode_frame(
            {"msg_type": "device_ping", "ping_id": ping_id, "ts": time.monotonic()},
            source_device=self.config.device_id,
        )
        unsent = set()
        for channel in channels:
            try:
                if channel == "lan":
                    # send_to_peer returns False on failure; a frame that never
                    # left this machine can never be ponged, so decide it now
                    # instead of burning the full timeout.
                    sent = bool(self.transport.send_to_peer(pid, frame))
                    if not sent:
                        entry["results"][channel]["error"] = "send_failed"
                else:
                    sent = self._relay_publish_to_peer(frame, pid)
                    if not sent:
                        entry["results"][channel]["error"] = "relay_offline"
            except Exception:
                logger.debug("Device probe: %s ping send failed", channel, exc_info=True)
                entry["results"][channel]["error"] = "send_failed"
                sent = False
            if not sent:
                unsent.add(channel)
        if unsent:
            with self._probes_lock:
                entry["replied"].update(unsent)
        deadline = time.monotonic() + self.DEVICE_PING_TIMEOUT
        while time.monotonic() < deadline:
            with self._probes_lock:
                if len(entry["replied"]) >= len(channels):
                    break
            entry["event"].wait(min(deadline - time.monotonic(), 0.2))
        with self._probes_lock:
            self._probes.pop(ping_id, None)
        results = [
            {
                "channel": row["channel"],
                "ok": bool(row["ok"]),
                "latency_ms": row["latency_ms"],
                "error": None if row["ok"] else row["error"],
            }
            for row in entry["results"].values()
        ]
        results.sort(key=lambda row: not row["ok"])
        return {"ok": any(row["ok"] for row in results), "results": results}

    def _peer_is_internet_reachable(self, pid):
        """True when *pid* can be reached over the public relay."""
        # A device this machine has removed is reachable nowhere.  The LAN side
        # says so three times over (a blacklisted peer, a dropped pin, a row that
        # is no longer drawn from the repository); this is the relay's own copy
        # of the rule, and the one that matters for a device whose relay secret
        # was still on disk -- a config written before removal dropped it, or an
        # unpair that failed to save.  Reachability is the whole gate a relay
        # frame passes, so without this a removed device could still hand this
        # machine clipboard content, chat and files.
        if pid in (getattr(self.config, "removed_peers", {}) or {}):
            return False
        if pid in (getattr(self.config, "netpair_secrets", {}) or {}):
            return True
        if pid in (getattr(self.config, "peer_relay_secrets", {}) or {}):
            peer = self.config.peers.get(pid)
            return peer is not None and bool(getattr(peer, "paired", False))
        return False

    def _relay_publish_to_peer(self, frame, peer_id):
        """Publish one frame to a single internet-reachable peer.

        Used by :meth:`_chat_send_fn` as the LAN fallback, so chat frames and
        chat file bytes reach a device across networks — and by the relay
        receipts, which have to travel back the same way.  A netpair entry wins
        when a peer is reachable over both derived channels (one publish, not
        two).
        """
        if self.relay is None or not self.config.internet_sync_enabled or not peer_id:
            return False
        # The same rule the inbound gate applies (``_peer_is_internet_reachable``):
        # a device this machine has removed is reachable nowhere.  Chat and the
        # delivery receipts both leave through here, and a removal has to stop
        # the traffic in both directions and not only in the one that arrives.
        if peer_id in (getattr(self.config, "removed_peers", {}) or {}):
            return False
        from internal.transport.relay import (
            derive_key,
            derive_topic,
            netpair_topic,
        )

        secret = (getattr(self.config, "netpair_secrets", {}) or {}).get(peer_id)
        if isinstance(secret, str) and secret:
            topic = netpair_topic(secret)
            # The key agreement's session key once this peer's public half is
            # known, and the code-derived key before that — a peer that has
            # never sent us a hello has no other way to read us.
            key = self.internet_pairing.netpair_key_for(secret, peer_id)
            if key is None:
                return False
        else:
            peer_secret = (getattr(self.config, "peer_relay_secrets", {}) or {}).get(peer_id)
            peer = self.config.peers.get(peer_id)
            my_secret = getattr(self.config, "relay_secret", "")
            if not peer_secret or peer is None or not bool(getattr(peer, "paired", False)):
                return False
            if not my_secret:
                return False
            topic, key = derive_topic(my_secret, peer_secret), derive_key(my_secret, peer_secret)
        msg = self._decode_frame(frame)
        # File chunks ride at QoS 1 so a best-effort public broker redelivers a
        # dropped packet; the receiver's per-index chunk dedup absorbs the
        # at-least-once duplicates that buys.
        is_chunk = getattr(msg, "msg_type", "") == "file_chunk"
        # A clipboard frame reaches this method when the offline queue retries a
        # publish that failed, and a frame too big for one relay message is the
        # case that queue exists for here -- so the same cutting the first
        # attempt does has to happen on the retry, or the queued row retries its
        # way to a failure it could never avoid.  Chat and receipt frames size
        # themselves and are published in one message, exactly as before.
        outcome = self._relay_publish(
            self.relay,
            frame,
            topic,
            key,
            peer_id,
            qos=1 if is_chunk else 0,
            clipboard=getattr(msg, "msg_type", "") == "clipboard",
        )
        if outcome == "sent":
            self._note_chat_sent(peer_id, msg)
        return outcome == "sent"

    @staticmethod
    def _decode_frame(data):
        """Decode one wire frame; None when it cannot be decoded at all."""
        try:
            return decode_message(data)
        except Exception:
            logger.debug("Frame decode failed", exc_info=True)
            return None

    def _answer_overlay(self, pid, info):
        """A sighting, with a directly-answered account of the peer laid over it.

        The same five facts arrive two ways — in the peer's mDNS record, which it
        broadcasts on its own schedule, and in its answer to a ``device_ping``,
        which is a reply to a question asked seconds ago — so they disagree
        exactly when the peer has upgraded and one of the two is behind.  Which
        one this machine believes is decided by **when it was observed**, not by
        which kind it is: a broadcast read ten minutes ago must not outrank a
        reply from ten seconds ago, and a reply must not outrank a sighting that
        arrived after it.  Both stamps come from this machine's own monotonic
        clock, so the comparison is well defined, and neither source can install
        a stale version over a fresh one.

        Only the facts travel.  The address and port stay the sighting's: an
        answer says what the peer *is*, not where it is, and the address book is
        the discovery layer's to keep.
        """
        answer = self._answers.get(pid)
        if not answer or answer["at"] <= self._sighted_at.get(pid, 0.0):
            return info
        merged = dict(info)
        for field in ("version", "os", "arch", "app"):
            if answer.get(field):
                merged[field] = answer[field]
        # The name only when the peer gave one: a peer that answers without a
        # name has not un-named itself, and the sighting's is then the better
        # answer.
        if answer.get("name"):
            merged["name"] = answer["name"]
            merged["named"] = True
        return merged

    def _ask_for_facts(self, pids):
        """Ask each peer in *pids* what it is, off the caller's thread.

        Fired and forgotten on purpose: the answer arrives as its own frame and
        is recorded by :meth:`_handle_device_probe`, which publishes the device
        list change the row needs.  Waiting here would put the dial's whole
        timeout back into the caller — the mistake `chat_invite` used to make.

        Rate-limited per peer, because the entry point is the refresh button and
        a reader who presses it twice should not put two rounds of frames on the
        network for one answer.
        """
        now = time.monotonic()
        asked = []
        with self._probes_lock:
            for pid in pids:
                if now - self._asked_at.get(pid, 0.0) < self.FACTS_ASK_INTERVAL:
                    continue
                self._asked_at[pid] = now
                asked.append(pid)
        for pid in asked:
            frame = encode_frame(
                {"msg_type": "device_ping", "ping_id": secrets.token_hex(6), "ts": now},
                source_device=self.config.device_id,
            )
            try:
                sent = bool(self.transport.send_to_peer(pid, frame))
            except Exception:
                logger.debug("Facts probe: LAN send failed", exc_info=True)
                sent = False
            if not sent and self._peer_is_internet_reachable(pid):
                try:
                    self._relay_publish_to_peer(frame, pid)
                except Exception:
                    logger.debug("Facts probe: relay send failed", exc_info=True)
        return asked

    def _handle_device_probe(self, kind, payload, pid, via_relay):
        """Answer a ping, or resolve an in-flight probe with a pong."""
        channel = "relay" if via_relay else "lan"
        if kind == "device_ping":
            facts = self.discovery.advertised_facts()
            frame = encode_frame(
                {
                    "msg_type": "device_pong",
                    "ping_id": str(payload.get("ping_id") or ""),
                    "ts": payload.get("ts"),
                    # What this device is, in the same five strings its mDNS
                    # record carries (see `Discovery.advertised_facts`): the
                    # question was "who are you", and a reply that gave only an
                    # echo left the asker to broadcast-and-wait for the rest.
                    "facts": facts,
                },
                source_device=self.config.device_id,
            )
            if via_relay:
                self._relay_publish_to_peer(frame, pid)
            else:
                # The LAN peer may have just dropped; a failed send is a valid
                # negative probe result, not an error.
                self.transport.send_to_peer(pid, frame)
            return
        ping_id = str(payload.get("ping_id") or "")
        if not ping_id:
            return
        self._note_answer(pid, payload.get("facts"), ping_id)
        with self._probes_lock:
            entry = self._probes.get(ping_id)
            if entry is None:
                return
            result = entry["results"].get(channel)
            if result is None:
                return
            result["ok"] = True
            result["latency_ms"] = (time.monotonic() - result["send_ts"]) * 1000.0
            entry["replied"].add(channel)
            entry["event"].set()

    def _note_answer(self, pid, facts, ping_id):
        """Record what a peer said it is, once, and publish the change.

        Written even when no probe of this machine's is waiting for it: the
        answer is good whether it arrived for a latency test, for a refresh, or
        unprompted.  Deduplicated by ``ping_id`` so a retransmitted pong cannot
        restamp an older answer and outrank a newer sighting.
        """
        if not isinstance(facts, dict) or not pid:
            return
        with self._probes_lock:
            if ping_id in self._answered_pings:
                return
            self._answered_pings.add(ping_id)
            # Bounded: the set only has to cover a duplicate of a pong this
            # machine could still be waiting on.
            if len(self._answered_pings) > 512:
                self._answered_pings.pop()
            self._answers[pid] = {
                "version": str(facts.get("version") or ""),
                "os": str(facts.get("os") or ""),
                "arch": str(facts.get("arch") or ""),
                "app": str(facts.get("app") or ""),
                "name": str(facts.get("name") or ""),
                "at": time.monotonic(),
            }
        self._refresh()

    def start_pairing(self, device_id):
        return self._command(self._start_pairing, device_id)

    def _start_pairing(self, device_id):
        pid = self._resolve(device_id)
        accepted = self._connect(pid)
        if accepted and pid in self.transport.get_connected_peers():
            with self._pairing_ops:
                if not self.pairing.is_peer_paired(pid):
                    self.pairing.generate_shared_pairing_code(pid)
        if not accepted and not self._stop_event.is_set():
            # Same reason, same event as _connect_device above: 配对 on a peer
            # with no address is a dial that never leaves this machine, and
            # {accepted: false} renders as nothing at all.  That is the click
            # the user reads as "no reaction": the button is live, the route
            # only says false, and nothing follows it.
            self._publish(
                "device.connection_unreachable",
                {"device_id": pid, "name": self._peer_name(pid)},
            )
        self._refresh()
        return {"accepted": accepted}

    def confirm_pairing(self, device_id, code):
        return self._command(self._confirm_pairing, device_id, code)

    def _confirm_pairing(self, device_id, code):
        pid = self._resolve(device_id)
        with self._pairing_ops:
            # Expire before mark/confirm so a late frame cannot revive trust.
            self.pairing.get_pending_pairings()
            accepted = self.pairing.confirm_pairing(pid, code)
            status = self.pairing.get_pairing_status(pid)
        # The prompt card is settled on this device either way — paired, or
        # waiting for the peer to confirm (legacy pushed the status along).
        self._publish("pairing.resolved", {"device_id": pid, "status": status})
        if accepted and not self._send_pairing(pid, "pairing_confirm"):
            with self._lock:
                self._deferred[pid] = ("pairing_confirm", time.monotonic() + self.PAIRING_SEND_WAIT)
            self._connect(pid)
        self._refresh()
        return {"paired": status == PAIRING_STATUS_PAIRED, "status": status}

    def _send_pairing(self, pid, kind):
        if self._stop_event.is_set():
            return False
        return self.transport.send_to_peer(
            pid, encode_frame({"msg_type": kind}, source_device=self.config.device_id)
        )

    def reject_pairing(self, device_id):
        return self._command(self._end_pairing, device_id, False)

    def unpair_device(self, device_id):
        return self._command(self._end_pairing, device_id, True)

    def _end_pairing(self, device_id, unpair):
        pid = self._resolve(device_id)
        if pid not in {p.device_id for p in self.pairing.get_known_peers()}:
            return {"accepted": False}
        with self._pairing_ops:
            self.pairing.mark_peer_unpaired(pid) if unpair else self.pairing.mark_peer_rejected(pid)
        with self._lock:
            self._deferred.pop(pid, None)
            # Resolved: a later genuine request from this peer must notify again.
            self._pairing_seen.pop(pid, None)
            # An unpair answers any pending certificate prompt too: there is no
            # pin left for "trust again" to replace, and re-pairing is what
            # establishes the new one.
            self._pending_certs.pop(pid, None)
        kind = "pairing_unpair" if unpair else "pairing_reject"
        try:
            notice_sent = self._send_pairing(pid, kind)
        finally:
            if unpair:
                # A deliberate break, and the only one that earns the permanent
                # one: ``forget_peer`` blacklists the peer at the transport
                # level, so nothing it sends is accepted until this machine
                # dials it again.
                self.transport.forget_peer(pid)
                # ...and a deliberate break is a break on both routes.  The
                # dialog behind this button says the device will no longer sync
                # and needs pairing again on both sides; a device that kept its
                # code pairing would go on syncing over the relay, which is the
                # one thing that sentence rules out.
                self._drop_internet_pairing(pid)
                try:
                    # Unpaired stops the peer's relay retries too: nothing may
                    # keep trying to deliver to a device the user broke with.
                    self.delivery.clear_peer(pid)
                except Exception:
                    logger.debug("Relay queue cleanup failed")
            # Declining one prompt is NOT a break with the device, so a plain
            # reject stops here and leaves the link standing.  It used to run
            # ``forget_peer`` as well, and that blacklist is what made a single
            # "no" permanent: every later connection from that peer was refused
            # before any handshake, and the one thing that lifts the blacklist is
            # this machine's own outbound dial -- which a peer-initiated re-pair
            # request can never be.  The next explicit request now just arrives
            # as a fresh prompt, since the dedup was cleared above.
            # A rejected or unpaired peer has no pending request left to answer.
            # Legacy pushed this without a status, so keep it empty.
            self._publish("pairing.resolved", {"device_id": pid, "status": ""})
            self._refresh()
        if not notice_sent:
            # The link was already down, so the peer was never told and would go
            # on believing the pairing is still live.  Hand the notice to the
            # maintenance tick, which retries it for PAIRING_SEND_WAIT and dials
            # the peer itself; the dial here is the first attempt.  For an
            # unpair that dial is also what lifts the blacklist ``forget_peer``
            # just installed, the only thing that can -- a declaration of
            # intent from this machine.  A mere reject installs no blacklist, so
            # the dial only carries the notice.
            with self._lock:
                self._deferred[pid] = (kind, time.monotonic() + self.PAIRING_SEND_WAIT)
            self._connect(pid)
        return {"accepted": True}

    def set_sync_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ApplicationError("INVALID_ARGUMENT", "enabled must be a boolean")
        return self._command(self._set_sync_enabled, enabled)

    def apply_settings(self, updated: dict, special: dict | None = None):
        """Apply settings changed through the sidecar to live LAN engines."""
        if "device_name" in updated:
            self.sync.device_name = self.config.device_name
            self.transport.device_name = self.config.device_name
            # ...and the identity, which is where the name the handshake
            # announces is read from.  The certificate's own copy cannot follow
            # a rename — every paired device pins its fingerprint — so the frame
            # carries this one instead, and a rename that never reaches here is
            # a rename the other side does not hear until both devices restart.
            self.pairing.set_device_name(self.config.device_name)
            # ...and the advertisement, which is the third place our own name
            # is published and the only one that reaches the other devices.
            # Leaving it out is why the settings page had to promise the rename
            # would take effect "after a restart" — and why a peer that had
            # already listed this device never saw the new name at all.
            self.discovery.set_device_name(self.config.device_name)
            self._refresh()
        if "source_tracking_enabled" in updated:
            # ``_monitor``, which is the name SyncManager holds it under and the
            # name the capture path already reads it by.  ``monitor`` is what
            # the legacy host's sync object called it; there is no such
            # attribute here, so this raised AttributeError on every change --
            # swallowed by the settings API into one ERROR line and an
            # "ok" response, leaving the switch to take effect at the next
            # restart and never before.
            self.sync._monitor.set_source_tracking(self.config.source_tracking_enabled)
        if "sync_enabled" in updated:
            # An explicit toggle outranks a pending timed pause (the legacy
            # host's ``_clear_pause_state``): drop the deadline in memory AND
            # on disk, or a restart re-arms a pause the user already ended.
            with config_lock:
                if float(getattr(self.config, "timed_pause_until", 0.0) or 0.0):
                    self.config.timed_pause_until = 0.0
                    try:
                        self._save_config()
                    except Exception:
                        logger.debug(
                            "Could not persist the cleared pause deadline", exc_info=True
                        )
            self.sync.set_enabled(bool(self.config.sync_enabled))
        if "filter_enabled_categories" in updated:
            self.content_filter.enabled_categories = self.config.filter_enabled_categories
        if "chat_open_to_all" in updated:
            # Takes effect on the next invitation or file offer.  A session
            # already live was admitted under the rule in force when it
            # opened, and is deliberately left alone.
            self.chat.set_open_to_all(bool(self.config.chat_open_to_all))
        if "file_open_to_all" in updated:
            # The transfer page's twin of the setting above, and read per
            # request rather than per session, so it takes effect on the next
            # file that arrives.
            self.file_transfer.set_file_open_to_all(bool(self.config.file_open_to_all))
        if "internet_sync_enabled" in updated:
            self._apply_internet_sync_enabled(bool(self.config.internet_sync_enabled))
        self._publish("settings.live_applied", {"fields": list(updated)})

    def apply_encryption(self, encryption) -> None:
        """Re-wire live app-layer encryption after a password/toggle change.

        ``None`` turns encryption off for subsequent frames.  The relay
        re-derives its netpair channel keys from the same password, so pairing
        traffic follows the change immediately instead of keeping the startup
        key state.
        """
        self.transport.set_encryption_manager(encryption)
        if self.relay is not None:
            self.relay.refresh_channels()

    def pause_sync_for(self, minutes):
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ApplicationError("INVALID_ARGUMENT", "minutes must be between 1 and 1440")
        deadline = time.time() + minutes * 60
        with config_lock:
            self.config.timed_pause_until = deadline
            self.config.sync_enabled = False
        self.sync.set_enabled(False)
        self._save_config()
        self._publish("sync.state.changed", {"sync_state": self.sync_state, "until": deadline})
        return {"enabled": False, "until": deadline}

    def resume_sync(self):
        with config_lock:
            self.config.timed_pause_until = 0.0
            self.config.sync_enabled = True
        self.sync.set_enabled(True)
        self._save_config()
        self._publish("sync.state.changed", {"sync_state": self.sync_state})
        return {"enabled": True}

    def _set_sync_enabled(self, enabled):
        with config_lock:
            self.config.sync_enabled = enabled
            self.config.timed_pause_until = 0.0
        self.sync.set_enabled(enabled)
        try:
            self._save_config()
        except Exception:
            raise ApplicationError(
                "SAVE_FAILED", "Could not save sync setting", retryable=True
            ) from None
        self._publish("sync.state.changed", {"sync_state": self.sync_state})
        return {"enabled": enabled}

    def _on_local_sync(self, msg):
        """Broadcast one local message; True only when it actually went out."""
        if self._stop_event.is_set() or not self.config.sync_enabled:
            return False
        content = msg.content
        if self.config.plain_text_only:
            content = strip_rich_formats(content)
        self.content_filter.enabled_categories = self.config.filter_enabled_categories
        if self.content_filter.is_active and self.content_filter.is_sensitive(content):
            content = self.content_filter.filter_content(content)
            self._publish("sync.redacted", {})
        if not has_syncable_types(content):
            return False
        outgoing = SyncMessage(content, msg.msg_id, self.config.device_id)
        data = encode_message(outgoing)
        if not data:
            # `has_syncable_types` is a cheap type-level gate and the encoder is
            # the real one: a clip whose only format is a FILE with no row to
            # name, or a URL that is really a path, passes the first and encodes
            # to nothing.  Broadcasting those bytes would put a zero-length
            # frame on the wire for every peer to trip over.
            return False
        if len(data) > MAX_FRAME_SIZE:
            self._error("CLIPBOARD_TOO_LARGE")
            return False
        if not self._stop_event.is_set() and self.config.sync_enabled:
            undelivered = self._send_local_sync(data)
            if undelivered:
                # The frame did not reach a peer that had no other route to it.
                # Say which limit it was cut for and who is missing the clip, and
                # report the capture as not synced: returning True here is what
                # made an oversized clip read as delivered while the other
                # machine's clipboard never changed.  Which of the pieces the
                # broker refused is not attributed to the limit -- the limit is
                # why the frame had to be cut at all, and the refusal is the
                # relay's own answer.
                self._error(
                    "CLIPBOARD_TOO_LARGE",
                    f"{len(data)}-byte clipboard frame over the relay's "
                    f"{self._relay_frame_limit()}-byte message limit did not reach "
                    f"{', '.join(str(peer)[:12] for peer in undelivered)}: it was cut "
                    f"into chunks that fit and the broker refused some of them",
                )
                return False
            return True
        return False

    def _clip_targets(self) -> set:
        """The peers a local clip could be carried to right now, either route.

        The union of the two halves ``_send_local_sync`` sends through: a peer
        whose certificate is pinned and that is on the local link, and a peer this
        machine holds a relay secret for.  A removed device is in neither — the
        repository drops the first, ``_peer_is_internet_reachable`` refuses the
        second — and the user's per-device pause is applied to both, the same way
        the two sending halves apply it.

        It exists to answer one question: a copy the manager has already seen is
        being copied again, and sending it again is only worth doing if somebody
        can receive it now.  Deliberately conservative — a peer counted here that
        a sending half would then skip costs one frame, and the receiver drops a
        frame whose content it has already applied.
        """
        targets = set()
        try:
            connected = list(self.transport.get_connected_peers() or ())
        except Exception:
            logger.debug("Clip targets: connected peers unavailable", exc_info=True)
            connected = []
        for pid in connected:
            try:
                if self.pairing.is_peer_paired(pid) and self.syncs_to(pid):
                    targets.add(pid)
            except Exception:
                logger.debug("Clip targets: %s unreadable", str(pid)[:12], exc_info=True)
        relay_ids = set(getattr(self.config, "netpair_secrets", {}) or ()) | set(
            getattr(self.config, "peer_relay_secrets", {}) or ()
        )
        for pid in relay_ids:
            try:
                if self._peer_is_internet_reachable(pid) and self.syncs_to(pid):
                    targets.add(pid)
            except Exception:
                logger.debug("Clip targets: %s unreadable", str(pid)[:12], exc_info=True)
        return targets

    def _send_local_sync(self, data: bytes) -> list[str]:
        """Hand one clipboard frame to each paired peer: LAN first, relay after.

        Returns the peers the relay could not carry the frame to, so the caller
        can report a send that did not happen instead of a success.  An empty
        list means every peer that was in scope has the frame (or nothing had to
        be sent).

        ``transport.broadcast`` answers only whether *some* peer took the frame,
        which is not enough to decide who still needs the relay copy — a peer
        counted as served by another peer's success would be handed nothing.  A
        per-peer send makes the choice answerable per peer, and the relay then
        carries the frame only to the ones whose LAN send actually failed, which
        is the fallback chat has always used (``_chat_send_fn``).

        What this stops is the mirror running between two devices that are
        already on the same network: every clip left this machine for whatever
        public broker the peer was enrolled on, whether or not it was sitting on
        the same desk.  What it costs is the redundant copy that used to cover a
        link dying between the connected-set read and the send — the relay
        ledger and its offline queue still cover a publish that *fails*, but not
        a LAN write that succeeded and then went nowhere.  ``send_to_peer``
        reports what it can; the rest is the price of the content staying local.

        The pairing filter is ``broadcast``'s own: clipboard content never
        reaches an unpaired peer on either channel.  The second filter is the
        user's: a device on ``config.sync_paused_peers`` is skipped here *and* in
        ``_publish_relay``, so turning a device off means off on both routes and
        not "off on the local link, still on the broker".
        """
        lan_delivered = set()
        try:
            connected = list(self.transport.get_connected_peers() or ())
        except Exception:
            logger.debug("Local sync: connected peers unavailable", exc_info=True)
            connected = []
        for pid in connected:
            try:
                if not self.pairing.is_peer_paired(pid):
                    continue
                if not self.syncs_to(pid):
                    continue
                if self.transport.send_to_peer(pid, data):
                    lan_delivered.add(pid)
            except Exception:
                logger.debug("Local sync: LAN send to %s failed", str(pid)[:12], exc_info=True)
        return self._publish_relay(data, lan_delivered=lan_delivered)

    def syncs_to(self, peer_id) -> bool:
        """Whether the user lets this machine send clipboard content to *peer_id*.

        Absent from ``sync_paused_peers`` means yes, which is what keeps a pairing
        made before the setting existed working, and what makes a newly paired
        device sync without a second step.

        Matching is by the id as given *and* by its hashed mDNS form, because the
        two are the same device under different names and the caller may hold
        either: a device paused while it was seen as a hash must stay paused when
        a session resolves it to its real id, or the switch silently comes back on.
        """
        paused = getattr(self.config, "sync_paused_peers", None) or ()
        if not paused:
            return True
        peers = set(paused)
        real = self._resolve(peer_id)
        return peer_id not in peers and real not in peers and peer_id_hash(real) not in peers

    def _publish_relay(self, data: bytes, lan_delivered=()) -> list[str]:
        """Mirror a clipboard frame to every internet-reachable peer.

        Returns the peers the relay could not carry the frame to (empty when
        every peer in scope has it, or when there is nothing to mirror to).

        *lan_delivered* names the peers the local link already carried it to;
        they are skipped, so the broker is handed a clip only when this machine
        could not put it on the wire itself.

        Delivery metadata is only tracked for clipboard frames (chat and file
        frames keep their best-effort mirror with no ledger): a successful
        publish is recorded as sent and awaits the peer's ``relay_ack``, a
        failed one is persisted to the offline queue for retransmission.

        A device that is BOTH a LAN-paired relay-enroll peer and a
        pairing-code peer can be reached over two derived topics; it is
        published to exactly one of them (the netpair channel wins), so the
        broker and the receiver are never handed the same frame twice.
        """
        relay = self.relay
        if relay is None or not self.config.internet_sync_enabled:
            return []
        from internal.transport.relay import (
            derive_key,
            derive_topic,
            netpair_topic,
        )

        delivery = self._delivery_metadata(data)
        undelivered: list[str] = []
        # This machine's own secret, generated on first use and shared by every
        # enrolled peer below: the derivation is symmetric — both ends compute
        # the same topic from the same pair of secrets — so there is one per
        # machine, never one per peer.
        secret = ""
        netpair_secrets = getattr(self.config, "netpair_secrets", {}) or {}
        # A device this machine has removed is sent nothing, on either derived
        # channel, whatever its secret still says on disk.  The LAN-derived half
        # is already covered -- a removed peer is no longer in ``peers``, and the
        # loop below requires it there -- but the pairing-code half reads the
        # secret table directly, and removal is the one act that has to stop
        # this machine's clipboard from being mirrored to a device the user
        # took away.
        removed = set(getattr(self.config, "removed_peers", {}) or {})
        for peer_id, peer_secret in (self.config.peer_relay_secrets or {}).items():
            if not peer_secret or peer_id in netpair_secrets or peer_id in removed:
                continue
            if peer_id in lan_delivered:
                continue
            if not self.syncs_to(peer_id):
                continue
            peer = self.config.peers.get(peer_id)
            if peer is None or not getattr(peer, "paired", False):
                continue
            if not secret:
                secret = self.internet_pairing.ensure_relay_secret()
            outcome = self._relay_publish(relay, data, derive_topic(secret, peer_secret),
                                          derive_key(secret, peer_secret), peer_id)
            self._record_relay_send(peer_id, outcome == "sent", delivery, data)
            if outcome == "undeliverable":
                undelivered.append(str(peer_id))
        for peer_id, peer_secret in netpair_secrets.items():
            if not peer_secret or peer_id == self.config.device_id or peer_id in removed:
                continue  # a stray self-entry must never mirror to ourselves
            if peer_id in lan_delivered:
                continue
            if not self.syncs_to(peer_id):
                continue
            # Same key the peer's own channel will be read under: the session
            # key when the handshake has happened, the code-derived one until
            # then (see ``netpair_key_for``).
            key = self.internet_pairing.netpair_key_for(peer_secret, peer_id)
            if key is None:
                continue
            outcome = self._relay_publish(relay, data, netpair_topic(peer_secret), key, peer_id)
            self._record_relay_send(peer_id, outcome == "sent", delivery, data)
            if outcome == "undeliverable":
                undelivered.append(str(peer_id))
        return undelivered

    def _relay_publish(self, relay, data: bytes, topic, key, peer_id: str = "",
                       *, qos: int = 0, clipboard: bool = True) -> str:
        """Hand one clipboard frame to the broker, cut to fit when it is too big.

        Returns one of:

        ``"sent"``
            the broker took every frame this message needed.
        ``"queued"``
            the frame fits one relay message and the broker refused it just now
            (offline, or its queue full).  The offline queue retries it, exactly
            as it always has, so this is a deferral rather than a drop.
        ``"undeliverable"``
            the frame is larger than one relay message and was still not carried
            after being cut into chunks that fit.  Nothing retries a partly
            carried fragmented frame into a whole one, so the caller must not
            report it as sent.

        The size decision is made against the live relay's own limit
        (:meth:`_relay_frame_limit`) rather than by attempting the publish and
        seeing it fail, so a frame that fits still goes out as exactly one frame
        on the first try -- the common path on a healthy LAN is untouched, and a
        broker that is merely away is not mistaken for an oversized frame.
        """
        if len(data) > self._relay_frame_limit() and clipboard:
            return self._relay_publish_chunked(relay, data, topic, key, peer_id)
        # A frame that fits -- and every frame kind that sizes itself (chat cuts
        # its own file chunks) -- goes out as one message on the first try, the
        # way it always has.
        return (
            "sent"
            if self._relay_publish_one(relay, data, topic, key, qos=qos, peer=peer_id)
            else "queued"
        )

    def _relay_publish_chunked(self, relay, data: bytes, topic, key, peer_id: str) -> str:
        """Carry a frame too big for one relay message as binary chunks.

        The return values are the caller's: ``"sent"`` only when every chunk was
        taken by the broker, ``"undeliverable"`` otherwise (see
        :meth:`_relay_publish`).
        """
        try:
            # The same arithmetic an internet file transfer is cut with: the
            # largest raw chunk whose binary frame still packs under this
            # relay's limit (``ChatManager.relay_chunk_for``).
            chunk_size = ChatManager.relay_chunk_for(self.config.relay_max_message_bytes)
            # The chunk frame is a ``BINARY_HEADER_SIZE``-byte (46) binary
            # header plus the payload, and `relay_chunk_for` is derived from the
            # *configured* limit while the check above used the live relay's.
            # Those are the same number in practice (the transport is built from
            # the setting); shrinking to the live one here is what keeps a drift
            # between them a smaller chunk rather than a piece the broker
            # refuses.
            chunk_size = min(chunk_size, max(1, self._relay_frame_limit() - BINARY_HEADER_SIZE))
            transfer_id = mint_clip_transfer_id()
            frames = split_frame(data, chunk_size, transfer_id)
        except Exception:
            logger.exception("Could not cut an oversized clipboard frame for the relay")
            return "undeliverable"
        # Kept so this peer's resend request has something to answer with.  The
        # registry is bounded and times out; a frame nobody asks about is
        # forgotten on the maintenance tick.
        self._clip_sends.remember(transfer_id, peer_id, frames)
        carried = True
        for frame in frames:
            # QoS 1, like every other chunk frame: a best-effort public broker
            # redelivers what it drops in flight, and the receiver dedups by
            # index.  Every chunk is offered even after a refusal, because a
            # refused one may be the queue filling rather than the link dying,
            # and the receiver's own resend request repairs the gap.
            if not self._relay_publish_one(relay, frame, topic, key, qos=1, peer=peer_id):
                carried = False
        logger.info(
            "Clipboard frame of %d bytes crossed the relay as %d chunk(s) of %d bytes%s",
            len(data),
            len(frames),
            chunk_size,
            "" if carried else " (some chunks were refused)",
        )
        return "sent" if carried else "undeliverable"

    def _relay_publish_one(self, relay, frame: bytes, topic, key, *, qos: int = 0,
                           peer: str = "") -> bool:
        """One publish attempt; its own failures are never propagated."""
        try:
            return bool(relay.publish(frame, topic, key, qos=qos))
        except Exception:
            logger.debug(
                "Relay publish%s failed",
                f" to {str(peer)[:12]}" if peer else "",
                exc_info=True,
            )
            return False

    def _relay_frame_limit(self) -> int:
        """The largest frame one relay message of this configuration carries.

        Read off the live relay when it can say (``RelayTransport.max_frame``),
        so the number that decides whether to cut a frame is the number the
        publish path refuses against.  The configuration is the fallback for a
        transport that cannot be asked -- a test double, or one built by hand.
        """
        limit = getattr(self.relay, "max_frame", None)
        if isinstance(limit, int) and limit > 0:
            return limit
        payload = getattr(self.config, "relay_max_message_bytes", MAX_RELAY_PAYLOAD)
        return frame_limit_for(int(payload or MAX_RELAY_PAYLOAD))

    def _sweep_clip_chunks(self) -> None:
        """Ask for the gaps in a fragmented clipboard frame, and forget the rest.

        The receiving half of the retransmit file transfers already use
        (``file_chunk_ack`` with ``missing_chunks``): a burst of chunks over a
        best-effort broker loses some, the sender is asked again for exactly
        those, and an assembly nobody finishes times out instead of being held
        for the life of the process.
        """
        for transfer_id, peer_id, missing in self._clip_chunks.claim_retransmit(
            self.CLIP_CHUNK_STALL_GRACE
        ):
            if not self._peer_is_internet_reachable(peer_id):
                continue
            logger.info(
                "Clipboard frame %s: %d chunk(s) missing -- asking %s to resend",
                transfer_id[:8],
                len(missing),
                str(peer_id)[:12],
            )
            self._relay_publish_to_peer(
                encode_frame(
                    {
                        "msg_type": "file_chunk_ack",
                        "transfer_id": transfer_id,
                        "missing_chunks": missing,
                    },
                    # The source has to travel.  A relay frame with no source is
                    # read for the binary chunk type alone -- there is nowhere in
                    # that 46-byte header to put a device id (see
                    # ``_receive_relay``) -- so a source-less control frame is
                    # dropped at the far end, and a request sent that way is a
                    # request never heard.
                    source_device=self.config.device_id,
                ),
                peer_id,
            )
        self._clip_chunks.sweep()
        self._clip_sends.sweep()

    def _receive_clip_chunk(self, payload: dict, pid: str, via_relay: bool) -> None:
        """One chunk of a fragmented clipboard frame: assemble it, then route it.

        The rebuilt bytes go back through the very router the single frame would
        have taken, so the far side sees exactly the clipboard message it would
        have seen without the fragmentation -- same channel binding, same trust
        gate, same dedup, same receipt back to the sender.  Anything else would
        make an oversized clip behave differently from an ordinary one on the
        receiving machine, which is the whole thing the framing exists to hide.
        """
        frame = self._clip_chunks.add(
            payload.get("transfer_id"),
            payload.get("chunk_index"),
            payload.get("total_chunks"),
            payload.get("_raw_data"),
            pid,
        )
        if frame is None:
            return
        logger.info(
            "Clipboard frame %s reassembled from chunks (%d bytes) -- routing it",
            str(payload.get("transfer_id"))[:8],
            len(frame),
        )
        if via_relay:
            self._receive_relay(frame)
            return
        rebuilt = decode_message(frame)
        if rebuilt is not None:
            self._receive(rebuilt, pid)

    def _resend_clip_chunks(self, payload: dict, pid: str) -> None:
        """Answer a peer's request for the chunks it did not get.

        The whole chunk set is held from the moment it was cut, and the request
        names only the indices that are missing, so the repair costs the gap and
        not the frame.
        """
        transfer_id = str(payload.get("transfer_id") or "")
        frames = self._clip_sends.resend(transfer_id, pid, payload.get("missing_chunks"))
        if not frames:
            return
        logger.info(
            "Clipboard frame %s: resending %d requested chunk(s) to %s",
            transfer_id[:8],
            len(frames),
            str(pid)[:12],
        )
        for frame in frames:
            self._relay_publish_to_peer(frame, pid)

    def _record_relay_send(self, peer_id, ok, delivery, data) -> None:
        """Ledger/queue one relayed clipboard send (legacy ``delivery`` dict)."""
        if delivery is None:
            return
        if ok:
            self.delivery.note_sent(
                peer_id, delivery["msg_id"], delivery["content_hash"], delivery["preview"]
            )
        else:
            self.delivery.enqueue(
                peer_id, delivery["msg_id"], delivery["content_hash"],
                delivery["preview"], data,
            )

    @staticmethod
    def _delivery_metadata(data: bytes):
        """``{msg_id, content_hash, preview}`` for a clipboard frame, else None.

        Chat and file frames are deliberately excluded: they keep their
        best-effort mirror with no ledger, exactly like the legacy host.
        """
        try:
            msg = decode_message(data)
        except Exception:
            logger.debug("delivery metadata decode failed", exc_info=True)
            return None
        if msg is None or getattr(msg, "msg_type", "clipboard") != "clipboard":
            return None
        content = getattr(msg, "content", None)
        return {
            "msg_id": getattr(msg, "msg_id", "") or "",
            "content_hash": content.hash_key() if content is not None else "",
            "preview": LanRuntime._delivery_preview(content),
        }

    @staticmethod
    def _delivery_preview(content) -> str:
        """First 40 chars of a clipboard message's text (for the send list).

        The two payloads with no text of their own are named by what they carry
        rather than left blank: a send ledger line reading 已送达 with nothing
        after it is the one row a reader cannot place, and a URL and a file are
        exactly the clips whose content the summary *is*.
        """
        try:
            types = getattr(content, "types", {}) or {}
            # A file copy on macOS carries a `public.url` beside its paths, and
            # the Finder also puts the file's *name* on the pasteboard as plain
            # text — so the file branches are asked first.  The name of the thing
            # beats a URI that is only a spelling of its path, and beats the same
            # name read back as text: what was copied is the file.  Same order,
            # and the same line, as the history row this clip becomes (see
            # `history_db._build_preview`), so the ledger and the row a reader
            # finds afterwards agree about what was sent.
            raw = types.get(ContentType.FILE)
            if raw:
                # The *display* reading, not `decode_paths`: this line is shown,
                # and a name that is not valid UTF-8 has to be read as the name
                # it is — surrogates are not characters, and a lone one reaching
                # the window is the same replacement character this is meant to
                # avoid.  Spelled exactly as `history_db._build_preview` spells
                # it, which is what the promise above comes to.
                paths = format.split_paths(format.decode_text(raw))
                if paths:
                    first = os.path.basename(paths[0]) or paths[0]
                    return first[:40] if len(paths) == 1 else f"{first[:40]} 等 {len(paths)} 个文件"
            offer = file_ref.parse(types.get(ContentType.FILE_REMOTE) or b"")
            if offer:
                return file_summary(offer["files"], offer["total"])[:40]
            for content_type in (ContentType.TEXT, ContentType.HTML, ContentType.RTF):
                raw = types.get(content_type)
                if not raw:
                    continue
                # `decode_text`, not a UTF-8 decode with replacement: this is the
                # label for a clip on its way to another device, and a zh_CN
                # Mac's GBK text would be listed here as U+FFFD — the same loss
                # the clipboard writer used to inflict, shown in the send ledger
                # instead of on the receiving clipboard.
                text = format.decode_text(raw).strip()
                if text:
                    return text[:40]
            raw = types.get(ContentType.URL)
            if raw:
                text = format.decode_text(raw).strip()
                if text:
                    return text[:40]
        except Exception:
            pass
        return ""

    def _receive(self, msg, peer_id, via_relay=False):
        with self._pairing_ops:
            self._on_peer_message(msg, peer_id, via_relay)

    def _on_peer_message(self, msg, peer_id, via_relay=False):
        """Route one decoded frame from either transport.

        ``via_relay`` marks a frame that arrived through the public relay: its
        sender is bound by the channel's shared secret rather than by a pinned
        LAN certificate, so a different gate decides whether to trust it.
        """
        if not peer_id or peer_id.startswith("__anon__"):
            return
        pid = self._resolve(peer_id)
        if via_relay:
            # Internet-paired peers are deliberately absent from the LAN
            # pairing repository, so reachability is the gate here.
            if not self._peer_is_internet_reachable(pid):
                return
        elif pid not in {p.device_id for p in self.pairing.get_known_peers()}:
            return
        # A frame from a peer is an active signal on either transport: a peer
        # paired on both must not wait for the other path to flush its queue.
        self.delivery.retry_peer(pid)
        kind = getattr(msg, "msg_type", "clipboard")
        trusted = (
            self._peer_is_internet_reachable(pid) if via_relay
            else self.pairing.is_peer_paired(pid)
        )
        if kind == "relay_ack":
            # A receipt for one of this machine's own sends, arriving on the
            # LAN.  The receiver emits one for every frame it accepted here (see
            # ``_receive``) and reading it only off the relay left rows the LAN
            # had already delivered unsettled: the relay copy was deduped at the
            # far end, a deduped frame earns no ack of any kind, and the ack
            # window then failed content the peer was holding -- the row read
            # 未送达 for a clip already in the other machine's history.  The
            # link has established who is speaking and the receipt is keyed by
            # msg_id, so this one is worth what the relay's is.
            ack_id = getattr(msg, "_raw_payload", {}).get("msg_id", "")
            if trusted and isinstance(ack_id, str) and ack_id:
                self.delivery.note_ack(pid, ack_id)
            return
        if kind.startswith("aiconfig_"):
            self.ai_config.handle_message(kind, getattr(msg, "_raw_payload", {}), pid)
            return
        if kind in ("device_ping", "device_pong"):
            if trusted:
                self._handle_device_probe(kind, getattr(msg, "_raw_payload", {}), pid, via_relay)
            return
        if kind == "nav_url":
            if trusted:
                self._handle_nav_url(getattr(msg, "_raw_payload", {}), pid)
            return
        if kind == "update_request":
            # Answered for a paired peer as before, and -- M2's 免配对 half --
            # for an unpaired one on this build's own platform.  The request is
            # what a peer sends back after an `update_offer` from this machine's
            # device list, so the person who asked for it is the one who started
            # the exchange.  Nothing about the asset's *bytes* is relaxed: the
            # receiving side still checks them against the published digest.
            payload = getattr(msg, "_raw_payload", {}) or {}
            if trusted or self._same_platform_peer(payload):
                self._serve_cached_update(pid, str(payload.get("version") or ""))
            return
        if kind == "update_offer":
            # LAN only: the offer names the peer's version and offers a transfer,
            # and a claim made on the public relay is a claim from nobody in
            # particular.  Both are things the pinned LAN link establishes.
            if not via_relay:
                self._on_update_offer(pid, getattr(msg, "_raw_payload", {}) or {})
            return
        if kind == "update_unavailable":
            # The answer to an ``update_request``, or a peer refusing an offer
            # this machine sent.  Either way it is read only from a device this
            # machine asked something of, and that is the ledger rather than the
            # payload's claim about its own platform -- a refusal for
            # ``other_platform`` names a platform that differs from ours by
            # definition, so a same-platform test would drop the one answer the
            # offering side is most likely to be waiting for.
            payload = getattr(msg, "_raw_payload", {}) or {}
            if (
                trusted
                or self._update_outstanding_for(pid)
                or self._offer_answer_pending_for(pid)
            ):
                self._on_update_unavailable(pid, payload)
            return
        if kind == "update_verdict":
            # The answer to a file this machine served.  Read for the same reason
            # `update_unavailable` is: it is only meaningful from a device this machine asked
            # something of, and the ledger is what establishes that, not the payload's claim.
            payload = getattr(msg, "_raw_payload", {}) or {}
            if trusted or self._update_outstanding_for(pid) or self._auto_update_served.get(pid):
                self._on_update_verdict(pid, payload)
            return
        if kind == "log_request":
            # A peer asking for this machine's log.  Answered from the setting
            # rather than from anything about the asker: sharing is either on
            # for the network or off, and the answer is always a redacted copy
            # (see ``_on_log_request``).
            if not via_relay:
                self._on_log_request(pid, getattr(msg, "_raw_payload", {}) or {})
            return
        if kind == "log_denied":
            # The answer to a request of ours, so it is read on the same terms
            # the request was made under, and its only effect is to stop this
            # side waiting.
            if trusted or self._log_outstanding_for(pid):
                self._on_log_denied(pid, getattr(msg, "_raw_payload", {}) or {})
            return
        if kind in CLIP_FILE_MSG_TYPES:
            # Answered over either route now.  This used to be LAN-only for two
            # reasons and only one of them still holds:
            #
            #   * "a request that arrived over the relay could only be answered
            #     with a transfer that never completes" -- that was true of 256 KiB
            #     frames, and it is why the answer is now cut to fit a broker
            #     message and chunk-acked at the far end.  The receiver's own
            #     replies travel the same way (see the file family's handler).
            #   * "answering it anyway would mean reading a peer's files on the
            #     strength of a claim made on the public relay" -- the claim is
            #     not the frame's.  A relay frame's source is bound to the channel
            #     it arrived on (the bind above), and a channel's topic and key
            #     come from the pairing secret, so a broker can neither forge a
            #     request nor replay one as a device it does not hold a secret
            #     for.  That is the same trust a LAN peer's pinned certificate
            #     carries, reached by a different mechanism -- which is why the
            #     rest of the file family, clipboard content and chat already
            #     cross this way.  What it is *not* is weaker consent: the reply
            #     still only serves the entry this side was asked for, through
            #     `_clip_file_source`.
            if not trusted:
                return
            if kind == "clip_file_request":
                self._serve_clip_file(pid, getattr(msg, "_raw_payload", {}))
            else:
                self._on_clip_file_denied(pid, getattr(msg, "_raw_payload", {}))
            return
        if kind in PAIRING_MSG_TYPES:
            if via_relay:
                # Pairing belongs to the LAN handshake alone: those frames ride
                # a TLS connection whose certificate is pinned, and no send path
                # falls back to the relay, so this side never emits one there.
                # A pairing frame off the relay is therefore always forged —
                # drop it before it can flip trust state.
                logger.warning("Dropping %s frame received over the relay", kind)
                return
            with self._pairing_ops:
                pending = {p[0] for p in self.pairing.get_pending_pairings()}
                if kind == "pairing_confirm":
                    if pid in pending:
                        self.pairing.mark_peer_confirmed(pid)
                    elif pid in self.transport.get_connected_peers():
                        # Nothing of ours to answer, but a live peer that says
                        # it confirmed: this is that peer asking, and the request
                        # it is answering is the one it raised itself.  A confirm
                        # arriving any other way — no link, no pinned certificate,
                        # no request — stays the no-op it was.
                        self._adopt_peer_pairing(pid)
                else:
                    self.pairing.mark_peer_rejected(
                        pid
                    ) if kind == "pairing_reject" else self.pairing.mark_peer_unpaired(pid)
                    with self._lock:
                        self._deferred.pop(pid, None)
            if kind == "pairing_unpair":
                # Only a declared break blacklists.  ``pairing_reject`` is the
                # peer turning down one prompt, and putting this side's
                # transport blacklist up for it is what made a single "no" stick
                # from both ends at once: our own dial is the only lift, and the
                # peer's next request arrives inbound, so the prompt could never
                # come back.  Nothing here auto-dials an unpaired peer
                # (``_peer_found`` dials paired peers only), so leaving the link
                # unblacklisted costs no repeated prompting.
                self.transport.forget_peer(pid)
                # The peer declared the break, and it ends the same trust this
                # side ends with its own 撤销信任 — the code pairing included,
                # which the peer's side of this frame has just dropped too.
                # Leaving ours standing would keep publishing into a topic
                # nothing listens on, and keep listing it as paired here.
                self._drop_internet_pairing(pid)
            # A peer's own confirm/reject settles the prompt card on this side
            # too, without waiting for the next poll (legacy pairing_resolved).
            self._publish(
                "pairing.resolved",
                {"device_id": pid, "status": self.pairing.get_pairing_status(pid)},
            )
            self._refresh()
            return
        if kind == "relay_enroll":
            # The peer's own relay secret, offered over the LAN link this frame
            # arrived on — the transport has already established that this *is*
            # that peer and that it is paired, and the port that listens on the
            # channel the secret derives is not one to open for a claim made off
            # the public relay.  A legacy host never publishes one there either
            # (its enroll send has no relay fallback), so refusing them changes
            # nothing a real peer does.
            if via_relay:
                logger.warning("Dropping relay_enroll frame received over the relay")
                return
            if self.internet_pairing.handle_enroll(
                pid, getattr(msg, "_raw_payload", {})
            ):
                self.internet_pairing.offer_enroll(pid)
            return
        if kind == "file_chunk":
            payload = getattr(msg, "_raw_payload", {}) or {}
            if is_clip_transfer_id(payload.get("transfer_id")):
                # A clipboard frame that was too big for one relay message,
                # arriving as the binary chunks it was cut into.  Clipboard
                # content never reaches an unpaired peer on either channel, so
                # these pieces are held to the same gate the whole frame is --
                # and a chunk the trust check refuses is dropped here rather
                # than handed to the chat or file layer under a name of its own.
                if trusted:
                    self._receive_clip_chunk(payload, pid, via_relay)
                else:
                    logger.debug(
                        "Dropping clipboard chunk for %s from an untrusted peer",
                        str(payload.get("transfer_id"))[:8],
                    )
                return
        if kind == "file_chunk_ack":
            payload = getattr(msg, "_raw_payload", {}) or {}
            if is_clip_transfer_id(payload.get("transfer_id")):
                # The far side is missing chunks of a fragmented clipboard
                # frame this machine cut.  Same message, same meaning, as the
                # file transfer's own retransmit request.
                if trusted:
                    self._resend_clip_chunks(payload, pid)
                return
        if kind == "file_chunk" and self.chat.handle_binary_chunk(
            getattr(msg, "_raw_payload", {}), pid, self._chat_send_fn(pid)
        ):
            return
        if kind.startswith("file_") or kind.startswith("speed_test"):
            # An unpaired peer is admitted here for everything the file family
            # carries, and the manager is what settles each kind: an update or a
            # log blob against a ledger this machine armed by asking, and a
            # `clip_file` against the download that asked for it.  A **plain**
            # file from an unpaired sender is not refused by pairing -- pairing
            # is deliberately not part of that question (see
            # `set_file_open_to_all`) -- it is settled by the `file_open_to_all`
            # setting: on (the default), taken on arrival into the receive
            # directory; off, the accept prompt.  This comment used to say the
            # manager "refuses a plain file from an unpaired sender before it can
            # raise the accept prompt", which is the opposite of what it does and
            # would have led a reader to believe pairing was the gate.
            #
            # Speed tests still need the pairing: there is nothing on the other
            # side of one but this machine's bandwidth.
            #
            # Relayed frames are handled now, which is what makes a file copied
            # on an internet-paired device downloadable here.  They used to be
            # dropped, with the note that the receiver's own send closure was
            # LAN-only so a relayed transfer "could never answer anyway" -- the
            # same 256 KiB reasoning the sender no longer holds to.  The closure
            # below is the chat one, which is LAN-first with a relay fallback, so
            # every reply this manager sends (a chunk ack, a retransmit request,
            # the completion handshake) can reach a peer that has no LAN link.
            allowed = trusted or kind in UNPAIRED_FILE_MSG_TYPES
            if allowed:
                self.file_transfer.handle_message(
                    kind,
                    getattr(msg, "_raw_payload", {}),
                    # The route-aware closure, not the LAN-only one: a transfer that arrived over
                    # the relay has to be able to answer over it.  `via_relay` is deliberately not
                    # part of the choice -- an internet-reachable peer is chunk-acked and
                    # relay-sized even while its LAN link is up, so a link that drops mid-transfer
                    # does not fail the half that already arrived.
                    self._chat_send_fn(pid),
                    pid,
                )
            return
        if kind.startswith("chat_"):
            if kind == "chat_invite":
                self._discard_pairing_for_chat(pid)
            accepted = self.chat.handle_message(
                kind, getattr(msg, "_raw_payload", {}), pid,
                self.chat.shorten_fingerprint(self.pairing.get_peer_fingerprint(pid)),
                self._chat_send_fn(pid),
            )
            # A chat frame the chat layer processed earns a relay receipt, so
            # the internet sender can mark its message delivered.
            if accepted:
                self._maybe_send_relay_ack(msg, pid)
            return
        # No fallthrough to SyncManager for chat/file/relay/control protocols.
        if kind != "clipboard" or not trusted:
            return
        content = deepcopy(msg.content)
        if self.config.plain_text_only:
            content = strip_rich_formats(content)
        incoming = SyncMessage(content, msg.msg_id, pid)
        # Which router called decides the route the history row reports, so the
        # relay path's own flag is carried into the one place that stamps it.
        accepted = self.sync.handle_remote_message(incoming, via_relay=via_relay)
        if accepted and msg.msg_id and not self._stop_event.is_set() and not via_relay:
            # Existing receipt wire format; LAN only, no relay enrollment,
            # offline delivery ledger or promises about outgoing delivery.
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {"msg_type": "relay_ack", "msg_id": msg.msg_id, "ts": time.time()},
                    source_device=self.config.device_id,
                ),
            )
        if accepted and via_relay:
            # An internet-reachable sender gets the receipt on the relay too —
            # that is the ack its ledger row is waiting for.  Only for a frame
            # that arrived that way: a clip the local link carried has no relay
            # row behind it (the sender mirrors only what its LAN send failed to
            # deliver), so this ack would settle nothing and would put a frame
            # per clip on the broker for an exchange that never needed it.  The
            # transport-level receipt above is what settles that clip, and the
            # sender has read it off the LAN since ``_receive``'s relay_ack arm.
            self._maybe_send_relay_ack(msg, pid)

    def _maybe_send_relay_ack(self, msg, pid):
        """Ack a clipboard or chat frame this side accepted, over the relay.

        Published on the same encrypted channel the frame came in on (the
        netpair channel wins over LAN-relay enrollment), keyed only by the
        source frame's msg_id, so the sender's ledger resolves the receipt back
        to the exact message and the relay's best-effort QoS 0 becomes a real
        delivered mark.
        """
        kind = getattr(msg, "msg_type", "clipboard")
        if kind != "clipboard" and kind not in CHAT_MSG_TYPES:
            return
        msg_id = getattr(msg, "msg_id", "") or ""
        if not pid or not msg_id or not self._peer_is_internet_reachable(pid):
            return
        self._relay_publish_to_peer(
            encode_frame(
                {"msg_type": "relay_ack", "msg_id": msg_id, "ts": time.time()},
                source_device=self.config.device_id,
            ),
            pid,
        )
