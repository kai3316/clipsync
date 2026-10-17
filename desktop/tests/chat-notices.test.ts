import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatView from "../src/components/ChatView.vue";
import { bridge } from "../src/api/bridge";
import { systemText } from "../src/i18n/chat";
import { setLocale } from "../src/i18n";
import type { ChatEntry } from "../src/api/types";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    chatDevices: vi.fn(), chatSessions: vi.fn(), chatMessages: vi.fn(),
    openChatFile: vi.fn(), markChatRead: vi.fn(), setChatMuted: vi.fn(),
    inviteChat: vi.fn(),
  },
}));

/** A system row as the sidecar actually builds one: no text, a dotted key. */
const notice = (text_key: string, fmt: Record<string, unknown> = {}) => ({
  entry_id: "entry", kind: "system", outgoing: false, ts: 1757812000,
  text: "", text_key, fmt, file_name: "", file_size: 0, mime: "",
  status: "", fraction: 1, saved_path: "", transfer_id: "", msg_id: "",
}) as unknown as ChatEntry;

const session = {
  session_id: "one", peer_id: "peer-one", peer_name: "Pixel 8 Pro", status: "closed",
  online: false, unread: 0, fingerprint_short: "", created_ts: 0, last_activity_ts: 0,
  last_preview: "",
};

let wrapper: ReturnType<typeof mount>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  vi.mocked(bridge.chatDevices).mockResolvedValue({ devices: [] });
  vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [session] } as any);
});
afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  setLocale("zh-CN");
});

describe("system notices", () => {
  it("renders the sentence a text_key stands for, never the key", () => {
    expect(systemText(notice("chat.system.peer_offline"), "Pixel 8 Pro"))
      .toBe("Pixel 8 Pro 已离线——消息将在其恢复在线后送达。");
    expect(systemText(notice("chat.system.session_closed_by_peer"), "Pixel 8 Pro"))
      .toBe("Pixel 8 Pro 已关闭会话。");
  });

  it("falls back to the generic label for a key this front does not know", () => {
    // Not the dotted key: a notice added to the sidecar and not translated here
    // should read as a system message, not as plumbing.
    expect(systemText(notice("chat.system.something_new"))).toBe("系统消息");
  });
});

describe("the conversation view", () => {
  it("shows a system row as a sentence, using the open session's peer name", async () => {
    vi.mocked(bridge.chatMessages).mockResolvedValue({
      messages: [notice("chat.system.session_closed_by_peer")],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".chat-message.system span").text())
      .toBe("Pixel 8 Pro 已关闭会话。");
  });
});

/** The devices and the conversations share one rail.
 *
 *   They were two tabs, and before that two sections of one rail with the
 *   devices last.  Both put the way to start a chat somewhere the reader had to
 *   already know about, which is what these two cover: the devices are on
 *   screen without a tab to find, and a device already being talked to is not
 *   offered a second time. */
describe("the rail the devices and the conversations share", () => {
  const device = { id: "peer-one", name: "Pixel 8 Pro", paired: true, connection_state: "online" };
  const devices = () => wrapper.findAll(".chat-device");
  const rows = () => wrapper.findAll(".chat-session");

  beforeEach(() => {
    vi.mocked(bridge.chatDevices).mockResolvedValue({ devices: [device] } as any);
  });

  it("shows the devices with no tab to find first", async () => {
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".page-tabs").exists()).toBe(false);
    expect(devices().map((row) => row.text())).toEqual(["Pixel 8 Pro已配对"]);
  });

  it("opens the conversation once an invite is answered", async () => {
    // The invite was answered with a session id, which means the conversation
    // is already up; the reader clicked a device to get one, so being left
    // looking at the list would hide the thing the click was for.
    //
    // The first read is the one before the click, and it has to come back
    // without that conversation: a live session for this peer is what takes the
    // device off the list, and there would be nothing to click.
    vi.mocked(bridge.chatSessions)
      .mockResolvedValueOnce({ sessions: [] } as any)
      .mockResolvedValue({
        sessions: [{ ...session, session_id: "two", status: "active" }],
      } as any);
    vi.mocked(bridge.inviteChat).mockResolvedValue({ chat_session_id: "two" } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    await devices()[0].trigger("click");
    await flushPromises();
    expect(wrapper.find(".chat-conversation").exists()).toBe(true);
  });

  it("drops a device from the device list once it has a live conversation", async () => {
    // Its conversation is one row below and opens the same session, so keeping
    // it here would be two rows for one device.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active" }],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(devices()).toHaveLength(0);
    expect(rows()).toHaveLength(1);
    // And the page says why the list is empty rather than leaving a gap.
    expect(wrapper.get(".chat-nearby-empty").text()).toBe("附近没有可聊天的设备。");
  });

  it("offers a device again once its conversation is closed", async () => {
    // The mocked session is `closed`, so the device is free to talk to — and
    // dropping it would leave no way to start the next conversation.
    wrapper = mount(ChatView);
    await flushPromises();
    expect(devices()).toHaveLength(1);
  });
});
