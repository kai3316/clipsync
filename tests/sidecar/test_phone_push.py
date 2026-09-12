"""The bridge from the runtime's events to the phone's WebSocket clients.

Legacy, the Application pushed these events itself from the same code that is
now the sidecar's runtime; nothing did under the sidecar, so the phone's
transfer bars, chat bubbles, relay badge and delivery marks only moved when
their REST route was next polled.  These tests pin the mapping back onto the
legacy WS contract — the message names and payload shapes the phone's ws.js
handles — and the two properties that matter for a listener on the runtime's
hot path: it never raises into the publisher, and it stops when detached.
"""

from types import SimpleNamespace

from internal.application.events import EventJournal
from internal.infrastructure.runtime.phone_push import PhonePush


class WebSocket:
    """Records what the bridge asked the manager to push."""

    def __init__(self):
        self.messages = []

    def broadcast(self, message_type, data=None):
        self.messages.append((message_type, data))

    def broadcast_transfer_progress(
        self, transfer_id, progress, status="transferring", direction=None
    ):
        self.broadcast("transfer_progress", {"id": transfer_id, "progress": progress})

    def broadcast_transfer_complete(self, transfer_id, success, cancelled=False):
        self.broadcast("transfer_complete", {"id": transfer_id, "success": success})

    def broadcast_chat_sessions(self, sessions=None):
        self.broadcast("chat_sessions", {"sessions": sessions or []})

    def broadcast_chat_message(self, session_id, entry):
        self.broadcast("chat_message", {"session_id": session_id, "entry": entry})

    def broadcast_chat_progress(self, session_id, transfer_id, fraction):
        self.broadcast(
            "chat_progress",
            {"session_id": session_id, "transfer_id": transfer_id, "fraction": fraction},
        )

    def broadcast_chat_file_done(self, session_id, transfer_id, success, saved_path, status):
        self.broadcast(
            "chat_file_done",
            {"session_id": session_id, "transfer_id": transfer_id, "success": success},
        )

    # The real manager builds the snapshot itself (it holds the history and the
    # config, and reads the favourites store) from the bridge's request, so
    # these record the request as the message it becomes on the wire.
    def broadcast_history(self):
        self.broadcast("history_updated", {"items": []})

    def broadcast_history_clear(self):
        self.broadcast("history_clear", {})

    def broadcast_favorites(self):
        self.broadcast("favorites_updated", {"favorites": []})


def rig():
    ws = WebSocket()
    runtime = SimpleNamespace(
        chat_sessions=lambda: {"sessions": [{"peer_id": "peer"}], "muted": []},
        current_relay_broker=lambda: "wss://broker:8884/mqtt",
    )
    journal = EventJournal()
    return SimpleNamespace(
        ws=ws, journal=journal, push=PhonePush(ws, runtime, journal),
        publish=journal.publish, messages=ws.messages,
    )


def test_file_transfers_reach_the_page_as_legacy_shaped_messages():
    r = rig()
    r.publish("transfer.progress", {"transfer_id": "t1", "progress": 0.5})
    r.publish("transfer.complete", {"transfer_id": "t1", "success": True, "cancelled": False})

    assert r.messages == [
        ("transfer_progress", {"id": "t1", "progress": 0.5}),
        ("transfer_complete", {"id": "t1", "success": True}),
    ]


def test_chat_messages_and_files_reach_the_page():
    r = rig()
    r.publish("chat.message", {"session_id": "s1", "entry": {"text": "hi"}})
    r.publish("chat.file.progress", {"session_id": "s1", "transfer_id": "t2", "fraction": 0.25})
    r.publish("chat.file.done", {
        "session_id": "s1", "transfer_id": "t2", "success": True,
        "saved_path": "C:/in/a.bin", "status": "done",
    })

    assert r.messages == [
        ("chat_message", {"session_id": "s1", "entry": {"text": "hi"}}),
        ("chat_progress", {"session_id": "s1", "transfer_id": "t2", "fraction": 0.25}),
        ("chat_file_done", {"session_id": "s1", "transfer_id": "t2", "success": True}),
    ]


