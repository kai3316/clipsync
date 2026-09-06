// Behavioral tests for the device-card component — the action-button matrix
// and what each button does, replacing the old source-string assertions.

import { describe, it, expect, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { loadComponents, makeT, makeStore, makeDevice, makeApi } from './helpers.js';

const t = makeT('en');
const Card = loadComponents('device-card.js')['device-card'];

function mountCard(device, storeOverrides = {}) {
  const store = makeStore(storeOverrides);
  const wrapper = mount(Card, {
    props: { device },
    global: {
      provide: { store },
      mocks: { t },
    },
  });
  return { wrapper, store };
}

function actionLabels(wrapper) {
  return wrapper.findAll('button.device-card__action').map((b) => b.text());
}

beforeEach(() => {
  makeApi(); // reset the ClipsyncAPI fake before each test
});

describe('device-card action-button matrix', () => {
  it('offers no actions for the local device', () => {
    const { wrapper } = mountCard(makeDevice({ device_id: 'self-dev' }));
    expect(wrapper.find('.device-card__actions').exists()).toBe(false);
  });

  it('labels an unpaired discovered device "Pair", not "Connect"', () => {
    const { wrapper } = mountCard(makeDevice()); // unpaired, not connected
    expect(actionLabels(wrapper)).toContain('Pair');
    expect(actionLabels(wrapper)).not.toContain('Connect');
    expect(actionLabels(wrapper)).toContain('Remove');
  });

  it('offers Disconnect + Unpair for a connected paired device', () => {
    const { wrapper } = mountCard(makeDevice({ connected: true, paired: true }));
    const labels = actionLabels(wrapper);
    expect(labels).toContain('Disconnect');
    expect(labels).toContain('Unpair');
    expect(labels).not.toContain('Connect');
  });

  it('offers Connect + Unpair for a paired offline device', () => {
    const { wrapper } = mountCard(makeDevice({ paired: true, connected: false }));
    const labels = actionLabels(wrapper);
    expect(labels).toContain('Connect');
    expect(labels).toContain('Unpair');
    expect(labels).not.toContain('Disconnect');
  });
});

describe('device-card chat + test-connection gating', () => {
  // Both actions need a transport that can carry a frame right now: a live
  // session, or the relay while the peer is LAN-offline.  Offering them on an
  // unreachable paired device meant buttons that could only ever fail.

  it('hides chat and test-connection on a paired offline device with no relay path', () => {
    const { wrapper } = mountCard(makeDevice({ paired: true, connected: false }));
    const labels = actionLabels(wrapper);
    expect(labels).not.toContain('Chat');
    expect(labels).not.toContain('Test connection');
    // The card is not stripped bare — the actions that DO work remain.
    expect(labels).toContain('Connect');
    expect(labels).toContain('Remove');
  });

  it('keeps them on a paired offline device the host can still reach by relay', () => {
    const { wrapper } = mountCard(makeDevice({
      paired: true, connected: false, relay_reachable: true,
    }));
    const labels = actionLabels(wrapper);
    expect(labels).toContain('Chat');
    expect(labels).toContain('Test connection');
  });

  it('keeps them on a connected device regardless of the relay flag', () => {
    const { wrapper } = mountCard(makeDevice({ paired: true, connected: true }));
    const labels = actionLabels(wrapper);
    expect(labels).toContain('Chat');
    expect(labels).toContain('Test connection');
  });

  it('hides them on a discovered unpaired device', () => {
    const { wrapper } = mountCard(makeDevice()); // unpaired, offline
    const labels = actionLabels(wrapper);
    expect(labels).not.toContain('Chat');
    expect(labels).not.toContain('Test connection');
  });

  it('treats a payload with no relay_reachable field as unreachable', () => {
    const device = makeDevice({ paired: true, connected: false });
    delete device.relay_reachable;
    const { wrapper } = mountCard(device);
    expect(actionLabels(wrapper)).not.toContain('Test connection');
  });
});

describe('device-card button behavior', () => {
  it('connect on a discovered device fires the API and a "Connecting…" toast', async () => {
    const api = makeApi();
    const { wrapper, store } = mountCard(makeDevice());

    const pairBtn = wrapper.findAll('button.device-card__action')
      .find((b) => b.text() === 'Pair');
    await pairBtn.trigger('click');
    await flushPromises();

    expect(api.connectDevice).toHaveBeenCalledWith('peer-1');
    expect(store.showToast).toHaveBeenCalledWith('Connecting…', expect.anything());
  });

  it('disconnect fires the API', async () => {
    const api = makeApi();
    const { wrapper } = mountCard(makeDevice({ connected: true, paired: true }));

    const btn = wrapper.findAll('button.device-card__action')
      .find((b) => b.text() === 'Disconnect');
    await btn.trigger('click');
    await flushPromises();

    expect(api.disconnectDevice).toHaveBeenCalledWith('peer-1');
  });

  it('unpair asks for confirmation then fires the API', async () => {
    const api = makeApi();
    const { wrapper, store } = mountCard(makeDevice({ paired: true, connected: false }));

    const btn = wrapper.findAll('button.device-card__action')
      .find((b) => b.text() === 'Unpair');
    await btn.trigger('click');
    await flushPromises();

    expect(store.confirm).toHaveBeenCalled();
    expect(api.unpairDevice).toHaveBeenCalledWith('peer-1');
  });

  it('remove asks for confirmation, fires the API, and drops the device from the store', async () => {
    const device = makeDevice();
    const api = makeApi();
    const { wrapper, store } = mountCard(device, { devices: [device] });

    const btn = wrapper.findAll('button.device-card__action')
      .find((b) => b.text() === 'Remove');
    await btn.trigger('click');
    await flushPromises();

    expect(store.confirm).toHaveBeenCalled();
    expect(api.forgetDevice).toHaveBeenCalledWith('peer-1');
    expect(store.devices).toEqual([]);
  });
});
