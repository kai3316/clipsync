"""Device-list backend: the remove/restore archive, the live-view snapshot and
the hashed-vs-real id dedup.

The front end's markup, class-name and locale-key assertions that used to live
here were dropped — a template's internals are not a contract.  What remains is
the backend: every removal is archived and can be restored without re-pairing,
the snapshot is a live view of the network rather than a config dump, and one
physical device is one row even when it is discovered under its hashed id.
"""

import threading
import time


class _StubPairingMgr:
    """Minimal pairing manager: records removals, restores forget them."""

    def __init__(self):
        self.removed = []
        self.rejected = []
        # paired flag the last restore_peer() was called with — restore must
        # never re-pair (see test_restore_does_not_repair_device).
        self.restored_paired = None

    def remove_peer(self, peer_id):
        self.removed.append(peer_id)

    def reject_pairing(self, peer_id):
        self.rejected.append(peer_id)

    def restore_peer(self, peer_id, name, paired=False):
        self.restored_paired = paired
        if peer_id in self.removed:
            self.removed.remove(peer_id)

    def get_known_peers(self):
        return []


class _StubTransportMgr:
    def __init__(self):
        self.disconnected = []
        self.rejected = []
        self.allowed = []
        self.connect_calls = []

    def disconnect_peer(self, peer_id, reject=False):
        self.disconnected.append(peer_id)
        if reject:
            self.rejected.append(peer_id)

    # _on_remove now calls forget_peer (reject) rather than disconnect_peer so
    # a forgotten device can't immediately reconnect — record it the same way.
    def forget_peer(self, peer_id):
        self.disconnected.append(peer_id)

    # Restore lifts the forget-time rejection so the peer can also initiate
    # the pairing from its own side.
    def allow_peer(self, peer_id):
        self.allowed.append(peer_id)

    def connect_to_peer(self, *a, **k):
        self.connect_calls.append((a, k))

    def get_peer_addresses(self):
        return {}

    # _on_connect walks these two before giving up; empty means "nowhere to
    # dial", which is exactly the unreachable path under test.
    def get_resolved_hashes(self):
        return {}

    def get_saved_address(self, peer_id):
        return None


def _remove_app(monkeypatch):
    """Application via __new__ for the remove/restore/purge backend."""
    from internal.config.config import Config
    from src.main import Application

    app = Application.__new__(Application)
    cfg = Config()
    cfg.peers = {}
    cfg.removed_peers = {}
    app.cfg = cfg
    app.pairing_mgr = _StubPairingMgr()
    app.transport_mgr = _StubTransportMgr()
    app.chat_mgr = None
    app._discovered_lock = threading.Lock()
    app._discovered_peers = {}
    app._push_web = lambda *a, **k: None
    monkeypatch.setattr(app, "_save_cfg_encrypted", lambda: None)
    return app


