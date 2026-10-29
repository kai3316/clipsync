"""Regression tests for the LAN-discovery fixes (严重-3/4, 中-11..16, 轻-7).

The companion file ``tests/test_discovery.py`` holds the behaviour these tests
are about; this file holds the specific faults AUDIT-PAIRING-2026-10-17 found in
it, one test per defect, so a regression names the finding it breaks.

Each test states the mechanism it pins, because the field evidence is the
reason the mechanism exists: 1828 of 3545 presence rounds in one user's log were
answered by nobody while the peers were up, 244 rebuilds followed, and 332
rebuilds of an idle network turned up in one five-day log.
"""

import logging
import os
import socket
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from zeroconf.const import _TYPE_PTR  # noqa: E402

from internal.transport import discovery as discovery_module  # noqa: E402
from internal.transport.discovery import Discovery  # noqa: E402
from tests.test_discovery import (  # noqa: E402
    fast_addresses,
    goes_silent,
    outlives_the_stale_window,
    rig,
    service,
    sighting,
)

# ── Test doubles ────────────────────────────────────────────────────────

class FakeZeroconf:
    """Just enough of a ``Zeroconf`` to register, unregister and browse."""

    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.listeners = []
        self.closed = False
        self.sent = []
        self.cache = SimpleNamespace(get_all_by_details=lambda *_a: [])

    def register_service(self, info, **_kwargs):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def add_listener(self, listener, question):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def send(self, message):
        self.sent.append(message)

    def generate_service_broadcast(self, info, ttl):
        return object()

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, *args, **kwargs):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeStopEvent:
    """The stop event, armed so waits return at once (as the other tests do)."""

    def __init__(self):
        self._set = False

    def wait(self, _timeout=None):
        return self._set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False


@pytest.fixture
def wire(monkeypatch):
    """Replace the network-facing collaborators with local fakes.

    Address enumeration, adapter classification and zero-configuration are all
    stubbed: the failures under test are decisions this module makes, not facts
    about the machine running the test suite.
    """

    def _wire(addresses=("192.168.1.5",)):
        monkeypatch.setattr(
            discovery_module, "get_all_local_addresses", lambda: list(addresses)
        )
        monkeypatch.setattr(
            discovery_module,
            "_get_local_address",
            lambda: addresses[0] if addresses else "127.0.0.1",
        )
        monkeypatch.setattr(discovery_module, "_adapter_kinds", lambda: {})
        monkeypatch.setattr(discovery_module, "_virtual_addresses", lambda: frozenset())
        monkeypatch.setattr(discovery_module, "_local_network_prefixes", lambda: {})
        monkeypatch.setattr(discovery_module, "ServiceBrowser", FakeBrowser)
        fresh = []

        def factory():
            zc = FakeZeroconf()
            fresh.append(zc)
            return zc

        monkeypatch.setattr(discovery_module, "Zeroconf", factory)
        return fresh, addresses

    return _wire


def a_discovery(monkeypatch):
    fast_addresses(monkeypatch)
    monkeypatch.setattr(discovery_module, "ServiceBrowser", FakeBrowser)
    subject = Discovery("local-device", "Desktop", 9999, "_clipsync._tcp.local.")
    subject._zc = FakeZeroconf()
    subject._netmon_stop = FakeStopEvent()
    return subject


def v6_sighting(properties, addresses, port=9999, name="Peer-9f2a._clipsync._tcp.local."):
    info = SimpleNamespace(properties=properties, addresses=list(addresses), port=port)
    zc = SimpleNamespace(get_service_info=lambda *_a, **_k: info)
    return zc, "_clipsync._tcp.local.", name


# ── 严重-3: a presence query that asks for a unicast answer ─────────────


def test_the_presence_question_asks_for_a_unicast_answer():
    """A QM question asks the responder to multicast; QU asks for a unicast one.

    The field failure was a machine that could send (``send_failures=0``) and
    heard nobody for 31 minutes at a stretch while the peers were up, and every
    rebuild "fixed" it because the library's own first query carries the QU bit.
    Asking that way every round is the fix; without it the peer answers into a
    multicast path that never reaches this process.
    """
    question = service()._build_query().questions[0]

    assert question.name.rstrip(".") == "_clipsync._tcp.local"
    assert question.type == _TYPE_PTR
    assert question.unicast is True, "the presence query is still a QM question"


