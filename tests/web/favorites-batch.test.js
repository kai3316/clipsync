// Behavioral test for the phone's multi-item favourite gestures.
//
// Every one of them — a drag, a group rename, a group delete — used to save by
// sending one PATCH per item.  That is one round trip per item over a phone
// connection and, now that a favourite write is published to the other
// surfaces, one snapshot per item behind a single gesture, each of them a half
// applied change.  So the panel sends the whole batch in one request, and the
// route applies it in one call.
//
// Both files are loaded as what they are: the real component (a plain IIFE
// registering itself on a controlled `window`) with a stub API, and the real
// `js/api.js` with a stub `fetch`, so what is pinned is the request the panel
// actually issues, not that it calls something.

import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const src = (...parts) =>
  readFileSync(resolve(ROOT, 'internal', 'web', 'static', ...parts), 'utf-8');

/** The component, with `ClipsyncAPI` (and the DOM it never touches) stubbed. */
function loadPanel(api) {
  const win = {};
  new Function('window', 'ClipsyncAPI', src('components', 'favorites-panel.js'))(
    win, api);
  // The gestures are `methods` entries, like everything else the panel does.
  return win.__CLIPSYNC_COMPONENTS__['favorites-panel'].methods;
}

/** The real API client, wired to a fake transport. */
function loadApi(fetchImpl) {
  return new Function('fetch', `${src('js', 'api.js')}\n;return ClipsyncAPI;`)(fetchImpl);
}

function fakeStore(favorites) {
  return {
    favorites,
    toasts: [],
    activeGroup: '',
    groupCalls: [],
    showToast(message, ms) { this.toasts.push([message, ms]); },
    renameGroup(oldName, newName) { this.groupCalls.push(['rename', oldName, newName]); },
    removeGroup(name) { this.groupCalls.push(['remove', name]); },
    confirm() { return Promise.resolve(true); },
  };
}

/** A component context: the panel methods only read `this.store` and `this.t`. */
function context(favorites) {
  const store = fakeStore(favorites);
  return { store, t: (key) => key };
}

/** An API whose single-favourite write is a bug: these gestures are batches. */
function batchOnly(onBatch) {
  return {
    updateFavoritesBatch(updates) { return onBatch(updates); },
    updateFavorite() { throw new Error('the gesture must not patch item by item'); },
  };
}

const settle = () => new Promise((done) => setTimeout(done, 0));

describe('the panel saves a drag as one request', () => {
  it('sends only the favourites that moved, in one call', async () => {
    const calls = [];
    const api = batchOnly((updates) => {
      calls.push(updates);
      return Promise.resolve({ ok: true });
    });
    const favorites = [
      { id: 'a', position: 1 },
      { id: 'b', position: 0 },
      { id: 'c', position: 2 },
    ];
    const ctx = context(favorites);

    loadPanel(api)._saveOrder.call(ctx);
    await settle();

    expect(calls).toEqual([[{ id: 'a', position: 0 }, { id: 'b', position: 1 }]]);
    expect(ctx.store.toasts).toEqual([['favorites.order_saved', 1500]]);
  });

  it('applies the new positions to the list it is looking at', async () => {
    const api = batchOnly(() => Promise.resolve({ ok: true }));
    const favorites = [{ id: 'a', position: 5 }, { id: 'b', position: 0 }];
    const ctx = context(favorites);

    loadPanel(api)._saveOrder.call(ctx);
    // Optimistic: the list is already what the user dropped, before the reply.
    expect(favorites.map((f) => f.position)).toEqual([0, 1]);
    await settle();
  });

  it('asks the server for nothing when the order is already saved', async () => {
    const api = batchOnly(() => { throw new Error('nothing moved'); });
    const ctx = context([{ id: 'a', position: 0 }, { id: 'b', position: 1 }]);

    loadPanel(api)._saveOrder.call(ctx);
    await settle();

    expect(ctx.store.toasts).toEqual([]);
  });

  it('reports a failed save, once, and keeps the order the user dropped', async () => {
    const calls = [];
    const api = batchOnly((updates) => {
      calls.push(updates);
      return Promise.reject(new Error('offline'));
    });
    const favorites = [{ id: 'a', position: 1 }, { id: 'b', position: 0 }];
    const ctx = context(favorites);

    loadPanel(api)._saveOrder.call(ctx);
    await settle();

    expect(calls).toHaveLength(1);
    expect(ctx.store.toasts).toEqual([['favorites.order_failed', 2000]]);
    expect(favorites.map((f) => f.position)).toEqual([0, 1]);
  });
});

