import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import NoticeStack from "../src/components/NoticeStack.vue";
import { t } from "../src/i18n";
import { createApplicationStore } from "../src/stores/application";

/**
 * Every event name `pushNotice` can be handed, plus the `ui.*` keys `toast`
 * uses.  Spelled out rather than derived from the store: the point is to catch
 * an event that was pushed without a title, and a list read off the same table
 * under test could not do that.
 */
const PUSHED_NOTICE_NAMES = [
  "runtime.error",
  "pairing.request",
  "pairing.failed",
  "transfer.request",
  "chat.message",
  "chat.connect_timeout",
  "url.received",
  "device.connected",
  "device.disconnected",
  "device.connection_rejected",
  "device.connection_unreachable",
  "sync.redacted",
  "clip.file.denied",
  "device.security_alert",
  "netpair.peer.changed",
  "update.peer_unavailable",
  "update.peer_notice",
  "log.collected",
  "log.failed",
  "log.unavailable",
  "ui.data_recovery",
  "ui.overview",
  "ui.history",
  "ui.favorites",
  "ui.transfers",
  "ui.chat",
  "ui.devices",
  "ui.ai",
  "ui.settings",
  "ui.sync",
] as const;

describe("notice titles", () => {
  it("words every pushed event instead of showing its wire name", () => {
    const store = createApplicationStore();
    store.state.notices.push(
      ...PUSHED_NOTICE_NAMES.map((title, index) => ({ id: index, title, message: "x" })),
    );
    const wrapper = mount(NoticeStack, { props: { store } });
    const titles = wrapper.findAll("strong").map((node) => node.text());

    // The three areas whose notices used to fall through to the raw event
    // name: an update exchange, a requested device log, an internet pairing.
    expect(titles).toContain(t("\u4e92\u8054\u7f51\u914d\u5bf9"));
    expect(titles).toContain(t("\u8f6f\u4ef6\u66f4\u65b0"));
    expect(titles).toContain(t("\u8bbe\u5907\u65e5\u5fd7"));

    // No notice may show the machine name it was keyed by.
    for (const name of PUSHED_NOTICE_NAMES) {
      expect(titles).not.toContain(name);
    }
    wrapper.unmount();
  });
});