# ── 严重-3: silence is not judged while this machine cannot hear ────────


def test_a_round_that_answered_nobody_keeps_a_cached_peer_as_stale():
    """A peer the browser still lists, in a round nobody answered: stale.

    The sweep is what reports a device that left, and it may not be driven by
    this machine's own deafness: a round in which *everybody* was silent while
    the browser's cache still holds the record is the fault the browse repair
    exists for.  The peer is kept (marked stale) rather than reported lost --
    which is what the field logs showed happening to devices that were up.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()

    goes_silent(subject, answering)
    subject._presence_round()

    assert lost == [], "a round that heard nobody reported a device it still lists"
    assert subject._known_peers and subject._known_peers[
        Discovery._hash_device_id("peer-2")
    ]["stale"] is True
    outlives_the_stale_window(subject)
    subject._presence_round()
    assert lost == [Discovery._hash_device_id("peer-2")]


def test_a_live_link_keeps_a_peer_the_browser_has_forgotten():
    """The runtime can vouch for a socket when the record has lapsed.

    ``set_link_checker`` is the transport's answer to "is that device still
    there": a peer whose mDNS answers stopped arriving but whose TCP link is
    live has not left, and reporting it lost is how a healthy device vanished
    from the list.  There is no callback wired in this test, so the browser's
    map is cleared to model the record's expiry.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    peer = Discovery._hash_device_id("peer-2")
    subject._presence_round()

    with subject._lock:
        subject._service_to_peer.clear()
    subject.set_link_checker(lambda _pid: True)
    goes_silent(subject, answering)
    subject._presence_round()

    assert lost == [], "a peer with a live link was reported lost"
    assert peer in subject._known_peers

    subject.set_link_checker(lambda _pid: False)
    subject._presence_round()
    assert lost == [peer]


def test_a_connected_peer_can_be_vouched_for_by_the_runtime_callback():
    """A raising checker is not evidence of absence: the sweep must survive it.

    With neither the browser's map nor a working link check behind it, the peer
    is judged on the ordinary timeout instead of on the exception.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()

    def boom(_pid):
        raise RuntimeError("transport went away")

    subject.set_link_checker(boom)
    with subject._lock:
        subject._service_to_peer.clear()
    goes_silent(subject, answering)
    subject._presence_round()

    assert lost == [Discovery._hash_device_id("peer-2")]


# ── 严重-3: the browse repair's backoff and its evidence ────────────────


def test_a_rebuild_that_does_not_help_waits_longer_next_time(monkeypatch):
    """Cooldown 5 min -> 10 min -> ..., so one unfixable fault is not a loop.

    The flat cooldown rebuilt the sockets every five minutes for as long as the
    process lived (332 rebuilds in one five-day log, median interval 306 s),
    each of which unregisters and re-registers this device's advertisement.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    # Pin the clock *before* the round that stamps the peer's answer, and to a
    # value near the host's own monotonic clock: the repair gives up when the
    # last answer is older than PEER_MEMORY_SECONDS, so a fixed 1000.0 on a
    # freshly booted CI runner (monotonic still in the low hundreds) looked
    # like a peer that left minutes ago and suppressed the rebuild.
    clock = {"now": discovery_module.time.monotonic()}
    monkeypatch.setattr(discovery_module.time, "monotonic", lambda: clock["now"])
    subject._presence_round()
    assert found, "the rig never discovered the peer"

    rebuilt: list[str] = []
    subject._restart_zeroconf = lambda reason="": rebuilt.append(reason)

    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()

    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS):
        subject._presence_round()
    assert len(rebuilt) == 1, f"expected one rebuild, got {len(rebuilt)}"

    clock["now"] += discovery_module.BROWSE_REBUILD_COOLDOWN_SECONDS + 1
    # The peer was seen a moment ago; it is this machine that cannot hear.  The
    # repair stops once the last answer is older than its memory window, which
    # is a separate rule with its own test.
    subject._last_peer_seen_at = clock["now"]
    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS):
        subject._presence_round()
    assert len(rebuilt) == 1, "the rebuild repeated at the flat cooldown"

    # The second wait is twice the first; one more cooldown on top of it passes.
    clock["now"] += discovery_module.BROWSE_REBUILD_COOLDOWN_SECONDS + 1
    subject._last_peer_seen_at = clock["now"]
    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS):
        subject._presence_round()
    assert len(rebuilt) == 2


