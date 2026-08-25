"""Shared device-identity helpers — single source of truth.

The hashed mDNS peer-id formula and peer-supplied-string sanitizing used to
be copy-pasted across transport, discovery, dashboard and web layers.  Every
caller now imports them from here so a change to the recipe can't silently
diverge between layers.
"""

import hashlib

__all__ = ["peer_id_hash", "sanitize_peer_str"]


def peer_id_hash(device_id: str) -> str:
    """The hashed mDNS peer id for *device_id*: sha256 hex, first 12 chars.

    This is the formula peers advertise in their mDNS TXT record; every
    layer maps a real device id to/from this hash, so it must never change
    without a coordinated migration across devices.
    """
    return hashlib.sha256(device_id.encode()).hexdigest()[:12]


def sanitize_peer_str(value: str, max_len: int = 64) -> str:
    """Strip control characters and cap length of peer-supplied strings.

    Names from mDNS instance names and certificate CN/OU fields are fully
    attacker-controlled on a LAN and flow into log lines, OS notifications
    and UI lists — keep them to sane printable text.
    """
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:max_len]
