// Behavioral test for the live favourites snapshot.
//
// The sidecar pushes `favorites_updated` whenever a favourite changes anywhere
// other than this page — a clip favourited in the native window, a favourite
// edited, regrouped, reordered or deleted there.  Two surfaces now share one
// `favorites.db` where legacy had a single one (its desktop *was* this page),
// so the page has to hear about a change it did not make.
//
// The store under test is the REAL js/store.js, not the minimal double the
// component tests build: the point of the change is that the live push and the
// page-1 load in app.js apply a snapshot through one helper, and a double would
// assert only that ws.js calls something.  Both files are plain IIFEs, so they
// are run with a controlled `window`, the real Vue and jsdom's localStorage,
// and the socket callbacks are driven by hand.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as Vue from 'vue';

const JS_DIR = resolve(
  dirname(fileURLToPath(import.meta.url)), '..', '..', 'internal', 'web', 'static', 'js');
const src = (name) => readFileSync(resolve(JS_DIR, name), 'utf-8');

/**
 * The smallest Storage the store uses, for the group registry it persists.
 *
 * This jsdom build has no storage at all (`window.localStorage` is undefined),
 * while the page's environment always does — `store.js` reads and writes
 * `clipsync_groups` through the bare global, so the test supplies one.
 */
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

/** Load the real store and the WS client into one shared `window`. */
function load() {
  const FakeSocket = makeFakeSocketClass();
  const win = { location: { hostname: 'localhost' } };
  new Function('window', 'Vue', src('store.js'))(win, Vue);
  const ClipsyncWS = new Function(
    'window', 'WebSocket', src('ws.js') + '\n;return ClipsyncWS;')(win, FakeSocket);
  ClipsyncWS.connect('ws://127.0.0.1:9580/ws', 'tok');
  const sock = FakeSocket.instances[0];
  sock.onopen();
  return { store: win.__CLIPSYNC_STORE__, sock, ClipsyncWS };
}

/** Deliver one server frame the way the socket would. */
function deliver(sock, type, data) {
  sock.onmessage({ data: JSON.stringify({ type: type, data: data }) });
}

const favorite = (id, title, group) => ({ id: id, title: title, group: group, content: 'x' });

beforeEach(() => {
  globalThis.localStorage = makeStorage();
});

afterEach(() => {
  delete globalThis.localStorage;
  vi.useRealTimers();
});

describe('a favourites snapshot pushed from the server', () => {
  it('replaces the list the page is holding', () => {
    const { store, sock } = load();
    store.replaceFavorites([favorite('a', 'Old', '')]);

    deliver(sock, 'favorites_updated', {
      favorites: [favorite('b', 'New', 'Work'), favorite('c', 'Other', '')],
    });

    expect(store.favorites.map((f) => f.title)).toEqual(['New', 'Other']);
  });

  it('drops a favourite deleted on the other surface', () => {
    // The case that motivated the push: the phone held a favourite the desktop
    // had already deleted, and only a reload would have noticed.
    const { store, sock } = load();
    store.replaceFavorites([favorite('a', 'Kept', ''), favorite('b', 'Deleted', '')]);

    deliver(sock, 'favorites_updated', { favorites: [favorite('a', 'Kept', '')] });

    expect(store.favorites.map((f) => f.id)).toEqual(['a']);
  });

  it('mutates the list in place, so computed getters over it stay valid', () => {
    const { store, sock } = load();
    store.replaceFavorites([favorite('a', 'Old', '')]);
    const list = store.favorites;

    deliver(sock, 'favorites_updated', { favorites: [favorite('b', 'New', '')] });

    expect(store.favorites).toBe(list);
  });

  it('applies an edit to a favourite the page already had', () => {
    const { store, sock } = load();
    store.replaceFavorites([favorite('a', 'Before', 'Work')]);

    deliver(sock, 'favorites_updated', { favorites: [favorite('a', 'After', 'Home')] });

    expect(store.favorites[0].title).toBe('After');
    expect(store.favorites[0].group).toBe('Home');
  });

  it('clears the ghost group registry when the snapshot is empty', () => {
    // A reset or deleted data folder leaves zero favourites but a stale
    // per-browser group registry; the page-1 load cleared it and the live push
    // goes through the same helper, so it must clear it too.
    const { store, sock } = load();
    store.groupNames = ['Work', 'Home'];
    store.persistGroups();
    store.replaceFavorites([favorite('a', 'Last', 'Work')]);

    deliver(sock, 'favorites_updated', { favorites: [] });

    expect(store.favorites).toEqual([]);
    expect(store.groupNames).toEqual([]);
    expect(localStorage.getItem('clipsync_groups')).toBe('[]');
  });

  it('keeps the groups it knows when the snapshot is not empty', () => {
    const { store, sock } = load();
    store.groupNames = ['Work'];
    store.persistGroups();

    deliver(sock, 'favorites_updated', { favorites: [favorite('a', 'Kept', 'Work')] });

    expect(store.groupNames).toEqual(['Work']);
  });

  it('ignores a payload that carries no list at all', () => {
    const { store, sock } = load();
    store.replaceFavorites([favorite('a', 'Kept', '')]);

    deliver(sock, 'favorites_updated', {});
    deliver(sock, 'favorites_updated', { favorites: null });
    deliver(sock, 'favorites_updated', { count: 3 });

    expect(store.favorites.map((f) => f.id)).toEqual(['a']);
  });

  it('still takes the older items key', () => {
    // app.js's loadFavorites tolerates `res.items` from an older server; the
    // live path accepts the same two keys so one page cannot be half-migrated.
    const { store, sock } = load();

    deliver(sock, 'favorites_updated', { items: [favorite('a', 'Legacy', '')] });

    expect(store.favorites.map((f) => f.title)).toEqual(['Legacy']);
  });
});