def test_a_peer_answering_again_shortens_the_next_rebuild_wait():
    """The backoff is a failure streak, not a permanent slow-down."""
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()
    rebuilt: list[str] = []
    subject._restart_zeroconf = lambda reason="": rebuilt.append(reason)
    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()

    subject._empty_rounds = discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS
    assert subject._repair_deaf_browse() is True
    assert len(rebuilt) == 1
    assert subject._browse_rebuild_backoff > discovery_module.BROWSE_REBUILD_COOLDOWN_SECONDS

    answering.add("Kitchen-9f2a._clipsync._tcp.local.")
    subject._presence_round()

    assert subject._browse_rebuild_backoff == discovery_module.BROWSE_REBUILD_COOLDOWN_SECONDS


def test_a_network_that_emptied_is_not_rebuilt_forever():
    """``_had_peers`` never came back down; a peer's last sighting does.

    Once no peer has answered for ``PEER_MEMORY_SECONDS`` the network really has
    emptied -- there is nobody to hear, so there is nothing a rebuild could
    repair -- and the repair must stay quiet.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()

    rebuilt: list[str] = []
    subject._restart_zeroconf = lambda reason="": rebuilt.append(reason)
    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()
    with subject._lock:
        subject._last_peer_seen_at -= (
            discovery_module.PEER_MEMORY_SECONDS + 3600.0
        )

    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS * 2):
        subject._presence_round()

    assert rebuilt == [], "an empty network was rebuilt"


def test_a_browser_cache_with_no_services_is_not_rebuilt():
    """An empty cache is an empty network: there is nothing to be deaf about.

    This is the observable form of the old ``_had_peers`` flaw -- the evidence
    is "the cache still holds services while answers are zero", which is the
    exact shape the field log could not show before the state line named both
    numbers.
    """
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()

    rebuilt: list[str] = []
    subject._restart_zeroconf = lambda reason="": rebuilt.append(reason)
    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()
    subject._zc.cache = SimpleNamespace(get_all_by_details=lambda *_a: [])

    for _ in range(discovery_module.BROWSE_REBUILD_AFTER_EMPTY_ROUNDS * 2):
        subject._presence_round()

    assert rebuilt == []


def test_the_state_line_names_the_browser_cache_and_the_answers(monkeypatch, caplog):
    """The one line a user's log has to answer "deaf machine or empty network"."""
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()

    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()
    with caplog.at_level(logging.DEBUG, logger="internal.transport.discovery"):
        for _ in range(discovery_module.DISCOVERY_STATE_EVERY_ROUNDS - 1):
            subject._presence_round()

    states = [
        record.getMessage()
        for record in caplog.records
        if "Discovery state:" in record.getMessage()
    ]
    assert len(states) == 1, f"expected one state line, got {states}"
    assert "cached_services=1" in states[0], states[0]
    assert "answered_this_round=0" in states[0], states[0]


# ── 严重-4: the show/hide intent bits ──────────────────────────────────


def test_a_hidden_device_is_not_advertised_again_by_a_presence_round(monkeypatch, wire):
    """Hiding is a decision, not a lost registration.

    The presence round could only see ``_service_info is None``, which is both
    "the registration was lost" and "the user hid this device", so it put a
    hidden device back on the network within ten seconds -- while the settings
    page went on showing "hidden".
    """
    fast_addresses(monkeypatch)
    subject = a_discovery(monkeypatch)
    subject._browser = FakeBrowser()
    subject._browse_since = discovery_module.time.monotonic()
    subject._service_info = SimpleNamespace(
        name="Desktop-4c1d._clipsync._tcp.local."
    )

    subject.stop_advertising()
    assert subject.is_advertising is False
    assert subject._advertise_wanted is False

    published: list[int] = []
    subject._publish = lambda: published.append(1) or True
    for _ in range(discovery_module.DISCOVERY_STATE_EVERY_ROUNDS + 1):
        subject._presence_round()

    assert published == [], "the repair re-registered a device the user hid"
    assert subject.is_advertising is False

    # Showing it again is the user's call, and it works.
    subject.start_advertising()
    assert subject.is_advertising is True


