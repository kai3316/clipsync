"""Phone-companion remote access through an MQTT relay.

The phone companion is an HTTP server on this machine.  A phone on the same
network reaches it directly; a phone on a mobile network cannot, because there
is no route to a NAT'd desktop and no reason to ask the user to forward a port.
This module carries the companion's API over an existing public MQTT broker as a
request/response tunnel, so both ends only ever make outbound connections.

The broker is untrusted: every frame is sealed with a key derived from the
companion's access token (the same AES-256-GCM envelope the internet relay
uses), and the topic is a hash of that token.  What the broker sees is
ciphertext, a topic, and timing -- never a request, a response, or the token.

Phase 1 carries small JSON bodies (the design's 24 KiB inner-frame budget); a
larger body is refused as ``413`` until the chunk frames in the design are
implemented.  The tunnel deliberately does not proxy arbitrary paths: an
explicit method/path allow-list is the difference between "the phone can read
history" and "whoever holds the token owns the API".

Wire format (one envelope payload, JSON, UTF-8)::

    {"v": 1, "type": "hello", "phone_id": "...", "name": "...", "caps": []}
    {"v": 1, "type": "hello_ack", "v": 1, "computer_name": "...", "caps": []}
    {"v": 1, "type": "req", "id": "uuid", "method": "GET", "path": "/api/status",
     "body": "<base64 or empty>"}
    {"v": 1, "type": "res", "id": "uuid", "status": 200, "body": "<base64>"}
    {"v": 1, "type": "evt", "name": "chat.message", "data": {...}}
    {"v": 1, "type": "ping" | "pong"}
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from internal.transport.relay import MAX_RELAY_FRAME, open_envelope_ex, pack_envelope

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
# The inner frame, not the envelope: an envelope is ~4/3 of the frame plus its
# JSON shell, and the design budgets one broker message per request/response.
MAX_INNER_BYTES = 32 * 1024
MAX_ENVELOPE_BYTES = 60 * 1024
REQUEST_TIMEOUT_SECONDS = 8.0
HELLO_TTL_SECONDS = 90.0
# How long one broker gets to answer CONNECT with a CONNACK.  This used to be a
# flat two seconds, and two seconds is not enough for a public broker over a
# phone tether: TLS plus CONNACK regularly takes longer, so a broker that would
# have worked was abandoned mid-handshake, the loop moved to the next one, and
# then to the third, and back again -- the tunnel spent its life rotating and
# never connected.  Measured against broker.emqx.io from a phone hotspot, the
# handshake often lands between three and six seconds.
CONNECT_TIMEOUT_SECONDS = 12.0
# Retry delays after consecutive failures, indexed by how many brokers have
# just failed in a row.  A fixed two-second retry made the "everything is down"
# case rotate as fast as the "one broker hiccuped" case, which is both the
# least useful time to spin and the most visible one.
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0)
# Reads allowed per rolling minute for one remote token.  The phone page's own
# polling used to spend most of a 20/min budget, so a burst of real actions
# (open every tab, send, favourite) pushed a user request into a 429 that looked
# like a failed action.  With the page now pacing its polls (and refreshing from
# pushed events), 60 leaves room for the reader while still bounding a leaked
# token to a slow, cheap trickle of loopback reads.
RATE_LIMIT_PER_MINUTE = 60
DEDUPE_CAPACITY = 256

# What may leave this machine as a push event.  The names are the existing
# phone-push table in ``phone_push._HANDLERS``; a new event is added here when a
# phone surface learns to draw it, never automatically.
EVENT_NAMES = frozenset({
    "transfer.progress",
    "transfer.complete",
    "history.changed",
    "chat.sessions.changed",
    "chat.message",
    "chat.file.progress",
    "chat.file.done",
    "relay.state.changed",
    "relay.delivery.changed",
    "netpair.peer.changed",
    "pairing.request",
    "pairing.resolved",
    "device.connection_rejected",
    "device.connection_unreachable",
    "device.connect_failed",
    "aiconfig.file",
    "update.state",
    "favorites.changed",
    "companion.changed",
})

# The API surface remote access is allowed to reach.  Full path matching, not a
# prefix: ``/api/history`` is fine, ``/api/history/clear`` is a POST of its own,
# and ``/api/history/../../...`` must not become a URL because a prefix matched.
#
# Reads and writes are separate because "remote access" is not one permission:
# a phone on a bus is a much worse place for "delete history" than for "show
# the last thirty clips".  ``readonly`` is the default mode; ``full`` adds the
# write half.  Both are still bounded by the path allow-list -- mode is a second
# gate, not the first one.
_READ_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/status(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/history(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/history/item(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/history/preview(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/favorites(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/phone-chat/messages(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/chat/sessions(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/chat/messages(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/chat/devices(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/transfers(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/transfer(?:[?&].*)?$")),
    # The phone's Files tab.  Upload is deliberately absent until the chunk
    # frames exist, but listing and downloading are small bodies and are what
    # the tab opens with; refusing them as "not on the allow-list" made the
    # page report a rotated token, which was the wrong diagnosis.
    ("GET", re.compile(r"^/api/files(?:[?&].*)?$")),
    ("GET", re.compile(r"^/api/download(?:[?&].*)?$")),
)

_WRITE_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/api/push$")),
    ("POST", re.compile(r"^/api/history/clear$")),
    ("POST", re.compile(r"^/api/pin$")),
    ("POST", re.compile(r"^/api/delete$")),
    ("POST", re.compile(r"^/api/chat/(?:send|read|accept|decline|close)$")),
    # The rest of the text-chat surface: an invite creates a session, text
    # carries the message, typing/resend/mute are the small verbs around it.
    # Chat file upload stays out until the chunk frames exist.
    ("POST", re.compile(r"^/api/chat/(?:invite|text|typing|resend|mute)$")),
    ("POST", re.compile(r"^/api/chat/file/(?:accept|decline|cancel)$")),
    ("POST", re.compile(r"^/api/phone-chat/(?:send|read)$")),
    ("POST", re.compile(r"^/api/transfer/(?:cancel|cancel-all|retry|accept|reject|pause|resume)$")),
    ("POST", re.compile(r"^/api/favorites$")),
    ("DELETE", re.compile(r"^/api/favorites$")),
    ("POST", re.compile(r"^/api/favorites/export$")),
    ("POST", re.compile(r"^/api/paste-rich$")),
    ("DELETE", re.compile(r"^/api/files$")),
)

_ALLOWED = _READ_ALLOWED + _WRITE_ALLOWED

REMOTE_MODES = ("readonly", "full")


def _hkdf(token: str, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 (one block) for the 32-byte channel key.

    A single-block expand is enough for AES-256 and keeps the Python and
    WebCrypto implementations easy to hold to a shared test vector.
    """
    prk = hmac.new(salt, token.encode("utf-8"), hashlib.sha256).digest()
    out = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return out[:length]


