"""The identity exchange, as a function of two channels rather than of a socket.

This is the piece the first three attempts at the handshake proof did not have.
The exchange is ordered -- each side's proof covers the *peer's* nonce, so the
side that speaks second is the one that can prove itself, and neither may wait for
a frame the other cannot send.  Getting that wrong is what made those attempts
either deadlock or tear the connection down, and the reason it was hard to see is
that the ordering was tangled up with TLS, threads and a reader that also probes
for rejection markers.

Stated as a function over two channels, the ordering is checkable in memory.  The
channel contract is two operations: ``send(bytes)`` and ``recv() -> bytes | None``.
The connection layer passes sockets; the tests pass pairs of queues.
"""

import contextlib
import logging
import struct

logger = logging.getLogger(__name__)

# The largest frame the identity exchange will read, matching the transport's own
# cap: the certificate plus its comment lines, and nothing legitimate beyond it.
MAX_IDENTITY_FRAME = 1 << 20

NONCE_PREFIX = "#clipsync-nonce="
PROOF_PREFIX = "#clipsync-proof="
VERSION_PREFIX = "#clipsync-v="

# The first handshake version that promises a proof of key possession.
#
# A version number rather than "did the proof field arrive", and the difference is
# the whole reason this constant exists: an attacker who has copied a certificate
# simply omits the field, so a gate keyed on presence is a gate an attacker opens
# by saying nothing.  A gate keyed on a *claim* cannot be dodged that way -- a peer
# that claims this version has promised a signature, and silence is a refusal --
# while a peer that does not claim it is a build from before the feature, which
# keeps working.  That is what makes the proof enforceable without breaking every
# existing pairing on the first upgrade.
PROOF_VERSION = 1


def should_refuse_unproven(pairing_mgr, claimed_device_id, proved, claimed_version=0):
    """Whether an incoming peer must be refused for failing to prove itself.

    The policy, in one place, because it is the security decision and it is one
    boolean away from being wrong in either direction:

    * a peer that *proved* possession is the device it claims -- accepted;
    * a peer that claimed `PROOF_VERSION` or later and did **not** prove itself is
      refused.  It promised a signature it did not produce, and the only peers that
      do that are ones lying about who they are or about what they are running;
    * a peer that did not claim that version is a build from before this feature.
      It is accepted, because refusing it would take every existing pairing down on
      the first upgrade, and because the *pin* is what refuses an impersonator in
      that case: a copied certificate is not the pinned one, so `add_peer` raises
      `CertificateChangedError` and the connection dies.  The residual risk lives
      here, and the caller logs it (`pinned-but-unproven`).

    The middle case is what makes this a gate rather than a report, and the version
    claim is what makes the middle case decidable.  An earlier version of this
    function refused every unproven peer claiming a pinned device -- which is the
    middle and bottom cases together -- and that would have broken every existing
    pairing the moment one side upgraded.
    """
    if proved:
        return False
    if not claimed_device_id:
        return False
    if int(claimed_version or 0) < PROOF_VERSION:
        return False
    return bool(pairing_mgr.get_peer_certificate(claimed_device_id))


def proof_state(proved, claimed_device_id, pinned, claimed_version=0):
    """A word for the log: how well this peer established who it is."""
    if proved:
        return "proved"
    if pinned:
        return "pinned-but-unproven"
    if int(claimed_version or 0) >= PROOF_VERSION:
        return "promised-a-proof-and-did-not"
    return "older-build" if claimed_device_id else "new-device"


def encode_identity(
    cert_pem,
    nonce="",
    proof="",
    listen_port=0,
    device_name="",
    no_auto_pairing=False,
    version=PROOF_VERSION,
):
    """One identity frame: our certificate, plus what proves we hold its key.

    The fields are comment lines in front of the PEM so that a reader which does
    not know them loads the same certificate -- a PEM parser scans for the BEGIN
    line -- and a frame from a build older than a field is that older frame
    exactly.
    """
    from internal.transport.connection import NO_PAIRING_MARKER, _sanitize_peer_str

    head = ""
    if version:
        head += f"{VERSION_PREFIX}{int(version)}\n"
    if nonce:
        head += f"{NONCE_PREFIX}{nonce}\n"
    if proof:
        head += f"{PROOF_PREFIX}{proof}\n"
    if listen_port:
        head += f"#clipsync-listen={int(listen_port)}\n"
    if device_name:
        head += f"#clipsync-name={_sanitize_peer_str(device_name, 128)}\n"
    text = head + cert_pem
    if no_auto_pairing:
        text += NO_PAIRING_MARKER
    return text.encode("utf-8")


