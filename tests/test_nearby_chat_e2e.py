"""End-to-end integration test for Nearby Chat over the REAL TCP+TLS transport.

Two full stacks (``TransportManager`` + ``ChatManager``) run on localhost with
real Ed25519 identities, real ephemeral ports and a real TLS 1.3 socket
connection.  A invites B to chat, B accepts, A sends text and a multi-chunk
file — every frame rides the actual wire, through the transport's unpaired-peer
gate, which is exactly the path the in-memory unit tests bypass.

Regression coverage: the unpaired-peer gate once dropped chat FILE BYTES
(``file_chunk`` frames), so a file roundtrip that never involves a pairing code
is the core assertion here.  The whole flow runs with both pairing managers
reporting the peer as NOT paired.
"""

import os
import socket
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.protocol.codec import CHAT_MSG_TYPES  # noqa: E402
from internal.security.pairing import PairingManager  # noqa: E402
from internal.sync.nearby_chat import ChatManager  # noqa: E402
from internal.transport.connection import PortInUseError, TransportManager  # noqa: E402

DEV_A = "device-aaaa"
DEV_B = "device-bbbb"
NAME_A = "Device A"
NAME_B = "Device B"

# ~1.5 MB source file: comfortably more than one 256 KB chat chunk.
FILE_BYTES = 1500 * 1024


def _free_port() -> int:
    """Return a currently-free ephemeral TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _deadline(pred, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """Poll *pred* to a deadline instead of sleeping a fixed amount."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class _Stack:
    """One full transport + chat stack on its own ephemeral localhost port.

    ``route`` implements the host router contract: decoded ``CHAT_MSG_TYPES``
    frames go to ``handle_message``, ``file_chunk`` frames to
    ``handle_binary_chunk`` (chat right-of-first-refusal), everything else is
    ignored.  ``send_fn_to`` is a closure that pushes bytes to one peer over
    the transport, i.e. over the real TLS connection.
    """

    def __init__(self, device_id: str, device_name: str, receive_dir):
        self.pairing = PairingManager(device_id, device_name)
        self.pairing.load_or_create_identity("", "")
        self.chat = ChatManager(device_id, device_name, receive_dir=str(receive_dir))
        self.chat.set_own_fingerprint(self.pairing.get_identity().fingerprint)
        self.transport = TransportManager(device_id, device_name, _free_port(), self.pairing)
        self.transport.set_on_peer_message(self._route)
        self.incoming_invites: list[dict] = []
        self.chat.set_on_incoming_invite(self.incoming_invites.append)

    # ---- transport → chat wiring (mirrors the host router contract) --------

    def _route(self, msg, peer_id):
        send_fn = lambda data, p=peer_id: self.transport.send_to_peer(p, data)  # noqa: E731
        fp = self.transport.get_peer_fingerprint(peer_id)
        fp_short = self.chat.shorten_fingerprint(fp)
        msg_type = getattr(msg, "msg_type", "")
        if msg_type == "file_chunk":
            return self.chat.handle_binary_chunk(msg._raw_payload, peer_id, send_fn)
        if msg_type in CHAT_MSG_TYPES:
            return self.chat.handle_message(
                msg_type, msg._raw_payload, peer_id, fp_short, send_fn,
            )
        return False

    def send_fn_to(self, peer_id):
        return lambda data, p=peer_id: self.transport.send_to_peer(p, data)  # noqa: E731

    @property
    def port(self) -> int:
        return self.transport._port

    def start(self):
        """Bind the server, retrying through any ephemeral-port race."""
        for _ in range(25):
            try:
                self.transport.start_server()
                return
            except PortInUseError:
                self.transport._port = _free_port()
        pytest.skip("could not bind an ephemeral localhost port for the E2E test")

    def stop(self):
        try:
            self.chat.shutdown()
        finally:
            self.transport.stop_server()


