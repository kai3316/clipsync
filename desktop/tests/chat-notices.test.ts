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

/** The page asks two questions, and the reader picks one:
 *
 *   the conversations this machine has, and the machines near it that it could
 *   start one with.  The second used to be the last section of the sessions
 *   rail, under every conversation in the list. */
describe("the chat page's two tabs", () => {
  const device = { id: "peer-one", name: "Pixel 8 Pro", paired: true, connection_state: "online" };
  const tabs = () => wrapper.findAll(".page-tabs button");

  beforeEach(() => {
    vi.mocked(bridge.chatDevices).mockResolvedValue({ devices: [device] } as any);
  });

  it("moves to the conversation once an invite is answered", async () => {
    // The invite was answered with a session id, which means the conversation
    // is already up — behind the other tab, where the reader would not see it.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, session_id: "two" }],
    } as any);
    vi.mocked(bridge.inviteChat).mockResolvedValue({ chat_session_id: "two" } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    await tabs()[1].trigger("click");
    await wrapper.get(".chat-device").trigger("click");
    await flushPromises();
    expect(wrapper.find(".chat-conversation").exists()).toBe(true);
    expect(wrapper.find(".chat-nearby").exists()).toBe(false);
  });
});