def test_on_remove_archives_paired_peer_for_restore(monkeypatch):
    """A paired peer being removed lands in removed_peers (not just cfg.peers
    being dropped) so the Removed section can offer a Restore action."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["peer-1"] = PeerInfo(
        device_id="peer-1",
        device_name="Old Mac",
        public_key_pem="pem",
        paired=True,
        notes="the old one",
        last_ip="192.168.1.20",
        last_port=19990,
    )

    app._on_remove("peer-1")

    assert "peer-1" not in app.cfg.peers
    assert app.pairing_mgr.removed == ["peer-1"]
    assert app.transport_mgr.disconnected == ["peer-1"]
    archived = app.cfg.removed_peers.get("peer-1")
    assert archived is not None, "removed peer must be archived"
    assert archived.device_name == "Old Mac"
    assert archived.paired is True
    assert archived.notes == "the old one"
    assert archived.last_ip == "192.168.1.20"
    assert archived.removed_at > 0

    # The archive is the universal recovery path: restore brings it back.
    assert app._on_restore_remove("peer-1") is True
    assert "peer-1" in app.cfg.peers
    assert "peer-1" not in app.cfg.removed_peers
    assert app.cfg.peers["peer-1"].device_name == "Old Mac"
    assert app.pairing_mgr.removed == []


def test_on_remove_archives_discovered_only_peer(monkeypatch):
    """A bare discovered advertisement (never paired, no cfg.peers row) must
    still be archived — the '点移除后无法找回' bug was that it vanished with no
    trace.  Discovery rows are keyed by the HASHED mDNS id, so the archive
    lookup has to try that form too."""
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    hashed = Discovery._hash_device_id("peer-9")
    app._discovered_peers[hashed] = {
        "name": "Living-room PC",
        "address": "192.168.1.5",
        "port": 19990,
    }

    app._on_remove("peer-9")

    archived = app.cfg.removed_peers.get("peer-9")
    assert archived is not None
    assert archived.device_name == "Living-room PC"
    assert archived.paired is False
    assert archived.last_ip == "192.168.1.5"
    assert archived.removed_at > 0
    assert "peer-9" not in app.cfg.peers
    # …and the hashed discovery row is gone so it does not reappear live.
    assert hashed not in app._discovered_peers

    assert app._on_restore_remove("peer-9") is True
    assert app.cfg.peers["peer-9"].device_name == "Living-room PC"


def test_on_remove_archives_temp_chat_peer(monkeypatch):
    """A peer that only exists as a nearby-chat session (no discovery row, no
    cfg.peers entry) is archived under its session name."""
    from types import SimpleNamespace

    app = _remove_app(monkeypatch)
    app.chat_mgr = SimpleNamespace(
        get_sessions=lambda: [{"peer_id": "peer-7", "peer_name": "ChatBuddy"}],
    )

    app._on_remove("peer-7")

    archived = app.cfg.removed_peers.get("peer-7")
    assert archived is not None
    assert archived.device_name == "ChatBuddy"
    assert archived.paired is False
    assert archived.removed_at > 0


def test_save_cfg_and_peers_prunes_restored_archive_rows(monkeypatch):
    """Once a peer is known again (re-paired / restored), its removed_peers
    row must be dropped — otherwise it lingers in 已移除设备 next to its live
    card.  Discovered-only forgets archive under the hashed mDNS id, so the
    prune must match that form too."""
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    # A stale archive row for a now-known peer…
    from internal.config.config import PeerInfo

    app.cfg.removed_peers["peer-1"] = PeerInfo(
        device_id="peer-1", device_name="Old Mac", removed_at=time.time()
    )
    app.cfg.removed_peers["peer-2"] = PeerInfo(
        device_id="peer-2", device_name="Other", removed_at=time.time()
    )
    # …keyed by the real id in one case and the hashed mDNS id in the other.
    hashed = Discovery._hash_device_id("peer-3")
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed, device_name="Hashed", removed_at=time.time()
    )

    app.pairing_mgr = _StubPairingMgr()
    known = []
    for pid, name in (("peer-1", "Old Mac"), ("peer-3", "New Name")):
        from types import SimpleNamespace

        known.append(
            SimpleNamespace(device_id=pid, device_name=name, certificate_pem="", paired=True)
        )
    app.pairing_mgr.get_known_peers = lambda: known

    app._save_cfg_and_peers()

    assert "peer-1" not in app.cfg.removed_peers
    assert hashed not in app.cfg.removed_peers
    assert "peer-2" in app.cfg.removed_peers  # still gone → row stays
    assert "peer-3" in app.cfg.peers
    assert app.cfg.peers["peer-1"].device_name == "Old Mac"


def test_restore_does_not_repair_device(monkeypatch):
    """Restoring from the archive must bring the device back UNPAIRED.

    _on_remove sends `pairing_unpair` to the peer, so the peer has already
    dropped its side of the trust.  Replaying the archived paired=True flag
    re-created one-sided trust the user never consented to ("设备移除再恢复会
    自动配对"), and the follow-up auto-connect fired an unsolicited pairing
    request at the peer.
    """
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["peer-1"] = PeerInfo(
        device_id="peer-1",
        device_name="Old Mac",
        public_key_pem="pem",
        paired=True,
        notes="alias",
        last_ip="192.168.1.20",
        last_port=19990,
    )
    app._on_remove("peer-1")
    assert app.cfg.removed_peers["peer-1"].paired is True  # archive is faithful

    assert app._on_restore_remove("peer-1") is True

    restored = app.cfg.peers["peer-1"]
    assert restored.paired is False, "restore must not re-pair the device"
    assert app.pairing_mgr.restored_paired is False
    # No unsolicited connection: connecting while unpaired is just a pairing
    # request the user did not ask for.
    assert app.transport_mgr.connect_calls == []
    # …but the address survives so the card's Pair button has a target
    # (_on_connect falls back to cfg.peers[...].last_ip).
    assert restored.last_ip == "192.168.1.20"
    assert restored.last_port == 19990
    assert restored.notes == "alias"
    # Archived rows carry a removal timestamp; an active row must not.
    assert restored.removed_at == 0
    # The forget-time rejection is lifted so the peer can start the pairing
    # from its own side instead of being silently refused.
    assert app.transport_mgr.allowed == ["peer-1"]


def test_reject_keeps_device_in_list(monkeypatch):
    """Rejecting a pairing request must not erase the device.

    reject_pairing only clears the pending request and disconnect_peer only
    tears the socket down — neither drops the peer, which survives at
    paired=False/connected=False.  cfg.peers (what the snapshot reads) only
    syncs from pairing_mgr in _save_cfg_and_peers, and the reject path had no
    sync point, so the card vanished ("配对点拒绝设备就会消失在列表里").
    """
    from types import SimpleNamespace

    app = _remove_app(monkeypatch)
    app._send_pairing_msg = lambda *a, **k: None
    # The TLS handshake already registered the requesting peer as
    # known-but-unpaired (connection.py add_peer(..., paired=was_paired)).
    app.pairing_mgr.get_known_peers = lambda: [
        SimpleNamespace(
            device_id="peer-5",
            device_name="Someones Laptop",
            certificate_pem="pem",
            paired=False,
        )
    ]

    assert app._handle_web_device_action("reject", "peer-5") is True

    assert app.pairing_mgr.rejected == ["peer-5"]
    assert app.transport_mgr.rejected == ["peer-5"]
    # The peer is persisted as known-but-unpaired rather than dropped…
    assert "peer-5" in app.cfg.peers
    assert app.cfg.peers["peer-5"].paired is False
    # …and therefore survives in the device snapshot WHILE ON THE NETWORK.
    # Under the live-view policy a rejected peer that is not currently
    # advertising on mDNS is hidden (it will reappear when it broadcasts
    # again).  The test must provide a discovered map, or the snapshot
    # drops the row — matching the "no cached devices" rule.
    from internal.web.api import devices as devices_api

    result, status = devices_api.get_devices(
        app.cfg,
        lambda: [],
        get_discovered=lambda: {"peer-5": {"name": "Someones Laptop"}},
    )
    assert status == 200
    ids = {d["device_id"] for d in result["devices"]}
    assert "peer-5" in ids, "must be listed while on the network"
    # Without the discovered map the row is correctly hidden.
    result2, _ = devices_api.get_devices(app.cfg, lambda: [])
    ids2 = {d["device_id"] for d in result2["devices"]}
    assert "peer-5" not in ids2, "cached offline row must be hidden"


def test_get_devices_lists_known_unpaired_peer():
    """The device page is a LIVE view of the network, not a config dump.

    A paired peer is always listed (it is a trust relationship, not cache).
    An unpaired peer is listed only while it is actually on the network —
    connected, or advertising on mDNS right now.  A row that is none of those
    is stale cache and must not be shown ("不要显示缓存的设备").
    """
    from internal.config.config import Config, PeerInfo
    from internal.transport.discovery import Discovery
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {
        "peer-1": PeerInfo(device_id="peer-1", device_name="Paired Box", paired=True),
        "peer-2": PeerInfo(device_id="peer-2", device_name="Live Unpaired", paired=False),
        "peer-3": PeerInfo(device_id="peer-3", device_name="Cached Ghost", paired=False),
    }
    cfg.removed_peers = {}

    # peer-2 is advertising: discovery keys it by the HASHED id, so the
    # snapshot has to hash each known id to recognise its own peer on the wire.
    result, status = devices_api.get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {
            Discovery._hash_device_id("peer-2"): {"name": "Live Unpaired"},
        },
    )
    assert status == 200
    by_id = {d["device_id"]: d for d in result["devices"]}

    assert "peer-1" in by_id, "a paired peer is always listed, online or not"
    assert by_id["peer-1"]["connected"] is False
    assert by_id["peer-1"]["known"] is True

    assert "peer-2" in by_id, "an unpaired peer on the network stays listed"
    assert by_id["peer-2"]["paired"] is False
    assert by_id["peer-2"]["connected"] is False
    assert by_id["peer-2"]["known"] is True
    # It is listed ONCE — as the known peer, not also as a bare sighting under
    # its hashed id.
    assert Discovery._hash_device_id("peer-2") not in by_id

    assert "peer-3" not in by_id, "cached offline unpaired row must be hidden"
    assert by_id[cfg.device_id]["known"] is True


def test_get_devices_marks_discovered_peers_not_known():
    """A bare mDNS sighting is genuinely 'Discovered' — known=False keeps it
    distinguishable from the known-but-unpaired rows above."""
    from internal.config.config import Config
    from internal.web.api import devices as devices_api

    cfg = Config()
    cfg.peers = {}
    cfg.removed_peers = {}

    result, _ = devices_api.get_devices(
        cfg,
        lambda: [],
        get_discovered=lambda: {"hash-abc": {"name": "Kitchen Pi"}},
    )
    by_id = {d["device_id"]: d for d in result["devices"]}
    assert by_id["hash-abc"]["known"] is False
    assert by_id["hash-abc"]["paired"] is False


def test_on_connect_pushes_unreachable_when_no_address(monkeypatch):
    """A known-but-unpaired peer that is not on mDNS and has no saved address
    has nowhere to connect.  The route only answers {ok:false} — which the card
    renders as a bare "操作失败" — so _on_connect pushes an explanatory event,
    mirroring the connect_rejected push."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    pushed = []
    app._push_web = lambda *a, **k: pushed.append((a, k))
    app.cfg.peers["peer-9"] = PeerInfo(
        device_id="peer-9",
        device_name="Ghost PC",
        paired=False,
        last_ip="",
    )

    assert app._on_connect("peer-9") is False
    assert app.transport_mgr.connect_calls == [], "nothing to dial"
    assert pushed, "the failure must be explained over the WS"
    args, _ = pushed[-1]
    assert args[0] == "broadcast"
    assert args[1] == "connect_unreachable"
    assert args[2]["peer_id"] == "peer-9"
    # The name makes the toast readable ("找不到 Ghost PC"), not a hex id.
    assert args[2]["name"] == "Ghost PC"


def test_real_peer_id_resolves_hashed_form(monkeypatch):
    """A hashed mDNS id resolves back to the real device_id we already know.

    mDNS advertises hash(device_id), so a discovered-only card carries the
    hash.  Persisting that hash keys a row nothing can ever fold into the real
    one (the hash is one-way) — the same PC then shows twice, once under its
    broadcast name and once under the name from its certificate.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    assert hashed != real
    app.cfg.peers[real] = PeerInfo(device_id=real, device_name="USER-2024")

    assert app._real_peer_id(hashed) == real
    # A real id is returned untouched, and an unknown hash stays as-is (the
    # discovered-only case still has to be removable).
    assert app._real_peer_id(real) == real
    assert app._real_peer_id("ffffffffffff") == "ffffffffffff"


def test_merge_hashed_peer_rows_collapses_duplicate(monkeypatch):
    """The repair path folds an existing phantom row into the real one.

    Reproduces the observed config exactly: b190acbc219a (= hash of
    abe14d10c140) alongside abe14d10c140, both at the same address.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    assert hashed == "b190acbc219a", "hash derivation changed — update this test"

    app.cfg.peers[real] = PeerInfo(device_id=real, device_name="USER-20240325OS")
    app.cfg.peers[hashed] = PeerInfo(
        device_id=hashed,
        device_name="pc-zhao-b190",
        last_ip="192.168.31.251",
        last_port=19990,
        notes="zhao's PC",
        paired=True,
    )

    app._merge_hashed_peer_rows()

    assert hashed not in app.cfg.peers, "phantom row must be gone"
    assert real in app.cfg.peers
    # Address and note were possibly all the phantom knew — carried over.
    assert app.cfg.peers[real].last_ip == "192.168.31.251"
    assert app.cfg.peers[real].last_port == 19990
    assert app.cfg.peers[real].notes == "zhao's PC"
    # paired is NOT carried over: trust binds to the certificate CN (the real
    # id), so a paired flag on a hash row is non-functional and copying it
    # would grant a pairing the peer never completed.
    assert app.cfg.peers[real].paired is False


def test_merge_hashed_peer_rows_leaves_normal_config_alone(monkeypatch):
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)
    app.cfg.peers["aaaaaaaaaaaa"] = PeerInfo(
        device_id="aaaaaaaaaaaa", device_name="A", paired=True, notes="keep"
    )
    app.cfg.peers["bbbbbbbbbbbb"] = PeerInfo(
        device_id="bbbbbbbbbbbb", device_name="B", paired=False
    )

    app._merge_hashed_peer_rows()

    assert set(app.cfg.peers) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}
    assert app.cfg.peers["aaaaaaaaaaaa"].notes == "keep"
    assert app.cfg.peers["aaaaaaaaaaaa"].paired is True


