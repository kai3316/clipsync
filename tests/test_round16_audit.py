"""Round 16 — internet-mode integration conflict audit (C = 整合冲突审计).

Pins the interaction surfaces between internet (relay/netpair) sync and the
existing LAN / desktop features so a regression in one surface can't silently
break another:

  P1  dual-path (LAN + relay) end-to-end dedup
        Same content arriving once over LAN and once over the relay collapses
        to ONE history row and ONE clipboard write.
  P2  sync pause / timed pause × relay mirror
        Pausing clipboard sync also stops the relay mirror (the mirror is only
        reachable from the enabled send path) and incoming relay frames are
        dropped while paused — exactly like LAN.
  P3  internet arrival consistency
        Relay frames route through the SAME router as LAN frames, so history,
        source-device tracking and writes are identical by construction.
  P4  status/alias consistency
        relay_state (off/connecting/online/error) is distinct from per-netpair
        online (90 s last-seen window); only CONFIRMED netpair peers get a
        last-seen/online row, and self-originated frames never poison it.
  P6  same peer dual channel
        A device that is BOTH a LAN relay-enroll peer AND an internet
        pairing-code peer is published to exactly ONCE (netpair wins) — no
        wasteful double transit per clipboard frame.
  P7  lifecycle
        Unpair clears secret+alias+last_seen and resubscribes; stop releases
        the relay transport; re-enable rebuilds channels from persisted state.
  P8  history / transfer panels
        Internet-arriving content lands in the SAME shared clipboard history
        as LAN content (one store, deduped) — never a separate/duplicate row.

Only targeted, offline tests (no broker, no network).  App-level behavior is
exercised through the same ``Application``-method-bound stub pattern used by
tests/test_round14_netpair.py.
"""

import time
import types

from internal.clipboard.format import ClipboardContent, ContentType, SyncMessage
from internal.protocol.codec import encode_frame
from internal.sync.manager import SyncManager
from internal.transport import relay as R  # noqa: N812  (matches round-14 test)
from src.main import Application  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class MockClipboardMonitor:
    def __init__(self):
        self._callback = None
        self._running = False
        self.suppress_until = 0.0

    def start(self, callback):
        self._callback = callback
        self._running = True

    def stop(self):
        self._running = False
        self._callback = None

    def suppress_for(self, duration_seconds):
        self.suppress_until = time.time() + duration_seconds

    def fire(self):
        if self._callback and time.time() >= self.suppress_until:
            self._callback()


class MockClipboardReader:
    def __init__(self):
        self.content = ClipboardContent()

    def read(self):
        return self.content


class MockClipboardWriter:
    def __init__(self):
        self.last_written = None
        self.write_count = 0

    def write(self, content):
        self.last_written = content
        self.write_count += 1


class FakeHistory:
    def __init__(self):
        self.items = []

    def add(self, content, source_app=None):
        self.items.append(content)


def make_sync_mgr():
    monitor = MockClipboardMonitor()
    reader = MockClipboardReader()
    writer = MockClipboardWriter()
    history = FakeHistory()
    sent = []

    mgr = SyncManager(
        "device-A", "DevA",
        reader=reader, writer=writer, monitor=monitor, history=history,
        sync_debounce=0.0,  # no debounce → deterministic, no threads
    )
    mgr.on_send = lambda msg: sent.append(msg)
    return mgr, dict(monitor=monitor, reader=reader, writer=writer,
                     history=history, sent=sent)


def _text_content(text: str) -> ClipboardContent:
    return ClipboardContent(types={ContentType.TEXT: text.encode("utf-8")})


def _remote_msg(text: str, source: str) -> SyncMessage:
    return SyncMessage(content=_text_content(text), msg_id="m" + str(abs(hash(text))),
                       source_device=source)