def decode_identity(data):
    """Split an identity frame into ``(cert_pem, nonce, proof, listen_port, name)``.

    The scan stops at the first line that is not one of ours, which is the PEM's
    own BEGIN, so a certificate containing text that looks like a prefix cannot
    have its body consumed.  Every field defaults to its "absent" value, which is
    what a frame from an older build looks like.
    """
    from internal.transport.connection import (
        LISTEN_PORT_PREFIX,
        NAME_PREFIX,
        NO_PAIRING_MARKER,
        _sanitize_peer_str,
    )

    text = data.decode("utf-8", errors="replace")
    no_auto_pairing = text.endswith(NO_PAIRING_MARKER)
    if no_auto_pairing:
        text = text[: -len(NO_PAIRING_MARKER)]
    nonce = proof = name = ""
    listen_port = 0
    version = 0
    while True:
        head, sep, rest = text.partition("\n")
        if not sep or not rest:
            break
        if head.startswith(VERSION_PREFIX):
            with contextlib.suppress(ValueError):
                version = max(0, int(head[len(VERSION_PREFIX) :]))
        elif head.startswith(NONCE_PREFIX):
            nonce = _sanitize_peer_str(head[len(NONCE_PREFIX) :], 128)
        elif head.startswith(PROOF_PREFIX):
            proof = _sanitize_peer_str(head[len(PROOF_PREFIX) :], 512)
        elif head.startswith(LISTEN_PORT_PREFIX):
            with contextlib.suppress(ValueError):
                listen_port = max(0, int(head[len(LISTEN_PORT_PREFIX) :]))
        elif head.startswith(NAME_PREFIX):
            name = _sanitize_peer_str(head[len(NAME_PREFIX) :])
        else:
            break
        text = rest
    return text, nonce, proof, listen_port, name, no_auto_pairing, version


def send_frame(channel, payload):
    """Length-prefixed, matching the transport's framing."""
    channel.sendall(struct.pack(">I", len(payload)) + payload)


def recv_frame(channel):
    """One frame, or None when the peer closed or sent something unreadable."""
    header = b""
    while len(header) < 4:
        chunk = channel.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_IDENTITY_FRAME:
        return None
    body = b""
    while len(body) < length:
        chunk = channel.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return body


def exchange_identity(channel, pairing_mgr, server_side, device_name="", listen_port=0,
                      no_auto_pairing=False):
    """Run one side of the handshake.  Returns what the caller needs to decide.

    The order is asymmetric on purpose and it is the whole design:

    * the **accepting** side speaks first, because it has seen no nonce and so
      cannot make a proof yet;
    * the **dialing** side answers with its identity *and* its proof, which it can
      make from the nonce that just arrived;
    * the accepting side answers that with its own proof.

    Neither waits for a frame the other cannot send.  Both sides sign
    *(peer nonce, own nonce)*, so a proof is bound to this exchange and to no
    other: a signature recorded from an earlier handshake does not verify here,
    and a nonce cannot be reflected back as an answer.
    """
    identity = pairing_mgr.get_identity()
    our_nonce = pairing_mgr.make_handshake_nonce()

    if server_side:
        # First, with no proof: there is no peer nonce to cover yet.
        send_frame(
            channel,
            encode_identity(
                identity.certificate_pem,
                nonce=our_nonce,
                device_name=device_name or identity.device_name,
                version=PROOF_VERSION,
            ),
        )
        first = recv_frame(channel)
        if first is None:
            return None
        (
            peer_cert_pem,
            peer_nonce,
            peer_proof,
            peer_port,
            peer_name,
            peer_no_auto,
            peer_version,
        ) = decode_identity(first)
        # Their proof is already in hand, so ours can follow immediately.
        our_proof = pairing_mgr.sign_handshake_nonce(peer_nonce, our_nonce)
        send_frame(
            channel,
            encode_identity(
                identity.certificate_pem,
                nonce=our_nonce,
                proof=our_proof,
                device_name=device_name or identity.device_name,
                version=PROOF_VERSION,
            ),
        )
    else:
        first = recv_frame(channel)
        if first is None:
            return None
        (
            peer_cert_pem,
            peer_nonce,
            peer_proof,
            peer_port,
            peer_name,
            peer_no_auto,
            peer_version,
        ) = decode_identity(first)
        # Their nonce is in hand, so our proof rides our very first frame.
        our_proof = pairing_mgr.sign_handshake_nonce(peer_nonce, our_nonce)
        send_frame(
            channel,
            encode_identity(
                identity.certificate_pem,
                nonce=our_nonce,
                proof=our_proof,
                listen_port=listen_port,
                device_name=device_name or identity.device_name,
                no_auto_pairing=no_auto_pairing,
                version=PROOF_VERSION,
            ),
        )
        second = recv_frame(channel)
        if second is None:
            return None
        (
            _cert2,
            _nonce2,
            peer_proof,
            _port2,
            _name2,
            _noauto2,
            _version2,
        ) = decode_identity(second)

    proved = pairing_mgr.verify_handshake_nonce(
        peer_cert_pem, peer_nonce, our_nonce, peer_proof
    )
    return {
        "peer_cert_pem": peer_cert_pem,
        "peer_nonce": peer_nonce,
        "peer_name": peer_name,
        "peer_port": peer_port,
        "peer_no_auto_pairing": peer_no_auto,
        "peer_version": peer_version,
        "proved": proved,
        "our_nonce": our_nonce,
    }