def test_a_session_change_pushes_the_whole_list():
    r = rig()
    r.publish("chat.sessions.changed", {})

    assert r.messages == [("chat_sessions", {"sessions": [{"peer_id": "peer"}]})]


def test_the_relay_badge_carries_the_broker_it_landed_on():
    r = rig()
    r.publish("relay.state.changed", {"state": "online"})

    assert r.messages == [
        ("relay_state", {"state": "online", "broker": "wss://broker:8884/mqtt"})
    ]


def test_a_ledger_transition_keeps_the_legacy_internet_delivery_shape():
    r = rig()
    r.publish("relay.delivery.changed", {
        "peer_id": "remote", "msg_id": "m1", "status": "delivered",
        "content_hash": "hash-1", "kind": "clipboard", "session_id": "",
    })

    assert r.messages == [
        (
            "internet_delivery",
            {
                "peer_id": "remote", "msg_id": "m1", "status": "delivered",
                "content_hash": "hash-1", "kind": "clipboard", "session_id": "",
            },
        )
    ]


def test_a_confirmed_pair_reaches_the_phone_pairing_panel():
    r = rig()
    r.publish("netpair.peer.changed", {
        "peer_id": "remote", "name": "Remote", "status": "paired",
        "online": True, "last_seen": 1700000000,
    })

    assert r.messages == [
        ("netpair_peer", {"peer_id": "remote", "name": "Remote", "status": "paired"})
    ]


def test_a_refused_connection_carries_the_name_the_peer_gave():
    """The refusing peer named itself in the handshake, so it is passed on."""
    r = rig()
    r.publish("device.connection_rejected", {"device_id": "0123456789abcdef", "name": "Desk"})
    r.publish("device.connection_rejected", {"device_id": "fedcba9876543210", "name": ""})

    assert r.messages == [
        ("connect_rejected", {"peer_id": "0123456789abcdef", "name": "Desk"}),
        ("connect_rejected", {"peer_id": "fedcba9876543210", "name": ""}),
    ]


def test_an_unreachable_peer_falls_back_to_its_short_id():
    """Nobody answered, so there is no name to show — only the id we dialed."""
    r = rig()
    r.publish("device.connection_unreachable", {"device_id": "0123456789abcdef", "name": "Desk"})
    r.publish("device.connection_unreachable", {"device_id": "fedcba9876543210", "name": ""})

    assert r.messages == [
        ("connect_unreachable", {"peer_id": "0123456789abcdef", "name": "Desk"}),
        ("connect_unreachable", {"peer_id": "fedcba9876543210", "name": "fedcba987654"}),
    ]


def test_a_pairing_request_keeps_the_phone_prompt_card_keys():
    """The card is keyed on peer_id/peer_name and shows the SAS beside the code."""
    r = rig()
    r.publish("pairing.request", {
        "device_id": "remote", "name": "Remote", "code": "ABCD-EFGH-JKLM", "sas": "4821",
    })

    assert r.messages == [
        (
            "pairing_request",
            {"peer_id": "remote", "peer_name": "Remote", "code": "ABCD-EFGH-JKLM", "sas": "4821"},
        )
    ]


def test_a_resolved_pairing_drops_the_phone_prompt_card():
    r = rig()
    r.publish("pairing.resolved", {"device_id": "remote", "status": "paired"})

    assert r.messages == [("pairing_resolved", {"peer_id": "remote", "status": "paired"})]


def test_an_ai_config_file_event_is_forwarded_as_it_arrived():
    r = rig()
    r.publish("aiconfig.file", {"event": {"file": "rules.md"}})

    assert r.messages == [("aiconfig_file", {"file": "rules.md"})]


def test_the_device_list_the_server_already_pushes_is_not_forwarded_twice():
    """The server's own fingerprint poll owns that snapshot."""
    r = rig()
    r.publish("devices.changed", {"items": []})
    r.publish("something.else", {"x": 1})

    assert r.messages == []