def test_restore_of_hashed_archive_keys_by_real_id(monkeypatch):
    """Restoring a discovered-only card must not materialise a hash-keyed peer.

    This was the mechanism that created the duplicate: _on_remove archived the
    hashed id, and _on_restore_remove fed it to pairing_mgr.restore_peer.
    """
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    # We know the real id (a handshake resolved it), only the archive is hashed.
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed,
        device_name="pc-zhao-b190",
        last_ip="192.168.31.251",
        last_port=19990,
        removed_at=1.0,
    )

    assert app._on_restore_remove(hashed) is True

    assert hashed not in app.cfg.peers, "must not re-create the phantom"
    assert real in app.cfg.peers
    assert app.cfg.peers[real].paired is False
    assert app.cfg.peers[real].last_ip == "192.168.31.251"
    assert app.transport_mgr.allowed == [real]


def test_restore_drops_archive_when_real_id_already_known(monkeypatch):
    """A hashed archive whose real id is already live is a duplicate — dropping
    it must not overwrite the live entry with the stale archive."""
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.peers[real] = PeerInfo(
        device_id=real, device_name="USER-20240325OS", paired=True, notes="the live one"
    )
    app.cfg.removed_peers[hashed] = PeerInfo(
        device_id=hashed, device_name="pc-zhao-b190", removed_at=1.0
    )

    assert app._on_restore_remove(hashed) is True

    assert hashed not in app.cfg.removed_peers
    assert hashed not in app.cfg.peers
    assert app.cfg.peers[real].paired is True, "live entry must survive"
    assert app.cfg.peers[real].notes == "the live one"