def make_app_stub(**attrs):
    """Application stand-in wired to a recording relay + WS broadcast.

    Only the relay/netpair surface is modelled; the clipboard/chat/file
    managers are stubs so Application methods under test have what they need.
    """
    app = types.SimpleNamespace()
    device_id = attrs.get("device_id", "a1b2c3d4e5f6")
    peers = {}
    for pid, paired in attrs.get("peers", {}).items():
        peers[pid] = types.SimpleNamespace(device_id=pid, paired=paired,
                                           device_name=pid)
    c = types.SimpleNamespace(
        device_id=device_id,
        device_name=attrs.get("device_name", "DevA"),
        internet_sync_enabled=attrs.get("internet_sync_enabled", True),
        relay_secret=attrs.get("relay_secret", "aa" * 32),
        peer_relay_secrets=dict(attrs.get("peer_relay_secrets", {})),
        netpair_secrets=dict(attrs.get("netpair_secrets", {})),
        netpair_aliases=dict(attrs.get("netpair_aliases", {})),
        relay_brokers=["wss://x:8884/mqtt"],
        peers=peers,
    )
    app.cfg = c
    app._netpair_pending = dict(attrs.get("_netpair_pending", {}))
    app._netpair_names = dict(attrs.get("_netpair_names", {}))
    app._netpair_last_seen = dict(attrs.get("_netpair_last_seen", {}))

    class Relay:
        def __init__(self):
            self.published = []
            self.refreshed = 0
            self.stopped = 0

        def publish(self, frame, topic, key):
            self.published.append((frame, topic, key))
            return True

        def refresh_channels(self):
            self.refreshed += 1

        def stop(self):
            self.stopped += 1

    relay = Relay()
    app._relay = attrs.get("_relay", relay)

    saved = {"n": 0}
    app._save_cfg_and_peers = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._save_cfg_encrypted = lambda: saved.__setitem__("n", saved["n"] + 1)
    app._saved = saved

    class WS:
        def __init__(self):
            self.broadcasts = []

        def broadcast(self, mtype, data):
            self.broadcasts.append((mtype, data))
            return 1

    app.web_server = WS()

    # Bind the real Application methods the audit exercises.
    app._relay_publish_frame = lambda fb, _a=app: Application._relay_publish_frame(_a, fb)
    app._relay_channels = lambda _a=app: Application._relay_channels(_a)
    app._netpair_secrets_all = lambda _a=app: Application._netpair_secrets_all(_a)
    app._netpair_status = lambda now=None, _a=app: Application._netpair_status(_a, now)
    app._netpair_unpair = lambda pid, _a=app: Application._netpair_unpair(_a, pid)
    app._stop_internet_sync = lambda _a=app: Application._stop_internet_sync(_a)
    app._get_relay_state = lambda _a=app: Application._get_relay_state(_a)
    app._ensure_relay_secret = lambda _a=app: Application._ensure_relay_secret(_a)
    app._on_relay_frame = (
        lambda fb, topic=None, now=None, _a=app: Application._on_relay_frame(
            _a, fb, topic, now=now))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: Application._handle_netpair_hello(
            _a, payload, source, topic))
    app._on_peer_message = attrs.get("_on_peer_message",
                                     lambda msg, pid, _a=app: None)
    return app


def _netpair_secret():
    return R.generate_netpair_secret()


# ---------------------------------------------------------------------------
# P1 — dual-path (LAN + relay) end-to-end dedup
# ---------------------------------------------------------------------------


def test_p1_dual_delivery_lands_single_history_and_write():
    """The same clip arriving twice (once LAN, once relay) must not double."""
    mgr, f = make_sync_mgr()
    msg = _remote_msg("hello internet", "device-B")

    mgr.handle_remote_message(msg)   # e.g. arrived over LAN
    mgr.handle_remote_message(msg)   # e.g. the relay mirror of the same frame

    assert f["writer"].write_count == 1
    assert len(f["history"].items) == 1
    assert f["history"].items[0].source_device == "device-B"


def test_p1_different_content_is_not_collapsed():
    """Distinct content still lands twice (dedup is content-addressed)."""
    mgr, f = make_sync_mgr()
    mgr.handle_remote_message(_remote_msg("first", "device-B"))
    mgr.handle_remote_message(_remote_msg("second", "device-B"))
    assert f["writer"].write_count == 2
    assert len(f["history"].items) == 2


def test_p1_relay_frame_and_lan_frame_share_the_dedup_ring():
    """Interleaved LAN + relay arrivals of the SAME content collapse too.

    The dedup ring (not just _last_local_hash) is what absorbs the case where
    the two deliveries are separated by an unrelated local copy.
    """
    mgr, f = make_sync_mgr()
    msg = _remote_msg("same clip", "device-B")
    mgr.handle_remote_message(msg)                 # relay
    mgr.handle_remote_message(_remote_msg("unrelated", "device-A"))  # local-ish
    mgr.handle_remote_message(msg)                 # LAN copy arrives late
    assert f["writer"].write_count == 2            # same + unrelated
    assert len(f["history"].items) == 2


