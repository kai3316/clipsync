import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
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
  "chat.invite",
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
  // The three ways an internet pairing stops moving (a wait nobody answered, a
  // generated code that expired, two devices whose passwords differ) and the
  // two halves of a dial that failed / a pairing that ended unpairing.  All are
  // pushed by name, so any of them without a title would show a wire string.
  "netpair.waiting_failed",
  "netpair.code.expired",
  "netpair.decrypt_failed",
  "device.connect_failed",
  "pairing.resolved",
  "update.peer_unavailable",
  "update.peer_notice",
  "log.collected",
  "log.failed",
  "log.unavailable",
  "ui.data_recovery",
  // The window's own key for the non-destructive identity alert: the device's
  // certificate is the pinned one, so it is not the certificate-change dialog
  // and must not borrow that title.
  "ui.device_identity",
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

/** A notice that asks something.
 *
 *   Three of them now: a pairing request, an incoming file, and an invitation to
 *   chat.  The first two point at the page that answers them; the invitation
 *   carries its own two answers, because the surface that would otherwise hold
 *   them is the conversation the reader has not opened.
 */
describe("a notice carrying its own answers", () => {
  const actions = () => [
    { label: t("接受"), primary: true, run: vi.fn() },
    { label: t("拒绝"), run: vi.fn() },
  ];

  it("draws one button per answer and closes when one is pressed", async () => {
    const store = createApplicationStore();
    const pair = actions();
    store.state.notices.push({
      id: 1, title: "chat.invite", message: "Studio 邀请你聊天", actions: pair,
    });
    const wrapper = mount(NoticeStack, { props: { store } });
    const buttons = wrapper.findAll(".notice-actions button");
    expect(buttons.map((button) => button.text())).toEqual([t("接受"), t("拒绝")]);
    // The affirmative answer is the one that moves the conversation on, and the
    // accent is how a stack of two buttons says which one that is.
    expect(buttons[0].classes()).toContain("primary");

    await buttons[1].trigger("click");
    await flushPromises();
    expect(pair[1].run).toHaveBeenCalledTimes(1);
    expect(pair[0].run).not.toHaveBeenCalled();
    // Answered is over: the card does not stay behind asking the same question.
    expect(store.state.notices).toEqual([]);
    wrapper.unmount();
  });

  it("leaves a notice with nothing to answer without a row of buttons", () => {
    const store = createApplicationStore();
    store.state.notices.push({ id: 1, title: "device.connected", message: "Studio 已连接" });
    const wrapper = mount(NoticeStack, { props: { store } });
    expect(wrapper.find(".notice-actions").exists()).toBe(false);
    wrapper.unmount();
  });
});