import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatView from "../src/components/ChatView.vue";
import { bridge } from "../src/api/bridge";
import { closeContextMenu, contextMenuState, runContextMenuItem } from "../src/lib/context-menu";
import { chatDrafts } from "../src/lib/chat-drafts";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    chatDevices: vi.fn(), chatSessions: vi.fn(), chatMessages: vi.fn(),
    phoneChatList: vi.fn().mockResolvedValue({ messages: [], last_id: 0 }),
    phoneChatSend: vi.fn(), phoneChatRead: vi.fn().mockResolvedValue({ ok: true }),
    // Remote access off, so the phone conversation — a fixed contact on this
    // rail — is not in the list these cases count rows in.
    companionRemoteStatus: vi.fn().mockResolvedValue({ enabled: false, token_set: false }),
    openChatFile: vi.fn(), revealChatFile: vi.fn(), markChatRead: vi.fn(),
    deleteChatMessage: vi.fn(),
    setChatMuted: vi.fn(), inviteChat: vi.fn(), sendChatText: vi.fn(),
    sendChatTyping: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

/** A session as the runtime reports it.  Closed and offline, which is what a
 *  conversation looks like after the peer has gone away. */
const session = {
  session_id: "one", peer_id: "peer-one", peer_name: "Pixel 8 Pro", status: "closed",
  online: false, unread: 0, fingerprint_short: "", created_ts: 0, last_activity_ts: 0,
  last_preview: "",
};

let wrapper: ReturnType<typeof mount>;

beforeEach(() => {
  // Clear rather than reset: the mock table's own answers (the phone list, the
  // typing ping) are part of how this page behaves when nobody is watching, and
  // a reset leaves them undefined and the page throwing on its own keystroke.
  vi.clearAllMocks();
  // The drafts are module state, so they reach past a case's own mount.
  chatDrafts.clear();
  vi.mocked(bridge.chatDevices).mockResolvedValue({ devices: [] } as any);
  vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [] } as any);
  vi.mocked(bridge.inviteChat).mockResolvedValue({ chat_session_id: "" } as any);
});
afterEach(() => { wrapper?.unmount(); closeContextMenu(); });

describe("a conversation that has ended", () => {
  it("says why there is nothing to type into, and offers the way back in", async () => {
    // The composer is not rendered for a closed session, and it used to be
    // absent with no word about why: the reader was left with a transcript
    // they could read and not answer, and no way to start over from here.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [session] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".chat-composer").exists()).toBe(false);
    expect(wrapper.text()).toContain("会话已关闭");
    await wrapper.findAll("button").find(button => button.text().includes("重新发起"))!.trigger("click");
    await flushPromises();
    // The peer's row on the rail can reopen it too; this is the one next to
    // the messages it would continue.
    expect(bridge.inviteChat).toHaveBeenCalledWith("peer-one", "Pixel 8 Pro");
  });

  it("says which peer refused, for a conversation that was never opened", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "declined_remote" }],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.text()).toContain("Pixel 8 Pro 拒绝了邀请");
    await wrapper.findAll("button").find(button => button.text().includes("重新发起"))!.trigger("click");
    await flushPromises();
    expect(bridge.inviteChat).toHaveBeenCalledWith("peer-one", "Pixel 8 Pro");
  });

  it("lets go of a conversation the runtime has dropped", async () => {
    // The runtime reaps them: an invitation nobody answered, or a closed
    // conversation an hour past its last message.  Its row leaves the rail, and
    // the page beside it used to keep the id — so the pane fell through to
    // 选择一个会话 over a list that still held conversations, with nothing
    // saying the one being read had gone.
    const two = { ...session, session_id: "two", peer_id: "peer-two", peer_name: "Desk" };
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }, two],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(bridge.chatMessages).toHaveBeenLastCalledWith("one", expect.any(Number));
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [two] } as any);
    await wrapper.get('[aria-label="刷新聊天"]').trigger("click");
    await flushPromises();
    // What is on the right is a conversation the list can still show the row
    // for, and the page no longer offers to start one from scratch.
    expect(bridge.chatMessages).toHaveBeenLastCalledWith("two", expect.any(Number));
    expect(wrapper.text()).not.toContain("选择一个会话");
  });
});

