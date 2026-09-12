"""Owned LAN discovery, two-sided pairing and clipboard sync without a GUI."""

import logging
import os
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
from internal.clipboard.clipboard import strip_rich_formats
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
    PAIRING_MSG_TYPES,
    decode_message,
    encode_frame,
    encode_message,
    has_syncable_types,
)
from internal.security.fingerprint import sas_code
from internal.security.pairing import PAIRING_STATUS_PAIRED, fingerprint_short
from internal.sync.ai_config import AIConfigManager
from internal.sync.file_transfer import FileTransferManager
from internal.sync.manager import SyncManager
from internal.sync.nearby_chat import CHAT_MSG_TYPES, ChatManager
from internal.system import updater
from internal.system.archive import ArchiveEmptyError, create_archive
from internal.transport.connection import MAX_FRAME_SIZE, TransportManager
from internal.transport.discovery import Discovery
from internal.transport.ids import peer_id_hash
from internal.transport.relay import RelayTransport, build_paho_client, netpair_device_tag

logger = logging.getLogger(__name__)


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
    PAIRING_SEND_WAIT = 12.0
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
        self._snapshot = {"items": []}
        self._pairing_ops = threading.RLock()
        self._probes = {}
        self._probes_lock = threading.Lock()
        self._dirty = threading.Event()
        self._persist_dirty = False
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
            lambda tid, progress: self._publish(
                "transfer.progress", {"transfer_id": tid, "progress": progress}
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
        # The temp archives of folder sends, by transfer id, waiting to be
        # unlinked when their transfer reaches a terminal state.  Its own lock
        # rather than the runtime's: the completion callback runs on the
        # transfer's own thread and must not queue behind a command.
        self._outgoing_archives = {}
        self._archive_lock = threading.Lock()
        self._update_sink = None
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
            lambda sid, tid, fraction: self._publish(
                "chat.file.progress",
                {"session_id": sid, "transfer_id": tid, "fraction": fraction},
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
        """
        picked = list(paths)
        if not picked or not all(isinstance(path, str) and path for path in picked):
            raise ApplicationError("INVALID_ARGUMENT", "No files to send")

        def run():
            # A file's path is passed through exactly as it was handed in: this
            # runtime is in no position to rewrite a caller's path, and a
            # round trip through Path would do just that on Windows.
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
            if not device_id:
                transfer_id = self.file_transfer.send_file(subject, self.transport.broadcast)
            else:
                pid = self._resolve(device_id)
                if pid not in (self.transport.get_connected_peers() or []):
                    if archive:
                        Path(archive).unlink(missing_ok=True)
                    raise ApplicationError("NOT_CONNECTED", "Device is not connected")
                transfer_id = self.file_transfer.send_file(
                    subject, lambda data: self.transport.send_to_peer(pid, data)
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

        return self._command(run)

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
                send_fn = (
                    (lambda data: self.transport.send_to_peer(peer_id, data))
                    if peer_id else self.transport.broadcast
                )
                return self.file_transfer.send_file(entry["source_path"], send_fn)
            raise ApplicationError("INVALID_ARGUMENT", "Unknown transfer action")
        return self._command(run)

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

    def request_update_from_peers(self) -> None:
        """Ask connected peers for their cached release asset.

        A peer that already downloaded the release answers with it
        (``kind="update"``); the release-server download runs in parallel, so
        nothing ever waits on a peer.
        """
        try:
            self.transport.broadcast(
                encode_frame(
                    {"msg_type": "update_request"}, source_device=self.config.device_id
                )
            )
            logger.info("Broadcast update_request to peers")
        except Exception:
            logger.warning("Failed to broadcast update_request", exc_info=True)

    def _serve_cached_update(self, pid: str) -> None:
        """Answer a paired peer's update_request with our cached asset, if any."""
        cached = updater.get_cached_asset()
        if not cached:
            logger.info("Peer asked for an update, but none is cached")
            return
        try:
            self.file_transfer.send_file(
                cached, lambda data: self.transport.send_to_peer(pid, data), kind="update"
            )
        except Exception:
            logger.exception("Failed to serve the cached update to a peer")

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
        return {"devices": self.devices()["items"]}

    def chat_sessions(self):
        return {"sessions": self.chat.get_sessions(), "muted": sorted(self._chat_muted)}

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
            # MAX_RELAY_PAYLOAD and stall the transfer.  A LAN-connected peer
            # keeps the LAN wire format so peers that predate the relay keep
            # interoperating unchanged.
            send.chunk_size = ChatManager.RELAY_CHUNK_SIZE
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
        return self._command(self._chat_invite, peer_id, peer_name)

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
        if (
            pid not in self.transport.get_connected_peers()
            and self._address(pid) is not None
            and not self._connect_and_wait(pid, no_auto_pairing=True)
        ):
            self._publish(
                "chat.connect_timeout",
                {"peer_id": pid, "name": self._peer_name(pid) or peer_name},
            )
            return None
        return self.chat.start_session(
            pid, peer_name,
            self.chat.shorten_fingerprint(self.pairing.get_peer_fingerprint(pid)),
            self._chat_send_fn(pid),
        )

    def _connect_and_wait(self, pid, timeout=None, no_auto_pairing=False):
        """Dial one peer and wait for the link; True once it is up."""
        if pid in self.transport.get_connected_peers():
            return True
        if not self._connect(pid, no_auto_pairing=no_auto_pairing):
            return False
        deadline = time.monotonic() + (
            self.CHAT_CONNECT_TIMEOUT if timeout is None else timeout
        )
        while time.monotonic() < deadline and not self._stop_event.is_set():
            if pid in self.transport.get_connected_peers():
                return True
            time.sleep(0.1)
        return pid in self.transport.get_connected_peers()

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
                return self.chat.decline_invitation(session_id, send_fn)
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

    def _command(self, callback, *args):
        if not self._enter():
            raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is not running")
        try:
            with self._pairing_ops:
                if self._stop_event.is_set():
                    raise ApplicationError("LAN_NOT_RUNNING", "LAN runtime is stopping")
                return callback(*args)
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError(
                "LAN_OPERATION_FAILED", "LAN operation failed", retryable=True
            ) from None
        finally:
            self._leave()

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
                self.relay = RelayTransport(
                    list(self.config.relay_brokers),
                    self.internet_pairing.channels,
                    lambda frame, *args: self._background(self._receive_relay, frame, *args),
                    self._relay_state_changed,
                    client_factory=build_paho_client,
                    username=getattr(self.config, "relay_username", ""),
                    password=getattr(self.config, "relay_password", ""),
                    private_brokers=list(getattr(self.config, "relay_private_brokers", []) or []),
                )
                self.internet_pairing.attach_relay(self.relay)
                self.relay.start()
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

    def _cleanup(self):
        self._start_done.wait()
        ok = True
        # Disable first, so a capture already reading cannot record or send.
        self.sync.set_enabled(False)
        try:
            self.chat.shutdown()
        except Exception:
            ok = False
        if self.relay is not None:
            try:
                self.relay.stop()
                self.relay = None
            except Exception:
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
        with self._lock:
            deferred = dict(self._deferred)
        connected = set(self.transport.get_connected_peers())
        pending = {p[0] for p in self.pairing.get_pending_pairings()}
        for pid, (kind, deadline) in deferred.items():
            # Cancel stale confirmations after reject/unpair/expiry.
            valid = pid in pending or self.pairing.is_peer_paired(pid)
            expired = time.monotonic() >= deadline
            sent = valid and pid in connected and self._send_pairing(pid, kind)
            if sent or not valid or expired:
                with self._lock:
                    if self._deferred.get(pid) == (kind, deadline):
                        self._deferred.pop(pid, None)
                if expired and valid and not sent:
                    self._error("PAIRING_SEND_FAILED")

    def _internet_unpaired(self, peer_id):
        """An internet pair was severed — drop its sends and tell the UIs.

        Legacy pushed ``netpair_peer`` {status:"unpaired"} from the REST route
        that did the unpairing; publishing it from the runtime instead means
        every caller (the phone, the desktop settings panel) reaches the same
        listeners, and the phone's other tabs can drop the row.
        """
        self.delivery.clear_peer(peer_id)
        self._publish("netpair.peer.changed", {"peer_id": peer_id, "status": "unpaired"})

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
        return device_id

    def _address(self, pid):
        with self._lock:
            discovered = dict(self._discovered)
        for key, info in discovered.items():
            if self._resolve(key) == pid:
                return info
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

    def _peer_found(self, pid, name, address, port):
        if pid in (self.config.device_id, peer_id_hash(self.config.device_id)):
            return
        with self._lock:
            previous = self._discovered.get(pid)
            self._discovered[pid] = (name, address, port)
        real = self._resolve(pid)
        with self._lock:
            dialing = self._connecting.get(real, 0) > time.monotonic()
        if self.pairing.is_peer_paired(real) and (not dialing or previous != (name, address, port)):
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

    def _refresh(self, publish=True):
        with self._pairing_ops:
            pending = {p[0]: p for p in self.pairing.get_pending_pairings()}
            self.pairing.drain_expiry_rollbacks()
            if publish:
                self._persist()
            known = {p.device_id: p for p in self.pairing.get_known_peers()}
            connected = {self._resolve(pid) for pid in self.transport.get_connected_peers()}
            with self._lock:
                discovered = dict(self._discovered)
                connecting = dict(self._connecting)
            names = {self._resolve(pid): info[0] for pid, info in discovered.items()}
            discovered_ids = set(names)
            names.update({pid: p.device_name for pid, p in known.items()})
            rows = []
            for pid, name in sorted(names.items()):
                peer = known.get(pid)
                paired = bool(peer and peer.paired)
                request = pending.get(pid)
                status = "paired" if paired else self.pairing.get_pairing_status(pid)
                fingerprint = self.pairing.get_peer_fingerprint(pid)
                mine = self.pairing.get_identity().fingerprint
                rows.append(
                    {
                        "id": pid,
                        "name": name,
                        "note": (getattr(peer, "notes", "") or "") if peer else "",
                        "paired": paired,
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
        # Only a live conversation suppresses the notice: a closed or declined
        # session must not silence this device's pairing notices forever.
        chat_peers = {
            session["peer_id"]
            for session in self.chat.get_sessions()
            if session["status"] in ("inviting", "invited", "active")
        }
        now = time.monotonic()
        events = []
        arrived = set()
        with self._lock:
            for pid in sorted(connected - self._connected_seen):
                arrived.add(pid)
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
        """Best-effort display name for a peer, empty when nothing knows it."""
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
        """An incoming chat invite cancels the pairing prompt for that peer.

        Every unpaired connection auto-generates a shared code, so a peer that
        invites us to chat would otherwise surface as "wants to pair" as well.
        Chatting is its own consent flow (the invite banner), so the pending
        pairing is dropped and the notice de-duplication forgotten — the legacy
        ``_chat_handle_incoming_invite`` did the same before answering. The
        deferred pairing frames are left to :meth:`_tick_locked`, which drops
        them for a peer that is neither pending nor paired.
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
                return info
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
        ``(name, address, port)`` tuple; the web devices API expects
        ``{id: {"name", "address", "port"}}`` exactly like the legacy
        ``Application._snapshot_discovered_peers``.
        """
        with self._lock:
            discovered = dict(self._discovered)
        return {
            pid: {"name": info[0], "address": info[1], "port": info[2]}
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
            if pid not in {peer.device_id for peer in self.pairing.get_known_peers()}:
                raise ApplicationError("NOT_FOUND", "Device not found")
            if not self.pairing.is_peer_paired(pid):
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
            if pid not in {p.device_id for p in self.pairing.get_known_peers()}:
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
        try:
            self._send_pairing(pid, "pairing_unpair" if unpair else "pairing_reject")
        finally:
            self.transport.forget_peer(pid)
            if unpair:
                try:
                    # Unpaired stops the peer's relay retries too: nothing may
                    # keep trying to deliver to a device the user broke with.
                    self.delivery.clear_peer(pid)
                except Exception:
                    logger.debug("Relay queue cleanup failed")
            # A rejected/unpaired peer has no pending request left to answer.
            # Legacy pushed this without a status, so keep it empty.
            self._publish("pairing.resolved", {"device_id": pid, "status": ""})
            self._refresh()
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
        for peer_id, peer_secret in (self.config.peer_relay_secrets or {}).items():
            if not peer_secret or peer_id in netpair_secrets:
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
            if not peer_secret or peer_id == self.config.device_id:
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
        """First 40 chars of a clipboard message's text (for the send list)."""
        try:
            types = getattr(content, "types", {}) or {}
            for content_type in (ContentType.TEXT, ContentType.HTML, ContentType.RTF):
                raw = types.get(content_type)
                if not raw:
                    continue
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
            if trusted:
                self._serve_cached_update(pid)
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
                else:
                    self.pairing.mark_peer_rejected(
                        pid
                    ) if kind == "pairing_reject" else self.pairing.mark_peer_unpaired(pid)
                    with self._lock:
                        self._deferred.pop(pid, None)
            if kind != "pairing_confirm":
                self.transport.forget_peer(pid)
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