def test_a_lost_registration_is_still_repaired_when_advertising_is_wanted(monkeypatch):
    """The control: the repair must still fire when the user asked to be seen."""
    fast_addresses(monkeypatch)
    subject = Discovery("local-device", "Desktop", 9999, "_clipsync._tcp.local.")
    subject._zc = FakeZeroconf()
    subject._browser = FakeBrowser()
    subject._netmon_stop = FakeStopEvent()
    subject._service_info = None
    assert subject._advertise_wanted is True

    published: list[int] = []

    def fake_publish():
        published.append(1)
        subject._service_info = SimpleNamespace(name="Desktop-4c1d._clipsync._tcp.local.")
        return True

    subject._publish = fake_publish
    subject._presence_round()

    assert published == [1]


def test_a_network_change_does_not_re_enable_what_the_user_turned_off(monkeypatch, wire):
    """``_restart_zeroconf`` used to call both starts unconditionally.

    A Wi-Fi switch, a VPN toggle or a wake therefore re-exposed a hidden device
    and re-opened a paused browse, with no event telling the settings page.
    """
    fast_addresses(monkeypatch)
    fresh, _addresses = wire()
    subject = a_discovery(monkeypatch)
    subject._browser = FakeBrowser()
    subject._service_info = SimpleNamespace(
        name="Desktop-4c1d._clipsync._tcp.local."
    )

    subject.stop_advertising()
    subject.stop_browsing()
    assert subject.is_advertising is False and subject.is_browsing is False

    subject._restart_zeroconf("a sleep/wake")

    assert subject.is_advertising is False, "the rebuild re-exposed a hidden device"
    assert subject.is_browsing is False, "the rebuild re-opened a paused browse"


def test_a_network_change_resumes_the_halves_that_were_wanted(monkeypatch, wire):
    """The control: a rebuild still re-registers and re-browses when asked to."""
    fast_addresses(monkeypatch)
    fresh, _addresses = wire()
    subject = a_discovery(monkeypatch)

    subject._restart_zeroconf("an address change")

    assert subject.is_advertising is True, "the rebuild left the device unadvertised"
    assert subject.is_browsing is True, "the rebuild left the browse closed"
    assert fresh, "no fresh Zeroconf instance was built"


def test_state_transitions_are_reported_to_the_callback(monkeypatch, wire):
    """``discovery.changed`` needs a source that is the truth, not the switch.

    The runtime wires this callback to publish ``discovery.changed``, so the
    settings page follows what the daemon is really doing (see the interface
    note in ``audit/fix/discovery-DONE.md``).
    """
    fast_addresses(monkeypatch)
    subject = a_discovery(monkeypatch)
    seen: list[dict] = []
    subject.set_state_callback(seen.append)

    subject.start_browsing()
    assert seen[-1] == {"enabled": True, "visible": False}

    subject.start_advertising()
    assert seen[-1] == {"enabled": True, "visible": True}

    subject.stop_advertising()
    assert seen[-1] == {"enabled": True, "visible": False}

    subject.stop_browsing()
    assert seen[-1] == {"enabled": False, "visible": False}


def test_a_raising_state_callback_does_not_break_the_round(monkeypatch, wire):
    """The callback is a notification, never a dependency."""
    fast_addresses(monkeypatch)
    subject = a_discovery(monkeypatch)

    def boom(_state):
        raise RuntimeError("event bus went away")

    subject.set_state_callback(boom)
    subject.start_browsing()  # must not raise
    assert subject.is_browsing is True


# ── 中-12: enumeration vs advertising policy ───────────────────────────


