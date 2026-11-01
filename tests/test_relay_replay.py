"""Relay envelope replay window and timestamp authentication.

``RELAY_TS_WINDOW`` is 1800 seconds.  The duplicate cache used to keep 512
ciphertext hashes with no TTL, so a busy channel could evict an envelope's own
hash while the envelope was still inside its timestamp window; the broker (or
anyone replaying a captured blob) could then have it routed as new.  The
envelope's ``ts`` was plaintext outside the GCM tag as well, so an in-flight
editor could move a recorded frame into a fresh window.

These tests pin the two fixes: a hash lives for the window its own timestamp
allows (and the cap is only a memory backstop much larger than the old 512),
and a v1-compatible keyed MAC covers ``ts`` while envelopes from older builds
(no ``mac`` field) still open.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from internal.transport import relay as relay_module
from internal.transport.relay import (
    ENVELOPE_VERSION,
    RELAY_TS_WINDOW,
    RelayTransport,
    _envelope_replay_key,
    open_envelope_ex,
    pack_envelope,
)

TOPIC = "clipsync/v1/test-replay"
KEY = b"k" * 32


def a_transport(routed: list[bytes]) -> RelayTransport:
    """A transport with one subscribed channel, no broker and no client."""
    return RelayTransport(
        [],
        lambda: {TOPIC: KEY},
        lambda frame, topic, key_index: routed.append(frame),
        lambda state: None,
        client_factory=lambda *args, **kwargs: None,
    )


def deliver(transport: RelayTransport, blob: bytes) -> None:
    transport._on_message(None, None, SimpleNamespace(topic=TOPIC, payload=blob))


def test_a_duplicate_blob_is_routed_once():
    routed: list[bytes] = []
    transport = a_transport(routed)
    blob = pack_envelope(b"frame", KEY, time.time())

    deliver(transport, blob)
    deliver(transport, blob)

    assert routed == [b"frame"]


def test_a_replay_after_more_than_the_old_512_frames_is_refused():
    """The old cache evicted the first hash before this many frames arrived."""
    assert relay_module._SEEN_CAP > 512
    routed: list[bytes] = []
    transport = a_transport(routed)
    now = time.time()
    first = pack_envelope(b"first", KEY, now)
    deliver(transport, first)
    for index in range(512):
        deliver(transport, pack_envelope(b"frame-%d" % index, KEY, now))
    assert len(routed) == 513

    # The broker redelivers the first blob while it is still inside its window.
    deliver(transport, first)

    assert len(routed) == 513


def test_a_fast_clock_replay_is_refused_until_its_own_window_closes(monkeypatch):
    """A hash has to outlive the extra window a fast sender clock buys."""
    clock = [1_000_000.0]
    monkeypatch.setattr(relay_module.time, "time", lambda: clock[0])
    routed: list[bytes] = []
    transport = a_transport(routed)
    # Received now, but stamped 1700s into the future: the timestamp check
    # accepts it (skew tolerance) and keeps accepting it for another 1700s.
    blob = pack_envelope(b"frame", KEY, clock[0] + RELAY_TS_WINDOW - 100)
    deliver(transport, blob)
    assert len(routed) == 1

    clock[0] += RELAY_TS_WINDOW + 100  # past receive + TTL, inside ts + window

    deliver(transport, blob)

    assert len(routed) == 1, "the hash must live until the envelope's own window closes"


def test_a_replay_past_the_timestamp_window_is_refused(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(relay_module.time, "time", lambda: clock[0])
    routed: list[bytes] = []
    transport = a_transport(routed)
    blob = pack_envelope(b"frame", KEY, clock[0])
    deliver(transport, blob)

    clock[0] += RELAY_TS_WINDOW + 1
    deliver(transport, blob)

    assert len(routed) == 1


def test_pack_envelope_binds_the_timestamp_with_a_mac():
    now = time.time()
    blob = pack_envelope(b"frame", KEY, now)
    env = json.loads(blob)

    assert env["v"] == ENVELOPE_VERSION, "the wire version must not change"
    assert isinstance(env.get("mac"), str) and len(env["mac"]) == 64
    assert open_envelope_ex(blob, KEY, now) == (b"frame", "")


@pytest.mark.parametrize("delta", [0.001, 5.0, -5.0, RELAY_TS_WINDOW - 1])
def test_editing_the_timestamp_fails_the_mac(delta):
    """The edit stays inside the window, so only the MAC can catch it."""
    now = time.time()
    env = json.loads(pack_envelope(b"frame", KEY, now))
    env["ts"] = env["ts"] + delta

    assert open_envelope_ex(json.dumps(env).encode("ascii"), KEY, now) == (None, "auth")


def test_an_envelope_without_a_mac_is_refused():
    """A MAC is what binds ``ts``; stripping it must not open the envelope.

    It used to: the absence was read as "a v1 build predates the field", and a
    recorded envelope with its ``mac`` deleted and its ``ts`` slid forward was
    accepted as new.  The timestamp is the one field a third party can rewrite
    in place, so an envelope that does not authenticate it is refused.
    """
    now = time.time()
    env = json.loads(pack_envelope(b"frame", KEY, now))
    env.pop("mac")

    blob = json.dumps(env, separators=(",", ":")).encode("ascii")

    assert open_envelope_ex(blob, KEY, now) == (None, "auth")


def test_an_envelope_without_a_mac_never_routes():
    routed: list[bytes] = []
    transport = a_transport(routed)
    now = time.time()
    env = json.loads(pack_envelope(b"frame", KEY, now))
    env.pop("mac")

    deliver(transport, json.dumps(env).encode("ascii"))

    assert routed == []


def test_rewriting_the_timestamp_does_not_buy_a_fresh_identity():
    """The replay cache keys on the ciphertext, not on the whole envelope.

    Hashing the blob let the same edit that defeats the timestamp window
    (rewrite ``ts``) also defeat the cache, because the new ``ts`` made a new
    hash.  The ciphertext is the part an attacker without the channel key
    cannot change, so it is the identity a replay is recognised by.
    """
    now = time.time()
    blob = pack_envelope(b"frame", KEY, now)
    env = json.loads(blob)
    env["ts"] = env["ts"] + 5.0
    edited = json.dumps(env, separators=(",", ":")).encode("ascii")

    assert _envelope_replay_key(blob) == _envelope_replay_key(edited)


def test_a_rerecorded_envelope_is_still_a_replay_even_inside_the_window():
    routed: list[bytes] = []
    transport = a_transport(routed)
    now = time.time()
    blob = pack_envelope(b"frame", KEY, now)
    deliver(transport, blob)

    # Same ciphertext, new timestamp, new MAC computed by nobody: the cache
    # must recognise the bytes it already routed.
    env = json.loads(blob)
    env["ts"] = env["ts"] + 5.0
    deliver(transport, json.dumps(env, separators=(",", ":")).encode("ascii"))

    assert len(routed) == 1


@pytest.mark.parametrize("mac", [None, 123, "", "not-a-mac", "\u00e9" * 64])
def test_a_malformed_mac_never_opens_the_envelope(mac):
    now = time.time()
    env = json.loads(pack_envelope(b"frame", KEY, now))
    env["mac"] = mac

    frame, why = open_envelope_ex(json.dumps(env).encode("utf-8"), KEY, now)

    assert frame is None and why in ("auth", "format")


def test_a_wrong_key_never_opens_a_macd_envelope():
    now = time.time()
    blob = pack_envelope(b"frame", KEY, now)

    frame, why = open_envelope_ex(blob, b"y" * 32, now)

    assert frame is None and why == "auth"


def test_the_transport_drops_an_envelope_whose_timestamp_was_edited():
    routed: list[bytes] = []
    transport = a_transport(routed)
    now = time.time()
    env = json.loads(pack_envelope(b"frame", KEY, now))
    env["ts"] = env["ts"] + 5

    deliver(transport, json.dumps(env).encode("ascii"))

    assert routed == []
