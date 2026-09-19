"""What this device says about itself on the LAN, and what it believes of others.

The mDNS record is the only place a device describes itself without being
asked, and it carries two names: the truncated instance label, which is what
keeps two devices with similar names from colliding, and — for a device whose
user chose one — the name itself.  Peers list a device by the second, and the
first is only what a peer older than that field can offer.
"""

import os
import socket
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zeroconf import DNSPointer, RecordUpdate  # noqa: E402

# The wire numbers for the PTR records these tests hand the listener, the same
# private constants the module under test imports for the question it asks.
from zeroconf.const import _CLASS_IN, _TYPE_PTR  # noqa: E402

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
    # ``timeout`` is how the presence round bounds a cache miss; the browser
    # path leaves it to the library.  Both have to be accepted here, because
    # the same method serves both.
    zc = SimpleNamespace(get_service_info=lambda *_, **__: info)
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


# ── who is still here ───────────────────────────────────────────────────
#
# Presence is measured, not inferred: this machine asks the network who is
# there every ten seconds, and a name that goes unheard for twenty is a device
# that has left.  Nothing here needs a socket or a thread -- the round takes
# the answers it is given.


def rig(peers):
    """A Discovery that runs presence rounds with no network and no waiting.

    ``peers`` maps the label a peer advertises under to its device id.  The
    fake zeroconf answers a round for every one of those names at the moment
    the round asks, which is when a real answer arrives and what the presence
    listener stamps ``_heard`` from.  Removing a name from the returned set is
    what "the peer stopped answering" looks like from here: the record is still
    in the cache and the round still asks, but no answer comes back.

    The stop event returns immediately instead of waiting out the 600 ms settle
    window, which is a real wait on a real network and not what is under test.
    """
    found, lost = [], []
    subject = service()
    subject._our_ip = "192.168.1.5"
    subject.set_callbacks(lambda *args: found.append(args), lost.append)
    infos = {
        f"{label}._clipsync._tcp.local.": SimpleNamespace(
            properties={b"device_id_hash": Discovery._hash_device_id(pid).encode()},
            addresses=[socket.inet_aton("192.168.1.7")],
            port=9999,
        )
        for label, pid in peers.items()
    }
    answering = set(infos)

    def ask(_outgoing):
        for name in answering:
            subject._heard[name] = time.monotonic()

    subject._zc = SimpleNamespace(
        get_service_info=lambda _type, name, timeout=None: infos.get(name),
        send=ask,
        generate_service_broadcast=lambda info, ttl: object(),
    )
    subject._browser = SimpleNamespace()
    subject._netmon_stop = SimpleNamespace(wait=lambda *_: False, is_set=lambda: False)
    return subject, found, lost, answering


def goes_silent(subject, answering):
    """The peer is switched off: its last answer ages out, and none follows.

    Cheaper than sleeping twenty seconds and the same thing -- what the sweep
    reads is how long ago a name was stamped, not the wall clock.
    """
    with subject._heard_lock:
        for name in subject._heard:
            subject._heard[name] -= discovery_module.PRESENCE_TIMEOUT_SECONDS + 1
    answering.clear()


def pointer(alias, created, type_name="_clipsync._tcp.local.", ttl=4500):
    """One PTR as an answer packet carries it: the service type it belongs to,
    the instance it names, and when this machine received it."""
    return DNSPointer(type_name, _TYPE_PTR, _CLASS_IN, ttl, alias, created)


def deliver(subject, now, records):
    """Hand one packet's worth of records to the presence listener.

    Rebuilt per call, which is the same thing as the one long-lived instance the
    round attaches: the listener carries nothing of its own — every stamp it
    makes lands on the Discovery it was built from.
    """
    discovery_module._PresenceListener(subject).async_update_records(
        None, now, [RecordUpdate(record) for record in records]
    )


def test_a_silent_peer_is_reported_lost():
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")

    subject._presence_round()
    assert [call[0] for call in found] == [peer]

    goes_silent(subject, answering)
    subject._presence_round()

    assert lost == [peer]
    assert subject._known_peers == {}


def test_a_peer_that_answers_again_is_on_the_list_again():
    """One answer is enough to come back, without re-announcing or restarting.

    This is what the browser cannot do: it fires Added only on *novelty*, and
    the cached PTR of a peer that was reported lost is not novel -- the library
    holds it for tens of minutes.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")

    subject._presence_round()
    goes_silent(subject, answering)
    subject._presence_round()
    assert lost == [peer]

    answering.add("Kitchen-9f2a._clipsync._tcp.local.")
    subject._presence_round()

    assert [call[0] for call in found] == [peer, peer]


def test_resuming_the_browse_does_not_lose_a_device_that_answers():
    """A pause and resume is the one moment every device is unvouched for.

    ``start_browsing`` clears the name→id map, and that map is the evidence a
    sweep reads, so a device that answers the round which follows has to be
    read back into it before the sweep asks -- or a resume flaps the whole list.
    """
    subject, found, lost, _answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()

    with subject._lock:
        subject._service_to_peer.clear()  # what start_browsing() does

    subject._presence_round()

    assert lost == []
    assert [call[0] for call in found] == [peer]


def test_only_a_pointer_that_just_arrived_counts_as_hearing_a_peer():
    """The listener stamps what arrived, not what the cache still holds.

    Registering a listener replays the cache, and the library's sweep delivers
    records on their way out; both come back looking exactly like an answer
    (``old`` is None either way).  A stamp from either would keep a device that
    has been switched off on the list for as long as its record lives -- and the
    library clamps a PTR's TTL up to 1125 seconds, so that is tens of minutes.
    """
    subject = service()
    subject._instance_name = "Desktop-4c1d._clipsync._tcp.local."
    now = 1_000_000.0
    kitchen = "Kitchen-9f2a._clipsync._tcp.local."

    deliver(subject, now, [pointer(kitchen, now)])
    assert list(subject._heard) == [kitchen]

    # Replayed from the cache, and swept out of it: neither is a peer talking.
    subject._heard.clear()
    deliver(
        subject,
        now,
        [
            pointer(kitchen, now - discovery_module.PRESENCE_STALE_MILLIS - 1),
            pointer("Hall-3c4d._clipsync._tcp.local.", now - 500, ttl=0),
        ],
    )
    assert subject._heard == {}

    # Our own record coming back to us, and a printer's, which shares the
    # multicast address and is not a peer.
    deliver(
        subject,
        now,
        [
            pointer(subject._instance_name, now),
            pointer("HP-1._ipp._tcp.local.", now, type_name="_ipp._tcp.local."),
        ],
    )
    assert subject._heard == {}


def test_the_question_claims_to_know_nothing():
    """A known answer suppresses the whole reply, additionals included.

    A responder entitled to suppress an answer sends nothing at all -- not the
    PTR, and not the SRV, TXT and addresses that would have travelled beside it
    -- so a question carrying the PTRs this machine already has cached is a
    question a peer that is sitting right there is never asked.  That is how
    "the device is not discovered" happens while the device is on the network,
    and it is why the question is built by hand rather than by the library's own
    service query.
    """
    question = service()._build_query()
    assert [(q.name, q.type) for q in question.questions] == [
        ("_clipsync._tcp.local.", _TYPE_PTR)
    ]
    assert question.answers == []


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