def test_a_public_numbered_lan_is_advertised_instead_of_loopback(monkeypatch):
    """A campus/enterprise LAN numbered with public addresses is still a LAN.

    Refusing everything outside RFC1918 enumerated to nothing, and the
    registration path then fell back to a ``127.0.0.1`` A record, which every
    peer discards -- so the device could list other devices and no device could
    list it.  The fallback is gone: a usable public address is advertised.
    """
    monkeypatch.setattr(discovery_module, "_primary_routable_address", lambda: None)
    monkeypatch.setattr(
        discovery_module,
        "_resolved_host_addresses",
        lambda _timeout: ["203.0.113.5"],
    )
    monkeypatch.setattr(discovery_module, "_adapter_kinds", lambda: {})
    monkeypatch.setattr(discovery_module, "_virtual_addresses", lambda: frozenset())
    monkeypatch.setattr(discovery_module, "_local_network_prefixes", lambda: {})

    assert discovery_module.get_all_local_addresses() == ["203.0.113.5"]
    assert discovery_module._get_local_address() == "203.0.113.5"

    subject = Discovery("local-device", "Desktop", 9999, "_clipsync._tcp.local.")
    subject._zc = FakeZeroconf()
    assert subject._register_service() is True
    addresses = [socket.inet_ntoa(raw) for raw in subject._zc.registered[0].addresses]
    assert addresses == ["203.0.113.5"]
    assert "127.0.0.1" not in addresses


def test_a_cgnat_address_is_enumerated_and_advertised(monkeypatch):
    """100.64/10 is not RFC1918 and is what a carrier LAN is numbered with."""
    monkeypatch.setattr(discovery_module, "_primary_routable_address", lambda: None)
    monkeypatch.setattr(
        discovery_module, "_resolved_host_addresses", lambda _timeout: ["100.64.0.5"]
    )
    monkeypatch.setattr(discovery_module, "_adapter_kinds", lambda: {})
    monkeypatch.setattr(discovery_module, "_virtual_addresses", lambda: frozenset())

    assert discovery_module.get_all_local_addresses() == ["100.64.0.5"]
    assert discovery_module._is_cgnat_ip("100.64.0.5") is True
    assert discovery_module._is_private_ip("100.64.0.5") is False


def test_a_loopback_only_machine_registers_with_no_address_record(monkeypatch, caplog):
    """Never advertise 127.0.0.1; say why the device cannot be listed instead.

    A loopback A record is worse than no record: it looks like an advertisement
    and resolves to an address every peer's filter drops, so the device is
    invisible while its log says "registered".
    """
    monkeypatch.setattr(discovery_module, "_primary_routable_address", lambda: None)
    monkeypatch.setattr(discovery_module, "_resolved_host_addresses", lambda _t: [])
    monkeypatch.setattr(discovery_module, "_adapter_kinds", lambda: {})
    monkeypatch.setattr(discovery_module, "_virtual_addresses", lambda: frozenset())
    monkeypatch.setattr(discovery_module, "_local_network_prefixes", lambda: {})

    assert discovery_module.get_all_local_addresses() == []

    subject = Discovery("local-device", "Desktop", 9999, "_clipsync._tcp.local.")
    subject._zc = FakeZeroconf()
    with caplog.at_level(logging.WARNING, logger="internal.transport.discovery"):
        assert subject._register_service() is True

    registered = subject._zc.registered[0]
    assert list(registered.addresses) == [], "the registration carried an address record"
    assert subject.address_policy()["reason"] == "loopback_only"
    assert "without an address record" in caplog.text


def test_a_public_address_is_dropped_beside_a_private_one():
    """Privacy: the public interface is the route out, not the LAN address."""
    kinds: dict[str, str] = {}
    advertise = discovery_module._advertisable
    assert advertise(["203.0.113.5", "192.168.1.5"], kinds) == ["192.168.1.5"]
    assert advertise(["203.0.113.5", "100.64.0.5"], kinds) == ["100.64.0.5"]
    # Nothing private-numbered: the public one is all there is, so it goes out.
    assert advertise(["203.0.113.5"], kinds) == ["203.0.113.5"]
    assert advertise([], kinds) == []


# ── 中-13: IPv6 records are decoded, not silently dropped ───────────────


def test_an_ipv6_only_peer_is_listed_with_its_address(monkeypatch):
    """``inet_ntoa`` raises on a 16-byte AAAA record; the peer vanished.

    The exception was swallowed, the candidate list came out empty, and the
    device was reported "no usable address" every ten seconds for as long as it
    was in range.  The record is decoded instead, and the peer is listed.
    """
    fast_addresses(monkeypatch)
    subject = service()
    subject._our_ip = "192.168.1.5"
    seen = []
    subject.set_callbacks(lambda *args: seen.append(args), lambda *_a: None)
    props = {b"device_id_hash": Discovery._hash_device_id("peer-6").encode()}
    zc, service_type, name = v6_sighting(
        props,
        [
            socket.inet_pton(socket.AF_INET6, "fe80::1"),
            socket.inet_pton(socket.AF_INET6, "2001:db8::5"),
        ],
    )

    subject._handle_service_added(zc, service_type, name)

    assert seen, "the IPv6-only peer was not reported at all"
    peer_id = Discovery._hash_device_id("peer-6")
    assert subject._known_peers[peer_id]["address"] == "2001:db8::5"
    assert seen[0][2] == "2001:db8::5"


