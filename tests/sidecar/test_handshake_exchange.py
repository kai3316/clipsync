"""The identity exchange over in-memory channels: the ordering, and the attack.

Companion to `tests/sidecar/test_identity_proof.py`, which pins the cryptography.
This pins the *exchange*: who speaks when, what each side learns, and whether a
device holding a copied certificate can get through.

The exchange had no testable shape before this file.  It lived inside
`_handle_accepted` and the dial path, tangled with TLS, worker threads and a
reader that also probes for rejection markers -- and three attempts to change it
either deadlocked or tore the connection down, with the reason invisible.  Two
channels and a function make the ordering something that can be asserted.
"""

import contextlib
import socket
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from internal.security.handshake import (  # noqa: E402
    PROOF_VERSION,
    decode_identity,
    encode_identity,
    exchange_identity,
    proof_state,
    recv_frame,
    send_frame,
    should_refuse_unproven,
)
from internal.security.pairing import PairingManager  # noqa: E402
from internal.transport.connection import (  # noqa: E402
    MAX_FRAME_SIZE,
    TransportManager,
)


class MemoryChannel:
    """One end of a real byte stream, with no TLS and no threads behind it.

    A queue was tried first and deadlocked, and the reason is worth keeping: a
    queue delivers *messages* while ``recv`` is a *byte stream*.  The framing code
    reads a 4-byte header and then the body, so a channel that hands back the whole
    frame for the first read leaves the second read with nothing -- which is a
    defect in the test double, not in the exchange it is meant to be checking.
    ``socketpair`` is a genuine stream, so the framing under test is the framing
    that ships.
    """

    def __init__(self, sock):
        self._sock = sock

    def sendall(self, payload):
        self._sock.sendall(payload)

    def recv(self, size):
        return self._sock.recv(size)

    def close(self):
        with contextlib.suppress(Exception):
            self._sock.close()


def channel_pair():
    left, right = socket.socketpair()
    for sock in (left, right):
        sock.settimeout(10)
    return MemoryChannel(left), MemoryChannel(right), (left, right)


def identity(device_id, tmp_path, name=None):
    manager = PairingManager(device_id, name or device_id)
    manager.load_or_create_identity(str(tmp_path / device_id), "")
    return manager


def paired_pair(tmp_path):
    peer = identity("peer-device", tmp_path, "Peer")
    victim = identity("victim-device", tmp_path, "Victim")
    victim.add_peer("peer-device", "Peer", peer.get_identity().certificate_pem, paired=True)
    peer.add_peer("victim-device", "Victim", victim.get_identity().certificate_pem, paired=True)
    return victim, peer


