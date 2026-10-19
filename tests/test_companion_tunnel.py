"""The companion relay tunnel: wire format, allow-list, receipts and filters.

These exercise the protocol without a broker and without a listening companion:
a fake MQTT client is the transport, a callable is the HTTP hop, and every phone
frame is sealed with the same envelope the real phone will use.  What they hold
is the part a PWA cannot be trusted to correct later -- which paths are
reachable, what a duplicate does, and what a wrong token sees.
"""

import base64
import json
import time

from internal.transport.companion_tunnel import (
    PROTOCOL_VERSION,
    CompanionTunnel,
    derive_key,
    is_allowed,
    topics,
)
from internal.transport.relay import open_envelope_ex, pack_envelope

TOKEN = "test-token-0123456789abcdef"
NOW = 1_000_000.0


class FakeBroker:
    def __init__(self, url="wss://broker.example:8084/mqtt"):
        self.url = url
        self.host = "broker.example"
        self.port = 8084
        self.path = "/mqtt"
        self.username = ""
        self.password = ""
        self.secure = True


class FakeClient:
    def __init__(self, broker):
        self._broker_url = broker.url
        self.subscribed = []
        self.published = []
        self.connecting = False
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def connect(self, host, port, keepalive=30):
        self.connecting = True
        if self.on_connect is not None:
            self.on_connect(self, None, {}, 0)

    def loop_start(self):
        return None

    def loop_stop(self):
        return None

    def disconnect(self):
        self.connecting = False

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return True


