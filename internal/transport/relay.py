"""Internet (cross-network) clipboard sync over public MQTT-over-WebSocket relays.

Design goals (user-mandated): zero cost, zero signup, no third-party client,
works behind NAT without port forwarding.  Peers talk to free public MQTT
brokers (an editable list, defaulting to broker.emqx.io / HiveMQ / Mosquitto).
The broker only ever sees two things:

  topic — ``clipsync/v1/<hash[:24]>`` derived from BOTH devices' relay secrets
          (unguessable: knowing only your own secret reveals nothing)
  data  — an AES-256-GCM ciphertext of a standard ClipSync frame; the key is
          derived from both secrets, so the relay never sees plaintext.

Secret enrollment: each device generates a random ``relay_secret`` once.  The
secrets reach already-paired peers over the existing TLS LAN channel via the
``relay_enroll`` message (sent by the sync manager on every successful
connect while internet sync is on), so paired devices need no extra setup.
Devices that have never met must pair once through the normal flow first.

Replay protection: every envelope carries a wall-clock timestamp; envelopes
outside +/- RELAY_TS_WINDOW seconds are dropped, and exact duplicate blobs are
dropped via a bounded LRU of recent ciphertext hashes.

Size cap: single envelopes larger than MAX_RELAY_PAYLOAD are refused (the
relay path is for clipboard text/images/small payloads; big files keep using
LAN transfers).

Everything here is deliberately decoupled from paho so the logic is testable
without a network: ``RelayTransport`` takes a ``client_factory`` and only the
thin adapter at the bottom touches paho-mqtt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import ssl
import threading
import time
from collections import OrderedDict
from typing import Callable

from internal.security.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)

TOPIC_PREFIX = "clipsync/v1/"
ENVELOPE_VERSION = 1
RELAY_TS_WINDOW = 300          # seconds of tolerated clock skew either way
MAX_RELAY_PAYLOAD = 256 * 1024  # refuse to carry anything larger than this
_SEEN_CAP = 512                 # recent ciphertext hashes remembered

# ── Internet pairing code (Round 14) ──────────────────────────────────────
# A self-contained shared-secret bootstrap for devices that have NEVER met:
# one device generates a short human-readable code (device tag + secret +
# checksum), the other types it in, and both derive the SAME netpair topic+key
# from the secret alone — no prior LAN pairing required.  The broker only ever
# sees an unguessable topic + ciphertext, exactly like LAN-relay enrollment,
# but the secret travels inside the code instead of over a TLS channel to an
# already-paired peer.
NETPAIR_TOPIC_PREFIX = "clipsync/net/v1/"
# 32-symbol alphabet (5 bits/char).  Removes the worst confusables 0/O/1/I so a
# code typed by hand fails loudly on a typo (checksum catches the rest).
NETPAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_NETPAIR_ALPHA_INDEX = {c: i for i, c in enumerate(NETPAIR_ALPHABET)}
NETPAIR_CODE_CHARS = 12        # "XXXX-XXXX-XXXX"
NETPAIR_DEVICE_CHARS = 4       # chars [0:4]  -> 20-bit device tag
NETPAIR_SECRET_CHARS = 7       # chars [4:11] -> 35-bit shared secret
NETPAIR_SECRET_BITS = NETPAIR_SECRET_CHARS * 5
# Optional user-set pairing passphrase layered on top of the code secret: the
# code still routes (topic) while the key strength becomes the passphrase's.
NETPAIR_PASSPHRASE_MIN = 12
NETPAIR_PASSPHRASE_MAX = 200

# Connection retry backoff across the broker list (seconds; capped).
BACKOFF_SEQUENCE = (1, 2, 4, 8, 15, 30, 60)
# How long to wait for the CONNACK/handshake before moving to the next broker.
CONNECT_TIMEOUT = 10.0
# How long stop() waits for the worker thread to exit before giving up.
STOP_JOIN_TIMEOUT = 5.0

STATE_OFF = "off"
STATE_CONNECTING = "connecting"
STATE_ONLINE = "online"
STATE_ERROR = "error"

try:  # optional dependency — absence degrades gracefully (state=error)
    import paho.mqtt.client as _mqtt
except Exception:  # pragma: no cover - exercised only without paho installed
    _mqtt = None


def generate_relay_secret() -> str:
    """A fresh per-device relay secret (hex, stored in config)."""
    import secrets as _secrets
    return _secrets.token_hex(32)


def _ca_bundle_path() -> str | None:
    """Path to a CA bundle usable by ``ssl``, or None to use system defaults.

    macOS system Pythons (and PyInstaller-frozen builds on any OS) often lack
    the OS trust store in OpenSSL's default search paths, so every ``wss://``
    handshake fails with ``CERTIFICATE_VERIFY_FAILED: unable to get local
    issuer certificate`` — the exact symptom on the macOS client.  certifi
    ships its own ``cacert.pem`` that PyInstaller bundles automatically (its
    hook collects the data file), so prefer it whenever it is installed.
    """
    try:
        import certifi
        return certifi.where()
    except Exception:
        return None


def build_paho_client():
    """Build one paho websocket client, or None when paho is missing."""

    if _mqtt is None:
        return None

    try:
        client = _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                              protocol=_mqtt.MQTTv311, transport="websockets")
    except AttributeError:  # paho 1.x
        client = _mqtt.Client(protocol=_mqtt.MQTTv311, transport="websockets")
    ca = _ca_bundle_path()
    if ca:
        # certifi is available — pin the CA bundle so TLS verification has a
        # trust store to check against on macOS / frozen builds.
        client.tls_set(ca_certs=ca, cert_reqs=ssl.CERT_REQUIRED)
    else:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    return client


def _sorted_pair(secret_a: str, secret_b: str) -> tuple[str, str]:
    a, b = sorted([secret_a or "", secret_b or ""])
    if not a or not b:
        raise ValueError("both relay secrets are required")
    return a, b


def derive_topic(secret_a: str, secret_b: str) -> str:
    """Unguessable MQTT topic for one pair of devices."""
    a, b = _sorted_pair(secret_a, secret_b)
    digest = hashlib.sha256((a + "|" + b).encode("ascii")).hexdigest()
    return TOPIC_PREFIX + digest[:24]


def derive_key(secret_a: str, secret_b: str) -> bytes:
    """AES-256 key both ends derive independently (HKDF-like, HMAC-SHA256)."""
    a, b = _sorted_pair(secret_a, secret_b)
    prk = hmac.new(b"clipsync-relay-salt", (a + "|" + b).encode("ascii"),
                   hashlib.sha256).digest()
    key = b""
    i = 1
    while len(key) < 32:
        key = hmac.new(prk, b"clipsync-relay-key" + bytes([i]),
                       hashlib.sha256).digest()
        i += 1
    return key[:32]


# --------------------------------------------------- internet pairing code

def _int_to_b32(value: int, nchars: int) -> str:
    """Render ``value`` (at most nchars*5 bits) as nchars base32 chars."""
    out = []
    for i in range(nchars - 1, -1, -1):
        out.append(NETPAIR_ALPHABET[(value >> (5 * i)) & 0x1F])
    return "".join(out)


def _b32_to_int(chars: str) -> int | None:
    value = 0
    for c in chars:
        i = _NETPAIR_ALPHA_INDEX.get(c)
        if i is None:
            return None
        value = (value << 5) | i
    return value


def _checksum_char(data11: str) -> str:
    """One base32 char derived from the first 11 code chars (typo detection)."""
    digest = hashlib.sha256(
        b"clipsync-netpair-check|" + data11.encode("ascii")).digest()
    return NETPAIR_ALPHABET[digest[0] & 0x1F]


def netpair_device_tag(device_id: str) -> str:
    """20-bit fingerprint of a device id -> 4 base32 chars (the code's device
    field).  Lossy by design: the code is compact, so the full 12-hex device id
    is not carried — the real identity is confirmed over the channel when the
    partner's hello frame arrives (its source_device is verified against this
    tag).
    """
    digest = hashlib.sha256(
        b"clipsync-netpair-device|"
        + str(device_id or "").strip().lower().encode("ascii"),
    ).digest()
    return _int_to_b32(int.from_bytes(digest[:3], "big") >> 4,
                       NETPAIR_DEVICE_CHARS)


def generate_netpair_secret() -> str:
    """A fresh 35-bit shared secret, rendered as 7 base32 chars."""
    import secrets as _secrets
    return _int_to_b32(_secrets.randbits(NETPAIR_SECRET_BITS),
                       NETPAIR_SECRET_CHARS)


def generate_netpair_code(device_id: str, secret: str) -> str:
    """Encode (device tag, secret) into a 12-char ``XXXX-XXXX-XXXX`` code."""
    tag = netpair_device_tag(device_id)
    if (not isinstance(secret, str) or len(secret) != NETPAIR_SECRET_CHARS
            or any(c not in _NETPAIR_ALPHA_INDEX for c in secret)):
        raise ValueError("invalid netpair secret")
    data = tag + secret                      # 11 chars: tag(4) + secret(7)
    code = data + _checksum_char(data)       # 12 chars: data(11) + checksum
    return f"{code[0:4]}-{code[4:8]}-{code[8:12]}"


def decode_netpair_code(code: str) -> tuple[str, str] | None:
    """Parse a pairing code back into ``(device_tag, secret)``, or None when
    the format, alphabet, length or checksum is wrong (typo on entry)."""
    if not isinstance(code, str):
        return None
    norm = "".join(ch for ch in code.upper() if ch not in " -")
    if len(norm) != NETPAIR_CODE_CHARS:
        return None
    if any(c not in _NETPAIR_ALPHA_INDEX for c in norm):
        return None
    data = norm[:11]
    if norm[11] != _checksum_char(data):
        return None
    tag = data[:NETPAIR_DEVICE_CHARS]
    secret = data[NETPAIR_DEVICE_CHARS:
                  NETPAIR_DEVICE_CHARS + NETPAIR_SECRET_CHARS]
    return tag, secret


def netpair_topic(secret: str) -> str:
    """Unguessable MQTT topic for one netpair shared secret."""
    digest = hashlib.sha256(
        b"clipsync-netpair-topic|" + secret.encode("ascii")).hexdigest()
    return NETPAIR_TOPIC_PREFIX + digest[:24]


def netpair_key(secret: str, password: str = "") -> bytes:
    """AES-256 key both ends derive independently (same HMAC style as relay).

    *password* optionally layers a user-set pairing passphrase on top of the
    35-bit code secret (see ``netpair_passphrase_error``): the code keeps doing
    the routing (the topic is still ``netpair_topic(secret)``) and doubles as a
    salt, while the actual key strength becomes the passphrase's — so brute-
    forcing the short code no longer reveals the key.  An empty password is the
    original secret-only derivation, byte-for-byte identical, so existing
    pairings and tests are unchanged.
    """
    keying = secret.encode("utf-8")
    if password:
        keying = keying + b"\x00" + password.encode("utf-8")
    prk = hmac.new(b"clipsync-netpair-salt", keying,
                   hashlib.sha256).digest()
    key = b""
    i = 1
    while len(key) < 32:
        key = hmac.new(prk, b"clipsync-netpair-key" + bytes([i]),
                       hashlib.sha256).digest()
        i += 1
    return key[:32]


def netpair_passphrase_error(pw: str) -> str | None:
    """Return an error tag for an invalid pairing passphrase, or None when it
    satisfies every rule: length within ``NETPAIR_PASSPHRASE_MIN`` .. ``MAX``,
    plus at least one of each of uppercase, lowercase, digit and a non-alnum
    non-space special character.  Enforced server-side on save so the strength
    rules can't be bypassed by editing the request body."""
    if not isinstance(pw, str):
        return "type"
    if not (NETPAIR_PASSPHRASE_MIN <= len(pw) <= NETPAIR_PASSPHRASE_MAX):
        return "length"
    if not any(c.isupper() for c in pw):
        return "upper"
    if not any(c.islower() for c in pw):
        return "lower"
    if not any(c.isdigit() for c in pw):
        return "digit"
    if not any(not c.isalnum() and not c.isspace() for c in pw):
        return "special"
    return None


