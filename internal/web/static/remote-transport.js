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
  // browser's own history does not carry the secret.  A reload restores it from
  // sessionStorage (same tab), not from the address bar.
  var SESSION_KEY = 'clipsync-remote-session';

  function saveSession(config) {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(config));
    } catch (e) {}
  }

  function loadSession() {
    try {
      var raw = sessionStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      var config = JSON.parse(raw);
      if (!config || !config.token || !Array.isArray(config.brokers) || !config.brokers.length) {
        return null;
      }
      return config;
    } catch (e) {
      return null;
    }
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
  }

  Channel.prototype.ready = function () {
    var self = this;
    if (self.key) return Promise.resolve(self.key);
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
        return subtle.importKey('raw', bits, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
      })
      .then(function (key) { self.key = key; return key; });
  };

  Channel.prototype.seal = function (inner) {
    var self = this;
    return self.ready().then(function (key) {
      var iv = crypto.getRandomValues(new Uint8Array(12));
      var plain = utf8(JSON.stringify(inner));
      return crypto.subtle.encrypt({ name: 'AES-GCM', iv: iv }, key, plain).then(function (ct) {
        return utf8(JSON.stringify({
          v: 1,
          ts: Date.now() / 1000,
          data: bytesToB64(concat([PREFIX, iv, new Uint8Array(ct)])),
        }));
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
    this.ws = null;
    this.connected = false;
    this.packetId = 1;
    this.pending = {};
    this.inbox = new Uint8Array(0);
    this.pingTimer = null;
    this.reconnectTimer = null;
    this.upTopic = '';
    this.downTopic = '';
    this.onlineHandlers = [];
    this._onlineWaiters = [];
  }

  Tunnel.prototype.start = function () {
    var self = this;
    return self.topics().then(function (topics) {
      self.upTopic = topics[0];
      self.downTopic = topics[1];
      self.connect();
    });
  };

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
    var broker = self.brokers[self.brokerIndex % self.brokers.length];
    self.brokerIndex += 1;
    var ws;
    try {
      ws = new WebSocket(broker.url, ['mqtt']);
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      self.scheduleReconnect();
      return;
    }
    self.ws = ws;
    ws.onopen = function () {
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
      var connect = mqttPacket(0x10, [
        mqttString('MQTT'), new Uint8Array([4, flags, 0, KEEPALIVE_SECONDS]),
        concat(payload),
      ]);
      ws.send(connect);
    };
    ws.onmessage = function (event) {
      self.feed(new Uint8Array(event.data));
    };
    ws.onclose = function () {
      self.connected = false;
      self.stopPing();
      self.scheduleReconnect();
    };
    ws.onerror = function () {
      try { ws.close(); } catch (e) {}
    };
  };

  Tunnel.prototype.scheduleReconnect = function () {
    var self = this;
    if (self.reconnectTimer) return;
    self.reconnectTimer = setTimeout(function () {
      self.reconnectTimer = null;
      self.connect();
    }, 1500);
  };

  Tunnel.prototype.stopPing = function () {
    if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
  };

  Tunnel.prototype.startPing = function () {
    var self = this;
    self.stopPing();
    self.pingTimer = setInterval(function () {
      if (self.connected) self.send(new Uint8Array([0xC0, 0x00]));
    }, KEEPALIVE_SECONDS * 1000 * 0.8);
  };

  Tunnel.prototype.send = function (bytes) {
    if (this.ws && this.connected) this.ws.send(bytes);
  };

  Tunnel.prototype.feed = function (chunk) {
    this.inbox = concat([this.inbox, chunk]);
    while (this.inbox.length >= 2) {
      var multiplier = 1, length = 0, index = 1, byte, complete = false;
      do {
        if (index >= this.inbox.length) return;
        byte = this.inbox[index++];
        length += (byte & 0x7f) * multiplier;
        multiplier *= 128;
        if (multiplier > 128 * 128 * 128) return;
      } while (byte & 0x80);
      var total = index + length;
      if (this.inbox.length < total) return;
      var packet = this.inbox.slice(0, total);
      this.inbox = this.inbox.slice(total);
      complete = true;
      if (!complete) return;
      this.handlePacket(packet, index);
    }
  };

  Tunnel.prototype.handlePacket = function (packet, headerLength) {
    var type = packet[0] >> 4;
    if (type === 2) { // CONNACK
      if (packet[headerLength + 1] !== 0) return;
      this._markOnline();
      this.startPing();
      var self = this;
      var packetId = this.nextId();
      var subscribe = mqttPacket(0x82, [
        new Uint8Array([packetId >> 8, packetId & 0xff]),
        mqttString(this.downTopic),
        new Uint8Array([1]),
      ]);
      this.send(subscribe);
      this.channel.seal({ v: PROTOCOL_VERSION, type: 'hello', phone_id: this.phoneId(), name: 'phone' })
        .then(function (blob) { self.publish(blob, 1); });
      this.onlineHandlers.forEach(function (handler) { handler(true); });
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
  };

  Tunnel.prototype.ack = function (messageId) {
    this.send(new Uint8Array([0x40, 0x02, messageId >> 8, messageId & 0xff]));
  };

  Tunnel.prototype.consume = function (blob) {
    var self = this;
    self.channel.open(blob).then(function (frame) {
      if (!frame) return;
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

  Tunnel.prototype.nextId = function () {
    this.packetId = (this.packetId % 65535) + 1;
    return this.packetId;
  };

  Tunnel.prototype.publish = function (blob, qos) {
    var packetId = qos > 0 ? this.nextId() : 0;
    var body = qos > 0
      ? concat([mqttString(this.upTopic), new Uint8Array([packetId >> 8, packetId & 0xff]), blob])
      : concat([mqttString(this.upTopic), blob]);
    this.send(mqttPacket(qos > 0 ? 0x32 : 0x30, [body]));
  };

  Tunnel.prototype._markOnline = function () {
    this.connected = true;
    var waiters = this._onlineWaiters;
    this._onlineWaiters = [];
    waiters.forEach(function (waiter) {
      clearTimeout(waiter.timer);
      waiter.resolve();
    });
    this.onlineHandlers.forEach(function (handler) { handler(true); });
  };

  /*
   * A page load races the broker handshake: the first history request is
   * issued while the MQTT socket is still connecting.  Failing that request
   * immediately produced the "same Wi-Fi" screen even though the relay came up
   * a second later.  Requests wait for the connection instead (bounded), and a
   * transient disconnect resolves on the next successful CONNACK.
   */
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
  if (config) {
    saveSession(config);
  } else {
    // A reload after the address bar was cleaned: same tab, same session, so
    // the token is still in sessionStorage even though the URL has no fragment.
    config = loadSession();
  }

  /*
   * A bare visit to the Pages URL has no fragment, so there is no token and no
   * broker: the page falls back to "same Wi-Fi" and the phone reports a LAN
   * failure that reads like the computer is down.  The static host cannot know
   * better, so say the one useful thing before the page's own error lands.
   */
  function showBareVisitHint() {
    if (!/(^|\.)pages\.dev$/i.test(location.hostname)) return;
    function insert() {
      if (document.getElementById('clipsync-remote-hint')) return;
      var hint = document.createElement('div');
      hint.id = 'clipsync-remote-hint';
      hint.setAttribute('role', 'status');
      hint.textContent =
        '这是远程页面。请从电脑端 ClipSync「设置 → 手机 Companion → 远程二维码」扫码打开；直接输入网址无法连接。';
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