describe("a peer that is not there", () => {
  it("says a message cannot go out before one is typed, and holds the send button", async () => {
    // Sending to an offline peer is refused outright, so a reader used to
    // learn it from a failure band after pressing Enter.  The composer stays
    // open — a box closing under a half-written message is worse — but the
    // reason is on screen and the button waits for the peer.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: false }],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.text()).toContain("对方当前离线");
    await wrapper.get('[aria-label="聊天消息"]').setValue("hello");
    expect(wrapper.get('[aria-label="发送消息"]').attributes("disabled")).toBeDefined();
    expect(bridge.sendChatText).not.toHaveBeenCalled();
  });

  it("says nothing of the sort while the peer is there", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }],
    } as any);
    vi.mocked(bridge.sendChatText).mockResolvedValue({ ok: true });
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.text()).not.toContain("对方当前离线");
    await wrapper.get('[aria-label="聊天消息"]').setValue("hello");
    expect(wrapper.get('[aria-label="发送消息"]').attributes("disabled")).toBeUndefined();
    // The form, not the button: jsdom does not submit a form from a click on
    // its own submit button, and the composer is the thing that answers.
    await wrapper.get(".chat-composer").trigger("submit");
    await flushPromises();
    expect(bridge.sendChatText).toHaveBeenCalledWith("one", "hello");
    // One read for the page, one for what the send brought back: the refresh
    // that follows a send carries the transcript, and asking for it beside the
    // refresh read the same page twice on every message sent.
    expect(bridge.chatMessages).toHaveBeenCalledTimes(2);
  });
});

describe("what is being typed", () => {
  it("stays with the conversation it was typed into", async () => {
    // One box for every conversation carried a half-written message into
    // whichever conversation the reader opened next, where Enter would send
    // it to the wrong device.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [
      { ...session, session_id: "one", peer_name: "One", status: "active", online: true },
      { ...session, session_id: "two", peer_id: "peer-two", peer_name: "Two", status: "active", online: true },
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    // The row is a container now, so the click lands on the button that opens
    // it — which is where a reader clicks too.  See U80.
    const row = (name: string) => wrapper.findAll(".chat-session")
      .find(candidate => candidate.text().includes(name))!
      .find(".chat-session__open");
    await wrapper.get('[aria-label="聊天消息"]').setValue("for one");
    await row("Two").trigger("click");
    await flushPromises();
    expect((wrapper.get('[aria-label="聊天消息"]').element as HTMLTextAreaElement).value).toBe("");
    await row("One").trigger("click");
    await flushPromises();
    expect((wrapper.get('[aria-label="聊天消息"]').element as HTMLTextAreaElement).value).toBe("for one");
  });

  it("outlives the page", async () => {
    // The window draws one page at a time, so stepping to the history unmounts
    // this one — and a draft held inside it went with it.  A reader who went to
    // check a clip and came back found the box empty, which is the one thing a
    // draft is for.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [
      { ...session, session_id: "one", peer_name: "One", status: "active", online: true },
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    await wrapper.get('[aria-label="聊天消息"]').setValue("half written");
    wrapper.unmount();
    wrapper = mount(ChatView);
    await flushPromises();
    expect((wrapper.get('[aria-label="聊天消息"]').element as HTMLTextAreaElement).value)
      .toBe("half written");
  });

  it("says once that it is typing, and once that it has stopped", async () => {
    // A keystroke is not a change of state.  Every letter used to be its own
    // call, and the sidecar answers all but one of them with the same
    // suppression — so the round trip was paid for an answer that was always
    // "still typing".
    vi.useFakeTimers();
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const box = wrapper.get('[aria-label="聊天消息"]');
    for (const value of ["h", "he", "hel", "hell", "hello"]) {
      await box.setValue(value);
    }
    expect(bridge.sendChatTyping).toHaveBeenCalledTimes(1);
    expect(bridge.sendChatTyping).toHaveBeenLastCalledWith("one", true);
    // Stopping is the one that has to get through: the peer's indicator is
    // what goes away on it.
    await vi.advanceTimersByTimeAsync(2000);
    expect(bridge.sendChatTyping).toHaveBeenLastCalledWith("one", false);
    vi.useRealTimers();
  });
});

