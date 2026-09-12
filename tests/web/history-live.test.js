// Behavioral test for the history snapshot the server pushes.
//
// The panel's own history routes used to answer the panels with a targeted
// `history_item_deleted` for a delete and a `history_clear` for a wipe.  They
// now publish `history.changed` on the runtime's journal instead — the same
// event the native window's own writes publish — and `PhonePush` turns that
// back into the two broadcasts it already knew how to send: a wipe stays a
// wipe, everything else becomes the page-1 snapshot.  So a delete initiated
// from a phone now arrives here as `history_updated`, exactly as a delete made
// in the window always has, and the deleted row has to disappear through the
// snapshot merge rather than through the targeted removal.
//
// The store under test is the REAL js/store.js and the REAL js/ws.js, driven by
// hand the way tests/web/favorites-live.test.js drives them.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as Vue from 'vue';

const JS_DIR = resolve(
  dirname(fileURLToPath(import.meta.url)), '..', '..', 'internal', 'web', 'static', 'js');
const src = (name) => readFileSync(resolve(JS_DIR, name), 'utf-8');

/** The smallest Storage the store uses, for the group registry it persists. */
function makeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(String(key)) ? map.get(String(key)) : null),
    setItem: (key, value) => { map.set(String(key), String(value)); },
    removeItem: (key) => { map.delete(String(key)); },
    clear: () => { map.clear(); },
    key: (i) => Array.from(map.keys())[i] ?? null,
    get length() { return map.size; },
  };
}

/** Sockets that never connect on their own — the test fires the callbacks. */
function makeFakeSocketClass() {
  const instances = [];
  function FakeSocket(url) {
    this.url = url;
    instances.push(this);
  }
  FakeSocket.prototype.close = function () {};
  FakeSocket.instances = instances;
  return FakeSocket;
}

/**
 * Load the real store and WS client into one shared `window`.
 *
 * `getHistory` is the authoritative-list fetch the ghost calibration makes; a
 * test that never trips the calibration can leave it out.
 */
function load(getHistory) {
  const FakeSocket = makeFakeSocketClass();
  const win = { location: { hostname: 'localhost' } };
  if (getHistory) win.ClipsyncAPI = { getHistory: getHistory };
  new Function('window', 'Vue', src('store.js'))(win, Vue);
  const ClipsyncWS = new Function(
    'window', 'WebSocket', src('ws.js') + '\n;return ClipsyncWS;')(win, FakeSocket);
  ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
  const sock = FakeSocket.instances[0];
  sock.onopen();
  const store = win.__CLIPSYNC_STORE__;
  // The broadcast schedules a debounced overview fetch; this test is about the
  // list, and the real fetch would need an API the panel has no reason to have.
  store.fetchOverview = () => {};
  return { store, sock };
}

/** Deliver one server frame the way the socket would. */
function deliver(sock, type, data) {
  sock.onmessage({ data: JSON.stringify({ type: type, data: data }) });
}

const row = (id, text) => ({ entry_id: id, text_preview: text, timestamp: id });

/** A first page of `n` rows, newest first, as the server would send it. */
function page(n, from = 1) {
  const items = [];
  for (let i = 0; i < n; i += 1) items.push(row(from + i, 'clip ' + (from + i)));
  return items;
}

beforeEach(() => {
  globalThis.localStorage = makeStorage();
});

afterEach(() => {
  delete globalThis.localStorage;
  vi.useRealTimers();
});

describe('a history snapshot pushed from the server', () => {
  it('drops a row deleted on another surface', () => {
    // The case the route change made: a delete made from a phone used to reach
    // the other panels as a targeted removal, and now reaches them as the
    // page-1 snapshot this merge path already handles.
    const { store, sock } = load();
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(3));

    deliver(sock, 'history_updated', { items: page(2), total: 2 });

    expect(store.history.map((h) => h.entry_id)).toEqual([1, 2]);
  });

  it('keeps the pagination honest about the smaller list', () => {
    const { store, sock } = load();
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(3));
    store.setHistoryCursor(3);

    deliver(sock, 'history_updated', { items: page(2), total: 2 });

    expect(store.historyOffset).toBe(2);
    expect(store.historyHasMore).toBe(false);
  });

  it('applies a pin toggled elsewhere in place', () => {
    const { store, sock } = load();
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(2));

    const pinned = page(2);
    pinned[0].pinned = true;
    deliver(sock, 'history_updated', { items: pinned, total: 2 });

    expect(store.history.map((h) => !!h.pinned)).toEqual([true, false]);
  });

  it('heals a ghost row in a paged list by calibrating', async () => {
    // A panel that has loaded past page 1 does not replace its list on a
    // snapshot — it merges, so a deleted row would sit there as a ghost until
    // the total no longer covers the loaded length and the calibration below
    // fetches the authoritative list.  That is the price of a delete arriving
    // as a snapshot, and it is why the calibration exists.
    const authoritative = page(39);
    const { store, sock } = load(() => Promise.resolve({ items: authoritative, total: 39 }));
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(40));
    store.setHistoryCursor(40);

    deliver(sock, 'history_updated', { items: page(29), total: 39 });
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(store.history.length).toBe(39);
    expect(store.history.map((h) => h.entry_id)).not.toContain(40);
    expect(store.historyHasMore).toBe(false);
  });

  it('ignores a payload that carries no list at all', () => {
    const { store, sock } = load();
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(2));

    deliver(sock, 'history_updated', { total: 2 });
    deliver(sock, 'history_updated', { items: null, total: 2 });

    expect(store.history.map((h) => h.entry_id)).toEqual([1, 2]);
  });
});

describe('a wipe pushed from the server', () => {
  it('empties the list and its pagination', () => {
    // A snapshot cannot express a wipe: the merge rebuilds the visible window
    // but keeps its cursor, so an emptied list would still offer "Load more"
    // over nothing.  The route's `{"cleared": n}` is the shape `PhonePush`
    // answers with this message, whichever surface emptied the list.
    const { store, sock } = load();
    store.settingsCache = { web_history_limit: 30 };
    store.replaceHistory(page(3));
    store.setHistoryCursor(3);
    store.selectedIds = new Set([1, 2]);

    deliver(sock, 'history_clear', {});

    expect(store.history).toEqual([]);
    expect(store.historyOffset).toBe(0);
    expect(store.historyHasMore).toBe(false);
    expect(store.selectedIds.size).toBe(0);
  });
});
