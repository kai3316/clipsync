"""Dial-path fixes: reconnect continuation, in-flight de-duplication, failure reasons.

Every case here is a dial that used to end in silence or in a stopped retry
chain: the reconnect ladder did not re-arm after a refusal that happened inside
``connect_to_peer`` (not in its exception handler), a second caller started a
whole second handshake, and a dial failure produced a log line and no event.
"""

import os
import ssl
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport import connection as C  # noqa: N812

# --------------------------------------------------------------- test helpers


class _Pairing:
    """Paired/not-paired stub with the three reads the dial path makes."""

    def __init__(self, paired=True, fingerprint="", certificate=""):
        self._paired = paired
        self._fingerprint = fingerprint
        self._certificate = certificate

    def is_peer_paired(self, device_id):
        return self._paired

    def get_peer_fingerprint(self, device_id):
        return self._fingerprint

    def get_peer_certificate(self, device_id):
        return self._certificate

    def add_peer(self, *args, **kwargs):
        return None

    def generate_shared_pairing_code(self, device_id):
        return "00000000"


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def settimeout(self, value):
        pass

    def gettimeout(self):
        return 30.0


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _manager(pairing=None):
    manager = C.TransportManager("dev-a", "Device A", 9999, pairing or _Pairing())
    manager._running = True
    return manager


def _shutdown(manager):
    for timer in list(manager._reconnect_timers.values()):
        timer.cancel()
    manager._running = False
    manager.stop_server(timeout=3)


def _wire(manager, dial, exchange):
    """Give the manager a scripted dial, TLS context/wrap and identity exchange."""
    manager._dial = dial
    manager._build_ssl_context = lambda **kwargs: object()
    manager._wrap_socket = lambda context, sock, **kwargs: _FakeSocket()
    manager._check_rejection = lambda sock, **kwargs: (0, b"")
    manager._identity_exchange = lambda sock, **kwargs: exchange


def _certificate(cn):
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
    return cert, serialization


# --------------------------------------------------------- severe-2: reconnect


def test_a_missing_identity_frame_keeps_the_reconnect_chain_alive():
    """One silent dial must not be the end of the retry chain.

    ``_try_reconnect`` pops the timer before it dials.  A dial that reaches the
    ``exchange is None`` refusal used to ``return`` without re-arming it — the
    only failure path outside the exception handler — so a paired device whose
    identity frame once arrived late stayed at "reconnecting 1/10" with nothing
    dialling until mDNS happened to see it again.
    """
    manager = _manager()
    dials = []

    def dial(address, port, timeout):
        dials.append((address, port))
        return _FakeSocket()

    _wire(manager, dial, None)
    failures = []
    manager.set_on_connect_failed(lambda name, pid, reason: failures.append((pid, reason)))
    try:
        manager.connect_to_peer("dev-b", "Device B", "127.0.0.1", 7000)
        assert _wait_until(lambda: "dev-b" not in manager._dialing and dials), (
            "the first dial never finished"
        )
        assert failures == [("dev-b", "identity")], failures
        assert "dev-b" in manager._reconnect_timers, (
            "the refusal ended the reconnect chain instead of re-arming it"
        )

        timer = manager._reconnect_timers.pop("dev-b")
        timer.cancel()
        manager._try_reconnect("dev-b")
        assert _wait_until(lambda: len(dials) == 2), (
            f"the second dial never happened (dials={len(dials)})"
        )
    finally:
        _shutdown(manager)


