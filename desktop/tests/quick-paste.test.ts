import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import QuickPaste from "../src/quick-paste/QuickPaste.vue";
import { bridge } from "../src/api/bridge";
import type { HistoryItem, HistoryPage, SidecarEvent, TransfersPage } from "../src/api/types";
import { setLocale, t } from "../src/i18n";

// The overlay's own commands, the two on-demand paste verbs, the transfer list
// its progress panel reads, and the forwarded sidecar events.
vi.mock("../src/api/bridge", () => ({
  bridge: {
    quickPasteSettings: vi.fn(),
    quickPasteList: vi.fn(),
    quickPasteChoose: vi.fn(),
    quickPasteClose: vi.fn(),
    quickPasteChooseFavorite: vi.fn(),
    quickPasteFavorites: vi.fn(),
    preparePasteEntry: vi.fn(),
    cancelPasteEntry: vi.fn(),
    transfers: vi.fn(),
    onSidecarEvent: vi.fn(),
    previewHistoryEntry: vi.fn(),
  },
}));

/** A clipboard row, with whatever a case asserts on. */
function hit(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    id: "h1", timestamp: 1, content_type: "TEXT", pinned: false,
    preview: "会议纪要：周三下午三点", source_name: "Notes", source_app: "",
    source_title: "", paste_count: 0,
    ...overrides,
  };
}

function listPage(items: HistoryItem[], total = items.length): HistoryPage {
  return {
    session_id: "s", seq: 0, offset: 0, total, items,
    has_history: items.length > 0,
  } as HistoryPage;
}

/** A remote-file row: its bytes are on another machine until a paste fetches
 *  them. */
function remoteFile(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return hit({
    id: "file-1", content_type: "FILE_REMOTE", preview: "report.pdf",
    source_name: "Laptop", offer_entry: "peer-7", ...overrides,
  });
}

function transfersPage(rows: Array<Record<string, unknown>> = []): TransfersPage {
  return { active: rows as never[], history: [], speed_test: {} };
}

/** The handler the overlay registered for `sidecar:event`, so a case can deliver
 *  an event the host would forward. */
let sidecarHandler: ((event: SidecarEvent) => void) | undefined;

/** Deliver one forwarded sidecar event, the way the host does. */
async function emitEvent(name: string, data: Record<string, unknown>) {
  sidecarHandler?.({ type: "event", name, session_id: "s", data });
  await flushPromises();
  await flushPromises();
}

/** Mount the overlay and let its two startup reads land: settings, then rows. */
async function mountOverlay(
  items: HistoryItem[] = [hit()],
  settings: { language: string; auto_paste_enabled: boolean; appearance_mode?: string } = {
    language: "zh-CN", auto_paste_enabled: true,
  },
) {
  vi.mocked(bridge.quickPasteSettings).mockResolvedValue(settings);
  vi.mocked(bridge.quickPasteList).mockResolvedValue(listPage(items));
  const wrapper = mount(QuickPaste);
  // Two awaits, one per read: `initialise` waits for the settings before it
  // asks for the rows, so a single flush would catch the list still empty.
  await flushPromises();
  await flushPromises();
  return wrapper;
}

/** The current keyboard cursor, as the row it is drawn on. */
function selectedIndex(wrapper: VueWrapper): number {
  return wrapper.findAll(".quick-paste-row")
    .findIndex(row => row.classes("quick-paste-row--selected"));
}

/** A keypress the way the window receives one — the overlay listens on
 *  `window`, not on the search box, so the pointer can have moved the focus. */
async function press(key: string, init: KeyboardEventInit = {}) {
  window.dispatchEvent(new KeyboardEvent("keydown", { key, ...init }));
  await flushPromises();
}

