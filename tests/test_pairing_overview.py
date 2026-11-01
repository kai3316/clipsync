"""The overview counts devices, not routes (中-30).

Reported symptom: the devices page shows two paired devices while the overview
says one.  The counters read the LAN pairing repository and the LAN transport
alone, so a device paired by internet code was invisible to them even though the
device list already draws it (and, with both routes, still draws it once).

These pin the union: LAN pinned ids ∪ confirmed code-pairing ids, deduplicated
by device id; "connected" is the LAN session *or* the relay's own liveness for
that pairing.  They also pin the degradation the module promises: a runtime with
no internet pairing service (a locked install, an old runtime, the fake used by
`tests/test_overview_device_names.py`) must keep its old numbers.
"""

from internal.application.use_cases.overview import build_overview
from internal.config.config import Config, PeerInfo

LAN = "lan-peer"
RELAY = "relay-peer"
BOTH = "both-peer"


class Transport:
    def __init__(self, connected=(), names=None):
        self._connected = list(connected)
        self._names = dict(names or {})

    def get_connected_peers(self):
        return list(self._connected)

    def get_connected_peers_with_names(self):
        return [(pid, self._names.get(pid, pid)) for pid in self._connected]


class Pairing:
    def __init__(self, peers=()):
        self._peers = [
            PeerInfo(device_id=pid, device_name=name, paired=True) for pid, name in peers
        ]

    def get_paired_peers(self):
        return list(self._peers)


class InternetPairing:
    def __init__(self, peers=(), broken=False):
        self._peers = list(peers)
        self._broken = broken

    def paired_peers(self):
        if self._broken:
            raise RuntimeError("relay channel not ready")
        return list(self._peers)


class Runtime:
    def __init__(self, connected=(), lan_peers=(), internet_peers=None, names=None):
        self.transport = Transport(connected, names)
        self.pairing = Pairing(lan_peers)
        if internet_peers is not None:
            self.internet_pairing = InternetPairing(internet_peers)


def internet(peer_id, online=True, name="", alias=""):
    return {
        "peer_id": peer_id,
        "name": name or peer_id,
        "alias": alias,
        "online": online,
        "last_seen": 1.0 if online else None,
        "paired": True,
    }


def config(peers=(), netpair_aliases=None):
    cfg = Config(encryption_enabled=False)
    cfg.device_id = "self1"
    cfg.device_name = "Self"
    cfg.peers = {
        pid: PeerInfo(device_id=pid, device_name=name, paired=True) for pid, name in peers
    }
    if netpair_aliases:
        cfg.netpair_aliases = dict(netpair_aliases)
    return cfg


def test_a_lan_only_install_keeps_its_old_counts():
    """The control: the change must not move numbers that were already right."""
    overview = build_overview(
        config(peers=[(LAN, "Desk")]),
        None,
        Runtime(connected=[LAN], lan_peers=[(LAN, "Desk")]),
        0.0,
    )
    assert overview["paired_count"] == 1
    assert overview["connected_count"] == 1
    assert overview["connected_names"] == ["Desk"]


def test_a_relay_paired_device_is_counted_and_named():
    overview = build_overview(
        config(),
        None,
        Runtime(internet_peers=[internet(RELAY, name="Laptop")]),
        0.0,
    )
    assert overview["paired_count"] == 1
    assert overview["connected_count"] == 1
    assert overview["connected_names"] == ["Laptop"]


def test_an_offline_relay_peer_is_paired_but_not_connected():
    overview = build_overview(
        config(), None, Runtime(internet_peers=[internet(RELAY, online=False)]), 0.0
    )
    assert overview["paired_count"] == 1
    assert overview["connected_count"] == 0
    assert overview["connected_names"] == []


def test_one_device_with_both_routes_is_counted_once():
    """The device list draws one row per device; so must the counters."""
    overview = build_overview(
        config(peers=[(BOTH, "Studio")]),
        None,
        Runtime(
            connected=[BOTH],
            lan_peers=[(BOTH, "Studio")],
            internet_peers=[internet(BOTH, name="Studio", online=True)],
        ),
        0.0,
    )
    assert overview["paired_count"] == 1
    assert overview["connected_count"] == 1
    assert overview["connected_names"] == ["Studio"]


def test_the_two_routes_union_instead_of_double_counting():
    overview = build_overview(
        config(peers=[(LAN, "Desk")]),
        None,
        Runtime(
            connected=[LAN],
            lan_peers=[(LAN, "Desk")],
            internet_peers=[internet(RELAY, name="Laptop")],
        ),
        0.0,
    )
    assert overview["paired_count"] == 2
    assert overview["connected_count"] == 2
    assert overview["connected_names"] == ["Desk", "Laptop"]


def test_a_relay_peer_is_named_the_way_the_device_list_names_it():
    """The user's own name wins over the one the peer published at hello."""
    overview = build_overview(
        config(netpair_aliases={RELAY: "书房的本子"}),
        None,
        Runtime(internet_peers=[internet(RELAY, name="Laptop")]),
        0.0,
    )
    assert overview["connected_names"] == ["书房的本子"]


def test_a_runtime_without_internet_pairing_still_renders():
    """The fake runtime older tests use has no service; the page must not fail."""
    overview = build_overview(config(), None, Runtime(connected=[LAN], lan_peers=[]), 0.0)
    assert overview["paired_count"] == 0
    assert overview["connected_count"] == 0


def test_a_broken_internet_pairing_service_degrades_to_the_lan_counts():
    runtime = Runtime(connected=[LAN], lan_peers=[(LAN, "Desk")])
    runtime.internet_pairing = InternetPairing(broken=True)
    overview = build_overview(config(), None, runtime, 0.0)
    assert overview["paired_count"] == 1
    assert overview["connected_count"] == 1
