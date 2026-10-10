"""The overview's 已连接设备 chips name a device the way the device list does.

Reported as "设备名有的时候会变成'试试'……历史记录里下面显示的名字不对".  This is the same
family, one more surface: the chips were built from the transport's own
``device_name`` — what the peer calls itself, or the label this machine happened
to dial with — so the overview showed the peer's name while the device list
beside it showed the name the user had typed.  Measured on the reporting install,
one device held ``notes = "试试"`` and ``device_name = "Kais-MacBook"`` at once.

The names now come from the same map the history rows use, with the transport's
as the fallback — a peer this machine has no record of still reads as itself
rather than vanishing from a list that counted it.
"""

from __future__ import annotations

from internal.application.use_cases.overview import build_overview
from internal.config.config import Config, PeerInfo

MACBOOK = "483fa196a05a"
OWN_NAME = "Kais-MacBook"
USER_NAME = "试试"


class Transport:
    """The two reads the overview makes of it, as the real one answers."""

    def get_connected_peers(self):
        return [MACBOOK]

    def get_connected_peers_with_names(self):
        # What the peer calls itself, straight off the connection.
        return [(MACBOOK, OWN_NAME)]


class Pairing:
    def get_paired_peers(self):
        return [PeerInfo(device_id=MACBOOK, device_name=OWN_NAME, paired=True)]


class Runtime:
    """A runtime with a link up and nothing else running.

    Only the two attributes this use case reads before its own guards take over:
    a missing manager degrades to a zero rather than failing the page, which is
    the contract the module states for itself.
    """

    transport = Transport()
    pairing = Pairing()


def config(note=USER_NAME, peers=True):
    cfg = Config(encryption_enabled=False)
    cfg.device_id = "4c5d7e51308a"
    cfg.device_name = "NC-5060"
    cfg.peers = (
        {MACBOOK: PeerInfo(device_id=MACBOOK, device_name=OWN_NAME, notes=note, paired=True)}
        if peers
        else {}
    )
    return cfg


def test_a_connected_device_is_named_the_way_the_device_list_names_it():
    overview = build_overview(config(), None, Runtime(), 0.0)
    assert overview["connected_count"] == 1
    assert overview["connected_names"] == [USER_NAME]


def test_the_peers_own_name_is_what_a_device_the_user_never_renamed_gets():
    """The control: dropping the transport's answer entirely would fail here."""
    overview = build_overview(config(note=""), None, Runtime(), 0.0)
    assert overview["connected_names"] == [OWN_NAME]


def test_a_peer_the_config_does_not_know_keeps_the_transports_name():
    """Counted and named from the same two sources, so it cannot be one without
    the other: a connected paired peer absent from `config.peers` still reads as
    itself rather than disappearing from a list whose badge counts it."""
    overview = build_overview(config(peers=False), None, Runtime(), 0.0)
    assert overview["connected_count"] == 1
    assert overview["connected_names"] == [OWN_NAME]
