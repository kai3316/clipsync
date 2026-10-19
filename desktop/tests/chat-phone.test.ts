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
});