def test_the_update_lifecycle_reaches_the_page_unwrapped():
    """Legacy broadcast it flat from `_set_update_state`.

    The runtime publishes the same dict wrapped as {"state": snapshot} — the
    shape status() returns, so the native window can apply either to one field
    — and nothing unwrapped it for the phone, whose settings card documents
    these events as how download progress arrives.
    """
    r = rig()
    r.publish("update.state", {"state": {"phase": "downloading", "fraction": 0.25}})
    r.publish("update.state", {"state": {"phase": "ready", "version": "1.0.1"}})

    assert r.messages == [
        ("update_state", {"phase": "downloading", "fraction": 0.25}),
        ("update_state", {"phase": "ready", "version": "1.0.1"}),
    ]


def test_an_update_event_the_page_would_drop_is_not_pushed():
    """`ws.js` accepts a payload only when it has a truthy `phase`.

    Pushing one it would discard is a message on the wire and nothing on the
    screen; the same test the client applies is applied here instead.
    """
    r = rig()
    r.publish("update.state", {})
    r.publish("update.state", {"state": None})
    r.publish("update.state", {"state": {"fraction": 1}})
    r.publish("update.state", {"state": {"phase": ""}})

    assert r.messages == []


def test_a_favourite_changed_elsewhere_reaches_the_page():
    """The native window invalidates its cached list on `favorites.changed`.

    Nothing told the phone, so a favourite added or edited on the desktop
    appeared there only after a reload.  Legacy had no such push and needed
    none: its desktop *was* this page, so there was one surface and one list.
    """
    r = rig()
    r.publish("favorites.changed", {})

    assert r.messages == [("favorites_updated", {"favorites": []})]


def test_the_page_gets_the_favourites_the_real_manager_loads(tmp_path, monkeypatch):
    """The payload is the one `GET /api/favorites` answers with, unmodified.

    The case above runs against a double that invents the payload; this one
    drives the real ``WebSocketManager`` against a real favourites store, so
    what the page merges is what the panel's own route would have handed it —
    the store the native window writes and the store this read share one file.
    """
    from internal.web.api import favorites as fav_api
    from internal.web.ws import WebSocketManager

    monkeypatch.setattr(fav_api, "_FAV_DB_PATH", str(tmp_path / "favorites.db"))
    fav_api._repository().add("Deploy key", "ssh-ed25519 AAAA", "Work")

    class Cfg:
        device_id = "me"
        device_name = "Desk"
        peers: dict = {}
        web_history_limit = 30

    mgr = WebSocketManager(Cfg(), None, None, lambda: [])
    sent = []
    mgr.broadcast = lambda message_type, data=None: sent.append((message_type, data))
    journal = EventJournal()
    push = PhonePush(mgr, None, journal)
    try:
        journal.publish("favorites.changed", {})
    finally:
        push.close()
        mgr.shutdown()

    assert [name for name, _ in sent] == ["favorites_updated"]
    favorites = sent[0][1]["favorites"]
    assert [f["title"] for f in favorites] == ["Deploy key"]
    assert favorites[0]["group"] == "Work"


def test_the_page_gets_the_lifecycle_the_real_service_publishes():
    """The envelope the bridge unwraps belongs to the producer, not the double.

    The two cases above feed the bridge a payload shaped the way this test says
    the service shapes it; were that envelope to change, they would stay green
    while the phone's update card froze.  So this drives the real
    ``UpdateService``: what must survive is the flat state ``ws.js`` merges.
    """
    from internal.system.update_service import UpdateService

    journal = EventJournal()
    ws = WebSocket()
    service = UpdateService(publish=journal.publish, home=".")
    push = PhonePush(ws, SimpleNamespace(current_relay_broker=lambda: ""), journal)
    try:
        service._set_state(phase="downloading", fraction=0.5)
    finally:
        push.close()

    assert [name for name, _ in ws.messages] == ["update_state"]
    state = ws.messages[0][1]
    assert state["phase"] == "downloading"
    assert state["fraction"] == 0.5
    # The whole state travels, as it did in legacy: the page folds it into its
    # own copy, so a field left out here would sit stale in the card.
    assert state["version"] == ""


