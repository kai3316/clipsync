import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatView from "../src/components/ChatView.vue";
import { bridge } from "../src/api/bridge";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    chatDevices: vi.fn(), chatSessions: vi.fn(), chatMessages: vi.fn(),
    openChatFile: vi.fn(), markChatRead: vi.fn(),
  },
}));

const session = (id: string) => ({
  session_id: id, peer_id: `peer-${id}`, peer_name: id, status: "closed",
  online: false, unread: 0,
});
const attachment = (extra = {}) => ({
  entry_id: "entry", kind: "file", outgoing: false, ts: 1,
  file_name: "report.txt", status: "done", saved_path: "C:\\received\\report.txt",
  transfer_id: "transfer", ...extra,
});
let wrapper: ReturnType<typeof mount>;
const openButton = () => wrapper.findAll("button").find(button => button.text() === "打开");

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  vi.mocked(bridge.chatDevices).mockResolvedValue({ devices: [] });
  vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [session("one")] } as any);
  vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [attachment()] } as any);
  vi.mocked(bridge.openChatFile).mockResolvedValue({ ok: true });
});
afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
});

describe("saved chat attachments", () => {
  it("opens received files in a closed session using IDs, never a renderer path", async () => {
    wrapper = mount(ChatView);
    await flushPromises();
    await openButton()!.trigger("click");
    await flushPromises();
    expect(bridge.openChatFile).toHaveBeenCalledExactlyOnceWith("one", "transfer");
  });

  it("does not offer opening outgoing, unfinished or unsaved attachments", async () => {
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [
      attachment({ entry_id: "a", outgoing: true }),
      attachment({ entry_id: "b", status: "await_accept" }),
      attachment({ entry_id: "c", saved_path: "" }),
      attachment({ entry_id: "d", transfer_id: "" }),
      attachment({ entry_id: "e", status: "failed" }),
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(openButton()).toBeUndefined();
    expect(bridge.openChatFile).not.toHaveBeenCalled();
  });

  it("blocks duplicate opens and keeps failures visible through polling", async () => {
    let reject!: (reason: unknown) => void;
    vi.mocked(bridge.openChatFile).mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    wrapper = mount(ChatView);
    await flushPromises();
    await openButton()!.trigger("click");
    expect(openButton()!.attributes("disabled")).toBeDefined();
    await openButton()!.trigger("click");
    expect(bridge.openChatFile).toHaveBeenCalledTimes(1);
    reject(new Error("Received file is not available"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("Received file is not available");
    expect(openButton()!.attributes("disabled")).toBeUndefined();
  });

  it("reports a negative open acknowledgement", async () => {
    vi.mocked(bridge.openChatFile).mockResolvedValue({ ok: false });
    wrapper = mount(ChatView);
    await flushPromises();
    await openButton()!.trigger("click");
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe("无法打开附件");
  });

  it("discards late attachment messages from the previously selected session", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [session("one"), session("two")],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    let resolve!: (value: any) => void;
    vi.mocked(bridge.chatMessages).mockImplementation(id => id === "one"
      ? new Promise(done => { resolve = done; }) : Promise.resolve({ messages: [] }));
    await vi.advanceTimersByTimeAsync(1500);
    await wrapper.findAll(".chat-session")[1].trigger("click");
    await flushPromises();
    resolve({ messages: [attachment()] });
    await flushPromises();
    expect(openButton()).toBeUndefined();
    expect(bridge.openChatFile).not.toHaveBeenCalled();
  });
});
