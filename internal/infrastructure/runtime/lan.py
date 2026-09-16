"""Owned LAN discovery, two-sided pairing and clipboard sync without a GUI."""

import logging
import os
import platform
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
from internal.config.config import PeerInfo, config_dir, config_lock
from internal.infrastructure.persistence.relay_delivery import RelayDeliveryQueue
from internal.infrastructure.runtime.internet_pairing import (
    InternetPairingService,
    is_provisional_key,
)
from internal.infrastructure.runtime.relay_delivery import RelayDelivery
from internal.platform.notify import notification_mgr
from internal.protocol.codec import (
    CLIP_FILE_MSG_TYPES,
    ENDING_MSG_TYPES,
    PAIRING_MSG_TYPES,
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
from internal.transport.connection import MAX_FRAME_SIZE, TransportManager
from internal.transport.discovery import Discovery
from internal.transport.ids import peer_id_hash
from internal.transport.relay import (
    MAX_RELAY_PAYLOAD,
    RelayTransport,
    build_paho_client,
    netpair_device_tag,
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


def _local_platform() -> tuple[str, str]:
    """This machine's os/arch, spelled the way the mDNS TXT records spell them.

    One spelling for both ends of the comparison: the values here are read
    against a peer's advertised ``os``/``arch``, which come off its own TXT
    records -- lowercased in one place and compared in the other, so the two
    have to be produced the same way or every peer looks like a different
    platform.
    """
    return platform.system().lower(), (platform.machine() or "").lower()


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
        super().__init__(*args, **kwargs)

    def _network_watch_loop(self):
        return self._owner._background(super()._network_watch_loop)

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
    # How often a deferred ending notice re-dials a peer it cannot reach.  The
    # maintenance loop runs four times a second, which is far more often than a
    # connection attempt needs and would keep several in flight at once.
    ENDING_DIAL_RETRY = 1.0
    DEVICE_PING_TIMEOUT = 4.0
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
        self._connecting = {}
        self._deferred = {}
        self._ending_dial_at = {}
        self._snapshot = {"items": []}
        self._pairing_ops = threading.RLock()
        self._probes = {}
        self._probes_lock = threading.Lock()
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
            lambda tid, name, size, mime, send_fn: self._publish(
                "transfer.request",
                {"transfer_id": tid, "filename": name, "size": size, "mime": mime},
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
        # The temp archives of folder sends, by transfer id, waiting to be
        # unlinked when their transfer reaches a terminal state.  Its own lock
        # rather than the runtime's: the completion callback runs on the
        # transfer's own thread and must not queue behind a command.
        self._outgoing_archives = {}
        self._archive_lock = threading.Lock()
        self._update_sink = None
        # peer device_id -> monotonic deadline, the ledger of peers this machine
        # has actually asked for a cached update asset.  Consulted by the update
        # guard above; a peer's answer that arrives inside the window is the only
        # update blob this side accepts.
        self._update_expectations: dict[str, float] = {}
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
            lambda sid, entry: self._publish(
                "chat.message", {"session_id": sid, "entry": entry}
            )
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
            lambda invite: self._publish("chat.invite", invite)
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
            return deepcopy(self._snapshot)

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
        """Where a peer-sent update blob goes (the update service's stage step)."""
        self._update_sink = sink

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

        Only a paired, currently-connected peer can be asked: the files travel
        over the LAN channel, which is the one whose certificate is pinned, and
        a request that named a relay peer would start a download that could
        never complete.  The counters are checked here so the caller learns
        immediately rather than watching a button do nothing.
        """
        if not entry_id or not isinstance(entry_id, str):
            raise ApplicationError("INVALID_ARGUMENT", "No entry to download")

        def run():
            pid = self._resolve(device_id)
            if not pid:
                raise ApplicationError("NOT_CONNECTED", "Device is not connected")
            if not self.pairing.is_peer_paired(pid):
                raise ApplicationError("NOT_PAIRED", "That device is not paired")
            if pid not in (self.transport.get_connected_peers() or []):
                raise ApplicationError("NOT_CONNECTED", "Device is not connected")
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "clip_file_request",
                        "entry": entry_id,
                        "ts": time.time(),
                    },
                    source_device=self.config.device_id,
                ),
            )
            with self._archive_lock:
                self._clip_file_outstanding[pid] = (entry_id, time.monotonic() + CLIP_FILE_WINDOW)
            logger.info("Asked %s for the files behind a history entry", pid[:8])
            return {"requested": True}

        return self._command(run)

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

        send_fn = lambda data: self.transport.send_to_peer(pid, data)  # noqa: E731
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
            self.transport.send_to_peer(
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
            )
        except Exception as exc:
            logger.warning("Failed to offer an update to %s", str(pid)[:12], exc_info=True)
            raise ApplicationError(
                "update.offer_failed", "The update could not be offered to that device"
            ) from exc
        logger.info("Offered update %s to peer %s", __version__, str(pid)[:12])
        return {"sent": True}

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
        """
        version = str(payload.get("version") or "")
        peer_os = str(payload.get("os") or "")
        peer_arch = str(payload.get("arch") or "")
        mine_os, mine_arch = _local_platform()
        # An empty platform is a peer too old to say, not a match.
        same_platform = bool(peer_os) and peer_os == mine_os and peer_arch == mine_arch
        if not same_platform or not updater.is_newer(version, __version__):
            logger.info(
                "Ignoring update offer %r from %s: platform %s/%s vs %s/%s",
                version, str(pid)[:12], peer_os, peer_arch, mine_os, mine_arch,
            )
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
        if peer_version and not updater.is_newer(__version__, peer_version):
            logger.info(
                "Peer %s asked for an update but runs %s", str(pid)[:12], peer_version
            )
            self._say_no_update_here(pid)
            return
        cached = updater.get_cached_asset()
        if not cached:
            logger.info("Peer asked for an update, but none is cached")
            self._say_no_update_here(pid)
            return
        try:
            self.file_transfer.send_file(
                cached, lambda data: self.transport.send_to_peer(pid, data), kind="update"
            )
        except Exception:
            logger.exception("Failed to serve the cached update to a peer")
            self._say_no_update_here(pid)

    def _say_no_update_here(self, pid: str) -> None:
        """Tell a peer that asked for an update that this machine has none."""
        try:
            self.transport.send_to_peer(
                pid,
                encode_frame(
                    {
                        "msg_type": "update_unavailable",
                        "version": __version__,
                        "os": _local_platform()[0],
                        "arch": _local_platform()[1],
                    },
                    source_device=self.config.device_id,
                ),
            )
        except Exception:
            logger.debug("Could not answer %s about the update", str(pid)[:12], exc_info=True)

    def _on_update_unavailable(self, pid: str, payload: dict) -> None:
        """A peer we asked has no installer to send.

        The refusal is the peer's word, and it is acted on as nothing more than
        news: no transfer is expected any more, so the ledger entry goes, and the
        window is told the click is over rather than left waiting on a file that
        is not coming.
        """
        with self._lock:
            self._update_expectations.pop(pid, None)
        self._publish(
            "update.peer_unavailable",
            {
                "device_id": pid,
                "name": self._peer_name(pid) or "",
                "version": str(payload.get("version") or ""),
            },
        )

    def _update_offerable(self, seen: dict) -> bool:
        """Whether *seen* (one discovery sighting) is a device this build updates.

        Three claims of the peer's, and all three have to hold: the same
        application, because an installer for one of the two published here
        cannot be installed by the other; the same platform, because the asset
        we would send is the one for this one; and a strictly older version,
        because an offer to a device that is already current is a transfer
        nobody wants.

        An empty version, or an empty app, means a peer too old to advertise --
        unknown, never "behind" -- so it is not offered to.  The app field
        decides more than which asset fits: reaching a peer that is not
        connected means dialling it, the dial carries no pairing request only
        for a build that reads one (``NO_PAIRING_MARKER``), and a dial a peer
        reads as a pairing request is a pairing code on that device's screen.
        A peer that cannot say which application it runs cannot be told that,
        so this machine does not call it.
        """
        version = str(seen.get("version") or "")
        if not version:
            return False
        if str(seen.get("app") or "") != updater.running_shell():
            return False
        if (str(seen.get("os") or ""), str(seen.get("arch") or "")) != _local_platform():
            return False
        return updater.is_newer(__version__, version)

    def _update_fetchable(self, seen: dict) -> bool:
        """Whether *seen* is a device this build can be updated *from*.

        The mirror of :meth:`_update_offerable`, and the direction the update
        path is meant to run in: the device that is behind is the one with a
        reason to act, so its own list is where the button belongs.  Same three
        claims, read the other way -- same application, same platform, and the
        peer's version strictly newer than this one -- and the same reason for
        the first of them: the request is a dial, and this machine will not
        make one it cannot mark as not-a-pairing-request.

        Nothing is trusted on the sighting: the blob it sends is checked against
        the published release digest before it can be installed, exactly as a
        GitHub download is.  This only decides whether offering the button is
        truthful at the moment the list is drawn.
        """
        version = str(seen.get("version") or "")
        if not version:
            return False
        if str(seen.get("app") or "") != updater.running_shell():
            return False
        if (str(seen.get("os") or ""), str(seen.get("arch") or "")) != _local_platform():
            return False
        return updater.is_newer(version, __version__)

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

    def _on_file_received(self, transfer_id, saved_path, _file_name):
        if self.file_transfer.take_received_kind(transfer_id) != "update":
            # Legacy beeped once per received file. The notification itself is
            # the host's job; only the sound is played here.
            self._play_transfer_sound()
            return
        sink = self._update_sink
        if sink is None:
            return
        # Verification looks up the published digest (up to ~30s), so it must
        # not run on the transfer receive thread that called this.
        threading.Thread(
            target=self._deliver_update_blob,
            args=(sink, saved_path),
            name="update-blob",
            daemon=True,
        ).start()

    @staticmethod
    def _deliver_update_blob(sink, saved_path):
        try:
            sink(saved_path)
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
            "sessions": self.chat.get_sessions(),
            "muted": sorted(self._chat_muted),
            "open_to_all": self.chat.open_to_all,
        }

    def set_chat_muted(self, peer_id, muted):
        def run():
            if muted:
                self._chat_muted.add(peer_id)
            else:
                self._chat_muted.discard(peer_id)
            with config_lock:
                self.config.chat_muted_peers = sorted(self._chat_muted)
            self._save_config()
            return {"ok": True, "muted": sorted(self._chat_muted)}
        return self._command(run)

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

        try:
            connected = set(self.transport.get_connected_peers() or [])
        except Exception:
            logger.debug("Chat send: connected peers unavailable", exc_info=True)
            connected = set()
        if peer_id not in connected and self._peer_is_internet_reachable(peer_id):
            # Every file byte has to ride the relay for a peer with no live LAN
            # connection, so chat chunks the file relay-safe and refuses
            # anything past the relay cap — a 256 KiB chunk would exceed
            # MAX_RELAY_PAYLOAD and stall the transfer.  The configured relay
            # limit picks the chunk: a broker that carries less than this app
            # assumes needs smaller chunks, not dropped ones.  A LAN-connected
            # peer keeps the LAN wire format so peers that predate the relay
            # keep interoperating unchanged.
            send.chunk_size = ChatManager.relay_chunk_for(self.config.relay_max_message_bytes)
            send.internet_cap = ChatManager.RELAY_FILE_CAP
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
        # Blocking: the dial below can hold this for CHAT_CONNECT_TIMEOUT.
        return self._command(self._chat_invite, peer_id, peer_name, blocking=True)

    def _chat_invite(self, peer_id, peer_name):
        """Open a chat session, dialing the peer first when it is not connected.

        Legacy dialed and waited up to 15s for the connection before starting
        the session, and told the user when it never came up; without the wait
        an invite to a discovered-but-idle device would be sent into a link that
        does not exist yet and silently go nowhere.  An internet-only peer has
        no LAN address to dial, but its send closure falls back to the relay, so
        a pairing code alone is enough to open the conversation.

        The dial suppresses the automatic pairing offer, as legacy's two
        chat-start paths both did: opening a conversation is not a request to
        pair, and the shared code the ordinary dial puts on both screens is
        consent neither side gave.
        """
        pid = self._resolve(peer_id)
        if pid not in self.transport.get_connected_peers() and self._address(pid) is not None:
            # The dial may hand back a different id than the one asked for --
            # the hash this device is discovered by becomes the real device_id
            # once the handshake lands -- and the session has to be keyed by the
            # one the transport delivers to, or every message is sent to an
            # address nothing is connected at.
            connected = self._connect_and_wait(pid, no_auto_pairing=True)
            if connected is None:
                self._publish(
                    "chat.connect_timeout",
                    {"peer_id": pid, "name": self._peer_name(pid) or peer_name},
                )
                return None
            pid = connected
        return self.chat.start_session(
            pid, peer_name,
            self.chat.shorten_fingerprint(self.pairing.get_peer_fingerprint(pid)),
            self._chat_send_fn(pid),
        )

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

    def _error(self, code):
        logger.warning("LAN runtime: %s", code)
        with self._lock:
            changed = self._last_error != code
            self._last_error = code
        if changed:
            self._publish("runtime.error", {"code": code, "message": "LAN operation failed"})

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

    def _cleanup(self):
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
            try:
                complete = stop()
                if complete is False:
                    ok = False
                else:
                    self._started.discard(name)
            except Exception:
                ok = False
                logger.warning("LAN runtime: RESOURCE_STOP_FAILED")
        if self._maintenance is not None and self._maintenance.ident is not None:
            self._maintenance.join()
        with self._idle:
            while self._active:
                self._idle.wait()
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
        self.delivery.tick()
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
        """
        with config_lock:
            peer = self.config.peers.get(pid)
            known = getattr(peer, "device_name", "") or ""
        if not known:
            known = self._peer_name(pid)
        return known or fallback

    def _address(self, pid):
        with self._lock:
            discovered = dict(self._discovered)
        for key, info in discovered.items():
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

    def _receive_relay(self, frame, topic=""):
        if not frame or self._stop_event.is_set():
            return
        msg = decode_message(frame)
        if msg is None:
            self._error("RELAY_FRAME_INVALID")
            return
        source = getattr(msg, "source_device", "") or ""
        if not source or source == self.config.device_id:
            return
        kind = getattr(msg, "msg_type", "")
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
                source, payload.get("peer_id", ""), payload.get("device_name", "") or "", topic
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
        # in the same place, in the same three cases.
        ident = self.internet_pairing.topic_identity(topic)
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
            previous = self._discovered.get(pid)
            self._discovered[pid] = row
        real = self._resolve(pid)
        with self._lock:
            dialing = self._connecting.get(real, 0) > time.monotonic()
        if self.pairing.is_peer_paired(real) and (not dialing or previous != row):
            self._connect(real)
        self._refresh()

    def _peer_lost(self, pid):
        real = self._resolve(pid)
        with self._lock:
            self._discovered.pop(pid, None)
            self._connecting.pop(real, None)
        self.transport.disconnect_peer(real)
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

    def _connect_rejected(self, name, pid):
        real = self._resolve(pid)
        with self._lock:
            self._connecting.pop(real, None)
        # The peer's name rides along so the phone can toast "«name» refused"
        # instead of a bare device id (legacy read it from the pairing repo).
        self._publish("device.connection_rejected", {"device_id": pid, "name": name or ""})
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
            with self._lock:
                discovered = dict(self._discovered)
                connecting = dict(self._connecting)
            # What each peer advertises about itself, keyed the way the rows
            # are: a device seen only over mDNS has no other source for these,
            # and a paired one that is currently away keeps the last sighting's
            # answer -- which is the version it will still be running when it
            # comes back.
            advertised = {
                self._resolve(pid): info for pid, info in discovered.items()
            }
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
            discovered_ids = set(advertised)
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
                        # The alias first, exactly as the pairing card reads it:
                        # it is the name the user chose for this device, and it
                        # outranks the one the peer published about itself.
                        "name": net.get("alias") or net.get("name") or pid,
                        # No note.  Notes live on a saved LAN peer, and there is
                        # none for a device this machine has never pinned; the
                        # row's rename writes the alias instead.
                        "note": "",
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

    def _peer_name(self, pid):
        """Best-effort display name for a peer, empty when nothing knows it.

        The name the peer publishes about itself comes first: it is the peer's
        own answer, and it is the one that changes when its user renames it —
        every other source here is a copy of what that answer used to be.
        """
        with self._lock:
            advertised = dict(self._discovered)
        # Resolved outside the lock: _resolve reads the transport and the
        # pairing manager, and holding _lock across those invites the reverse
        # order from whichever thread walks them the other way round.  A hash
        # nothing has resolved yet is dropped rather than keyed under "", so an
        # unresolved sighting cannot answer for an unnamed peer.
        published = {
            real: info["name"]
            for key, info in advertised.items()
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
            # Removed is terminal: forget the pairing notice de-duplication.
            self._pairing_seen.pop(pid, None)
            # A forgotten device has no pin to replace, and its prompt must not
            # outlive it in the window either.
            self._pending_certs.pop(pid, None)
            self._cert_alert_seen.pop(pid, None)
        self.transport.forget_peer(pid)
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
        with self._lock:
            discovered = dict(self._discovered)
        for key, info in discovered.items():
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
        """
        with self._lock:
            discovered = dict(self._discovered)
        return {
            pid: {"name": info["name"], "address": info["address"], "port": info["port"]}
            for pid, info in discovered.items()
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
            netpair_key,
            netpair_topic,
        )

        secret = (getattr(self.config, "netpair_secrets", {}) or {}).get(peer_id)
        if isinstance(secret, str) and secret:
            topic = netpair_topic(secret)
            key = netpair_key(secret, self.internet_pairing.netpair_password())
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
        try:
            ok = bool(self.relay.publish(frame, topic, key, qos=1 if is_chunk else 0))
        except Exception:
            logger.debug("Relay publish to %s failed", str(peer_id)[:12], exc_info=True)
            return False
        if ok:
            self._note_chat_sent(peer_id, msg)
        return ok

    @staticmethod
    def _decode_frame(data):
        """Decode one wire frame; None when it cannot be decoded at all."""
        try:
            return decode_message(data)
        except Exception:
            logger.debug("Frame decode failed", exc_info=True)
            return None

    def _handle_device_probe(self, kind, payload, pid, via_relay):
        """Answer a ping, or resolve an in-flight probe with a pong."""
        channel = "relay" if via_relay else "lan"
        if kind == "device_ping":
            frame = encode_frame(
                {
                    "msg_type": "device_pong",
                    "ping_id": str(payload.get("ping_id") or ""),
                    "ts": payload.get("ts"),
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

    def start_pairing(self, device_id):
        return self._command(self._start_pairing, device_id)

    def _start_pairing(self, device_id):
        pid = self._resolve(device_id)
        accepted = self._connect(pid)
        if accepted and pid in self.transport.get_connected_peers():
            with self._pairing_ops:
                if not self.pairing.is_peer_paired(pid):
                    self.pairing.generate_shared_pairing_code(pid)
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
            # ...and the advertisement, which is the third place our own name
            # is published and the only one that reaches the other devices.
            # Leaving it out is why the settings page had to promise the rename
            # would take effect "after a restart" — and why a peer that had
            # already listed this device never saw the new name at all.
            self.discovery.set_device_name(self.config.device_name)
            self._refresh()
        if "source_tracking_enabled" in updated:
            self.sync.monitor.set_source_tracking(self.config.source_tracking_enabled)
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
            self.transport.broadcast(data)
            self._publish_relay(data)
            return True
        return False

    def _publish_relay(self, data: bytes) -> None:
        """Mirror a clipboard frame to every internet-reachable peer.

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
            return
        from internal.transport.relay import (
            derive_key,
            derive_topic,
            netpair_key,
            netpair_topic,
        )

        delivery = self._delivery_metadata(data)
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
            peer = self.config.peers.get(peer_id)
            if peer is None or not getattr(peer, "paired", False):
                continue
            if not secret:
                secret = self.internet_pairing.ensure_relay_secret()
            ok = self._relay_publish(relay, data, derive_topic(secret, peer_secret),
                                     derive_key(secret, peer_secret))
            self._record_relay_send(peer_id, ok, delivery, data)
        for peer_id, peer_secret in netpair_secrets.items():
            if not peer_secret or peer_id == self.config.device_id or peer_id in removed:
                continue  # a stray self-entry must never mirror to ourselves
            ok = self._relay_publish(
                relay, data, netpair_topic(peer_secret),
                netpair_key(peer_secret, self.internet_pairing.netpair_password()),
            )
            self._record_relay_send(peer_id, ok, delivery, data)

    def _relay_publish(self, relay, data: bytes, topic, key) -> bool:
        """Hand one frame to the broker; any failure counts as not delivered."""
        try:
            return bool(relay.publish(data, topic, key))
        except Exception:
            logger.debug("Relay publish failed", exc_info=True)
            return False

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
            for content_type in (ContentType.TEXT, ContentType.HTML, ContentType.RTF):
                raw = types.get(content_type)
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    return text[:40]
            # A file copy on macOS carries a `public.url` beside its paths, so
            # the paths are asked first — the name of the thing beats a URI that
            # is only a spelling of its path.  Same order, and the same line, as
            # the history row this clip will become.
            raw = types.get(ContentType.FILE)
            if raw:
                paths = format.split_paths(raw.decode("utf-8", errors="replace"))
                if paths:
                    first = os.path.basename(paths[0]) or paths[0]
                    return first[:40] if len(paths) == 1 else f"{first[:40]} 等 {len(paths)} 个文件"
            offer = file_ref.parse(types.get(ContentType.FILE_REMOTE) or b"")
            if offer:
                return file_summary(offer["files"], offer["total"])[:40]
            raw = types.get(ContentType.URL)
            if raw:
                text = raw.decode("utf-8", errors="replace").strip()
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
            # The answer to an ``update_request``, so it is held to the same two
            # callers that request may come from -- and to no more, since its
            # only effect is to stop this side waiting.
            payload = getattr(msg, "_raw_payload", {}) or {}
            if trusted or self._same_platform_peer(payload):
                self._on_update_unavailable(pid, payload)
            return
        if kind in CLIP_FILE_MSG_TYPES:
            # Asked and answered on the LAN only.  The files travel over the
            # channel whose certificate is pinned, so a request that arrived
            # over the relay could only be answered with a transfer that never
            # completes — and answering it anyway would mean reading a peer's
            # files on the strength of a claim made on the public relay.
            if not (trusted and not via_relay):
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
        if kind == "file_chunk" and self.chat.handle_binary_chunk(
            getattr(msg, "_raw_payload", {}), pid, self._chat_send_fn(pid)
        ):
            return
        if kind.startswith("file_") or kind.startswith("speed_test"):
            # Dashboard transfers keep their LAN-only send closure (as legacy
            # did), so a relayed one could never answer anyway.
            if trusted and not via_relay:
                self.file_transfer.handle_message(
                    kind,
                    getattr(msg, "_raw_payload", {}),
                    lambda data: self.transport.send_to_peer(pid, data),
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
        if accepted:
            # An internet-reachable sender gets the receipt on the relay too —
            # that is the ack its ledger row is waiting for.
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