def test_remove_of_hashed_card_archives_under_real_id(monkeypatch):
    from internal.config.config import PeerInfo
    from internal.transport.discovery import Discovery

    app = _remove_app(monkeypatch)
    real = "abe14d10c140"
    hashed = Discovery._hash_device_id(real)
    app.transport_mgr.get_resolved_hashes = lambda: {hashed: real}
    app.cfg.peers[real] = PeerInfo(
        device_id=real,
        device_name="USER-20240325OS",
        paired=True,
        last_ip="192.168.31.251",
        last_port=19990,
    )
    app._send_pairing_msg = lambda *a, **k: None
    app._close_chat_for_peer = lambda pid: None

    app._on_remove(hashed)

    assert real not in app.cfg.peers
    assert hashed not in app.cfg.peers
    # Archived under the real id, with the real row's details (not a bare hash).
    assert real in app.cfg.removed_peers
    assert app.cfg.removed_peers[real].device_name == "USER-20240325OS"
    assert app.cfg.removed_peers[real].last_ip == "192.168.31.251"
    assert hashed not in app.cfg.removed_peers


def test_save_cfg_and_peers_preserves_address_and_note(monkeypatch):
    """cfg.peers is rebuilt from pairing_mgr, which tracks neither the note nor
    the address — dropping them blanked the last known address of every OFFLINE
    peer, killing the last_ip fallback the Pair button relies on."""
    from internal.config.config import PeerInfo

    app = _remove_app(monkeypatch)

    class _Known:
        device_id = "abe14d10c140"
        device_name = "USER-20240325OS"
        certificate_pem = "pem"
        paired = True

    app.pairing_mgr.get_known_peers = lambda: [_Known()]
    app.cfg.peers["abe14d10c140"] = PeerInfo(
        device_id="abe14d10c140",
        device_name="USER-20240325OS",
        paired=True,
        notes="desk PC",
        last_ip="192.168.31.251",
        last_port=19990,
    )

    app._save_cfg_and_peers()

    row = app.cfg.peers["abe14d10c140"]
    assert row.notes == "desk PC"
    assert row.last_ip == "192.168.31.251"
    assert row.last_port == 19990


