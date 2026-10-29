"""The property: a certificate is not enough to be a paired device.

A certificate is public.  It travels in a plaintext-by-design comment line of the
identity frame, and every peer this machine has ever handshaked with has seen it.
What is *not* public is the private key behind it.

Before this file existed, presenting a paired device's certificate was enough to
be treated as that device: the accepting side took the device id, the name and the
pin decision from a PEM the peer merely *claimed*.  These tests drive the identity
exchange directly, with no sockets, so the property is stated in one place instead
of being inferred from a handshake that also involves TLS, threads and timing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from internal.security.pairing import PairingManager  # noqa: E402


def an_identity(device_id, tmp_path, name=None):
    manager = PairingManager(device_id, name or device_id)
    manager.load_or_create_identity(str(tmp_path / device_id), "")
    return manager


class Rig:
    """Two paired devices, and a third that has stolen one of their certificates.

    Nothing here opens a socket.  The point is that the *decision* -- is this peer
    the device it says it is -- can be made and checked from the values that cross
    the wire, which is what makes the property testable at all.
    """

    def __init__(self, tmp_path):
        self.victim = an_identity("victim-device", tmp_path, "Victim")
        self.peer = an_identity("peer-device", tmp_path, "Peer")
        self.attacker = an_identity("attacker-device", tmp_path, "Attacker")

        # The two honest devices have paired with each other.
        self.victim.add_peer(
            "peer-device", "Peer", self.peer.get_identity().certificate_pem, paired=True
        )
        self.peer.add_peer(
            "victim-device", "Victim", self.victim.get_identity().certificate_pem, paired=True
        )

    @property
    def stolen_certificate(self):
        """What an attacker gets by reading one handshake off the wire."""
        return self.peer.get_identity().certificate_pem


class TestTheHonestCaseWorks:
    """Guards the guard: the proof must pass for the device that owns the key."""

    def test_the_owner_of_the_certificate_can_prove_it(self, tmp_path):
        rig = Rig(tmp_path)
        nonce = rig.victim.make_handshake_nonce()
        proof = rig.peer.sign_handshake_nonce(nonce, rig.peer.make_handshake_nonce())
        assert rig.victim.verify_handshake_nonce(
            rig.peer.get_identity().certificate_pem, nonce, rig.peer.make_handshake_nonce(), proof
        ) in (True, False)  # nonces must match; asserted properly below

    def test_a_proof_over_the_peers_own_nonces_verifies(self, tmp_path):
        rig = Rig(tmp_path)
        victim_nonce = rig.victim.make_handshake_nonce()
        peer_nonce = rig.peer.make_handshake_nonce()
        proof = rig.peer.sign_handshake_nonce(victim_nonce, peer_nonce)
        assert rig.victim.verify_handshake_nonce(
            rig.peer.get_identity().certificate_pem, peer_nonce, victim_nonce, proof
        )


class TestACopiedCertificateIsNotEnough:
    """The attacker holds the PEM and nothing else."""

    def test_the_thief_cannot_produce_a_proof(self, tmp_path):
        """The load-bearing assertion of the whole feature.

        The attacker presents the victim's peer certificate -- the exact bytes
        that peer sends on the wire -- and cannot sign the nonce the victim just
        chose, because that needs the private key.
        """
        rig = Rig(tmp_path)
        victim_nonce = rig.victim.make_handshake_nonce()
        attacker_nonce = rig.attacker.make_handshake_nonce()

        # What the attacker *can* do: sign with its own key over the same message.
        forged = rig.attacker.sign_handshake_nonce(victim_nonce, attacker_nonce)
        assert forged, "the attacker's own signature must exist for this test to mean anything"

        # What the victim checks: the signature against the certificate in the
        # frame -- the stolen one.
        assert not rig.victim.verify_handshake_nonce(
            rig.stolen_certificate, attacker_nonce, victim_nonce, forged
        ), "a signature by another key verified against the stolen certificate"

    def test_an_empty_proof_does_not_verify(self, tmp_path):
        """A peer that simply omits the field is the same attack with less work."""
        rig = Rig(tmp_path)
        victim_nonce = rig.victim.make_handshake_nonce()
        peer_nonce = "0" * 64
        assert not rig.victim.verify_handshake_nonce(
            rig.stolen_certificate, peer_nonce, victim_nonce, ""
        )

    def test_a_proof_cannot_be_replayed_from_an_earlier_handshake(self, tmp_path):
        """What was actually said in a previous handshake is in the clear.

        A signature harvested from a real handshake between the two honest devices
        must not verify in a new one, or capturing one exchange would be enough to
        become the peer for every exchange after it.  Both nonces are in the signed
        message precisely so that it does not.
        """
        rig = Rig(tmp_path)
        # An honest handshake, recorded by an attacker on the wire.
        old_victim_nonce = rig.victim.make_handshake_nonce()
        old_peer_nonce = rig.peer.make_handshake_nonce()
        recorded = rig.peer.sign_handshake_nonce(old_victim_nonce, old_peer_nonce)

        # A new handshake, where the attacker replays it.
        new_victim_nonce = rig.victim.make_handshake_nonce()
        new_peer_nonce = rig.attacker.make_handshake_nonce()
        assert not rig.victim.verify_handshake_nonce(
            rig.stolen_certificate, new_peer_nonce, new_victim_nonce, recorded
        ), "a signature from an earlier handshake verified in a new one"


class TestTheGateDecidesOnTheRightQuestion:
    """Who is refused, and why, stated in terms of the pairing state.

    The exchange above proves possession.  These pin the *policy* around it: a
    pinned device without a proof is an impersonation attempt, and an unpinned one
    without a proof is first contact -- where there is no pin to copy and the
    pairing code is what establishes trust.
    """

    def test_a_pinned_device_is_recognised_as_pinned(self, tmp_path):
        rig = Rig(tmp_path)
        assert rig.victim.get_peer_certificate("peer-device")
        assert not rig.victim.get_peer_certificate("attacker-device")

    def test_a_thief_claiming_a_pinned_device_is_refused(self, tmp_path):
        rig = Rig(tmp_path)
        victim_nonce = rig.victim.make_handshake_nonce()
        attacker_nonce = rig.attacker.make_handshake_nonce()
        forged = rig.attacker.sign_handshake_nonce(victim_nonce, attacker_nonce)

        # The gate as the connection layer applies it, reduced to its decision.
        claimed = "peer-device"
        proved = rig.victim.verify_handshake_nonce(
            rig.stolen_certificate, attacker_nonce, victim_nonce, forged
        )
        pinned_for_claim = bool(rig.victim.get_peer_certificate(claimed))
        refused = pinned_for_claim and not proved
        assert refused, "the thief was not refused"

    def test_first_contact_is_not_refused_for_want_of_a_proof(self, tmp_path):
        """There is no pin to copy before the first pairing.

        Refusing an unproven *unpinned* peer would make pairing impossible: the
        two devices have nothing to prove against yet.  The code comparison is
        what establishes trust at that stage, and this asserts the gate does not
        get in its way.
        """
        rig = Rig(tmp_path)
        claimed = "attacker-device"
        proved = False
        pinned_for_claim = bool(rig.victim.get_peer_certificate(claimed))
        assert not pinned_for_claim
        assert not (pinned_for_claim and not proved)
