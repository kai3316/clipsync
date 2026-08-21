"""WebSocket handler for real-time data push to web clients.

Implements RFC 6455 WebSocket protocol (handshake + framing) using
only Python stdlib (hashlib, struct, base64, threading).
"""

import base64
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


class WebSocketClient:
    """Represents a single connected WebSocket client."""

    def __init__(self, sock: socket.socket, addr: tuple):
        self.sock = sock
        self.addr = addr
        self._lock = threading.Lock()
        self._closed = False
        # A stalled client (phone asleep, cable pulled) must not block a
        # broadcast forever: bound every send/recv on this socket so
        # sendall() raises instead of blocking indefinitely.  This also
        # closes clients that send a partial frame and then stall.
        try:
            self.sock.settimeout(5.0)
        except OSError:
            pass

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

    def send_bytes(self, payload: bytes) -> bool:
        """Send a pre-serialized JSON text-frame payload.

        Avoids re-running ``json.dumps`` once per client on a broadcast.
        Returns True on success.
        """
        try:
            self._send_frame(_OP_TEXT, payload)
            return True
        except (OSError, ConnectionError) as e:
            logger.debug("WS send error (%s): %s", self.addr[0], e)
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
        """Send close frame and shut down the socket."""
        if self._closed:
            return
        try:
            self._send_frame(_OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def _wait_readable(self, timeout: float) -> bool:
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
            return bool(r)
        except (select.error, ValueError):
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
            except (OSError, ConnectionError, socket.timeout):
                return None
        return buf

    def _send_frame(self, opcode: int, payload: bytes):
        """Send a WebSocket frame."""
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
            self.sock.sendall(bytes(frame))


class WebSocketManager:
    """Manages all connected WebSocket clients and provides broadcast."""

    def __init__(self, cfg, history, sync_mgr, get_connected_ids, get_discovered=None,
                 get_resolved_hashes=None, get_pending_pairings=None,
                 on_client_attached=None):
        self._cfg = cfg
        self._history = history
        self._sync_mgr = sync_mgr
        self._get_connected_ids = get_connected_ids
        self._get_discovered = get_discovered
        self._get_resolved_hashes = get_resolved_hashes
        self._get_pending_pairings = get_pending_pairings
        # Called (with no args) after a new client finishes its handshake
        # snapshot; the DialogManager uses it to flush dialogs that were
        # queued while no client was connected.
        self._on_client_attached = on_client_attached
        self._clients: list[WebSocketClient] = []
        self._lock = threading.Lock()

    @property
    def on_client_attached(self):
        """Callback invoked after a new client's snapshot is sent."""
        return self._on_client_attached

    @on_client_attached.setter
    def on_client_attached(self, cb):
        self._on_client_attached = cb

    def handle_handshake(self, sock: socket.socket, addr: tuple,
                         request_headers: dict[str, str]) -> WebSocketClient | None:
        """Perform WebSocket handshake on an already-accepted socket.

        Returns the WebSocketClient on success, None on failure.
        """
        key = request_headers.get("sec-websocket-key", "")
        if not key:
            logger.debug("WS handshake: missing Sec-WebSocket-Key from %s", addr[0])
            return None

        # Compute accept key
        accept = base64.b64encode(
            hashlib.sha1(key.encode() + _WS_MAGIC).digest()
        ).decode()

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
        with self._lock:
            self._clients.append(client)

        self._send_snapshot(client)
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
        from internal.web.api.devices import get_devices
        dev_data, _ = get_devices(
            self._cfg, self._get_connected_ids, self._get_discovered,
            get_resolved_hashes=self._get_resolved_hashes,
            get_pending_pairings=self._get_pending_pairings,
        )
        client.send_json({"type": "devices_updated", "data": dev_data})

        # History
        from internal.web.api.history import get_history
        hist_data, _ = get_history(self._history, self._cfg)
        client.send_json({"type": "history_updated", "data": hist_data})

    def broadcast(self, message_type: str, data: dict | None = None):
        """Send a JSON message to all connected clients.

        message_type: one of 'devices_updated', 'history_updated',
                      'transfer_progress', 'clipboard_changed'
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
        for client in clients:
            if client.closed:
                dead.append(client)
                continue
            if not client.send_bytes(payload):
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
                try:
                    client.close()
                except Exception:
                    pass

    def remove_client(self, client: WebSocketClient) -> None:
        """Remove a client from the managed list."""
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                logger.info("WS client removed: %s:%d (%d clients)",
                            client.addr[0], client.addr[1], len(self._clients))

    def broadcast_history(self):
        """Convenience: broadcast full history to all clients."""
        from internal.web.api.history import get_history
        hist_data, _ = get_history(self._history, self._cfg)
        self.broadcast("history_updated", hist_data)

    def broadcast_devices(self):
        """Convenience: broadcast device list to all clients."""
        from internal.web.api.devices import get_devices
        dev_data, _ = get_devices(
            self._cfg, self._get_connected_ids, self._get_discovered,
            get_resolved_hashes=self._get_resolved_hashes,
            get_pending_pairings=self._get_pending_pairings,
        )
        self.broadcast("devices_updated", dev_data)

    def broadcast_transfer_progress(self, transfer_id: str, progress: float,
                                    status: str = "transferring"):
        """Convenience: broadcast transfer progress."""
        self.broadcast("transfer_progress", {
            "id": transfer_id,
            "progress": progress,
            "status": status,
        })

    def broadcast_transfer_complete(self, transfer_id: str, success: bool):
        """Convenience: broadcast a transfer completion event."""
        self.broadcast("transfer_complete", {
            "id": transfer_id,
            "success": bool(success),
        })

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def shutdown(self):
        """Close all connections and clear client list."""
        # Snapshot the list and clear under the lock, then close each client
        # outside it: close() sends a WS close frame (blocking socket I/O) and
        # must not stall the manager lock.
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        logger.info("WebSocket manager shut down")