describe("a window nobody is looking at", () => {
  it("stops reading the conversation while it is away, and reads at once on return", async () => {
    // The poll reads every session, every message and the device list once
    // every second and a half; a minimized window has nobody looking at any of
    // it.  Coming back is the moment the transcript is read again, so it is
    // fetched then rather than an interval and a half later.
    vi.useFakeTimers();
    vi.mocked(bridge.chatSessions).mockClear();
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }],
    } as any);
    const hide = async (hidden: boolean) => {
      Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
      await flushPromises();
    };
    wrapper = mount(ChatView);
    await flushPromises();
    const reads = vi.mocked(bridge.chatSessions).mock.calls.length;
    await vi.advanceTimersByTimeAsync(4500);
    expect(vi.mocked(bridge.chatSessions).mock.calls.length).toBeGreaterThan(reads);
    await hide(true);
    const before = vi.mocked(bridge.chatSessions).mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);
    expect(vi.mocked(bridge.chatSessions).mock.calls.length).toBe(before);
    await hide(false);
    expect(vi.mocked(bridge.chatSessions).mock.calls.length).toBe(before + 1);
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    vi.useRealTimers();
  });
});

describe("a muted conversation", () => {
  it("is quiet in the list as well as on its bell", async () => {
    // The bell on the row already said it by its own shape, which is one icon
    // per row to read: a glance down the list should show which conversations
    // are muted without stopping on each.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }], muted: [],
    } as any);
    vi.mocked(bridge.setChatMuted).mockResolvedValue({ muted: ["peer-one"] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const row = () => wrapper.findAll(".chat-session")
      .find(candidate => !candidate.classes().includes("phone-chat-row"))!;
    expect(row().classes()).not.toContain("chat-session--muted");
    await row().get('[aria-label="静音"]').trigger("click");
    await flushPromises();
    expect(row().classes()).toContain("chat-session--muted");
  });
});

/** The first read of the page is the one nothing is cached behind, and it was
 *  the one asking for the same transcript twice: the refresh opened the
 *  conversation it found, then read the one it had just opened. */
describe("the page's first read", () => {
  it("opens the conversation it finds with one read, not two", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...session, status: "active", online: true }],
    } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(bridge.chatMessages).toHaveBeenCalledTimes(1);
  });
});

/** The transcript is read once every second and a half while the page is open,
 *  so how much of it a poll carries is how expensive an idle page is. */
describe("a conversation longer than one page", () => {
  const live = { ...session, status: "active", online: true };

  function entries(count: number) {
    return Array.from({ length: count }, (_, index) => ({
      entry_id: `e${index}`, kind: "text", outgoing: false, ts: index,
      text: `message ${index}`, status: "sent",
    }));
  }

  /** A conversation of `total` messages, answering the page it is asked for the
   *  way the sidecar does: the last `limit` entries of it. */
  function transcript(total: number) {
    const all = entries(total);
    return async (_id: string, limit = 0) =>
      ({ messages: all.slice(all.length - limit) } as any);
  }

  it("asks for the newest page, and says what it is not carrying", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockImplementation(transcript(600));
    wrapper = mount(ChatView);
    await flushPromises();
    // One row more than the page holds, because that row is the only evidence
    // there is that the conversation goes on above it.
    expect(bridge.chatMessages).toHaveBeenCalledWith("one", 201);
    // Said rather than left out, or a reader who walked to the top of the list
    // would be entitled to read what is on screen as the whole thing.
    expect(wrapper.text()).toContain("只显示最近 200 条消息");
    await wrapper.findAll("button").find(button => button.text().includes("加载更早的"))!.trigger("click");
    await flushPromises();
    expect(bridge.chatMessages).toHaveBeenLastCalledWith("one", 401);
    expect(wrapper.text()).toContain("只显示最近 400 条消息");
  });

  it("claims no history for a conversation of exactly one page", async () => {
    // Asking for a ceiling and receiving it was the old evidence of more, and
    // a conversation of exactly one page answers that question with a yes: the
    // page said it was holding the newest 200 of something and then, on the
    // click that would find out, changed nothing.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockImplementation(transcript(200));
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".chat-earlier").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("只显示最近");
  });

  it("holds the reader's place when a stretch is added above them", async () => {
    // Rows going in above the ones being read make a taller list under the
    // same scrollTop, which is the reader landing somewhere else than where
    // they asked for more of.  jsdom lays nothing out, so the height the page
    // measures is put there — and grows when the answer arrives, the way it
    // would once the rows were drawn.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockImplementation(transcript(600));
    wrapper = mount(ChatView);
    await flushPromises();
    const list = wrapper.get(".chat-messages").element as HTMLElement;
    let height = 1000;
    Object.defineProperty(list, "scrollHeight", { get: () => height, configurable: true });
    list.scrollTop = 0;
    // The answer to the click is what makes the list taller.
    vi.mocked(bridge.chatMessages).mockImplementation(async (id: string, limit = 0) => {
      height = 2200;
      return transcript(600)(id, limit);
    });
    await wrapper.findAll("button").find(button => button.text().includes("加载更早的"))!.trigger("click");
    await flushPromises();
    expect(list.scrollTop).toBe(1200);
  });

  it("runs one read at a time, so a double click cannot put the shorter page back", async () => {
    // Two reads with two ceilings can answer out of order, and the shorter one
    // arriving last would put the older page back on screen — with the reader's
    // place measured against neither of them.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockImplementation(transcript(600));
    wrapper = mount(ChatView);
    await flushPromises();
    // Hold the answer, so the second click lands while the first read is still
    // out — which is exactly what a double click is.
    let answer: (page: unknown) => void = () => {};
    vi.mocked(bridge.chatMessages).mockImplementation(
      () => new Promise((resolve) => { answer = resolve; }) as any);
    const button = wrapper.findAll("button").find(b => b.text().includes("加载更早的"))!;
    await button.trigger("click");
    await button.trigger("click");
    answer({ messages: entries(401) });
    await flushPromises();
    expect(vi.mocked(bridge.chatMessages).mock.calls.length).toBe(2);
    expect(bridge.chatMessages).toHaveBeenLastCalledWith("one", 401);
  });

  it("says nothing about paging when the conversation fits on one", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: entries(3) } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(bridge.chatMessages).toHaveBeenCalledWith("one", 201);
    expect(wrapper.find(".chat-earlier").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("只显示最近");
  });
});

