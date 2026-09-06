"""The shared peer-id bridge (internal/transport/peer_id.py).

Discovery advertises the HASHED device id; pairing, config and every UI row are
keyed by the REAL one.  Both device-list builders — ``src/main.get_device_states``
(desktop + chat picker) and ``internal.web.api.devices.get_devices`` (web page) —
have to bridge those two forms, and they used to do it independently: the web one
skipped the hashing entirely and leaned on the transport manager's resolved-hash
map plus a device-NAME heuristic, so a peer renamed on either side was listed
TWICE on the device page (real id + hashed id) while the desktop listed it once.

These tests pin the shared helper's behaviour and guard against the two builders
growing their own copy again.
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── hashed_id ───────────────────────────────────────────────────────────


def test_hashed_id_matches_what_discovery_advertises():
    # The whole point: the same value discovery puts in the TXT record, so a
    # sighting can be matched without going through discovery itself.
    assert hashed_id("peer-1") == Discovery._hash_device_id("peer-1")


def test_hashed_id_of_empty_is_empty():
    # "" must never become a hash that could collide with a real sighting.
    assert hashed_id("") == ""
    assert hashed_id(None) == ""


def test_hashed_id_never_raises():
    # A bad id degrades to "no hashed form" — that can cost a dedup, never
    # invent a wrong match, and must not take a device list down with it.
    assert hashed_id(12345) == ""


# ── id_forms / expand_id_forms ──────────────────────────────────────────


def test_id_forms_carries_both_shapes():
    forms = id_forms("peer-1")
    assert forms == {"peer-1", Discovery._hash_device_id("peer-1")}


def test_id_forms_of_empty_is_empty_set():
    assert id_forms("") == set()


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


def test_expand_id_forms_accepts_a_generator():
    # Callers pass a comprehension over their row list, not a materialised set.
    assert expand_id_forms(x for x in ["p"]) == id_forms("p")


# ── is_on_network ───────────────────────────────────────────────────────


def test_is_on_network_matches_the_hashed_sighting():
    live = {Discovery._hash_device_id("peer-2")}
    assert is_on_network("peer-2", live) is True


def test_is_on_network_matches_an_already_hashed_id():
    # A bare mDNS row is itself keyed by the hash; the caller should not have to
    # know which form it is holding.
    hashed = Discovery._hash_device_id("peer-3")
    assert is_on_network(hashed, {hashed}) is True


def test_is_on_network_false_for_absent_or_empty():
    assert is_on_network("peer-4", {Discovery._hash_device_id("other")}) is False
    assert is_on_network("peer-4", set()) is False
    assert is_on_network("", {"anything"}) is False


# ── drift guard ─────────────────────────────────────────────────────────


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _slice(src: str, start: str, end: str) -> str:
    return src.split(start)[1].split(end)[0]


def test_web_device_snapshot_uses_the_shared_bridge():
    src = _read("internal", "web", "api", "devices.py")
    assert "from internal.transport.peer_id import" in src
    # Both dedup sets go through the shared expansion, and presence through the
    # shared check — no hand-rolled hashing left in this builder.
    assert "seen_ids = expand_id_forms(" in src
    assert "removed_ids = expand_id_forms(" in src
    assert "return is_on_network(peer_id, live_ids)" in src
    body = _slice(src, "def get_devices(", "\ndef ")
    assert "Discovery._hash_device_id" not in body, (
        "get_devices grew its own hashing again — use internal.transport.peer_id"
    )


def test_host_device_states_uses_the_shared_bridge():
    src = _read("src", "main.py")
    states = _slice(src, "    def get_device_states(self)", "    def _chat_device_address(self")
    assert "from internal.transport.peer_id import hashed_id" in states
    assert "h = hashed_id(real)" in states
    assert "Discovery._hash_device_id" not in states, "get_device_states grew its own hashing again"


def test_chat_picker_presence_uses_the_shared_bridge():
    src = _read("src", "main.py")
    chat = _slice(src, "    def _get_chat_devices(self)", "    def _chat_device_address(self")
    assert "from internal.transport.peer_id import is_on_network" in chat
    assert 'is_on_network(d["peer_id"], live)' in chat
    assert "Discovery._hash_device_id" not in chat


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
