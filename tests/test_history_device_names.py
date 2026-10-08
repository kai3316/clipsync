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
from internal.config.config import Config, PeerInfo, known_device_names

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
