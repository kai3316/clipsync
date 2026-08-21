"""TLS-encrypted TCP connection management for peer-to-peer sync."""

import hashlib
import logging
import os
import select
import socket
import ssl
import struct
import threading
import time


class PortInUseError(OSError):
    """Raised when the TCP port is already in use by another process."""

    def __init__(self, port: int):
        self.port = port
        super().__init__(f"Port {port} is already in use")
import uuid
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding

from internal.protocol.codec import decode_message
from internal.security.encryption import is_encrypted
from internal.security.pairing import CertificateChangedError, PairingManager, fingerprint_pem

logger = logging.getLogger(__name__)

MAX_FRAME_SIZE = 10 * 1024 * 1024  # 10 MB
FRAME_HEADER_SIZE = 4
DATA_TIMEOUT = 30.0  # socket read timeout
MAX_RECONNECT_ATTEMPTS = 10
MAX_RECONNECT_BACKOFF = 30
MIN_RECONNECT_DELAY = 3  # minimum 3s before first reconnect to allow accept_loop to resolve bidirectional races
MAX_IDLE_SECONDS = 120.0  # max time with no received bytes before a connection is declared dead
KEEPALIVE_IDLE = 30  # TCP keepalive: idle seconds before probes start
KEEPALIVE_INTERVAL = 10  # TCP keepalive: seconds between probes
KEEPALIVE_COUNT = 6  # TCP keepalive: failed probes before declaring the connection dead

# Rejection frame sent after identity exchange when the accepting side
# refuses the connection (peer in _rejected_peer_ids).  The connecting
# side reads this before creating a PeerConnection and knows not to
# schedule a reconnect.
_REJECT_MARKER = b"\xff\xff\xff\xffRJCT"


class PeerConnection:
    """Represents a TLS connection to a single peer."""

    def __init__(self, device_id: str, device_name: str, sock: socket.socket,
                 peer_fingerprint: str = "", enc_mgr=None, pairing_mgr=None):
        self.device_id = device_id
        self.device_name = device_name
        self._sock = sock
        self._sock.settimeout(DATA_TIMEOUT)
        self._send_lock = threading.Lock()
        self._recv_thread: threading.Thread | None = None
        self._running = False
        self._on_message: Callable | None = None
        self._on_disconnect: Callable | None = None
        self._peer_fingerprint = peer_fingerprint
        self._enc_mgr = enc_mgr
        self._pairing_mgr = pairing_mgr
        self._last_recv_time = time.monotonic()
        self._auth_failures = 0
        self._enable_keepalive()

    def set_on_message(self, callback: Callable):
        self._on_message = callback

    def set_on_disconnect(self, callback: Callable):
        self._on_disconnect = callback

    def start(self):
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        logger.debug("[%s] PeerConnection started (conn=%s)", self.device_name, hex(id(self)))

    def stop(self):
        self._running = False
        logger.debug("[%s] PeerConnection stopping (conn=%s)", self.device_name, hex(id(self)))
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    def _enable_keepalive(self):
        """Enable TCP keepalive with short intervals so half-open connections
        (a peer that vanished without RST) are detected quickly. Best-effort:
        some platforms or socket wrappers don't support these options."""
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE)
            if hasattr(socket, "TCP_KEEPINTVL"):
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL)
            if hasattr(socket, "TCP_KEEPCNT"):
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
        except (OSError, AttributeError):
            pass

    def send(self, data: bytes) -> bool:
        """Send a frame. Returns False if the send failed.

        If app-layer encryption is enabled, the payload is encrypted with
        a per-peer frame key derived from the paired certificates.
        """
        try:
            if self._enc_mgr and self._peer_fingerprint:
                data = self._enc_mgr.encrypt_frame(data, self._peer_fingerprint)
                logger.info(
                    "[%s] App-layer encrypted frame payload (%d bytes on wire)",
                    self.device_name, len(data),
                )
            else:
                logger.debug(
                    "[%s] Sending unencrypted frame (%d bytes) — enc_mgr=%s peer_fp=%s",
                    self.device_name, len(data),
                    "set" if self._enc_mgr else "none",
                    "set" if self._peer_fingerprint else "empty",
                )
            if len(data) > MAX_FRAME_SIZE:
                # Refuse oversized frames here so the receiver never sees a
                # frame_len that trips its MAX_FRAME_SIZE guard and disconnects.
                logger.warning(
                    "[%s] refusing to send oversized frame (%d bytes > %d)",
                    self.device_name, len(data), MAX_FRAME_SIZE,
                )
                return False
            frame = struct.pack(">I", len(data)) + data
            with self._send_lock:
                self._sock.sendall(frame)
            return True
        except Exception as e:
            logger.warning("Send to %s failed: %s", self.device_name, e)
            return False

    def health_check(self) -> bool:
        """Check if the underlying TCP connection is still alive.

        Uses SO_ERROR for fast broken-connection detection, then a
        non-blocking peek to catch remote-close (EOF) which SO_ERROR
        does not surface. Returns False if the socket is dead.
        """
        try:
            error = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error != 0:
                return False
            if not self._running:
                return False
            # Non-blocking peek: if the socket is readable with no data
            # pending, the remote has cleanly closed (EOF).
            ready, _, _ = select.select([self._sock], [], [], 0)
            if ready:
                try:
                    data = self._sock.recv(1, socket.MSG_PEEK)
                    if data == b"":
                        return False
                except Exception:
                    # SSL sockets may raise on MSG_PEEK (internal
                    # buffering). If select() reported readable, the
                    # connection is still alive — don't kill it.
                    pass
            return True
        except Exception:
            return False

    def _max_idle_expired(self) -> bool:
        """True if the connection has been idle past the deadline with no
        keepalive progress (SO_ERROR set by failed keepalive probes). Healthy
        idle connections keep SO_ERROR clear, so this never kills them."""
        if time.monotonic() - self._last_recv_time < MAX_IDLE_SECONDS:
            return False
        try:
            return self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0
        except Exception:
            return False

    def _recv_loop(self):
        end_reason = "unknown"
        while self._running:
            # Half-open detection fallback: keepalive probes set SO_ERROR when
            # a peer vanishes without RST. If we've had no bytes for a long
            # window and the socket reports an error, the connection is dead.
            # Healthy idle connections keep SO_ERROR clear, so this never
            # kills a legitimate idle peer.
            if self._max_idle_expired():
                end_reason = "no bytes and no keepalive progress for too long"
                break
            try:
                header = self._recv_exact(FRAME_HEADER_SIZE)
                if header is None:
                    end_reason = "header recv returned None (remote closed or error)"
                    break
                frame_len = struct.unpack(">I", header)[0]
                if frame_len == 0 or frame_len > MAX_FRAME_SIZE:
                    end_reason = f"invalid frame size: {frame_len}"
                    break

                payload = self._recv_exact(frame_len)
                if payload is None:
                    end_reason = "payload recv returned None"
                    break

                # App-layer decryption
                if self._enc_mgr and self._peer_fingerprint:
                    pt = self._enc_mgr.decrypt_frame(payload, self._peer_fingerprint)
                    if pt is not None:
                        logger.debug(
                            "[%s] App-layer decrypted frame payload (%d bytes plaintext)",
                            self.device_name, len(pt),
                        )
                        payload = pt
                    elif is_encrypted(payload):
                        # Auth tag mismatch — fail closed: skip the frame instead
                        # of feeding the encrypted bytes to decode_message.
                        self._auth_failures += 1
                        logger.warning(
                            "[%s] App-layer decrypt FAILED — auth tag mismatch! "
                            "Possible tampering or wrong password.", self.device_name,
                        )
                        if self._auth_failures >= 3:
                            logger.error(
                                "[%s] repeated auth failures (%d) — peer may be "
                                "using a different key or frames are being tampered with",
                                self.device_name, self._auth_failures,
                            )
                        continue
                    else:
                        logger.warning(
                            "[%s] Received unencrypted frame (%d bytes) — passing through",
                            self.device_name, len(payload),
                        )
                else:
                    logger.debug(
                        "[%s] Received frame (%d bytes) — no enc_mgr, passing through",
                        self.device_name, len(payload),
                    )

                msg = decode_message(payload)
                if msg and self._on_message:
                    # Incoming pairing gate: only paired peers may send app-level
                    # messages. Pairing happens via the connection's own identity
                    # frames, not decoded app frames, so no legitimate pre-pairing
                    # app frames exist. Dropping them stops unpaired LAN peers
                    # from injecting clipboard / nav_url / file-dialog content.
                    if self._pairing_mgr is not None and not self._pairing_mgr.is_peer_paired(self.device_id):
                        logger.warning(
                            "[%s] dropping %s frame from unpaired peer (device_id=%s)",
                            self.device_name, getattr(msg, "msg_type", "clipboard"),
                            self.device_id[:12],
                        )
                        continue
                    # Pass this connection's peer id so handlers can respond to
                    # the sender (e.g. file-transfer acks/chunks) instead of
                    # broadcasting to every peer.
                    self._on_message(msg, self.device_id)

            except (ConnectionError, OSError) as e:
                end_reason = f"ConnectionError: {e}"
                break
            except Exception as e:
                end_reason = f"Exception: {type(e).__name__}: {e}"
                break

        logger.info("[%s] recv_loop ended: %s (conn=%s)", self.device_name, end_reason, hex(id(self)))
        self._running = False
        if self._on_disconnect:
            logger.debug("[%s] calling on_disconnect callback (conn=%s)", self.device_name, hex(id(self)))
            self._on_disconnect(self.device_id, self)

    def _recv_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    logger.info("[%s] recv returned empty bytes (remote closed connection)", self.device_name)
                    return None
                buf.extend(chunk)
                self._last_recv_time = time.monotonic()
            except socket.timeout:
                if not self._running:
                    return None
                continue
            except Exception as e:
                logger.info("[%s] recv exception: %s: %s", self.device_name, type(e).__name__, e)
                return None
        return bytes(buf)


