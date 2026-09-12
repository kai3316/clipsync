"""WebSocket handler for real-time data push to web clients.

Implements RFC 6455 WebSocket protocol (handshake + framing) using
only Python stdlib (hashlib, struct, base64, threading).
"""

import base64
import contextlib
import hashlib
import json
import logging
import select
import socket
import struct
import threading
import time

logger = logging.getLogger(__name__)

# WebSocket magic GUID per RFC 6455
_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Frame opcodes
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

# Broadcasts bound each client's send to a shorter deadline than the steady-
# state 5s socket timeout, so one client that stops reading (backing up its
# send buffer) cannot stall every other client's update for the full 5s.
# The stuck client still self-evicts via send_bytes failing.
_BROADCAST_SEND_TIMEOUT = 1.0


class WebSocketClient:
    """Represents a single connected WebSocket client."""

    def __init__(self, sock: socket.socket, addr: tuple):
        self.sock = sock
        self.addr = addr
        self._lock = threading.Lock()
        self._closed = False
        # Separate from _closed: a failed send/recv sets _closed ("no longer
        # usable") long before anyone calls close() ("socket released").
        # Conflating the two meant the socket was never actually closed on the
        # common disconnect path.
        self._sock_closed = False
        # Monotonic timestamp of the last successfully-received frame. The
        # keepalive thread pings clients and drops any that go quiet for
        # several intervals (phone asleep, cable pulled) so a zombie
        # connection is not reported as "connected" forever.
        self.last_recv = time.monotonic()
        # A stalled client (phone asleep, cable pulled) must not block a
        # broadcast forever: bound every send/recv on this socket so
        # sendall() raises instead of blocking indefinitely.  This also
        # closes clients that send a partial frame and then stall.
        with contextlib.suppress(OSError):
            self.sock.settimeout(5.0)

    def send_json(self, data: dict) -> bool:
        """Send a JSON message to the client. Returns True on success."""
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_frame(_OP_TEXT, payload)
            return True
        except (OSError, ConnectionError) as e:
            logger.debug("WS send error (%s): %s", self.addr[0], e)
            self._closed = True
            return False

    def send_bytes(self, payload: bytes, timeout: float | None = None) -> bool:
        """Send a pre-serialized JSON text-frame payload.

        Avoids re-running ``json.dumps`` once per client on a broadcast.
        ``timeout``, when given, narrows the socket's send deadline for just
        this frame and restores it afterwards; broadcasts use it so one
        stalled client cannot delay the rest for the full steady-state 5s.
        Returns True on success.
        """
        try:
            self._send_frame(_OP_TEXT, payload, timeout)
            return True
        except (OSError, ConnectionError) as e:
            logger.debug("WS send error (%s): %s", self.addr[0], e)
            self._closed = True
            return False

    def send_ping(self) -> bool:
        """Send a WebSocket ping (keepalive) frame. Returns True on success.

        The browser's WebSocket implementation answers automatically with a
        pong, which the next ``recv_frame`` consumes and records as liveness.
        """
        try:
            self._send_frame(_OP_PING, b"")
            return True
        except (OSError, ConnectionError) as e:
            logger.debug("WS ping send error (%s): %s", self.addr[0], e)
            self._closed = True
            return False

    def recv_frame(self, timeout: float = 0.05) -> bytes | None:
        """Receive one complete frame payload (unmasked text data).
        Returns None if no data or connection closed.
        """
        try:
            if not self._wait_readable(timeout):
                return None
            # Read first 2 bytes
            header = self._recv_exact(2)
            if header is None:
                self._closed = True
                return None

            first_byte, second_byte = header[0], header[1]
            opcode = first_byte & 0x0F
            masked = (second_byte & 0x80) != 0
            payload_len = second_byte & 0x7F
            # Any successfully-read frame proves the connection is alive —
            # browsers auto-answer our ping with a pong, which lands here and
            # refreshes last_recv even though we discard the payload.
            self.last_recv = time.monotonic()

            # Handle extended payload length
            if payload_len == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    self._closed = True
                    return None
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    self._closed = True
                    return None
                payload_len = struct.unpack("!Q", ext)[0]
                if payload_len > 1024 * 1024:  # 1 MB max
                    self._closed = True
                    return None

            # Read mask key (client-to-server frames MUST be masked)
            if masked:
                mask_key = self._recv_exact(4)
                if mask_key is None:
                    self._closed = True
                    return None
            else:
                mask_key = None

            # Read payload
            payload = self._recv_exact(payload_len)
            if payload is None:
                self._closed = True
                return None

            # Unmask if needed
            if mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            # Handle control frames
            if opcode == _OP_CLOSE:
                self._send_frame(_OP_CLOSE, b"")
                self._closed = True
                return None
            elif opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                return None
            elif opcode == _OP_PONG:
                return None
            elif opcode == _OP_TEXT:
                return payload
            else:
                logger.debug("WS unknown opcode: %d", opcode)
                return None

        except (OSError, ConnectionError) as e:
            logger.debug("WS recv error (%s): %s", self.addr[0], e)
            self._closed = True
            return None

    def serve(self):
        """Run a read loop until the client disconnects.

        Blocks the calling thread.  Handles incoming frames (ping/pong,
        close) and cleans up on exit.  Call this after a successful
        handshake to keep the WebSocket connection alive.
        """
        try:
            while not self._closed:
                self.recv_frame(timeout=1.0)
        finally:
            self.close()

    def close(self):
        """Send a close frame (best effort) and shut the socket down.

        This has to run even when ``_closed`` is already True.  That flag is
        set by recv_frame/send_* the moment the peer goes away, and the normal
        disconnect path is exactly: recv_frame sets _closed -> serve()'s loop
        exits -> ``finally: self.close()``.  The old ``if self._closed:
        return`` guard therefore returned without ever calling
        ``sock.close()``, leaking one file descriptor (and one thread's worth
        of kernel buffers) for every browser tab that ever disconnected.
        ``_sock_closed`` keeps the method idempotent instead.
        """
        with self._lock:
            if self._sock_closed:
                return
            self._sock_closed = True
            send_close = not self._closed
            self._closed = True
        if send_close:
            # Only worth attempting while the peer is believed alive; a CLOSE
            # frame down a dead socket just raises.
            with contextlib.suppress(OSError, ConnectionError):
                self._send_frame(_OP_CLOSE, b"")
        # shutdown() first so a peer blocked reading us sees EOF immediately
        # rather than waiting for the OS to time the connection out.
        with contextlib.suppress(OSError, ConnectionError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _wait_readable(self, timeout: float) -> bool:
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
            return bool(r)
        except (OSError, ValueError):
            return False

    def _recv_exact(self, n: int) -> bytes | None:
        """Receive exactly n bytes or None on error/eof."""
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    return None
                buf += chunk
            except (TimeoutError, OSError, ConnectionError):
                return None
        return buf

    def _send_frame(self, opcode: int, payload: bytes, timeout: float | None = None):
        """Send a WebSocket frame.

        ``timeout`` (if given) temporarily narrows the socket timeout around
        the sendall and restores the previous value afterwards.
        """
        with self._lock:
            frame = bytearray()
            frame.append(0x80 | opcode)  # FIN + opcode

            length = len(payload)
            if length < 126:
                frame.append(length)
            elif length < 65536:
                frame.append(126)
                frame.extend(struct.pack("!H", length))
            else:
                frame.append(127)
                frame.extend(struct.pack("!Q", length))

            frame.extend(payload)
            prev_timeout = self.sock.gettimeout()
            if timeout is not None:
                self.sock.settimeout(timeout)
            try:
                self.sock.sendall(bytes(frame))
            finally:
                if timeout is not None:
                    self.sock.settimeout(prev_timeout)


class WebSocketManager:
    """Manages all connected WebSocket clients and provides broadcast."""

    def __init__(
        self,
        cfg,
        history,
        sync_mgr,
        get_connected_ids,
        get_discovered=None,
        get_resolved_hashes=None,
        get_pending_pairings=None,
        get_reconnect_states=None,
        on_client_attached=None,
    ):
        self._cfg = cfg
        self._history = history
        self._sync_mgr = sync_mgr
        self._get_connected_ids = get_connected_ids
        self._get_discovered = get_discovered
        self._get_resolved_hashes = get_resolved_hashes
        self._get_pending_pairings = get_pending_pairings
        # Optional transport auto-reconnect bookkeeping source; forwarded to
        # the device snapshot so offline peers mid-reconnect carry the
        # reconnecting/reconnect_attempt/reconnect_max fields.
        self._get_reconnect_states = get_reconnect_states
        # Called (with no args) after a new client finishes its handshake
        # snapshot; the DialogManager uses it to flush dialogs that were
        # queued while no client was connected.
        self._on_client_attached = on_client_attached
        self._clients: list[WebSocketClient] = []
        self._lock = threading.Lock()
        # Keepalive: browsers cannot send control frames, so the server must
        # ping and drop clients that stop answering (phone asleep, network
        # drop) — otherwise they linger as "connected" zombies forever.
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="ws-heartbeat",
        )
        self._hb_thread.start()

    @property
    def on_client_attached(self):
        """Callback invoked after a new client's snapshot is sent."""
        return self._on_client_attached

    @on_client_attached.setter
    def on_client_attached(self, cb):
        self._on_client_attached = cb

    def handle_handshake(
        self, sock: socket.socket, addr: tuple, request_headers: dict[str, str]
    ) -> WebSocketClient | None:
        """Perform WebSocket handshake on an already-accepted socket.

        Returns the WebSocketClient on success, None on failure.
        """
        key = request_headers.get("sec-websocket-key", "")
        if not key:
            logger.debug("WS handshake: missing Sec-WebSocket-Key from %s", addr[0])
            return None

        # Compute accept key
        accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_MAGIC).digest()).decode()

        # Build upgrade response
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        try:
            sock.sendall(response.encode())
        except OSError as e:
            logger.debug("WS handshake send failed: %s", e)
            return None

        client = WebSocketClient(sock, addr)
        # Send the snapshot BEFORE adding the client to the broadcast list.
        # The snapshot send holds the client's per-client send lock, so any
        # broadcast that includes this client (only possible after the
        # append below) queues behind the snapshot and cannot be clobbered
        # by it.  The old order (append → snapshot) let a broadcast that
        # raced the handshake arrive first and then be overwritten by the
        # stale snapshot's wholesale replace.
        self._send_snapshot(client)
        with self._lock:
            self._clients.append(client)
        # Flush any dialogs that were queued while no client was connected so
        # an incoming transfer that arrived "blind" is shown to this client
        # instead of being silently rejected (see DialogManager.flush_pending).
        if self._on_client_attached is not None:
            try:
                self._on_client_attached()
            except Exception:
                logger.debug("on_client_attached hook failed", exc_info=True)
        logger.info("WS client connected: %s:%d (%d clients)", addr[0], addr[1], len(self._clients))
        return client

    def _send_snapshot(self, client: WebSocketClient):
        """Send initial state snapshot to a newly connected client."""
        # Device list
        client.send_json({"type": "devices_updated", "data": self._devices_snapshot()})

        # History
        from internal.web.api.history import get_history

        hist_data, _ = get_history(self._history, self._cfg)
        client.send_json({"type": "history_updated", "data": hist_data})

    def broadcast(self, message_type: str, data: dict | None = None) -> int:
        """Send a JSON message to all connected clients.

        message_type: one of 'devices_updated', 'history_updated',
                      'transfer_progress', 'clipboard_changed'

        Returns the number of clients the message was actually delivered to
        (0 when no client was connected or every send failed).  Callers such
        as DialogManager use this to distinguish "delivered" from merely
        "has a registered client", so a dead connection is not treated as a
        delivered dialog.
        """
        message = {
            "type": message_type,
            "data": data or {},
            "ts": time.time(),
        }
        # Pre-serialize once and reuse the same bytes for every client instead
        # of re-running json.dumps per client.
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        # Snapshot the client list under the lock, then send outside it.
        # send_bytes() performs blocking socket I/O (sendall); holding the
        # manager lock while blocked would stall every other broadcast and
        # new handshake for as long as the slowest client's TCP backpressure.
        with self._lock:
            clients = list(self._clients)
        dead: list[WebSocketClient] = []
        delivered = 0
        for client in clients:
            if client.closed:
                dead.append(client)
                continue
            # Bound each client's send to a shorter deadline than the steady-
            # state 5s socket timeout: a client that is not reading backs up
            # its send buffer and would otherwise stall every other client's
            # update for the full 5s.  A timed-out send still self-evicts the
            # stuck client below.
            if client.send_bytes(payload, timeout=_BROADCAST_SEND_TIMEOUT):
                delivered += 1
            else:
                dead.append(client)
        # Clean up dead clients.  Close them outside the lock — close() sends
        # a WS close frame (blocking socket I/O) which would stall the manager
        # lock if done under it.
        if dead:
            with self._lock:
                for client in dead:
                    if client in self._clients:
                        self._clients.remove(client)
            for client in dead:
                with contextlib.suppress(Exception):
                    client.close()
        return delivered

    def remove_client(self, client: WebSocketClient) -> None:
        """Remove a client from the managed list."""
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                logger.info(
                    "WS client removed: %s:%d (%d clients)",
                    client.addr[0],
                    client.addr[1],
                    len(self._clients),
                )

    def _ping_and_collect_stale(self, now: float | None = None) -> list:
        """Ping every live client and return the ones to drop.

        A client is stale when its connection is already closed, when sending
        the ping fails (dead socket), or when it has not delivered any frame
        (including the auto-pong) for more than ``PING_INTERVAL * MISSED_LIMIT``
        seconds.  Extracted from the heartbeat loop so the drop decision is
        unit-testable without waiting on real timers.
        """
        now = time.monotonic() if now is None else now
        ping_interval = 30.0
        missed_limit = 3  # ~90s of silence before dropping a client
        with self._lock:
            clients = list(self._clients)
        stale: list[WebSocketClient] = []
        for client in clients:
            if client.closed:
                stale.append(client)
                continue
            try:
                # send_ping() swallows OSError and returns False -- it does not
                # raise -- so the bare try/except never fired and a client with
                # a dead socket survived until the 90s silence window instead
                # of being dropped on the spot.  Read the return value.
                if not client.send_ping():
                    logger.debug(
                        "WS keepalive: ping failed, dropping %s:%d", client.addr[0], client.addr[1]
                    )
                    stale.append(client)
                    continue
            except Exception:
                stale.append(client)
                continue
            if now - client.last_recv > ping_interval * missed_limit:
                logger.debug(
                    "WS keepalive: dropping stalled client %s:%d", client.addr[0], client.addr[1]
                )
                stale.append(client)
        return stale

    def _drop_clients(self, stale: list) -> None:
        """Remove and close the given clients."""
        if not stale:
            return
        with self._lock:
            for client in stale:
                if client in self._clients:
                    self._clients.remove(client)
        for client in stale:
            with contextlib.suppress(Exception):
                client.close()

    def _heartbeat_loop(self) -> None:
        """Keepalive loop: ping every client and drop stale ones.

        Runs on a daemon thread for the manager's lifetime.  Every 30s each
        live client is sent a WS ping frame (the browser auto-answers with a
        pong, which ``recv_frame`` records as liveness).  A client that goes
        ~90s without any received frame is dead and is removed so the UI stops
        reporting it as connected.
        """
        ping_interval = 30.0
        while not self._hb_stop.wait(ping_interval):
            self._drop_clients(self._ping_and_collect_stale())

    def broadcast_history(self):
        """Convenience: broadcast full history to all clients."""
        from internal.web.api.history import get_history

        hist_data, _ = get_history(self._history, self._cfg)
        self.broadcast("history_updated", hist_data)

    def broadcast_history_deleted(self, entry_ids, total: int | None = None):
        """Convenience: tell clients specific history entries were removed.

        The ``history_updated`` merge on the client can only upsert/prepend —
        it cannot express a deletion.  A delete / batch-delete therefore sends
        this event so every client removes the entries (and the ones that
        issued the request don't race their own local splice).
        """
        self.broadcast(
            "history_item_deleted",
            {
                "entry_ids": list(entry_ids or []),
                "total": total,
            },
        )

    def broadcast_history_clear(self):
        """Convenience: tell clients the entire history was wiped."""
        self.broadcast("history_clear", {})

    def broadcast_favorites(self):
        """Convenience: broadcast the full favourites list to all clients.

        Snapshot-shaped like the history broadcast above, and built here rather
        than passed in: the manager holds no favourites store, and the loader is
        the same one behind ``GET /api/favorites``, so a single function decides
        what a favourite looks like on the wire.  The client takes the whole
        list — a favourite carries a user-edited title and a position, so a
        partial update would have to re-state both.
        """
        from internal.web.api.favorites import get_favorites

        data, _status = get_favorites()
        self.broadcast("favorites_updated", data)

    def _devices_snapshot(self) -> dict:
        """Compute the authoritative web device snapshot.

        Covers the device list plus pending pairings and the removed archive —
        everything the device page renders — so a fingerprint of it is a sound
        "did anything user-visible change" signal.
        """
        from internal.web.api.devices import get_devices

        dev_data, _ = get_devices(
            self._cfg,
            self._get_connected_ids,
            self._get_discovered,
            get_resolved_hashes=self._get_resolved_hashes,
            get_pending_pairings=self._get_pending_pairings,
            get_reconnect_states=self._get_reconnect_states,
        )
        return dev_data

    def devices_fingerprint(self) -> str:
        """Stable serialization of the device snapshot for change detection.

        Any rendered-field change — connected/paired transitions, reconnect
        progress, names, addresses, notes, the removed archive — yields a
        different fingerprint, so the peer-status loop can broadcast promptly
        on real changes instead of a coarse name+suffix string.
        """
        import json

        return json.dumps(
            self._devices_snapshot(),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    def broadcast_devices(self):
        """Convenience: broadcast device list to all clients."""
        self.broadcast("devices_updated", self._devices_snapshot())

    def broadcast_transfer_progress(
        self,
        transfer_id: str,
        progress: float,
        status: str = "transferring",
        direction: str | None = None,
    ):
        """Convenience: broadcast transfer progress.

        ``direction`` is optional ('up'/'down'): when present it is forwarded
        so the UI can render the correct arrow; when omitted the payload is
        exactly the legacy shape, so existing callers are unaffected.
        """
        payload = {
            "id": transfer_id,
            "progress": progress,
            "status": status,
        }
        if direction is not None:
            payload["direction"] = direction
        self.broadcast("transfer_progress", payload)

    def broadcast_transfer_complete(self, transfer_id: str, success: bool, cancelled: bool = False):
        """Convenience: broadcast a transfer completion event."""
        self.broadcast(
            "transfer_complete",
            {
                "id": transfer_id,
                "success": bool(success),
                "cancelled": bool(cancelled),
            },
        )

    def broadcast_chat_sessions(self, sessions=None):
        """Convenience: broadcast the full nearby-chat session list.

        ``sessions`` is the list of session dicts from ``ChatManager.get_sessions()``
        (the web UI re-renders from the whole list on any change).
        """
        self.broadcast("chat_sessions", {"sessions": sessions or []})

    def broadcast_chat_message(self, session_id: str, entry: dict):
        """Convenience: broadcast one new chat message entry."""
        self.broadcast(
            "chat_message",
            {
                "session_id": session_id,
                "entry": entry or {},
            },
        )

    def broadcast_chat_progress(self, session_id: str, transfer_id: str, fraction: float):
        """Convenience: broadcast chat file-transfer progress (both directions)."""
        self.broadcast(
            "chat_progress",
            {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "fraction": fraction,
            },
        )

    def broadcast_chat_file_done(
        self, session_id: str, transfer_id: str, success: bool, saved_path: str, status: str
    ):
        """Convenience: broadcast a completed chat file transfer."""
        self.broadcast(
            "chat_file_done",
            {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "success": bool(success),
                "saved_path": saved_path or "",
                "status": status or "",
            },
        )

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def shutdown(self):
        """Close all connections and clear client list."""
        # Stop the keepalive thread first so it can't race the teardown.
        self._hb_stop.set()
        # Snapshot the list and clear under the lock, then close each client
        # outside it: close() sends a WS close frame (blocking socket I/O) and
        # must not stall the manager lock.
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                client.close()
        logger.info("WebSocket manager shut down")
