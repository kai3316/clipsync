"""TLS-encrypted TCP connection management for peer-to-peer sync."""

import contextlib
import logging
import os
import socket
import ssl
import struct
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from internal.config.config import config_dir
from internal.protocol.codec import (
    UNPAIRED_GATE_MSG_TYPES,
    decode_message,
)
from internal.security.encryption import is_encrypted
from internal.security.handshake import (
    PROOF_VERSION,
    proof_state,
    should_refuse_unproven,
)
from internal.security.pairing import CertificateChangedError, PairingManager, fingerprint_pem
from internal.transport.ids import peer_id_hash
from internal.transport.ids import sanitize_peer_str as _sanitize_peer_str

logger = logging.getLogger(__name__)


class PortInUseError(OSError):
    """Raised when the TCP port is already in use by another process."""

    def __init__(self, port: int):
        self.port = port
        super().__init__(f"Port {port} is already in use")


MAX_FRAME_SIZE = 10 * 1024 * 1024  # 10 MB
FRAME_HEADER_SIZE = 4
DATA_TIMEOUT = 30.0  # socket read timeout
MAX_RECONNECT_ATTEMPTS = 10
MAX_RECONNECT_BACKOFF = 30
MIN_RECONNECT_DELAY = (
    3  # minimum 3s before first reconnect to allow accept_loop to resolve bidirectional races
)
MAX_IDLE_SECONDS = 120.0  # max time with no received bytes before a connection is declared dead
KEEPALIVE_IDLE = 30  # TCP keepalive: idle seconds before probes start
KEEPALIVE_INTERVAL = 10  # TCP keepalive: seconds between probes
KEEPALIVE_COUNT = 6  # TCP keepalive: failed probes before declaring the connection dead

# Total lifetime for unauthenticated ("anonymous") incoming connections.
# They never sent an identity frame, so they can never carry sync traffic —
# without this cap a LAN device could hold threads/file-descriptors hostage
# with silent TLS connections (they pass health checks: healthy TCP keeps
# SO_ERROR clear).
ANON_CONN_MAX_LIFE = 60

# Rejection frame sent after identity exchange when the accepting side
# refuses the connection (peer in _rejected_peer_ids, or a pinned device that
# could not prove it holds its certificate's key).  The connecting side reads
# this before creating a PeerConnection and knows not to schedule a reconnect.
#
# The marker carries a one-byte reason after it, because the *reason* decides
# what the other end should say and do, and it cannot work that out itself:
# "this device forgot us" and "this device will not trust us until we re-pair"
# look identical from the dialling side -- it has the identity it always had and
# the peer simply stopped accepting it.  Without the code the user gets one
# vague toast for two different situations, one of which has an action
# ("re-pair with it") and one of which does not.
_REJECT_MARKER = b"\xff\xff\xff\xffRJCT"

# Rejection reasons.  Zero is reserved for a marker from a build that predates
# the reason byte, so a peer older than this field is reported as unspecified
# rather than silently mislabelled as something specific.
REJECT_UNSPECIFIED = 0
# The peer removed this device; pairing must be redone from *its* side.
REJECT_REMOVED = 1
# The peer has this device pinned, but this device could not prove it holds the
# key behind the certificate it presented.  Either it is an older build (the
# peer cannot tell that from an impostor) or it is copying that certificate.
# The fix is an explicit re-pair on both sides, which replaces the pin.
REJECT_IDENTITY_UNPROVEN = 2

_REJECT_REASONS = {
    REJECT_UNSPECIFIED: "unspecified",
    REJECT_REMOVED: "removed",
    REJECT_IDENTITY_UNPROVEN: "identity-unproven",
}

# Appended to the identity frame's PEM by a dialer whose connection is not a
# pairing request — a chat invite, an update offer, an update fetch.  The
# accepting side reads it and skips generating a shared code, which is the one
# thing that puts a pairing card, and a "wants to pair" notice, in front of a
# user who asked for neither.
#
# ``connect_to_peer(no_auto_pairing=True)`` used to be a promise only the dialer
# kept: the flag never crossed the wire, so the accepting end generated a code
# anyway and the offer arrived looking like a pairing request.  A PEM comment
# line is the whole mechanism — it rides the frame that already exists, an older
# peer parses the PEM and ignores what follows it, and a newer peer strips the
# tail before the PEM reaches anything that reads it.  Neither fingerprint nor
# pin can see it: both hash the DER.
NO_PAIRING_MARKER = "\n#clipsync-no-pairing"

# Prepended to the identity frame's PEM by a dialer: the port its own server
# listens on.  The accepting side has the dialer's address — ``addr[0]`` off the
# socket — but not the port to dial it back on: ``addr[1]`` is the ephemeral
# port the call came FROM, and a connection to it reaches nothing.  So a device
# that has only ever been dialed had no way back, and a peer that ended a call
# (a rejected pairing offer, a manual disconnect) stayed unreachable until it
# happened to dial again — which, on a network where its own mDNS sighting of
# us is missing, is never.
#
# Same mechanism as the marker below, at the other end of the frame: a PEM
# comment line.  A reader that does not know the field still loads the same
# certificate (a PEM parser scans for the BEGIN line), and the fingerprint — so
# the pin, and the certificate-change alarm — is taken from the DER bytes, which
# the decoration cannot reach.  A peer that sends no line means "unknown", not
# "port 0": an older build's frame is exactly what it always was.
LISTEN_PORT_PREFIX = "#clipsync-listen="

# Prepended to the identity frame's PEM: the name this device answers to now.
# The certificate carries one too, in its OU, and that one is frozen — it is
# written when the identity is minted, and reissuing the certificate on a rename
# would change the fingerprint every paired peer pins.  A device renamed in the
# settings therefore kept introducing itself under its old name for the life of
# the certificate, and the peers that read it from there — everything that never
# hears the mDNS advertisement, which is every dial across a subnet, a VPN or a
# flaky LAN — listed it as a name its user had already changed.
#
# Same mechanism as the port above: a PEM comment line, before the PEM.  A
# reader that does not know the field loads the same certificate (a PEM parser
# scans for the BEGIN line) and pins the same DER, and a peer that sends no line
# means "no name to say", which is an older build's frame exactly as it was.
NAME_PREFIX = "#clipsync-name="

# The handshake's proof of key possession, as two more PEM comment lines: the
# nonce this side chose, and a signature over *(their nonce, our nonce)* made with
# the key belonging to the certificate in the same frame.  Same mechanism as the
# fields above, so an older peer loads the certificate and ignores both lines, and
# a frame without them is that older frame exactly.
#
# What they buy: a certificate is otherwise only *claimed*, and a claim is not
# enough to be taken for a paired device.  The accepting side never asks for the
# client certificate (`CERT_NONE`), so the certificate actually presented is
# unavailable -- and Python offers no way to request one without validating it:
# `CERT_OPTIONAL` aborts the handshake on a self-signed certificate, which every
# ClipSync device's is.  So the proof is made here, over the Ed25519 key the
# certificate already carries, and the nonce is what makes it worth something: it
# is chosen microseconds before it is sent and given to nobody else, so a
# signature over it cannot be a recording of an earlier handshake.
NONCE_PREFIX = "#clipsync-nonce="
PROOF_PREFIX = "#clipsync-proof="
# The handshake version this frame was written by, so a peer can tell a build
# that promises a proof from one that predates the field.  A gate keyed on
# "did the proof arrive" is a gate an attacker opens by omitting it; a gate
# keyed on the *claim* is not.  See `handshake.PROOF_VERSION`.
HANDSHAKE_VERSION_PREFIX = "#clipsync-v="

# The first release that reads the marker above, and therefore the floor for
# every dial this build makes on a user's behalf: a call to an older peer is a
# pairing card on that screen, which is the one outcome the marker exists to
# prevent, so a peer older than this one is not called at all.
#
# It is a release rather than something the peer advertises, because the advert
# has no word for it.  ``app`` was standing in for one — only 1.0.13 and later
# broadcast a field at all — and that stand-in is gone now that the update offer
# no longer asks which application a peer runs.
NO_PAIRING_MARKER_SINCE = "1.0.12"

# How long a manual disconnect keeps the peer's reconnects out.
#
# ``disconnect_peer(reject=True)`` used to park the peer in
# ``_rejected_peer_ids`` — the same set, and the same rejection marker, as
# ``forget_peer``.  The two are not the same act: one means "not now", the
# other means "you are removed".  Sharing the state made a device whose user
# pressed 断开连接 indistinguishable from one that had deleted this device, so
# the other side reported a removal that never happened and — because nothing
# but this device's own outbound dial clears that set — kept reporting it for
# the rest of the session, through every later attempt to pair.
#
# A hold expires, so the peer comes back on its own; a removal does not, and is
# reported as one.  The window is longer than the ~3-30 s reconnect backoff it
# has to outlast, and short enough that a user who disconnects and then changes
# their mind is not left waiting.
DISCONNECT_HOLD_SECONDS = 180.0

# A health tick this far out of line with the expected 15 s interval means the
# process was frozen.
WAKE_GAP_SECONDS = 60.0


def _looks_like_wake(wall_gap: float, mono_gap: float) -> bool:
    """True if the health loop was frozen (system sleep), not merely re-clocked.

    Neither clock alone is enough.  ``time.monotonic()`` includes suspend time
    on Windows and macOS, so a large ``mono_gap`` is direct proof the loop
    stopped running; on Linux ``CLOCK_MONOTONIC`` pauses during suspend, and the
    only evidence is the wall clock having run far ahead of it.  Reading the
    wall gap alone (the original check) fired a full reconnect sweep on any
    forward NTP step, and a *backward* correction made the gap negative — so a
    genuine sleep went unnoticed and every peer socket stayed dead until the
    user restarted the app.
    """
    if mono_gap > WAKE_GAP_SECONDS:
        return True
    return (wall_gap - mono_gap) > WAKE_GAP_SECONDS