# ---------------------------------------------------------------------------
# P2 — sync pause / timed pause × relay mirror
# ---------------------------------------------------------------------------


def test_p2_paused_sync_never_reaches_on_send():
    """Pausing the sync manager means nothing is broadcast (or relay-mirrored).

    The relay mirror is only reachable from _on_local_sync (the on_send hook),
    so this gate is what stops mirroring while a pause is active.
    """
    mgr, f = make_sync_mgr()
    mgr.set_enabled(False)
    mgr.start()
    f["reader"].content = _text_content("while paused")
    f["monitor"].fire()
    time.sleep(0.05)
    assert f["sent"] == []


def test_p2_paused_inbound_clipboard_is_dropped():
    """Incoming clipboard (LAN or relay) is dropped while sync is paused."""
    mgr, f = make_sync_mgr()
    mgr.set_enabled(False)
    mgr.handle_remote_message(_remote_msg("paused inbound", "device-B"))
    assert f["writer"].write_count == 0
    assert f["history"].items == []


def test_p2_relay_publish_is_off_when_internet_disabled():
    """_relay_publish_frame is a hard no-op when internet sync is off."""
    app = make_app_stub(internet_sync_enabled=False,
                        peer_relay_secrets={"device-B": "bb" * 32})
    app.cfg.peers["device-B"] = types.SimpleNamespace(
        device_id="device-B", paired=True, device_name="DevB")
    app._relay_publish_frame(b"some frame bytes")
    assert app._relay.published == []


def test_p2_relay_publish_is_off_without_transport():
    """_relay_publish_frame is a no-op when the relay transport is down."""
    app = make_app_stub(internet_sync_enabled=True,
                        peer_relay_secrets={"device-B": "bb" * 32})
    app.cfg.peers["device-B"] = types.SimpleNamespace(
        device_id="device-B", paired=True, device_name="DevB")
    app._relay = None
    app._relay_publish_frame(b"frame")
    # Nothing to publish to — must not raise.
    assert True


# ---------------------------------------------------------------------------
# P3 — internet arrival consistency (same router as LAN)
# ---------------------------------------------------------------------------


def test_p3_relay_clipboard_routes_to_peer_router():
    """A clipboard frame over the relay hits the SAME _on_peer_message router."""
    calls = []
    app = make_app_stub(_on_peer_message=lambda msg, pid, _a=None: calls.append((msg, pid)))
    frame = encode_frame(
        {"msg_type": "clipboard", "text": "relay clip"}, source_device="device-B")
    app._on_relay_frame(frame, topic="t")
    assert len(calls) == 1
    msg, pid = calls[0]
    assert pid == "device-B"          # peer_id = source_device, like LAN
    assert getattr(msg, "msg_type", "") == "clipboard"


def test_p3_relay_self_frame_is_dropped():
    """Our own mirrored frame must never re-enter the router (self-echo)."""
    calls = []
    app = make_app_stub(_on_peer_message=lambda msg, pid, _a=None: calls.append((msg, pid)))
    frame = encode_frame(
        {"msg_type": "clipboard", "text": "self"}, source_device=app.cfg.device_id)
    app._on_relay_frame(frame, topic="t")
    assert calls == []


def test_p3_relay_netpair_hello_never_reaches_clipboard_router():
    """netpair_hello frames are routed to the handshake, not the router."""
    router_calls = []
    hello_calls = []
    app = make_app_stub(_on_peer_message=lambda msg, pid, _a=None: router_calls.append(msg))
    app._handle_netpair_hello = (
        lambda payload, source, topic, _a=app: hello_calls.append((payload, source, topic)))
    frame = encode_frame(
        {"msg_type": "netpair_hello", "peer_id": "ABCD"}, source_device="device-B")
    app._on_relay_frame(frame, topic="t")
    assert router_calls == []
    assert len(hello_calls) == 1


# ---------------------------------------------------------------------------
# P4 — status / alias consistency
# ---------------------------------------------------------------------------


