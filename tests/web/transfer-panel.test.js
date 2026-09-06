// Behavioral tests for the transfers panel's history rows.
//
// The rows had no @contextmenu binding at all, and app.js suppresses the
// native menu everywhere outside editable regions — so right-clicking a
// transfer-history row used to do literally nothing.  These tests pin the
// binding and the row's Retry button down to observable behavior.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { loadComponents, makeT, makeStore, makeTransfer, makeApi } from './helpers.js';

const t = makeT('en');
const Panel = loadComponents('transfer-panel.js')['transfer-panel'];

function mountPanel(history, storeOverrides = {}) {
  const store = makeStore({
    onlineDevices: () => [],
    activeTransfers: [],
    transferHistory: history,
    // Real shape matters: the template gates on `resultMbps !== null`, so a
    // bare {} would render the result row and crash on undefined.toFixed().
    speedTest: { running: false, resultMbps: null, quality: '', progress: 0, status: '', error: '' },
    refreshTransfers: () => Promise.resolve(),
    ...storeOverrides,
  });
  const wrapper = mount(Panel, {
    global: { provide: { store }, mocks: { t } },
  });
  return { wrapper, store };
}

beforeEach(() => {
  makeApi();
});

describe('transfer-history row context menu', () => {
  it('right-clicking a row opens the menu in "transfer" mode with that row as target', async () => {
    const row = makeTransfer();
    const { wrapper, store } = mountPanel([row]);

    const el = wrapper.find('.transfer-history-item');
    expect(el.exists()).toBe(true);

    await el.trigger('contextmenu', { clientX: 120, clientY: 240 });

    expect(store.contextMenu.visible).toBe(true);
    expect(store.contextMenu.mode).toBe('transfer');
    expect(store.contextMenu.target).toBe(row);
    expect(store.contextMenu.x).toBe(120);
    expect(store.contextMenu.y).toBe(240);
  });

  it('targets the row that was right-clicked, not the first one', async () => {
    const a = makeTransfer({ id: 'tr-a' });
    const b = makeTransfer({ id: 'tr-b' });
    const { wrapper, store } = mountPanel([a, b]);

    const rows = wrapper.findAll('.transfer-history-item');
    await rows[1].trigger('contextmenu');

    expect(store.contextMenu.target).toBe(b);
  });
});

describe('transfer-history row Retry button', () => {
  it('delegates to the shared store helper (which the row menu uses too)', async () => {
    const row = makeTransfer({ id: 'tr-up', direction: 'up', status: 'failed' });
    const { wrapper, store } = mountPanel([row]);

    const btn = wrapper.find('button[aria-label="Retry"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger('click');
    await flushPromises();

    expect(store.retryTransfer).toHaveBeenCalledWith('tr-up');
  });

  it('ignores a second click while the first retry is still in flight', async () => {
    const row = makeTransfer({ id: 'tr-up', direction: 'up', status: 'failed' });
    let resolveRetry;
    const retryTransfer = vi.fn(() => new Promise((r) => { resolveRetry = r; }));
    const { wrapper } = mountPanel([row], { retryTransfer });

    const btn = wrapper.find('button[aria-label="Retry"]');
    await btn.trigger('click');
    await btn.trigger('click');

    expect(retryTransfer).toHaveBeenCalledTimes(1);
    resolveRetry(true);
    await flushPromises();
  });

  it('offers no Retry on a completed incoming row', () => {
    const { wrapper } = mountPanel([makeTransfer()]); // direction 'down', completed
    expect(wrapper.find('button[aria-label="Retry"]').exists()).toBe(false);
  });
});