def _reach_cfg(**attrs):
    from internal.config.config import Config, PeerInfo

    cfg = Config()
    cfg.peers = {
        "peer-on": PeerInfo(device_id="peer-on", device_name="Desk", paired=True),
        "peer-off": PeerInfo(device_id="peer-off", device_name="Laptop", paired=True),
    }
    cfg.removed_peers = {}
    for k, v in attrs.items():
        setattr(cfg, k, v)
    return cfg


def _reach_rows(cfg, connected=()):
    from internal.web.api import devices as devices_api

    result, _ = devices_api.get_devices(cfg, lambda: list(connected))
    return {d["device_id"]: d for d in result["devices"]}


def test_internet_paired_peer_is_relay_reachable_while_lan_offline():
    cfg = _reach_cfg(internet_sync_enabled=True, netpair_secrets={"peer-off": "ABCDEFG"})
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is True
    # The peer without a secret stays unreachable — this is per-peer, not a
    # blanket "internet sync is on" flag.
    assert rows["peer-on"]["relay_reachable"] is False


def test_lan_paired_peer_with_an_exchanged_relay_secret_is_reachable():
    cfg = _reach_cfg(internet_sync_enabled=True, peer_relay_secrets={"peer-off": "s3cret"})
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is True


def test_relay_secret_without_pairing_is_not_reachable():
    # Mirrors _relay_publish_to_peer: an unpaired peers row refuses the publish,
    # so the UI must not advertise a path through it.
    from internal.config.config import PeerInfo

    cfg = _reach_cfg(internet_sync_enabled=True, peer_relay_secrets={"peer-off": "s3cret"})
    cfg.peers["peer-off"] = PeerInfo(device_id="peer-off", device_name="Laptop", paired=False)
    rows = _reach_rows(cfg, connected=["peer-off"])  # keep the row listed
    assert rows["peer-off"]["relay_reachable"] is False