class TestNearbyChatE2E:
    """Real-TLS nearby-chat roundtrips between two unpaired devices."""

    @pytest.fixture
    def rig(self, tmp_path):
        dir_a = tmp_path / "receive_a"
        dir_b = tmp_path / "receive_b"
        a = _Stack(DEV_A, NAME_A, dir_a)
        b = _Stack(DEV_B, NAME_B, dir_b)
        try:
            a.start()
            b.start()
        except OSError as e:
            a.stop()
            b.stop()
            pytest.skip(f"environment cannot bind localhost sockets: {e}")

        # A dials B directly over 127.0.0.1 (no mDNS in this test).
        a.transport.connect_to_peer(DEV_B, NAME_B, "127.0.0.1", b.port)
        assert _deadline(
            lambda: DEV_B in a.transport.get_connected_peers(), timeout=10,
        ), "A never connected to B"
        assert _deadline(
            lambda: DEV_A in b.transport.get_connected_peers(), timeout=10,
        ), "B never saw A as connected"

        rig = SimpleNamespace(a=a, b=b, dir_a=dir_a, dir_b=dir_b)
        yield rig
        a.stop()
        b.stop()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _status(stack: _Stack, peer_id: str) -> str | None:
        for s in stack.chat.get_sessions():
            if s["peer_id"] == peer_id:
                return s["status"]
        return None

    @staticmethod
    def _entry(stack: _Stack, sid: str, tid: str) -> dict | None:
        for e in stack.chat.get_messages(sid):
            if e["transfer_id"] == tid:
                return e
        return None

    # ---- tests -------------------------------------------------------------

    def test_unpaired_tls_connection_establishes(self, rig):
        assert DEV_B in rig.a.transport.get_connected_peers()
        assert DEV_A in rig.b.transport.get_connected_peers()
        # Both devices know each other's certs but never paired.
        assert not rig.a.pairing.is_peer_paired(DEV_B)
        assert not rig.b.pairing.is_peer_paired(DEV_A)

    def test_chat_invite_text_file_roundtrip_over_tls(self, rig):
        a, b = rig.a, rig.b

        # --- 1. invite → accept, both sessions active ---------------------
        sid = a.chat.start_session(DEV_B, NAME_B, "", a.send_fn_to(DEV_B))
        assert sid, "start_session returned None"
        assert _deadline(lambda: len(b.incoming_invites) == 1), \
            "chat invite never reached B over TLS"
        invite = b.incoming_invites[0]
        assert invite["peer_id"] == DEV_A
        assert invite["session_id"] == sid
        assert b.chat.accept_invitation(invite["session_id"], b.send_fn_to(DEV_A))
        assert _deadline(lambda: self._status(a, DEV_B) == "active"), \
            "A session never became active"
        assert _deadline(lambda: self._status(b, DEV_A) == "active"), \
            "B session never became active"

        # --- 2. text roundtrip ---------------------------------------------
        assert a.chat.send_text(sid, "hello over TLS", a.send_fn_to(DEV_B))
        assert _deadline(lambda: any(
            e["kind"] == "text" and e["text"] == "hello over TLS"
            for e in b.chat.get_messages(sid)
        )), "chat text never reached B"

        # --- 3. multi-chunk file roundtrip ---------------------------------
        payload = (bytes(range(256)) * (FILE_BYTES // 256)) + b"e2e-tail"
        src = rig.dir_a / "payload.bin"
        src.write_bytes(payload)

        tid = a.chat.send_file(sid, str(src), a.send_fn_to(DEV_B))
        assert tid, "send_file returned None"
        assert _deadline(lambda: (self._entry(b, sid, tid) or {}).get("status") == "await_accept"), \
            "file offer never reached B"
        assert b.chat.accept_file(sid, tid, b.send_fn_to(DEV_A))
        assert _deadline(lambda: (self._entry(b, sid, tid) or {}).get("status") == "done", timeout=20), \
            "B never finalized the received file"
        saved = (self._entry(b, sid, tid) or {}).get("saved_path")
        assert saved, "B did not record a saved path"
        assert os.path.getsize(saved) == len(payload)
        with open(saved, "rb") as fh:
            assert fh.read() == payload, "received file bytes differ from source"

        # Sender converges to "done" once the receiver's completion ack lands.
        assert _deadline(lambda: (self._entry(a, sid, tid) or {}).get("status") == "done", timeout=10), \
            "A never marked the send done"

        # --- 4. never paired, anywhere, at any point -----------------------
        assert not a.pairing.is_peer_paired(DEV_B)
        assert not b.pairing.is_peer_paired(DEV_A)
