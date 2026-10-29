"""X25519 key agreement for internet pairing (netpair).

Why this exists
---------------
A netpair channel used to be keyed by ``netpair_key(code_secret, password)``
alone.  The code secret is 35 bits — it has to survive being read off one
screen and typed into another — so every frame on that channel was protected
by something an attacker can enumerate: record the ciphertext, take it home,
try all 34 billion secrets at GPU speed.  The optional pairing passphrase
closes that window only for users who set one.

The fix is not a longer secret but a different kind of secret: one that is
never transmitted *at all*.  Each device keeps an X25519 private key that
never leaves it, publishes only the matching public key (inside the pairing
hello), and both ends derive the channel key from

    DH(my_private, peer_public)  salted with the code secret

The two ends get the same value because ``DH(a, B) == DH(b, A)`` — the same
number reached by two different routes — and a recording of the exchange
carries only the two public keys.  Recovering the shared value from those is
the elliptic-curve discrete-log problem, so a cracked code reveals nothing
about a recorded session: the code is still needed (it is the salt, and it is
what says *which* peer this is), but it is no longer sufficient.

What is left is the live case: an attacker who knows the code and is present
during the pairing handshake itself can still sit in the middle.  Everything
after it is out of reach, and the handshake frames carry device names and
public keys, not user content.

The keys are static rather than per-session (persisted in the config, like
``relay_secret``) because a netpair channel has to be derivable again after a
restart by both ends without a new round trip.  That gives up forward secrecy
against a later compromise of the device's stored private key — a far higher
bar than cracking a 35-bit code, and the trade this app's threat model wants:
its adversary is a public MQTT broker and whoever is reading it.
"""

from __future__ import annotations

import base64
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

logger = logging.getLogger(__name__)

#: Length of a raw X25519 public key, in bytes.
PUBLIC_KEY_BYTES = 32


def generate_keypair() -> tuple[str, str]:
    """A fresh X25519 pair as ``(private_b64, public_b64)``.

    Base64 text rather than raw bytes because this is persisted in the JSON
    config beside the other secrets this app stores.
    """
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    return (
        base64.b64encode(
            private.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        ).decode("ascii"),
        base64.b64encode(
            public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii"),
    )


def public_from_private(private_b64: str) -> str | None:
    """The public half of a stored private key, or None when it is unusable.

    Needed because only the private half is persisted: the public one is
    recomputed on demand so the two can never disagree.
    """
    try:
        raw = base64.b64decode(private_b64, validate=True)
        if len(raw) != PUBLIC_KEY_BYTES:
            return None
        private = x25519.X25519PrivateKey.from_private_bytes(raw)
    except Exception:
        logger.debug("netpair: unusable stored X25519 private key", exc_info=True)
        return None
    return base64.b64encode(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")


def shared_secret(private_b64: str, peer_public_b64: str) -> bytes | None:
    """``DH(my_private, peer_public)``, or None when either half is malformed.

    None rather than an exception: the public half arrives over the relay from
    a peer, so a garbage value is a peer to refuse, not a crash to raise.
    """
    try:
        raw_private = base64.b64decode(private_b64, validate=True)
        raw_public = base64.b64decode(peer_public_b64, validate=True)
        if len(raw_private) != PUBLIC_KEY_BYTES or len(raw_public) != PUBLIC_KEY_BYTES:
            return None
        private = x25519.X25519PrivateKey.from_private_bytes(raw_private)
        peer_public = x25519.X25519PublicKey.from_public_bytes(raw_public)
        return private.exchange(peer_public)
    except Exception:
        logger.debug("netpair: key agreement failed", exc_info=True)
        return None


def is_public_key(value) -> bool:
    """Whether *value* is shaped like a public key this module can use."""
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return False
    return len(raw) == PUBLIC_KEY_BYTES