def test_an_address_record_is_decoded_by_its_length():
    """Four bytes is IPv4, sixteen is IPv6, and anything else is not an address."""
    text = discovery_module._address_text
    assert text(socket.inet_aton("192.168.1.7")) == "192.168.1.7"
    assert text(socket.inet_pton(socket.AF_INET6, "2001:db8::5")) == "2001:db8::5"
    assert text(b"\x01\x02") is None
    assert text(None) is None


def test_a_link_local_ipv6_loses_to_a_global_one():
    """Both are dialable in principle; only one needs no scope id."""
    pick = discovery_module._pick_best_address
    assert (
        pick(["fe80::1", "2001:db8::5"], "192.168.1.5", frozenset(), 24)
        == "2001:db8::5"
    )


# ── 中-14: the browser cache, not "ever had a peer" ────────────────────


def test_the_repair_requires_a_peer_to_have_been_seen_recently():
    """The old flag was monotonic; the new evidence is a last-seen time."""
    subject, found, lost, answering = rig({"Kitchen-9f2a": "peer-2"})
    subject._presence_round()
    assert subject._last_peer_seen_at is not None

    answering.clear()
    with subject._heard_lock:
        subject._heard.clear()
    subject._restart_zeroconf = lambda reason="": None
    with subject._lock:
        subject._last_peer_seen_at -= discovery_module.PEER_MEMORY_SECONDS + 1.0

    assert subject._repair_deaf_browse() is False


# ── 中-15: the service type is a shape ─────────────────────────────────


def test_the_service_type_predicate_matches_the_shapes_it_names():
    """The same check the settings layer uses to reject a mistyped value."""
    predicate = discovery_module.is_valid_service_type
    cases = [
        ("_clipsync._tcp.local.", True),
        ("_clipsync._udp.local.", True),
        ("_http._tcp.local.", True),
        ("_printer._sub._ipp._tcp.local.", True),
        # The shapes that used to be accepted by length and then killed the LAN
        # engine: no trailing dot, no leading underscore, not a type at all.
        ("_clipsync._tcp.local", False),
        ("clipsync._tcp.local.", False),
        ("_clipsync._tcp.", False),
        ("_clipsync._tcp.example.com.", False),
        ("myclipsync", False),
        ("", False),
        (" _clipsync._tcp.local.", False),
        (None, False),
        (123, False),
    ]
    wrong = [(value, predicate(value)) for value, want in cases if predicate(value) != want]
    assert wrong == [], f"unexpected answers: {wrong}"


def test_a_service_type_without_the_trailing_dot_is_completed(monkeypatch):
    """The config validator accepts both spellings, so discovery has to as well.

    A hand-written config that predates the trailing-dot rule keeps loading; the
    alternative -- replacing the saved value with the default -- would be a
    silent change to what the user configured.
    """
    fast_addresses(monkeypatch)
    subject = Discovery("local-device", "Desktop", 9999, "_clipsync._tcp.local")

    assert subject._service_type == "_clipsync._tcp.local."


def test_an_invalid_service_type_falls_back_instead_of_failing(monkeypatch, caplog):
    """A text field must not be able to take discovery, pairing and sync down."""
    fast_addresses(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="internal.transport.discovery"):
        subject = Discovery("local-device", "Desktop", 9999, "myclipsync")

    assert subject._service_type == discovery_module.DEFAULT_SERVICE_TYPE
    assert "not a valid DNS-SD type" in caplog.text


def test_a_browser_that_refuses_the_type_is_reported_not_raised(monkeypatch, caplog):
    """Defence in depth: the library's own refusal must not reach the caller."""
    fast_addresses(monkeypatch)
    subject = a_discovery(monkeypatch)

    def refusing(*_args, **_kwargs):
        raise RuntimeError("Type 'myclipsync' must end with '._tcp.local.'")

    monkeypatch.setattr(discovery_module, "ServiceBrowser", refusing)
    with caplog.at_level(logging.ERROR, logger="internal.transport.discovery"):
        subject._open_browser()  # must not raise

    assert subject.is_browsing is False
    assert "Could not browse for mDNS service type" in caplog.text


