"""Round 11 — internet relay core (pure logic + transport lifecycle)."""

import json
import threading
import time

import pytest

from internal.transport import relay as R


# ---------------------------------------------------------------- pure crypto

def test_topic_derivation_is_deterministic_and_symmetric():
    t1 = R.derive_topic("aaa", "bbb")
    assert t1 == R.derive_topic("bbb", "aaa")
    assert t1.startswith(R.TOPIC_PREFIX)
    assert len(t1) == len(R.TOPIC_PREFIX) + 24


def test_topic_differs_per_pair_and_hides_single_secret():
    t_ab = R.derive_topic("secret-a", "secret-b")
    t_ac = R.derive_topic("secret-a", "secret-c")
    assert t_ab != t_ac


def test_key_derivation_matches_both_sides():
    k1 = R.derive_key("s1", "s2")
    assert k1 == R.derive_key("s2", "s1")
    assert len(k1) == 32
    assert R.derive_key("s1", "s2") != R.derive_key("s1", "s3")


def test_envelope_roundtrip():
    key = R.derive_key("a", "b")
    frame = b"\x43\x53\x02" + b"payload-bytes"
    blob = R.pack_envelope(frame, key, time.time())
    assert R.open_envelope(blob, key, time.time()) == frame


def test_envelope_tamper_and_wrong_key_rejected():
    key = R.derive_key("a", "b")
    other = R.derive_key("a", "c")
    blob = bytearray(R.pack_envelope(b"hello", key, time.time()))
    blob[-3] ^= 0xFF
    assert R.open_envelope(bytes(blob), key, time.time()) is None
    good = R.pack_envelope(b"hello", key, time.time())
    assert R.open_envelope(good, other, time.time()) is None


def test_envelope_timestamp_window():
    key = R.derive_key("a", "b")
    now = time.time()
    stale = R.pack_envelope(b"x", key, now - R.RELAY_TS_WINDOW - 5)
    future = R.pack_envelope(b"x", key, now + R.RELAY_TS_WINDOW + 5)
    assert R.open_envelope(stale, key, now) is None
    assert R.open_envelope(future, key, now) is None


def test_oversized_frame_refused():
    key = R.derive_key("a", "b")
    with pytest.raises(ValueError):
        R.pack_envelope(b"x" * (R.MAX_RELAY_PAYLOAD + 1), key, time.time())


def test_missing_secrets_raise():
    with pytest.raises(ValueError):
        R.derive_topic("", "b")
    with pytest.raises(ValueError):
        R.derive_key(None, "b")


# ------------------------------------------------------------- fake client

class FakeClient:
    """paho-shaped stand-in: captures calls, lets tests drive callbacks."""

    def __init__(self):
        self.subscribed = []
        self.published = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.disconnected = False
        self.loop_stopped = False
        self.connect_ok = True

    def ws_set_options(self, path=None):
        self.path = path

    def tls_set(self, **kwargs):
        self.tls_kwargs = kwargs

    def connect(self, host, port, keepalive=60):
        if not self.connect_ok:
            raise OSError("refused")
        self.host, self.port = host, port

    def loop_start(self):
        pass

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, bytes(payload)))

        class Info:
            rc = 0
        return Info()

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.loop_stopped = True

    # test helpers -----------------------------------------------------
    def fire_connect(self, index, transport, reason=0):
        transport._connected_on_broker = None
        self.on_connect(self, None, None, reason)

    def fire_message(self, transport, topic, payload):
        class Msg:
            pass
        m = Msg()
        m.topic = topic
        m.payload = payload
        self.on_message(self, None, m)


@pytest.fixture()
def channels():
    return {R.derive_topic("a", "b"): R.derive_key("a", "b")}


