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
        # The keyword the real registration carries (cooperating_responders)
        # is accepted and ignored: what this test holds is which name went out.
        register_service=lambda info, **_: registered.append(info),
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

    # Unless the app has asked to forget the peer -- a device removed and then
    # restored comes back announcing exactly this record, and this cache is
    # what would read it as "nothing changed" and report nothing at all.
    subject.forget_peer("peer-2")
    subject._handle_service_added(*sighting(properties))
    assert len(seen) == 2


# ── who is still here ───────────────────────────────────────────────────
#
# Presence is measured, not inferred: this machine asks the network who is
# there every ten seconds, and a name that goes unheard for sixty is a device
# that has left.  Twenty was the old budget, two dropped answers, and the logs
# showed it produced flaps while the peers were up -- so the timeout is six
# rounds now, and the tests below pin both halves of that.


def rig(peers):
    """A Discovery that runs presence rounds with no network and no waiting.

    ``peers`` maps the label a peer advertises under to its device id.  The
    fake zeroconf answers a round for every one of those names at the moment
    the round asks, which is when a real answer arrives and what the presence
    listener stamps ``_heard`` from.  Removing a name from the returned set is
    what "the peer stopped answering" looks like from here: the record is still
    in the cache and the round still asks, but no answer comes back.

    The stop event returns immediately instead of waiting out the settle
    window (``SCAN_SETTLE_SECONDS``), which is a real wait on a real network and
    not what is under test.  Nothing here sets ``_browse_since``, so the sweep
    reports losses the moment it is asked -- the grace it grants a freshly
    rebuilt browse is one test's subject, not this rig's default.
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

    # And it counts even when the answer lands between two rounds.  The listener
    # stamps it whenever the packet arrives, so an answer that missed the window
    # is a round late rather than a device that stays off the list until it
    # restarts -- which is what a round that read only what arrived *during* the
    # window would make of it.  (A record held back a second by the library's
    # flood protection is this, and so is any answer to somebody else's query.)
    goes_silent(subject, answering)
    subject._presence_round()
    assert lost == [peer, peer]

    with subject._heard_lock:
        subject._heard["Kitchen-9f2a._clipsync._tcp.local."] = time.monotonic()
    subject._presence_round()

    assert [call[0] for call in found] == [peer, peer, peer]
    assert lost == [peer, peer]


def test_resuming_the_browse_does_not_lose_a_device_that_answers():
    """A pause and resume is the one moment every device is unvouched for.

    ``start_browsing`` clears the name→id map, and that map is the evidence a
    sweep reads, so a device that answers the round which follows has to be
    read back into it before the sweep asks -- or a resume flaps the whole list.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()

    def rebuild():
        """What start_browsing() does, plus the clock it restarts with it."""
        with subject._lock:
            subject._service_to_peer.clear()
            subject._browse_since = time.monotonic()

    rebuild()
    subject._presence_round()

    assert lost == []
    assert [call[0] for call in found] == [peer]

    # The other half of it is the round that runs before anyone has answered
    # the new browse: the map is empty, so on the evidence available every peer
    # looks gone, and a network change, a wake or a VPN toggle would empty the
    # list on the way back up.  Silence is evidence only once it has had a
    # timeout in which it could have been broken.
    rebuild()
    answering.clear()
    goes_silent(subject, answering)
    subject._presence_round()

    assert lost == []
    assert subject._known_peers != {}


