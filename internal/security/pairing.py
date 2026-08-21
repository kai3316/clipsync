"""Device pairing and certificate management.

Security model:
1. Each device generates an Ed25519 keypair + self-signed X.509 certificate.
2. Devices discover each other via mDNS on the LAN.
3. On first contact, each device generates an 8-digit pairing code.
   The user must verify the codes match on both devices.
4. Upon code confirmation, certificates are exchanged and pinned (TOFU).
5. Future connections require the pinned certificate — any change is rejected.
6. Certificate fingerprints (SHA-256) are available for out-of-band verification.

Threats addressed:
- Eavesdropping: TLS 1.3 encrypts all traffic.
- Impersonation: Certificate pinning prevents MITM after first pairing.
- Brute-force pairing: 8-digit code (10^8 space) + rate limiting (5 attempts).
- Rogue devices: Only paired peers can connect.
"""

import datetime
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Pairing code: 8 digits = 100 million combinations
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_BYTES = 5

# Rate limiting for pairing confirmation attempts
MAX_PAIRING_ATTEMPTS = 5
PAIRING_ATTEMPT_WINDOW = 300  # 5 minutes

logger = logging.getLogger(__name__)


def fingerprint_pem(certificate_pem: str) -> str:
    """SHA-256 fingerprint of a PEM certificate, colon-separated hex."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def fingerprint_short(certificate_pem: str) -> str:
    """Short version: first and last 8 hex chars."""
    fp = fingerprint_pem(certificate_pem).replace(":", "")
    return f"{fp[:8]}...{fp[-8:]}"


@dataclass
class DeviceIdentity:
    """A device's cryptographic identity."""
    device_id: str
    device_name: str
    private_key: ed25519.Ed25519PrivateKey
    certificate: x509.Certificate
    certificate_pem: str
    private_key_pem: str
    fingerprint: str = ""
    fingerprint_short: str = ""


@dataclass
class PeerIdentity:
    """A known peer's pinned identity."""
    device_id: str
    device_name: str
    certificate_pem: str
    paired: bool = True
    fingerprint: str = ""


