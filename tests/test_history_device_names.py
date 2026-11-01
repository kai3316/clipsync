"""A history row names the device a clip came from, whatever route it arrived by.

Reported as "互联网配对的主机名字现在可以正常使用，但好像有些时候，比如在历史记录里，下面显示的名字
不对。好像升级完以后，它的名字显示也会不太对".  Both halves are one defect.

`_history_source_name` (and the web panel's own copy of the same map) built the name map from
`config.peers` -- the **LAN** peers only -- and `source_name` falls back to `source_device` itself,
which for anything paired by internet code is a 16-character hex peer id.  So a clip that arrived
over the relay was labelled with that id while the device list showed a name.  Measured before the
fix:

    LAN peer                       -> "LAN laptop"     (worked)
    internet-paired, alias set     -> "2222222222222222"
    internet-paired, no alias      -> "3333333333333333"

The upgrade half is that the name a peer publishes at hello lived only in
`internet_pairing._names` -- memory -- so a restart forgot it until that peer reconnected, and
`netpair_names` now keeps it on disk.
"""

from __future__ import annotations

import pytest

from internal.application.use_cases.history import source_name
from internal.config.config import (
    Config,
    PeerInfo,
    chosen_device_name,
    known_device_names,
)

LAN_PEER = "1111111111111111"
NETPAIR_WITH_ALIAS = "2222222222222222"
NETPAIR_NO_ALIAS = "3333333333333333"


@pytest.fixture
def cfg():
    config = Config(encryption_enabled=False)
    config.device_id = "0" * 16
    config.device_name = "This machine"
    config.peers = {
        LAN_PEER: PeerInfo(device_id=LAN_PEER, device_name="LAN laptop", paired=True),
    }
    config.netpair_secrets = {NETPAIR_WITH_ALIAS: "s", NETPAIR_NO_ALIAS: "s2"}
    config.netpair_aliases = {NETPAIR_WITH_ALIAS: "书房的那台"}
    config.netpair_names = {NETPAIR_NO_ALIAS: "Kitchen iMac"}
    return config


def label(cfg, source_device: str) -> str:
    """What a history row shows, through the same two calls the runtime makes."""
    names = {cfg.device_id: cfg.device_name}
    names.update(known_device_names(cfg))
    return source_name(source_device, names, cfg.device_name)


def test_a_clip_from_an_internet_pairing_is_named_not_hashed(cfg):
    """The reported defect: the row showed the peer id."""
    assert label(cfg, NETPAIR_NO_ALIAS) == "Kitchen iMac"

    # An alias outranks the name the peer published about itself, which is the rule the device list
    # applies (`lan.py` builds a relay row as `alias or name or pid`).
    assert label(cfg, NETPAIR_WITH_ALIAS) == "书房的那台"


def test_a_lan_peer_and_this_machine_are_unaffected(cfg):
    """The control: the two cases that already worked must keep working."""
    assert label(cfg, LAN_PEER) == "LAN laptop"
    assert label(cfg, cfg.device_id) == "This machine"
    # An empty source is a locally captured clip, which has always fallen back to this device's
    # name.
    assert label(cfg, "") == "This machine"


def test_an_unknown_id_still_falls_back_to_the_id(cfg):
    """A device this machine has never paired with has no name to show, and the id is honest."""
    assert label(cfg, "deadbeefdeadbeef") == "deadbeefdeadbeef"


def test_the_local_device_is_in_the_map_itself(cfg):
    """It was missing, and the probe written to check this fix is what caught it.

    `source_name` falls back to this device's name only for an *empty* source, so a row that
    carried this machine's own id -- which the relay path does stamp -- printed the id.  Two
    callers had been adding the local entry themselves, which is what goes wrong when a map is
    assembled in more than one place.
    """
    names = known_device_names(cfg)
    assert names[cfg.device_id] == "This machine"


def test_the_persisted_peer_name_beats_the_alias_only_when_there_is_no_alias(cfg):
    """The precedence, asserted directly rather than inferred from the two labels above."""
    # Give the peer with an alias a learned name too, and check which wins.
    cfg.netpair_names[NETPAIR_WITH_ALIAS] = "Something I Did Not Choose"
    assert label(cfg, NETPAIR_WITH_ALIAS) == "书房的那台"
    # And the learned name is what a peer with no alias gets.
    assert label(cfg, NETPAIR_NO_ALIAS) == "Kitchen iMac"