def test_a_couple_of_missed_answers_does_not_report_a_peer_lost():
    """Two dropped multicast answers are not a departure.

    The old timeout was 20 s, so the silence measured here (two rounds plus a
    second) reported the peer lost while it was still there; the logs show that
    as the Peer lost/Discovered pairs 0.3 s apart.  The peer has to miss six
    consecutive answers now, and the second half of the test pins that a
    genuinely long silence is still reported.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()
    assert [call[0] for call in found] == [peer]

    with subject._heard_lock:
        for name in subject._heard:
            subject._heard[name] -= 2 * discovery_module.PRESENCE_INTERVAL_SECONDS + 1
    answering.clear()
    subject._presence_round()

    assert lost == [], "two missed rounds were read as a departure"
    assert peer in subject._known_peers

    # Six missed rounds, on the other hand, is a device that has left.
    with subject._heard_lock:
        for name in subject._heard:
            subject._heard[name] -= discovery_module.PRESENCE_TIMEOUT_SECONDS
    subject._presence_round()

    assert lost == [peer]
    assert subject._known_peers == {}


def test_a_browse_that_asks_and_hears_nobody_is_rebuilt(caplog, monkeypatch):
    """asked=True answered=0 for a minute is a deaf socket, not a quiet LAN.

    ``zc.send`` does not raise into a socket with no route, so the presence
    round keeps logging asked=True while the device list empties -- six ~31
    minute runs of exactly that are in pc-zhao's log.  The repair replaces the
    Zeroconf instance through the path the address watcher already uses, which
    is what clears it in the field; a peer that was seen at least once is what
    tells this apart from a network with nobody on it.
    """
    import logging

    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()
    assert found, "the rig never discovered the peer"

    rebuilt: list[int] = []
    subject._restart_zeroconf = lambda: rebuilt.append(1)
    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()

    # A fresh host's monotonic clock starts near zero, and the cooldown must
    # not read that as "rebuilt just now" (CI caught exactly this).
    monkeypatch.setattr(discovery_module.time, "monotonic", lambda: 5.0)
    with caplog.at_level(logging.INFO, logger="internal.transport.discovery"):
        for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS):
            subject._presence_round()

    assert rebuilt == [1], f"the deaf browse was rebuilt {len(rebuilt)} times"
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "heard nobody" in r.getMessage()
    ]
    assert warnings, "the rebuild was not said out loud"


def test_a_network_with_no_peers_is_not_rebuilt():
    """A repair that fires on an empty network is worse than none.

    Nothing has ever answered here, so there is no evidence that anything is
    broken -- the browse must not be torn down and rebuilt every minute just
    because the user is alone on the LAN.
    """
    subject, found, lost, answering = rig({})

    rebuilt: list[int] = []
    subject._restart_zeroconf = lambda: rebuilt.append(1)

    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS + 2):
        subject._presence_round()

    assert rebuilt == []


def test_a_removed_record_does_not_report_a_peer_that_just_answered():
    """Removed is not proof of departure while the peer is still answering.

    The field logs have the ServiceBrowser log ``Peer lost`` 0.3 s before the
    presence round logs ``Discovered peer`` and ``answered=3 known=3`` for the
    same device.  One path must not overrule the answers; the sweep, which
    reads them, decides.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()

    subject._handle_service_removed("Kitchen-9f2a._clipsync._tcp.local.")

    assert lost == [], "a Removed flap reported a peer that was answering"
    assert peer in subject._known_peers
    assert "Kitchen-9f2a._clipsync._tcp.local." in subject._service_to_peer

    # And the arbitration still ends: with no answer for a full timeout the
    # sweep reports the loss it deferred.
    goes_silent(subject, answering)
    subject._presence_round()
    assert lost == [peer]


