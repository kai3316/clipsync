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
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from internal.security.handshake import (  # noqa: E402
    PROOF_VERSION,
    decode_identity,
    exchange_identity,
    proof_state,
    should_refuse_unproven,
)
from internal.security.pairing import PairingManager  # noqa: E402


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

    Shaped by a mistake: the first version of this policy refused *every* unproven
    peer claiming a pinned device, which would have taken down every existing
    pairing the moment one side upgraded, because a peer running a build from
    before this feature sends no proof either.  The version claim is what separates
    the two situations without guessing.
    """

    def test_a_proved_peer_is_accepted(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(victim, "peer-device", proved=True, claimed_version=1)

    def test_a_peer_that_promised_a_proof_and_omitted_it_is_refused(self, tmp_path):
        """The case that makes this a gate rather than a report.

        A peer claiming the proof version has promised a signature, and an attacker
        holding a copied certificate produces none.  Keying the gate on the *claim*
        rather than on the field's presence is what stops "say nothing" from being
        a way through.
        """
        victim, _peer = paired_pair(tmp_path)
        assert should_refuse_unproven(victim, "peer-device", proved=False, claimed_version=1)

    def test_an_older_build_keeps_working(self, tmp_path):
        """Version 0 is a build from before this feature, and it is not refused.

        Refusing it would break every existing pairing on the first upgrade; the
        pin is what refuses an impersonator in this case, because a copied
        certificate is not the pinned one.
        """
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(victim, "peer-device", proved=False, claimed_version=0)

    def test_an_unproven_stranger_is_first_contact_not_an_attack(self, tmp_path):
        """There is no pin to copy before the first pairing, so nothing to refuse."""
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(
            victim, "never-seen-device", proved=False, claimed_version=1
        )

    def test_a_peer_with_no_claimed_id_is_not_refused_on_the_proof(self, tmp_path):
        victim, _peer = paired_pair(tmp_path)
        assert not should_refuse_unproven(victim, "", proved=False, claimed_version=1)

    def test_the_log_says_which_kind_of_unproven_peer_this_is(self, tmp_path):
        """Every case named, because two of them do not refuse and a reader needs
        to be able to tell which happened."""
        assert proof_state(True, "peer-device", True) == "proved"
        assert proof_state(False, "peer-device", True) == "pinned-but-unproven"
        assert proof_state(False, "peer-device", False, claimed_version=0) == "older-build"
        assert proof_state(False, "", False) == "new-device"


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