describe("quick paste overlay", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setLocale("zh-CN");
    sidecarHandler = undefined;
    vi.mocked(bridge.quickPasteChoose).mockResolvedValue({ ok: true });
    vi.mocked(bridge.quickPasteClose).mockResolvedValue({ ok: true });
    vi.mocked(bridge.preparePasteEntry).mockResolvedValue({ requested: true });
    vi.mocked(bridge.cancelPasteEntry).mockResolvedValue({ cancelled: 1 });
    vi.mocked(bridge.transfers).mockResolvedValue(transfersPage());
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "files", image: "", width: 0, height: 0, files: [], total: 0,
    });
    vi.mocked(bridge.onSidecarEvent).mockImplementation(async (handler) => {
      sidecarHandler = handler;
      return () => { sidecarHandler = undefined; };
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(bridge.quickPasteSettings).mockReset();
    vi.mocked(bridge.quickPasteList).mockReset();
    vi.mocked(bridge.quickPasteChoose).mockReset();
    vi.mocked(bridge.quickPasteClose).mockReset();
    vi.mocked(bridge.preparePasteEntry).mockReset();
    vi.mocked(bridge.cancelPasteEntry).mockReset();
    vi.mocked(bridge.transfers).mockReset();
    vi.mocked(bridge.onSidecarEvent).mockReset();
    vi.mocked(bridge.previewHistoryEntry).mockReset();
    setLocale("zh-CN");
    // The overlay writes the theme on the shared jsdom document, like the main
    // window does: a case that picked one must not paint the next case.
    delete document.documentElement.dataset.theme;
  });

  it("lists the newest clips on open and puts the cursor on the first one", async () => {
    // Opening the window is the query: the rows a reader wants are the ones at
    // the top of the history, and an empty search box asking for nothing would
    // open on an empty list.
    const wrapper = await mountOverlay([
      hit({ id: "a", preview: "第一条" }),
      hit({ id: "b", preview: "第二条" }),
    ]);
    try {
      expect(bridge.quickPasteList).toHaveBeenCalledWith("", "all", 20, "newest");
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(2);
      // The cursor starts on the first row and the list says so: Enter with
      // nothing pressed else is "take the newest clip".
      expect(selectedIndex(wrapper)).toBe(0);
      expect(wrapper.findAll(".quick-paste-row")[0].attributes("aria-selected")).toBe("true");
      expect(wrapper.get(".quick-paste-text").text()).toBe("第一条");
      // The second line names the kind, the source and the time, which is what
      // tells two clips with the same words apart.
      expect(wrapper.get(".quick-paste-meta").text()).toContain("文本");
      expect(wrapper.get(".quick-paste-meta").text()).toContain("Notes");
    } finally {
      wrapper.unmount();
    }
  });

  it("shows a picture on an image row, and asks only for image rows", async () => {
    // The sidecar downscales the picture for the history card already; the
    // palette asks for the same card, so a glance at the list is enough to
    // recognise a screenshot without pasting it first.
    const image = hit({ id: "img-1", content_type: "IMAGE_PNG", preview: "[Image]" });
    const text = hit({ id: "text-1", content_type: "TEXT", preview: "会议纪要" });
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "image", image: "data:image/png;base64,AAAA", width: 800, height: 600,
      files: [], total: 0,
    });
    const wrapper = await mountOverlay([image, text]);
    try {
      await flushPromises();
      expect(bridge.previewHistoryEntry).toHaveBeenCalledWith("img-1");
      expect(bridge.previewHistoryEntry).not.toHaveBeenCalledWith("text-1");
      const thumb = wrapper.get(".quick-paste-glyph--image img");
      expect(thumb.attributes("src")).toBe("data:image/png;base64,AAAA");
      // The text row keeps the kind glyph: one preview read is one image, not
      // one per row.
      expect(wrapper.findAll(".quick-paste-glyph--image")).toHaveLength(1);
    } finally {
      wrapper.unmount();
    }
  });

  it("opens the enlarged picture when the pointer rests on an image row", async () => {
    // The history list has done this since the preview card existed; the
    // palette now says the same thing without giving up a list column.
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "image", image: "data:image/png;base64,AAAA", width: 800, height: 600,
      files: [], total: 0,
    });
    const wrapper = await mountOverlay([
      hit({ id: "img-3", content_type: "IMAGE_PNG", preview: "[Image]" }),
      hit({ id: "text-3", content_type: "TEXT", preview: "会议纪要" }),
    ]);
    try {
      await flushPromises();
      const rows = wrapper.findAll(".quick-paste-row");
      await rows[0].trigger("mouseenter", { clientX: 120, clientY: 160 });
      vi.advanceTimersByTime(200);
      await flushPromises();
      const card = wrapper.get(".quick-paste-preview");
      expect(card.get("img").attributes("src")).toBe("data:image/png;base64,AAAA");
      expect(card.text()).toContain("800 × 600");

      await rows[0].trigger("mouseleave");
      expect(wrapper.find(".quick-paste-preview").exists()).toBe(false);

      // A text row is not a picture; resting on it must not open the card.
      await rows[1].trigger("mouseenter", { clientX: 120, clientY: 200 });
      vi.advanceTimersByTime(200);
      await flushPromises();
      expect(wrapper.find(".quick-paste-preview").exists()).toBe(false);
    } finally {
      wrapper.unmount();
    }
  });

  it("keeps the kind glyph when an image preview cannot be read", async () => {    vi.mocked(bridge.previewHistoryEntry).mockRejectedValue(new Error("no preview"));
    const wrapper = await mountOverlay([hit({ id: "img-2", content_type: "IMAGE_PNG", preview: "[Image]" })]);
    try {
      await flushPromises();
      expect(wrapper.find(".quick-paste-glyph--image").exists()).toBe(false);
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
      expect(bridge.previewHistoryEntry).toHaveBeenCalledTimes(1);
    } finally {
      wrapper.unmount();
    }
  });

  it("switches to the 收藏 library and pastes a favourite from it", async () => {
    vi.mocked(bridge.quickPasteFavorites).mockResolvedValue({
      items: [{
        id: "f1", title: "常用地址", preview: "北京市朝阳区…", group: "工作",
        position: 0, created: 1, updated: 2,
      }],
      total: 1, offset: 0, groups: ["工作"], session_id: "s", seq: 0,
    } as any);
    const wrapper = await mountOverlay([hit()]);
    try {
      const favoriteChip = wrapper.findAll("button")
        .find(node => (node.text() || "").includes("收藏"))!;
      await favoriteChip.trigger("click");
      await flushPromises();
      expect(bridge.quickPasteFavorites).toHaveBeenCalledWith("", 20);
      expect(wrapper.get(".quick-paste-text").text()).toBe("常用地址");

      await press("Enter");
      expect(bridge.quickPasteChooseFavorite).toHaveBeenCalledWith("f1");
      expect(bridge.quickPasteChoose).not.toHaveBeenCalled();
    } finally {
      wrapper.unmount();
    }
  });

  it("waits 120 ms after a keystroke before it searches", async () => {
    const wrapper = await mountOverlay();
    try {
      await wrapper.get('[aria-label="搜索剪贴板历史"]').setValue("会议");
      // The word is not one query per letter: the box waits for the typing to
      // stop, and the overlay waits less than the shell's pages because this
      // window is opened mid-gesture and closed by the same Return.
      expect(bridge.quickPasteList).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(100);
      expect(bridge.quickPasteList).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(30);
      await flushPromises();
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("会议", "all", 20, "newest");
    } finally {
      wrapper.unmount();
    }
  });

  it("searches at once when a kind chip is picked, with the chip's own id", async () => {
    const wrapper = await mountOverlay();
    try {
      await wrapper.findAll(".quick-paste-chip")[2].trigger("click");
      await flushPromises();
      // A chip is a decision rather than typing, so it has no pause to wait
      // out — and the id is the history page's own vocabulary.
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("", "image", 20, "newest");
      expect(wrapper.findAll(".quick-paste-chip")[2].attributes("aria-pressed")).toBe("true");
    } finally {
      wrapper.unmount();
    }
  });

  it("moves the cursor with the arrow keys, wrapping at both ends, and scrolls it into view", async () => {
    // jsdom has no layout, so `scrollIntoView` does not exist on its elements:
    // the stub is what a case can observe, and the browser the window really
    // runs in is what the call is for.
    const scrollIntoView = vi.fn();
    (HTMLElement.prototype as any).scrollIntoView = scrollIntoView;
    const wrapper = await mountOverlay([
      hit({ id: "a" }), hit({ id: "b" }), hit({ id: "c" }),
    ]);
    try {
      await press("ArrowDown");
      expect(selectedIndex(wrapper)).toBe(1);
      // The row the cursor moved to is brought into the visible part of the
      // list — a keyboard that moves a selection nobody can see is not a key
      // that works.
      expect(scrollIntoView).toHaveBeenCalled();
      await press("ArrowDown");
      await press("ArrowDown");
      // Past the last row is the first one, like every other list in the window.
      expect(selectedIndex(wrapper)).toBe(0);
      await press("ArrowUp");
      expect(selectedIndex(wrapper)).toBe(2);
    } finally {
      wrapper.unmount();
      delete (HTMLElement.prototype as any).scrollIntoView;
    }
  });

  it("pastes the selected row on Enter, and the words only on Shift+Enter", async () => {
    const wrapper = await mountOverlay([hit({ id: "a" }), hit({ id: "b" })]);
    try {
      await press("ArrowDown");
      await press("Enter");
      expect(bridge.quickPasteChoose).toHaveBeenLastCalledWith("b", false);
      // Shift+Enter is the same row's text form: a clip that also carries
      // formatting arrives as markup in a terminal, and this is the flag the
      // history page's own plain copy sends.
      await press("Enter", { shiftKey: true });
      expect(bridge.quickPasteChoose).toHaveBeenLastCalledWith("b", true);
      // The host hides the window on success; the overlay asks it for nothing.
      expect(bridge.quickPasteClose).not.toHaveBeenCalled();
    } finally {
      wrapper.unmount();
    }
  });

  it("pastes the row a click lands on, after one double-click interval", async () => {
    const wrapper = await mountOverlay([hit({ id: "a" }), hit({ id: "b" })]);
    try {
      // A click is a choice; the short delay is what keeps a reader's habitual
      // double click from pasting twice (or clicking the app behind once the
      // overlay closes).  Both gestures end as one paste.
      await wrapper.findAll(".quick-paste-row")[1].trigger("click");
      await wrapper.findAll(".quick-paste-row")[1].trigger("click");
      vi.advanceTimersByTime(200);
      await flushPromises();
      expect(bridge.quickPasteChoose).toHaveBeenCalledTimes(1);
      expect(bridge.quickPasteChoose).toHaveBeenLastCalledWith("b", false);
    } finally {
      wrapper.unmount();
    }
  });

  it("ignores Enter and the arrows an IME is still using", async () => {
    const wrapper = await mountOverlay();
    try {
      // Confirming a candidate with Enter is not a request to paste the row
      // under the cursor mid-composition.
      await press("Enter", { isComposing: true });
      await press("ArrowDown", { isComposing: true });
      expect(bridge.quickPasteChoose).not.toHaveBeenCalled();
      expect(selectedIndex(wrapper)).toBe(0);
    } finally {
      wrapper.unmount();
    }
  });

  it("takes one pick at a time, and hides when the focus leaves even mid-pick", async () => {
    const wrapper = await mountOverlay();
    try {
      let settle: (result: { ok: boolean }) => void = () => {};
      vi.mocked(bridge.quickPasteChoose).mockReturnValue(
        new Promise<{ ok: boolean }>(resolve => { settle = resolve; }));
      await press("Enter");
      await press("Enter");
      await press("Enter", { shiftKey: true });
      expect(bridge.quickPasteChoose).toHaveBeenCalledTimes(1);
      // The overlay hides on every blur now, the pick in flight included: the
      // reader looking away is the reader dismissing it, and a window that
      // "关不掉" while it waits for the host was the reported bug.
      window.dispatchEvent(new Event("blur"));
      await flushPromises();
      expect(bridge.quickPasteClose).toHaveBeenCalledOnce();
      settle({ ok: true });
      await flushPromises();
    } finally {
      wrapper.unmount();
    }
  });

  it("closes on Escape", async () => {
    const wrapper = await mountOverlay();
    try {
      await press("Escape");
      expect(bridge.quickPasteClose).toHaveBeenCalledOnce();
    } finally {
      wrapper.unmount();
    }
  });

  it("closes when the focus leaves the overlay", async () => {
    const wrapper = await mountOverlay();
    try {
      window.dispatchEvent(new Event("blur"));
      await flushPromises();
      expect(bridge.quickPasteClose).toHaveBeenCalledOnce();
    } finally {
      wrapper.unmount();
    }
  });

  it("starts a fresh search when the host shows the window again", async () => {
    // The host creates the window once and hides/shows it for every press, so
    // the page outlives each opening: without a reset the second press would
    // open on the last query and the last list, and the clip just copied would
    // not be on screen.
    const wrapper = await mountOverlay([hit({ id: "a" }), hit({ id: "b" })]);
    try {
      await wrapper.get('[aria-label="搜索剪贴板历史"]').setValue("会议");
      await vi.advanceTimersByTimeAsync(130);
      await wrapper.findAll(".quick-paste-chip")[2].trigger("click");
      await flushPromises();
      expect((wrapper.get('[aria-label="搜索剪贴板历史"]').element as HTMLInputElement).value)
        .toBe("会议");
      window.dispatchEvent(new Event("focus"));
      await flushPromises();
      await flushPromises();
      expect((wrapper.get('[aria-label="搜索剪贴板历史"]').element as HTMLInputElement).value).toBe("");
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("", "all", 20, "newest");
      expect(wrapper.findAll(".quick-paste-chip")[0].attributes("aria-pressed")).toBe("true");
    } finally {
      wrapper.unmount();
    }
  });

  it("does not reset out from under the failure the host re-showed the window for", async () => {
    vi.mocked(bridge.quickPasteChoose).mockResolvedValue({
      ok: false, copied: true, code: "PASTE_FAILED", message: "系统拒绝了粘贴",
    });
    const wrapper = await mountOverlay();
    try {
      await press("Enter");
      expect(wrapper.get('[role="alert"]').text()).toBe("系统拒绝了粘贴");
      // The focus the host gives back to show that sentence is not a new press;
      // a reset here would take away the only thing the window was re-shown for.
      const reads = vi.mocked(bridge.quickPasteList).mock.calls.length;
      window.dispatchEvent(new Event("focus"));
      await flushPromises();
      expect(wrapper.get('[role="alert"]').text()).toBe("系统拒绝了粘贴");
      expect(vi.mocked(bridge.quickPasteList).mock.calls.length).toBe(reads);
    } finally {
      wrapper.unmount();
    }
  });

  it("keeps the rows and says why when the paste half is refused", async () => {
    // `{ok: false, copied: true}` is not "nothing happened": the clipboard holds
    // the clip, and the host re-shows the window so the sentence can be read
    // and the row tried again.
    vi.mocked(bridge.quickPasteChoose).mockResolvedValue({
      ok: false, copied: true, code: "PASTE_FAILED", message: "系统拒绝了粘贴",
    });
    const wrapper = await mountOverlay();
    try {
      await press("Enter");
      expect(wrapper.get('[role="alert"]').text()).toBe("系统拒绝了粘贴");
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
    } finally {
      wrapper.unmount();
    }
  });

  it("says a read failed instead of showing rows a word no longer matches", async () => {
    vi.mocked(bridge.quickPasteSettings).mockResolvedValue({
      language: "zh-CN", auto_paste_enabled: true,
    });
    vi.mocked(bridge.quickPasteList).mockRejectedValue({
      code: "RPC_FAILED", message: "连接已断开",
    });
    const wrapper = mount(QuickPaste);
    try {
      await flushPromises();
      await flushPromises();
      expect(wrapper.get('[role="alert"]').text()).toBe("连接已断开");
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(0);
      // The failure keeps a retry: the list is the whole window, and the only
      // thing that can fix a failed read is another read.
      vi.mocked(bridge.quickPasteList).mockResolvedValue(listPage([hit()]));
      await wrapper.findAll("button").find(node => node.text() === t("重试"))!.trigger("click");
      await flushPromises();
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
    } finally {
      wrapper.unmount();
    }
  });

  it("names an empty history, an empty filter and a missing word differently", async () => {
    const wrapper = await mountOverlay([]);
    try {
      expect(wrapper.get(".quick-paste-empty-title").text()).toBe(t("还没有可粘贴的记录"));
      // A word that found nothing is not an empty history: the hint says which
      // one it is, because the fix is different.
      await wrapper.get('[aria-label="搜索剪贴板历史"]').setValue("会议");
      await vi.advanceTimersByTimeAsync(130);
      await flushPromises();
      expect(wrapper.get(".quick-paste-empty-title").text()).toBe("没有找到“会议”");
      await wrapper.get('[aria-label="搜索剪贴板历史"]').setValue("");
      await vi.advanceTimersByTimeAsync(130);
      await wrapper.findAll(".quick-paste-chip")[2].trigger("click");
      await flushPromises();
      expect(wrapper.get(".quick-paste-empty-title").text()).toBe(t("还没有{type}", { type: t("图片") }));
    } finally {
      wrapper.unmount();
    }
  });

  /* ── Order and the two client-side filters (1.2.4) ───────────────────── */

  it("asks the host for the order it shows, instead of reversing the page", async () => {
    const wrapper = await mountOverlay();
    try {
      const sortChip = wrapper.get(".quick-paste-sort");
      expect(sortChip.text()).toContain(t("最新优先"));
      await sortChip.trigger("click");
      await flushPromises();
      // "oldest" is a different read: the oldest rows of the history are what
      // the choice is for, and they are not the tail of a 20-row page.
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("", "all", 20, "oldest");
      expect(wrapper.get(".quick-paste-sort").text()).toContain(t("最旧优先"));
      await wrapper.get(".quick-paste-sort").trigger("click");
      await flushPromises();
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("", "all", 20, "newest");
    } finally {
      wrapper.unmount();
    }
  });

  it("narrows the panel by time on this side, asking for a wider page first", async () => {
    const now = Date.now();
    const wrapper = await mountOverlay([
      hit({ id: "today", preview: "今天复制的", timestamp: now / 1000 - 60 }),
      hit({ id: "old", preview: "很久以前", timestamp: now / 1000 - 3 * 24 * 3600 }),
    ]);
    try {
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(2);
      // 今天 is a calendar question, and the host's page is twenty rows: the
      // filter is client-side, so the page it filters has to be wide enough to
      // contain the answer.  100 is the host's own cap.
      await wrapper.findAll(".quick-paste-time").find(node => node.text() === t("今天"))!.trigger("click");
      await flushPromises();
      await flushPromises();
      expect(bridge.quickPasteList).toHaveBeenLastCalledWith("", "all", 100, "newest");
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
      expect(wrapper.get(".quick-paste-row").text()).toContain("今天复制的");
    } finally {
      wrapper.unmount();
    }
  });

  it("narrows by source device, offering the devices the page contains", async () => {
    const wrapper = await mountOverlay([
      hit({ id: "local", preview: "本机的", source_device: "", source_name: "" }),
      hit({ id: "laptop", preview: "笔记本的", source_device: "peer-1", source_name: "Laptop" }),
      hit({ id: "studio", preview: "工作站的", source_device: "peer-2", source_name: "Studio" }),
    ]);
    try {
      const options = wrapper.findAll(".quick-paste-device option").map(node => node.text());
      // The local option exists because a local row does; each device is named
      // by its source_name rather than by its id.
      expect(options).toEqual([t("全部设备"), t("本机"), "Laptop", "Studio"]);
      await wrapper.get(".quick-paste-device").setValue("peer-1");
      await flushPromises();
      await flushPromises();
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
      expect(wrapper.get(".quick-paste-row").text()).toContain("笔记本的");
    } finally {
      wrapper.unmount();
    }
  });

  it("says the filters are what emptied the list", async () => {
    const wrapper = await mountOverlay([
      hit({ id: "old", preview: "上个月的", timestamp: Date.now() / 1000 - 30 * 24 * 3600 }),
    ]);
    try {
      await wrapper.findAll(".quick-paste-time").find(node => node.text() === t("今天"))!.trigger("click");
      await flushPromises();
      await flushPromises();
      // "还没有可粘贴的记录" would be false — there is one, it is just older —
      // and a hint that only named the keyword would leave the time chip unseen.
      expect(wrapper.get(".quick-paste-empty-title").text()).toBe(t("没有符合当前筛选条件的记录"));
      expect(wrapper.get(".quick-paste-empty").text()).toContain(t("换个关键词或筛选条件试试。"));
    } finally {
      wrapper.unmount();
    }
  });

  it("takes its language from the host before it paints", async () => {
    const wrapper = await mountOverlay([hit()], { language: "en", auto_paste_enabled: true });
    try {
      // The overlay's document has no `App.vue` to have set a locale, so the
      // language arrives from `quick_paste_settings` — and it arrives before
      // the first search, so nothing flashes in the wrong language.
      expect(bridge.quickPasteSettings).toHaveBeenCalledOnce();
      expect(wrapper.get('input[type="search"]').attributes("placeholder"))
        .toBe("Search clipboard history…");
      expect(wrapper.get(".quick-paste-footer").text()).toContain("Enter: paste");
    } finally {
      wrapper.unmount();
      setLocale("zh-CN");
    }
  });

  it("paints in the theme the host stored, and falls back to system for one it does not know", async () => {
    // The overlay is a second document: it does not inherit the main window's
    // `data-theme`, so a reader on 午夜 would have got a light overlay until the
    // host started sending the choice through `quick_paste_settings`.
    const themed = await mountOverlay([hit()], {
      language: "zh-CN", auto_paste_enabled: true, appearance_mode: "midnight",
    });
    try {
      expect(document.documentElement.dataset.theme).toBe("midnight");
    } finally {
      themed.unmount();
    }
    // A value from a newer build is not a theme this stylesheet has: it reads
    // as `system` rather than leaving the document with no tokens at all.
    const unknown = await mountOverlay([hit()], {
      language: "zh-CN", auto_paste_enabled: true, appearance_mode: "neon",
    });
    try {
      expect(document.documentElement.dataset.theme).toBe("system");
    } finally {
      unknown.unmount();
    }
    // A host older than the field sends no theme; the OS preference is what
    // such a build could have shown anyway.
    const legacy = await mountOverlay([hit()], { language: "zh-CN", auto_paste_enabled: true });
    try {
      expect(document.documentElement.dataset.theme).toBe("system");
    } finally {
      legacy.unmount();
    }
  });
});