class PeerConnection:
    """Represents a TLS connection to a single peer."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        sock: socket.socket,
        peer_fingerprint: str = "",
        enc_mgr=None,
        pairing_mgr=None,
        pending_recv: bytes = b"",
        is_anonymous: bool = False,
    ):
        self.device_id = device_id
        self.device_name = device_name
        self.is_anonymous = is_anonymous
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
        self._remote_closed = False
        self.created_at = time.monotonic()
        self._rejected_by_peer = False
        self._auth_failures = 0
        # Consecutive frames that arrived in an unexpected form while app-layer
        # encryption is enabled (undecryptable or plaintext) — enough of them
        # means the session's key state is broken, so the connection is closed
        # and the normal reconnect machinery takes over.
        self._frame_failures = 0
        # Set once a frame pattern consistent with an encryption-setting
        # mismatch between paired peers is detected (we encrypt but the peer
        # sends plaintext, or we don't encrypt but the peer sends encrypted
        # frames).  The transport backs off instead of reconnecting into the
        # drop-and-tear-down loop.
        self._crypto_mismatch = False
        # Bytes consumed by the post-handshake rejection probe that turned
        # out not to be a rejection marker — replayed by _recv_exact so the
        # first application frame stays intact.
        self._pending_recv = pending_recv
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
        with contextlib.suppress(Exception):
            self._sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            self._sock.close()

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
                logger.debug(
                    "[%s] App-layer encrypted frame payload (%d bytes on wire)",
                    self.device_name,
                    len(data),
                )
            else:
                logger.debug(
                    "[%s] Sending unencrypted frame (%d bytes) — enc_mgr=%s peer_fp=%s",
                    self.device_name,
                    len(data),
                    "set" if self._enc_mgr else "none",
                    "set" if self._peer_fingerprint else "empty",
                )
            if len(data) > MAX_FRAME_SIZE:
                # Refuse oversized frames here so the receiver never sees a
                # frame_len that trips its MAX_FRAME_SIZE guard and disconnects.
                logger.warning(
                    "[%s] refusing to send oversized frame (%d bytes > %d)",
                    self.device_name,
                    len(data),
                    MAX_FRAME_SIZE,
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
            # EOF: the recv loop sets _remote_closed when it observes an empty
            # recv (clean FIN). SO_ERROR doesn't surface a clean close, and
            # SSLSocket rejects MSG_PEEK, so read the flag instead.
            return not self._remote_closed
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
                    # A rejected peer's marker can arrive after the short
                    # handshake probe gave up (congested LAN).  Recognize it
                    # here so the disconnect path clears the saved address
                    # instead of reconnecting into an endless reject loop.
                    if frame_len == 0xFFFFFFFF:
                        tail = self._recv_exact(len(b"RJCT"))
                        if tail == b"RJCT":
                            self._rejected_by_peer = True
                            end_reason = "rejected by peer"
                            break
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
                            self.device_name,
                            len(pt),
                        )
                        payload = pt
                        self._frame_failures = 0
                    elif is_encrypted(payload):
                        # Auth tag mismatch — fail closed: skip the frame instead
                        # of feeding the encrypted bytes to decode_message.
                        self._auth_failures += 1
                        self._frame_failures += 1
                        logger.warning(
                            "[%s] App-layer decrypt FAILED — auth tag mismatch! "
                            "Possible tampering or wrong password.",
                            self.device_name,
                        )
                        if self._frame_failures >= 5:
                            end_reason = (
                                "repeated app-layer decrypt failures (wrong key state or tampering)"
                            )
                            break
                        if self._auth_failures >= 3:
                            logger.error(
                                "[%s] repeated auth failures (%d) — peer may be "
                                "using a different key or frames are being tampered with",
                                self.device_name,
                                self._auth_failures,
                            )
                        continue
                    else:
                        # Encrypted mode + plaintext frame: fail closed.  A peer
                        # that suddenly sends unencrypted frames either lost its
                        # key state (e.g. re-paired on one side only) or has
                        # app-layer encryption DISABLED while we have it enabled
                        # (an encryption-setting mismatch between paired
                        # devices) — acting on that data would be unsafe.  Flag
                        # the likely mismatch so the disconnect path backs off
                        # instead of reconnecting into a tear-down loop.
                        self._frame_failures += 1
                        logger.warning(
                            "[%s] Dropped unencrypted frame (%d bytes) while "
                            "encryption is enabled (%d consecutive) — peer "
                            "appears to have encryption DISABLED; check that "
                            "both devices use the same encryption setting",
                            self.device_name,
                            len(payload),
                            self._frame_failures,
                        )
                        if self._frame_failures >= 5:
                            self._crypto_mismatch = True
                            end_reason = (
                                "encryption mismatch: peer sending unencrypted "
                                "frames while we have encryption enabled"
                            )
                            break
                        continue
                else:
                    if is_encrypted(payload):
                        # No enc_mgr but the peer sent an encrypted frame — the
                        # paired devices disagree on app-layer encryption (peer
                        # has it ENABLED, we have it DISABLED).  Such frames can
                        # never be parsed, so drop them and back off once the
                        # pattern is clear instead of silently discarding sync.
                        self._frame_failures += 1
                        logger.warning(
                            "[%s] Dropped encrypted frame (%d bytes) while "
                            "encryption is disabled (%d consecutive) — peer "
                            "appears to have encryption ENABLED; check that "
                            "both devices use the same encryption setting",
                            self.device_name,
                            len(payload),
                            self._frame_failures,
                        )
                        if self._frame_failures >= 5:
                            self._crypto_mismatch = True
                            end_reason = (
                                "encryption mismatch: peer sending encrypted "
                                "frames while we have encryption disabled"
                            )
                            break
                        continue
                    logger.debug(
                        "[%s] Received frame (%d bytes) — no enc_mgr, passing through",
                        self.device_name,
                        len(payload),
                    )

                try:
                    msg = decode_message(payload)
                    if msg and self._on_message:
                        # Incoming pairing gate: only paired peers may send app-level
                        # messages. Pairing happens via the connection's own identity
                        # frames, not decoded app frames, so no legitimate pre-pairing
                        # app frames exist. Dropping them stops unpaired LAN peers
                        # from injecting clipboard / nav_url / file-dialog content.
                        if self._pairing_mgr is not None and not self._pairing_mgr.is_peer_paired(
                            self.device_id
                        ):
                            # Anonymous connections never sent an identity frame —
                            # no pairing and no chat (both require a real
                            # certificate identity).  Dropping every app frame here
                            # stops a flood of fresh TLS connections from spamming
                            # chat invites, each with a fresh per-connection rate
                            # budget.
                            if self.is_anonymous:
                                logger.debug(
                                    "[%s] dropping app frame from anonymous connection",
                                    self.device_name,
                                )
                                continue
                            # Pairing lifecycle messages must pass through so a peer
                            # can confirm / reject / unpair even before it is paired
                            # — that is exactly how the two-sided handshake completes.
                            # Nearby-chat messages also pass: they are consent-gated
                            # at the application layer (the receiving user must
                            # explicitly accept each chat invitation before any
                            # content flows). ``file_chunk`` additionally passes so
                            # chat file bytes can reach unpaired peers (the app
                            # router gives chat right-of-first-refusal on it, and
                            # FileTransferManager no-ops unknown transfer_ids, so
                            # clipboard transfers still cannot be initiated by
                            # unpaired peers). Every other app frame from an
                            # unpaired peer is dropped.
                            if getattr(msg, "msg_type", "clipboard") not in UNPAIRED_GATE_MSG_TYPES:
                                logger.warning(
                                    "[%s] dropping %s frame from unpaired peer (device_id=%s)",
                                    self.device_name,
                                    getattr(msg, "msg_type", "clipboard"),
                                    self.device_id[:12],
                                )
                                continue
                        # Pass this connection's peer id so handlers can respond to
                        # the sender (e.g. file-transfer acks/chunks) instead of
                        # broadcasting to every peer.
                        self._on_message(msg, self.device_id)
                except Exception as e:
                    # One bad frame must not take down the connection: log and
                    # drop it, keep the socket alive.  A decode or handler bug
                    # is contained to this frame instead of tearing down the
                    # connection + scheduling a reconnect (which drops every
                    # peer and redials).
                    logger.warning(
                        "[%s] dropping frame that failed to process (%s: %s)",
                        self.device_name,
                        type(e).__name__,
                        e,
                    )
                    continue

            except (ConnectionError, OSError) as e:
                end_reason = f"ConnectionError: {e}"
                break
            except Exception as e:
                end_reason = f"Exception: {type(e).__name__}: {e}"
                break

        logger.info(
            "[%s] recv_loop ended: %s (conn=%s)", self.device_name, end_reason, hex(id(self))
        )
        self._running = False
        if self._on_disconnect:
            logger.debug(
                "[%s] calling on_disconnect callback (conn=%s)", self.device_name, hex(id(self))
            )
            self._on_disconnect(self.device_id, self)

    def _recv_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        # Replay any bytes the rejection probe pushed back before touching
        # the socket again.
        if self._pending_recv:
            take = min(n - len(buf), len(self._pending_recv))
            buf.extend(self._pending_recv[:take])
            self._pending_recv = self._pending_recv[take:]
            self._last_recv_time = time.monotonic()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    logger.info(
                        "[%s] recv returned empty bytes (remote closed connection)",
                        self.device_name,
                    )
                    self._remote_closed = True
                    return None
                buf.extend(chunk)
                self._last_recv_time = time.monotonic()
            except TimeoutError:
                if not self._running:
                    return None
                # Re-check the half-open deadline here, not only in _recv_loop:
                # waiting for a frame header parks inside this loop for as long
                # as the peer stays quiet, so the check at the top of _recv_loop
                # never ran on an idle connection and MAX_IDLE_SECONDS was dead
                # code exactly in the case it exists for.  Mid-frame this cannot
                # misfire — _last_recv_time advances with every byte received.
                if self._max_idle_expired():
                    logger.info(
                        "[%s] no bytes for %ds and the socket reports an error "
                        "— treating the connection as dead",
                        self.device_name,
                        int(MAX_IDLE_SECONDS),
                    )
                    return None
                continue
            except Exception as e:
                logger.info("[%s] recv exception: %s: %s", self.device_name, type(e).__name__, e)
                return None
        return bytes(buf)


class TransportManager:
    """Manages peer connections — server and client side."""

    # How many accepted connections may be mid-handshake at once.  Each one is
    # a thread that lives for up to the TLS and identity timeouts (~25 s), so
    # without a ceiling anything on the network that opens sockets faster than
    # this machine can handshake them is an unbounded thread count on a desktop
    # app -- the listening port is on the LAN, and a LAN is not necessarily
    # ours.  Well above what a household of peers needs, and low enough that a
    # flood cannot spend the machine's memory before the backlog (listen(5))
    # and this ceiling start refusing work.
    MAX_ACCEPT_WORKERS = 64

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
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._pending_sockets: dict[threading.Thread, set[socket.socket]] = {}
        self._connections: set[PeerConnection] = set()
        self._callbacks: dict[threading.Thread, int] = {}
        self._callback_done = threading.Condition(self._lock)
        self._stop_calls = 0
        self._last_health_tick = 0.0
        self._last_health_mono = 0.0
        self._peer_addresses: dict[str, tuple[str, str, int]] = {}
        # peer_id -> (name, address, port) for peers that dialed US, learned
        # from the call itself.  Kept apart from _peer_addresses because the
        # two answer different questions: that map is where auto-reconnect
        # dials from (_schedule_reconnect, wake recovery), and a peer whose call
        # we ended — a rejected pairing offer, a manual 断开连接 — must not be
        # called again behind the user's back.  This one is for the user's own
        # 配对/连接设备 click, and for the row that offers it.
        self._callback_addresses: dict[str, tuple[str, str, int]] = {}
        self._reconnect_attempts: dict[str, int] = {}
        self._reconnect_timers: dict[str, threading.Timer] = {}
        # peer_id -> (real_id, fingerprint refused on).  A cert-pin mismatch
        # suppresses auto-reconnect; this is what lets the health loop notice
        # the user re-pairing and lift the suppression.
        self._cert_pin_blocked: dict[str, tuple[str, str]] = {}
        self._max_reconnect_attempts = max_reconnect_attempts
        self._hash_to_real_id: dict[str, str] = {}
        self._rejected_peer_ids: set[str] = set()
        # peer id -> monotonic deadline: refused for a while, not for good.
        # See DISCONNECT_HOLD_SECONDS.
        self._connect_holds: dict[str, float] = {}
        self._enc_mgr = None
        self._on_security_alert: Callable | None = None
        self._on_connect_rejected: Callable | None = None

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

        Called with (peer_name, peer_id, expected_fingerprint,
        received_fingerprint, new_cert_pem).  ``new_cert_pem`` is the
        certificate that triggered the alert (empty if unknown).
        """
        self._on_security_alert = callback

    def set_on_connect_rejected(self, callback: Callable):
        """Set a callback fired when a peer refuses our connection attempt.

        The accepting side sends its rejection marker right after its
        identity frame when it refuses us (typically its user removed or
        forgot this device).  Called with ``(peer_name, peer_id)`` from the
        connect thread; never raises.
        """
        self._on_connect_rejected = callback

    def _notify_connect_rejected(
        self, peer_name: str, peer_id: str, reason: int = REJECT_UNSPECIFIED
    ) -> None:
        cb = self._on_connect_rejected
        if cb is None:
            return
        try:
            self._invoke_callback(cb, peer_name, peer_id, reason)
        except Exception:
            logger.debug("on_connect_rejected callback failed", exc_info=True)

    def _invoke_callback(self, callback, *args):
        """Admit callbacks atomically with stop, without holding a lock in user code."""
        current = threading.current_thread()
        with self._lock:
            if self._stopped.is_set() or callback is None:
                return
            self._callbacks[current] = self._callbacks.get(current, 0) + 1
        try:
            return callback(*args)
        finally:
            with self._callback_done:
                self._callbacks[current] -= 1
                if not self._callbacks[current]:
                    del self._callbacks[current]
                self._callback_done.notify_all()

    def _prune_workers(self):
        # Called under _lock; retain even replaced connections until recv exits.
        self._workers = {t for t in self._workers if t.is_alive()}
        self._connections = {
            c for c in self._connections if c._recv_thread and c._recv_thread.is_alive()
        }

    def _handshakes_full(self) -> bool:
        """Whether MAX_ACCEPT_WORKERS accepted connections are already running."""
        with self._lock:
            # The set is pruned of finished threads by _start_worker, but this
            # is asked before that runs, so prune here too: a stale entry is a
            # slot a peer would be refused for.
            live = sum(
                1
                for worker in self._workers
                if worker.name == "clipsync-accept" and worker.is_alive()
            )
        return live >= self.MAX_ACCEPT_WORKERS

    def _start_worker(self, target, args=(), name=None):
        with self._lock:
            if not self._running:
                return None
            self._prune_workers()

            def run():
                try:
                    target(*args)
                finally:
                    with self._lock:
                        self._pending_sockets.pop(threading.current_thread(), None)

            worker = threading.Thread(target=run, daemon=True, name=name)
            self._workers.add(worker)
            try:
                worker.start()
            except BaseException:
                self._workers.discard(worker)
                raise
            return worker

    @staticmethod
    def _close_socket(sock):
        with contextlib.suppress(Exception):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            sock.close()

    def _track_socket(self, sock):
        with self._lock:
            if self._running:
                self._pending_sockets.setdefault(threading.current_thread(), set()).add(sock)
                return
        self._close_socket(sock)
        raise OSError("Transport stopped")

    def _dial(self, address, port, timeout):
        """Connect a socket this manager owns, so stopping can interrupt it.

        `socket.create_connection` builds the socket itself and only hands it back
        once connected, which leaves the whole dial -- resolve, then connect, both
        of which can outlast a stop -- with nothing registered to close.  On a stop
        the socket sets were closed, the dial came back connected a moment later
        and registered a live peer, and the drain then waited for a thread that was
        never going to be interrupted.

        Measured, that is what the shutdown defect was: the drain reported
        `clipsync-connect` still inside `create_connection` at the five-second
        deadline, the runtime never released ownership, and the caller logged a
        bare timeout.  The socket is registered *before* it connects, so
        `stop_server` closing it aborts the dial where it stands.
        """
        last = None
        for family, kind, proto, _, sockaddr in socket.getaddrinfo(
            address, port, type=socket.SOCK_STREAM
        ):
            sock = socket.socket(family, kind, proto)
            sock.settimeout(timeout)
            try:
                # Raises OSError("Transport stopped") if a stop landed first.
                self._track_socket(sock)
            except BaseException:
                sock.close()
                raise
            try:
                sock.connect(sockaddr)
                # A closed socket can still report a successful connect: the
                # peer's listen backlog accepted it before the close landed, and
                # `connect` returns as soon as the kernel has it queued.  Handing
                # that back would be worse than a failed dial -- the caller goes
                # on to wrap it, and every later operation fails somewhere less
                # obvious than here.  Re-checked under the same lock that closes
                # them, so a stop cannot slide in between.
                with self._lock:
                    if not self._running or sock.fileno() == -1:
                        raise OSError("Transport stopped")
                return sock
            except OSError as exc:
                # Closed from under us: that is a stop, and it is not a dial
                # failure to retry against the next address.
                if not self._running or sock.fileno() == -1:
                    sock.close()
                    raise OSError("Transport stopped") from None
                last = exc
                self._forget_socket(sock)
                sock.close()
        if last is not None:
            raise last
        raise OSError(f"No address for {address}:{port}")

    def _forget_socket(self, sock):
        with self._lock:
            group = self._pending_sockets.get(threading.current_thread())
            if group is not None:
                group.discard(sock)

    def _wrap_socket(self, context, sock, **kwargs):
        # Register the SSL wrapper before its blocking handshake: wrap_socket
        # detaches the raw fd, so closing only the raw socket cannot interrupt it.
        with self._lock:
            if not self._running:
                raise OSError("Transport stopped")
            wrapped = context.wrap_socket(sock, do_handshake_on_connect=False, **kwargs)
            self._pending_sockets.setdefault(threading.current_thread(), set()).add(wrapped)
        try:
            wrapped.do_handshake()
        except BaseException:
            self._close_socket(wrapped)
            raise
        return wrapped

    def _start_peer(self, conn):
        with self._lock:
            if not self._running:
                conn.stop()
                return False
            conn.set_on_message(lambda *args: self._invoke_callback(self._on_peer_message, *args))
            conn.set_on_disconnect(self._on_peer_disconnected)
            self._connections.add(conn)
            try:
                conn.start()
            except BaseException:
                conn.stop()
                self._connections.discard(conn)
                raise
            self._workers.add(conn._recv_thread)
            return True

    @staticmethod
    def _secure_scratch_dir() -> Path:
        """Per-user scratch directory for temporary key material.

        Uses the platform config directory (not system /tmp) so files are
        never world-readable and survive only as long as the app runs.
        Stale files from previous crashes are cleaned on next startup.
        """
        scratch = config_dir() / ".scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        # On Unix, restrict permissions so only the owner can read
        if os.name != "nt":
            with contextlib.suppress(Exception):
                scratch.chmod(0o700)
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

    def _build_ssl_context(
        self, server_side: bool = True, verify_peer_id: str | None = None
    ) -> ssl.SSLContext:
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
            elif verify_peer_id:
                # A dial to a *specific* device: built without the platform trust
                # store, because `create_default_context(Purpose.SERVER_AUTH)`
                # loads it and `load_verify_locations` only ever *adds* to
                # whatever is there.  The old shape therefore meant
                # `CERT_REQUIRED` accepted a chain to the pinned certificate **or
                # to any of the ~250 public roots on this machine**, with
                # `check_hostname` off so no name had to match either -- a pin
                # that let a publicly-signed impostor terminate the handshake.
                # Measured: the store still held 251 CA roots after the pin was
                # loaded.
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            else:
                ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

            ssl_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            ssl_context.check_hostname = False
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_3

            peer_cert = (
                self._pairing_mgr.get_peer_certificate(verify_peer_id) if verify_peer_id else ""
            )
            if verify_peer_id and peer_cert:
                ssl_context.verify_mode = ssl.CERT_REQUIRED
                ca_path = scratch / f"peer_cert_{uid}.pem"
                try:
                    ca_path.write_text(peer_cert, encoding="ascii")
                    ssl_context.load_verify_locations(cafile=str(ca_path))
                finally:
                    with contextlib.suppress(OSError):
                        ca_path.unlink()
            else:
                # No pin to check against.  CERT_REQUIRED with no CA loaded
                # would fall back to the system trust store — with
                # check_hostname off that accepts ANY publicly-signed cert,
                # which is weaker than the app-layer identity-frame check that
                # follows.  Stay on CERT_NONE and let that check decide.
                if verify_peer_id:
                    logger.debug(
                        "No pinned certificate for %s — TLS verification "
                        "deferred to the identity frame",
                        verify_peer_id[:12],
                    )
                ssl_context.verify_mode = ssl.CERT_NONE

            return ssl_context

        finally:
            for p in [key_path, cert_path]:
                with contextlib.suppress(OSError):
                    p.unlink()

    @staticmethod
    def _send_identity(
        sock: ssl.SSLSocket,
        cert_pem: str,
        no_auto_pairing: bool = False,
        listen_port: int = 0,
        device_name: str = "",
        nonce: str = "",
        proof: str = "",
        version: int = 0,
    ):
        """Send our certificate PEM as an application-level frame.

        *no_auto_pairing* appends :data:`NO_PAIRING_MARKER`, telling the
        accepting side that this connection is not a pairing request.

        *listen_port* prepends the port this device's own server accepts on, so
        the side that takes the call can dial back (see
        :data:`LISTEN_PORT_PREFIX`).  Zero sends nothing.

        *device_name* prepends the name this device answers to now (see
        :data:`NAME_PREFIX`), so the side that reads it can list this device
        under the name its user chose rather than the one frozen on the
        certificate.  Empty sends nothing.

        *nonce* and *proof* are the handshake's proof of key possession: the
        nonce this side chose, and a signature over *(the peer's nonce, ours)*
        made with the key behind the certificate in this frame.  Both live in the
        frame rather than a second one, and that is load-bearing -- a second frame
        arriving before the application's first is a shape the reading side does
        not expect.  See `internal/security/handshake.py`.
        """
        head = ""
        if version:
            head += f"{HANDSHAKE_VERSION_PREFIX}{int(version)}\n"
        if nonce:
            head += f"{NONCE_PREFIX}{nonce}\n"
        if proof:
            head += f"{PROOF_PREFIX}{proof}\n"
        if listen_port:
            head += f"{LISTEN_PORT_PREFIX}{int(listen_port)}\n"
        if device_name:
            # A comment line ends at the newline, so a name that carries one
            # would end the field early and leave the rest of it at the head of
            # the PEM.  Sanitizing drops the control characters with it.
            head += f"{NAME_PREFIX}{_sanitize_peer_str(device_name, 128)}\n"
        text = head + cert_pem
        if no_auto_pairing:
            text += NO_PAIRING_MARKER
        data = text.encode("utf-8")
        frame = struct.pack(">I", len(data)) + data
        sock.sendall(frame)

    @staticmethod
    def _identity_payload(
        data: bytes,
    ) -> tuple[str, bool, int, str, str, str, int]:
        """Split an identity frame into its fields and the certificate.

        Returns ``(cert_pem, no_auto_pairing, listen_port, device_name, nonce,
        proof)``.  The marker and the leading comment lines are removed before the
        PEM reaches a parser or a pin, so a frame that carries them is
        byte-for-byte the frame that did not.

        A field that is absent is reported as its "unknown" — 0, or "" — which is
        what every frame from a build older than that field looks like.  The name
        is decoded as UTF-8: device names are the user's to write, and this
        application's users write them in Chinese.
        """
        text = data.decode("utf-8", errors="replace")
        no_auto_pairing = text.endswith(NO_PAIRING_MARKER)
        if no_auto_pairing:
            text = text[: -len(NO_PAIRING_MARKER)]
        listen_port = 0
        device_name = ""
        nonce = ""
        proof = ""
        version = 0
        # The lines before the PEM, in whatever order a sender wrote them.  The
        # scan stops at the first line that is not one of ours — the PEM's own
        # BEGIN — so a certificate that contains text resembling a prefix
        # cannot have its body consumed.
        while True:
            head, sep, rest = text.partition("\n")
            if not sep or not rest:
                break
            # A line of ours whose value cannot be read is consumed like any
            # other: it is not the PEM's BEGIN line, so leaving it in place
            # would only put a comment in front of the certificate.
            if head.startswith(HANDSHAKE_VERSION_PREFIX):
                with contextlib.suppress(ValueError):
                    version = max(0, int(head[len(HANDSHAKE_VERSION_PREFIX) :]))
            elif head.startswith(NONCE_PREFIX):
                nonce = _sanitize_peer_str(head[len(NONCE_PREFIX) :], 128)
            elif head.startswith(PROOF_PREFIX):
                proof = _sanitize_peer_str(head[len(PROOF_PREFIX) :], 512)
            elif head.startswith(LISTEN_PORT_PREFIX):
                with contextlib.suppress(ValueError):
                    listen_port = max(0, int(head[len(LISTEN_PORT_PREFIX) :]))
            elif head.startswith(NAME_PREFIX):
                device_name = _sanitize_peer_str(head[len(NAME_PREFIX) :])
            else:
                break
            text = rest
        return text, no_auto_pairing, listen_port, device_name, nonce, proof, version

    @staticmethod
    def _restore_timeout(sock: ssl.SSLSocket, timeout: float | None) -> None:
        """Put a socket's timeout back, without letting that become the error.

        ``SSLSocket.settimeout`` raises ``RuntimeError("handshake not done yet")``
        once the socket has been closed underneath it — its ``_sslobj`` is gone —
        and this runs from ``finally`` blocks, where an exception replaces whatever
        actually went wrong.  That is how a peer closing the connection
        mid-handshake came to be logged as "connect failed: handshake not done
        yet", which names nothing: the reason was a socket the peer had already
        dropped.  A timeout that cannot be restored is not a failure of its own.
        """
        with contextlib.suppress(Exception):
            sock.settimeout(timeout)

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
            TransportManager._restore_timeout(sock, prev_timeout)

    @staticmethod
    def _send_rejection(sock: ssl.SSLSocket, reason: int = REJECT_UNSPECIFIED):
        """Send a rejection marker so the peer knows why not to reconnect.

        *reason* is one of ``REJECT_*``: the dialling side cannot work out which
        it is, and one of them has an action for the user.
        """
        with contextlib.suppress(Exception):
            code = reason if 0 <= reason <= 255 else REJECT_UNSPECIFIED
            sock.sendall(_REJECT_MARKER + bytes([code]))

    def _check_rejection(
        self, sock: ssl.SSLSocket, timeout: float = 0.25, max_wait: float = 1.0
    ) -> tuple[int, bytes]:
        """Check whether the server sent the application-level rejection
        marker right after its identity frame.

        Returns ``(reason, leftover_bytes)`` -- *reason* being one of the
        ``REJECT_*`` codes, zero when this was not a rejection -- where
        *leftover_bytes* holds
        anything read during the probe that was NOT part of a rejection —
        the caller must hand it to the new PeerConnection so the first
        application frame stays intact.

        The marker only ever arrives on the active-rejection path, so the
        common case is *no bytes at all*: the probe must be brief (the old
        implementation blocked a flat 2 s on every outbound connect) and
        must not consume stream bytes.  A frame header starts with a zero
        length byte while the marker starts with ``\\xff``, so a partial
        marker can never be mistaken for frame data and vice versa.
        """
        prev_timeout = sock.gettimeout()
        deadline = time.monotonic() + max_wait
        # One byte past the marker, because the reason rides after it.  Reading
        # only the marker and stopping is how the reason got lost: the probe saw
        # a marker with nothing after it in the same segment and reported
        # "unspecified" for every rejection, however specific the sender had been.
        want = len(_REJECT_MARKER) + 1
        data = b""
        try:
            while len(data) < want:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    sock.settimeout(min(timeout, remaining))
                    chunk = sock.recv(want - len(data))
                except TimeoutError:
                    if data and _REJECT_MARKER.startswith(data):
                        continue  # possible torn marker — keep probing
                    break
                if not chunk:
                    break  # remote closed mid-probe
                data += chunk
                if not _REJECT_MARKER.startswith(data):
                    break  # application data — definitely not a rejection
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                TransportManager._restore_timeout(sock, prev_timeout)
        if data.startswith(_REJECT_MARKER):
            if len(data) > len(_REJECT_MARKER):
                return data[len(_REJECT_MARKER)], b""
            # A marker whose reason byte has not arrived (or never will, from a
            # build that predates it).  Still a rejection: frames start with a
            # zero length byte and the marker with 0xff, so this cannot be the
            # start of anything else, and discarding it would leave the frame
            # reader to read four 0xff bytes as a 0xFFFFFFFF length.
            logger.debug(
                "[%s] %d-byte reject marker with no reason byte — treating as unspecified",
                self._device_name,
                len(data),
            )
            return REJECT_UNSPECIFIED, b""
        return 0, data

    @staticmethod
    def _read_rejection_marker(sock: ssl.SSLSocket) -> int | None:
        """Consume a rejection marker from *sock*, returning its reason.

        Called *before* the frame reader, and that ordering is the whole point:
        the marker begins with four ``0xff`` bytes, which the frame reader reads
        as a length header of 0xFFFFFFFF and discards.  Every rejection therefore
        used to reach the dial as "the peer sent no identity frame" or "the peer
        vanished", with the reason gone before anything could report it.

        Returns the reason code, or None when the next bytes are not a marker.
        """
        prev_timeout = sock.gettimeout()
        sock.settimeout(0.25)
        want = len(_REJECT_MARKER) + 1
        try:
            data = b""
            while len(data) < want:
                try:
                    chunk = sock.recv(want - len(data))
                except TimeoutError:
                    break
                if not chunk:
                    break
                data += chunk
                # Divergence is only possible *within* the marker's length: the
                # reason byte follows it, so by the time `data` is longer than the
                # marker the prefix test would compare against a longer string and
                # reject the very marker it just matched.
                if len(data) <= len(_REJECT_MARKER) and not _REJECT_MARKER.startswith(data):
                    return None
            if not data.startswith(_REJECT_MARKER):
                return None
            if len(data) > len(_REJECT_MARKER):
                return data[len(_REJECT_MARKER)]
            return REJECT_UNSPECIFIED
        except Exception:
            return None
        finally:
            TransportManager._restore_timeout(sock, prev_timeout)

    def start_server(self):
        attempted = False
        try:
            with self._lock:
                if self._stop_calls:
                    raise RuntimeError("Transport shutdown is still in progress")
                if self._running:
                    return
                self._prune_workers()
                if self._workers or self._callbacks:
                    raise RuntimeError("Transport shutdown is still in progress")
                attempted = True
                self._cleanup_stale_scratch()
                ssl_context = self._build_ssl_context(server_side=True)
                self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    self._server_sock.bind(("0.0.0.0", self._port))
                except OSError as e:
                    raise PortInUseError(self._port) from e
                # The port the kernel actually gave us.  Asked for port 0 this is
                # the only way to learn it, and *asking* for 0 is the only way to
                # get a port without a race: any caller that first probes for a
                # free port and then binds it has handed the interval between the
                # two to every other process on the machine.  Read back
                # unconditionally, so a real port reads as itself and only the
                # ephemeral case learns something.
                self._port = self._server_sock.getsockname()[1]
                self._server_sock.listen(5)
                self._server_sock.settimeout(1.0)
                self._stopped.clear()
                self._running = True
                # Wall time advances across sleep; monotonic is immune to NTP.
                self._last_health_tick = time.time()
                self._last_health_mono = time.monotonic()
                self._health_thread = self._start_worker(self._health_check_loop)
                self._server_thread = self._start_worker(self._accept_loop, (ssl_context,))
        except BaseException:
            if attempted:
                self.stop_server()
            raise
        logger.info("TCP server listening on port %d", self._port)

    def stop_server(self, timeout: float = 5.0) -> bool:
        """Stop admitting work and drain it within one timeout budget.

        True means all owned threads and admitted callbacks have exited. False
        means the caller must retain ownership and retry (e.g. blocked user code
        or DNS). Called inside a worker/callback, this signals stop without
        joining: an external owner must call again after that callback unwinds.
        Repeated calls are safe. Restart is refused until the drain completes.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        current = threading.current_thread()
        with self._lock:
            self._stop_calls += 1
            self._running = False
            self._stopped.set()
            server, self._server_sock = self._server_sock, None
            peers = set(self._peers.values()) | self._connections
            self._peers.clear()
            timers = list(self._reconnect_timers.values())
            self._reconnect_timers.clear()
            self._reconnect_attempts.clear()
            sockets = {s for group in self._pending_sockets.values() for s in group}
            for conn in peers:
                if conn._recv_thread is not None:
                    self._workers.add(conn._recv_thread)
            workers = set(self._workers)
            inside = current in workers or current in self._callbacks
        try:
            for timer in timers:
                timer.cancel()
            if server is not None:
                self._close_socket(server)
            for sock in sockets:
                self._close_socket(sock)
            for conn in peers:
                conn.stop()
            if inside:
                return False
            for worker in workers:
                if worker is not current and worker.ident is not None:
                    worker.join(max(0.0, deadline - time.monotonic()))
            with self._callback_done:
                while self._callbacks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._report_stragglers(workers)
                        return False
                    self._callback_done.wait(remaining)
                self._prune_workers()
                if self._workers:
                    self._report_stragglers(workers)
                return not self._workers and self._stop_calls == 1
        finally:
            with self._lock:
                self._stop_calls -= 1

    def _report_stragglers(self, workers) -> None:
        """Name what a failed drain was waiting for, and where it is stuck.

        The alternative is the shape this used to have: the caller logs "did not
        release ownership within its budget", which says a stage was slow but not
        which one, and the answer is not recoverable after the fact -- the threads
        are gone by the time anyone reads the log.  A stack per live thread is the
        whole diagnosis, and it is what found the shutdown defect:
        `clipsync-connect` still inside `create_connection`.
        """
        live = [worker for worker in workers if worker.is_alive()]
        pending = sorted(self._callbacks)
        logger.warning(
            "Transport drain incomplete: %d worker(s) %s, %d callback group(s) %s",
            len(live),
            [worker.name for worker in live] or "-",
            len(pending),
            pending or "-",
        )
        frames = sys._current_frames()
        for worker in live:
            frame = frames.get(worker.ident)
            if frame is None:
                continue
            stack = traceback.extract_stack(frame, limit=6)
            where = " <- ".join(
                f"{os.path.basename(entry.filename)}:{entry.lineno} {entry.name}"
                for entry in reversed(stack)
            )
            logger.warning("  %s is in: %s", worker.name, where)

    def connect_to_peer(
        self, peer_id: str, peer_name: str, address: str, port: int, no_auto_pairing: bool = False
    ):
        """Connect to *peer_id* at (address, port).

        *no_auto_pairing* is set by the nearby-chat flow: an unpaired peer
        connected purely for a consent-gated chat must NOT be auto-offered a
        shared pairing code (chat has its own invite/accept + fingerprint
        consent).  Default connections (clipboard sync, explicit pairing) keep
        the auto shared-code pairing offer.
        """
        with self._lock:
            if not self._running:
                return
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
                self._connect_holds.pop(pid, None)
            if peer_id in self._peers:
                logger.debug(
                    "[%s] connect_to_peer: already connected (peer_id=%s), skipping",
                    peer_name,
                    peer_id[:12],
                )
                return  # already connected
            # Resolve hashed mDNS ID to real device_id so we don't create
            # a duplicate connection when the peer is stored under its real ID.
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id and real_id in self._peers:
                logger.debug(
                    "[%s] connect_to_peer: already connected under real ID %s, skipping",
                    peer_name,
                    real_id[:12],
                )
                return
            self._peer_addresses[peer_id] = (peer_name, address, port)
        logger.info("[%s] connecting to %s:%d (peer_id=%s)", peer_name, address, port, peer_id[:12])

        def _connect():
            # The peer is free to answer with a name of its own (see
            # :meth:`_send_identity`), so the label this dial carried is rebound
            # below — keeping every later log line, the stored record and the
            # connection's own name on what the peer actually calls itself.
            nonlocal peer_name
            sock = None
            ssl_sock = None
            peer_cert_pem = ""
            real_peer_id = ""
            try:
                logger.info("[%s] TCP connecting to %s:%d", peer_name, address, port)
                # Through `_dial`, not `socket.create_connection`: the socket has
                # to be registered before it connects, or a stop during the dial
                # has nothing to close and this thread outlives the teardown.
                sock = self._dial(address, port, 10)
                logger.info("[%s] TCP connected, starting TLS handshake", peer_name)

                # Peers are stored under their real device id, but discovery may
                # call us with a hashed mDNS id — resolve it so pinning still
                # applies to an already-paired peer (otherwise is_peer_paired()
                # returns False and we'd silently drop to CERT_NONE).
                real = self._hash_to_real_id.get(peer_id, peer_id)
                verify_id = real if self._pairing_mgr.is_peer_paired(real) else None
                ssl_context = self._build_ssl_context(server_side=False, verify_peer_id=verify_id)
                logger.info(
                    "[%s] SSL context built (verify_id=%s)",
                    peer_name,
                    verify_id[:12] if verify_id else "None",
                )

                ssl_sock = self._wrap_socket(ssl_context, sock, server_hostname=peer_id)
                logger.info("[%s] TLS handshake complete", peer_name)

                # Exchange identity at application level: send our cert,
                # then read the server's cert from its identity frame.
                identity = self._pairing_mgr.get_identity()
                # The flag has to travel with the frame: the accepting side is
                # the one that generates the code, and it cannot read an
                # argument that stayed on this machine.  The port travels with
                # it for the same reason: the accepting side is the one that
                # needs to call back, and the socket only tells it where this
                # call came from.  The name travels with it because the
                # certificate's copy is the one written when the identity was
                # minted, and a device renamed since would otherwise introduce
                # itself here under a name its user has already changed.
                # Read the peer's identity frame *first*, because our own frame
                # carries the proof and the proof is a signature over the nonce
                # that arrives in theirs.  This is the one ordering that does not
                # deadlock: only one side may speak first, and the accepting side
                # is the one that does (it answers the call).  See
                # `internal/security/handshake.py`, which states the exchange as a
                # function and `tests/sidecar/test_handshake_exchange.py`, which
                # drives it over a plain byte stream.
                #
                # Reading before sending also removes the need to probe the stream
                # for a rejection marker: a peer that refuses this call sends the
                # marker instead of an identity frame, and the read below fails
                # into the refusal path a few lines down.  The old probe read raw
                # bytes off the stream to tell the two apart, which is precisely
                # what the extra proof frame used to trip over.
                server_cert_data = self._recv_identity(ssl_sock)
                if server_cert_data:
                    (
                        peer_cert_pem,
                        _server_flags,
                        _peer_port,
                        server_device_name,
                        server_nonce,
                        _server_proof,
                        _server_version,
                    ) = self._identity_payload(server_cert_data)
                    peer_cert = x509.load_pem_x509_certificate(peer_cert_pem.encode())
                    our_nonce = self._pairing_mgr.make_handshake_nonce()
                    self._send_identity(
                        ssl_sock,
                        identity.certificate_pem,
                        no_auto_pairing=no_auto_pairing,
                        listen_port=self._port,
                        device_name=identity.device_name,
                        nonce=our_nonce,
                        proof=self._pairing_mgr.sign_handshake_nonce(server_nonce, our_nonce),
                        version=PROOF_VERSION,
                    )
                    logger.info("[%s] sent identity frame", peer_name)

                    # The accepting side answers with its own identity frame,
                    # which is where its proof of *our* nonce arrives.
                    answer = self._recv_identity(ssl_sock)
                    peer_proof = ""
                    if answer:
                        (
                            _cert2,
                            _flags2,
                            _port2,
                            _name2,
                            _nonce2,
                            peer_proof,
                            _version2,
                        ) = self._identity_payload(answer)
                    if not self._pairing_mgr.verify_handshake_nonce(
                        peer_cert_pem, server_nonce, our_nonce, peer_proof
                    ):
                        # Reported, not fatal.  The TLS layer already refused
                        # anything that cannot chain to this device's pinned
                        # certificate, so an impostor is stopped there; what this
                        # adds is the application-layer half of the same question,
                        # and a peer older than the field cannot answer it at all.
                        logger.debug(
                            "[%s] no verifiable handshake proof (own_nonce=%s "
                            "peer_nonce=%s proof_len=%d)",
                            peer_name,
                            our_nonce[:8],
                            server_nonce[:8],
                            len(peer_proof),
                        )

                # Check if the server rejected us at the application level
                # (e.g. we were forgotten by this peer, or it will not trust this
                # certificate until we re-pair).  The server sends a rejection
                # marker after its identity frame, with a reason byte that says
                # which -- the two look identical from here otherwise, and only one
                # of them has an action for the user.  The probe is brief when
                # there is no rejection; anything it consumes that isn't the marker
                # is replayed into the connection below so the first application
                # frame stays intact.
                reject_reason, probe_leftover = self._check_rejection(ssl_sock)
                if reject_reason:
                    logger.info(
                        "[%s] peer rejected this connection (%s) — clearing saved "
                        "address to prevent auto-reconnect",
                        peer_name,
                        _REJECT_REASONS.get(reject_reason, f"code {reject_reason}"),
                    )
                    with self._lock:
                        self._peer_addresses.pop(peer_id, None)
                        self._reconnect_attempts.pop(peer_id, None)
                    # Surface the refusal to the app layer (e.g. a web toast) —
                    # otherwise a "Connect" click on a peer that removed us
                    # looks like a silent no-op, and a peer that needs re-pairing
                    # looks like a device that mysteriously stopped working.
                    self._notify_connect_rejected(peer_name, peer_id, reject_reason)
                    ssl_sock.close()
                    return

                real_peer_id = peer_id
                if not server_cert_data:
                    # No identity frame at all.  This used to fall through and
                    # build the connection anyway, with the id this side *dialed*
                    # and that id's pinned fingerprint -- so the peer's identity
                    # was never established, and the connection was still treated
                    # as that (possibly paired) device.  An attacker who answers
                    # the dial and then says nothing inherited the trust; so did an
                    # honest peer merely slower than the 10 s identity read, whose
                    # late frame was then dropped as a bad one.  A dial that did
                    # not authenticate anyone is not a connection to whoever was
                    # dialed.
                    logger.warning(
                        "[%s] no identity frame from %s:%d — refusing the dial",
                        peer_name,
                        address,
                        port,
                    )
                    self._notify_connect_rejected(
                        peer_name, peer_id, reject_reason or REJECT_UNSPECIFIED
                    )
                    ssl_sock.close()
                    return
                if server_cert_data:
                    # Bind the TLS-presented cert to the app-layer identity cert:
                    # they must be the same, else the peer is two principals.
                    # (`_identity_payload` was read above, before our own frame was
                    # sent: our proof is a signature over the nonce in that frame,
                    # so it has to be in hand first.)
                    tls_der = ssl_sock.getpeercert(binary_form=True)
                    if tls_der is not None and tls_der != peer_cert.public_bytes(
                        serialization.Encoding.DER
                    ):
                        logger.warning(
                            "[%s] TLS cert differs from identity cert — refusing",
                            peer_name,
                        )
                        ssl_sock.close()
                        return

                    try:
                        cn_attrs = peer_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                        if cn_attrs:
                            real_peer_id = _sanitize_peer_str(cn_attrs[0].value)
                    except Exception:
                        pass

                    if server_device_name:
                        # What the peer calls itself now, which is the name it
                        # is listed under here — the label this dial carried was
                        # this machine's own reading, and the certificate's copy
                        # is the one frozen when the peer's identity was minted.
                        peer_name = server_device_name

                    was_paired = self._pairing_mgr.is_peer_paired(real_peer_id)
                    logger.info(
                        "[%s] cert extracted: real_peer_id=%s, was_paired=%s, peer_id=%s",
                        peer_name,
                        real_peer_id[:12],
                        was_paired,
                        peer_id[:12],
                    )
                    self._pairing_mgr.add_peer(
                        real_peer_id,
                        peer_name,
                        peer_cert_pem,
                        paired=was_paired,
                    )
                    if not was_paired and not no_auto_pairing:
                        try:
                            shared_code = self._pairing_mgr.generate_shared_pairing_code(
                                real_peer_id
                            )
                            logger.info(
                                "[%s] pairing code: %s**** — verify on both devices",
                                peer_name,
                                shared_code[:4],
                            )
                        except Exception as e:
                            logger.debug("Could not generate shared pairing code: %s", e)

                # Resolve peer fingerprint for app-layer encryption
                peer_fp = (
                    self._pairing_mgr.get_peer_fingerprint(real_peer_id) if real_peer_id else ""
                )

                conn = PeerConnection(
                    real_peer_id,
                    peer_name,
                    ssl_sock,
                    peer_fingerprint=peer_fp,
                    enc_mgr=self._enc_mgr,
                    pairing_mgr=self._pairing_mgr,
                    pending_recv=probe_leftover,
                )
                if not self._start_peer(conn):
                    return

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
                    # The user may have decided something about this peer while the
                    # dial was in flight -- the connect, TLS and identity exchange
                    # take a round trip each.  断开连接 records a hold and 移除设备
                    # records a rejection, and both used to be consulted only on the
                    # inbound path, so a dial that started before the click finished
                    # after it and put the peer straight back: the button looked
                    # like it did nothing, and the device went on receiving
                    # clipboard broadcasts, which is exactly the state a user
                    # disconnects a machine to leave.  Re-checked here, in the same
                    # critical section that installs it, so there is no window
                    # between the check and the installation.
                    if real_peer_id and (
                        real_peer_id in self._rejected_peer_ids
                        or self._hold_active(real_peer_id)
                    ):
                        logger.info(
                            "[%s] peer was disconnected or removed while this dial was "
                            "in flight — discarding the connection",
                            peer_name,
                        )
                        conn.set_on_disconnect(None)
                        conn.stop()
                        return
                    # If discovery used a hashed ID, clean up and update mapping
                    if real_peer_id != peer_id:
                        logger.debug(
                            "[%s] hash→real mismatch: hash=%s real=%s",
                            peer_name,
                            peer_id[:12],
                            real_peer_id[:12],
                        )
                        old_hash = self._peers.pop(peer_id, None)
                        if old_hash:
                            logger.debug(
                                "[%s] removing stale hash-keyed connection (conn=%s)",
                                peer_name,
                                hex(id(old_hash)),
                            )
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
                            peer_name,
                            real_peer_id[:12],
                            len(self._peers),
                        )
                    elif self._device_id < real_peer_id:
                        # We are the lower ID — our outgoing wins.
                        logger.info(
                            "[%s] tiebreaker: our outgoing wins (we=%s < peer=%s) — replacing",
                            peer_name,
                            self._device_id[:12],
                            real_peer_id[:12],
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
                        if not self._running:
                            conn.stop()
                            return
                        if self._peers.get(real_peer_id) is existing:
                            logger.info(
                                "[%s] tiebreaker: peer should win but existing is dead — keeping our outgoing",  # noqa: E501
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
                        "[%s] tiebreaker: peer's outgoing wins (peer=%s < we=%s) — discarding our outgoing",  # noqa: E501
                        peer_name,
                        real_peer_id[:12],
                        self._device_id[:12],
                    )
                    conn.set_on_disconnect(None)
                    to_stop.append(conn)
                for c in to_stop:
                    c.stop()

                # Success clears the reconnect backoff for both the hashed and
                # real id under the lock — every other _reconnect_attempts
                # mutation takes the lock, so a racing _schedule_reconnect in
                # the disconnect thread must not interleave with these pops
                # (a lost update would leave a stale backoff running).
                with self._lock:
                    self._reconnect_attempts.pop(peer_id, None)
                    self._reconnect_attempts.pop(real_peer_id, None)
                    # A working TLS session means the pin matches again, so any
                    # earlier cert-pin block is stale.
                    self._cert_pin_blocked.pop(peer_id, None)
                    self._cert_pin_blocked.pop(real_peer_id, None)
                logger.info(
                    "[%s] connected [%s] (%s:%d)", peer_name, real_peer_id[:12], address, port
                )

            except CertificateChangedError:
                # Expected = the stored fingerprint of the previously paired
                # cert; received = the fingerprint of the new cert that just
                # triggered the alert. Populate both so the alert is useful.
                expected_fp = (
                    self._pairing_mgr.get_peer_fingerprint(real_peer_id) if real_peer_id else ""
                )
                received_fp = fingerprint_pem(peer_cert_pem) if peer_cert_pem else ""
                logger.error(
                    "SECURITY: Certificate for %s has changed — possible MITM attack! "
                    "Connection rejected. Expected fp: %s Got: %s",
                    peer_name,
                    expected_fp[:16] if expected_fp else "n/a",
                    received_fp[:16] if received_fp else "n/a",
                )
                if self._on_security_alert:
                    self._invoke_callback(
                        self._on_security_alert,
                        peer_name,
                        real_peer_id,
                        expected_fp,
                        received_fp,
                        peer_cert_pem,
                    )
                if ssl_sock:
                    with contextlib.suppress(Exception):
                        ssl_sock.close()
                if sock:
                    with contextlib.suppress(Exception):
                        sock.close()
            except Exception as e:
                logger.warning("[%s] connect failed: %s", peer_name, e)
                if ssl_sock:
                    with contextlib.suppress(Exception):
                        ssl_sock.close()
                if sock:
                    with contextlib.suppress(Exception):
                        sock.close()
                lookup = self._hash_to_real_id.get(peer_id, peer_id)
                # A paired peer whose certificate no longer matches the pin
                # raises SSLCertVerificationError on wrap_socket.  Surface it
                # as a security alert instead of silently reconnecting forever
                # — otherwise the device sits "paired but unreachable" with no
                # user-visible signal.  (The server side detects the change via
                # add_peer and alerts with the new cert; here we have no cert.)
                if isinstance(e, ssl.SSLCertVerificationError) and self._pairing_mgr.is_peer_paired(
                    lookup
                ):
                    expected_fp = self._pairing_mgr.get_peer_fingerprint(lookup)
                    logger.error(
                        "SECURITY: Certificate for %s no longer matches the "
                        "pinned cert — not auto-reconnecting",
                        peer_name,
                    )
                    if self._on_security_alert:
                        self._invoke_callback(
                            self._on_security_alert,
                            peer_name,
                            lookup,
                            expected_fp,
                            "",
                            "",
                        )
                    # Remember the pin we refused on.  Suppressing the
                    # reconnect is deliberate, but nothing used to lift the
                    # suppression: after the user re-paired and accepted the new
                    # certificate the device stayed "paired but unreachable"
                    # until the app was restarted, because no code path ever
                    # re-armed the timer.  The health loop watches this map and
                    # reconnects as soon as the pinned fingerprint changes.
                    with self._lock:
                        self._cert_pin_blocked[peer_id] = (lookup, expected_fp or "")
                elif self._pairing_mgr.is_peer_paired(lookup):
                    self._schedule_reconnect(peer_id)
                else:
                    logger.info(
                        "[%s] connect failed and peer not paired — no auto-reconnect", peer_name
                    )

        self._start_worker(_connect, name="clipsync-connect")

    def broadcast(self, data: bytes) -> bool:
        """Send *data* to every paired peer.  Returns True if at least one
        peer received the frame.

        Callers that need to know whether the data actually went out (chat's
        ``send_text`` treats a False as "nothing was delivered") rely on this
        return value — it must be a real bool, not None.
        """
        delivered = False
        with self._lock:
            peers = list(self._peers.values())
        for conn in peers:
            # Outgoing pairing gate: never leak clipboard content to unpaired
            # or anonymous peers. Pairing is done via the connection's own
            # identity frames, not broadcast, so this is safe to skip.
            if not self._pairing_mgr.is_peer_paired(conn.device_id):
                logger.debug(
                    "[%s] broadcast: skipping unpaired peer",
                    conn.device_id[:12],
                )
                continue
            if conn.send(data):
                delivered = True
        return delivered

    def send_to_peer(self, peer_id: str, data: bytes) -> bool:
        """Send *data* to one peer.  Returns True on delivery, False when the
        peer is not connected or the send failed."""
        with self._lock:
            conn = self._peers.get(peer_id)
        if conn is None:
            # Debug, not warning: an internet-paired peer has no LAN connection
            # by design, and the callers that route through the relay ask here
            # first — so every heartbeat for such a peer logged a warning that
            # named a routine state.  The False return is the answer; the
            # caller decides whether that is worth telling the user.
            logger.debug("send_to_peer: peer %s not connected", peer_id[:12])
            return False
        return conn.send(data)

    def get_connected_peers_with_names(self) -> list[tuple[str, str]]:
        """Return list of (peer_id, device_name) for all connected peers."""
        with self._lock:
            return [(pid, conn.device_name) for pid, conn in self._peers.items()]

    def disconnect_peer(self, peer_id: str, reject: bool = False):
        """Disconnect a peer and optionally hold its reconnections off.

        When *reject* is True, incoming connections from this peer are refused
        for :data:`DISCONNECT_HOLD_SECONDS` — long enough that the button is not
        undone by the peer's next reconnect, and expiring so that a disconnect
        is never mistaken for a removal.  ``forget_peer`` is the permanent one.
        """
        timers_to_cancel = []
        with self._lock:
            # Discovery (e.g. _on_peer_lost) may call us with a hashed mDNS id,
            # but _peers is keyed by the real device_id — resolve it first so
            # the pop lands (the same resolution forget_peer does), otherwise
            # the connection lingers until idle timeouts clean it up.
            real_id = self._hash_to_real_id.get(peer_id, peer_id)
            conn = self._peers.pop(real_id, None)
            # Reconnect bookkeeping may be keyed under the hashed id or the
            # resolved real id — clear both so a stale timer can't reconnect
            # into a peer the user just disconnected.
            for pid in {peer_id, real_id}:
                timer = self._reconnect_timers.pop(pid, None)
                if timer:
                    timers_to_cancel.append(timer)
                self._reconnect_attempts.pop(pid, None)
                self._cert_pin_blocked.pop(pid, None)
            if reject:
                deadline = time.monotonic() + DISCONNECT_HOLD_SECONDS
                self._connect_holds[peer_id] = deadline
                self._connect_holds[real_id] = deadline
        for t in timers_to_cancel:
            t.cancel()
        if conn:
            logger.info("[%s] manual disconnect%s", peer_id[:12], " (rejected)" if reject else "")
            conn.set_on_disconnect(None)
            conn.stop()

    def _hold_active(self, peer_id: str) -> bool:
        """`_on_hold` without the lock, for a caller already holding it.

        Split because `_connect` re-checks the hold inside the critical section
        that installs a dialed connection, and calling the locking version there
        would deadlock on a non-reentrant lock.
        """
        deadline = self._connect_holds.get(peer_id)
        if deadline is None:
            return False
        if deadline <= time.monotonic():
            self._connect_holds.pop(peer_id, None)
            return False
        return True

    def _on_hold(self, peer_id: str) -> bool:
        """Whether a manual disconnect is still keeping this peer out.

        Expired entries are dropped as they are met, so the map stays the size
        of the disconnects still in force.
        """
        with self._lock:
            return self._hold_active(peer_id)

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

            # Also find any hashed IDs stored under either address table that
            # share the same (address, port) and are not yet covered
            addr_info = self._peer_addresses.get(peer_id) or self._callback_addresses.get(peer_id)
            if addr_info:
                _, addr, port = addr_info
                for table in (self._peer_addresses, self._callback_addresses):
                    for pid, (_, a, p) in list(table.items()):
                        if a == addr and p == port:
                            ids_to_reject.add(pid)

            # Purge every collected ID from all tracking structures
            timers_to_cancel = []
            conns_to_stop = []
            for pid in ids_to_reject:
                self._rejected_peer_ids.add(pid)
                self._peer_addresses.pop(pid, None)
                # A removal takes the way back with it: the address this peer's
                # own call taught us would otherwise keep it dialable in the
                # list the user just removed it from.
                self._callback_addresses.pop(pid, None)
                self._reconnect_attempts.pop(pid, None)
                t = self._reconnect_timers.pop(pid, None)
                if t:
                    timers_to_cancel.append(t)
                c = self._peers.pop(pid, None)
                if c:
                    conns_to_stop.append(c)
                self._hash_to_real_id.pop(pid, None)

        for t in timers_to_cancel:
            t.cancel()
        if conns_to_stop:
            logger.info(
                "[%s] forget: manual disconnect (%d connection(s))",
                peer_id[:12],
                len(conns_to_stop),
            )
            for conn_to_stop in conns_to_stop:
                conn_to_stop.set_on_disconnect(None)
                conn_to_stop.stop()

    def allow_peer(self, peer_id: str):
        """Lift a previous reject/forget so this peer may connect again.

        ``forget_peer`` parks a peer in ``_rejected_peer_ids``, which refuses
        its INBOUND connections; ``disconnect_peer(reject=True)`` holds it off
        for a while.  Only an outbound ``connect_to_peer`` clears those, so a
        device restored from the removed archive could not be paired *from the
        other side* — its connection was silently refused.  Restoring calls
        this instead, which clears both without opening a connection (restore
        must not pair).

        Covers the real device_id, its hashed mDNS form, and any hash that
        resolves to it — the same id fan-out forget_peer used to set the flag.
        """
        with self._lock:
            ids = {peer_id}
            real_id = self._hash_to_real_id.get(peer_id, peer_id)
            ids.add(real_id)
            for h, r in self._hash_to_real_id.items():
                if r == real_id:
                    ids.add(h)
            try:
                ids.add(peer_id_hash(peer_id))
                ids.add(peer_id_hash(real_id))
            except Exception:
                logger.debug("allow_peer: could not hash %s", peer_id[:12], exc_info=True)
            for pid in ids:
                self._rejected_peer_ids.discard(pid)
                self._connect_holds.pop(pid, None)
        logger.info("[%s] rejection lifted — peer may connect again", peer_id[:12])

    def get_connected_peers(self) -> list[str]:
        with self._lock:
            return list(self._peers.keys())

    def get_peer_fingerprint(self, peer_id: str) -> str:
        """Return the full fingerprint of a connected peer, or '' if unknown.

        The fingerprint is captured from the peer's certificate during the
        TLS identity exchange; the nearby-chat UI surfaces its short form so
        users can visually confirm who they are talking to.
        """
        with self._lock:
            conn = self._peers.get(peer_id)
            if conn is not None:
                return getattr(conn, "_peer_fingerprint", "") or ""
            # Discovery uses hashed ids; resolve to the real id first.
            real_id = self._hash_to_real_id.get(peer_id)
            if real_id:
                conn = self._peers.get(real_id)
                if conn is not None:
                    return getattr(conn, "_peer_fingerprint", "") or ""
        return ""

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
        """Return a copy of all last-known peer addresses (real-id → tuple).

        Includes the addresses learned from peers that dialed us: both are
        "where this peer is", and the callers of this method — persisting the
        addresses so a restart can still reach a peer — want the union.
        """
        with self._lock:
            merged = dict(self._callback_addresses)
            merged.update(self._peer_addresses)
            return merged

    def get_saved_address(self, peer_id: str) -> tuple[str, str, int] | None:
        """Return the last known (name, address, port) for a peer, or None.

        Handles real device IDs, the hashed mDNS IDs used during discovery,
        and the hash→real mapping, so callers can reconnect to a known peer
        even when it is momentarily absent from mDNS.

        Addresses we dialed come first; addresses a peer's own call taught us
        (see :data:`LISTEN_PORT_PREFIX`) answer for peers we have never dialed,
        which is every peer that reached us first.
        """

        def _hashed(pid: str) -> str:
            return peer_id_hash(pid)

        with self._lock:
            for table in (self._peer_addresses, self._callback_addresses):
                if peer_id in table:
                    return table[peer_id]
                hashed = _hashed(peer_id)
                if hashed in table:
                    return table[hashed]
                # Any hashed mDNS ID that resolves to this real ID
                for h, r in self._hash_to_real_id.items():
                    if r == peer_id and h in table:
                        return table[h]
        return None

    def get_reconnect_states(self) -> dict[str, dict]:
        """Return per-peer auto-reconnect bookkeeping for status display.

        Keyed by whichever id form reconnect scheduling used (the real device
        id or the hashed mDNS id — callers should try both).  Each value
        carries ``attempts`` (reconnect attempts already initiated, always
        inside the fast budget) and ``max_attempts``.

        A peer past that budget is not listed at all.  Past it the transport is
        on slow retry — one dial every ``MAX_RECONNECT_BACKOFF`` seconds, for as
        long as the peer stays away — and that is a background poll rather than
        a reconnection anyone is waiting on.  Reported anyway (capped at the
        ceiling, as the counter used to be), it kept every front end saying
        重连中 10/10 for as long as the peer was gone, so the state the row is
        meant to reach when the transport gives up — offline, per
        ``rpc.attach_reconnect_progress`` — was unreachable for exactly the
        peers a reader wanted it for.  A peer absent from the map is connected,
        never scheduled, or given up on.
        """
        with self._lock:
            return {
                pid: {
                    "attempts": attempts,
                    "max_attempts": self._max_reconnect_attempts,
                }
                for pid, attempts in self._reconnect_attempts.items()
                if attempts < self._max_reconnect_attempts
            }

    def _on_peer_disconnected(self, peer_id: str, conn=None):
        with self._lock:
            if not self._running:
                return
            current = self._peers.get(peer_id)
            if current is None and conn is not None:
                # Anonymous connections are keyed by __anon__{addr} while their
                # PeerConnection.device_id is "unknown", so the peer_id lookup
                # misses — locate the entry by the connection object itself.
                for _k, _v in self._peers.items():
                    if _v is conn:
                        peer_id = _k
                        current = _v
                        break
            if current is None:
                logger.info(
                    "[%s] disconnect: already removed from _peers (conn=%s)",
                    peer_id[:12],
                    hex(id(conn)) if conn else "N/A",
                )
                return
            elif conn is not None and current is not conn:
                # Disconnect is from a stale connection that was already
                # replaced by a newer one (e.g. during bidirectional connection
                # race). Don't delete the good connection.
                logger.info(
                    "[%s] disconnect from STALE connection (old=%s, current=%s) — ignoring",
                    peer_id[:12],
                    hex(id(conn)),
                    hex(id(current)),
                )
                return
            else:
                logger.info(
                    "[%s] disconnect from current connection (conn=%s) — removing from _peers",
                    peer_id[:12],
                    hex(id(conn)) if conn else "N/A",
                )
                del self._peers[peer_id]
        # A peer that explicitly rejected this connection (forgotten/removed)
        # must not be reconnected to — clear the saved address and stop the
        # connect/reject/reconnect loop.  Deliberately NOT added to
        # _rejected_peer_ids: that set is also consulted for INBOUND
        # connections, so a forgotten peer must still be able to reach us
        # again after the user re-pairs.
        if conn is not None and getattr(conn, "_rejected_by_peer", False):
            logger.info(
                "[%s] peer rejected this connection — clearing saved address, no auto-reconnect",
                peer_id[:12],
            )
            with self._lock:
                self._peer_addresses.pop(peer_id, None)
                self._reconnect_attempts.pop(peer_id, None)
            return
        if conn is not None and getattr(conn, "_crypto_mismatch", False):
            # The peers disagree on app-layer encryption (the recv loop detected
            # it above): reconnecting would only repeat the drop-and-tear-down
            # cycle, so back off.  Keep the saved address so a manual reconnect
            # or restart works once both devices use the same setting.
            logger.info(
                "[%s] encryption-setting mismatch — keeping connection down, "
                "no auto-reconnect until both devices use the same setting",
                peer_id[:12],
            )
            return
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
                    "[%s] reconnect: no saved address, skipping",
                    peer_id[:12],
                )
                return
            if not self._running:
                return
            attempts = self._reconnect_attempts.get(peer_id, 0)
            if attempts >= self._max_reconnect_attempts:
                # Past the fast-retry budget, downgrade to a slow fixed
                # interval instead of clearing the saved address.  On an
                # always-on desktop a Wi-Fi/router outage longer than ~3
                # minutes used to exhaust every attempt and leave the pair
                # disconnected until an app restart — mDNS never re-announces
                # a peer whose registration did not change, so nothing else
                # would re-trigger the connection.
                if attempts == self._max_reconnect_attempts:
                    logger.warning(
                        "[%s] fast reconnect budget exhausted (%d attempts) "
                        "— retrying every %ds until it comes back",
                        peer_id[:12],
                        self._max_reconnect_attempts,
                        MAX_RECONNECT_BACKOFF,
                    )
                delay = MAX_RECONNECT_BACKOFF
            else:
                delay = max(MIN_RECONNECT_DELAY, min(2**attempts, MAX_RECONNECT_BACKOFF))
            self._reconnect_attempts[peer_id] = attempts + 1
            if attempts < self._max_reconnect_attempts:
                logger.debug(
                    "[%s] scheduling reconnect attempt %d/%d in %.0fs",
                    peer_id[:12],
                    attempts + 1,
                    self._max_reconnect_attempts,
                    delay,
                )
            else:
                # Past the budget the count is no fraction of anything: "69/10"
                # in the log reads as a counter that escaped its ceiling rather
                # than as the slow retry it is.
                logger.debug(
                    "[%s] retrying slowly (attempt %d, %d fast attempts spent) "
                    "in %.0fs",
                    peer_id[:12],
                    attempts + 1,
                    self._max_reconnect_attempts,
                    delay,
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
            self._prune_workers()
            if isinstance(timer, threading.Thread):
                self._workers.add(timer)
            try:
                timer.start()
            except BaseException:
                self._workers.discard(timer)
                self._reconnect_timers.pop(peer_id, None)
                raise

    def _try_reconnect(self, peer_id: str):
        with self._lock:
            current = threading.current_thread()
            if current in self._workers and self._reconnect_timers.get(peer_id) is not current:
                return
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
                    peer_id[:12],
                    real_id[:12],
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
        logger.info(
            "[%s] reconnect timer fired — reconnecting to %s:%d", peer_id[:12], address, port
        )
        self.connect_to_peer(peer_id, peer_name, address, port)

    def _health_check_loop(self):
        """Periodically check connection health and detect sleep/wake events."""
        while self._running:
            self._stopped.wait(15)
            if not self._running:
                break

            now = time.time()  # wall clock — advances across sleep
            now_mono = time.monotonic()  # immune to clock adjustments
            gap = now - self._last_health_tick
            mono_gap = now_mono - self._last_health_mono
            self._last_health_tick = now
            self._last_health_mono = now_mono

            # Two independent signals, because no single clock covers every
            # platform: on Windows/macOS monotonic includes suspend time, so a
            # large mono_gap is itself proof the loop was frozen; on Linux
            # CLOCK_MONOTONIC stops during suspend, and only the wall clock
            # running ahead of it reveals the sleep.  Keying off the wall gap
            # alone (as before) fired a full reconnect sweep on any forward NTP
            # step, and — worse — a backward clock correction made `gap`
            # negative, so a real sleep went undetected and every peer socket
            # stayed dead until the user restarted the app.
            slept = _looks_like_wake(gap, mono_gap)
            if slept:
                logger.info(
                    "Sleep/wake detected (wall gap=%.0fs, monotonic gap=%.0fs), "
                    "recovering %d connections",
                    gap,
                    mono_gap,
                    len(self._peers),
                )
                self._handle_wake()
                if self._on_wake:
                    try:
                        self._invoke_callback(self._on_wake)
                    except Exception as e:
                        logger.debug("on_wake callback error: %s", e)
            else:
                self._recheck_cert_pin_blocks()
                with self._lock:
                    peers = list(self._peers.items())
                for peer_id, conn in peers:
                    # Unauthenticated ("anonymous") connections must not live
                    # forever: they never sent an identity frame, so they can
                    # never carry sync traffic — reap them once past their
                    # lifetime so silent TLS connections can't exhaust
                    # threads/fds and block legitimate peers.
                    if peer_id.startswith("__anon__"):
                        if time.monotonic() - conn.created_at > ANON_CONN_MAX_LIFE:
                            logger.info(
                                "[__anon__] connection from %s exceeded %ds "
                                "lifetime without identity — closing",
                                peer_id[len("__anon__") :][:21],
                                int(ANON_CONN_MAX_LIFE),
                            )
                            conn.set_on_disconnect(None)
                            conn.stop()
                            with self._lock:
                                if self._peers.get(peer_id) is conn:
                                    self._peers.pop(peer_id, None)
                        continue
                    if not conn.health_check():
                        logger.warning(
                            "[%s] health check FAILED — disconnecting",
                            peer_id[:12],
                        )
                        conn.set_on_disconnect(None)
                        conn.stop()
                        # Clean up and schedule reconnect — we bypassed
                        # _on_peer_disconnected so the stale entry won't
                        # linger in _peers and cause repeated failures.
                        self._on_peer_disconnected(peer_id, conn)

    def _recheck_cert_pin_blocks(self) -> None:
        """Re-arm reconnects for peers whose refused pin has since changed.

        A cert-pin mismatch deliberately stops auto-reconnect, but the block
        has to be liftable: once the user re-pairs (accepting the new
        certificate) the stored fingerprint no longer matches, and the peer is
        reachable again.  Without this the pair stayed dead until an app
        restart, since nothing else ever re-armed the timer.
        """
        with self._lock:
            blocked = list(self._cert_pin_blocked.items())
        if not blocked:
            return
        for peer_id, (real_id, refused_fp) in blocked:
            try:
                still_paired = self._pairing_mgr.is_peer_paired(real_id)
                current_fp = self._pairing_mgr.get_peer_fingerprint(real_id) or ""
            except Exception:
                continue
            if not still_paired:
                # Unpaired in the meantime: drop the block without reconnecting.
                with self._lock:
                    self._cert_pin_blocked.pop(peer_id, None)
                continue
            if current_fp == refused_fp:
                continue  # same pin, same mismatch — stay blocked
            logger.info(
                "[%s] pinned certificate was replaced — resuming reconnects",
                peer_id[:12],
            )
            with self._lock:
                self._cert_pin_blocked.pop(peer_id, None)
                self._reconnect_attempts.pop(peer_id, None)
            self._schedule_reconnect(peer_id)

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
            len(stale_peers),
            len(saved_addresses),
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
        failures = 0
        while self._running:
            try:
                client_sock, addr = self._server_sock.accept()
            except TimeoutError:
                continue
            except Exception as e:
                if self._running:
                    logger.warning("Accept error: %s: %s", type(e).__name__, e)
                    # Back off briefly so a persistent accept failure (e.g.
                    # fd exhaustion) cannot spin this loop at 100% CPU.
                    failures += 1
                    if failures >= 3:
                        self._stopped.wait(min(failures, 10) * 0.1)
                continue
            failures = 0
            logger.info("TCP accepted from %s:%d", addr[0], addr[1])
            # Complete TLS + identity handshake on a per-connection thread:
            # the old inline path serialized every accepted connection behind
            # up to ~25 s of handshake/identity timeouts, so one stalled (or
            # half-open) client delayed every other peer trying to connect.
            try:
                self._track_socket(client_sock)
                if self._handshakes_full():
                    # Refused before a thread is spent on it, so a flood costs a
                    # socket and a log line rather than a thread each.  A real
                    # peer that lands in a full moment reaches its own retry on
                    # the next reconnect, which is why this is a refusal and not
                    # an error the UI is told about.
                    logger.debug("Refusing an accepted connection: handshakes are at the ceiling")
                    self._close_socket(client_sock)
                elif (
                    self._start_worker(
                        self._handle_accepted, (client_sock, addr, ssl_context), "clipsync-accept"
                    )
                    is None
                ):
                    self._close_socket(client_sock)
            except Exception:
                self._close_socket(client_sock)
                if self._running:
                    logger.warning("Could not start accepted connection", exc_info=True)
            finally:
                with self._lock:
                    pending = self._pending_sockets.get(threading.current_thread(), set())
                    pending.discard(client_sock)

    def _handle_accepted(
        self, client_sock: socket.socket, addr: tuple, ssl_context: ssl.SSLContext
    ):
        """Finish one accepted connection: TLS handshake, identity exchange,
        pairing gate and race resolution.  Runs on its own thread."""
        ssl_sock = None
        peer_id = ""
        peer_name = ""
        peer_cert_pem = ""
        try:
            self._track_socket(client_sock)
            client_sock.settimeout(15)  # TLS handshake timeout
            try:
                ssl_sock = self._wrap_socket(ssl_context, client_sock, server_side=True)
                logger.info("TLS handshake OK with %s:%d", addr[0], addr[1])
            except (ssl.SSLError, TimeoutError) as e:
                # A MAC failure here is not a timeout and not a name that arrived
                # late: it says the bytes being decrypted are not the ones this
                # handshake's key state expects, which on a reconnect means the
                # previous connection's traffic reached this socket or this socket
                # was handed two handshakes.  The address alone cannot tell those
                # apart -- it is the *same* address on every reconnect from one
                # peer, which is the whole reason this case is hard to see.  The
                # counters below are what a reader needs: how many handshakes are
                # in flight, and whether something from this address is already
                # established.
                with self._lock:
                    # `Connection` has no address of its own; the dial-side table
                    # is where an address is remembered, so count from there.
                    from_this_address = sum(
                        1
                        for entry in self._peer_addresses.values()
                        if entry and entry[1] == addr[0]
                    )
                    active = len(self._peers)
                    on_this_thread = len(
                        self._pending_sockets.get(threading.current_thread(), ())
                    )
                logger.warning(
                    "TLS handshake failed from %s:%d: %s "
                    "(established peers=%d, dialed addresses from this host=%d, "
                    "sockets on this thread=%d)",
                    addr[0],
                    addr[1],
                    e,
                    active,
                    from_this_address,
                    on_this_thread,
                )
                client_sock.close()
                return

            # Exchange identity at application level: send our cert, then read
            # the client's.  This side speaks first because it is the one that
            # took the call, and the order is what makes the proof possible at
            # all: the client's signature covers the nonce in the frame below, so
            # the client cannot speak first (it has seen no nonce) and this side
            # cannot prove itself until the client's nonce arrives.  See
            # `internal/security/handshake.py`.
            identity = self._pairing_mgr.get_identity()
            our_nonce = self._pairing_mgr.make_handshake_nonce()
            # The name goes out with the certificate for the same reason as on
            # the dial side: the certificate's own copy was written when this
            # identity was minted, so a device renamed since would introduce
            # itself here under the name its user has already changed.
            self._send_identity(
                ssl_sock,
                identity.certificate_pem,
                device_name=identity.device_name,
                nonce=our_nonce,
                version=PROOF_VERSION,
            )

            client_cert_data = self._recv_identity(ssl_sock)
            if client_cert_data:
                (
                    peer_cert_pem,
                    peer_no_auto_pairing,
                    peer_listen_port,
                    client_device_name,
                    client_nonce,
                    client_proof,
                    client_version,
                ) = self._identity_payload(client_cert_data)
                peer_cert = x509.load_pem_x509_certificate(peer_cert_pem.encode())
                proved = self._pairing_mgr.verify_handshake_nonce(
                    peer_cert_pem, client_nonce, our_nonce, client_proof
                )
                # Our proof follows, and only now can it be made: it covers the
                # nonce that arrived in the frame just read.  The client is
                # waiting for it, which is why it is sent before anything that
                # could fail below -- a refusal would otherwise leave the peer
                # blocking on a frame that never comes.
                self._send_identity(
                    ssl_sock,
                    identity.certificate_pem,
                    device_name=identity.device_name,
                    nonce=our_nonce,
                    proof=self._pairing_mgr.sign_handshake_nonce(client_nonce, our_nonce),
                    version=PROOF_VERSION,
                )

                # Bind the TLS-presented cert to the app-layer identity cert
                # (defense-in-depth against a relay/MITM).
                tls_der = ssl_sock.getpeercert(binary_form=True)
                if tls_der is not None and tls_der != peer_cert.public_bytes(
                    serialization.Encoding.DER
                ):
                    logger.warning(
                        "TLS cert differs from identity cert from %s:%d — refusing",
                        addr[0],
                        addr[1],
                    )
                    self._send_rejection(ssl_sock)
                    ssl_sock.close()
                    return

                try:
                    cn_attrs = peer_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
                    if cn_attrs:
                        peer_id = _sanitize_peer_str(cn_attrs[0].value)
                except Exception:
                    pass

                # What the exchange established, said in the log.  A pinned peer
                # that could not prove itself is worth naming: it is either a
                # build from before this feature or an attempt to be that device
                # with a copied certificate, and the two cannot be told apart from
                # here.  The pin check below is what refuses the attack -- a copied
                # certificate is not the pinned one -- and a peer that *is* pinned
                # and merely old keeps working, which is what stops one side
                # upgrading from breaking every pairing it has.
                pinned = bool(peer_id and self._pairing_mgr.get_peer_certificate(peer_id))
                if not proved:
                    logger.info(
                        "[%s] handshake identity: %s (version %d, no valid proof)",
                        peer_id[:12] if peer_id else "?",
                        proof_state(proved, peer_id, pinned, client_version),
                        client_version,
                    )
                if should_refuse_unproven(
                    self._pairing_mgr, peer_id, proved, claimed_version=client_version
                ):
                    # A pinned device that cannot prove it holds the key behind
                    # the certificate it presented.  This side does not check the
                    # client certificate at the TLS layer -- it cannot, because
                    # every ClipSync certificate is self-signed (see
                    # `_build_ssl_context`) -- so this is the check that decides
                    # whether the peer is the device it names.
                    #
                    # Refused, and told why: the two ways to arrive here are an
                    # older build and an impostor presenting a copied
                    # certificate, and they are indistinguishable from here.
                    # Saying nothing would leave a user whose peer merely needs
                    # updating with a device that stopped working and no reason
                    # given, so the marker carries `REJECT_IDENTITY_UNPROVEN` and
                    # the other end can offer the one action that fixes it.
                    logger.warning(
                        "[%s] incoming connection at %s:%d claims %s but cannot "
                        "prove it holds that certificate's key (handshake version "
                        "%d, no valid proof) — refusing; re-pairing is required",
                        peer_id[:12],
                        addr[0],
                        addr[1],
                        peer_id[:12],
                        client_version,
                    )
                    self._send_rejection(ssl_sock, REJECT_IDENTITY_UNPROVEN)
                    ssl_sock.close()
                    # The user has to be told, and this is the one refusal with an
                    # action attached: re-pairing replaces the pin, which is the
                    # user saying "yes, this is that device".  Reported through the
                    # same channel as a changed certificate, because it is the same
                    # prompt -- the device is paired here with a certificate, and
                    # this connection presented one that could not be proven.
                    # Throttled there, so a peer reconnecting in a loop is answered
                    # once with the certificate from the prompt actually shown.
                    if self._on_security_alert and peer_id:
                        self._invoke_callback(
                            self._on_security_alert,
                            peer_name or peer_id,
                            peer_id,
                            self._pairing_mgr.get_peer_fingerprint(peer_id) or "",
                            fingerprint_pem(peer_cert_pem),
                            peer_cert_pem,
                        )
                    return

                try:
                    ou_attrs = peer_cert.subject.get_attributes_for_oid(
                        NameOID.ORGANIZATIONAL_UNIT_NAME
                    )
                    if ou_attrs:
                        peer_name = _sanitize_peer_str(ou_attrs[0].value)
                except Exception:
                    pass

                if client_device_name:
                    # The name the peer's user chose, which is the one to list it
                    # under -- the attribute above is written once, when that
                    # device's identity is minted, and is the name it had then.
                    # A peer older than the field sends none and keeps the
                    # certificate's reading, which is all it has ever known.
                    peer_name = client_device_name

                logger.info(
                    "Identity from %s:%d — peer_id=%s, peer_name=%s",
                    addr[0],
                    addr[1],
                    peer_id[:12] if peer_id else "N/A",
                    peer_name or "N/A",
                )
                # Refuse connections from peers the user has explicitly
                # rejected or forgotten.
                if peer_id and peer_id in self._rejected_peer_ids:
                    logger.warning(
                        "[%s] incoming connection from a peer this device removed — refusing; "
                        "pairing must be redone from that device",
                        peer_id[:12],
                    )
                    self._send_rejection(ssl_sock, REJECT_REMOVED)
                    ssl_sock.close()
                    return
                # A manual disconnect holds the peer off without the marker and
                # without the removal notice: it is a pause, and the peer's own
                # reconnect brings it back once the hold expires.
                if peer_id and self._on_hold(peer_id):
                    logger.info(
                        "[%s] incoming connection while held after a manual disconnect — "
                        "refusing for now",
                        peer_id[:12],
                    )
                    ssl_sock.close()
                    return
                was_paired = self._pairing_mgr.is_peer_paired(peer_id)
                self._pairing_mgr.add_peer(
                    peer_id,
                    peer_name,
                    peer_cert_pem,
                    paired=was_paired,
                )
                if not was_paired and peer_id and not peer_no_auto_pairing:
                    try:
                        shared_code = self._pairing_mgr.generate_shared_pairing_code(peer_id)
                        logger.info(
                            "[%s] pairing code: %s**** — verify on both devices",
                            peer_name or peer_id,
                            shared_code[:4],
                        )
                    except Exception as e:
                        logger.debug("Could not generate shared pairing code: %s", e)
                elif not was_paired and peer_id:
                    # The dialer said this connection is not a pairing request,
                    # so no code — and no card or notice asking to compare one.
                    logger.info(
                        "[%s] connected without a pairing request — no code generated",
                        peer_name or peer_id,
                    )
            else:
                logger.warning(
                    "No identity frame from %s:%d — anonymous connection", addr[0], addr[1]
                )

            display_id = peer_id or "unknown"
            peer_fp2 = self._pairing_mgr.get_peer_fingerprint(peer_id) if peer_id else ""
            conn = PeerConnection(
                display_id,
                peer_name or str(addr),
                ssl_sock,
                peer_fingerprint=peer_fp2,
                enc_mgr=self._enc_mgr,
                pairing_mgr=self._pairing_mgr,
                is_anonymous=not bool(peer_id),
            )
            if not self._start_peer(conn):
                return
            # Prevent the outer except handler from closing client_sock
            # now that PeerConnection owns the ssl_sock (which wraps it).
            client_sock = None
            ssl_sock = None

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
                    # Remember how to reach this peer.  It dialed us, so the
                    # address it came from is one we can dial back — with the
                    # port its own server listens on, which is the half the
                    # socket cannot tell us: ``addr[1]`` is the ephemeral port
                    # the call came from and reaching it reaches nothing.  A
                    # peer that sent no port (an older build) leaves whatever we
                    # already had, rather than a port we made up.
                    if peer_listen_port:
                        self._callback_addresses[peer_id] = (
                            peer_name,
                            addr[0],
                            peer_listen_port,
                        )
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
                    # can deduplicate.  The hash IS derivable from the real
                    # id (see peer_id_hash), so match exactly instead of the
                    # old IP-equality heuristic, which merged distinct
                    # devices whenever two peers shared one NATed source
                    # address (phone-hotspot topologies): whichever connected
                    # first claimed every hash registered for that IP.
                    expected_hash = peer_id_hash(peer_id)
                    for hash_id in list(self._peer_addresses):
                        if hash_id == expected_hash and hash_id != peer_id:
                            logger.debug(
                                "Mapping hash_id %s → real_id %s (derived)",
                                hash_id[:12],
                                peer_id[:12],
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
                            peer_name or peer_id,
                            peer_id[:12],
                            len(self._peers),
                        )
                    elif self._device_id > peer_id:
                        # Peer is lower — their outgoing (this incoming) wins.
                        logger.info(
                            "[%s] tiebreaker: peer's outgoing wins (peer=%s < we=%s) — replacing",
                            peer_id[:12],
                            peer_id[:12],
                            self._device_id[:12],
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
                    if not self._running:
                        conn.stop()
                        return
                    if self._peers.get(peer_id) is existing:
                        logger.info(
                            "[%s] tiebreaker: we should win but existing is dead — keeping incoming",  # noqa: E501
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
                    peer_id[:12],
                    self._device_id[:12],
                    peer_id[:12],
                )
                conn.set_on_disconnect(None)
                to_stop.append(conn)
            for c in to_stop:
                c.stop()

            logger.info(
                "Accepted connection from %s:%d [%s]",
                addr[0],
                addr[1],
                peer_id[:12] if peer_id else "N/A",
            )

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
                self._invoke_callback(
                    self._on_security_alert,
                    peer_name or "unknown",
                    peer_id,
                    expected_fp,
                    received_fp,
                    peer_cert_pem,
                )
            # Close both the SSL wrapper and the raw socket so no
            # half-open connection is left behind.
            if ssl_sock:
                with contextlib.suppress(Exception):
                    ssl_sock.close()
            if client_sock:
                with contextlib.suppress(Exception):
                    client_sock.close()
        except Exception as e:
            if self._running:
                logger.warning("Accept error: %s: %s", type(e).__name__, e, exc_info=True)
            if ssl_sock:
                with contextlib.suppress(Exception):
                    ssl_sock.close()
            if client_sock:
                with contextlib.suppress(Exception):
                    client_sock.close()