def test_internet_sync_off_overrides_every_stored_secret():
    # The secrets survive the toggle, but _relay_publish_to_peer refuses while
    # internet sync is off, so nothing is reachable through the relay.
    cfg = _reach_cfg(
        internet_sync_enabled=False,
        netpair_secrets={"peer-off": "ABCDEFG"},
        peer_relay_secrets={"peer-on": "s3cret"},
    )
    rows = _reach_rows(cfg)
    assert rows["peer-off"]["relay_reachable"] is False
    assert rows["peer-on"]["relay_reachable"] is False


def _dedupe_rows(cfg, discovered, **kw):
    from internal.web.api import devices as devices_api

    result, status = devices_api.get_devices(
        cfg, lambda: [], get_discovered=lambda: discovered, **kw
    )
    assert status == 200
    return {d["device_id"]: d for d in result["devices"]}


def _dedupe_cfg(**peers):
    from internal.config.config import Config, PeerInfo

    cfg = Config()
    cfg.peers = {
        pid: PeerInfo(device_id=pid, device_name=spec[0], paired=spec[1])
        for pid, spec in peers.items()
    }
    cfg.removed_peers = {}
    return cfg


def test_renamed_paired_peer_is_not_listed_twice():
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r1": ("Old Stored Name", True)})
    hashed = Discovery._hash_device_id("peer-r1")
    # The device advertises a name that shares nothing with the stored one, so
    # the name heuristic cannot dedupe it — only the hashed id can.
    rows = _dedupe_rows(cfg, {hashed: {"name": "Totally-Different"}})
    assert "peer-r1" in rows
    assert hashed not in rows, "same device listed twice (real id + hashed id)"
    assert len(rows) == 2, "local device + the peer, nothing else"