def make_transport(channels_dict, brokers=None):
    received = []
    states = []
    clients = []

    def factory():
        c = FakeClient()
        with clients_lock:
            clients.append(c)
        return c

    clients_lock = threading.Lock()

    def wait_for(cond, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with clients_lock:
                snapshot = list(clients)
            if cond(snapshot):
                return True
            time.sleep(0.005)
        return False

    t = R.RelayTransport(
        brokers or ["wss://broker.example:8884/mqtt"],
        get_channels=lambda: dict(channels_dict),
        on_frame=lambda frame, _topic=None: received.append(frame),
        on_state=states.append,
        client_factory=factory,
        sleeper=lambda s: None,
    )
    t._test_wait_clients = lambda n=1: wait_for(lambda cl: len(cl) >= n)
    return t, clients, received, states


# ------------------------------------------------------------ lifecycle

def test_offline_states_and_subscribe_on_connect(channels):
    t, clients, _, states = make_transport(channels)
    t.start()
    assert t.state == R.STATE_CONNECTING
    assert t._test_wait_clients(1)
    topic = next(iter(channels))
    clients[0].fire_connect(0, t)
    assert t.state == R.STATE_ONLINE
    assert clients[0].subscribed == [topic]
    t.stop()
    assert t.state == R.STATE_OFF
    assert clients[0].disconnected


def test_publish_only_when_online(channels):
    t, clients, _, _ = make_transport(channels)
    frame = b"frame-bytes"
    assert t.publish(frame, next(iter(channels)), next(iter(channels.values()))) is False
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    topic, key = next(iter(channels.items()))
    deadline = time.time() + 2
    while t.state != R.STATE_ONLINE and time.time() < deadline:
        time.sleep(0.005)
    assert t.publish(frame, topic, key) is True
    sent_topic, blob = clients[0].published[-1]
    assert sent_topic == topic
    assert R.open_envelope(blob, key, time.time()) == frame
    t.stop()


def test_receive_delivers_and_dedupes(channels):
    t, clients, received, _ = make_transport(channels)
    t.start()
    topic, key = next(iter(channels.items()))
    blob = R.pack_envelope(b"inner-frame", key, time.time())
    clients[0].fire_connect(0, t)
    clients[0].fire_message(t, topic, blob)
    clients[0].fire_message(t, topic, blob)          # duplicate dropped
    clients[0].fire_message(t, "unknown/topic", blob)  # not subscribed pair
    assert received == [b"inner-frame"]
    t.stop()


def test_receive_bad_payload_dropped(channels):
    t, clients, received, _ = make_transport(channels)
    t.start()
    topic, key = next(iter(channels.items()))
    clients[0].fire_connect(0, t)
    clients[0].fire_message(t, topic, json.dumps({"v": 99}).encode())
    clients[0].fire_message(t, topic, b"not-json")
    wrong_key_blob = R.pack_envelope(b"x", R.derive_key("q", "z"), time.time())
    clients[0].fire_message(t, topic, wrong_key_blob)
    assert received == []
    t.stop()


def test_failover_to_next_broker():
    ch = {R.derive_topic("a", "b"): R.derive_key("a", "b")}
    brokers = ["bad-scheme-no-host", "wss://first:8884/mqtt"]
    t, clients, _, _ = make_transport(ch, brokers=brokers)
    # first endpoint unparsable -> skipped; the client created belongs to #1
    t.start()
    parsed = [t._parse_endpoint(b) for b in brokers]
    assert parsed[0] is None
    assert parsed[1] is not None
    assert t._test_wait_clients(1)
    clients[0].fire_connect(1, t)
    deadline = time.time() + 2
    while t.state != R.STATE_ONLINE and time.time() < deadline:
        time.sleep(0.005)
    assert t.state == R.STATE_ONLINE
    t.stop()


def test_no_brokers_means_error_state():
    t, clients, _, _ = make_transport({})
    t._brokers = []
    t.start()
    assert t.state == R.STATE_ERROR
    t.stop()


def test_refresh_channels_resubscribes_new_topics():
    ch = {R.derive_topic("a", "b"): R.derive_key("a", "b")}
    t, clients, _, _ = make_transport(ch)
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    new_topic = R.derive_topic("c", "d")
    ch[new_topic] = R.derive_key("c", "d")
    t.refresh_channels()
    assert new_topic in clients[0].subscribed
    t.stop()


def test_stop_joins_cleanly_under_contention(channels):
    t, clients, _, _ = make_transport(channels)
    t.start()
    assert t._test_wait_clients(1)
    clients[0].fire_connect(0, t)
    stopper = threading.Thread(target=t.stop)
    stopper.start()
    clients[0].fire_connect(0, t)  # race a callback against stop
    stopper.join(timeout=5)
    assert not stopper.is_alive()