# ── 中-16: the netmask, not a /24 guess ───────────────────────────────


def test_a_subnet_larger_than_24_picks_the_peer_on_that_subnet(monkeypatch):
    """On a /16 a peer's third octet differs and the /24 guess missed it.

    ``10.4.2.30`` and ``10.5.9.20`` were both "not on our /24" and both routed
    by the default route, so the tie was the address string and the machine
    dialled the unreachable interface every time.  With the real netmask the
    second one is on-link and ranks first.
    """
    monkeypatch.setattr(
        discovery_module, "_local_network_prefixes", lambda: {"10.5.1.10": 16}
    )
    monkeypatch.setattr(discovery_module, "_route_source", lambda _ip: "10.5.1.10")

    chosen = discovery_module._pick_best_address(["10.4.2.30", "10.5.9.20"], "10.5.1.10")

    assert chosen == "10.5.9.20"


def test_a_24_network_still_matches_itself(monkeypatch):
    """The ordinary case, pinned: the real mask must not change it."""
    monkeypatch.setattr(
        discovery_module, "_local_network_prefixes", lambda: {"192.168.31.250": 24}
    )
    monkeypatch.setattr(discovery_module, "_route_source", lambda _ip: None)

    chosen = discovery_module._pick_best_address(
        ["10.8.0.2", "192.168.31.238"], "192.168.31.250"
    )

    assert chosen == "192.168.31.238"


# ── 轻-7: the whole of a peer's identity, and the log's own words ──────


def test_a_peer_whose_platform_changed_is_reported_again(monkeypatch):
    """``os``/``arch``/``app`` are part of "changed", not only the version.

    The device list shows all three and the update offer reads them; a peer that
    re-announced after changing one was read as "nothing changed" and kept the
    old values for the life of the process.
    """
    fast_addresses(monkeypatch)
    registered = []
    subject = service()
    subject._our_ip = "192.168.1.5"
    subject.set_callbacks(lambda *args: registered.append(args), lambda *_a: None)
    hashed = Discovery._hash_device_id("peer-2").encode()

    subject._handle_service_added(
        *sighting({b"device_id_hash": hashed, b"app": b"tauri", b"os": b"darwin"})
    )
    subject._handle_service_added(
        *sighting(
            {b"device_id_hash": hashed, b"app": b"companion", b"os": b"windows"}
        )
    )

    assert len(registered) == 2, "the platform change was read as no change"
    assert registered[-1][7] == "companion"
    assert subject._known_peers[Discovery._hash_device_id("peer-2")]["app"] == "companion"
    assert subject._known_peers[Discovery._hash_device_id("peer-2")]["os"] == "windows"


def test_tunnel_addresses_are_not_broadcast_while_a_wire_exists():
    """The rule, stated: a wire wins, a tunnel is only a last resort.

    A peer that dials a tunnel address dials a black hole and lists a device it
    cannot talk to.  The deliberate cost is that a peer reachable only through
    the same VPN is not discovered by mDNS; a machine whose only address *is*
    the tunnel still advertises it, because the alternative is nothing.
    """
    kinds = {"192.168.1.5": "wifi", "10.8.0.7": "virtual"}
    assert discovery_module._advertisable(
        ["10.8.0.7", "192.168.1.5"], kinds
    ) == ["192.168.1.5"]
    assert discovery_module._advertisable(["10.8.0.7"], kinds) == ["10.8.0.7"]


def test_the_rebuild_log_names_its_own_reason(monkeypatch, caplog):
    """A browse repair that logs "after a network change" sends the reader away."""
    fast_addresses(monkeypatch)
    subject = a_discovery(monkeypatch)
    with caplog.at_level(logging.INFO, logger="internal.transport.discovery"):
        subject._restart_zeroconf("a browse that stopped hearing answers")

    lines = [record.getMessage() for record in caplog.records]
    assert any(
        "Rebuilt mDNS sockets after a browse that stopped hearing answers" in line
        for line in lines
    ), lines
    assert not any("after a network change" in line for line in lines), lines