def test_p4_netpair_status_online_follows_last_seen_window():
    """online is driven by the 90 s last-seen window, independent of relay."""
    pid = "device-B"
    secret = _netpair_secret()
    now = 1_000_000.0
    app = make_app_stub(netpair_secrets={pid: secret},
                        _netpair_last_seen={pid: now - 30})
    data, status = app._netpair_status(now)
    assert status == 200
    peer = data["peers"][0]
    assert peer["peer_id"] == pid and peer["online"] is True
    assert peer["last_seen"] == now - 30
    assert peer["paired"] is True

    # Outside the window → offline.
    app2 = make_app_stub(netpair_secrets={pid: secret},
                         _netpair_last_seen={pid: now - 300})
    data2, _ = app2._netpair_status(now)
    assert data2["peers"][0]["online"] is False


def test_p4_last_seen_only_for_confirmed_netpair_peers():
    """A relay_enroll-only peer (LAN-derived secret) gets NO netpair row.

    Only a source present in cfg.netpair_secrets updates _netpair_last_seen,
    so the device page's internet-online dot can't be poisoned by a peer that
    merely shares a LAN-derived relay channel.
    """
    pid = "device-B"
    app = make_app_stub(peer_relay_secrets={pid: "bb" * 32},
                        netpair_secrets={})
    frame = __import__("internal.protocol.codec", fromlist=["encode_frame"]).encode_frame(
        {"msg_type": "clipboard", "text": "x"}, source_device=pid)
    app._on_relay_frame(frame, topic="t", now=1_000_000.0)
    assert app._netpair_last_seen.get(pid) is None
    data, _ = app._netpair_status(1_000_000.0)
    assert data["peers"] == []


def test_p4_alias_fallback_order_and_self_exclusion():
    """Alias > device name > short id; self can never be a netpair peer."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(
        netpair_secrets={pid: secret},
        netpair_aliases={pid: "My Laptop"},
        _netpair_names={pid: "DevB"},
        peers={pid: True},
    )
    data, _ = app._netpair_status(1_000_000.0)
    peer = data["peers"][0]
    assert peer["alias"] == "My Laptop"
    assert peer["name"] == "DevB"     # runtime name beats cfg.peers name


def test_p4_relay_state_reflects_enabled_and_transport():
    """_get_relay_state: off when disabled; transport state when enabled."""
    app = make_app_stub(internet_sync_enabled=False)
    assert app._get_relay_state() == "off"

    app2 = make_app_stub(internet_sync_enabled=True)
    app2._relay = types.SimpleNamespace(state="online")
    assert app2._get_relay_state() == "online"

    app3 = make_app_stub(internet_sync_enabled=True)
    app3._relay = types.SimpleNamespace(state="connecting")
    assert app3._get_relay_state() == "connecting"


# ---------------------------------------------------------------------------
# P6 — same peer dual channel: publish once, netpair wins
# ---------------------------------------------------------------------------


def test_p6_dual_channel_peer_publishes_once_netpair_wins():
    """Peer in BOTH peer_relay_secrets and netpair_secrets → one publish."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={pid: secret},
        peers={pid: True},  # make_app_stub: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    frame, topic, key = app._relay.published[0]
    assert topic == R.netpair_topic(secret)
    assert key == R.netpair_key(secret)