/** A list that holds still under a reader who has scrolled back is a list that
 *  can receive a message without them knowing.  The poll behind this page runs
 *  every second and a half, so the notice is what stands between "nothing is
 *  happening" and a transcript that grew while they were reading yesterday. */
describe("a message that arrives while the reader is up in the history", () => {
  const live = { ...session, status: "active", online: true };

  function page(from: number, count: number) {
    return Array.from({ length: count }, (_, index) => ({
      entry_id: `e${from + index}`, kind: "text", outgoing: false,
      ts: from + index, text: `message ${from + index}`, status: "sent",
    }));
  }

  /** The list as a scroll container with a viewport: jsdom lays nothing out, so
   *  the two measurements the listener compares are put there. */
  function viewport(list: HTMLElement, height: number, visible: number) {
    Object.defineProperty(list, "scrollHeight", { get: () => height, configurable: true });
    Object.defineProperty(list, "clientHeight", { get: () => visible, configurable: true });
  }

  it("counts what landed below, and takes the reader back down when asked", async () => {
    vi.useFakeTimers();
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: page(0, 3) } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const list = wrapper.get(".chat-messages").element as HTMLElement;
    viewport(list, 1000, 400);
    list.scrollTop = 0;
    // Scrolling back is the reader saying they are reading upwards.
    await wrapper.get(".chat-messages").trigger("scroll");
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: page(0, 5) } as any);
    await vi.advanceTimersByTimeAsync(1500);
    // Two rows arrived and the transcript stayed where it was, which is the
    // point — and the reason it has to say so.
    expect(list.scrollTop).toBe(0);
    expect(wrapper.get(".chat-unseen").text()).toContain("2 条新消息");
    await wrapper.get(".chat-unseen").trigger("click");
    expect(list.scrollTop).toBe(1000);
    expect(wrapper.find(".chat-unseen").exists()).toBe(false);
    vi.useRealTimers();
  });

  it("says nothing to a reader who is already at the end", async () => {
    // The same two messages, with the list sitting at its newest row: the poll
    // carries the view down to them, so there is nothing to have missed.
    vi.useFakeTimers();
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: page(0, 3) } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const list = wrapper.get(".chat-messages").element as HTMLElement;
    viewport(list, 1000, 400);
    list.scrollTop = 600;
    await wrapper.get(".chat-messages").trigger("scroll");
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: page(0, 5) } as any);
    await vi.advanceTimersByTimeAsync(1500);
    expect(list.scrollTop).toBe(1000);
    expect(wrapper.find(".chat-unseen").exists()).toBe(false);
    vi.useRealTimers();
  });
});