class Phone:
    """A phone-side encoder using only the public wire format."""

    def __init__(self, token=TOKEN, now=None):
        self.key = derive_key(token)
        self._now = now or (lambda: NOW)

    def frame(self, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return pack_envelope(raw, self.key, self._now())

    def request(self, method, path, body=b"", request_id="r-1", token=None):
        key = self.key if token is None else derive_key(token)
        raw = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": "req",
                "id": request_id,
                "method": method,
                "path": path,
                "body": base64.b64encode(body).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode()
        return pack_envelope(raw, key, self._now())


def build(monkeypatch, http=None, client=None, token=TOKEN, mode="full"):
    client = client or FakeClient(FakeBroker())
    calls = []

    def factory(_broker):
        calls.append(_broker)
        return client

    def default_http(method, path, body, headers):
        return 200, b'{"ok":true}'

    tunnel = CompanionTunnel(
        token,
        ["wss://broker.example:8084/mqtt"],
        19991,
        computer_name="Desk",
        mode=mode,
        client_factory=factory,
        http_request=http or default_http,
        clock=lambda: NOW,
    )
    # The default client wires its callbacks when paho creates it; a fake is
    # created outside, so the same wiring happens here.
    client.on_connect = tunnel._on_connect
    client.on_message = tunnel._on_message
    client.on_disconnect = tunnel._on_disconnect
    return tunnel, client, calls


def connected_tunnel(monkeypatch, http=None, mode="full"):
    tunnel, client, calls = build(monkeypatch, http=http, mode=mode)
    tunnel.start()
    deadline = time.monotonic() + 2.0
    while not tunnel.status()["connected"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tunnel.status()["connected"], "the fake broker never connected"
    return tunnel, client, calls


def last_response(client):
    topic, blob, qos = client.published[-1]
    inner, reason = open_envelope_ex(blob, derive_key(TOKEN), 1_000_000.0)
    assert inner is not None, reason
    return topic, json.loads(inner), qos


def test_key_and_topics_are_deterministic_and_up_down_are_distinct():
    assert derive_key(TOKEN) == derive_key(TOKEN)
    assert len(derive_key(TOKEN)) == 32
    up, down = topics(TOKEN)
    assert up != down
    assert up.endswith("/up") and down.endswith("/down")
    assert TOKEN not in up


def test_the_allow_list_matches_the_design_and_refuses_everything_else():
    assert is_allowed("GET", "/api/status")
    assert is_allowed("GET", "/api/history?limit=30")
    assert is_allowed("GET", "/api/history/item?entry_id=x")
    assert is_allowed("GET", "/api/history/preview?entry_id=1")
    assert is_allowed("GET", "/api/chat/sessions")
    assert is_allowed("GET", "/api/chat/devices")
    assert is_allowed("GET", "/api/phone-chat/messages?after_id=0")
    assert is_allowed("POST", "/api/phone-chat/send")
    assert is_allowed("POST", "/api/chat/text")
    assert is_allowed("POST", "/api/chat/invite")
    assert is_allowed("POST", "/api/chat/mute")
    assert is_allowed("POST", "/api/push")
    assert is_allowed("POST", "/api/chat/send")
    assert is_allowed("GET", "/api/files")
    assert is_allowed("GET", "/api/transfer")
    assert is_allowed("POST", "/api/transfer/cancel-all")
    assert is_allowed("POST", "/api/favorites")
    assert is_allowed("POST", "/api/paste-rich")
    assert is_allowed("DELETE", "/api/files")
    assert is_allowed("GET", "/api/download?file=clip.png")
    assert not is_allowed("GET", "/api/settings")
    assert not is_allowed("POST", "/api/upload")
    assert not is_allowed("GET", "/api/history/../secret")
    assert not is_allowed("POST", "/api/history?x=1")


def test_an_allowed_request_is_proxied_and_sealed(monkeypatch):
    seen = []

    def http(method, path, body, headers):
        seen.append((method, path, body))
        return 200, b'{"items":[]}'

    tunnel, client, calls = connected_tunnel(monkeypatch, http=http)
    try:
        tunnel.handle_blob(Phone().request("GET", "/api/history?limit=30"))
        topic, frame, qos = last_response(client)
        assert topic.endswith("/down") and qos == 1
        assert frame["type"] == "res" and frame["status"] == 200
        assert base64.b64decode(frame["body"]) == b'{"items":[]}'
        assert seen == [("GET", "/api/history?limit=30", b"")]
    finally:
        tunnel.stop()


def test_a_disallowed_path_is_refused_without_reaching_the_companion(monkeypatch):
    seen = []

    def http(method, path, body, headers):
        seen.append(path)
        return 200, b"{}"

    tunnel, client, _ = connected_tunnel(monkeypatch, http=http)
    try:
        tunnel.handle_blob(Phone().request("GET", "/api/settings"))
        _, frame, _ = last_response(client)
        assert frame["status"] == 501
        assert seen == []
    finally:
        tunnel.stop()


def test_readonly_mode_still_reads_but_refuses_a_write(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch, mode="readonly")
    try:
        tunnel.handle_blob(Phone().request("GET", "/api/history?limit=1"))
        _, frame, _ = last_response(client)
        assert frame["status"] == 200
        tunnel.handle_blob(Phone().request("POST", "/api/push", request_id="push-1"))
        _, frame, _ = last_response(client)
        assert frame["status"] == 501
        assert json.loads(base64.b64decode(frame["body"]))["error"] == "remote_unsupported"
    finally:
        tunnel.stop()


def test_a_duplicate_request_returns_the_cached_answer_without_asking_twice(monkeypatch):
    calls = []

    def http(method, path, body, headers):
        calls.append(path)
        return 200, b"first"

    tunnel, client, _ = connected_tunnel(monkeypatch, http=http)
    try:
        phone = Phone()
        frame = phone.request("GET", "/api/status", request_id="same-id")
        tunnel.handle_blob(frame)
        tunnel.handle_blob(frame)
        assert calls == ["/api/status"]
        assert len([p for p in client.published if p[2] == 1]) == 2
    finally:
        tunnel.stop()


def test_the_wrong_token_cannot_open_the_channel(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        tunnel.handle_blob(Phone(token="another-token").request("GET", "/api/status"))
        assert client.published == []
    finally:
        tunnel.stop()


def test_a_stale_envelope_is_ignored(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        phone = Phone(now=lambda: NOW - 3600.0)
        tunnel.handle_blob(phone.request("GET", "/api/status"))
        assert client.published == []
    finally:
        tunnel.stop()


def test_a_hello_answers_hello_ack_and_marks_the_phone_online(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        tunnel.handle_blob(
            Phone().frame({"v": PROTOCOL_VERSION, "type": "hello", "phone_id": "phone-1"})
        )
        _, frame, qos = last_response(client)
        assert frame["type"] == "hello_ack" and frame["computer_name"] == "Desk"
        status = tunnel.status()
        assert status["phone_online"] is True and status["phone_id"] == "phone-1"
    finally:
        tunnel.stop()


def test_events_are_filtered_by_the_phone_push_table(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        tunnel.publish_event("chat.message", {"session_id": "s1"})
        published = [p for p in client.published if p[2] == 0]
        assert published, "an allowed event was not published"
        inner, _ = open_envelope_ex(published[-1][1], derive_key(TOKEN), 1_000_000.0)
        assert json.loads(inner)["name"] == "chat.message"
        before = len(client.published)
        tunnel.publish_event("some.unknown.event", {})
        assert len(client.published) == before
    finally:
        tunnel.stop()


def test_the_rate_limit_answers_429_after_the_budget(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        phone = Phone()
        for index in range(21):
            tunnel.handle_blob(phone.request("GET", "/api/status", request_id=f"r{index}"))
        statuses = []
        for _, blob, _ in client.published:
            inner, _ = open_envelope_ex(blob, derive_key(TOKEN), 1_000_000.0)
            statuses.append(json.loads(inner)["status"])
        assert statuses[-1] == 429
        assert statuses.count(429) == 1
    finally:
        tunnel.stop()


def test_a_body_over_the_inner_budget_is_refused(monkeypatch):
    tunnel, client, _ = connected_tunnel(monkeypatch)
    try:
        tunnel.handle_blob(Phone().request("POST", "/api/push", body=b"x" * (40 * 1024)))
        _, frame, _ = last_response(client)
        assert frame["status"] in (400, 413)
    finally:
        tunnel.stop()


def test_an_unstarted_tunnel_is_not_marked_connected():
    tunnel, _client, _ = build(None)
    status = tunnel.status()
    assert status["connected"] is False and status["phone_online"] is False
