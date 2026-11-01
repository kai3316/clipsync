"""A broker that will not answer must not hold the tunnel hostage.

The connect loop used to give each broker a flat two seconds and then move on.
Two seconds is shorter than a public broker's TLS handshake over a phone
tether, so a broker that would have worked was abandoned mid-handshake, the
loop moved to the next one, then to the third, and back -- the tunnel spent its
whole life rotating and never connected, which is what "opens but is
unavailable" looked like from the phone.

These hold the two halves of the replacement: wait for an *answer*, and treat a
refusal as one.  A wrong fix here is silent -- the tunnel still reports
"connecting", nothing raises -- so the timing is the assertion.
"""

import threading
import time

import internal.transport.companion_tunnel as tunnel_module
from internal.transport.companion_tunnel import CompanionTunnel

TOKEN = "test-token-0123456789abcdef"


class Broker:
    def __init__(self, url):
        self.url = url
        rest = url.split("://", 1)[-1]
        self.host = rest.split(":")[0]
        self.port = 8084
        self.path = "/mqtt"
        self.username = ""
        self.password = ""
        self.secure = True


class Client:
    """An MQTT client whose CONNACK is scripted.

    ``rc is None`` models the case that matters: the WebSocket is accepted and
    then the broker says nothing at all.  No callback, no close, no error --
    exactly what a blocked port looks like from the browser.
    """

    def __init__(self, broker, rc):
        self._broker_url = broker.url
        self._rc = rc
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscribed = []

    def connect(self, host, port, keepalive=30):
        if self._rc is not None and self.on_connect is not None:
            self.on_connect(self, None, {}, self._rc)

    def loop_start(self):
        return None

    def loop_stop(self):
        return None

    def disconnect(self):
        return None

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0):
        return True


def _start(script):
    """Start a tunnel over ``script`` — a list of (url, CONNACK code or None)."""
    holder = {}
    codes = dict(script)
    tried = []

    def factory(broker):
        tried.append(broker.url)
        client = Client(broker, codes[broker.url])
        tunnel = holder.get("tunnel")
        if tunnel is not None:
            client.on_connect = tunnel._on_connect
            client.on_message = tunnel._on_message
            client.on_disconnect = tunnel._on_disconnect
        return client

    tunnel = CompanionTunnel(
        TOKEN,
        [url for url, _ in script],
        19991,
        client_factory=factory,
    )
    holder["tunnel"] = tunnel
    tunnel.start()
    return tunnel, tried


def _wait_connected(tunnel, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not tunnel.status()["connected"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return tunnel.status()


def test_a_refused_broker_hands_over_without_waiting_out_the_timeout():
    """A CONNACK that says no is an answer.

    The loop used to return from the callback and leave the socket open with no
    timer: on the phone side the same mistake left a dead tunnel the page could
    not recover from until it was reloaded.
    """
    tunnel, tried = _start([
        ("wss://refusing.example:8084/mqtt", 5),   # 5 = not authorised
        ("wss://working.example:8084/mqtt", 0),
    ])
    try:
        started = time.monotonic()
        status = _wait_connected(tunnel)
        elapsed = time.monotonic() - started
        assert status["connected"], "the second broker never got a turn"
        assert status["broker"].startswith("wss://working.example")
        assert tried[:2] == [
            "wss://refusing.example:8084/mqtt",
            "wss://working.example:8084/mqtt",
        ]
        # The refusal must not cost the connect timeout; only the retry delay
        # between the two brokers is legitimate here.
        assert elapsed < tunnel_module.CONNECT_TIMEOUT_SECONDS
    finally:
        tunnel.stop()


def test_a_silent_broker_times_out_and_the_next_one_gets_a_turn(monkeypatch):
    """The blocked-port case: socket accepted, nothing ever answers.

    Without a deadline this is the state that made the phone unusable -- broker
    #1 never reported anything, so brokers #2 and #3 were never tried.
    """
    monkeypatch.setattr(tunnel_module, "CONNECT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(tunnel_module, "RETRY_BACKOFF_SECONDS", (0.05,))
    tunnel, tried = _start([
        ("wss://silent.example:8084/mqtt", None),
        ("wss://working.example:8084/mqtt", 0),
    ])
    try:
        status = _wait_connected(tunnel)
        assert status["connected"], "the silent broker blocked the whole chain"
        assert status["broker"].startswith("wss://working.example")
        assert tried[:2] == [
            "wss://silent.example:8084/mqtt",
            "wss://working.example:8084/mqtt",
        ]
    finally:
        tunnel.stop()


def test_a_silent_broker_is_reported_rather_than_left_blank(monkeypatch):
    """The status line has to say *something* while a broker is being written
    off -- "connecting" with no reason is what made this hard to diagnose."""
    monkeypatch.setattr(tunnel_module, "CONNECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(tunnel_module, "RETRY_BACKOFF_SECONDS", (5.0,))
    tunnel, _tried = _start([("wss://silent.example:8084/mqtt", None)])
    try:
        deadline = time.monotonic() + 3.0
        while not tunnel.status()["error"] and time.monotonic() < deadline:
            time.sleep(0.01)
        status = tunnel.status()
        assert status["connected"] is False
        assert "did not answer" in status["error"]
    finally:
        tunnel.stop()


def test_stop_reaches_a_thread_that_is_still_building_the_next_client(monkeypatch):
    """``stop()`` has to reach a loop that is constructing the next client.

    The loop used to read ``_stop`` once at the top of the round and arm the
    CONNACK event only afterwards, so a ``stop()`` landing inside that window
    had its release thrown away: the wait below ran its whole budget, ``stop()``
    itself blocked out its join, and when it gave up the thread was still alive
    and went on to CONNECT and SUBSCRIBE a broker the application had already
    switched off — a second tunnel talking to the same phone inbox.
    """
    monkeypatch.setattr(tunnel_module, "CONNECT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(tunnel_module, "RETRY_BACKOFF_SECONDS", (5.0,))
    inside = threading.Event()
    release = threading.Event()
    built: list[Client] = []

    def factory(broker):
        client = Client(broker, None)
        built.append(client)
        inside.set()
        # Hold open the gap between the loop's stop check and its wait.
        release.wait(1.0)
        return client

    url = "wss://building.example:8084/mqtt"
    tunnel = CompanionTunnel(TOKEN, [url], 19992, client_factory=factory)
    started = time.monotonic()
    tunnel.start()
    try:
        assert inside.wait(3.0), "the connect loop never built a client"
        tunnel.stop()
        elapsed = time.monotonic() - started
        # The join has to be answered, not timed out: waiting out the connect
        # budget is exactly what the release signal exists to avoid.
        assert elapsed < 2.0, f"stop() blocked {elapsed:.1f}s on the connect timeout"
        time.sleep(0.2)
        assert len(built) == 1, f"a client was built after stop(): {built}"
        assert built[0].subscribed == [], "subscribed to a broker after stop()"
    finally:
        release.set()
        tunnel.stop()
