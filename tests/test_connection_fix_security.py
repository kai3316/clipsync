"""Security-path fixes: what a refused identity means, and a broken frame key.

Two defects share the theme "the transport knows something it never says":

* the accepting side refused a pinned peer that could not prove itself and then
  told the app layer "certificate changed" even when the presented PEM was
  byte-for-byte the pinned one (a LAN host copying a public certificate).  The
  alert now carries `changed`, so the app can show a non-destructive notice
  instead of the dialog whose "keep unpaired" answer revokes a healthy device;
* two devices with different app-layer encryption passwords dropped the link on
  every clipboard frame and reconnected forever, with one log line per frame.
  Auth-tag failures now enter the same bad-key state as an on/off mismatch and
  report which of the two it was.
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.security.encryption import is_encrypted
from internal.security.pairing import fingerprint_pem
from internal.transport import connection as C  # noqa: N812

# --------------------------------------------------------------- test helpers


class _PinnedPairing:
    """A pairing store with one pinned certificate for the peer under test."""

    def __init__(self, certificate_pem, fingerprint="", paired=True):
        self._certificate = certificate_pem
        self._fingerprint = fingerprint
        self._paired = paired

    def get_peer_certificate(self, device_id):
        return self._certificate

    def get_peer_fingerprint(self, device_id):
        return self._fingerprint

    def is_peer_paired(self, device_id):
        return self._paired


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def shutdown(self, how):
        pass

    def settimeout(self, value):
        pass

    def getpeercert(self, binary_form=False):
        # None: this side never asks for a client certificate on accept, so the
        # TLS-cert-vs-identity-cert binding has nothing to compare here.
        return None


class _SockBuf:
    """Socket whose reads serve a fixed byte string, so a recv loop can run."""

    def __init__(self, data):
        self._buffer = data

    def settimeout(self, value):
        pass

    def gettimeout(self):
        return 30.0

    def setsockopt(self, *args):
        pass

    def recv(self, size):
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def shutdown(self, how):
        pass

    def close(self):
        pass


class _NeverDecrypts:
    """An encryption manager whose peer key is wrong (the password case)."""

    def decrypt_frame(self, data, peer_fingerprint):
        return None

    def encrypt_frame(self, data, peer_fingerprint):
        return data


def _manager(pairing):
    manager = C.TransportManager("dev-a", "Device A", 0, pairing)
    manager._running = True
    return manager


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _identity_exchange(peer_cert_pem, proved=False, name="Device B"):
    return {
        "peer_cert_pem": peer_cert_pem,
        "peer_nonce": "abcd",
        "peer_proof": "",
        "peer_name": name,
        "peer_port": 7000,
        "peer_no_auto_pairing": False,
        "peer_version": 1,
        "proved": proved,
        "our_nonce": "efgh",
    }


def _present_identity(manager, peer_cert_pem, proved=False):
    """Drive one accepted connection through the identity checks, return alerts."""
    alerts = []
    rejections = []
    manager.set_on_security_alert(lambda *args: alerts.append(args))
    manager._wrap_socket = lambda context, sock, **kwargs: _FakeSocket()
    manager._identity_exchange = lambda sock, server_side=False, **kw: _identity_exchange(
        peer_cert_pem, proved=proved
    )
    manager._send_rejection = lambda sock, reason=0: rejections.append(reason)
    client_sock = _FakeSocket()
    try:
        manager._handle_accepted(client_sock, ("127.0.0.1", 51234), object())
    finally:
        manager._running = False
        manager._pending_sockets.clear()
    return alerts, rejections


def _certificate_pem(cn):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID

    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .sign(key, None)
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _framed(payloads):
    return b"".join(struct.pack(">I", len(p)) + p for p in payloads)


def _run_recv_loop(connection):
    connection.start()
    connection._recv_thread.join(5)
    assert not connection._recv_thread.is_alive(), "the recv loop did not stop"


# -------------------------------------- severe-8: copied PEM is not "changed"


def test_a_copied_certificate_is_refused_but_reported_as_unchanged():
    """A host presenting the pinned PEM without the key changed nothing.

    Any LAN host can open the accept port, read the identity PEM this side sends
    first, dial back with that same public certificate and no proof.  The accept
    side refuses it correctly.  But the pinned fingerprint equals the presented
    one, so "certificate changed" is false — and the dialog behind that claim
    offers "keep unpaired", which really does revoke the healthy device.
    """
    cert_pem = _certificate_pem("dev-b")
    manager = _manager(_PinnedPairing(cert_pem, fingerprint_pem(cert_pem)))
    alerts, rejections = _present_identity(manager, cert_pem, proved=False)

    assert rejections == [C.REJECT_IDENTITY_UNPROVEN]
    assert len(alerts) == 1, alerts
    name, peer_id, expected_fp, received_fp, presented_pem, changed = alerts[0]
    assert peer_id == "dev-b"
    assert expected_fp == received_fp == fingerprint_pem(cert_pem)
    assert presented_pem == cert_pem
    assert changed is False, "a copied PEM was reported as a changed certificate"


def test_a_genuinely_different_certificate_is_reported_as_changed():
    """Control: a real pin mismatch is still a change, with both fingerprints.

    The app needs both to offer "trust again"; dropping the comparison must not
    turn every real change into a benign notice either.
    """
    cert_pem = _certificate_pem("dev-b")
    old_fp = ":".join(["AA"] * 32)
    manager = _manager(_PinnedPairing(cert_pem, fingerprint=old_fp))
    alerts, _rejections = _present_identity(manager, cert_pem, proved=False)

    assert len(alerts) == 1, alerts
    _name, _peer_id, expected_fp, received_fp, presented_pem, changed = alerts[0]
    assert expected_fp == old_fp
    assert received_fp == fingerprint_pem(cert_pem)
    assert presented_pem == cert_pem
    assert changed is True


# ---------------------------------- medium-17: different passwords, bad key state


def test_different_passwords_enter_the_bad_key_state_and_say_why():
    """Auth-tag failures used to break the loop without setting the flag.

    That left `_on_peer_disconnected` treating it as an ordinary drop: reconnect,
    next clipboard frame, same tear-down — forever, and one warning per frame
    with nothing in the app layer to show the user which setting to fix.
    """
    payloads = [b"\x01CBE" + bytes(20)] * 5
    assert all(is_encrypted(payload) for payload in payloads)
    connection = C.PeerConnection(
        "dev-b",
        "Device B",
        _SockBuf(_framed(payloads)),
        peer_fingerprint="AA:BB",
        enc_mgr=_NeverDecrypts(),
    )
    disconnected = []
    connection.set_on_disconnect(lambda peer_id, conn: disconnected.append(peer_id))
    _run_recv_loop(connection)

    assert connection._crypto_mismatch is True
    assert connection._crypto_mismatch_reason == C.CRYPTO_MISMATCH_PASSWORD
    assert disconnected == ["dev-b"]


def test_an_on_off_setting_mismatch_keeps_its_own_reason():
    """The other mismatch is still a mismatch, but a different user action."""
    payloads = [b'{"msg_type": "clipboard"}'] * 5
    connection = C.PeerConnection(
        "dev-b",
        "Device B",
        _SockBuf(_framed(payloads)),
        peer_fingerprint="AA:BB",
        enc_mgr=_NeverDecrypts(),
    )
    connection.set_on_disconnect(lambda peer_id, conn: None)
    _run_recv_loop(connection)

    assert connection._crypto_mismatch is True
    assert connection._crypto_mismatch_reason == C.CRYPTO_MISMATCH_SETTING


def test_a_bad_key_state_stops_the_reconnect_loop_and_notifies():
    """No reconnect into the same tear-down, and the reason reaches the app."""
    manager = _manager(_PinnedPairing("", paired=True))
    scheduled = []
    manager._schedule_reconnect = scheduled.append
    seen = []
    manager.set_on_crypto_mismatch(
        lambda name, peer_id, reason: seen.append((name, peer_id, reason))
    )
    connection = C.PeerConnection(
        "dev-b",
        "Device B",
        _SockBuf(b""),
        peer_fingerprint="AA:BB",
        enc_mgr=_NeverDecrypts(),
    )
    connection._crypto_mismatch = True
    connection._crypto_mismatch_reason = C.CRYPTO_MISMATCH_PASSWORD
    manager._peers["dev-b"] = connection

    manager._on_peer_disconnected("dev-b", connection)

    assert scheduled == [], "reconnected into the same bad-key tear-down"
    assert seen == [("Device B", "dev-b", C.CRYPTO_MISMATCH_PASSWORD)]
    manager._running = False
    manager._pending_sockets.clear()


# ------------------------------- health of a connection that has not started


class _OpenSocket:
    """A socket with nothing to read and no error on it, for `health_check`."""

    def settimeout(self, value):
        pass

    def setsockopt(self, *args):
        pass

    def getsockopt(self, level, option):
        return 0

    def recv(self, size):
        return b""

    def shutdown(self, how):
        pass

    def close(self):
        pass


def test_a_connection_that_has_not_started_is_not_reported_dead():
    """An unstarted connection is open, not dead.

    The transport now registers a connection and starts it only after the
    tiebreaker has decided whether to keep it — so that one which loses the
    race never hands its frames to the application.  That leaves a window in
    which a connection is in `_peers` without having been started, and
    `health_check` answered False for it, which is what made a tiebreaker keep
    a connection the peer had already decided to drop: each end stopped the
    half the other was keeping, and no link survived long enough to carry an
    unpair notice.  The socket is open — TLS and the identity exchange are
    both behind it — so "not started yet" has to read as alive.
    """
    connection = C.PeerConnection("dev-b", "Device B", _OpenSocket())

    assert connection._recv_thread is None
    assert connection.health_check() is True


def test_a_connection_that_was_started_and_stopped_is_reported_dead():
    """The same flag after `stop()` still means what it always meant."""
    connection = C.PeerConnection(
        "dev-b",
        "Device B",
        _SockBuf(_framed([])),
        pairing_mgr=_PinnedPairing("", paired=True),
    )
    _run_recv_loop(connection)
    assert connection._recv_thread is not None

    connection.stop()
    assert connection.health_check() is False
