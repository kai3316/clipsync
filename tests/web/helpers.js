// Shared helpers for ClipSync behavioral component tests.
//
// The web UI is a set of Vue 3 Options-API components that are registered
// onto `window.__CLIPSYNC_COMPONENTS__` by plain-JS IIFEs (no ES modules,
// no .vue SFCs).  These helpers load those files into the jsdom `window`,
// provide the globals the components reach for (`ClipsyncAPI`), and build
// a minimal reactive store + i18n `t()` so a test can mount a component
// and assert its rendered behavior instead of grepping its source.

import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';

const _ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const COMPONENTS_DIR = resolve(_ROOT, 'internal', 'web', 'static', 'components');
const LOCALES_DIR = resolve(_ROOT, 'internal', 'web', 'static', 'locales');
const JS_DIR = resolve(_ROOT, 'internal', 'web', 'static', 'js');

/**
 * Install the shared `ClipsyncFormat` global from the real js/format.js.
 *
 * Components call ClipsyncFormat.size()/speed() for every rendered byte count,
 * so it is as much a hard dependency as Vue itself.  Loading the real module
 * (rather than stubbing it) means a test asserting "1.2 KB" is asserting the
 * formatting users actually see.  Idempotent.
 */
export function loadFormat() {
  if (globalThis.ClipsyncFormat) return globalThis.ClipsyncFormat;
  const src = readFileSync(resolve(JS_DIR, 'format.js'), 'utf-8');
  // The file declares `var ClipsyncFormat = (function(){...})()`, which stays
  // local to the new Function scope — hand it back explicitly.
  globalThis.ClipsyncFormat = new Function(src + '\n;return ClipsyncFormat;')();
  return globalThis.ClipsyncFormat;
}

/** Load one component file and return its `window.__CLIPSYNC_COMPONENTS__`. */
export function loadComponents(filename) {
  loadFormat();
  const src = readFileSync(resolve(COMPONENTS_DIR, filename), 'utf-8');
  const win = globalThis.window;
  win.__CLIPSYNC_COMPONENTS__ = win.__CLIPSYNC_COMPONENTS__ || {};
  // The IIFE writes onto its `window` argument; run it and hand back the map.
  const fn = new Function('window', src + '\n;return window.__CLIPSYNC_COMPONENTS__;');
  return fn(win);
}

/** A real i18n `t()` backed by en.json (so button labels read as words). */
export function makeT(locale = 'en') {
  const dict = JSON.parse(readFileSync(resolve(LOCALES_DIR, `${locale}.json`), 'utf-8'));
  return function t(key, fmt) {
    let text = dict[key];
    if (typeof text !== 'string') text = key;
    if (fmt && typeof fmt === 'object') {
      for (const k of Object.keys(fmt)) {
        text = text.split('{' + k + '}').join(fmt[k]);
      }
    }
    return text;
  };
}

/** A minimal store object with the surface the device components touch. */
export function makeStore(overrides = {}) {
  return {
    deviceId: 'self-dev',
    devices: [],
    activeTab: 'devices',
    activeChatSession: null,
    contextMenu: {},
    showToast: vi.fn(),
    confirm: vi.fn(() => Promise.resolve()),
    prompt: vi.fn(() => Promise.resolve('')),
    testPeerConnection: vi.fn(() => Promise.resolve()),
    // Shared transfer actions the transfers panel and its row context menu
    // both delegate to (so the two callers cannot drift).
    openTransferFile: vi.fn(() => Promise.resolve(true)),
    revealTransferFile: vi.fn(() => Promise.resolve(true)),
    retryTransfer: vi.fn(() => Promise.resolve(true)),
    deleteTransferHistoryItem: vi.fn(() => Promise.resolve(true)),
    ...overrides,
  };
}

/** A transfer-history row as `/api/transfers` returns it (`_map_history`). */
export function makeTransfer(overrides = {}) {
  return {
    id: 'tr-1',
    filename: 'notes.txt',
    size: 1234,
    status: 'completed',
    reason: '',
    path: 'C:\\Users\\me\\Downloads\\notes.txt',
    peer_id: 'peer-1',
    direction: 'down',
    timestamp: 1700000000,
    ...overrides,
  };
}

/** A device object as `/api/devices` returns it. */
export function makeDevice(overrides = {}) {
  return {
    device_id: 'peer-1',
    device_name: 'Peer One',
    os: 'Windows',
    connected: false,
    paired: false,
    note: '',
    encrypted: false,
    // The host's own answer to "can a frame reach this device while it is
    // LAN-offline?" — chat and the connectivity probe are gated on it.
    relay_reachable: false,
    ...overrides,
  };
}

/**
 * Install a fake `ClipsyncAPI` global and return it.  Each method resolves
 * with `{ok:true}` by default so the components' success paths run.
 */
export function makeApi(overrides = {}) {
  const api = {
    connectDevice: vi.fn(() => Promise.resolve({ ok: true })),
    disconnectDevice: vi.fn(() => Promise.resolve({ ok: true })),
    unpairDevice: vi.fn(() => Promise.resolve({ ok: true })),
    forgetDevice: vi.fn(() => Promise.resolve({ ok: true })),
    updateDeviceNote: vi.fn(() => Promise.resolve({ ok: true })),
    chatInvite: vi.fn(() => Promise.resolve({ session_id: 's1' })),
    ...overrides,
  };
  globalThis.ClipsyncAPI = api;
  return api;
}