/** A row in the rail used to be a `role="button"` div, which meant every key a
 *  button answers was this page's to implement — and the bell that mutes a peer
 *  sat inside that button, which is invalid ARIA and cost a screen reader the
 *  control altogether.  The row is a plain container now, opening it is a real
 *  button, and the bell is that button's sibling.  See U80. */
describe("a conversation row, from the keyboard", () => {
  const first = {
    ...session, session_id: "one", peer_id: "peer-one", peer_name: "Pixel 8 Pro",
    status: "active", online: true,
  };
  const second = {
    ...session, session_id: "two", peer_id: "peer-two", peer_name: "Laptop",
    status: "active", online: true,
  };

  beforeEach(async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [first, second] } as any);
  });

  const row = (name: string) =>
    wrapper.findAll(".chat-session").find(node => node.text().includes(name))!;
  const open = (name: string) => row(name).find(".chat-session__open");
  const bell = (name: string) =>
    row(name).findAll("button").find(node => (node.attributes("aria-label") || "").includes("静音"))!;
  const heading = () => wrapper.findAll(".chat-conversation .chat-heading")[0].text();

  it("opens from a button, which is what answers Enter and Space", async () => {
    // The page opens on the first conversation it finds; the second is reached
    // by its row.  Enter and Space are the browser's to answer now, because the
    // control is a button — there is no key handler left to get them wrong.
    wrapper = mount(ChatView);
    await flushPromises();
    expect(heading()).toContain("Pixel 8 Pro");
    expect(open("Laptop").element.tagName).toBe("BUTTON");
    expect(open("Laptop").attributes("type")).toBe("button");
    // No hand-rolled role: the element itself is the control.
    expect(open("Laptop").attributes("role")).toBeUndefined();
    expect(open("Laptop").attributes("aria-current")).toBeUndefined();
    await open("Laptop").trigger("click");
    await flushPromises();
    expect(heading()).toContain("Laptop");
    expect(open("Laptop").attributes("aria-current")).toBe("true");
  });

  it("keeps 静音 a control of its own, beside the row's button and not in it", async () => {
    // A button inside a `role="button"` is invalid ARIA, and the inner one is
    // the one a reader lost: 静音 was inside the row's own button, so some
    // screen readers announced the row and never the control.
    vi.mocked(bridge.setChatMuted).mockResolvedValue({ muted: ["peer-two"] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const line = row("Laptop");
    expect(line.element.tagName).toBe("DIV");
    expect(line.attributes("role")).toBeUndefined();
    expect(line.findAll("button").length).toBe(2);
    const mute = bell("Laptop");
    expect(open("Laptop").element.contains(mute.element)).toBe(false);
    // And it still does its own job without opening the conversation.
    await mute.trigger("click");
    await flushPromises();
    expect(bridge.setChatMuted).toHaveBeenCalledWith("peer-two", true);
    expect(heading()).toContain("Pixel 8 Pro");
    expect(line.classes()).toContain("chat-session--muted");
  });

  it("opens its menu on Shift+F10", async () => {
    // 静音 and 关闭会话 live on that menu, and a mouse was the only way to it.
    wrapper = mount(ChatView);
    await flushPromises();
    await row("Laptop").trigger("keydown", { key: "F10", shiftKey: true });
    const menu = contextMenuState();
    expect(menu.open).toBe(true);
    expect(menu.items.map(item => item.id)).toContain("mute");
    expect(menu.items.map(item => item.id)).toContain("close");
  });

  it("offers the same menu from the bell, which is part of the row it sits on", async () => {
    // Right-clicking the bell already opened the row's menu; the menu key did
    // nothing there, because the row used to own the bell and swallowed its
    // keys.  The two spellings of "the menu for this" should agree.
    wrapper = mount(ChatView);
    await flushPromises();
    await bell("Laptop").trigger("keydown", { key: "F10", shiftKey: true });
    expect(contextMenuState().open).toBe(true);
  });

  it("leaves the keys pressed on the row's own bell to the bell", async () => {
    // Enter on the bell used to open the conversation as well, because the
    // row's handler heard the key on its way past.
    wrapper = mount(ChatView);
    await flushPromises();
    await bell("Laptop").trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(heading()).toContain("Pixel 8 Pro");
  });
});