/** The remote-file paste: choose the row, fetch it, watch it, paste it.
 *
 * Its state machine is the point of the 1.3.1 overlay work, so these cases are
 * about the panel rather than the list: which call starts a fetch, what the
 * confirmation asks, what the progress panel reads, and which events end it. */
describe("on-demand paste of a remote file", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setLocale("zh-CN");
    sidecarHandler = undefined;
    vi.mocked(bridge.quickPasteChoose).mockResolvedValue({ ok: true });
    vi.mocked(bridge.quickPasteClose).mockResolvedValue({ ok: true });
    vi.mocked(bridge.preparePasteEntry).mockResolvedValue({ requested: true });
    vi.mocked(bridge.cancelPasteEntry).mockResolvedValue({ cancelled: 1 });
    vi.mocked(bridge.transfers).mockResolvedValue(transfersPage());
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "files", image: "", width: 0, height: 0, files: [], total: 0,
    });
    vi.mocked(bridge.onSidecarEvent).mockImplementation(async (handler) => {
      sidecarHandler = handler;
      return () => { sidecarHandler = undefined; };
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(bridge.quickPasteSettings).mockReset();
    vi.mocked(bridge.quickPasteList).mockReset();
    vi.mocked(bridge.quickPasteChoose).mockReset();
    vi.mocked(bridge.quickPasteClose).mockReset();
    vi.mocked(bridge.preparePasteEntry).mockReset();
    vi.mocked(bridge.cancelPasteEntry).mockReset();
    vi.mocked(bridge.transfers).mockReset();
    vi.mocked(bridge.onSidecarEvent).mockReset();
    vi.mocked(bridge.previewHistoryEntry).mockReset();
    setLocale("zh-CN");
  });

  it("marks a row whose bytes are still on the other machine", async () => {
    const wrapper = await mountOverlay([
      remoteFile(),
      hit({ id: "plain", preview: "just words" }),
    ]);
    try {
      // The row says where it came from and what pressing Enter will do, before
      // the key is pressed: one of these rows is not a copy.
      expect(wrapper.get(".quick-paste-remote").text()).toBe("来自 Laptop · 粘贴时取回");
      expect(wrapper.findAll(".quick-paste-remote")).toHaveLength(1);
    } finally { wrapper.unmount(); }
  });

  it("starts the fetch on Enter and draws its progress", async () => {
    vi.mocked(bridge.transfers).mockResolvedValue(transfersPage([{
      id: "t1", filename: "report.pdf", size: 2048, direction: "down",
      status: "running", progress: 42, speed: 1024, eta: "1 分钟",
      paste_entry: "file-1",
    }]));
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      expect(bridge.preparePasteEntry).toHaveBeenCalledWith("file-1", false);
      // The list becomes the panel: a file must move before anything is pasted.
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(true);
      expect(wrapper.get(".quick-paste-transfer-name").text()).toBe("report.pdf");
      expect(wrapper.get(".quick-paste-transfer-percent").text()).toBe("42%");
      expect(wrapper.text()).toContain("1 分钟");
      // It is not pasted yet: the files are still arriving.
      expect(bridge.quickPasteChoose).not.toHaveBeenCalled();
    } finally { wrapper.unmount(); }
  });

  it("asks before starting a paste the sidecar wants confirmed", async () => {
    vi.mocked(bridge.preparePasteEntry)
      .mockRejectedValueOnce({ code: "PASTE_CONFIRM_REQUIRED", message: "large", retryable: false })
      .mockResolvedValueOnce({ requested: true });
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      expect(wrapper.get(".quick-paste-confirm").text()).toContain("这个文件较大");
      // The answer is the same call with the confirmation attached.
      await wrapper.get(".quick-paste-confirm").findAll("button")
        .find(node => node.text() === "继续取回")!.trigger("click");
      await flushPromises();
      expect(bridge.preparePasteEntry).toHaveBeenLastCalledWith("file-1", true);
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(true);
    } finally { wrapper.unmount(); }
  });

  it("shows the paste's size when the offer can be read", async () => {
    // PASTE_CONFIRM_REQUIRED carries no byte count through the host, so the
    // panel reads the row's offer — the same preview the hover card uses — and
    // sums the files.  A folder offer would answer 0 and keep the general
    // wording; a plain file offer can say how much is about to move.
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "files", image: "", width: 0, height: 0,
      files: [{ name: "video.mov", size: 2 * 1024 * 1024, kind: "file", exists: false }],
      total: 1,
    } as any);
    vi.mocked(bridge.preparePasteEntry).mockRejectedValueOnce({
      code: "PASTE_CONFIRM_REQUIRED", message: "large", retryable: false,
    });
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      expect(wrapper.get(".quick-paste-confirm").text())
        .toContain(t("文件约 {size}，粘贴前需先从另一台设备传输。继续？", { size: "2.0 MB" }));
    } finally { wrapper.unmount(); }
  });

  it("continues the large-file question with Enter", async () => {
    // The confirmation panel advertises Enter 继续 in its hint, so Enter has to
    // do it: the panel is a question, and the window's one key answers it.
    vi.mocked(bridge.preparePasteEntry)
      .mockRejectedValueOnce({ code: "PASTE_CONFIRM_REQUIRED", message: "large", retryable: false })
      .mockResolvedValueOnce({ requested: true });
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      expect(wrapper.find(".quick-paste-confirm").exists()).toBe(true);
      await press("Enter");
      expect(bridge.preparePasteEntry).toHaveBeenLastCalledWith("file-1", true);
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(true);
    } finally { wrapper.unmount(); }
  });

  it("keeps the caret in the search box while the filters are used", async () => {
    // The overlay's gesture is one word and Return: a chip that took the focus
    // ended the word at the first click, and the reader had to click back into
    // the box before typing the next one.
    vi.mocked(bridge.quickPasteSettings).mockResolvedValue({ language: "zh-CN", auto_paste_enabled: true });
    vi.mocked(bridge.quickPasteList).mockResolvedValue(listPage([hit()]));
    const wrapper = mount(QuickPaste, { attachTo: document.body });
    try {
      await flushPromises();
      await flushPromises();
      const search = wrapper.get('input[type="search"]').element as HTMLInputElement;
      search.focus();
      await wrapper.findAll(".quick-paste-time").find(node => node.text() === t("今天"))!.trigger("click");
      await flushPromises();
      expect(document.activeElement).toBe(search);
      await wrapper.get(".quick-paste-sort").trigger("click");
      await flushPromises();
      expect(document.activeElement).toBe(search);
      await wrapper.get(".quick-paste-device").setValue("all");
      await flushPromises();
      expect(document.activeElement).toBe(search);
    } finally {
      wrapper.unmount();
    }
  });

  it("pastes the row once the files are here", async () => {
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      await emitEvent("clip_files.paste_ready", { entry_id: "file-1", paths: ["C:/files/report.pdf"] });
      // By the time the event lands the row is a local file and the clipboard
      // holds it, so the overlay's ordinary choose is all that is left.
      expect(bridge.quickPasteChoose).toHaveBeenCalledWith("file-1", false);
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(false);
    } finally { wrapper.unmount(); }
  });

  it("keeps a failed fetch on screen with a reason and a way to retry", async () => {
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      await emitEvent("clip_files.paste_failed", { entry_id: "file-1", reason: "timeout" });
      expect(wrapper.get('[role="alert"]').text()).toBe("等待传输超时，请重试");
      expect(wrapper.findAll("button").map(node => node.text())).toEqual(expect.arrayContaining(["重试", "关闭"]));
      // Retrying asks the sidecar to fetch it again rather than replaying a
      // failure that is already over.
      await wrapper.findAll("button").find(node => node.text() === "重试")!.trigger("click");
      await flushPromises();
      expect(bridge.preparePasteEntry).toHaveBeenLastCalledWith("file-1", true);
    } finally { wrapper.unmount(); }
  });

  it("cancels the fetch from the panel, and from Esc", async () => {
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      await wrapper.findAll("button").find(node => node.text() === "取消取回")!.trigger("click");
      await flushPromises();
      expect(bridge.cancelPasteEntry).toHaveBeenCalledWith("file-1");
      // Back to the list, not closed: the reader may pick another row.
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(false);
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);

      await press("Enter");
      await press("Escape");
      expect(bridge.cancelPasteEntry).toHaveBeenCalledTimes(2);
      expect(bridge.quickPasteClose).toHaveBeenCalledOnce();
    } finally { wrapper.unmount(); }
  });

  it("takes one fetch at a time, and cancels it when the window is dismissed", async () => {
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      await press("Enter");
      await press("Enter", { shiftKey: true });
      expect(bridge.preparePasteEntry).toHaveBeenCalledTimes(1);
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(true);
      // Blur now hides from every state, progress panel included (1.2.4): the
      // reader turning away from a fetch is the reader cancelling it, and the
      // cancel goes to the runtime before the window goes away.
      window.dispatchEvent(new Event("blur"));
      await flushPromises();
      expect(bridge.cancelPasteEntry).toHaveBeenCalledWith("file-1");
      expect(bridge.quickPasteClose).toHaveBeenCalledOnce();
    } finally { wrapper.unmount(); }
  });

  it("shows a refused start where the list is, and stays listable", async () => {
    vi.mocked(bridge.preparePasteEntry).mockRejectedValue({
      code: "PASTE_NOT_AVAILABLE", message: "gone", retryable: false,
    });
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      expect(wrapper.get('[role="alert"]').text()).toBe("这个条目已无法从另一台设备取回");
      // Nothing started, so the list is still what the reader has.
      expect(wrapper.find(".quick-paste-transfer").exists()).toBe(false);
      expect(wrapper.findAll(".quick-paste-row")).toHaveLength(1);
    } finally { wrapper.unmount(); }
  });

  it("reports a final paste failure on the panel instead of closing", async () => {
    vi.mocked(bridge.quickPasteChoose).mockResolvedValueOnce({
      ok: false, copied: true, code: "PASTE_FAILED", message: "系统拒绝了粘贴",
    });
    const wrapper = await mountOverlay([remoteFile()]);
    try {
      await press("Enter");
      await emitEvent("clip_files.paste_ready", { entry_id: "file-1", paths: ["C:/files/report.pdf"] });
      expect(wrapper.get('[role="alert"]').text()).toBe("系统拒绝了粘贴");
      // The bytes are here; retrying only repeats the paste.
      await wrapper.findAll("button").find(node => node.text() === "重试")!.trigger("click");
      await flushPromises();
      expect(bridge.quickPasteChoose).toHaveBeenCalledTimes(2);
      expect(bridge.preparePasteEntry).toHaveBeenCalledTimes(1);
    } finally { wrapper.unmount(); }
  });
});