def test_a_removed_record_after_the_grace_window_is_still_reported():
    """A goodbye from a peer that stopped answering is reported by this path.

    The grace window is bounded: once the last answer is older than it, the
    Removed record is the only evidence left and the loss is reported at once.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()

    with subject._heard_lock:
        for name in subject._heard:
            subject._heard[name] -= discovery_module.REMOVED_GRACE_SECONDS + 1
    subject._handle_service_removed("Kitchen-9f2a._clipsync._tcp.local.")

    assert lost == [peer]
    assert subject._known_peers == {}


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


# ── Names that are not worth resolving ────────────────────────────────────────

# Transcribed from the log, not invented: 30 zero nibbles and the ip6.arpa zone.
THE_REVERSE_NAME = "1.0." + "0." * 29 + "ip6.arpa"


def test_a_reverse_dns_name_is_not_looked_up(monkeypatch):
    """The measurement that prompted this: 408 failures, one every 30 seconds.

    `getaddrinfo` is replaced with a recorder, so the assertion is about the call and not
    about how quiet the failure was.
    """
    calls: list[str] = []

    def recorder(name, *_args, **_kwargs):
        calls.append(name)
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(discovery_module.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(discovery_module.socket, "getfqdn", lambda: THE_REVERSE_NAME)
    monkeypatch.setattr(discovery_module.socket, "getaddrinfo", recorder)

    discovery_module._resolved_host_addresses(timeout=2.0)

    assert calls == ["host-a"], f"the reverse name was resolved: {calls}"


def test_the_skipped_name_is_still_explained(monkeypatch, caplog):
    """Said once, at debug: a reader should learn why `getfqdn()` contributed nothing."""
    import logging

    # The "said" flag is module state, because the fact it guards is about the machine and cannot
    # change while the process lives.  A test that reads the line therefore has to own the flag:
    # another test enumerating addresses first leaves it set, and this then finds no line at all --
    # which is exactly how it passed alone and failed in the suite.
    monkeypatch.setattr(discovery_module, "_skipped_names_said", False)

    monkeypatch.setattr(discovery_module.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(discovery_module.socket, "getfqdn", lambda: THE_REVERSE_NAME)
    monkeypatch.setattr(
        discovery_module.socket,
        "getaddrinfo",
        lambda name, *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )

    with caplog.at_level(logging.DEBUG, logger="internal.transport.discovery"):
        found = discovery_module._resolved_host_addresses(timeout=2.0)

    assert found == ["10.0.0.5"]
    skipped = [r.getMessage() for r in caplog.records if "Skipped unresolvable" in r.getMessage()]
    assert len(skipped) == 1, f"expected one line, got {skipped}"


def test_a_real_host_name_is_still_resolved(monkeypatch):
    """The control: filtering must not stop ordinary names from working."""
    calls: list[str] = []

    def recorder(name, *_args, **_kwargs):
        calls.append(name)
        return [(2, 1, 6, "", ("192.168.1.20", 0))]

    monkeypatch.setattr(discovery_module.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(discovery_module.socket, "getfqdn", lambda: "host-a.lan")
    monkeypatch.setattr(discovery_module.socket, "getaddrinfo", recorder)

    found = discovery_module._resolved_host_addresses(timeout=2.0)

    assert calls == ["host-a", "host-a.lan"]
    assert "192.168.1.20" in found


def test_the_predicate_matches_the_shapes_it_names():
    """Table-driven, because the cases are the specification."""
    predicate = discovery_module._is_resolvable_name
    cases = [
        (THE_REVERSE_NAME, False),
        ("38.31.168.192.in-addr.arpa", False),
        ("arpa", False),
        ("localhost", False),
        ("foo.localhost", False),
        ("", False),
        ("   ", False),
        ("Kais-MacBook-Air.local", True),
        ("NAME.Example.COM.", True),
        # An ordinary host may contain "arpa"; only a reverse name ends with it.
        ("my-arpa-company.example.com", True),
    ]
    wrong = [(n, predicate(n)) for n, want in cases if predicate(n) != want]
    assert wrong == [], f"unexpected answers: {wrong}"
# ── The presence round asks, rather than waiting to be told ───────────────────

def a_discovery(browsing=True, settled=True):
    """A real Discovery with its network collaborators replaced.

    Only `_zc` and `_browser` are stood in for: everything else is what the constructor
    builds, which is what keeps this test from failing on its own scaffolding.
    """
    sent: list = []

    class Recorder:
        def send(self, message):
            sent.append(message)

    discovery = discovery_module.Discovery(
        device_id="local-device",
        device_name="Host",
        port=19990,
        service_type="_clipsync._tcp.local.",
    )
    discovery._zc = Recorder()
    discovery._browser = object() if browsing else None
    # The round waits on this event for its settle window.  Set, it returns at once, which
    # keeps the test quick -- but the event is *also* how the advertisement repair knows a
    # shutdown is in progress, so a test that expects a repair has to leave it clear.
    if settled:
        discovery._netmon_stop.set()
    return discovery, sent


def test_a_presence_round_asks_the_network_who_is_here():
    """The re-query is the mechanism, so its absence would be the bug.

    A browser alone learns of a peer from an announcement, and one missed while this
    machine was not listening is one it never gets.  The round sends a PTR question of its
    own, which is what lets a peer that came back be noticed without a restart.
    """
    discovery, sent = a_discovery()

    discovery._presence_round()

    assert sent, "a presence round sent no query"
    question = sent[0].questions[0]
    assert question.name.rstrip(".") == discovery._service_type.rstrip("."), (
        f"asked about {question.name}, not the service type"
    )


def test_the_query_carries_no_known_answers():
    """Because a responder may suppress an answer it can see we already have.

    The round's own comment: the library's service query carries the cached PTRs it is
    asking about, "and a responder that is entitled to suppress those answers sends nothing
    at all -- PTR, SRV, TXT and address alike -- so a peer that is sitting there answering
    questions is never asked one it will answer."
    """
    discovery, sent = a_discovery()

    discovery._presence_round()

    assert not sent[0].answers, f"the query carried {len(sent[0].answers)} known answers"


def test_the_browse_session_gates_the_round():
    """No browser means this machine is not listening, so a round must do nothing.

    The loop's comment says why: a round that ran anyway "would report every peer lost for
    the crime of going unheard by a machine that had stopped listening".
    """
    discovery, sent = a_discovery(browsing=False)

    discovery._presence_round()

    assert sent == [], "a round queried the network while browsing was paused"


def test_a_failed_query_is_reported(monkeypatch, caplog):
    """`zc.send` raising is how a round does nothing while looking healthy.

    zeroconf raises on an interface it cannot use -- an adapter left by WSL or Docker is the
    usual one, and this machine's log has `Error with socket (('172.19.0.1', 5353))`.  The
    question then never leaves, and before this the application said nothing about it and
    went on discovering nobody until it was restarted.
    """
    import logging

    discovery, _ = a_discovery()

    class Refusing:
        def send(self, message):
            raise OSError(59, "An unexpected network error occurred")

    discovery._zc = Refusing()

    with caplog.at_level(logging.DEBUG, logger="internal.transport.discovery"):
        discovery._presence_round()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a failed query produced no warning"
    assert "could not ask" in warnings[0].getMessage()


def test_the_round_is_counted_so_asking_is_answerable(caplog):
    """The debug line is the evidence that distinguishes the two halves of the symptom.

    "We asked and nobody answered" and "we never asked" need different fixes, and without
    this line they are indistinguishable in a user's log.
    """
    import logging

    discovery, _ = a_discovery()

    with caplog.at_level(logging.DEBUG, logger="internal.transport.discovery"):
        discovery._presence_round()
        discovery._presence_round()

    rounds = [r.getMessage() for r in caplog.records if "Presence round" in r.getMessage()]
    assert len(rounds) == 2, f"expected one line per round, got {rounds}"
    assert "asked=True" in rounds[0], rounds[0]
    assert "answered=0" in rounds[0], rounds[0]


def test_a_round_still_survives_a_failing_send():
    """Suppression stays: a raising round must not kill the presence thread."""
    discovery, _ = a_discovery()

    class Refusing:
        def send(self, message):
            raise OSError(59, "nope")

    discovery._zc = Refusing()
    # Returning rather than raising is the assertion; the thread this runs on has no
    # handler above it, so an exception here is the failure mode being guarded against.
    discovery._presence_round()


# ── The discovery state line and the advertisement repair ─────────────────────

def test_the_state_line_is_reported_on_its_cadence(caplog):
    """One line a minute, not one a round: it is a status, and it is read by a person.

    It answers "can this machine be found, and can it find anyone" from a log alone, which is
    what a fault that a restart cures needs -- a restart says the state was wrong, and this
    says which half of it.
    """
    import logging

    discovery, _ = a_discovery()
    # A registered service, so the repair below stays out of the way.
    discovery._service_info = object()

    with caplog.at_level(logging.DEBUG, logger="internal.transport.discovery"):
        for _ in range(discovery_module.DISCOVERY_STATE_EVERY_ROUNDS):
            discovery._presence_round()

    states = [r.getMessage() for r in caplog.records if "Discovery state:" in r.getMessage()]
    assert len(states) == 1, f"expected one line per cadence, got {len(states)}"
    for fragment in ("advertising=True", "browsing=True", "known_peers=", "heard_services="):
        assert fragment in states[0], f"{fragment!r} missing from {states[0]!r}"


def test_a_lost_advertisement_is_re_registered(caplog):
    """The condition a restart cures: no service info while the runtime is up.

    `_network_watch_loop` only rebuilds when the local address *set* changes, so a
    registration lost while the address stays the same was never retried.  A device in that
    state is reachable by address, healthy, and invisible.
    """
    import logging

    discovery, _ = a_discovery(settled=False)
    discovery._service_info = None  # the advertisement has gone

    calls: list[int] = []

    def fake_publish():
        calls.append(1)
        discovery._service_info = object()  # and this time it works
        return True

    discovery._publish = fake_publish

    with caplog.at_level(logging.INFO, logger="internal.transport.discovery"):
        discovery._presence_round()

    assert calls == [1], "the lost advertisement was not registered again"
    said = [r.getMessage() for r in caplog.records if "not advertising" in r.getMessage()]
    assert len(said) == 1, f"the repair was not said out loud: {said}"


def test_a_healthy_advertisement_is_left_alone():
    """A repair that fires when nothing is wrong is worse than none."""
    discovery, _ = a_discovery(settled=False)
    discovery._service_info = object()

    calls: list[int] = []
    discovery._publish = lambda: calls.append(1) or True

    discovery._presence_round()

    assert calls == [], "the repair ran while the device was advertising"


def test_nothing_is_repaired_while_stopping():
    """A registration that loses the race with shutdown is not a fault to report."""
    discovery, _ = a_discovery(settled=True)
    discovery._service_info = None  # and a shutdown is in progress

    calls: list[int] = []
    discovery._publish = lambda: calls.append(1) or True

    discovery._presence_round()

    assert calls == [], "the repair ran during a shutdown"
