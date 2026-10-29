/*
 * Companion remote transport for the https://*.pages.dev copy of the phone page.
 *
 * The phone page normally talks to the desktop's HTTP server on the same
 * network.  Loaded from Cloudflare Pages there is no such server, so this file
 * carries the same API over an MQTT broker as an encrypted request/response
 * tunnel.  Load order matters: the build puts this before the page's own script
 * so `window.fetch` is already wrapped when the page starts polling.
 *
 * It is a plain ES5-style script (no modules, no build step) because it is
 * served as a static asset next to mobile.html.  It implements just enough
 * MQTT 3.1.1 over WebSocket (CONNECT/SUBSCRIBE/PUBLISH QoS1/ACK/PING) to hold
 * one tunnel; the protocol and crypto must match
 * internal/transport/companion_tunnel.py exactly.
 */
(function () {
  'use strict';

  var PREFIX = new Uint8Array([0x01, 0x43, 0x42, 0x45]);
  var PROTOCOL_VERSION = 1;
  var REQUEST_TIMEOUT_MS = 8000;
  var KEEPALIVE_SECONDS = 30;
  /*
   * How long the raced brokers get to produce the computer once they have all
   * answered (or been written off).  A CONNACK only says the phone reached *a*
   * broker; the desktop sits on one of the configured brokers and public
   * brokers do not federate.  Every broker is dialled at once and the first
   * one whose sealed `hello_ack` comes back wins -- a live computer answers in
   * one round trip, so waiting longer than this is waiting for nobody.
   */
  var HELLO_TIMEOUT_MS = 6000;
  /*
   * The link can also go quiet after the handshake: a cell handover, the
   * computer going to sleep, or the desktop tunnel moving to another broker.
   * MQTT's PINGRESP only proves the broker is still there, so the page asks
   * the computer itself with the protocol's own `ping` frame.  Two unanswered
   * pings in a row rotate to the next broker; one lost pong is not a
   * disconnect.
   *
   * Five seconds between checks keeps the visible outage on a real broker
   * move to ~10-15 s: the desktop's tunnel really does hop between public
   * brokers when one drops it, and every hop used to mean half a minute of
   * "中继正在连接" on the phone.  A ping is ~40 bytes and does not count
   * against the desktop's request budget.
   */
  var PRESENCE_INTERVAL_MS = 5000;
  var PRESENCE_MISSES = 2;
  /*
   * How long one broker gets to answer MQTT CONNECT with a CONNACK.  Without
   * this the page had no upper bound at all: on a mobile network a broker
   * whose WebSocket port is blocked leaves the browser in CONNECTING, where no
   * `close` event ever arrives, so a hung socket would sit in the race forever
   * and hold the deadline open for the brokers that are working.
   */
  var CONNECT_TIMEOUT_MS = 10000;
  /*
   * One upper bound for a whole race, on top of the per-broker watchdogs.  A
   * public broker occasionally accepts the TCP/TLS connection and then never
   * sends CONNACK; each such attempt is abandoned after CONNECT_TIMEOUT_MS,
   * and without a cap the first attempt could spend every one of those
   * deadlines plus the hello budget before the page retried -- the "it just
   * keeps spinning" case.  A retry re-dials every broker, which is what pulled
   * the worst observed load back from ~18 s to the cost of a fresh handshake.
   */
  var RACE_TIMEOUT_MS = 12000;
  var RECONNECT_BASE_MS = 1500;
  var RECONNECT_MAX_MS = 15000;
  var SALT = 'clipsync-companion-v1';
  var INFO_STRING = 'channel-key';

  function bytesToB64(bytes) {
    var text = '';
    for (var i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
    return btoa(text);
  }

  function b64ToBytes(value) {
    var raw = atob(value);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function bytesToHex(bytes) {
    var text = '';
    for (var i = 0; i < bytes.length; i++) {
      var byte = bytes[i].toString(16);
      text += byte.length === 1 ? '0' + byte : byte;
    }
    return text;
  }

  // Python's repr() of the float on the other end of the MAC.  JavaScript's
  // Number#toString() is also the shortest round-trip form, but it drops the
  // trailing ".0" that repr() keeps for an integral float.  The timestamp is
  // rounded to milliseconds first, so only that one difference is reachable.
  function pythonFloatRepr(value) {
    var text = String(value);
    if (text.indexOf('.') === -1 && text.indexOf('e') === -1 && text.indexOf('E') === -1) {
      text += '.0';
    }
    return text;
  }

  function utf8(text) { return new TextEncoder().encode(text); }
  function fromUtf8(bytes) { return new TextDecoder().decode(bytes); }

  function concat(parts) {
    var size = 0, i;
    for (i = 0; i < parts.length; i++) size += parts[i].length;
    var out = new Uint8Array(size), at = 0;
    for (i = 0; i < parts.length; i++) { out.set(parts[i], at); at += parts[i].length; }
    return out;
  }

  // ---------------------------------------------------------------- parsing

  function parseFragment() {
    var hash = (location.hash || '').replace(/^#/, '');
    if (!hash) return null;
    var params = new URLSearchParams(hash);
    if (params.get('v') !== '1') return null;
    var token = params.get('t') || '';
    if (!token) return null;
    var brokers = [];
    try {
      var raw = params.get('b') || '';
      // `b` is URLSearchParams-decoded already; it is a JSON array so a comma in
      // a URL is preserved.
      brokers = JSON.parse(raw);
    } catch (e) { return null; }
    if (!Array.isArray(brokers) || !brokers.length) return null;
    return { token: token, brokers: brokers.filter(function (b) { return typeof b === 'string' && b; }) };
  }

  // The page reads its token from location.search (`params.get('token')`), so
  // move the token into the query before its script runs.  The token stays in
  // the address bar only long enough for that script; after load both the query
  // value and the fragment are removed, so a screenshot, a shared URL or the
  // browser's own history does not carry the secret.
  //
  // The pairing is kept in *both* sessionStorage and localStorage.  Session
  // storage covers a reload in the tab that scanned the code; localStorage is
  // what makes a bookmark, a home-screen icon or a reopened browser tab work
  // without scanning the QR code again -- those all open the bare page URL,
  // which has no fragment to read.  Local storage is cleared as soon as the
  // computer answers 403 (a rotated token), so a stale pairing cannot outlive
  // the credential it was made with.
  var SESSION_KEY = 'clipsync-remote-session';
  var PERSIST_KEY = 'clipsync-remote-pairing-v1';

  function saveSession(config) {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(config));
    } catch (e) {}
  }

  function loadStored(key, storage) {
    try {
      var raw = storage.getItem(key);
      if (!raw) return null;
      var config = JSON.parse(raw);
      if (!config || !config.token || !Array.isArray(config.brokers) || !config.brokers.length) {
        return null;
      }
      return { token: config.token, brokers: config.brokers };
    } catch (e) {
      return null;
    }
  }

  function loadSession() {
    return loadStored(SESSION_KEY, sessionStorage);
  }

  function savePersistent(config) {
    try {
      localStorage.setItem(PERSIST_KEY, JSON.stringify({ token: config.token, brokers: config.brokers }));
    } catch (e) {}
  }

  function loadPersistent() {
    return loadStored(PERSIST_KEY, localStorage);
  }

  function forgetPersistent() {
    try { localStorage.removeItem(PERSIST_KEY); } catch (e) {}
    try { sessionStorage.removeItem(SESSION_KEY); } catch (e) {}
  }

  function adoptToken(token) {
    try {
      var url = new URL(location.href);
      if (!url.searchParams.get('token')) {
        url.searchParams.set('token', token);
        history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
    } catch (e) {}
  }

  function forgetTokenInAddressBar() {
    try {
      var url = new URL(location.href);
      if (!url.searchParams.has('token') && !url.hash) return;
      url.searchParams.delete('token');
      history.replaceState(null, '', url.pathname + url.search);
    } catch (e) {}
  }

  // ---------------------------------------------------------------- crypto

  function Channel(token) {
    this.token = token;
    this.key = null;
    // The timestamp MAC needs the raw derived bits; the AES key object cannot
    // be asked for them again.
    this.macKey = null;
  }

  Channel.prototype.ready = function () {
    var self = this;
    if (self.key && self.macKey) return Promise.resolve(self.key);
    var subtle = (window.crypto && crypto.subtle) || null;
    if (!subtle) return Promise.reject(new Error('remote needs a secure context (https)'));
    var usages = ['deriveBits'];
    return subtle.importKey('raw', utf8(self.token), { name: 'HKDF' }, false, usages)
      .then(function (ikm) {
        return subtle.deriveBits({
          name: 'HKDF', hash: 'SHA-256', salt: utf8(SALT), info: utf8(INFO_STRING),
        }, ikm, 256);
      })
      .then(function (bits) {
        return Promise.all([
          subtle.importKey('raw', bits, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']),
          subtle.importKey('raw', bits, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']),
        ]);
      })
      .then(function (keys) {
        self.key = keys[0];
        self.macKey = keys[1];
        return self.key;
      });
  };

  Channel.prototype.seal = function (inner) {
    var self = this;
    return self.ready().then(function (key) {
      var iv = crypto.getRandomValues(new Uint8Array(12));
      var plain = utf8(JSON.stringify(inner));
      // Same rounding as `pack_envelope`: both ends MAC the value that is
      // actually on the wire, not the one the clock produced.
      var ts = Math.round(Date.now()) / 1000;
      return crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv }, key, plain).then(function (ct) {
        return crypto.subtle.sign(
          'HMAC', self.macKey, utf8('relay-ts' + pythonFloatRepr(ts)),
        ).then(function (mac) {
          return utf8(JSON.stringify({
            v: 1,
            ts: ts,
            data: bytesToB64(concat([PREFIX, iv, new Uint8Array(ct)])),
            mac: bytesToHex(new Uint8Array(mac)),
          }));
        });
      });
    });
  };

  Channel.prototype.open = function (blob) {
    var self = this;
    return self.ready().then(function (key) {
      var envelope = JSON.parse(fromUtf8(blob));
      if (!envelope || envelope.v !== 1) throw new Error('bad envelope');
      var sealed = b64ToBytes(envelope.data || '');
      for (var i = 0; i < PREFIX.length; i++) {
        if (sealed[i] !== PREFIX[i]) throw new Error('bad envelope prefix');
      }
      var iv = sealed.slice(PREFIX.length, PREFIX.length + 12);
      var ct = sealed.slice(PREFIX.length + 12);
      return crypto.subtle.decrypt({ name: 'AES-GCM', iv: iv }, key, ct)
        .then(function (plain) { return JSON.parse(fromUtf8(new Uint8Array(plain))); });
    });
  };

  // ---------------------------------------------------------------- MQTT

  function mqttString(text) {
    var raw = utf8(text);
    var out = new Uint8Array(2 + raw.length);
    out[0] = raw.length >> 8;
    out[1] = raw.length & 0xff;
    out.set(raw, 2);
    return out;
  }

  function mqttRemainingLength(length) {
    var out = [];
    do {
      var byte = length % 128;
      length = Math.floor(length / 128);
      if (length > 0) byte |= 0x80;
      out.push(byte);
    } while (length > 0);
    return new Uint8Array(out);
  }

  function mqttPacket(header, parts) {
    var body = concat(parts);
    return concat([new Uint8Array([header]), mqttRemainingLength(body.length), body]);
  }

  function mqttConnectPacket(broker) {
    var flags = 0x02; // clean session
    var payload = [mqttString('clipsync-phone-' + Math.random().toString(36).slice(2, 10))];
    if (broker.username) {
      flags |= 0x80;
      payload.push(mqttString(broker.username));
      if (broker.password) {
        flags |= 0x40;
        payload.push(mqttString(broker.password));
      }
    }
    return mqttPacket(0x10, [
      mqttString('MQTT'), new Uint8Array([4, flags, 0, KEEPALIVE_SECONDS]),
      concat(payload),
    ]);
  }

  function mqttPublishPacket(topic, packetId, blob) {
    var body = packetId
      ? concat([mqttString(topic), new Uint8Array([packetId >> 8, packetId & 0xff]), blob])
      : concat([mqttString(topic), blob]);
    return mqttPacket(packetId ? 0x32 : 0x30, [body]);
  }

  /*
   * Take every complete MQTT packet out of `holder.inbox` and hand it to
   * `onPacket(packet, headerLength)`.  The raced probes and the live tunnel
   * read their sockets through the same framing rules; only what a packet
   * means differs.
   */
  function drainPackets(holder, onPacket) {
    while (holder.inbox.length >= 2) {
      var multiplier = 1, length = 0, index = 1, byte;
      do {
        if (index >= holder.inbox.length) return;
        byte = holder.inbox[index++];
        length += (byte & 0x7f) * multiplier;
        multiplier *= 128;
        if (multiplier > 128 * 128 * 128) return;
      } while (byte & 0x80);
      var total = index + length;
      if (holder.inbox.length < total) return;
      var packet = holder.inbox.slice(0, total);
      holder.inbox = holder.inbox.slice(total);
      onPacket(packet, index);
    }
  }

  function BrokerUrl(url) {
    var parsed = new URL(url.indexOf('://') === -1 ? 'wss://' + url : url);
    this.href = parsed.href;
    this.secure = parsed.protocol === 'wss:' || parsed.protocol === 'mqtts:';
    this.path = parsed.pathname || '/mqtt';
    this.url = (this.secure ? 'wss://' : 'ws://') + parsed.host + this.path;
    this.username = decodeURIComponent(parsed.username || '');
    this.password = decodeURIComponent(parsed.password || '');
  }

  function Tunnel(cfg, channel) {
    this.cfg = cfg;
    this.channel = channel;
    this.brokers = cfg.brokers.map(function (url) { return new BrokerUrl(url); });
    this.brokerIndex = 0;
    // Start where the computer was last found.  The remembered URL is matched
    // against the parsed list (not stored as an index) because the QR code's
    // broker list can be reordered between visits.
    var preferred = loadLastBroker();
    for (var preferredAt = 0; preferredAt < this.brokers.length; preferredAt++) {
      if (this.brokers[preferredAt].url === preferred) {
        this.brokerIndex = preferredAt;
        break;
      }
    }
    this.ws = null;
    this.connected = false;
    this.packetId = 1;
    this.pending = {};
    this.inbox = new Uint8Array(0);
    this.pingTimer = null;
    this.reconnectTimer = null;
    this.connectTimer = null;
    // Which socket attempt this is.  `close` and `error` arrive
    // asynchronously and can belong to a socket the page has already given up
    // on; a handler whose number is stale must not tear down the live one.
    this.generation = 0;
    // Consecutive failures, for the retry backoff.  Reset by every CONNACK.
    this.attempts = 0;
    // One PINGREQ is allowed in flight at a time: a broker that never answers
    // it is a half-open socket, not a slow one.
    this.pingOutstanding = false;
    // `ready` is the MQTT handshake: the broker accepted us and the down topic
    // is subscribed.  `connected` is the stronger claim the page renders as
    // "online": the computer answered hello on this broker.  Keeping them
    // apart is what stops a CONNACK from being reported as a usable tunnel.
    this.ready = false;
    // Presence check while online -- see PRESENCE_INTERVAL_MS.
    this.presenceTimer = null;
    this.pongOutstanding = false;
    this.pongMisses = 0;
    this.computerName = '';
    this.lastFailure = '';
    this.upTopic = '';
    this.downTopic = '';
    this.onlineHandlers = [];
    // What the page shows while the tunnel is being established.  The page had
    // only two states to render -- "loaded" and "failed" -- so a phone that
    // was still dialling broker #2 looked exactly like a computer that was
    // off.  These are the states in between.
    this.stateHandlers = [];
    this.state = null;
    this._onlineWaiters = [];
    // Runtime events the desktop pushes over the same sealed channel (chat
    // messages, clipboard changes, transfers...).  The page subscribes to
    // these so it can refresh on the event instead of polling for it.
    this.eventHandlers = [];
  }

  Tunnel.prototype.report = function (state, extra) {
    var payload = { state: state };
    if (extra) {
      for (var key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) payload[key] = extra[key];
      }
    }
    this.state = payload;
    for (var i = 0; i < this.stateHandlers.length; i++) {
      try { this.stateHandlers[i](payload); } catch (e) {}
    }
  };

  /** Subscribe and receive the current state immediately if there is one. */
  Tunnel.prototype.onState = function (handler) {
    this.stateHandlers.push(handler);
    if (this.state) {
      try { handler(this.state); } catch (e) {}
    }
  };

  /*
   * Push events from the desktop: `{type:"evt", name, data}` frames on the
   * down topic.  QoS 0 and best effort by design -- the page's fallback poll
   * (and a resync on reconnect) covers anything a lost event would have said,
   * which is why this is a refresh signal rather than the only source of truth.
   */
  Tunnel.prototype.onEvent = function (handler) {
    this.eventHandlers.push(handler);
  };

  Tunnel.prototype.emitEvent = function (name, data) {
    for (var i = 0; i < this.eventHandlers.length; i++) {
      try { this.eventHandlers[i](name, data); } catch (e) {}
    }
  };

  Tunnel.prototype.start = function () {
    var self = this;
    return self.topics().then(function (topics) {
      self.upTopic = topics[0];
      self.downTopic = topics[1];
      self.connect();
    });
  };

  /*
   * Where the computer was last found.
   *
   * The desktop's broker is not a property of the QR code: it is whichever
   * public broker its own tunnel happened to get into, and it can move.  Every
   * broker that is not that one costs a hello timeout, so a page that
   * re-learns it on every visit makes the reader wait for a handshake it just
   * performed.  Starting from the remembered broker makes the common case one
   * round trip; when the memory is stale it costs exactly one hello timeout
   * before the normal rotation finds the computer again, so it is never worse
   * than starting from the top of the list.
   */
  var LAST_BROKER_KEY = 'clipsync-remote-last-broker';

  function loadLastBroker() {
    try { return localStorage.getItem(LAST_BROKER_KEY) || ''; } catch (e) { return ''; }
  }

  function saveLastBroker(url) {
    try { localStorage.setItem(LAST_BROKER_KEY, url || ''); } catch (e) {}
  }

  Tunnel.prototype.topics = function () {
    var self = this;
    if (self._topics) return Promise.resolve(self._topics);
    return crypto.subtle.digest('SHA-256', utf8('clipsync-companion-topic|' + self.cfg.token))
      .then(function (digest) {
        var hex = Array.prototype.map.call(new Uint8Array(digest), function (byte) {
          return ('0' + byte.toString(16)).slice(-2);
        }).join('');
        self._topics = [
          'clipsync/comp/v1/' + hex.slice(0, 24) + '/up',
          'clipsync/comp/v1/' + hex.slice(0, 24) + '/down',
        ];
        return self._topics;
      });
  };

  Tunnel.prototype.connect = function () {
    var self = this;
    if (!self.brokers.length) return;
    if (self.connectTimer) { clearTimeout(self.connectTimer); self.connectTimer = null; }
    var generation = ++self.generation;
    // Each attempt is a new MQTT stream: a half-parsed packet left over from
    // the socket just given up on would be read as the head of the next one.
    self.inbox = new Uint8Array(0);
    self.ready = false;
    self.stopPresence();
    if (self.ws) {
      try { self.ws.close(); } catch (e) {}
      self.ws = null;
    }
    // The remembered broker goes first in the dial order (see LAST_BROKER_KEY),
    // although every broker is dialled at once: the order only decides which
    // handshake the status line is allowed to name first.
    var start = self.brokerIndex % self.brokers.length;
    self.brokerIndex += 1;
    var ordered = [];
    for (var i = 0; i < self.brokers.length; i++) {
      ordered.push(self.brokers[(start + i) % self.brokers.length]);
    }
    self.report('connecting', { parallel: true, total: ordered.length });
    self._race(generation, ordered);
  };

  /*
   * The race won: make one socket the live tunnel.
   *
   * The winner has already done CONNECT/CONNACK, SUBSCRIBE and hello, so this
   * only swaps the probe handlers for the tunnel's own and starts the timers
   * that assume an established channel.
   */
  Tunnel.prototype._attachLiveSocket = function (ws, generation) {
    var self = this;
    self.ws = ws;
    ws.onopen = function () {};
    ws.onmessage = function (event) {
      if (generation !== self.generation) return;
      self.feed(new Uint8Array(event.data));
    };
    ws.onclose = function () {
      if (generation !== self.generation) return;
      self.ready = false;
      self.markOffline();
      if (self.connectTimer) { clearTimeout(self.connectTimer); self.connectTimer = null; }
      self.stopPing();
      self.stopPresence();
      self.lastFailure = 'closed';
      self.retry(generation);
    };
    ws.onerror = function () {
      if (generation !== self.generation) return;
      try { ws.close(); } catch (e) {}
    };
  };

  /*
   * Dial every configured broker at once and keep the one the computer answers
   * on.
   *
   * Public brokers are independent instances and the desktop is on exactly one
   * of them.  Dialling in order and giving each one a full hello timeout made
   * the first page load the sum of several handshakes (12-20 s of "connecting"
   * was typical on a phone); racing them makes it the cost of the *fastest*
   * handshake, and the losers cost nothing but a closed socket.  The first
   * sealed hello_ack wins; every other attempt is closed on the spot.
   */
  Tunnel.prototype._race = function (generation, brokers) {
    var self = this;
    var attempts = [];
    var winner = null;
    var helloArmed = false;
    var verifyingShown = false;
    var lastGone = 'closed';
    var hardTimer = null;

    function current() { return self.generation === generation; }

    function closeAttempt(attempt) {
      if (attempt.timer) { clearTimeout(attempt.timer); attempt.timer = null; }
      var ws = attempt.ws;
      attempt.ws = null;
      attempt.dead = true;
      if (ws) { try { ws.close(); } catch (e) {} }
    }

    function acceptedAlive() {
      for (var i = 0; i < attempts.length; i++) {
        if (!attempts[i].dead && attempts[i].accepted) return true;
      }
      return false;
    }

    function stillConnecting() {
      for (var i = 0; i < attempts.length; i++) {
        if (!attempts[i].dead && !attempts[i].accepted) return true;
      }
      return false;
    }

    function allDead() {
      for (var i = 0; i < attempts.length; i++) {
        if (!attempts[i].dead) return false;
      }
      return true;
    }

    function endRace(reason) {
      // Once a hello_ack has won, the tunnel is live; a deadline that was
      // already queued when the answer landed must not retire it.
      if (winner || self.ready || !current()) return;
      if (self.connectTimer) { clearTimeout(self.connectTimer); self.connectTimer = null; }
      if (hardTimer) { clearTimeout(hardTimer); hardTimer = null; }
      for (var i = 0; i < attempts.length; i++) closeAttempt(attempts[i]);
      self.fail(generation, reason);
    }

    function maybeArmHello() {
      if (helloArmed || winner || !current()) return;
      if (!acceptedAlive() || stillConnecting()) return;
      helloArmed = true;
      self.connectTimer = setTimeout(function () {
        self.connectTimer = null;
        // The answer can win the race between the timer being armed and this
        // callback running under a loaded main thread.  A tunnel that is
        // already live must never be retired by its own handshake deadline.
        if (self.ready) return;
        endRace('computer');
      }, HELLO_TIMEOUT_MS);
    }

    function finishIfHopeless() {
      if (winner || !current()) return;
      if (allDead()) { endRace(lastGone); return; }
      maybeArmHello();
    }

    function sendTo(attempt, bytes) {
      if (!attempt.ws || attempt.dead) return;
      try { attempt.ws.send(bytes); } catch (e) {}
    }

    function adopt(attempt, frame) {
      if (winner || !current()) return;
      winner = attempt;
      if (self.connectTimer) { clearTimeout(self.connectTimer); self.connectTimer = null; }
      if (hardTimer) { clearTimeout(hardTimer); hardTimer = null; }
      for (var i = 0; i < attempts.length; i++) {
        if (attempts[i] !== attempt) closeAttempt(attempts[i]);
      }
      if (attempt.timer) { clearTimeout(attempt.timer); attempt.timer = null; }
      var leftover = attempt.inbox;
      attempt.inbox = new Uint8Array(0);
      attempt.dead = true;
      self.currentBroker = attempt.broker.url;
      self.computerName = frame && typeof frame.computer_name === 'string' ? frame.computer_name : '';
      self.inbox = leftover;
      self._attachLiveSocket(attempt.ws, generation);
      self.ready = true;
      self._markOnline();
      if (leftover.length) self.feed(new Uint8Array(0));
    }

    function racePacket(attempt, packet, headerLength) {
      var type = packet[0] >> 4;
      if (type === 2) { // CONNACK
        if (attempt.timer) { clearTimeout(attempt.timer); attempt.timer = null; }
        if (packet[headerLength + 1] !== 0) {
          lastGone = 'refused';
          closeAttempt(attempt);
          finishIfHopeless();
          return;
        }
        if (attempt.accepted) return;
        attempt.accepted = true;
        if (!verifyingShown) {
          verifyingShown = true;
          self.report('verifying', { broker: attempt.broker.url });
        }
        var packetId = self.nextId();
        sendTo(attempt, mqttPacket(0x82, [
          new Uint8Array([packetId >> 8, packetId & 0xff]),
          mqttString(self.downTopic),
          new Uint8Array([1]),
        ]));
        self.channel.seal({
          v: PROTOCOL_VERSION, type: 'hello', phone_id: self.phoneId(), name: 'phone',
        }).then(function (blob) {
          if (!current() || winner || attempt.dead) return;
          sendTo(attempt, mqttPublishPacket(self.upTopic, self.nextId(), blob));
        }).catch(function () {});
        finishIfHopeless();
        return;
      }
      if (type === 3) { // PUBLISH
        var qos = (packet[0] >> 1) & 0x03;
        var topicLength = (packet[headerLength] << 8) | packet[headerLength + 1];
        var topic = fromUtf8(packet.slice(headerLength + 2, headerLength + 2 + topicLength));
        var at = headerLength + 2 + topicLength;
        var messageId = 0;
        if (qos > 0) {
          messageId = (packet[at] << 8) | packet[at + 1];
          at += 2;
        }
        if (qos === 1) sendTo(attempt, new Uint8Array([0x40, 0x02, messageId >> 8, messageId & 0xff]));
        if (topic !== self.downTopic) return;
        self.channel.open(packet.slice(at)).then(function (frame) {
          if (!frame || !current() || winner) return;
          if (frame.type === 'hello_ack') adopt(attempt, frame);
        }).catch(function () {});
      }
      // PINGRESP/PUBACK/SUBACK carry nothing the race needs.
    }

    for (var b = 0; b < brokers.length; b++) {
      (function (broker) {
        var attempt = {
          broker: broker,
          ws: null,
          inbox: new Uint8Array(0),
          dead: false,
          accepted: false,
          timer: null,
        };
        attempts.push(attempt);
        var ws;
        try {
          ws = new WebSocket(broker.url, ['mqtt']);
          ws.binaryType = 'arraybuffer';
        } catch (e) {
          attempt.dead = true;
          lastGone = 'open';
          return;
        }
        attempt.ws = ws;
        /*
         * The watchdog for the case the WebSocket API cannot report: a socket
         * stuck in CONNECTING fires neither `open` nor `close`, so it would sit
         * in the race forever and never let the deadline start for the brokers
         * that did answer.
         */
        attempt.timer = setTimeout(function () {
          attempt.timer = null;
          if (!current() || winner || attempt.dead || attempt.accepted) return;
          lastGone = 'timeout';
          closeAttempt(attempt);
          finishIfHopeless();
        }, CONNECT_TIMEOUT_MS);
        ws.onopen = function () {
          if (!current() || winner || attempt.dead) return;
          try { ws.send(mqttConnectPacket(broker)); } catch (e) {}
        };
        ws.onmessage = function (event) {
          if (!current() || winner || attempt.dead) return;
          attempt.inbox = concat([attempt.inbox, new Uint8Array(event.data)]);
          drainPackets(attempt, function (packet, headerLength) {
            if (!current() || winner || attempt.dead) return;
            racePacket(attempt, packet, headerLength);
          });
        };
        ws.onclose = function () {
          if (!current() || winner || attempt.dead) return;
          lastGone = 'closed';
          closeAttempt(attempt);
          finishIfHopeless();
        };
        ws.onerror = function () {
          if (!current() || winner || attempt.dead) return;
          try { ws.close(); } catch (e) {}
        };
      })(brokers[b]);
    }
    hardTimer = setTimeout(function () {
      hardTimer = null;
      if (self.ready) return;
      endRace('computer');
    }, RACE_TIMEOUT_MS);
    finishIfHopeless();
  };

  /*
   * Give up on the current socket and move to the next broker.
   *
   * Everything that ends an attempt funnels through here, so the browser's own
   * `close`, a CONNACK that says no, and the connect watchdog cannot each
   * schedule their own reconnect and race each other.
   */
  Tunnel.prototype.fail = function (generation, reason) {
    var self = this;
    if (generation !== self.generation) return;
    self.lastFailure = reason || '';
    if (self.connectTimer) { clearTimeout(self.connectTimer); self.connectTimer = null; }
    var ws = self.ws;
    self.ws = null;
    self.ready = false;
    self.markOffline();
    self.stopPing();
    self.stopPresence();
    if (ws) { try { ws.close(); } catch (e) {} }
    // The socket's own `close` handler will run now; bumping the generation
    // first makes it a no-op instead of a second retry.
    self.generation += 1;
    // A retry that was already queued belongs to the socket just abandoned.
    // Leaving it in place would make `retry` refuse to schedule this one and
    // strand the tunnel with no timer at all.
    if (self.reconnectTimer) { clearTimeout(self.reconnectTimer); self.reconnectTimer = null; }
    self.retry(self.generation);
  };

  /*
   * Backoff, not a fixed 1.5 s.  A fixed delay meant "one broker hiccuped" and
   * "the phone is on a captive-portal Wi-Fi" were retried at the same speed,
   * so three dead brokers were re-dialled every 4.5 seconds forever -- which
   * on a phone is battery and radios, and on a rate-limited public broker is
   * how an IP gets throttled.
   */
  Tunnel.prototype.retry = function (generation) {
    var self = this;
    if (generation !== undefined && generation !== self.generation) return;
    if (self.reconnectTimer) return;
    self.attempts += 1;
    var ceiling = Math.min(RECONNECT_BASE_MS * Math.pow(2, self.attempts - 1), RECONNECT_MAX_MS);
    // ±20% so several phones behind one NAT do not retry in lockstep.
    var delay = Math.round(ceiling * (0.8 + Math.random() * 0.4));
    self.report('retrying', {
      reason: self.lastFailure,
      delay: Math.round(delay / 1000),
      attempts: self.attempts,
      total: self.brokers.length,
    });
    self.reconnectTimer = setTimeout(function () {
      self.reconnectTimer = null;
      self.connect();
    }, delay);
  };

  Tunnel.prototype.stopPing = function () {
    if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
  };

  Tunnel.prototype.startPing = function () {
    var self = this;
    self.stopPing();
    self.pingOutstanding = false;
    self.pingTimer = setInterval(function () {
      if (!self.connected) return;
      if (self.pingOutstanding) {
        /*
         * The socket is open but the broker stopped answering.  A mobile
         * network that drops a flow without a FIN leaves exactly this: writes
         * succeed into a void and every request times out, which reads as
         * "the app is broken" rather than "the link is gone".  Treat it as a
         * disconnect so the next broker gets a turn.
         */
        self.fail(self.generation, 'stale');
        return;
      }
      self.pingOutstanding = true;
      self.send(new Uint8Array([0xC0, 0x00]));
    }, KEEPALIVE_SECONDS * 1000 * 0.8);
  };

  Tunnel.prototype.stopPresence = function () {
    if (this.presenceTimer) { clearInterval(this.presenceTimer); this.presenceTimer = null; }
    this.pongOutstanding = false;
    this.pongMisses = 0;
  };

  /*
   * Ask the computer -- not the broker -- whether it is still on this channel.
   *
   * The MQTT keepalive above only reaches the broker, so it can report a
   * perfectly healthy socket while the desktop tunnel is asleep or has moved
   * to another broker; the page then polls and times out forever.  The app
   * ping frame is already in the protocol and the desktop already answers it
   * with `pong`, so this is the one signal that means "the computer is here".
   */
  Tunnel.prototype.startPresence = function () {
    var self = this;
    self.stopPresence();
    self.presenceTimer = setInterval(function () {
      if (!self.connected) return;
      if (self.pongOutstanding) {
        self.pongMisses += 1;
        if (self.pongMisses >= PRESENCE_MISSES) {
          self.fail(self.generation, 'computer');
          return;
        }
      } else {
        self.pongMisses = 0;
      }
      self.sendPresencePing();
    }, PRESENCE_INTERVAL_MS);
  };

  Tunnel.prototype.sendPresencePing = function () {
    var self = this;
    if (!self.connected) return;
    self.pongOutstanding = true;
    self.channel.seal({
      v: PROTOCOL_VERSION,
      type: 'ping',
      ts: Math.round(Date.now()) / 1000,
    }).then(function (blob) { self.publish(blob, 1); }).catch(function () {});
  };

  /*
   * The page came back to the foreground.
   *
   * A hidden tab throttles its timers, so the presence check can have counted
   * missed pongs that were never really missed; the phone would then rotate a
   * perfectly good tunnel right as its owner looks at it.  Ask the computer
   * now and start from a clean counter instead of carrying the hidden tab's
   * stale misses into the first visible poll.
   */
  Tunnel.prototype.wake = function () {
    if (!this.connected) return;
    this.pongOutstanding = false;
    this.pongMisses = 0;
    this.sendPresencePing();
  };

  Tunnel.prototype.send = function (bytes) {
    // `ready`, not `connected`: the hello frame goes out between the CONNACK
    // and the hello_ack, when the socket is usable but the computer has not
    // answered yet.  Everything that must wait for the computer (requests,
    // presence pings) is gated on `connected` by its caller.
    if (this.ws && this.ready) this.ws.send(bytes);
  };

  Tunnel.prototype.feed = function (chunk) {
    var self = this;
    self.inbox = concat([self.inbox, chunk]);
    drainPackets(self, function (packet, headerLength) {
      self.handlePacket(packet, headerLength);
    });
  };

  Tunnel.prototype.handlePacket = function (packet, headerLength) {
    var type = packet[0] >> 4;
    if (type === 2) { // CONNACK
      // The race consumed the handshake; the live socket must never see one.
      // If it does, the broker restarted the session underneath us -- this
      // transport cannot resume a clean session, so treat it as broken rather
      // than pretending the tunnel is still there.
      if (packet[headerLength + 1] !== 0) this.fail(this.generation, 'refused');
      return;
    }
    if (type === 3) { // PUBLISH
      var qos = (packet[0] >> 1) & 0x03;
      var topicLength = (packet[headerLength] << 8) | packet[headerLength + 1];
      var topic = fromUtf8(packet.slice(headerLength + 2, headerLength + 2 + topicLength));
      var at = headerLength + 2 + topicLength;
      var messageId = 0;
      if (qos > 0) {
        messageId = (packet[at] << 8) | packet[at + 1];
        at += 2;
      }
      if (qos === 1) this.ack(messageId);
      if (topic !== this.downTopic) return;
      this.consume(packet.slice(at));
      return;
    }
    if (type === 4) { // PUBACK for our QoS1 publish
      return;
    }
    if (type === 13) { // PINGRESP — the only proof the path is still there
      this.pingOutstanding = false;
      return;
    }
  };

  Tunnel.prototype.ack = function (messageId) {
    this.send(new Uint8Array([0x40, 0x02, messageId >> 8, messageId & 0xff]));
  };

  Tunnel.prototype.consume = function (blob) {
    var self = this;
    self.channel.open(blob).then(function (frame) {
      if (!frame) return;
      /*
       * The computer's half of the presence check.  It is read here, before
       * `res`, because a pong can arrive while no request is outstanding.
       * (The handshake's hello_ack is consumed by the race, before this socket
       * becomes the live tunnel.)
       */
      if (frame.type === 'pong') {
        self.pongOutstanding = false;
        self.pongMisses = 0;
        return;
      }
      if (frame.type === 'evt') {
        self.emitEvent(String(frame.name || ''), frame.data || {});
        return;
      }
      if (frame.type === 'res' && frame.id && self.pending[frame.id]) {
        var waiter = self.pending[frame.id];
        delete self.pending[frame.id];
        clearTimeout(waiter.timer);
        var body = frame.body ? b64ToBytes(frame.body) : new Uint8Array(0);
        waiter.resolve({ status: frame.status || 0, body: body });
      }
      // Events are not consumed yet: the phone page polls.  A later phase can
      // hook frame.type === 'evt' into the same UI reload calls.
    }).catch(function () {});
  };

  /*
   * Leaving the online state has to be as loud as entering it.  The page keeps
   * its own copy of the answer (`window.__CLIPSYNC_REMOTE__.online`) and any
   * later code that reads it -- a retry guard, a liveness badge -- must not be
   * told the tunnel is usable after the computer stopped answering.
   */
  Tunnel.prototype.markOffline = function () {
    if (!this.connected) return;
    this.connected = false;
    this.onlineHandlers.forEach(function (handler) { handler(false); });
  };

  Tunnel.prototype._markOnline = function () {
    this.ready = true;
    this.connected = true;
    // The computer answering ends the failure run: the next disconnect should
    // retry in 1.5 s rather than at the backoff ceiling.  (A CONNACK used to
    // reset this, which counted a broker that led nowhere as progress.)
    this.attempts = 0;
    this.pingOutstanding = false;
    if (this.connectTimer) { clearTimeout(this.connectTimer); this.connectTimer = null; }
    // Remember the broker the computer was actually reachable on, so the next
    // page load starts its race with the broker that is known to answer (see
    // LAST_BROKER_KEY).  Saved only after the computer answered.
    saveLastBroker(this.currentBroker);
    this.report('online', { broker: this.currentBroker || '', computer: this.computerName });
    var waiters = this._onlineWaiters;
    this._onlineWaiters = [];
    waiters.forEach(function (waiter) {
      clearTimeout(waiter.timer);
      waiter.resolve();
    });
    this.onlineHandlers.forEach(function (handler) { handler(true); });
    this.startPing();
    this.startPresence();
  };

  Tunnel.prototype.nextId = function () {
    this.packetId = (this.packetId % 65535) + 1;
    return this.packetId;
  };

  Tunnel.prototype.publish = function (blob, qos) {
    var packetId = qos > 0 ? this.nextId() : 0;
    this.send(mqttPublishPacket(this.upTopic, packetId, blob));
  };

  Tunnel.prototype.whenOnline = function (timeoutMs) {
    var self = this;
    if (self.connected) return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var waiter = { resolve: resolve, timer: null };
      waiter.timer = setTimeout(function () {
        var index = self._onlineWaiters.indexOf(waiter);
        if (index !== -1) self._onlineWaiters.splice(index, 1);
        reject(new Error('remote tunnel offline'));
      }, timeoutMs || 15000);
      self._onlineWaiters.push(waiter);
    });
  };

  Tunnel.prototype.request = function (path, method, bodyBytes) {
    var self = this;
    if (!self.connected) {
      return self.whenOnline(15000).then(function () {
        return self.request(path, method, bodyBytes);
      });
    }
    var id = (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        delete self.pending[id];
        reject(new Error('remote timeout'));
      }, REQUEST_TIMEOUT_MS);
      self.pending[id] = { resolve: resolve, timer: timer };
      self.channel.seal({
        v: PROTOCOL_VERSION, type: 'req', id: id, method: method,
        path: path, body: bytesToB64(bodyBytes || new Uint8Array(0)),
      }).then(function (blob) { self.publish(blob, 1); })
        .catch(function (error) {
          clearTimeout(timer);
          delete self.pending[id];
          reject(error);
        });
    });
  };

  Tunnel.prototype.phoneId = function () {
    try {
      var key = 'clipsync-remote-phone-id';
      var stored = localStorage.getItem(key);
      if (stored) return stored;
      var minted = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(key, minted);
      return minted;
    } catch (e) {
      return 'phone-' + Math.random().toString(36).slice(2);
    }
  };

  // ---------------------------------------------------------------- entry

  var config = parseFragment();
  var resumed = false;
  if (config) {
    saveSession(config);
    savePersistent(config);
  } else {
    // No fragment: a reload after the address bar was cleaned, a bookmark, or
    // a home-screen icon.  The same tab finds the pairing in sessionStorage;
    // any other tab finds the persisted one (the QR code only has to be
    // scanned once per browser profile).
    config = loadSession();
    if (!config) {
      config = loadPersistent();
      resumed = !!config;
    }
  }

  /*
   * A bare visit to the Pages URL has no fragment, so there is no token and no
   * broker: the page falls back to "same Wi-Fi" and the phone reports a LAN
   * failure that reads like the computer is down.  The static host cannot know
   * better, so say the one useful thing before the page's own error lands.
   * (A bookmark from a page that was paired once does not land here -- that is
   * what the persistent pairing above is for.)
   */
  function showBareVisitHint() {
    if (!/(^|\.)pages\.dev$/i.test(location.hostname)) return;
    function insert() {
      if (document.getElementById('clipsync-remote-hint')) return;
      var hint = document.createElement('div');
      hint.id = 'clipsync-remote-hint';
      hint.setAttribute('role', 'status');
      hint.textContent =
        '这是远程页面，还没有配对。请从电脑端 ClipSync「设置 → 手机 Companion → 远程二维码」' +
        '扫码打开一次；之后把本页存成书签或添加到主屏幕，直接打开就能连。';
      hint.style.cssText =
        'position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483647;' +
        'padding:12px 14px;border-radius:10px;background:#111827;color:#E8EDF5;' +
        'border:1px solid #22D3EE;font:14px/1.5 system-ui,sans-serif;' +
        'box-shadow:0 8px 28px rgba(0,0,0,.45);';
      document.body.appendChild(hint);
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', insert);
    } else {
      insert();
    }
  }

  if (!config) {
    showBareVisitHint();
    return; // A LAN page: nothing to wrap.
  }
  adoptToken(config.token);
  // After the page's own script has read location.search, hide the token.  The
  // page keeps it in its closure; the URL no longer does.
  window.addEventListener('load', forgetTokenInAddressBar, { once: true });
  setTimeout(forgetTokenInAddressBar, 2000);

  var channel = new Channel(config.token);
  var tunnel = new Tunnel(config, channel);
  window.__CLIPSYNC_REMOTE__ = {
    tunnel: tunnel,
    online: false,
    request: function (path, method, body) { return tunnel.request(path, method, body); },
    // The page renders "connecting to relay 2 of 3" from these.  A page that
    // subscribes late still gets the current one.
    onState: function (handler) { return tunnel.onState(handler); },
    // Push events: the page refreshes the affected tab instead of waiting for
    // its next poll (one desktop event replaces one broker round trip).
    onEvent: function (handler) { return tunnel.onEvent(handler); },
    state: function () { return tunnel.state; },
    // The page calls this when it becomes visible again: a tab that slept
    // through several presence intervals must not be treated as a dead link.
    wake: function () { tunnel.wake(); },
    // A 403 means the stored token was rotated or cleared on the computer:
    // drop the pairing so the page asks for a fresh QR code instead of
    // re-reading a credential that can never work again.
    forgetPairing: function () { forgetPersistent(); },
    // True when this page opened without a fragment and used the saved
    // pairing.  A long failure on such a page may mean the token was rotated.
    resumedPairing: function () { return resumed; },
  };
  tunnel.onlineHandlers.push(function (online) { window.__CLIPSYNC_REMOTE__.online = online; });
  tunnel.start();

  function bodyBytes(init) {
    if (!init || init.body === undefined || init.body === null) return new Uint8Array(0);
    var body = init.body;
    if (typeof body === 'string') return utf8(body);
    if (body instanceof ArrayBuffer) return new Uint8Array(body);
    if (body instanceof Uint8Array) return body;
    if (body instanceof URLSearchParams) return utf8(body.toString());
    throw new Error('remote transport cannot carry this body');
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var target = typeof input === 'string' ? input : (input && input.url) || '';
    var url;
    try { url = new URL(target, location.href); } catch (e) { return nativeFetch(input, init); }
    if (!url.pathname.startsWith('/api/')) return nativeFetch(input, init);
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    var body;
    try { body = bodyBytes(init); } catch (e) { return Promise.reject(e); }
    return tunnel.request(url.pathname + url.search, method, body)
      .then(function (result) {
        return new Response(result.body, {
          status: result.status,
          headers: { 'Content-Type': 'application/json' },
        });
      });
  };

  /*
   * Downloads are an <a download> navigation, not fetch(): the Files tab builds
   * a URL and clicks a link.  On the static host that link points at the Pages
   * origin, which has no /api/download.  Intercept it and run the same request
   * through the tunnel, then hand the browser a Blob URL.
   */
  document.addEventListener('click', function (event) {
    var anchor = event.target && event.target.closest ? event.target.closest('a[href]') : null;
    if (!anchor) return;
    var url;
    try { url = new URL(anchor.href, location.href); } catch (e) { return; }
    if (url.pathname !== '/api/download') return;
    event.preventDefault();
    event.stopPropagation();
    tunnel.request(url.pathname + url.search, 'GET', new Uint8Array(0))
      .then(function (result) {
        if (result.status !== 200) throw new Error('download failed');
        var name = anchor.getAttribute('download') || 'download';
        var blob = new Blob([result.body], { type: 'application/octet-stream' });
        var href = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = href;
        link.download = name;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        setTimeout(function () { URL.revokeObjectURL(href); }, 30000);
      })
      .catch(function () {
        if (window.__CLIPSYNC_REMOTE__) window.__CLIPSYNC_REMOTE__.lastDownloadError = 'download failed';
      });
  }, true);
})();
