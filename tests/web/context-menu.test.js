// Behavioral tests for the context menu's 'transfer' mode — the right-click
// menu on a transfer-history row.  These assert which entries appear for which
// kind of row (received vs. sent, completed vs. failed) and that each one
// delegates to the shared store helper rather than reimplementing the action.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { loadComponents, makeT, makeStore, makeTransfer, makeApi } from './helpers.js';

const t = makeT('en');
const Menu = loadComponents('context-menu.js')['context-menu'];

function mountMenu(transfer, storeOverrides = {}) {
  const store = makeStore({
    contextMenu: {
      visible: true,
      x: 10,
      y: 10,
      mode: 'transfer',
      target: transfer,
    },
    ...storeOverrides,
  });
  const wrapper = mount(Menu, {
    global: { provide: { store }, mocks: { t } },
    attachTo: document.body,
  });
  return { wrapper, store };
}

function labels(wrapper) {
  return wrapper.findAll('.context-menu__item-label').map((n) => n.text());
}

function clickEntry(wrapper, label) {
  const item = wrapper.findAll('.context-menu__item')
    .find((n) => n.find('.context-menu__item-label').text() === label);
  expect(item, `menu entry "${label}" not found in ${JSON.stringify(labels(wrapper))}`).toBeTruthy();
  return item.trigger('click');
}

beforeEach(() => {
  makeApi();
});

describe('context menu — transfer mode entry matrix', () => {
  it('offers open + reveal + copies + remove for a received file with a path', () => {
    const { wrapper } = mountMenu(makeTransfer());
    const l = labels(wrapper);
    expect(l).toContain('Open');
    expect(l).toContain('Reveal in folder');
    expect(l).toContain('Copy file name');
    expect(l).toContain('Copy file path');
    expect(l).toContain('Remove from history');
    // A received row was never sent from here, so there is nothing to resend.
    expect(l).not.toContain('Resend');
  });

  it('hides Open for an outgoing row (its path is the sender\'s source file)', () => {
    const { wrapper } = mountMenu(makeTransfer({ direction: 'up' }));
    const l = labels(wrapper);
    expect(l).not.toContain('Open');
    expect(l).toContain('Reveal in folder');
  });

  it('hides both path entries when the host recorded no path', () => {
    const { wrapper } = mountMenu(makeTransfer({ path: '' }));
    const l = labels(wrapper);
    expect(l).not.toContain('Open');
    expect(l).not.toContain('Reveal in folder');
    expect(l).not.toContain('Copy file path');
    // The file name and the history delete still work without a path.
    expect(l).toContain('Copy file name');
    expect(l).toContain('Remove from history');
  });

  it('offers Resend only on a failed outgoing row', () => {
    expect(labels(mountMenu(makeTransfer({ direction: 'up', status: 'failed' })).wrapper))
      .toContain('Resend');
    expect(labels(mountMenu(makeTransfer({ direction: 'up', status: 'completed' })).wrapper))
      .not.toContain('Resend');
    expect(labels(mountMenu(makeTransfer({ direction: 'up', status: 'cancelled' })).wrapper))
      .not.toContain('Resend');
    expect(labels(mountMenu(makeTransfer({ direction: 'down', status: 'failed' })).wrapper))
      .not.toContain('Resend');
  });

  it('renders nothing for a row-less menu of another mode', () => {
    const { wrapper } = mountMenu(null, {
      contextMenu: { visible: true, x: 0, y: 0, mode: 'chat-message', target: null },
    });
    expect(labels(wrapper)).not.toContain('Remove from history');
  });
});

describe('context menu — transfer mode actions', () => {
  it('Open delegates to the store and closes the menu', async () => {
    const row = makeTransfer();
    const { wrapper, store } = mountMenu(row);

    await clickEntry(wrapper, 'Open');
    await flushPromises();

    expect(store.openTransferFile).toHaveBeenCalledWith(row.path);
    expect(store.contextMenu.visible).toBe(false);
  });

  it('Reveal in folder delegates to the store', async () => {
    const row = makeTransfer();
    const { wrapper, store } = mountMenu(row);

    await clickEntry(wrapper, 'Reveal in folder');
    await flushPromises();

    expect(store.revealTransferFile).toHaveBeenCalledWith(row.path);
  });

  it('Resend delegates to the same store helper the panel button uses', async () => {
    const row = makeTransfer({ id: 'tr-up', direction: 'up', status: 'failed' });
    const { wrapper, store } = mountMenu(row);

    await clickEntry(wrapper, 'Resend');
    await flushPromises();

    expect(store.retryTransfer).toHaveBeenCalledWith('tr-up');
  });

  it('Remove from history deletes by id and closes the menu', async () => {
    const row = makeTransfer({ id: 'tr-gone' });
    const { wrapper, store } = mountMenu(row);

    await clickEntry(wrapper, 'Remove from history');
    await flushPromises();

    expect(store.deleteTransferHistoryItem).toHaveBeenCalledWith('tr-gone');
    expect(store.contextMenu.visible).toBe(false);
  });

  it('copies the file name to the clipboard', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(globalThis.navigator, 'clipboard', {
      value: { writeText }, configurable: true, writable: true,
    });
    const { wrapper } = mountMenu(makeTransfer({ filename: 'report.pdf' }));

    await clickEntry(wrapper, 'Copy file name');
    await flushPromises();

    expect(writeText).toHaveBeenCalledWith('report.pdf');
  });
});