describe("the transcript's landmarks", () => {
  const live = { ...session, status: "active", online: true };
  /** A timestamp `days` days back, at 09:00 local — the hour does not matter,
   *  only which calendar day it lands on. */
  function at(days: number, index: number) {
    return Math.floor(new Date(2026, 9, 20 - days, 9, index).getTime() / 1000);
  }
  function text(entry: string, ts: number, outgoing = false) {
    return { entry_id: entry, kind: "text", outgoing, ts, text: entry, status: "sent" };
  }

  it("marks the break between two days, once, and nothing inside one", async () => {
    // U143: a transcript that crossed midnight read as one long afternoon,
    // because the rows carry a clock and nothing else.
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [
      text("a", at(0, 1)), text("b", at(0, 2)), text("c", at(1, 1)), text("d", at(1, 2)),
      text("e", at(3, 1)),
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const days = wrapper.findAll(".chat-day");
    expect(days.length).toBe(3);
    expect(new Set(days.map((day) => day.text())).size).toBe(3);
    // One divider for the run under it, not one per message.
    const first = days[0].element.nextElementSibling as HTMLElement;
    expect(first.dataset.entryId).toBe("a");
  });

  it("draws the line where this reader left off, from the count before it was cleared", async () => {
    // U143's other half.  The sidecar answers a count, not a marker, so the
    // place is arithmetic — and it has to be taken before `markRead` zeroes
    // the count, or the line lands above everything in the transcript.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...live, unread: 2 }],
    } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [
      text("a", at(0, 1)), text("b", at(0, 2), true), text("c", at(0, 3)), text("d", at(0, 4)),
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    // Two unread incoming, so the run starts at the third of the three: the
    // outgoing one in the middle is not something anybody sent to this reader.
    const line = wrapper.get(".chat-unread-line").element;
    expect((line.nextElementSibling as HTMLElement).dataset.entryId).toBe("c");
    expect(bridge.markChatRead).toHaveBeenCalledWith("one");
  });

  it("offers the way up to that line, and stops offering it once taken", async () => {
    // U150: sixty unread messages opened at the newest one and the reader had
    // to scroll up to find where they had got to.
    vi.mocked(bridge.chatSessions).mockResolvedValue({
      sessions: [{ ...live, unread: 3 }],
    } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [
      text("a", at(0, 1)), text("b", at(0, 2)), text("c", at(0, 3)), text("d", at(0, 4)),
    ] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const jump = wrapper.get(".chat-unread-jump");
    await jump.trigger("click");
    expect(wrapper.find(".chat-unread-jump").exists()).toBe(false);
    // The line it led to is still there: the mark is a fact about the
    // conversation, not a hint that goes away once used.
    expect(wrapper.find(".chat-unread-line").exists()).toBe(true);
  });

  it("offers nothing when there was nothing waiting", async () => {
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [{ ...live, unread: 0 }] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [text("a", at(0, 1))] } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    expect(wrapper.find(".chat-unread-line").exists()).toBe(false);
    expect(wrapper.find(".chat-unread-jump").exists()).toBe(false);
  });

  it("deletes one message from this device, saying who keeps it", async () => {
    // U144: the row could only be copied.  Deleting is the local half of a
    // recall — the peer keeps its copy — and the dialog says so before it
    // acts, because a reader who expects a recall learns the difference from
    // the other screen otherwise.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [live] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [text("a", at(0, 1))] } as any);
    vi.mocked(bridge.deleteChatMessage).mockResolvedValue({ ok: true } as any);
    wrapper = mount(ChatView);
    await flushPromises();
    const reads = vi.mocked(bridge.chatMessages).mock.calls.length;

    await wrapper.get(".chat-message").trigger("contextmenu");
    expect(contextMenuState().items.map((item) => item.id)).toEqual(["copy", "delete"]);
    expect(contextMenuState().items[1].danger).toBe(true);
    await runContextMenuItem(contextMenuState().items[1]);
    await flushPromises();
    expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("对方仍然能看到它");

    await wrapper.get('[aria-labelledby="chat-delete-title"] .danger').trigger("click");
    await flushPromises();
    expect(bridge.deleteChatMessage).toHaveBeenCalledExactlyOnceWith("one", "a");
    // The transcript is re-read rather than having the row spliced out here:
    // the page is a page, and what moves up into its place may be a row it
    // does not hold.
    expect(vi.mocked(bridge.chatMessages).mock.calls.length).toBe(reads + 1);
  });
});