def test_a_tls_identity_mismatch_keeps_the_reconnect_chain_alive():
    """The other non-exception refusal path gets the same rule."""
    manager = _manager()
    peer_cert, serialization = _certificate("dev-b")
    tls_cert, _ = _certificate("dev-b")
    exchange = {
        "peer_cert_pem": peer_cert.public_bytes(serialization.Encoding.PEM).decode(),
        "peer_nonce": "abcd",
        "peer_proof": "",
        "peer_name": "Device B",
        "peer_port": 7000,
        "peer_no_auto_pairing": False,
        "peer_version": 1,
        "proved": True,
        "our_nonce": "efgh",
    }

    class _TwoPrincipalSocket(_FakeSocket):
        def getpeercert(self, binary_form=False):
            return tls_cert.public_bytes(serialization.Encoding.DER)

    dials = []

    def dial(address, port, timeout):
        dials.append((address, port))
        return _FakeSocket()

    _wire(manager, dial, exchange)
    manager._wrap_socket = lambda context, sock, **kwargs: _TwoPrincipalSocket()
    seen = []
    manager.set_on_connect_failed(lambda name, pid, reason: seen.append(reason))
    try:
        manager.connect_to_peer("dev-b", "Device B", "127.0.0.1", 7000)
        assert _wait_until(lambda: "dev-b" not in manager._dialing and dials)
        assert seen == ["identity"], seen
        assert "dev-b" in manager._reconnect_timers, (
            "a paired peer's cert/identity mismatch ended the retry chain"
        )
        timer = manager._reconnect_timers.pop("dev-b")
        timer.cancel()
        manager._try_reconnect("dev-b")
        assert _wait_until(lambda: len(dials) == 2), (
            f"the second dial never happened (dials={len(dials)})"
        )
    finally:
        _shutdown(manager)


# ------------------------------------------------------ light-6: one dial only


def test_a_second_dial_while_one_is_in_flight_is_ignored():
    """Three sources dial the same peer; only one may do the handshake.

    The dial window is up to ~30 s, and the connect button, the presence loop
    and the reconnect timer all call ``connect_to_peer`` inside it.  Without an
    in-flight set each started its own TLS + identity exchange and the race
    tiebreaker then discarded one connection.
    """
    manager = _manager()
    started = threading.Event()
    release = threading.Event()
    dials = []

    def dial(address, port, timeout):
        dials.append((address, port))
        started.set()
        release.wait(5)
        raise ConnectionRefusedError("no route to host")

    _wire(manager, dial, None)
    try:
        manager.connect_to_peer("dev-b", "Device B", "127.0.0.1", 7000)
        assert started.wait(5), "the first dial never started"

        # The second call must return without a second dial.
        manager.connect_to_peer("dev-b", "Device B", "127.0.0.1", 7000)
        assert len(dials) == 1, f"{len(dials)} dials for one peer at once"
    finally:
        release.set()
        _wait_until(lambda: "dev-b" not in manager._dialing)
        _shutdown(manager)


# ------------------------------------------- medium-27: a failed dial says why


@pytest.mark.parametrize(
    "error,expected",
    [
        (ConnectionRefusedError("refused"), "unreachable"),
        (TimeoutError("timed out"), "timeout"),
        (ConnectionResetError("reset by peer"), "closed"),
        (ssl.SSLError("bad record mac"), "crypto"),
        (ssl.SSLError("handshake failure"), "tls"),
        (ssl.SSLCertVerificationError("certificate verify failed"), "identity"),
    ],
)
def test_a_failed_dial_reports_a_reason_code(error, expected):
    """A dial that never connected was silent: no event and no reason."""
    manager = _manager(pairing=_Pairing(paired=False))

    def dial(*_args, **_kwargs):
        raise error

    _wire(manager, dial, None)
    seen = []
    manager.set_on_connect_failed(lambda name, pid, reason: seen.append(reason))
    try:
        manager.connect_to_peer("dev-b", "Device B", "127.0.0.1", 7000)
        assert _wait_until(lambda: seen), "the failure callback never fired"
        assert seen == [expected], seen
    finally:
        _shutdown(manager)


def test_the_reason_codes_are_the_documented_ones():
    """The app layer publishes this list; a new code is additive, not a rename."""
    assert C.CONNECT_FAILURE_REASONS == (
        "unreachable",
        "timeout",
        "tls",
        "closed",
        "identity",
        "crypto",
        "port",
        "other",
    )
    for code in C.CONNECT_FAILURE_REASONS:
        assert code.islower() and " " not in code

    assert C._connect_failure_reason(C.PortInUseError(9500)) == "port"
    assert C._connect_failure_reason(RuntimeError("something else")) == "other"