describe('the panel renames a group in one request', () => {
  function renameContext(favorites) {
    const ctx = context(favorites);
    ctx.renamingGroup = 'work';
    ctx.renameValue = 'job';
    return ctx;
  }

  it('renames every member in one call, and the group itself locally', async () => {
    const calls = [];
    const api = batchOnly((updates) => {
      calls.push(updates);
      return Promise.resolve({ ok: true, updated: 2 });
    });
    const favorites = [
      { id: 'a', group: 'work' },
      { id: 'b', group: 'work' },
      { id: 'c', group: 'home' },
    ];
    const ctx = renameContext(favorites);

    loadPanel(api).saveRenameGroup.call(ctx);
    await settle();

    expect(calls).toEqual([[
      { id: 'a', group: 'job' },
      { id: 'b', group: 'job' },
    ]]);
    expect(favorites.map((f) => f.group)).toEqual(['job', 'job', 'home']);
    expect(ctx.store.groupCalls).toEqual([['rename', 'work', 'job']]);
    expect(ctx.store.toasts).toEqual([['favorites.group_renamed', 2000]]);
  });

  it('renames the empty group locally and asks for nothing', async () => {
    const api = batchOnly(() => { throw new Error('no members'); });
    const ctx = renameContext([{ id: 'a', group: 'home' }]);

    loadPanel(api).saveRenameGroup.call(ctx);
    await settle();

    expect(ctx.store.groupCalls).toEqual([['rename', 'work', 'job']]);
    expect(ctx.store.toasts).toEqual([['favorites.group_renamed', 2000]]);
  });

  it('says nothing was renamed when the save fails', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const api = batchOnly(() => Promise.reject(new Error('offline')));
    const favorites = [{ id: 'a', group: 'work' }];
    const ctx = renameContext(favorites);

    loadPanel(api).saveRenameGroup.call(ctx);
    await settle();

    expect(ctx.store.toasts).toEqual([]);
    expect(logged).toHaveBeenCalled();
    logged.mockRestore();
  });
});

describe('the panel deletes a group in one request', () => {
  it('ungroups every member in one call, after the confirmation', async () => {
    const calls = [];
    const api = batchOnly((updates) => {
      calls.push(updates);
      return Promise.resolve({ ok: true, updated: 2 });
    });
    const favorites = [
      { id: 'a', group: 'work' },
      { id: 'b', group: 'work' },
      { id: 'c', group: 'home' },
    ];
    const ctx = context(favorites);
    ctx.contextMenu = { show: true };

    loadPanel(api).deleteGroup.call(ctx, 'work');
    await settle();

    expect(calls).toEqual([[
      { id: 'a', group: '' },
      { id: 'b', group: '' },
    ]]);
    expect(favorites.map((f) => f.group)).toEqual(['', '', 'home']);
    expect(ctx.store.groupCalls).toEqual([['remove', 'work']]);
    expect(ctx.store.toasts).toEqual([['favorites.group_deleted', 2000]]);
  });

  it('says nothing was deleted when the save fails', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const api = batchOnly(() => Promise.reject(new Error('offline')));
    const ctx = context([{ id: 'a', group: 'work' }]);
    ctx.contextMenu = { show: true };

    loadPanel(api).deleteGroup.call(ctx, 'work');
    await settle();

    expect(ctx.store.toasts).toEqual([]);
    expect(logged).toHaveBeenCalled();
    logged.mockRestore();
  });
});

describe('the api client sends the batch body', () => {
  it('PATCHes the same route the single-favourite update uses', async () => {
    const seen = [];
    const api = loadApi((url, options) => {
      seen.push({ url, options });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ok: true, updated: 2 }),
      });
    });
    api.init('http://127.0.0.1:9580', 'token-1');

    const updates = [{ id: 'a', position: 0 }, { id: 'b', group: 'job' }];
    await expect(api.updateFavoritesBatch(updates)).resolves.toEqual({ ok: true, updated: 2 });

    expect(seen).toHaveLength(1);
    expect(seen[0].options.method).toBe('PATCH');
    expect(seen[0].url).toContain('/api/favorites?token=token-1');
    expect(JSON.parse(seen[0].options.body)).toEqual({ updates });
  });

  it('surfaces a rejected batch as an error the caller can catch', async () => {
    const api = loadApi(() => Promise.resolve({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ ok: false, error: 'updates repeats an id' }),
    }));
    api.init('http://127.0.0.1:9580', 'token-1');

    await expect(api.updateFavoritesBatch([{ id: 'a', position: 0 }]))
      .rejects.toThrow('updates repeats an id');
  });
});