def test_the_name_survives_a_round_trip_through_the_config(cfg, tmp_path, monkeypatch):
    """The upgrade half: a name that is only in memory is lost at every restart.

    Written and re-read rather than checked in memory, because "it survives" is the whole claim.
    """
    from internal.config.config import load, save

    monkeypatch.setenv("CLIPSYNC_CONFIG_DIR", str(tmp_path))
    cfg.netpair_names = {NETPAIR_NO_ALIAS: "Kitchen iMac"}
    save(cfg)
    reloaded = load()
    assert reloaded.netpair_names.get(NETPAIR_NO_ALIAS) == "Kitchen iMac"
    assert label(reloaded, NETPAIR_NO_ALIAS) == "Kitchen iMac"


# ── One name: what the user chose, above what the peer published ──────────────
#
# Reported as "设备名有的时候会变成'试试'……历史记录里下面显示的名字不对".  Measured on the
# reporting install, one device held all three of these at once:
#
#     peers["483fa196a05a"].device_name = "Kais-MacBook"   # the peer's own name
#     peers["483fa196a05a"].notes       = "试试"            # the user's name
#     netpair_names["483fa196a05a"]     = "Kais-MacBook"
#
# The device list read `note or name` and showed 试试; this map applied the
# self-reported names last and the history row showed Kais-MacBook.  The two
# cases below are that pair of values, so the test fails if the precedence ever
# goes back to preferring what a peer says about itself over what the user said.


@pytest.fixture
def reported():
    """The reported device: a LAN peer the user renamed, which also said hello."""
    config = Config(encryption_enabled=False)
    config.device_id = "4c5d7e51308a"
    config.device_name = "NC-5060"
    config.peers = {
        "483fa196a05a": PeerInfo(
            device_id="483fa196a05a",
            device_name="Kais-MacBook",
            notes="试试",
            paired=True,
        ),
    }
    config.netpair_names = {"483fa196a05a": "Kais-MacBook"}
    return config


def test_the_users_own_name_outranks_the_name_the_peer_published(reported):
    """The reported defect, with the values it was reported with.

    The self-reported name is a source this map has to keep — it is all there is
    for a device the user never renamed, and case 2 below pins that.  What it may
    not do is outrank the rename.
    """
    macbook = "483fa196a05a"
    assert chosen_device_name(reported, macbook) == "试试"
    assert known_device_names(reported)[macbook] == "试试"
    assert label(reported, macbook) == "试试"


def test_the_peers_own_name_still_names_a_device_the_user_never_renamed(reported):
    """The control: dropping the self-reported source entirely would fail here."""
    reported.peers["483fa196a05a"].notes = ""
    assert chosen_device_name(reported, "483fa196a05a") == ""
    assert known_device_names(reported)["483fa196a05a"] == "Kais-MacBook"
    assert label(reported, "483fa196a05a") == "Kais-MacBook"


def test_an_alias_outranks_a_note_and_both_are_the_users_own(reported):
    """`chosen_device_name` reads both fields the rename dialog can write.

    One fact, two fields — the alias an internet pairing is renamed through and
    the note a saved LAN peer is renamed through — so a reader that consults only
    one of them disagrees with the row the user just renamed.
    """
    macbook = "483fa196a05a"
    reported.netpair_aliases = {macbook: "书房的那台"}
    assert chosen_device_name(reported, macbook) == "书房的那台"
    assert known_device_names(reported)[macbook] == "书房的那台"
    # A note of nothing but spaces is not a name, so the alias is not needed to
    # make this one fall through.
    reported.netpair_aliases = {}
    reported.peers[macbook].notes = "   "
    assert chosen_device_name(reported, macbook) == ""
    assert known_device_names(reported)[macbook] == "Kais-MacBook"


def test_a_name_is_only_read_for_a_device_that_has_one(reported):
    """No id, no config, a device nobody named: empty, never a guess."""
    assert chosen_device_name(reported, "") == ""
    assert chosen_device_name(None, "483fa196a05a") == ""
    assert chosen_device_name(reported, "deadbeefdeadbeef") == ""


def test_the_device_list_a_window_reads_without_an_engine_carries_the_name_too(reported):
    """One more consumer of the rule, found by reading every name surface.

    The device list has two producers: the runtime's rows, and this fallback for
    a window whose engine is not up.  The fallback carried `name` alone, so on a
    locked or degraded application the list named a device by what the peer calls
    itself — the same list the rename is checked on, reading differently
    depending on whether the engine happened to be running.
    """
    from internal.application.bootstrap import SidecarApplication

    app = SidecarApplication.__new__(SidecarApplication)
    app.config = reported
    app.runtime = None

    items = app.devices()["items"]
    macbook = next(item for item in items if item["id"] == "483fa196a05a")
    # The window's own rule is `note or name`, which is what the row is called.
    assert (macbook["note"] or macbook["name"]) == "试试"
