"""What this device says about itself on the LAN, and what it believes of others.

The mDNS record is the only place a device describes itself without being
asked, and it carries two names: the truncated instance label, which is what
keeps two devices with similar names from colliding, and — for a device whose
user chose one — the name itself.  Peers list a device by the second, and the
first is only what a peer older than that field can offer.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internal.system.updater import running_shell  # noqa: E402
from internal.transport import discovery as discovery_module  # noqa: E402
from internal.transport.discovery import Discovery  # noqa: E402


def service(device_name="Desktop", device_id="peer-1"):
    return Discovery(device_id, device_name, 9999, "_clipsync._tcp.local.")


def fast_addresses(monkeypatch, addresses=("192.168.1.5",)):
    monkeypatch.setattr(discovery_module, "get_all_local_addresses", lambda: list(addresses))
    monkeypatch.setattr(discovery_module, "_get_local_address", lambda: addresses[0])


def hostname(monkeypatch, name):
    monkeypatch.setattr(discovery_module.platform, "node", lambda: name)


# ── what we publish ─────────────────────────────────────────────────────


def test_the_label_stays_truncated_and_id_tagged(monkeypatch):
    """The instance name is a collision-free handle, not a name to read.

    Two devices whose names share their first 8 characters both advertise under
    "<8 chars>-<id hash>", which is what stops them resolving to each other.
    """
    long_name = "书房的台式机"
    label = Discovery._label(long_name, Discovery._hash_device_id("peer-1"))
    assert label == f"{long_name[:8]}-{Discovery._hash_device_id('peer-1')[:4]}"


def test_a_chosen_name_is_published_in_the_txt_record(monkeypatch):
    """Peers have to be able to learn the name; the label cannot carry it."""
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    props = service("书房的台式机")._service_props()
    assert props[b"n"].decode("utf-8") == "书房的台式机"
    # Alongside what the record already carried, not instead of it.
    assert props[b"device_id_hash"] == Discovery._hash_device_id("peer-1").encode()
    assert b"v" in props and b"os" in props and b"arch" in props
    # Which of the two applications this is.  Both are published from one
    # repository and one release, so a Windows device here may be running
    # either, and the installer for one is not installable by the other.
    assert props[b"app"] == running_shell().encode("utf-8")


def test_the_hostname_is_not_published(monkeypatch):
    """A name the user never chose is not something this record hands out.

    The truncated label exists so a hostname is not broadcast in plaintext on
    the LAN, and the default device name IS the hostname.
    """
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    assert b"n" not in service("sukai-desktop")._service_props()
    # A name the user did choose goes out even when it reads like one.
    assert b"n" in service("sukai-laptop")._service_props()


def test_a_name_too_long_for_a_txt_string_is_trimmed(monkeypatch):
    """The wire caps a TXT string at 255 bytes; the settings page caps nothing.

    Character count is not that cap — 64 emoji are 256 bytes — and a record
    that cannot be packed is a registration that raises rather than a device
    that advertises.
    """
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    published = service("🎈" * 200)._service_props()[b"n"]
    assert len(published) <= 200
    assert published.decode("utf-8") == "🎈" * 50, "whole characters only"


def test_a_rename_is_registered_rather_than_only_stored(monkeypatch):
    """An instance name is part of the registration, so a rename is a new one.

    Both instances must not stay live: a peer that learned the old name keeps
    resolving to it, which is a device that is present and unreachable.
    """
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    registered, unregistered = [], []
    subject = service("书房的台式机")
    subject._zc = SimpleNamespace(
        register_service=registered.append,
        unregister_service=unregistered.append,
    )
    # Not None: this device is advertising.
    subject._service_info = SimpleNamespace(name=f"{subject._display_name}.{subject._service_type}")
    old_label = subject._display_name

    subject.set_device_name("书房的笔记本")

    assert [info.name for info in unregistered] == [f"{old_label}._clipsync._tcp.local."]
    assert [info.name for info in registered] == [
        f"{Discovery._label('书房的笔记本', subject._device_id_hash)}._clipsync._tcp.local."
    ]
    assert registered[0].properties[b"n"] == "书房的笔记本".encode()


def test_a_rename_of_only_the_hostname_changes_nothing_to_register(monkeypatch):
    """Same 8 characters means the same instance name: nothing to re-register."""
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    registered = []
    subject = service("sukai-desktop-one")
    subject._zc = SimpleNamespace(
        register_service=registered.append,
        unregister_service=lambda _info: None,
    )
    subject._service_info = SimpleNamespace(name=f"{subject._display_name}.{subject._service_type}")

    subject.set_device_name("sukai-desktop-two")

    # It is stored — the address book of who we are is not the label — but no
    # second instance is registered for a name peers cannot tell apart anyway.
    assert subject._device_name == "sukai-desktop-two"
    assert registered == []


def test_renaming_to_the_same_name_is_not_a_change(monkeypatch):
    fast_addresses(monkeypatch)
    hostname(monkeypatch, "sukai-desktop")
    registered = []
    subject = service("书房的台式机")
    subject._zc = SimpleNamespace(
        register_service=registered.append,
        unregister_service=lambda _info: None,
    )
    subject._service_info = SimpleNamespace(name=f"{subject._display_name}.{subject._service_type}")
    subject.set_device_name("书房的台式机")
    assert registered == []


# ── what we believe of others ───────────────────────────────────────────


def sighting(properties, address="192.168.1.7", name="Kitchen-9f2a._clipsync._tcp.local."):
    """The three arguments the browser hands _handle_service_added."""
    info = SimpleNamespace(
        properties=properties,
        addresses=[__import__("socket").inet_aton(address)],
        port=9999,
    )
    zc = SimpleNamespace(get_service_info=lambda *_: info)
    return zc, "_clipsync._tcp.local.", name


def discovered():
    seen = []
    subject = service()
    subject._our_ip = "192.168.1.5"
    subject.set_callbacks(
        lambda *args: seen.append(args),
        lambda *_args: None,
    )
    return subject, seen


def test_a_peer_that_published_its_name_is_listed_under_it(monkeypatch):
    fast_addresses(monkeypatch)
    subject, seen = discovered()
    zc, service_type, name = sighting(
        {
            b"device_id_hash": Discovery._hash_device_id("peer-2").encode(),
            b"n": "厨房的树莓派".encode(),
            b"app": b"tauri",
        }
    )

    subject._handle_service_added(zc, service_type, name)

    peer_id, peer_name, address, port, *_rest, named = seen[0]
    assert (peer_id, peer_name, address, port) == (
        Discovery._hash_device_id("peer-2"),
        "厨房的树莓派",
        "192.168.1.7",
        9999,
    )
    # Which application the peer runs travels with the rest of what it said: it
    # is not a property of the OS, and the device list needs it to know whether
    # an update exchange is possible at all.
    assert seen[0][7] == "tauri"
    assert named is True, "the peer's own answer may outrank a name on record"


def test_a_peer_that_published_nothing_falls_back_to_its_label(monkeypatch):
    """Older builds send no name; the truncated label is all they offer."""
    fast_addresses(monkeypatch)
    subject, seen = discovered()
    zc, service_type, name = sighting(
        {
            b"device_id_hash": Discovery._hash_device_id("peer-2").encode(),
        }
    )

    subject._handle_service_added(zc, service_type, name)

    *_, named = seen[0]
    assert seen[0][1] == "Kitchen-9f2a"
    assert named is False, "a label is not a name, and may not replace one"


def test_a_rename_re_announces_as_a_change(monkeypatch):
    """The re-announcement is the only word this machine gets of a rename."""
    fast_addresses(monkeypatch)
    subject, seen = discovered()
    hashed = Discovery._hash_device_id("peer-2").encode()
    before = sighting({b"device_id_hash": hashed, b"n": "厨房的树莓派".encode()})
    after = sighting({b"device_id_hash": hashed, b"n": "客厅的树莓派".encode()})

    subject._handle_service_added(*before)
    subject._handle_service_added(*after)

    assert [call[1] for call in seen] == ["厨房的树莓派", "客厅的树莓派"]


def test_the_same_sighting_twice_is_not_reported_twice(monkeypatch):
    """mDNS re-announces constantly, and every report is a device-list rebuild."""
    fast_addresses(monkeypatch)
    subject, seen = discovered()
    properties = {
        b"device_id_hash": Discovery._hash_device_id("peer-2").encode(),
        b"n": "厨房的树莓派".encode(),
    }
    subject._handle_service_added(*sighting(properties))
    subject._handle_service_added(*sighting(properties))
    assert len(seen) == 1


# ── a tunnel is not a piece of wire ─────────────────────────────────────


def test_a_tunnel_is_read_from_the_description_not_the_name(monkeypatch):
    """The friendly name is whatever the vendor or the locale made it, and the
    description is the product's own words.

    "VirtualNet" and "Heysocks" match nothing a list of tunnel names would
    hold, so a live tunnel read as an ordinary adapter -- and its address, being
    private by range, went out to every peer on the LAN as one to dial.
    """
    kind = discovery_module._kind_of
    assert kind("VirtualNet VirtualNet Tunnel") == "virtual"
    assert kind("Heysocks TAP-Windows Adapter V9") == "virtual"
    assert kind("WireGuard Tunnel WireGuard Tunnel") == "virtual"
    # A real adapter, wherever its name is in a language this code cannot read.
    assert kind("Wi-Fi MediaTek Wi-Fi 7 MT7925 Wireless LAN Card") == "wifi"
    assert kind("Ethernet Intel(R) Ethernet Controller I226-V") == "ethernet"
    # The ordering the whole classification rests on: a virtual adapter whose
    # description contains a wire's word is still virtual.
    assert kind("vEthernet (WSL) Hyper-V Virtual Ethernet Adapter") == "virtual"
    # Where a name is all the platform offers, it carries the same distinction.
    assert kind("en0", "en0") == "ethernet"
    assert kind("utun3", "utun3") == "virtual"
    assert kind("eth0", "eth0") == "ethernet"
    assert kind("wlp2s0", "wlp2s0") == "wifi"
    assert kind("br-1a2b3c", "br-1a2b3c") == "virtual"


def test_a_tunnel_address_is_advertised_only_when_it_is_all_there_is(monkeypatch):
    """A peer that dials a tunnel address is a peer that lists a dead device."""
    kinds = {"192.168.31.250": "wifi", "172.19.0.1": "virtual"}
    assert discovery_module._advertisable(["172.19.0.1", "192.168.31.250"], kinds) == [
        "192.168.31.250"
    ]
    # Nothing left to offer but the tunnel: an address a peer may not reach
    # beats no address at all.
    assert discovery_module._advertisable(["172.19.0.1"], kinds) == ["172.19.0.1"]


def test_a_peer_is_dialled_where_this_machine_would_reach_it(monkeypatch):
    """Two private addresses used to rank equally, and the tie was the string.

    So a peer advertising both its LAN address and its tunnel's was dialled at
    the tunnel's — "10." and "172." sort before "192." — from any machine not
    on its own /24.  Which of the two leaves over a real interface is the
    routing table's answer, and it is the only thing asked.
    """
    virtual = frozenset({"172.19.0.1"})
    pick = discovery_module._pick_best_address
    routes = {}
    monkeypatch.setattr(discovery_module, "_route_source", lambda ip: routes.get(ip))

    # The peer's tunnel address would leave through ours; its LAN address would
    # not.  This is the pair the old string tie-break got backwards.
    routes.update({"10.8.0.2": "172.19.0.1", "192.168.1.238": "192.168.31.250"})
    assert pick(["10.8.0.2", "192.168.1.238"], "192.168.31.250", virtual) == "192.168.1.238"

    # A tunnel is a real route and a missing one is not: of two addresses that
    # are not on our subnet, the one this machine can actually send to wins.
    routes.clear()
    routes.update({"10.8.0.2": None, "172.16.5.9": "172.19.0.1"})
    assert pick(["10.8.0.2", "172.16.5.9"], "192.168.31.250", virtual) == "172.16.5.9"

    # Same subnet still wins outright, whatever the routes say: this is the
    # case that must never change, and it is most of them.
    routes.clear()
    routes.update({"10.8.0.2": "192.168.31.250", "192.168.31.238": "172.19.0.1"})
    assert pick(["10.8.0.2", "192.168.31.238"], "192.168.31.250", virtual) == "192.168.31.238"