def derive_key(token: str) -> bytes:
    """The AES-256 key both ends derive from the companion token."""
    return _hkdf(token, b"clipsync-companion-v1", b"channel-key")


def channel_id(token: str) -> str:
    """The topic component: a hash, so the topic does not carry the token."""
    return hashlib.sha256(b"clipsync-companion-topic|" + token.encode("utf-8")).hexdigest()[:24]


def topics(token: str) -> tuple[str, str]:
    """``(phone → desktop, desktop → phone)`` topics for one token."""
    chan = channel_id(token)
    return (
        f"clipsync/comp/v1/{chan}/up",
        f"clipsync/comp/v1/{chan}/down",
    )


def is_allowed(method: str, path: str, mode: str = "full") -> bool:
    """Whether a remote request may reach this machine's companion API.

    ``mode`` is ``"readonly"`` or ``"full"``; the default keeps the pre-mode
    call sites (and the tests that describe the whole surface) working.
    """
    method = method.upper()
    allowed = _ALLOWED if mode == "full" else _READ_ALLOWED
    return any(method == table_method and pattern.match(path) for table_method, pattern in allowed)


class _Broker:
    """One parsed broker endpoint, including the optional credentials."""

    def __init__(self, url: str):
        parsed = urlparse(url if "://" in url else f"wss://{url}")
        self.url = url
        self.secure = parsed.scheme in ("wss", "tls", "ssl", "mqtts")
        self.host = parsed.hostname or ""
        self.port = parsed.port or (8084 if self.secure else 8083)
        self.path = parsed.path or "/mqtt"
        self.username = parsed.username or ""
        self.password = parsed.password or ""