def pack_envelope(frame_bytes: bytes, key: bytes, now: float) -> bytes:
    """Wrap an encoded ClipSync frame into an encrypted relay envelope."""
    if len(frame_bytes) > MAX_RELAY_PAYLOAD:
        raise ValueError(
            f"frame too large for relay: {len(frame_bytes)} > {MAX_RELAY_PAYLOAD}")
    ct = encrypt(frame_bytes, key)
    env = {
        "v": ENVELOPE_VERSION,
        "ts": round(float(now), 3),
        "data": base64.b64encode(ct).decode("ascii"),
    }
    return json.dumps(env, separators=(",", ":")).encode("ascii")


def open_envelope(blob: bytes, key: bytes, now: float) -> bytes | None:
    """Decrypt+validate an envelope. Returns the inner frame bytes, or None."""
    try:
        env = json.loads(blob.decode("ascii"))
    except Exception:
        return None
    if not isinstance(env, dict) or env.get("v") != ENVELOPE_VERSION:
        return None
    ts = env.get("ts")
    if not isinstance(ts, (int, float)) or abs(now - float(ts)) > RELAY_TS_WINDOW:
        logger.debug("Relay envelope rejected (timestamp out of window)")
        return None
    try:
        ct = base64.b64decode(env.get("data", ""))
    except Exception:
        return None
    return decrypt(ct, key)