class PairingManager:
    """Manages device identity, pairing codes, and peer certificates."""

    def __init__(self, device_id: str, device_name: str):
        self._device_id = device_id
        self._device_name = device_name
        self._identity: DeviceIdentity | None = None
        self._peers: dict[str, PeerIdentity] = {}
        self._pending_pairings: dict[str, tuple[str, float]] = {}  # peer_id -> (code, timestamp)
        # peer_id -> list of attempt timestamps. NOTE: keyed only by peer_id,
        # so an attacker who rotates device identities can reset the count per
        # new identity. Low impact (each identity still pays the 5/5min cost)
        # and intentionally not restructured — see MAX_PAIRING_ATTEMPTS.
        self._pairing_attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._on_new_pairing: Callable | None = None  # called when a new pairing code is generated

    def set_on_new_pairing(self, callback: Callable):
        """Set callback invoked when a new pairing code is generated.

        Called as ``callback(peer_id, code, peer_name)`` so the UI can
        notify the user and show the pairing dialog on the target device.
        """
        self._on_new_pairing = callback

    def load_or_create_identity(
        self, private_key_pem: str, certificate_pem: str,
    ) -> DeviceIdentity:
        """Load existing identity from saved keys, or create new one."""
        if private_key_pem and certificate_pem:
            logger.info("Loading existing device identity")
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None,
            )
            certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
            fp = fingerprint_pem(certificate_pem)
            self._identity = DeviceIdentity(
                device_id=self._device_id,
                device_name=self._device_name,
                private_key=private_key,
                certificate=certificate,
                certificate_pem=certificate_pem,
                private_key_pem=private_key_pem,
                fingerprint=fp,
                fingerprint_short=fingerprint_short(certificate_pem),
            )
        else:
            self._identity = self._create_identity()
        return self._identity

    def get_identity(self) -> DeviceIdentity:
        if self._identity is None:
            raise RuntimeError("Identity not loaded")
        return self._identity

    def add_peer(self, device_id: str, device_name: str, certificate_pem: str, paired: bool = True):
        fp = fingerprint_pem(certificate_pem)
        with self._lock:
            existing = self._peers.get(device_id)
            if existing and existing.paired and existing.fingerprint != fp:
                # Certificate changed for a paired peer — reject!
                logger.error(
                    "Certificate changed for %s (%s)! Expected: %s... Got: %s...",
                    device_name, device_id, existing.fingerprint[:16], fp[:16],
                )
                raise CertificateChangedError(
                    f"Certificate for {device_name} ({device_id}) has changed! "
                    f"Expected: {existing.fingerprint[:16]}... "
                    f"Got: {fp[:16]}..."
                )
            self._peers[device_id] = PeerIdentity(
                device_id=device_id,
                device_name=device_name,
                certificate_pem=certificate_pem,
                paired=paired,
                fingerprint=fp,
            )

    def is_peer_paired(self, device_id: str) -> bool:
        with self._lock:
            peer = self._peers.get(device_id)
            return peer is not None and peer.paired

    def get_peer_certificate(self, device_id: str) -> str | None:
        with self._lock:
            peer = self._peers.get(device_id)
            return peer.certificate_pem if peer else None

    def get_peer_fingerprint(self, device_id: str) -> str:
        """Return the SHA-256 fingerprint of a peer's certificate, or '' if unknown."""
        with self._lock:
            peer = self._peers.get(device_id)
            return peer.fingerprint if peer else ""

    def verify_peer_fingerprint(self, device_id: str, fingerprint: str) -> bool:
        """Verify a peer's certificate fingerprint (for out-of-band verification)."""
        with self._lock:
            peer = self._peers.get(device_id)
            if not peer:
                return False
            return peer.fingerprint.replace(":", "").upper() == fingerprint.replace(":", "").upper()

    def generate_pairing_code(self, peer_id: str) -> str:
        """Generate a pairing code for a peer. Returns the code to display."""
        raw = secrets.token_bytes(PAIRING_CODE_BYTES)
        code_int = int.from_bytes(raw, "big") % (10 ** PAIRING_CODE_LENGTH)
        code = str(code_int).zfill(PAIRING_CODE_LENGTH)
        with self._lock:
            self._pending_pairings[peer_id] = (code, time.time())
            self._pairing_attempts[peer_id] = []
        logger.info("Generated pairing code for %s", peer_id)
        return code

    def generate_shared_pairing_code(self, peer_id: str) -> str:
        """Generate a pairing code derived from both devices' certificate fingerprints.

        Both sides independently compute the same code after TLS cert exchange,
        so users can visually verify the codes match — like Bluetooth Numeric Comparison.
        """
        identity = self._identity
        if not identity:
            raise RuntimeError("Identity not loaded")
        with self._lock:
            peer = self._peers.get(peer_id)
        if not peer:
            raise ValueError(f"Peer {peer_id} not known — cert exchange required first")

        # Sort fingerprints so both sides derive the same code
        fps = sorted([identity.fingerprint, peer.fingerprint])
        shared = hashlib.sha256((fps[0] + fps[1]).encode()).hexdigest()
        code_int = int(shared[:10], 16) % (10 ** PAIRING_CODE_LENGTH)
        code = str(code_int).zfill(PAIRING_CODE_LENGTH)

        with self._lock:
            self._pending_pairings[peer_id] = (code, time.time())
            # Do NOT reset _pairing_attempts here: a connecting peer could
            # otherwise nullify the rate limit by reconnecting and re-generating
            # the shared code. Attempts are cleared only on a successful
            # confirm_pairing() below, or on a deliberate local pairing attempt.
        logger.info("Shared pairing code for %s: %s**** (derived from cert fingerprints)", peer_id, code[:4])
        if self._on_new_pairing:
            peer_name = peer.device_name
            try:
                self._on_new_pairing(peer_id, code, peer_name)
            except Exception:
                logger.debug("on_new_pairing callback failed", exc_info=True)
        return code

    def confirm_pairing(self, peer_id: str, code: str) -> bool:
        """User confirms the pairing code. Rate-limited to prevent brute force."""
        with self._lock:
            pending = self._pending_pairings.get(peer_id)
            if not pending:
                return False

            expected, _timestamp = pending
            now = time.time()

            # Sliding window rate limit: drop attempts older than the window
            attempts = self._pairing_attempts.get(peer_id, [])
            attempts = [t for t in attempts if now - t < PAIRING_ATTEMPT_WINDOW]
            if len(attempts) >= MAX_PAIRING_ATTEMPTS:
                logger.warning("Rate limit hit for pairing with %s", peer_id)
                self._pairing_attempts[peer_id] = attempts
                return False

            attempts.append(now)
            self._pairing_attempts[peer_id] = attempts

            if expected == code:
                del self._pending_pairings[peer_id]
                self._pairing_attempts.pop(peer_id, None)
                if peer_id in self._peers:
                    self._peers[peer_id].paired = True
                logger.info("Pairing confirmed for %s", peer_id)
                return True
            logger.debug("Pairing code mismatch for %s (attempt %d)", peer_id, len(attempts))
            return False

    def reject_pairing(self, peer_id: str):
        with self._lock:
            self._pending_pairings.pop(peer_id, None)

    def unpair_peer(self, peer_id: str):
        """Mark a paired peer as unpaired without removing it."""
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer:
                peer.paired = False
                logger.info("Peer unpaired: %s (%s)", peer.device_name, peer_id)

    def remove_peer(self, peer_id: str):
        """Remove a peer entirely from both known and pending lists."""
        with self._lock:
            self._peers.pop(peer_id, None)
            self._pending_pairings.pop(peer_id, None)
            self._pairing_attempts.pop(peer_id, None)

    def get_pending_pairings(self) -> list[tuple[str, str, str]]:
        """Returns list of (peer_id, code, peer_name) for pending pairings."""
        with self._lock:
            result = []
            for pid, (code, _) in self._pending_pairings.items():
                peer = self._peers.get(pid)
                name = peer.device_name if peer else pid[:12]
                result.append((pid, code, name))
            return result

    def get_paired_peers(self) -> list[PeerIdentity]:
        with self._lock:
            return [p for p in self._peers.values() if p.paired]

    def get_known_peers(self) -> list[PeerIdentity]:
        with self._lock:
            return list(self._peers.values())

    def _create_identity(self) -> DeviceIdentity:
        logger.info("Generating new Ed25519 device identity")
        private_key = ed25519.Ed25519PrivateKey.generate()

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, self._device_id),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ClipSync"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, self._device_name),
        ])

        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(secrets.randbits(64))
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(self._device_id)]),
                critical=False,
            )
            .sign(private_key, None)  # Ed25519 uses its own hashing
        )

        cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode()
        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        fp = fingerprint_pem(cert_pem)

        self._identity = DeviceIdentity(
            device_id=self._device_id,
            device_name=self._device_name,
            private_key=private_key,
            certificate=certificate,
            certificate_pem=cert_pem,
            private_key_pem=key_pem,
            fingerprint=fp,
            fingerprint_short=fingerprint_short(cert_pem),
        )
        return self._identity


class CertificateChangedError(Exception):
    """Raised when a paired peer's certificate changes (potential MITM)."""
    pass