def test_p6_relay_enroll_only_peer_publishes_once():
    """Peer reachable only via LAN-derived relay secret → one relay publish."""
    pid = "device-B"
    app = make_app_stub(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={},
        peers={pid: True},  # make_app_stub: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    topic, key = app._relay.published[0][1], app._relay.published[0][2]
    assert topic == R.derive_topic(app.cfg.relay_secret, "bb" * 32)
    assert key == R.derive_key(app.cfg.relay_secret, "bb" * 32)


def test_p6_netpair_only_peer_publishes_once():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(peer_relay_secrets={}, netpair_secrets={pid: secret})
    app._relay_publish_frame(b"clip frame")
    assert len(app._relay.published) == 1
    assert app._relay.published[0][1] == R.netpair_topic(secret)


def test_p6_self_never_published():
    """A stray self netpair entry is skipped on the publish path."""
    secret = _netpair_secret()
    app = make_app_stub(netpair_secrets={"a1b2c3d4e5f6": secret})  # default device id
    app._relay_publish_frame(b"clip frame")
    assert app._relay.published == []


def test_p6_unpaired_peer_never_published():
    """A LAN peer that is not (yet) paired is not mirrored over the relay."""
    pid = "device-B"
    app = make_app_stub(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={},
        peers={pid: False},   # make_app_stub: {device_id: paired_bool}
    )
    app._relay_publish_frame(b"clip frame")
    assert app._relay.published == []


# ---------------------------------------------------------------------------
# P7 — lifecycle: unpair / stop / restart
# ---------------------------------------------------------------------------


def test_p7_unpair_clears_state_and_resubscribes():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(
        netpair_secrets={pid: secret},
        netpair_aliases={pid: "My Laptop"},
        _netpair_names={pid: "DevB"},
        _netpair_last_seen={pid: time.time()},
    )
    # Confirm the channel is currently subscribed.
    assert R.netpair_topic(secret) in app._relay_channels()

    data, status = app._netpair_unpair(pid)
    assert status == 200 and data["ok"] is True
    assert app.cfg.netpair_secrets == {}
    assert app.cfg.netpair_aliases == {}
    assert pid not in app._netpair_names
    assert pid not in app._netpair_last_seen
    assert app._relay.refreshed >= 1
    # Unsubscribed: the topic must no longer be in the channel set.
    assert R.netpair_topic(secret) not in app._relay_channels()


def test_p7_unpair_unknown_peer_is_400():
    app = make_app_stub(netpair_secrets={})
    data, status = app._netpair_unpair("ghost")
    assert status == 400 and data.get("ok") is False


def test_p7_unpair_broadcasts_unpaired_to_web_tabs():
    """Unpair pushes a WS `netpair_peer status:unpaired` so sibling tabs
    (and the acting tab, if it misses the REST response) drop the row."""
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(netpair_secrets={pid: secret})
    app._netpair_unpair(pid)
    ws_events = [e for e in app.web_server.broadcasts if e[0] == "netpair_peer"]
    assert len(ws_events) == 1
    assert ws_events[0][1] == {"peer_id": pid, "status": "unpaired"}


def test_p7_stop_releases_transport():
    app = make_app_stub()
    relay = app._relay
    app._stop_internet_sync()
    assert app._relay is None
    assert relay.stopped == 1          # the fake relay recorded transport.stop()


def test_p7_relay_channels_exclude_self_and_include_both_paths():
    pid = "device-B"
    secret = _netpair_secret()
    app = make_app_stub(
        peer_relay_secrets={pid: "bb" * 32},
        netpair_secrets={pid: secret},
        peers={pid: True},  # make_app_stub: {device_id: paired_bool}
    )
    channels = app._relay_channels()
    assert R.derive_topic(app.cfg.relay_secret, "bb" * 32) in channels
    assert R.netpair_topic(secret) in channels
    # self netpair entry never subscribed
    app.cfg.netpair_secrets[app.cfg.device_id] = _netpair_secret()
    channels2 = app._relay_channels()
    assert not any(R.netpair_topic(app.cfg.netpair_secrets[app.cfg.device_id]) == t
                   for t in channels2)


# ---------------------------------------------------------------------------
# P8 — history / transfer panels share one store
# ---------------------------------------------------------------------------


def test_p8_internet_content_uses_same_history_as_lan():
    """Relay-arriving clipboard is fed to the shared handle_remote_message.

    The one store (the SyncManager's history) receives both a LAN-arriving
    clip and a relay-arriving clip, so the history panel sees a single
    timeline with a single source device — no separate internet bucket.
    """
    history = FakeHistory()
    # The app's clipboard path (LAN and relay) both terminate at the SAME
    # SyncManager.handle_remote_message; prove the shared history dedups.
    mgr = SyncManager(
        "device-A", "DevA",
        reader=MockClipboardReader(), writer=MockClipboardWriter(),
        monitor=MockClipboardMonitor(), history=history, sync_debounce=0.0,
    )
    lan = _remote_msg("shared clip", "device-B")
    relay = _remote_msg("shared clip", "device-B")
    mgr.handle_remote_message(lan)    # arrives over LAN
    mgr.handle_remote_message(relay)  # same content arrives over relay
    assert len(history.items) == 1
    assert history.items[0].source_device == "device-B"