class CompanionTunnel:
    """Carry the companion API over MQTT, with the token as the only secret.

    ``client_factory`` and ``http_request`` are injectable so the protocol can
    be tested without a broker or a listening companion; the defaults are paho
    and a loopback HTTP client.
    """

    def __init__(
        self,
        token: str,
        brokers: list[str],
        companion_port: int,
        *,
        companion_token: str | None = None,
        computer_name: str = "",
        mode: str = "readonly",
        client_factory: Callable[[_Broker], Any] | None = None,
        http_request: Callable[[str, str, bytes, dict], tuple[int, bytes]] | None = None,
        clock: Callable[[], float] = time.time,
        max_envelope_bytes: int = MAX_ENVELOPE_BYTES,
    ):
        if not token:
            raise ValueError("companion tunnel needs a token")
        self._token = token
        # The secret that authenticates to the *local* companion is not the
        # secret the phone uses for the tunnel.  They were the same value while
        # remote access fell back to web_token; with a separate remote token
        # they are two credentials, and the loopback hop still needs the LAN one.
        self._companion_token = token if companion_token is None else companion_token
        self._brokers = [_Broker(url) for url in brokers if url]
        self._port = int(companion_port)
        self._computer_name = computer_name
        self._mode = mode if mode in REMOTE_MODES else "readonly"
        self._client_factory = client_factory or self._default_client
        self._http_request = http_request or self._default_http
        self._clock = clock
        self._max_envelope = min(max_envelope_bytes, MAX_RELAY_FRAME)
        self._key = derive_key(token)
        self._up_topic, self._down_topic = topics(token)
        self._lock = threading.RLock()
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set by every CONNACK, successful or not, so the connect loop waits
        # for an *answer* instead of a slice of wall-clock time.  Sticky, so a
        # fake client that answers inside ``connect()`` is not missed.
        self._connected_event = threading.Event()
        self._connect_rc: int | None = None
        self._connected_broker = ""
        self._phone_id = ""
        self._last_seen = 0.0
        self._rx = 0
        self._tx = 0
        self._error = ""
        self._seen: OrderedDict[str, bytes] = OrderedDict()
        self._rate: deque[float] = deque()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._connect_loop, daemon=True, name="companion-tunnel"
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Release a loop parked on a broker that has not answered yet; the
        # thread would otherwise sit out the whole connect timeout before
        # noticing the stop.
        self._connected_event.set()
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                logger.debug("Companion tunnel disconnect failed", exc_info=True)
            with contextlib.suppress(Exception):
                client.loop_stop()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            self._connected_broker = ""
            self._phone_id = ""
            self._last_seen = 0.0

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "connected": bool(self._connected_broker),
                "broker": self._connected_broker,
                "phone_online": bool(
                    self._phone_id and self._clock() - self._last_seen <= HELLO_TTL_SECONDS
                ),
                "phone_id": self._phone_id,
                "last_seen": self._last_seen,
                "mode": self._mode,
                "rx": self._rx,
                "tx": self._tx,
                "error": self._error,
            }

    # -------------------------------------------------------------- transport

    def _connect_loop(self) -> None:
        index = 0
        failures = 0
        while not self._stop.is_set() and self._brokers:
            broker = self._brokers[index % len(self._brokers)]
            index += 1
            client = None
            answered = False
            try:
                # Cleared *before* any slow work, not immediately before the
                # wait: ``stop()`` sets this event to release a loop that is
                # parked, and clearing it after a construct+assign round trip
                # leaves a window in which that release is thrown away, the
                # wait below then runs its full timeout, and a broker gets
                # dialled by a thread whose ``stop()`` has already returned.
                self._connected_event.clear()
                client = self._client_factory(broker)
                if self._stop.is_set():
                    return
                with self._lock:
                    self._client = client
                    self._connect_rc = None
                client.connect(broker.host, broker.port, keepalive=30)
                client.loop_start()
                # Wait for the broker's answer rather than for a fixed slice of
                # time -- see CONNECT_TIMEOUT_SECONDS for why two seconds was
                # the bug, not a tuning knob.
                self._connected_event.wait(CONNECT_TIMEOUT_SECONDS)
                if self._stop.is_set():
                    return
                with self._lock:
                    code = self._connect_rc
                if code is None:
                    logger.info(
                        "Companion tunnel: %s did not answer in %.0fs",
                        broker.url,
                        CONNECT_TIMEOUT_SECONDS,
                    )
                    with self._lock:
                        self._error = "broker did not answer"
                    failures += 1
                elif code != 0:
                    logger.info("Companion tunnel: %s refused (%s)", broker.url, code)
                    failures += 1
                else:
                    answered = True
                    failures = 0
                    # A healthy client stays here; a disconnect clears
                    # ``_connected_broker`` and the next broker gets a turn.
                    while not self._stop.is_set() and self._connected_broker:
                        self._stop.wait(1.0)
            except Exception as exc:
                logger.info("Companion tunnel: %s unavailable (%s)", broker.url, exc)
                with self._lock:
                    self._error = str(exc)
                failures += 1
            finally:
                # Released either way.  Each round used to drop the previous
                # client without disconnecting it, and paho's client is a
                # network thread plus a socket that keeps trying to reconnect
                # on its own — so a broker that stayed down leaked one of each
                # every couple of seconds, and "down" was also the state that
                # made the loop spin fastest.
                if client is not None:
                    self._release_client(client)
                with self._lock:
                    # Empty, not None: this field is published as ``"broker"``
                    # in ``status()`` and every other writer leaves a string
                    # there, so a null would travel to a client whose type says
                    # ``string`` — the kind of lie that only breaks once someone
                    # calls ``.startsWith`` on it.
                    self._connected_broker = ""
                    if self._client is client:
                        self._client = None
            if self._stop.is_set():
                return
            # A connection that worked and then dropped retries immediately:
            # the broker is reachable, so backing off would only stretch the
            # outage.  A run of failures backs off, so three dead brokers are
            # retried every twenty seconds rather than every two.
            if answered:
                self._stop.wait(1.0)
            else:
                # ``failures`` is at least 1 here — every path that leaves
                # ``answered`` false increments it — and ``max`` says so out
                # loud.  Relying on the caller was one refactor away from an
                # index of -1, which Python answers with the *last* backoff:
                # a twenty-second sleep where "retry now" was meant, and no
                # error to notice it by.
                step = min(max(failures, 1), len(RETRY_BACKOFF_SECONDS)) - 1
                self._stop.wait(RETRY_BACKOFF_SECONDS[step])

    def _release_client(self, client) -> None:
        """Stop and disconnect one paho client, never raising."""
        try:
            client.loop_stop()
        except Exception:
            logger.debug("Companion tunnel loop_stop failed", exc_info=True)
        try:
            client.disconnect()
        except Exception:
            logger.debug("Companion tunnel disconnect failed", exc_info=True)

    def _default_client(self, broker: _Broker) -> Any:
        import paho.mqtt.client as mqtt

        kwargs = {
            "transport": "websockets",
            "client_id": f"clipsync-comp-{uuid.uuid4().hex[:10]}",
            "clean_session": True,
            "protocol": mqtt.MQTTv311,
        }
        try:
            # paho 2.0 made ``callback_api_version`` a required first argument;
            # 2.1 defaulted it and only warns.  The declared floor is
            # ``paho-mqtt>=2.0``, so a 2.0.x install raises ValueError here —
            # inside ``_connect_loop``'s catch, which reported it as "broker
            # unavailable" and left the tunnel dead with no usable reason.
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1, **kwargs)
        except TypeError:
            client = mqtt.Client(**kwargs)
        client.ws_set_options(path=broker.path)
        if broker.secure:
            client.tls_set()
        if broker.username:
            client.username_pw_set(broker.username, broker.password)
        # Read back in `_on_connect`; paho does not carry the URL itself.
        client._broker_url = broker.url
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        return client

    def _default_http(
        self, method: str, path: str, body: bytes, headers: dict
    ) -> tuple[int, bytes]:
        request = Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=body if body else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._companion_token}",
                "Content-Type": "application/json",
                "Host": f"127.0.0.1:{self._port}",
                **headers,
            },
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return int(response.status), response.read()
        except HTTPError as exc:
            return int(exc.code), exc.read()
        except (URLError, TimeoutError, OSError) as exc:
            logger.debug("Companion tunnel HTTP failed: %s", exc)
            return 503, b'{"error":"companion unavailable"}'

    # -------------------------------------------------------------- MQTT callbacks

    def _on_connect(self, client, userdata, flags, rc=0, *extra) -> None:
        code = int(rc or 0)
        with self._lock:
            self._connect_rc = code
            if code != 0:
                self._error = f"broker refused connection ({code})"
            else:
                self._connected_broker = str(getattr(client, "_broker_url", "") or "")
                self._error = ""
        # Wake the connect loop either way: a refusal is an answer, and waiting
        # out the full timeout on it would waste the whole budget before the
        # next broker got a turn.
        self._connected_event.set()
        if code != 0:
            return
        client.subscribe(self._up_topic, qos=1)

    def _on_disconnect(self, client, userdata, *args) -> None:
        with self._lock:
            self._connected_broker = ""
            self._phone_id = ""

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload
            topic = getattr(message, "topic", "")
            if topic != self._up_topic:
                return
            self.handle_blob(payload)
        except Exception:
            logger.debug("Companion tunnel dropped a frame", exc_info=True)

    # -------------------------------------------------------------- frames

    def _seal(self, inner: dict) -> bytes:
        raw = json.dumps(inner, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_INNER_BYTES:
            raise ValueError("inner frame too large")
        return pack_envelope(raw, self._key, self._clock(), max_frame=self._max_envelope)

    def _open(self, blob: bytes) -> dict | None:
        inner, reason = open_envelope_ex(blob, self._key, self._clock())
        if inner is None:
            with self._lock:
                self._error = f"envelope rejected ({reason})"
            return None
        try:
            frame = json.loads(inner.decode("utf-8"))
        except Exception:
            return None
        return frame if isinstance(frame, dict) else None

    def publish_event(self, name: str, data: dict) -> None:
        """Forward one runtime event to the phone, if it is one it can draw."""
        if name not in EVENT_NAMES:
            return
        client = self._client
        if client is None or not self._connected_broker:
            return
        try:
            blob = self._seal({"v": PROTOCOL_VERSION, "type": "evt", "name": name, "data": data})
            # QoS 1: the phone now uses these frames as its primary refresh
            # signal, and a dropped one shows up as a tab that lags a whole
            # poll behind.  The page ignores duplicates (throttled reloads), so
            # the broker's retransmit is harmless; requests stay QoS 1 for the
            # same reason on this channel.
            client.publish(self._down_topic, blob, qos=1)
            with self._lock:
                self._tx += 1
        except Exception:
            logger.debug("Companion tunnel event publish failed", exc_info=True)

    def handle_blob(self, blob: bytes) -> None:
        """Process one encrypted phone→desktop blob (also the test seam)."""
        frame = self._open(blob)
        if frame is None:
            return
        with self._lock:
            self._rx += 1
            self._last_seen = self._clock()
        kind = frame.get("type")
        if kind == "hello":
            self._handle_hello(frame)
        elif kind == "ping":
            self._reply({"v": PROTOCOL_VERSION, "type": "pong", "ts": self._clock()}, qos=0)
        elif kind == "req":
            self._handle_request(frame)
        # res/evt/hello_ack/pong are desktop→phone frames; a phone sending one
        # is malformed and ignored.

    def _handle_hello(self, frame: dict) -> None:
        phone_id = frame.get("phone_id")
        with self._lock:
            self._phone_id = str(phone_id or "")
            self._last_seen = self._clock()
        self._reply(
            {
                "v": PROTOCOL_VERSION,
                "type": "hello_ack",
                "computer_name": self._computer_name,
                "caps": ["status", "history", "favorites", "chat", "transfers"],
                "ts": self._clock(),
            },
            qos=1,
        )

    def _handle_request(self, frame: dict) -> None:
        request_id = str(frame.get("id") or "")
        method = str(frame.get("method") or "").upper()
        path = str(frame.get("path") or "")
        if not request_id or not method or not path:
            return
        with self._lock:
            cached = self._seen.get(request_id)
        if cached is not None:
            self._publish_raw(cached, qos=1)
            return
        now = self._clock()
        with self._lock:
            while self._rate and now - self._rate[0] > 60.0:
                self._rate.popleft()
            if len(self._rate) >= RATE_LIMIT_PER_MINUTE:
                response = {"v": PROTOCOL_VERSION, "type": "res", "id": request_id,
                            "status": 429, "body": ""}
            else:
                self._rate.append(now)
                response = None
        if response is None:
            response = self._perform(request_id, method, path, frame)
        try:
            raw = self._seal(response)
        except ValueError:
            # The response itself is over the frame budget.  It used to escape
            # as an exception into the message callback's catch-all, which
            # logged and returned — so the phone's request simply hung until it
            # timed out, with nothing to distinguish it from a dead tunnel.
            # Answer 413 instead, like the two size checks inside _perform.
            logger.warning(
                "Companion tunnel response for %s %s exceeds the frame budget",
                method,
                path,
            )
            response = {
                "v": PROTOCOL_VERSION,
                "type": "res",
                "id": request_id,
                "status": 413,
                "body": "",
            }
            raw = self._seal(response)
        with self._lock:
            self._seen[request_id] = raw
            while len(self._seen) > DEDUPE_CAPACITY:
                self._seen.popitem(last=False)
        self._publish_raw(raw, qos=1)

    def _perform(self, request_id: str, method: str, path: str, frame: dict) -> dict:
        if not is_allowed(method, path, self._mode):
            # 501, not 403: the phone page treats *any* 403 as "the token was
            # rotated" and pops a re-scan prompt.  This is a policy refusal, and
            # saying so is the difference between "remote mode does not support
            # this" and a false alarm about credentials.
            body = base64.b64encode(
                b'{"error":"remote_unsupported","mode":"' + self._mode.encode("ascii") + b'"}'
            ).decode("ascii")
            return {"v": PROTOCOL_VERSION, "type": "res", "id": request_id,
                    "status": 501, "body": body}
        try:
            body = base64.b64decode(str(frame.get("body") or ""), validate=True)
        except Exception:
            return {"v": PROTOCOL_VERSION, "type": "res", "id": request_id,
                    "status": 400, "body": ""}
        if len(body) > MAX_INNER_BYTES:
            return {"v": PROTOCOL_VERSION, "type": "res", "id": request_id,
                    "status": 413, "body": ""}
        try:
            status, response_body = self._http_request(method, path, body, {})
        except Exception:
            logger.debug("Companion tunnel request failed", exc_info=True)
            status, response_body = 503, b""
        if len(response_body) > MAX_INNER_BYTES:
            return {"v": PROTOCOL_VERSION, "type": "res", "id": request_id,
                    "status": 413, "body": ""}
        return {
            "v": PROTOCOL_VERSION,
            "type": "res",
            "id": request_id,
            "status": int(status),
            "body": base64.b64encode(response_body).decode("ascii"),
        }

    def _reply(self, frame: dict, *, qos: int) -> None:
        try:
            self._publish_raw(self._seal(frame), qos=qos)
        except Exception:
            logger.debug("Companion tunnel reply failed", exc_info=True)

    def _publish_raw(self, blob: bytes, *, qos: int) -> bool:
        client = self._client
        if client is None or not self._connected_broker:
            return False
        try:
            client.publish(self._down_topic, blob, qos=qos)
            with self._lock:
                self._tx += 1
            return True
        except Exception:
            return False