def test_a_history_change_reaches_the_page_as_a_fresh_snapshot():
    """Legacy wired `sync_mgr.on_history_change` to `broadcast_history()`.

    Under the sidecar that hook went to the event journal and the panel half
    was dropped, so a clip copied here — or arriving from a peer — was invisible
    to an open phone until the page was reloaded.  The snapshot carries no
    "what changed" payload on purpose: the client replaces the visible window.
    """
    r = rig()
    r.publish("history.changed", {})
    r.publish("history.changed", {"ids": ["e1"]})
    # A one-row delete publishes the singular key, so the panel's own delete
    # route lands here with the same shape a change made in the window does.
    r.publish("history.changed", {"id": "e1"})

    assert r.messages == [
        ("history_updated", {"items": []}),
        ("history_updated", {"items": []}),
        ("history_updated", {"items": []}),
    ]


def test_a_wiped_history_carries_the_clear_the_snapshot_cannot_express():
    """An empty snapshot leaves the client's pagination standing, so a wiped
    list would still offer "Load more" over nothing."""
    r = rig()
    r.publish("history.changed", {"cleared": 3})
    # The count is not the point: clearing an already-empty list still means
    # "forget your offsets", so a falsy 0 must clear too.
    r.publish("history.changed", {"cleared": 0})

    assert r.messages == [("history_clear", {}), ("history_clear", {})]


def test_the_bridge_asks_the_manager_for_a_snapshot_it_really_builds():
    """The manager owns the payload; the bridge only picks the helper.

    Every other case here runs against a double that implements whatever the
    bridge calls, so a helper renamed or emptied on the manager would leave
    them green and the phone silent.  This one drives the real
    ``WebSocketManager``: what must survive is the shape the page's ``ws.js``
    merges (`items`, `total`) and the wipe it takes as its own message.
    """
    from internal.web.ws import WebSocketManager

    class Cfg:
        device_id = "me"
        device_name = "Desk"
        peers: dict = {}
        web_history_limit = 30

    class History:
        def get_all(self):
            return [
                {
                    "timestamp": 1.0,
                    "content_type": "TEXT",
                    "text_preview": "hi",
                    "source_device": "",
                    "entry_id": "e1",
                }
            ]

    mgr = WebSocketManager(Cfg(), History(), None, lambda: [])
    sent = []
    mgr.broadcast = lambda message_type, data=None: sent.append((message_type, data))
    journal = EventJournal()
    push = PhonePush(mgr, None, journal)
    try:
        journal.publish("history.changed", {})
        journal.publish("history.changed", {"cleared": 1})
    finally:
        push.close()
        mgr.shutdown()

    assert [name for name, _ in sent] == ["history_updated", "history_clear"]
    # A client-less broadcast is the no-op above; this is the snapshot itself.
    assert sent[0][1]["items"][0]["entry_id"] == "e1"
    assert sent[0][1]["total"] == 1


def test_a_failing_push_never_reaches_the_publisher():
    class Exploding(WebSocket):
        def broadcast(self, message_type, data=None):
            raise RuntimeError("client gone")

    journal = EventJournal()
    push = PhonePush(Exploding(), SimpleNamespace(current_relay_broker=lambda: ""), journal)
    try:
        journal.publish("relay.state.changed", {"state": "off"})
        # The journal still recorded the event, so the UI stream is unaffected.
        events, _gap = journal.since(0)
        assert [event["name"] for event in events] == ["relay.state.changed"]
    finally:
        push.close()


def test_detaching_stops_the_pushes():
    r = rig()
    r.publish("relay.state.changed", {"state": "online"})
    assert len(r.messages) == 1

    r.push.close()
    r.push.close()  # idempotent — a stop may run twice
    r.publish("relay.state.changed", {"state": "off"})
    assert len(r.messages) == 1
