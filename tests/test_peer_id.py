"""The shared peer-id bridge (internal/transport/peer_id.py).

Discovery advertises the HASHED device id; pairing, config and every UI row are
keyed by the REAL one.  Both device-list builders — ``src/main.get_device_states``
(desktop + chat picker) and ``internal.web.api.devices.get_devices`` (web page) —
bridge those two forms with this helper, after the web one's own hand-rolled
hashing listed a renamed peer TWICE (real id + hashed id) while the desktop
listed it once.

These tests pin the bridge itself and the one regression it exists for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.transport.discovery import Discovery  # noqa: E402
from internal.transport.peer_id import (  # noqa: E402
    expand_id_forms,
    hashed_id,
    id_forms,
    is_on_network,
)

# ── hashed_id ───────────────────────────────────────────────────────────


def test_hashed_id_matches_what_discovery_advertises():
    # The whole point: the same value discovery puts in the TXT record, so a
    # sighting can be matched without going through discovery itself.
    assert hashed_id("peer-1") == Discovery._hash_device_id("peer-1")


def test_hashed_id_of_empty_is_empty():
    # "" must never become a hash that could collide with a real sighting.
    assert hashed_id("") == ""
    assert hashed_id(None) == ""


# ── id_forms / expand_id_forms ──────────────────────────────────────────


def test_expand_id_forms_unions_every_peer():
    out = expand_id_forms(["a-id", "b-id"])
    for pid in ("a-id", "b-id"):
        assert pid in out
        assert Discovery._hash_device_id(pid) in out


def test_expand_id_forms_skips_blank_ids():
    # A row with no device_id must not put "" in the set: the discovered sweep
    # tests membership for every sighting, and "" would be a silent wildcard
    # for any peer whose id failed to hash.
    assert expand_id_forms(["", None, "real"]) == id_forms("real")


# ── is_on_network ───────────────────────────────────────────────────────


def test_is_on_network_matches_the_hashed_sighting():
    live = {Discovery._hash_device_id("peer-2")}
    assert is_on_network("peer-2", live) is True


# ── the regression the bridge exists for ────────────────────────────────


def test_both_builders_agree_on_a_renamed_peer():
    """The regression itself: one device, one row, in BOTH builders.

    The web builder is called directly; the host builder is represented by the
    id-form bridge it now shares, so this stays a unit test (get_device_states
    needs a live pairing/transport/chat stack).
    """
    from internal.config.config import Config, PeerInfo
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {"peer-r": PeerInfo(device_id="peer-r", device_name="Stored Name", paired=True)}
    cfg.removed_peers = {}
    hashed = Discovery._hash_device_id("peer-r")

    result, status = devices_api.get_devices(
        cfg,
        lambda: [],
        # Advertising under a name that shares nothing with the stored one, so
        # only the id bridge can dedup it.
        get_discovered=lambda: {hashed: {"name": "Renamed-Box"}},
        get_resolved_hashes=lambda: {},
    )
    assert status == 200
    ids = [d["device_id"] for d in result["devices"]]
    assert ids.count("peer-r") == 1
    assert hashed not in ids
    # And the same sighting is recognised as "this peer is on the network".
    assert is_on_network("peer-r", {hashed}) is True
