// A check that is still coming up is neither a pass nor a failure.
//
// `summarize` leaves `pending` checks out of the verdict, so a surface that only
// reads `ok` has to do the same or it contradicts the verdict.  The panel's
// legacy flat list read `ok === false` as a red ✕ — which is how the very state
// the fix describes (our own mDNS record still being published) would have been
// drawn as a failure next to a summary that said everything was fine.

import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import { loadComponents, makeT, makeStore } from './helpers.js';

const t = makeT('en');
const Panel = loadComponents('diagnostics-panel.js')['diagnostics-panel'];

/** The panel with its flat list already revealed, which the timers normally do. */
async function mountRevealed(checks) {
  const wrapper = mount(Panel, {
    global: { provide: { store: makeStore() }, mocks: { t } },
  });
  wrapper.vm.diagChecks = checks;
  wrapper.vm.diagSummary = 'ok';
  wrapper.vm.diagScanning = false;
  wrapper.vm.diagRevealed = checks.length;
  // The reveal is driven by timers in the component; here it is set outright, so
  // the DOM has to be given its turn before anything is read off it.
  await wrapper.vm.$nextTick();
  return wrapper;
}

function marks(wrapper) {
  return wrapper.findAll('.diag-check__status').map((el) => el.text());
}

describe('a pending check in the diagnostics panel', () => {
  it('is drawn as neither a pass nor a failure', async () => {
    const wrapper = await mountRevealed([
      { id: 'server_port', ok: true, detail: 'listening' },
      { id: 'advertising', ok: false, pending: true, detail: 'announcing this device' },
      { id: 'network', ok: false, detail: 'no LAN address' },
    ]);
    expect(marks(wrapper)).toEqual(['✓', '·', '✕']);
  });

  it('is not marked with the failure class, unlike a settled failure', async () => {
    const wrapper = await mountRevealed([
      { id: 'advertising', ok: false, pending: true, detail: 'announcing this device' },
      { id: 'network', ok: false, detail: 'no LAN address' },
    ]);
    const rows = wrapper.findAll('.diag-check');
    expect(rows.length).toBe(2);
    expect(rows[0].classes()).not.toContain('diag-check--fail');
    // The control: the settled one beside it is still drawn as the failure it is.
    expect(rows[1].classes()).toContain('diag-check--fail');
  });
});
