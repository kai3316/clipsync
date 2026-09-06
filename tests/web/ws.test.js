// Behavioral tests for the WebSocket client's reconnect lifecycle.
//
// js/ws.js is a plain IIFE (`var ClipsyncWS = (function () {...})()`), so it
// is loaded the same way the component tests load components: run the source
// with a controlled `window` and a fake `WebSocket` constructor, then drive
// the socket callbacks by hand.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const _ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WS_SRC = readFileSync(
  resolve(_ROOT, 'internal', 'web', 'static', 'js', 'ws.js'), 'utf-8');

/** Sockets that never connect on their own — the test fires the callbacks. */
function makeFakeSocketClass(opts = {}) {
  const instances = [];
  function FakeSocket(url) {
    if (opts.throwOnConstruct) throw new Error('CSP blocked the WebSocket');
    this.url = url;
    this.closeCalls = [];
    instances.push(this);
  }
  FakeSocket.prototype.close = function (code, reason) {
    // The real close() resolves asynchronously; onclose is fired by the test
    // so the ordering under scrutiny stays explicit.
    this.closeCalls.push({ code, reason });
  };
  FakeSocket.instances = instances;
  return FakeSocket;
}

function loadWS(FakeSocket) {
  const win = { __CLIPSYNC_STORE__: null };
  const fn = new Function(
    'window', 'WebSocket', WS_SRC + '\n;return ClipsyncWS;');
  return { ws: fn(win, FakeSocket), win };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('reconnect backoff', () => {
  it('backs off when the WebSocket constructor throws synchronously', () => {
    // CSP block, malformed URL, no network stack: `new WebSocket()` throws
    // right in _doConnect, which calls straight back into
    // _scheduleReconnect from inside the timer callback.  The doubling used
    // to sit BELOW that call, so it had not run yet and every retry was
    // scheduled with the delay the failed one had just used — the client
    // hammered away at ~1s forever instead of easing off to 30s.
    let attempts = 0;
    function ThrowingSocket() {
      attempts += 1;
      throw new Error('nope');
    }
    const win = { __CLIPSYNC_STORE__: null };
    const ClipsyncWS = new Function(
      'window', 'WebSocket', WS_SRC + '\n;return ClipsyncWS;')(win, ThrowingSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    expect(attempts).toBe(1);           // immediate attempt

    vi.advanceTimersByTime(999);
    expect(attempts).toBe(1);
    vi.advanceTimersByTime(1);
    expect(attempts).toBe(2);           // retry #1 after 1s

    vi.advanceTimersByTime(1000);
    expect(attempts).toBe(2);           // 1s is no longer enough...
    vi.advanceTimersByTime(1000);
    expect(attempts).toBe(3);           // ...retry #2 lands at 2s

    vi.advanceTimersByTime(3999);
    expect(attempts).toBe(3);
    vi.advanceTimersByTime(1);
    expect(attempts).toBe(4);           // and retry #3 at 4s
  });

  it('caps the delay instead of growing without bound', () => {
    let attempts = 0;
    function ThrowingSocket() {
      attempts += 1;
      throw new Error('nope');
    }
    const win = { __CLIPSYNC_STORE__: null };
    const ClipsyncWS = new Function(
      'window', 'WebSocket', WS_SRC + '\n;return ClipsyncWS;')(win, ThrowingSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    vi.advanceTimersByTime(10 * 60 * 1000);
    const settled = attempts;
    // Once capped at 30s, exactly two more attempts fit in a minute.
    vi.advanceTimersByTime(60 * 1000);
    expect(attempts - settled).toBe(2);
  });

  it('resets the backoff after a successful open', () => {
    const FakeSocket = makeFakeSocketClass();
    const { ws: ClipsyncWS } = loadWS(FakeSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    const first = FakeSocket.instances[0];
    first.onopen();
    expect(ClipsyncWS.connected).toBe(true);

    first.onclose({ code: 1006, reason: '' });
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances.length).toBe(2);  // back to a 1s first retry
  });
});

describe('disconnect()', () => {
  it('detaches the old socket so its late onclose cannot speak for a new one', () => {
    // onclose fires asynchronously.  By the time it arrives, a connect()
    // (token change, server switch) may already have installed a new socket
    // — and the old handler would null it out, chime "disconnected" and
    // schedule a reconnect on top of a connection that is perfectly fine.
    const FakeSocket = makeFakeSocketClass();
    const { ws: ClipsyncWS } = loadWS(FakeSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    const dead = FakeSocket.instances[0];
    ClipsyncWS.disconnect();

    expect(dead.closeCalls.length).toBe(1);
    expect(dead.onclose).toBe(null);
    expect(dead.onopen).toBe(null);
    expect(dead.onmessage).toBe(null);
    expect(dead.onerror).toBe(null);
    expect(ClipsyncWS.connected).toBe(false);
  });

  it('leaves the client able to auto-reconnect after the next drop', () => {
    // The "intentional close" flag used to be cleared only by the onclose
    // handler — which disconnect() now removes, and which never ran at all
    // when ws was already null.  Stuck at true it makes the NEXT genuine
    // drop look intentional: no reconnect, a UI that quietly stops updating.
    const FakeSocket = makeFakeSocketClass();
    const { ws: ClipsyncWS } = loadWS(FakeSocket);

    ClipsyncWS.disconnect();            // never connected: ws is null
    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    const sock = FakeSocket.instances[0];
    sock.onopen();

    const drops = [];
    ClipsyncWS.on('disconnected', (d) => drops.push(d));
    sock.onclose({ code: 1006, reason: 'network gone' });

    expect(drops.length).toBe(1);
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances.length).toBe(2);
  });

  it('stops reconnecting when the caller asked to disconnect', () => {
    const FakeSocket = makeFakeSocketClass();
    const { ws: ClipsyncWS } = loadWS(FakeSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    FakeSocket.instances[0].onopen();
    ClipsyncWS.disconnect();

    vi.advanceTimersByTime(60 * 1000);
    expect(FakeSocket.instances.length).toBe(1);
  });
});

describe('superseded sockets', () => {
  it('a stale socket closing must not strand the live connection', () => {
    // connect() twice without a disconnect in between: the first socket is
    // still open and still holding its handlers.  When it finally closes it
    // used to null out `ws` — the LIVE one — and schedule a reconnect.
    const FakeSocket = makeFakeSocketClass();
    const { ws: ClipsyncWS } = loadWS(FakeSocket);

    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
    const stale = FakeSocket.instances[0];
    ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok2');
    const live = FakeSocket.instances[1];
    live.onopen();
    expect(ClipsyncWS.connected).toBe(true);

    stale.onclose({ code: 1006, reason: 'late' });

    expect(ClipsyncWS.connected).toBe(true);
    vi.advanceTimersByTime(60 * 1000);
    expect(FakeSocket.instances.length).toBe(2);  // no spurious reconnect
  });
});
