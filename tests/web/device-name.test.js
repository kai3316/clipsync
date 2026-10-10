// One name for a device: what the user chose, above what the peer published.
//
// Reported as "设备名有的时候会变成'试试'……历史记录里下面显示的名字不对".  Measured on
// the reporting install, one device held both of these at once:
//
//     device_name = "Kais-MacBook"   # the name the peer published about itself
//     note        = "试试"            # the name the user gave it on this machine
//
// This dashboard's own rename writes `note` (`context-menu.js::renameDevice`),
// while the card, the transfer selector and three toasts read `device_name`
// first — so a device renamed here kept the peer's own name on the very card
// whose menu performed the rename.  The rule lives once, in
// `ClipsyncFormat.deviceName`, and these are the cases that pin it.

import { describe, it, expect, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { loadComponents, loadFormat, makeT, makeStore, makeDevice, makeApi } from './helpers.js';

const t = makeT('en');
const format = loadFormat();
const Card = loadComponents('device-card.js')['device-card'];

const REPORTED = {
  device_id: '483fa196a05a',
  device_name: 'Kais-MacBook',
  note: '试试',
};

beforeEach(() => {
  makeApi();
});

describe('ClipsyncFormat.deviceName', () => {
  it("prefers the user's own name for the device", () => {
    expect(format.deviceName(REPORTED)).toBe('试试');
  });

  it('falls back to the name the peer published, then to the id', () => {
    expect(format.deviceName({ ...REPORTED, note: '' })).toBe('Kais-MacBook');
    expect(format.deviceName({ device_id: 'peer-1' })).toBe('peer-1');
  });

  it('reads the runtime snapshot spelling too (the phone page rows)', () => {
    // /api/chat/devices carries `name`; /api/devices carries `device_name`.
    expect(format.deviceName({ name: 'Kais-MacBook', note: '' })).toBe('Kais-MacBook');
    expect(format.deviceName({ name: 'Kais-MacBook', note: '试试', peer_id: 'p1' })).toBe('试试');
    expect(format.deviceName({ peer_id: 'p1' })).toBe('p1');
  });

  it('does not treat whitespace as a name', () => {
    expect(format.deviceName({ device_name: 'Kais-MacBook', note: '   ' })).toBe('Kais-MacBook');
  });

  it('answers empty rather than throwing for nothing', () => {
    expect(format.deviceName(null)).toBe('');
    expect(format.deviceName({})).toBe('');
  });
});

describe('the device card is headed by the name the user chose', () => {
  function mountCard(device) {
    const store = makeStore();
    return mount(Card, {
      props: { device },
      global: { provide: { store }, mocks: { t } },
    });
  }

  it('shows the user\'s name, not the one the peer published', () => {
    const wrapper = mountCard(makeDevice(REPORTED));
    expect(wrapper.find('.device-card__name').text()).toBe('试试');
  });

  it('still shows the peer\'s name for a device nobody renamed', () => {
    const wrapper = mountCard(makeDevice({ device_name: 'Kais-MacBook', note: '' }));
    expect(wrapper.find('.device-card__name').text()).toBe('Kais-MacBook');
  });

  it('opens the conversation under that same name', async () => {
    const api = makeApi();
    const wrapper = mountCard(makeDevice({ ...REPORTED, paired: true, connected: true }));
    await wrapper.vm.startChat();
    expect(api.chatInvite).toHaveBeenCalledWith(REPORTED.device_id, '试试');
  });
});