def run_exchange(server_mgr, client_mgr, server_side_first=True, **client_kwargs):
    """Drive both sides, in threads, and return (accept_result, dial_result)."""
    victim_channel, peer_channel, sockets = channel_pair()
    results = {}

    def serve():
        try:
            results["accept"] = exchange_identity(victim_channel, server_mgr, server_side=True)
        except Exception as exc:  # noqa: BLE001 - surfaced as a test failure below
            results["accept_error"] = exc

    def dial():
        try:
            results["dial"] = exchange_identity(
                peer_channel, client_mgr, server_side=False, **client_kwargs
            )
        except Exception as exc:  # noqa: BLE001
            results["dial_error"] = exc

    threads = [threading.Thread(target=serve), threading.Thread(target=dial)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    for sock in sockets:
        with contextlib.suppress(Exception):
            sock.close()
    assert not any(thread.is_alive() for thread in threads), (
        "the exchange deadlocked: a side waited for a frame the other cannot send"
    )
    for side in ("accept", "dial"):
        assert f"{side}_error" not in results, f"{side} side raised: {results.get(side + '_error')}"
    return results["accept"], results["dial"]


class TestTheTwoHonestDevicesProveThemselves:
    def test_both_sides_verify_each_other(self, tmp_path):
        victim, peer = paired_pair(tmp_path)
        accepted, dialed = run_exchange(victim, peer)
        assert accepted is not None and dialed is not None
        assert accepted["proved"], "the accepting side did not verify the dialing side"
        assert dialed["proved"], "the dialing side did not verify the accepting side"

    def test_each_side_learns_the_name_the_other_chooses(self, tmp_path):
        """The name is a field of the exchange, and the rename path depends on it."""
        victim, peer = paired_pair(tmp_path)
        peer.set_device_name("书房的本子")
        accepted, _dialed = run_exchange(victim, peer)
        assert accepted["peer_name"] == "书房的本子"

    def test_the_dialing_side_reports_its_own_listen_port(self, tmp_path):
        """The accepting side needs it to dial back; the socket cannot tell it."""
        victim, peer = paired_pair(tmp_path)
        accepted, _dialed = run_exchange(victim, peer, listen_port=45731)
        assert accepted["peer_port"] == 45731


class TestThePolicyDecidesTheRightQuestion:
    """The security decision: which case refuses, and which must not.

    Shaped by two mistakes, both recorded here because both looked reasonable:

    1. The first version refused *every* unproven peer claiming a pinned device,
       with no way to tell an older build from an impostor.
    2. The second used the handshake version to tell them apart -- a peer claiming
       the current version had promised a signature and was refused, an older one
       was accepted.  The claim is free, so an impostor just says "older build".
       A gate an attacker opens by choosing a number is not a gate.

    The decision now is strict: unproven plus pinned is refused, whatever version
    is claimed, and the other end is told to re-pair.
    """

    def test_a_proved_peer_is_accepted(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(victim, "peer-device", proved=True, claimed_version=1)

    def test_an_unproven_claim_on_a_pinned_device_is_refused(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        assert should_refuse_unproven(victim, "peer-device", proved=False, claimed_version=1)

    def test_claiming_an_old_version_does_not_open_the_gate(self, tmp_path):
        """The load-bearing assertion of the strict policy.

        An impostor holding a copied certificate sends no proof.  It can also send
        any version it likes.  Neither gets it through, which is the difference
        between a gate and a report.
        """
        victim, _peer = paired_pair(tmp_path)
        for version in (0, 1, 2, 99):
            assert should_refuse_unproven(
                victim, "peer-device", proved=False, claimed_version=version
            ), f"claiming version {version} was accepted without a proof"

    def test_a_forged_signature_does_not_open_the_gate(self, tmp_path):
        """The full attack: the right version, a signature by another key."""
        victim, peer = paired_pair(tmp_path)
        stranger = identity("attacker-device", tmp_path, "Attacker")
        victim_nonce = victim.make_handshake_nonce()
        stranger_nonce = stranger.make_handshake_nonce()
        forged = stranger.sign_handshake_nonce(victim_nonce, stranger_nonce)
        proved = victim.verify_handshake_nonce(
            peer.get_identity().certificate_pem, stranger_nonce, victim_nonce, forged
        )
        assert not proved
        assert should_refuse_unproven(victim, "peer-device", proved, claimed_version=1)

    def test_an_unproven_stranger_is_first_contact_not_an_attack(self, tmp_path):
        """No pin means nothing to refuse on: the pairing code establishes trust."""
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(
            victim, "never-seen-device", proved=False, claimed_version=1
        )

    def test_a_peer_with_no_claimed_id_is_not_refused_on_the_proof(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(victim, "", proved=False, claimed_version=1)

    def test_the_log_says_which_kind_of_unproven_peer_this_is(self):
        """Diagnostic only -- nothing branches on these -- but a reader working out
        why a peer could not prove itself wants to know whether it even tried."""
        assert proof_state(True, "peer-device", True) == "proved"
        assert proof_state(False, "peer-device", True, claimed_version=0) == "pinned-unproven-v0"
        assert proof_state(False, "peer-device", True, claimed_version=1) == "pinned-unproven-v1"
        assert proof_state(False, "", False) == "new-device"
        assert (
            proof_state(False, "stranger", False, claimed_version=1)
            == "unpinned-promised-a-proof"
        )
        assert proof_state(False, "stranger", False, claimed_version=0) == "unpinned-v0"


class TestTheVersionSurvivesTheExchange:
    """The claim the policy reads must actually arrive, or the gate is blind."""

    def test_both_sides_learn_the_other_version(self, tmp_path):
        victim, peer = paired_pair(tmp_path)
        accepted, dialed = run_exchange(victim, peer)
        assert accepted["peer_version"] == PROOF_VERSION
        assert dialed["peer_version"] == PROOF_VERSION

    def test_a_frame_without_the_field_reads_as_version_zero(self, tmp_path):
        """An older build sends no version line at all, and that is version 0."""
        victim, _peer = paired_pair(tmp_path)
        from internal.security.handshake import encode_identity

        frame = encode_identity(
            victim.get_identity().certificate_pem, nonce="n" * 64, version=0
        )
        assert decode_identity(frame)[6] == 0

    def test_the_version_round_trips(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        from internal.security.handshake import encode_identity

        frame = encode_identity(
            victim.get_identity().certificate_pem, nonce="n" * 64, version=7
        )
        assert decode_identity(frame)[6] == 7

    def test_the_exchange_declares_the_version_it_implements(self):
        """A build that sends a proof must also declare the version.

        Without the declaration the peer cannot tell it apart from an older build,
        and the gate would have nothing to act on.
        """
        source = (
            Path(__file__).resolve().parents[2] / "internal" / "security" / "handshake.py"
        ).read_text(encoding="utf-8")
        assert source.count("version=PROOF_VERSION") >= 3, (
            "not every frame the exchange sends declares the version"
        )


class TestTheCopiedCertificateIsRefused:
    def test_a_thief_holding_the_certificate_fails_the_proof(self, tmp_path):
        """The attack this feature exists for, run through the real exchange.

        The attacker presents the peer's certificate -- the exact bytes that peer
        sends on the wire, which anyone can read off a handshake -- and signs with
        a key of its own.  The accepting side checks the signature against the
        certificate *in the frame*, so it fails.
        """
        victim, peer = paired_pair(tmp_path)
        stolen = peer.get_identity().certificate_pem
        attacker = Thief("attacker-device", "Peer", stolen, peer)

        accepted, _dialed = run_exchange(victim, attacker)
        assert accepted is not None
        assert accepted["peer_cert_pem"] == stolen, (
            "the accepting side should have been handed the stolen certificate"
        )
        assert not accepted["proved"], (
            "a signature by another key verified against the stolen certificate"
        )
        # And the gate refuses it: the device it claims is one this machine pinned.
        assert victim.get_peer_certificate("peer-device")


class Thief(PairingManager):
    """A real pairing manager wearing someone else's certificate.

    Built by subclassing rather than by a stub, so the exchange runs against the
    real machinery and the only lie is which certificate it presents and which key
    it signs with -- which is exactly the position an impersonator is in.
    """

    def __init__(self, device_id, claimed_name, stolen_certificate, victim_of_theft):
        super().__init__(device_id, claimed_name)
        self.load_or_create_identity("", "")
        # Read the real identity through the parent, once, before the override can
        # intercept: `load_or_create_identity` itself reaches `get_identity`, so an
        # override that read its own attributes would recurse into an object that
        # is not finished being built.
        real = PairingManager.get_identity(self)
        self._stolen_certificate = stolen_certificate
        self._own_key = real.private_key
        self._claimed_name = claimed_name
        self._own_fingerprint = real.fingerprint

    def get_identity(self):
        return SimpleNamespace(
            # The public certificate of the device it is pretending to be...
            certificate_pem=self._stolen_certificate,
            # ...with a signature made by a key that is not the one behind it.
            private_key=self._own_key,
            device_name=self._claimed_name,
            fingerprint=self._own_fingerprint,
        )


class CaptureChannel:
    """A channel that keeps what was written to it, for byte-level assertions."""

    def __init__(self):
        self.data = bytearray()

    def sendall(self, payload):
        self.data += payload


class BytesChannel:
    """A channel that hands out one buffer, ``recv``-sized, like a socket."""

    def __init__(self, data):
        self.buffer = bytes(data)
        self.timeout = None

    def recv(self, size):
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout


class TestProductionAndTheExchangeShareOneFrame:
    """The connection layer's identity frame is handshake.py's frame.

    Production used to carry its own encoder and parser beside
    ``handshake.encode_identity``/``decode_identity`` -- the same loop written
    twice, which is how a wire format drifts while every test stays green.  These
    drive the production entry points against the exchange's: byte for byte for
    the encoder, field for field for the parser, and in both directions across
    the two.
    """

    def _frame(self, manager, **fields):
        capture = CaptureChannel()
        TransportManager._send_identity(
            capture, manager.get_identity().certificate_pem, **fields
        )
        return bytes(capture.data)

    def test_send_identity_writes_exactly_what_encode_identity_writes(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        fields = {
            "nonce": "n" * 64,
            "proof": "ab" * 64,
            "listen_port": 45731,
            "device_name": "Desk",
            "no_auto_pairing": True,
            "version": PROOF_VERSION,
        }
        expected = CaptureChannel()
        send_frame(
            expected,
            encode_identity(victim.get_identity().certificate_pem, **fields),
        )
        assert self._frame(victim, **fields) == bytes(expected.data)

    def test_the_frame_is_still_the_documented_bytes(self, tmp_path):
        """The refactor must not quietly re-order or re-spell the head.

        Built from the literal field names rather than from either encoder, so
        this test says what "the same frame" means: version, nonce, proof, port,
        name, then the PEM, then the no-pairing marker.
        """
        victim, _peer = paired_pair(tmp_path)
        cert = victim.get_identity().certificate_pem
        head = (
            "#clipsync-v=1\n"
            "#clipsync-nonce=" + "n" * 64 + "\n"
            "#clipsync-proof=" + "ab" * 64 + "\n"
            "#clipsync-listen=45731\n"
            "#clipsync-name=Desk\n"
        )
        body = (head + cert + "\n#clipsync-no-pairing").encode()
        assert self._frame(
            victim,
            nonce="n" * 64,
            proof="ab" * 64,
            listen_port=45731,
            device_name="Desk",
            no_auto_pairing=True,
            version=1,
        ) == struct.pack(">I", len(body)) + body

    def test_identity_payload_reads_every_field_decode_identity_reads(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        name = "\u4e66\u623f\u7684\u7b14\u8bb0\u672c"
        frame = self._frame(
            victim,
            nonce="n" * 64,
            proof="ab" * 64,
            listen_port=45731,
            device_name=name,
            no_auto_pairing=True,
            version=7,
        )
        body = frame[4:]
        production = TransportManager._identity_payload(body)
        decoded = decode_identity(body)
        assert production == (
            decoded[0],
            decoded[5],
            decoded[3],
            decoded[4],
            decoded[1],
            decoded[2],
            decoded[6],
        )
        cert, no_auto_pairing, listen_port, device_name, nonce, proof, version = production
        assert cert == victim.get_identity().certificate_pem
        assert no_auto_pairing is True
        assert listen_port == 45731
        assert device_name == name
        assert nonce == "n" * 64
        assert proof == "ab" * 64
        assert version == 7

    def test_recv_identity_reads_what_the_exchange_writes(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        frame = self._frame(victim, nonce="n" * 64, version=PROOF_VERSION)
        assert recv_frame(BytesChannel(frame)) == frame[4:]
        assert TransportManager._recv_identity(BytesChannel(frame)) == frame[4:]

    def test_a_frame_from_the_oldest_build_reads_the_same_both_ways(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        frame = self._frame(victim)
        assert TransportManager._identity_payload(frame[4:]) == (
            victim.get_identity().certificate_pem,
            False,
            0,
            "",
            "",
            "",
            0,
        )
        assert decode_identity(frame[4:]) == (
            victim.get_identity().certificate_pem,
            "",
            "",
            0,
            "",
            False,
            0,
        )

    def test_a_field_whose_value_cannot_be_read_is_consumed_both_ways(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        cert = victim.get_identity().certificate_pem
        body = (f"#clipsync-v=not-a-number\n#clipsync-listen=also-not\n{cert}").encode()
        assert TransportManager._identity_payload(body) == (cert, False, 0, "", "", "", 0)
        assert decode_identity(body) == (cert, "", "", 0, "", False, 0)

    def test_the_long_fields_are_capped_at_the_same_lengths(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        cert = victim.get_identity().certificate_pem
        body = (
            "#clipsync-nonce=" + "n" * 300 + "\n"
            "#clipsync-proof=" + "p" * 600 + "\n"
            "#clipsync-name=" + "N" * 200 + "\n"
            + cert
        ).encode()
        _cert, _no_auto, _port, name, nonce, proof, _version = (
            TransportManager._identity_payload(body)
        )
        assert (len(nonce), len(proof), len(name)) == (128, 512, 64)
        decoded = decode_identity(body)
        assert (len(decoded[1]), len(decoded[2]), len(decoded[4])) == (128, 512, 64)

    def test_the_transport_cap_is_not_silently_the_handshake_default(self):
        """The transport accepted a larger frame before the parser was shared.

        ``handshake.recv_frame``'s own ceiling is 1 MiB; ``_recv_identity`` used
        to accept up to the transport's 10 MiB.  The explicit ``max_frame`` is
        what keeps that from becoming a hidden tightening.
        """
        body = b"x" * ((1 << 20) + 1)
        frame = struct.pack(">I", len(body)) + body
        assert recv_frame(BytesChannel(frame)) is None
        assert TransportManager._recv_identity(BytesChannel(frame)) == body

    def test_a_zero_length_or_oversized_frame_is_refused(self):
        assert TransportManager._recv_identity(BytesChannel(b"\x00\x00\x00\x00")) is None
        oversized = struct.pack(">I", MAX_FRAME_SIZE + 1)
        assert TransportManager._recv_identity(BytesChannel(oversized)) is None

    def test_the_wire_prefixes_have_one_definition(self):
        """The format literals live in connection.py, not in two modules."""
        source = (
            Path(__file__).resolve().parents[2]
            / "internal"
            / "security"
            / "handshake.py"
        ).read_text(encoding="utf-8")
        for literal in (
            "#clipsync-v=",
            "#clipsync-nonce=",
            "#clipsync-proof=",
            "#clipsync-listen=",
            "#clipsync-name=",
            "#clipsync-no-pairing",
        ):
            assert literal not in source, f"{literal} is defined in handshake.py too"