def probe_relay_endpoint(endpoint: str, timeout: float = 4.0) -> dict:
    """Probe one relay broker endpoint with a TCP (+TLS) handshake.

    A pure connectivity check — opens its own socket and never touches the
    live ``RelayTransport`` client, so clicking "test connection" cannot
    disturb an active relay session.  Returns:
    ``{"endpoint", "ok", "latency_ms", "detail"}`` and never raises.
    """
    try:
        scheme, _, rest = endpoint.partition("://")
        host_port, _, _path = rest.partition("/")
        host, _, port = host_port.partition(":")
        port = int(port or 8884)
    except Exception:
        return {"endpoint": endpoint, "ok": False, "latency_ms": None, "detail": "invalid endpoint"}
    if not host:
        return {"endpoint": endpoint, "ok": False, "latency_ms": None, "detail": "invalid endpoint"}
    use_tls = scheme.lower() in ("wss", "ssl", "tls", "mqtts")
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            if use_tls:
                # Same CA-bundle strategy as build_paho_client(): a default
                # context on macOS / frozen builds can't find the OS trust
                # store and would report every wss:// endpoint as unreachable.
                ca = _ca_bundle_path()
                ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host):
                    pass  # handshake completed — reachable
        latency_ms = round((time.time() - started) * 1000.0, 1)
        return {"endpoint": endpoint, "ok": True, "latency_ms": latency_ms, "detail": "reachable"}
    except socket.timeout:
        return {"endpoint": endpoint, "ok": False, "latency_ms": None, "detail": "timeout"}
    except Exception as e:
        detail = str(e) or type(e).__name__
        return {"endpoint": endpoint, "ok": False, "latency_ms": None, "detail": detail[:120]}