class TransportManager:
    """Manages peer connections — server and client side."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        port: int,
        pairing_mgr: PairingManager,
        max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
    ):
        self._device_id = device_id
        self._device_name = device_name
        self._port = port
        self._pairing_mgr = pairing_mgr
        self._server_sock: socket.socket | None = None
        self._server_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._peers: dict[str, PeerConnection] = {}
        self._running = False
        self._on_peer_message: Callable | None = None
        self._on_wake: Callable | None = None
        self._lock = threading.Lock()
        self._last_health_tick = 0.0
        self._peer_addresses: dict[str, tuple[str, str, int]] = {}
        self._reconnect_attempts: dict[str, int] = {}
        self._reconnect_timers: dict[str, threading.Timer] = {}
        self._max_reconnect_attempts = max_reconnect_attempts
        self._hash_to_real_id: dict[str, str] = {}
        self._rejected_peer_ids: set[str] = set()
        self._enc_mgr = None
        self._on_security_alert: Callable | None = None

    def set_encryption_manager(self, enc_mgr) -> None:
        """Set the encryption manager for app-layer encryption."""
        self._enc_mgr = enc_mgr

    def set_on_peer_message(self, callback: Callable):
        self._on_peer_message = callback

    def set_on_wake(self, callback: Callable):
        """Set a callback invoked when sleep/wake is detected.

        The callback receives no arguments. Use it to re-register
        mDNS or perform other post-wake recovery.
        """
        self._on_wake = callback

    def set_on_security_alert(self, callback: Callable):
        """Set a callback for security events (e.g. certificate changed).

        Called with (peer_name, expected_fingerprint, received_fingerprint).
        """
        self._on_security_alert = callback

    @staticmethod
    def _secure_scratch_dir() -> Path:
        """Per-user scratch directory for temporary key material.

        Uses the platform config directory (not system /tmp) so files are
        never world-readable and survive only as long as the app runs.
        Stale files from previous crashes are cleaned on next startup.
        """
        import platform
        system = platform.system()
        if system == "Windows":
            base = os.environ.get("APPDATA", str(Path.home()))
            scratch = Path(base) / "ClipSync" / ".scratch"
        elif system == "Darwin":
            scratch = Path.home() / "Library" / "Application Support" / "ClipSync" / ".scratch"
        else:
            scratch = Path.home() / ".config" / "clipsync" / ".scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        # On Unix, restrict permissions so only the owner can read
        if system != "Windows":
            try:
                scratch.chmod(0o700)
            except Exception:
                pass
        return scratch

    @staticmethod
    def _cleanup_stale_scratch():
        """Remove any leftover key files from a previous crash."""
        try:
            scratch = TransportManager._secure_scratch_dir()
            for f in scratch.glob("*.pem"):
                try:
                    f.unlink()
                    logger.debug("Cleaned up stale temp file: %s", f.name)
                except OSError:
                    pass
        except Exception:
            pass

    def _build_ssl_context(self, server_side: bool = True,
                           verify_peer_id: str | None = None) -> ssl.SSLContext:
        """Build an SSL context from in-memory identity and optional peer cert.

        Writes key material to a secure per-user scratch directory (NOT system
        /tmp). Files are cleaned up immediately after loading into the SSL context.

        server_side: True for accept(), False for connect().
        verify_peer_id: if set, require and pin this peer's certificate.
        """
        identity = self._pairing_mgr.get_identity()
        scratch = self._secure_scratch_dir()

        uid = uuid.uuid4().hex[:8]
        key_path = scratch / f"identity_key_{uid}.pem"
        cert_path = scratch / f"identity_cert_{uid}.pem"

        try:
            key_path.write_text(identity.private_key_pem, encoding="ascii")
            cert_path.write_text(identity.certificate_pem, encoding="ascii")

            if server_side:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            else:
                ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

            ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            ssl_context.check_hostname = False
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3

            if verify_peer_id:
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                peer_cert = self._pairing_mgr.get_peer_certificate(verify_peer_id)
                if peer_cert:
                    ca_path = scratch / f"peer_cert_{uid}.pem"
                    try:
                        ca_path.write_text(peer_cert, encoding="ascii")
                        ssl_context.load_verify_locations(cafile=str(ca_path))
                    finally:
                        try:
                            ca_path.unlink()
                        except OSError:
                            pass
            else:
                ssl_context.verify_mode = ssl.CERT_NONE

            return ssl_context

        finally:
            for p in [key_path, cert_path]:
                try:
                    p.unlink()
                except OSError:
                    pass

    @staticmethod
    def _send_identity(sock: ssl.SSLSocket, cert_pem: str):
        """Send our certificate PEM as the first application-level frame."""
        data = cert_pem.encode("ascii")
        frame = struct.pack(">I", len(data)) + data
        sock.sendall(frame)

    @staticmethod
    def _recv_identity(sock: ssl.SSLSocket, timeout: float = 10.0) -> bytes | None:
        """Read the first frame from the peer — expected to be their cert PEM."""
        prev_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            header = b""
            while len(header) < FRAME_HEADER_SIZE:
                chunk = sock.recv(FRAME_HEADER_SIZE - len(header))
                if not chunk:
                    return None
                header += chunk
            frame_len = struct.unpack(">I", header)[0]
            if frame_len == 0 or frame_len > MAX_FRAME_SIZE:
                return None
            data = b""
            while len(data) < frame_len:
                chunk = sock.recv(frame_len - len(data))
                if not chunk:
                    return None
                data += chunk
            return data
        except Exception as e:
            logger.warning("Failed to read identity frame: %s", e)
            return None
        finally:
            sock.settimeout(prev_timeout)

    @staticmethod
    def _send_rejection(sock: ssl.SSLSocket):
        """Send a rejection marker so the peer knows not to reconnect."""
        try:
            sock.sendall(_REJECT_MARKER)
        except Exception:
            pass

    @staticmethod
    def _check_rejection(sock: ssl.SSLSocket, timeout: float = 2.0) -> bool:
        """Check if the server sent a rejection marker after identity exchange.

        Returns True if the peer rejected this connection.
        """
        prev_timeout = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            data = b""
            while len(data) < len(_REJECT_MARKER):
                chunk = sock.recv(len(_REJECT_MARKER) - len(data))
                if not chunk:
                    break
                data += chunk
            return data == _REJECT_MARKER
        except Exception:
            return False
        finally:
            sock.settimeout(prev_timeout)

    def start_server(self):
        self._cleanup_stale_scratch()
        ssl_context = self._build_ssl_context(server_side=True)

        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_sock.bind(("0.0.0.0", self._port))
        except OSError as e:
            self._server_sock.close()
            self._server_sock = None
            raise PortInUseError(self._port) from e
        self._server_sock.listen(5)
        self._server_sock.settimeout(1.0)

        self._running = True
        self._last_health_tick = time.monotonic()
        self._health_thread = threading.Thread(
            target=self._health_check_loop, daemon=True,
        )
        self._health_thread.start()
        self._server_thread = threading.Thread(
            target=self._accept_loop, args=(ssl_context,), daemon=True,
        )
        self._server_thread.start()
        logger.info("TCP server listening on port %d", self._port)

    def stop_server(self):
        self._running = False
        # Unblock accept() by connecting to our own port
        if self._server_sock:
            try:
                unblocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    unblocker.connect(("127.0.0.1", self._port))
                except Exception:
                    pass
                unblocker.close()
            except Exception:
                pass
        # Disconnect all peers (outside lock to avoid holding it during I/O)
        with self._lock:
            peers = list(self._peers.values())
            self._peers.clear()
        for conn in peers:
            conn.stop()
        with self._lock:
            timers = list(self._reconnect_timers.values())
            self._reconnect_timers.clear()
        for timer in timers:
            timer.cancel()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass

    def connect_to_peer(self, peer_id: str, peer_name: str, address: str, port: int):
        with self._lock:
            # Clear rejected status — user explicitly wants to connect now.
            # Also clear any related IDs (hashed or real) so the incoming
            # side of the connection won't be refused.
            ids_to_clear = {peer_id}
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id:
                ids_to_clear.add(real_id)
            for h, r in list(self._hash_to_real_id.items()):
                if r == peer_id:
                    ids_to_clear.add(h)
            for pid in ids_to_clear:
                self._rejected_peer_ids.discard(pid)
            if peer_id in self._peers:
                logger.debug(
                    "[%s] connect_to_peer: already connected (peer_id=%s), skipping",
                    peer_name, peer_id[:12],
                )
                return  # already connected
            # Resolve hashed mDNS ID to real device_id so we don't create
            # a duplicate connection when the peer is stored under its real ID.
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id and real_id in self._peers:
                logger.debug(
                    "[%s] connect_to_peer: already connected under real ID %s, skipping",
                    peer_name, real_id[:12],
                )
                return
            self._peer_addresses[peer_id] = (peer_name, address, port)
        logger.info("[%s] connecting to %s:%d (peer_id=%s)", peer_name, address, port, peer_id[:12])

        def _connect():
            sock = None
            try:
                logger.info("[%s] TCP connecting to %s:%d", peer_name, address, port)
                sock = socket.create_connection((address, port), timeout=10)
                logger.info("[%s] TCP connected, starting TLS handshake", peer_name)

                # Peers are stored under their real device id, but discovery may
                # call us with a hashed mDNS id — resolve it so pinning still
                # applies to an already-paired peer (otherwise is_peer_paired()
                # returns False and we'd silently drop to CERT_NONE).
                real = self._hash_to_real_id.get(peer_id, peer_id)
                verify_id = real if self._pairing_mgr.is_peer_paired(real) else None
                ssl_context = self._build_ssl_context(
                    server_side=False, verify_peer_id=verify_id)
                logger.info("[%s] SSL context built (verify_id=%s)", peer_name, verify_id[:12] if verify_id else "None")

                ssl_sock = ssl_context.wrap_socket(sock, server_hostname=peer_id)
                logger.info("[%s] TLS handshake complete", peer_name)

                # Exchange identity at application level: send our cert,
                # then read the server's cert from its identity frame.
                identity = self._pairing_mgr.get_identity()
                self._send_identity(ssl_sock, identity.certificate_pem)
                logger.info("[%s] sent identity frame", peer_name)

                # Read server's identity frame (cert PEM)
                server_cert_data = self._recv_identity(ssl_sock)

                # Check if the server rejected us at the application level
                # (e.g. we were forgotten by this peer).  The server sends
                # a rejection marker after its identity frame.
                if self._check_rejection(ssl_sock):
                    logger.info(
                        "[%s] peer explicitly rejected this connection — "
                        "clearing saved address to prevent auto-reconnect",
                        peer_name,
                    )
                    with self._lock:
                        self._peer_addresses.pop(peer_id, None)
                        self._reconnect_attempts.pop(peer_id, None)
                    ssl_sock.close()
                    return

                real_peer_id = peer_id
                if server_cert_data:
                    peer_cert_pem = server_cert_data.decode("ascii")
                    peer_cert = x509.load_pem_x509_certificate(peer_cert_pem.encode())

                    try:
                        cn_attrs = peer_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                        if cn_attrs:
                            real_peer_id = cn_attrs[0].value
                    except Exception:
                        pass

                    was_paired = self._pairing_mgr.is_peer_paired(real_peer_id)
                    logger.info(
                        "[%s] cert extracted: real_peer_id=%s, was_paired=%s, peer_id=%s",
                        peer_name, real_peer_id[:12], was_paired, peer_id[:12],
                    )
                    self._pairing_mgr.add_peer(
                        real_peer_id, peer_name, peer_cert_pem,
                        paired=was_paired,
                    )
                    if not was_paired:
                        try:
                            shared_code = self._pairing_mgr.generate_shared_pairing_code(real_peer_id)
                            logger.info(
                                "[%s] pairing code: %s**** — verify on both devices",
                                peer_name, shared_code[:4],
                            )
                        except Exception as e:
                            logger.debug("Could not generate shared pairing code: %s", e)

                # Resolve peer fingerprint for app-layer encryption
                peer_fp = self._pairing_mgr.get_peer_fingerprint(real_peer_id) if real_peer_id else ""

                conn = PeerConnection(real_peer_id, peer_name, ssl_sock,
                                      peer_fingerprint=peer_fp, enc_mgr=self._enc_mgr,
                                      pairing_mgr=self._pairing_mgr)
                conn.set_on_message(self._on_peer_message)
                conn.set_on_disconnect(self._on_peer_disconnected)
                conn.start()

                # Socket shutdown/close and health checks do I/O — collect the
                # connections to stop under the lock, then stop them outside it.
                to_stop: list = []
                existing = None
                with self._lock:
                    if not self._running:
                        logger.debug("[%s] server stopped, discarding new connection", peer_name)
                        conn.set_on_disconnect(None)
                        conn.stop()
                        return
                    # If discovery used a hashed ID, clean up and update mapping
                    if real_peer_id != peer_id:
                        logger.debug(
                            "[%s] hash→real mismatch: hash=%s real=%s",
                            peer_name, peer_id[:12], real_peer_id[:12],
                        )
                        old_hash = self._peers.pop(peer_id, None)
                        if old_hash:
                            logger.debug("[%s] removing stale hash-keyed connection (conn=%s)",
                                         peer_name, hex(id(old_hash)))
                            old_hash.set_on_disconnect(None)
                            to_stop.append(old_hash)
                        # Re-key address tracking under the real ID
                        addr_info = self._peer_addresses.pop(peer_id, None)
                        if addr_info:
                            self._peer_addresses[real_peer_id] = addr_info
                        # Remember mapping so UI can deduplicate
                        self._hash_to_real_id[peer_id] = real_peer_id
                    # Bidirectional connection race resolution:
                    # Both sides may attempt outgoing connections simultaneously,
                    # creating two TCP connections. Use a deterministic tiebreaker
                    # so both sides agree on which one survives: the device with
                    # the lower device_id acts as client — its outgoing wins.
                    existing = self._peers.get(real_peer_id)
                    if existing is None:
                        self._peers[real_peer_id] = conn
                        logger.debug(
                            "[%s] stored in _peers[%s] (total peers: %d)",
                            peer_name, real_peer_id[:12], len(self._peers),
                        )
                    elif self._device_id < real_peer_id:
                        # We are the lower ID — our outgoing wins.
                        logger.info(
                            "[%s] tiebreaker: our outgoing wins (we=%s < peer=%s) — replacing",
                            peer_name, self._device_id[:12], real_peer_id[:12],
                        )
                        self._peers.pop(real_peer_id)
                        existing.set_on_disconnect(None)
                        to_stop.append(existing)
                        self._peers[real_peer_id] = conn
                        existing = None
                    # else: peer has the lower ID — their outgoing should win.
                    # Check the existing connection's health outside the lock.
                if existing is not None and not existing.health_check():
                    # Peer has lower ID but existing connection is dead
                    # (e.g. closed by peer during a previous race round).
                    # Keep our outgoing so we don't lose both connections.
                    with self._lock:
                        if self._peers.get(real_peer_id) is existing:
                            logger.info(
                                "[%s] tiebreaker: peer should win but existing is dead — keeping our outgoing",
                                peer_name,
                            )
                            self._peers.pop(real_peer_id)
                            existing.set_on_disconnect(None)
                            to_stop.append(existing)
                            self._peers[real_peer_id] = conn
                            existing = None
                        elif real_peer_id not in self._peers:
                            # Existing was removed concurrently — keep our outgoing.
                            self._peers[real_peer_id] = conn
                            existing = None
                if existing is not None:
                    # Peer has lower ID — their outgoing wins, discard ours.
                    logger.info(
                        "[%s] tiebreaker: peer's outgoing wins (peer=%s < we=%s) — discarding our outgoing",
                        peer_name, real_peer_id[:12], self._device_id[:12],
                    )
                    conn.set_on_disconnect(None)
                    to_stop.append(conn)
                for c in to_stop:
                    c.stop()

                self._reconnect_attempts.pop(peer_id, None)
                self._reconnect_attempts.pop(real_peer_id, None)
                logger.info("[%s] connected [%s] (%s:%d)", peer_name, real_peer_id[:12], address, port)

            except CertificateChangedError:
                # Expected = the stored fingerprint of the previously paired
                # cert; received = the fingerprint of the new cert that just
                # triggered the alert. Populate both so the alert is useful.
                expected_fp = self._pairing_mgr.get_peer_fingerprint(real_peer_id) if real_peer_id else ""
                received_fp = fingerprint_pem(peer_cert_pem) if peer_cert_pem else ""
                logger.error(
                    "SECURITY: Certificate for %s has changed — possible MITM attack! "
                    "Connection rejected. Expected fp: %s Got: %s",
                    peer_name,
                    expected_fp[:16] if expected_fp else "n/a",
                    received_fp[:16] if received_fp else "n/a",
                )
                if self._on_security_alert:
                    self._on_security_alert(peer_name, expected_fp, received_fp)
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("[%s] connect failed: %s", peer_name, e)
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                # Only auto-reconnect for paired peers
                lookup = self._hash_to_real_id.get(peer_id, peer_id)
                if self._pairing_mgr.is_peer_paired(lookup):
                    self._schedule_reconnect(peer_id)
                else:
                    logger.info("[%s] connect failed and peer not paired — no auto-reconnect", peer_name)

        threading.Thread(target=_connect, daemon=True).start()

    def broadcast(self, data: bytes):
        with self._lock:
            peers = list(self._peers.values())
        for conn in peers:
            # Outgoing pairing gate: never leak clipboard content to unpaired
            # or anonymous peers. Pairing is done via the connection's own
            # identity frames, not broadcast, so this is safe to skip.
            if not self._pairing_mgr.is_peer_paired(conn.device_id):
                logger.debug(
                    "[%s] broadcast: skipping unpaired peer", conn.device_id[:12],
                )
                continue
            conn.send(data)

    def send_to_peer(self, peer_id: str, data: bytes):
        with self._lock:
            conn = self._peers.get(peer_id)
        if conn is None:
            logger.warning("send_to_peer: peer %s not found", peer_id[:12])
            return
        conn.send(data)

    def get_connected_peers_with_names(self) -> list[tuple[str, str]]:
        """Return list of (peer_id, device_name) for all connected peers."""
        with self._lock:
            return [(pid, conn.device_name) for pid, conn in self._peers.items()]

    def disconnect_peer(self, peer_id: str, reject: bool = False):
        """Disconnect a peer and optionally reject reconnections.

        When *reject* is True, incoming connections from this peer are
        refused until the next explicit connect_to_peer() call.  Use this
        for user-initiated disconnects to prevent the remote side from
        immediately reconnecting.
        """
        with self._lock:
            conn = self._peers.pop(peer_id, None)
            # Cancel any pending reconnect timer so we don't race with it
            timer = self._reconnect_timers.pop(peer_id, None)
            self._reconnect_attempts.pop(peer_id, None)
            if reject:
                self._rejected_peer_ids.add(peer_id)
        if timer:
            timer.cancel()
        if conn:
            logger.info("[%s] manual disconnect%s", peer_id[:12],
                        " (rejected)" if reject else "")
            conn.set_on_disconnect(None)
            conn.stop()

    def forget_peer(self, peer_id: str):
        """Disconnect and permanently reject this peer.

        After calling this, the peer will not be able to reconnect
        (incoming connections are refused) until the user explicitly
        connects again. Use this for Reject and Forget actions.
        """
        # Collect all IDs that refer to the same peer.  There are three
        # cases:
        #   1. peer_id is the real device_id → purge peer_id + every
        #      hashed mDNS ID that maps to it.
        #   2. peer_id is a hashed mDNS ID → purge peer_id + the real
        #      device_id it maps to + every other hash that maps to the
        #      same real ID.
        #   3. peer_id is an anonymous key (__anon__…) → just purge it.
        ids_to_reject = {peer_id}

        with self._lock:
            # If peer_id is a hashed ID, resolve it to the real ID
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id:
                ids_to_reject.add(real_id)
            else:
                # peer_id might be the real ID — find every hash that
                # resolves to it
                real_id = peer_id

            # Find all hashed IDs that map to this real ID
            for h, r in list(self._hash_to_real_id.items()):
                if r == real_id:
                    ids_to_reject.add(h)

            # Also find any hashed IDs stored in _peer_addresses that
            # share the same (address, port) and are not yet covered
            addr_info = self._peer_addresses.get(peer_id)
            if addr_info:
                _, addr, port = addr_info
                for pid, (_, a, p) in list(self._peer_addresses.items()):
                    if a == addr and p == port:
                        ids_to_reject.add(pid)

            # Purge every collected ID from all tracking structures
            timer_to_cancel = None
            conn_to_stop = None
            for pid in ids_to_reject:
                self._rejected_peer_ids.add(pid)
                self._peer_addresses.pop(pid, None)
                self._reconnect_attempts.pop(pid, None)
                t = self._reconnect_timers.pop(pid, None)
                if t:
                    timer_to_cancel = t
                c = self._peers.pop(pid, None)
                if c:
                    conn_to_stop = c
                self._hash_to_real_id.pop(pid, None)

        if timer_to_cancel:
            timer_to_cancel.cancel()
        if conn_to_stop:
            logger.info("[%s] forget: manual disconnect", peer_id[:12])
            conn_to_stop.set_on_disconnect(None)
            conn_to_stop.stop()

    def get_connected_peers(self) -> list[str]:
        with self._lock:
            return list(self._peers.keys())

    def get_resolved_hashes(self) -> dict[str, str]:
        """Return mapping of hashed mDNS peer_id → real device_id.

        Discovery uses a hashed device_id for privacy; after the TLS
        handshake the real device_id is extracted from the peer
        certificate.  Callers can use this mapping to deduplicate
        peer lists.
        """
        with self._lock:
            return dict(self._hash_to_real_id)

    def get_peer_addresses(self) -> dict[str, tuple[str, str, int]]:
        """Return a copy of all last-known peer addresses (real-id → tuple)."""
        with self._lock:
            return dict(self._peer_addresses)

    def get_saved_address(self, peer_id: str) -> tuple[str, str, int] | None:
        """Return the last known (name, address, port) for a peer, or None.

        Handles real device IDs, the hashed mDNS IDs used during discovery,
        and the hash→real mapping, so callers can reconnect to a known peer
        even when it is momentarily absent from mDNS.
        """
        def _hashed(pid: str) -> str:
            return hashlib.sha256(pid.encode()).hexdigest()[:12]
        with self._lock:
            if peer_id in self._peer_addresses:
                return self._peer_addresses[peer_id]
            hashed = _hashed(peer_id)
            if hashed in self._peer_addresses:
                return self._peer_addresses[hashed]
            # Any hashed mDNS ID that resolves to this real ID
            for h, r in self._hash_to_real_id.items():
                if r == peer_id and h in self._peer_addresses:
                    return self._peer_addresses[h]
        return None

    def _on_peer_disconnected(self, peer_id: str, conn=None):
        with self._lock:
            current = self._peers.get(peer_id)
            if current is None:
                logger.info(
                    "[%s] disconnect: already removed from _peers (conn=%s)",
                    peer_id[:12], hex(id(conn)) if conn else "N/A",
                )
                return
            elif conn is not None and current is not conn:
                # Disconnect is from a stale connection that was already
                # replaced by a newer one (e.g. during bidirectional connection
                # race). Don't delete the good connection.
                logger.info(
                    "[%s] disconnect from STALE connection (old=%s, current=%s) — ignoring",
                    peer_id[:12], hex(id(conn)), hex(id(current)),
                )
                return
            else:
                logger.info(
                    "[%s] disconnect from current connection (conn=%s) — removing from _peers",
                    peer_id[:12], hex(id(conn)) if conn else "N/A",
                )
                del self._peers[peer_id]
        # Only auto-reconnect to paired peers. Unpaired connections
        # (during pairing) should be user-initiated to avoid a
        # bidirectional reconnect race that tears down connections
        # before the user can enter the pairing code.
        if self._pairing_mgr.is_peer_paired(peer_id):
            self._schedule_reconnect(peer_id)
        else:
            # Also check if the hashed mDNS ID maps to a paired real ID
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id and self._pairing_mgr.is_peer_paired(real_id):
                self._schedule_reconnect(peer_id)
            else:
                logger.info(
                    "[%s] peer not paired — skipping auto-reconnect",
                    peer_id[:12],
                )

    def _schedule_reconnect(self, peer_id: str):
        with self._lock:
            if peer_id not in self._peer_addresses:
                logger.debug(
                    "[%s] reconnect: no saved address, skipping", peer_id[:12],
                )
                return
            if not self._running:
                return
            attempts = self._reconnect_attempts.get(peer_id, 0)
            if attempts >= self._max_reconnect_attempts:
                saved = self._peer_addresses.pop(peer_id, None)
                self._reconnect_attempts.pop(peer_id, None)
                if saved:
                    logger.warning(
                        "[%s] gave up reconnecting to %s:%d after %d attempts",
                        peer_id[:12], saved[1], saved[2], self._max_reconnect_attempts,
                    )
                return
            delay = max(MIN_RECONNECT_DELAY, min(2 ** attempts, MAX_RECONNECT_BACKOFF))
            self._reconnect_attempts[peer_id] = attempts + 1
            logger.debug(
                "[%s] scheduling reconnect attempt %d/%d in %.0fs",
                peer_id[:12], attempts + 1, self._max_reconnect_attempts, delay,
            )
            # Atomically replace any existing timer: pop + insert under one
            # lock so two concurrent calls can't leak a stale timer.
            old = self._reconnect_timers.pop(peer_id, None)
            timer = threading.Timer(delay, self._try_reconnect, args=(peer_id,))
            timer.daemon = True
            self._reconnect_timers[peer_id] = timer
        if old:
            logger.debug("[%s] cancelled previous reconnect timer", peer_id[:12])
            old.cancel()
        timer.start()

    def _try_reconnect(self, peer_id: str):
        with self._lock:
            # This timer just fired — remove it from the dict so it doesn't
            # linger (a later _schedule_reconnect would otherwise try to
            # cancel a timer that already ran).
            self._reconnect_timers.pop(peer_id, None)
            if peer_id in self._peers:
                logger.debug(
                    "[%s] reconnect timer fired but peer already connected — skipping",
                    peer_id[:12],
                )
                self._reconnect_attempts.pop(peer_id, None)
                return
            # Resolve hashed mDNS ID → real device_id so we don't reconnect
            # when the peer is already connected under its real ID.
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id and real_id in self._peers:
                logger.debug(
                    "[%s] reconnect timer fired but peer connected under real ID %s — skipping",
                    peer_id[:12], real_id[:12],
                )
                self._reconnect_attempts.pop(peer_id, None)
                return
            if not self._running:
                return
            saved = self._peer_addresses.get(peer_id)
            if not saved:
                logger.debug(
                    "[%s] reconnect timer fired but no saved address — skipping",
                    peer_id[:12],
                )
                return
            peer_name, address, port = saved
        logger.info("[%s] reconnect timer fired — reconnecting to %s:%d", peer_id[:12], address, port)
        self.connect_to_peer(peer_id, peer_name, address, port)

    def _health_check_loop(self):
        """Periodically check connection health and detect sleep/wake events."""
        while self._running:
            time.sleep(15)
            if not self._running:
                break

            now = time.monotonic()
            gap = now - self._last_health_tick
            self._last_health_tick = now

            if gap > 60:
                logger.info(
                    "Sleep/wake detected (gap=%.0fs), recovering %d connections",
                    gap, len(self._peers),
                )
                self._handle_wake()
                if self._on_wake:
                    try:
                        self._on_wake()
                    except Exception as e:
                        logger.debug("on_wake callback error: %s", e)
            else:
                with self._lock:
                    peers = list(self._peers.items())
                for peer_id, conn in peers:
                    if not conn.health_check():
                        logger.warning(
                            "[%s] health check FAILED — disconnecting", peer_id[:12],
                        )
                        conn.set_on_disconnect(None)
                        conn.stop()
                        # Clean up and schedule reconnect — we bypassed
                        # _on_peer_disconnected so the stale entry won't
                        # linger in _peers and cause repeated failures.
                        self._on_peer_disconnected(peer_id, conn)

    def _handle_wake(self):
        """Disconnect stale connections and reconnect to previously known peers."""
        with self._lock:
            stale_peers = list(self._peers.values())
            self._peers.clear()
            saved_addresses = dict(self._peer_addresses)
            # Cancel stale reconnect timers so they don't race with
            # the wake-initiated reconnections below.
            for timer in self._reconnect_timers.values():
                timer.cancel()
            self._reconnect_timers.clear()
            self._reconnect_attempts.clear()
        logger.info(
            "Wake recovery: clearing %d stale connections, reconnecting to %d known peers",
            len(stale_peers), len(saved_addresses),
        )
        for conn in stale_peers:
            try:
                conn.set_on_disconnect(None)
                conn.stop()
            except Exception:
                pass
        for peer_id, (name, address, port) in saved_addresses.items():
            lookup = self._hash_to_real_id.get(peer_id, peer_id)
            if not self._pairing_mgr.is_peer_paired(lookup):
                logger.debug("Wake recovery: skipping unpaired peer %s", name)
                continue
            logger.info("Wake recovery: reconnecting to %s", name)
            self.connect_to_peer(peer_id, name, address, port)

    def _accept_loop(self, ssl_context: ssl.SSLContext):
        while self._running:
            client_sock = None
            try:
                client_sock, addr = self._server_sock.accept()
                logger.info("TCP accepted from %s:%d", addr[0], addr[1])
                client_sock.settimeout(15)  # TLS handshake timeout
                try:
                    ssl_sock = ssl_context.wrap_socket(client_sock, server_side=True)
                    logger.info("TLS handshake OK with %s:%d", addr[0], addr[1])
                except ssl.SSLError as e:
                    logger.warning("TLS handshake failed from %s:%d: %s", addr[0], addr[1], e)
                    client_sock.close()
                    continue

                peer_id = ""
                peer_name = ""
                peer_cert_pem = ""

                # Exchange identity at application level: send our cert,
                # then read the client's cert from its identity frame.
                identity = self._pairing_mgr.get_identity()
                self._send_identity(ssl_sock, identity.certificate_pem)

                client_cert_data = self._recv_identity(ssl_sock)
                if client_cert_data:
                    peer_cert_pem = client_cert_data.decode("ascii")
                    peer_cert = x509.load_pem_x509_certificate(peer_cert_pem.encode())

                    try:
                        cn_attrs = peer_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                        if cn_attrs:
                            peer_id = cn_attrs[0].value
                    except Exception:
                        pass

                    try:
                        ou_attrs = peer_cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
                        if ou_attrs:
                            peer_name = ou_attrs[0].value
                    except Exception:
                        pass

                    logger.info(
                        "Identity from %s:%d — peer_id=%s, peer_name=%s",
                        addr[0], addr[1], peer_id[:12] if peer_id else "N/A", peer_name or "N/A",
                    )
                    # Refuse connections from peers the user has explicitly
                    # rejected or forgotten.
                    if peer_id and peer_id in self._rejected_peer_ids:
                        logger.info(
                            "[%s] incoming connection from rejected peer — refusing",
                            peer_id[:12],
                        )
                        self._send_rejection(ssl_sock)
                        ssl_sock.close()
                        continue
                    was_paired = self._pairing_mgr.is_peer_paired(peer_id)
                    self._pairing_mgr.add_peer(
                        peer_id, peer_name,
                        peer_cert_pem,
                        paired=was_paired,
                    )
                    if not was_paired and peer_id:
                        try:
                            shared_code = self._pairing_mgr.generate_shared_pairing_code(peer_id)
                            logger.info(
                                "[%s] pairing code: %s**** — verify on both devices",
                                peer_name or peer_id, shared_code[:4],
                            )
                        except Exception as e:
                            logger.debug("Could not generate shared pairing code: %s", e)
                else:
                    logger.warning("No identity frame from %s:%d — anonymous connection", addr[0], addr[1])

                display_id = peer_id or "unknown"
                peer_fp2 = self._pairing_mgr.get_peer_fingerprint(peer_id) if peer_id else ""
                conn = PeerConnection(display_id, peer_name or str(addr), ssl_sock,
                                      peer_fingerprint=peer_fp2, enc_mgr=self._enc_mgr,
                                      pairing_mgr=self._pairing_mgr)
                conn.set_on_message(self._on_peer_message)
                conn.set_on_disconnect(self._on_peer_disconnected)
                conn.start()
                # Prevent the outer except handler from closing client_sock
                # now that PeerConnection owns the ssl_sock (which wraps it).
                client_sock = None

                # Socket shutdown/close and health checks do I/O — collect the
                # connections to stop under the lock, then stop them outside it.
                to_stop: list = []
                existing = None
                with self._lock:
                    if not self._running:
                        # Server was stopped during TLS handshake — clean up
                        logger.debug("Server stopped, discarding accepted connection from %s", addr)
                        conn.set_on_disconnect(None)
                        conn.stop()
                        return
                    if peer_id:
                        # Cancel any pending reconnect — the peer is reaching out
                        # to us, so we don't need to reconnect to them.
                        timer = self._reconnect_timers.pop(peer_id, None)
                        if timer:
                            logger.debug(
                                "[%s] cancelled pending reconnect timer",
                                peer_id[:12],
                            )
                            timer.cancel()
                        self._reconnect_attempts.pop(peer_id, None)
                        # Map hashed mDNS IDs to the real peer_id so the UI
                        # can deduplicate.
                        for hash_id, (_, h_addr, _) in list(self._peer_addresses.items()):
                            if h_addr == addr[0] and hash_id != peer_id:
                                logger.debug(
                                    "Mapping hash_id %s → real_id %s (IP match: %s)",
                                    hash_id[:12], peer_id[:12], addr[0],
                                )
                                self._hash_to_real_id[hash_id] = peer_id
                        # Tiebreaker: the device with lower device_id acts as
                        # client — its outgoing connection wins. This incoming
                        # connection IS the peer's outgoing. If the peer has
                        # the lower ID, this incoming wins over any existing.
                        existing = self._peers.get(peer_id)
                        if existing is None:
                            self._peers[peer_id] = conn
                            logger.debug(
                                "[%s] stored in _peers[%s] (total peers: %d)",
                                peer_name or peer_id, peer_id[:12], len(self._peers),
                            )
                        elif self._device_id > peer_id:
                            # Peer is lower — their outgoing (this incoming) wins.
                            logger.info(
                                "[%s] tiebreaker: peer's outgoing wins (peer=%s < we=%s) — replacing",
                                peer_id[:12], peer_id[:12], self._device_id[:12],
                            )
                            self._peers.pop(peer_id)
                            existing.set_on_disconnect(None)
                            to_stop.append(existing)
                            self._peers[peer_id] = conn
                            existing = None
                        # else: we are lower — our outgoing wins unless the
                        # existing connection is dead. Check its health outside
                        # the lock (health_check does non-blocking I/O).
                    else:
                        # Track anonymous connections so they can be cleaned up
                        anon_key = f"__anon__{addr[0]}:{addr[1]}"
                        self._peers[anon_key] = conn
                        logger.debug("Stored anonymous connection under %s", anon_key)
                if existing is not None and not existing.health_check():
                    # We are lower but existing connection is dead
                    # (e.g. our outgoing was closed by peer in a
                    # previous race round). Keep the incoming so
                    # we don't lose both connections.
                    with self._lock:
                        if self._peers.get(peer_id) is existing:
                            logger.info(
                                "[%s] tiebreaker: we should win but existing is dead — keeping incoming",
                                peer_id[:12],
                            )
                            self._peers.pop(peer_id)
                            existing.set_on_disconnect(None)
                            to_stop.append(existing)
                            self._peers[peer_id] = conn
                            existing = None
                        elif peer_id not in self._peers:
                            # Existing was removed concurrently — keep incoming.
                            self._peers[peer_id] = conn
                            existing = None
                if existing is not None:
                    # We are lower — our outgoing wins, discard incoming.
                    logger.info(
                        "[%s] tiebreaker: our outgoing wins (we=%s < peer=%s) — discarding incoming",
                        peer_id[:12], self._device_id[:12], peer_id[:12],
                    )
                    conn.set_on_disconnect(None)
                    to_stop.append(conn)
                for c in to_stop:
                    c.stop()

                logger.info("Accepted connection from %s:%d [%s]", addr[0], addr[1], peer_id[:12] if peer_id else "N/A")

            except socket.timeout:
                continue
            except CertificateChangedError:
                # Expected = the stored fingerprint of the previously paired
                # cert; received = the fingerprint of the new cert that just
                # triggered the alert. Populate both so the alert is useful.
                expected_fp = self._pairing_mgr.get_peer_fingerprint(peer_id) if peer_id else ""
                received_fp = fingerprint_pem(peer_cert_pem) if peer_cert_pem else ""
                logger.error(
                    "SECURITY: Incoming connection presented changed certificate — "
                    "possible MITM attack! Connection rejected. Expected fp: %s Got: %s",
                    expected_fp[:16] if expected_fp else "n/a",
                    received_fp[:16] if received_fp else "n/a",
                )
                if self._on_security_alert:
                    self._on_security_alert(peer_name or "unknown", expected_fp, received_fp)
                if client_sock:
                    try:
                        client_sock.close()
                    except Exception:
                        pass
            except Exception as e:
                if self._running:
                    logger.warning("Accept error: %s: %s", type(e).__name__, e, exc_info=True)
                if client_sock:
                    try:
                        client_sock.close()
                    except Exception:
                        pass