def test_renamed_unpaired_known_peer_is_not_listed_twice():
    # The unpaired case is the one the Discovered section shows: both rows
    # landed there (paired=False, connected=False) under different ids.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r2": ("Stored Name", False)})
    hashed = Discovery._hash_device_id("peer-r2")
    rows = _dedupe_rows(cfg, {hashed: {"name": "Renamed-Box"}})
    assert "peer-r2" in rows, "on-network unpaired peer keeps its known row"
    assert rows["peer-r2"]["known"] is True
    assert hashed not in rows
    discovered_section = [d for d in rows.values() if not d["paired"] and not d["connected"]]
    assert len(discovered_section) == 1


def test_dedupe_does_not_need_the_resolved_hash_map():
    # Before start-up handshakes there is nothing in get_resolved_hashes(), which
    # is exactly when the duplicate showed up.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r3": ("Box", True)})
    hashed = Discovery._hash_device_id("peer-r3")
    rows = _dedupe_rows(cfg, {hashed: {"name": "Renamed"}}, get_resolved_hashes=lambda: {})
    assert hashed not in rows and "peer-r3" in rows


def test_a_genuinely_different_device_still_gets_its_own_row():
    # The fix must not swallow real neighbours: an unknown hash is still a card.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg(**{"peer-r4": ("Mine", True)})
    other = Discovery._hash_device_id("some-other-device")
    rows = _dedupe_rows(
        cfg, {Discovery._hash_device_id("peer-r4"): {"name": "Mine"}, other: {"name": "Neighbour"}}
    )
    assert "peer-r4" in rows
    assert other in rows and rows[other]["known"] is False


def test_local_device_hash_is_deduped_too():
    # Discovery filters our own service by hash, but a mirrored/echoed sighting
    # of ourselves must never become a second card for this machine.
    from internal.transport.discovery import Discovery

    cfg = _dedupe_cfg()
    rows = _dedupe_rows(cfg, {Discovery._hash_device_id(cfg.device_id): {"name": "Me"}})
    assert list(rows) == [cfg.device_id]