class RelayTransport:
    """Connection lifecycle + pub/sub over a failover list of MQTT brokers.

    Parameters:
      brokers           -- list of ``wss://host:port[/path]`` endpoints
      get_channels      -- returns {topic: key}; re-read on reconnect and when
                           ``refresh_channels`` is called (new enrollments)
      on_frame          -- called with (inner frame bytes, topic) for received
                           frames; the topic lets the owner map a frame back to
                           the channel/secret it arrived on (netpair handshake)
      on_state          -- called with one of STATE_* on every change
      client_factory    -- returns a paho-compatible client, or None when the
                           optional dependency is missing (tests inject fakes)
      sleeper           -- called between retries (tests inject instant sleep);
                           defaults to a stop-interruptible sleep
    """

    def __init__(
        self,
        brokers: list[str],
        get_channels: Callable[[], dict[str, bytes]],
        on_frame: Callable[[bytes], None],
        on_state: Callable[[str], None],
        client_factory: Callable | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self._brokers = [b for b in brokers if isinstance(b, str) and b]
        self._get_channels = get_channels
        self._on_frame = on_frame
        self._on_state = on_state
        self._client_factory = client_factory or build_paho_client
        self._sleeper = sleeper if sleeper is not None else self._interruptible_sleep
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._client = None
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._subscribed: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._connected_on_broker: int | None = None
        self._state = STATE_OFF

    # ------------------------------------------------------------- state --
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state == state:
                return
            self._state = state
        try:
            self._on_state(state)
        except Exception:
            logger.debug("state callback failed", exc_info=True)

    # ------------------------------------------------------------ public --
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if not self._brokers:
                self._set_state(STATE_ERROR)
                return
            self._stop.clear()
            self._wakeup.clear()   # a prior stop() left it set; don't let the
                                   # fresh worker inherit a spurious wakeup
            self._set_state(STATE_CONNECTING)
            self._thread = threading.Thread(
                target=self._run, name="relay-sync", daemon=True)

        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        with self._lock:
            client, self._client = self._client, None
            thread = self._thread
            self._thread = None
            self._subscribed.clear()
            self._connected_on_broker = None
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                logger.debug("relay disconnect during stop failed", exc_info=True)
        if thread is not None:
            # Wait (bounded) for the worker to notice _stop and exit before a
            # rapid stop() → start() restart can spawn a second worker running
            # alongside the old one (which would still be mid-CONNACK wait or
            # backoff).  The worker's waits are all stop-aware, so this returns
            # promptly in practice and only falls through on the timeout if a
            # blocking broker call itself never returns.
            thread.join(timeout=STOP_JOIN_TIMEOUT)
        self._set_state(STATE_OFF)

    def restart(self) -> None:
        """Re-read brokers/channels after a settings change."""
        self.stop()
        self.start()

    def refresh_channels(self) -> None:
        """Channel set changed — resubscribe without dropping the link.

        Subscribes any topic that is not yet subscribed AND unsubscribes any
        topic that is no longer in the current channel set (e.g. an internet
        pair was removed), so a stale subscription cannot keep delivering
        frames from an unpaired peer.
        """
        with self._lock:
            client = self._client
        if client is None:
            return
        channels = self._safe_channels()
        for topic, key in channels.items():
            if topic in self._subscribed:
                continue
            try:
                client.subscribe(topic)
                self._subscribed.add(topic)
            except Exception:
                logger.debug("subscribe %s failed", topic, exc_info=True)
        for topic in list(self._subscribed):
            if topic in channels:
                continue
            try:
                client.unsubscribe(topic)
            except Exception:
                logger.debug("unsubscribe %s failed", topic, exc_info=True)
            self._subscribed.discard(topic)

    def publish(self, frame_bytes: bytes, topic: str, key: bytes) -> bool:
        """Publish one encoded frame to ``topic``. False if currently offline."""
        with self._lock:
            client = self._client
            # Gate on a *confirmed* connection (CONNACK received), not just the
            # last remembered state: after a disconnect the state may lag until
            # the worker reconnects, and publish() must not report frames as
            # sent into a dead link.
            online = self._connected_on_broker is not None and client is not None
        if not online:
            return False
        try:
            blob = pack_envelope(frame_bytes, key, time.time())
        except ValueError:
            logger.debug("relay publish skipped (oversized frame)")
            return False
        except Exception:
            logger.debug("relay pack failed", exc_info=True)
            return False
        try:
            info = client.publish(topic, blob, qos=0)
            return getattr(info, "rc", 0) == 0
        except Exception:
            logger.debug("relay publish failed", exc_info=True)
            return False

    # ------------------------------------------------------------- worker --
    def _safe_channels(self) -> dict[str, bytes]:
        try:
            return dict(self._get_channels() or {})
        except Exception:
            logger.debug("get_channels failed", exc_info=True)
            return {}

    def _interruptible_sleep(self, delay: float) -> None:
        """Default between-retry sleep; stop() can interrupt it via _wakeup.

        A plain ``time.sleep(delay)`` here would let the worker miss a stop()
        that lands mid-backoff and keep running alongside the restarted one;
        waiting on ``_wakeup`` (set by stop()) returns as soon as the transport
        is stopping, so restart()'s join actually sees the old thread exit.
        """
        self._wakeup.wait(timeout=delay)
        self._wakeup.clear()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            for index, endpoint in enumerate(self._brokers):
                if self._stop.is_set():
                    return
                try:
                    ok = self._connect_one(index)
                except Exception:
                    # Never let an unexpected setup/connect error kill the
                    # worker thread silently (a dead thread leaves the state
                    # stuck on "connecting" with no recovery path).
                    logger.warning("relay broker #%d raised during connect",
                                   index, exc_info=True)
                    ok = False
                if ok:
                    attempt = 0
                    self._serve_until_lost(index)
                    if self._stop.is_set():
                        return
                    break  # connection lost — restart from the first broker
            else:
                attempt += 1
            delay = BACKOFF_SEQUENCE[min(attempt, len(BACKOFF_SEQUENCE) - 1)]
            if not self._stop.is_set():
                self._set_state(STATE_CONNECTING)
                self._sleeper(delay)
        # all brokers failing permanently is signalled by the owner via stop()

    def _parse_endpoint(self, endpoint: str) -> tuple[str, int, str] | None:
        try:
            rest = endpoint.split("://", 1)[1]
            host_port, _, path = rest.partition("/")
            host, _, port = host_port.partition(":")
            if not host:
                return None
            return host, int(port or 8884), "/" + path.lstrip("/") if path else "/mqtt"
        except Exception:
            return None

    def _connect_one(self, index: int) -> bool:
        endpoint = self._brokers[index]
        parsed = self._parse_endpoint(endpoint)
        if parsed is None:
            logger.warning("Invalid relay broker endpoint: %r", endpoint)
            return False
        host, port, path = parsed
        with self._lock:
            old_client, self._client = self._client, None
            self._connected_on_broker = None
        if old_client is not None:
            # A previous attempt's client may still be running (loop_start()
            # spawned its own network thread).  Stop it before installing the
            # new one — an orphaned client leaks a thread + socket, and its
            # late on_connect would otherwise satisfy the next CONNACK wait
            # with a stale "connected" and churn failover.
            try:
                old_client.loop_stop()
                old_client.disconnect()
            except Exception:
                logger.debug("relay client cleanup failed", exc_info=True)
        client = self._client_factory()
        if client is None:
            logger.warning("paho-mqtt not available — internet sync disabled")
            self._set_state(STATE_ERROR)
            self._stop.set()
            return False
        client.on_connect = self._make_on_connect(index)
        client.on_disconnect = self._make_on_disconnect(index)
        client.on_message = self._on_message
        with self._lock:
            self._client = client
        try:
            # NOTE: TLS is configured inside build_paho_client() (the factory).
            # Calling client.tls_set() again here raises
            # "SSL/TLS has already been configured" on paho 2.x, which killed
            # the relay thread on real paho (unit fakes never noticed).  Only
            # the websocket path is transport-specific and set per-endpoint.
            client.ws_set_options(path=path)
            client.connect(host, port, keepalive=45)
            client.loop_start()
        except Exception:
            logger.warning("relay connect to %s:%s failed", host, port,
                           exc_info=True)
            with self._lock:
                self._client = None
            return False
        # paho's connect() returns before CONNACK; on_connect fires from the
        # network thread later.  Wait here so the retry loop doesn't treat
        # "handshake in flight" as "connection lost" and hot-reconnect.
        deadline = time.time() + CONNECT_TIMEOUT
        while time.time() < deadline:
            with self._lock:
                if self._connected_on_broker == index:
                    return True
                if self._client is not client or self._stop.is_set():
                    return False  # stop()/restart replaced us
            time.sleep(0.05)
        logger.debug("relay handshake timeout to broker #%d (%s:%s)", index, host, port)
        with self._lock:
            if self._client is client:
                self._client = None
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            logger.debug("cleanup after handshake timeout failed", exc_info=True)
        return False

    def _serve_until_lost(self, index: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                connected = self._connected_on_broker == index
            if connected:
                self._wakeup.wait(timeout=1.0)
                self._wakeup.clear()
            else:
                break

    # ----------------------------------------------------------- callbacks --
    def _make_on_connect(self, index: int):
        def _on_connect(client, userdata, flags, reason_code, properties=None):
            try:
                ok = int(reason_code) == 0
            except Exception:
                ok = str(reason_code) in ("0", "Success")
            if not ok:
                logger.debug("broker #%d refused connection: %s", index, reason_code)
                return
            with self._lock:
                self._connected_on_broker = index
                self._subscribed = set(self._safe_channels())
            for topic in list(self._subscribed):
                try:
                    client.subscribe(topic)
                except Exception:
                    logger.debug("subscribe %s failed", topic, exc_info=True)
            logger.info("Relay online via broker #%d", index)
            self._set_state(STATE_ONLINE)
        return _on_connect

    def _make_on_disconnect(self, index: int):
        def _on_disconnect(client, userdata, *args):
            was_current = False
            with self._lock:
                # Only a drop of the *installed* client counts — a stale
                # callback from a retired client (or one mid-cleanup) must not
                # pull a healthy new connection back out of ONLINE.
                if self._client is client and self._connected_on_broker == index:
                    self._connected_on_broker = None
                    was_current = True
            if was_current:
                # An observed drop (TCP close or keepalive timeout) must leave
                # ONLINE immediately — on a silent partition publish() would
                # otherwise keep reporting frames as sent while they are being
                # silently dropped.  CONNECTING makes publish() return False so
                # frames queue/retry instead of being reported as delivered.
                self._set_state(STATE_CONNECTING)
            logger.debug("relay disconnected from broker #%d", index)
        return _on_disconnect

    def _on_message(self, client, userdata, msg):
        channels = self._safe_channels()
        key = channels.get(msg.topic)
        if key is None:
            return
        blob_hash = hashlib.sha256(bytes(msg.payload)).hexdigest()
        with self._lock:
            if blob_hash in self._seen:
                return  # exact duplicate (broker redelivery / our own echo)
            self._seen[blob_hash] = None
            while len(self._seen) > _SEEN_CAP:
                self._seen.popitem(last=False)
        frame = open_envelope(bytes(msg.payload), key, time.time())
        if frame is None:
            logger.debug("relay frame dropped (auth/window/format)")
            return
        try:
            self._on_frame(frame, msg.topic)
        except Exception:
            logger.debug("relay on_frame callback failed", exc_info=True)
