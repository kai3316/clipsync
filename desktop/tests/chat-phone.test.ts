import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatView from "../src/components/ChatView.vue";
import { bridge } from "../src/api/bridge";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    chatDevices: vi.fn().mockResolvedValue({ devices: [] }),
    chatSessions: vi.fn().mockResolvedValue({ sessions: [], muted: [] }),
    chatMessages: vi.fn().mockResolvedValue({ messages: [] }),
    phoneChatList: vi.fn(),
    phoneChatSend: vi.fn(),
    phoneChatRead: vi.fn().mockResolvedValue({ ok: true }),
    companionRemoteStatus: vi.fn().mockResolvedValue({ enabled: true, token_set: true }),
  },
}));

const message = (id: number, sender: "phone" | "desktop", text: string) => ({
  id, sender, text, ts: 1_752_000_000, read: false,
});

let wrapper: ReturnType<typeof mount>;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(bridge.phoneChatList).mockResolvedValue({
    messages: [message(1, "phone", "hello from phone")],
    last_id: 1,
  } as any);
  vi.mocked(bridge.phoneChatSend).mockResolvedValue({ ok: true } as any);
  vi.mocked(bridge.companionRemoteStatus).mockResolvedValue({ enabled: true, token_set: true } as any);
});
afterEach(() => { wrapper?.unmount(); });

describe("the phone conversation merged into nearby chat", () => {
  it("sits in the rail with its latest line and opens from one click", async () => {
    wrapper = mount(ChatView);
    await flushPromises();
    const row = wrapper.get(".phone-chat-row");
    expect(row.text()).toContain("hello from phone");
    await row.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("hello from phone");
    expect(bridge.phoneChatRead).toHaveBeenCalled();
  });

  it("hides the phone conversation when remote access is off", async () => {
    vi.mocked(bridge.companionRemoteStatus).mockResolvedValue({ enabled: false } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".phone-chat-row").exists()).toBe(false);
    expect(bridge.phoneChatList).not.toHaveBeenCalled();
  });

  it("sends through phone_chat, not through the peer chat transport", async () => {
    wrapper = mount(ChatView);
    await flushPromises();
    await wrapper.get(".phone-chat-row").trigger("click");
    await flushPromises();
    const box = wrapper.get("textarea");
    await box.setValue("reply from desktop");
    await wrapper.get(".chat-composer").trigger("submit");
    await flushPromises();
    expect(bridge.phoneChatSend).toHaveBeenCalledExactlyOnceWith("reply from desktop");
  });

  it("asks only for what is new once the conversation has been read once", async () => {
    // The read runs every second and a half for as long as the page is open,
    // so a poll that asked for the whole conversation each time carried every
    // message the two had ever exchanged, over and over.
    wrapper = mount(ChatView);
    await flushPromises();
    expect(bridge.phoneChatList).toHaveBeenCalledWith(0);
    vi.mocked(bridge.phoneChatList).mockResolvedValue({
      messages: [message(2, "phone", "second")], last_id: 2,
    } as any);
    await wrapper.get('[aria-label="刷新聊天"]').trigger("click");
    await flushPromises();
    expect(bridge.phoneChatList).toHaveBeenLastCalledWith(1);
    // The rail only ever shows the latest line, so the proof that the answer
    // was added rather than substituted is the conversation itself.
    await wrapper.get(".phone-chat-row").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("hello from phone");
    expect(wrapper.text()).toContain("second");
  });

  it("clears its unread badge on the copy the page is counting", async () => {
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.get(".phone-chat-row .badge").text()).toBe("1");
    await wrapper.get(".phone-chat-row").trigger("click");
    await flushPromises();
    expect(bridge.phoneChatRead).toHaveBeenCalled();
    // The store marked them and the poll now only asks for what is new, so the
    // flag can never come back over the wire — the page settles its own list.
    expect(wrapper.find(".phone-chat-row .badge").exists()).toBe(false);
  });

  it("counts a line that lands below a reader who scrolled back", async () => {
    // Following again is not the whole of the fix: a reader up in the history
    // still has to be told, or the phone's conversation is the one transcript
    // where a message can arrive and nothing anywhere changes.
    wrapper = mount(ChatView);
    await flushPromises();
    await wrapper.get(".phone-chat-row").trigger("click");
    await flushPromises();
    const list = wrapper.get(".chat-messages").element as HTMLElement;
    Object.defineProperty(list, "scrollHeight", { get: () => 900, configurable: true });
    Object.defineProperty(list, "clientHeight", { get: () => 300, configurable: true });
    list.scrollTop = 0;
    await wrapper.get(".chat-messages").trigger("scroll");
    vi.mocked(bridge.phoneChatList).mockResolvedValue({
      messages: [message(2, "phone", "second")], last_id: 2,
    } as any);
    await wrapper.get('[aria-label="刷新聊天"]').trigger("click");
    await flushPromises();
    expect(list.scrollTop).toBe(0);
    expect(wrapper.get(".chat-unseen").text()).toContain("1 条新消息");
    await wrapper.get(".chat-unseen").trigger("click");
    expect(list.scrollTop).toBe(900);
    expect(wrapper.find(".chat-unseen").exists()).toBe(false);
  });

  it("follows what arrives while it is being read, and holds still when it is not", async () => {
    // A peer's transcript has followed its poll from the start; this one, read
    // by the same poll, never moved at all — a line arriving on a phone the
    // reader was talking to landed under the fold.
    wrapper = mount(ChatView);
    await flushPromises();
    await wrapper.get(".phone-chat-row").trigger("click");
    await flushPromises();
    const list = wrapper.get(".chat-messages").element as HTMLElement;
    // jsdom lays nothing out, so the two measurements a scroll container is
    // read through are put there: 900 of rows in a 300-high viewport.
    Object.defineProperty(list, "scrollHeight", { get: () => 900, configurable: true });
    Object.defineProperty(list, "clientHeight", { get: () => 300, configurable: true });
    // Up in the history, which is where a reader is when the next line lands.
    list.scrollTop = 0;
    await wrapper.get(".chat-messages").trigger("scroll");
    vi.mocked(bridge.phoneChatList).mockResolvedValue({
      messages: [message(2, "phone", "second")], last_id: 2,
    } as any);
    await wrapper.get('[aria-label="刷新聊天"]').trigger("click");
    await flushPromises();
    expect(list.scrollTop).toBe(0);
    // Back at the end, the next one is carried into view instead of waiting to
    // be found.
    list.scrollTop = 600;
    await wrapper.get(".chat-messages").trigger("scroll");
    vi.mocked(bridge.phoneChatList).mockResolvedValue({
      messages: [message(3, "phone", "third")], last_id: 3,
    } as any);
    await wrapper.get('[aria-label="刷新聊天"]').trigger("click");
    await flushPromises();
    expect(list.scrollTop).toBe(900);
  });
});
