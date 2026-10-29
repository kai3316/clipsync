import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import QuickSearch from "../src/components/QuickSearch.vue";
import { bridge } from "../src/api/bridge";
import type { FavoritesPage, HistoryItem, HistoryPage } from "../src/api/types";

vi.mock("../src/api/bridge", () => ({
  bridge: { history: vi.fn(), favorites: vi.fn() },
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

function historyPage(items: HistoryItem[], total = items.length): HistoryPage {
  return { session_id: "s", seq: 0, offset: 0, total, items, has_history: items.length > 0 } as HistoryPage;
}

function favoritesPage(items: FavoritesPage["items"], total = items.length): FavoritesPage {
  return { items, total, offset: 0, groups: [], session_id: "s", seq: 0 } as FavoritesPage;
}

function favourite(id: string, title: string, group = "工作") {
  return { id, title, preview: "", group, position: 0, created: 1, updated: 1 };
}

/** Type into the palette's box and let the pause it waits for pass. */
async function type(wrapper: ReturnType<typeof mount>, text: string) {
  await wrapper.get('[aria-label="搜索历史与收藏"]').setValue(text);
  await vi.advanceTimersByTimeAsync(260);
  await flushPromises();
}

describe("quick search", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(bridge.history).mockResolvedValue(historyPage([hit()], 12));
    vi.mocked(bridge.favorites).mockResolvedValue(favoritesPage([favourite("f1", "周报模板")], 3));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(bridge.history).mockReset();
    vi.mocked(bridge.favorites).mockReset();
  });

  it("asks the history list for the word and shows what came back", async () => {
    // The three searches the window had each only knew their own page, so a
    // reader who half-remembered a snippet had to answer "did I save it?" before
    // being allowed to look.  This asks the one question they can answer.
    // See U50.
    const wrapper = mount(QuickSearch);
    try {
      await type(wrapper, "会议");
      // The page's own interface, at the page's own paging shape: the palette
      // shows the first few of exactly what the history page would show.
      expect(bridge.history).toHaveBeenCalledWith("会议", 0, 6, "", "");
      expect(wrapper.get(".quick-search-count").text()).toBe("找到 12 条");
      expect(wrapper.findAll(".quick-search-rows li")).toHaveLength(1);
      expect(wrapper.get(".quick-search-text").text()).toBe("会议纪要：周三下午三点");
      // The row's source is the second line, which is what tells two clips with
      // the same text apart.
      expect(wrapper.get(".quick-search-detail").text()).toBe("Notes");
      // Records, not a guess: the count is the sidecar's total for the query,
      // which is larger than the six rows on screen.
      expect(wrapper.get(".quick-search-open").text()).toBe("在剪贴板历史中查看全部 12 条");
    } finally {
      wrapper.unmount();
    }
  });

  it("searches the library when the scope is switched, and drops the group", async () => {
    const wrapper = mount(QuickSearch);
    try {
      await type(wrapper, "周报");
      vi.mocked(bridge.history).mockClear();
      await wrapper.findAll(".quick-search-scopes button")[1].trigger("click");
      await flushPromises();
      // A switch is a decision, not typing: it searches at once rather than
      // waiting out a pause nobody is taking.
      expect(bridge.favorites).toHaveBeenCalledWith("周报", "", 0, 6);
      expect(bridge.history).not.toHaveBeenCalled();
      // No group: the palette counts every favourite that holds the word, and a
      // hand-over to a page still filtered to a group would show fewer than the
      // number printed here.
      expect(wrapper.get(".quick-search-text").text()).toBe("周报模板");
      expect(wrapper.get(".quick-search-detail").text()).toBe("工作");
      expect(wrapper.get(".quick-search-open").text()).toBe("在收藏库中查看全部 3 条");
      // And the switch says which one is on, to a reader and to a screen reader.
      const chips = wrapper.findAll(".quick-search-scopes button");
      expect(chips.map(chip => chip.attributes("aria-pressed"))).toEqual(["false", "true"]);
    } finally {
      wrapper.unmount();
    }
  });

  it("hands the word over on Enter, with the scope it was found in", async () => {
    const wrapper = mount(QuickSearch);
    try {
      await type(wrapper, "会议");
      await wrapper.get('[aria-label="搜索历史与收藏"]').trigger("keydown", { key: "Enter" });
      expect(wrapper.emitted("open")).toEqual([["history", "会议"]]);
      // The library hands over the same way, which is the half a reader lands on
      // when the word was something they had saved rather than copied.
      await wrapper.findAll(".quick-search-scopes button")[1].trigger("click");
      await flushPromises();
      await wrapper.get(".quick-search-open").trigger("click");
      expect(wrapper.emitted("open")).toEqual([["history", "会议"], ["favorites", "会议"]]);
    } finally {
      wrapper.unmount();
    }
  });

  it("does not search on an empty box, or on the spaces around a word", async () => {
    const wrapper = mount(QuickSearch);
    try {
      // Opening the palette is not a query: the hint is what a reader who has
      // not typed anything needs, and "nothing found" would be a claim about a
      // search that never ran.
      expect(wrapper.get(".note").text()).toContain("输入关键词");
      await vi.advanceTimersByTimeAsync(300);
      expect(bridge.history).not.toHaveBeenCalled();
      // A word and then spaces around it is one query, not three.
      await type(wrapper, "  会议  ");
      await type(wrapper, "会议   ");
      expect(bridge.history).toHaveBeenCalledTimes(2);
      expect(vi.mocked(bridge.history).mock.calls[1][0]).toBe("会议");
      // Emptying the box puts the hint back and asks nothing.
      vi.mocked(bridge.history).mockClear();
      await type(wrapper, "");
      expect(bridge.history).not.toHaveBeenCalled();
      expect(wrapper.get(".note").text()).toContain("输入关键词");
      expect(wrapper.findAll(".quick-search-rows li")).toHaveLength(0);
    } finally {
      wrapper.unmount();
    }
  });

  it("says a search failed rather than showing the last one's rows", async () => {
    const wrapper = mount(QuickSearch);
    try {
      await type(wrapper, "会议");
      expect(wrapper.findAll(".quick-search-rows li")).toHaveLength(1);
      vi.mocked(bridge.history).mockRejectedValue({ code: "RPC_FAILED", message: "连接已断开" });
      await type(wrapper, "周报");
      expect(wrapper.get('[role="alert"]').text()).toBe("连接已断开");
      // The rows from the previous word are gone: a failed second search leaving
      // the first answer on screen is a list that no longer matches the box.
      expect(wrapper.findAll(".quick-search-rows li")).toHaveLength(0);
      expect(wrapper.find(".quick-search-open").exists()).toBe(false);
    } finally {
      wrapper.unmount();
    }
  });

  it("keeps the newer answer when a slower request lands after it", async () => {
    const wrapper = mount(QuickSearch);
    try {
      // The first word's answer is held open; the second word's arrives first.
      // Without a generation counter the slow one would paint over it and the
      // box would be showing a word the reader has already replaced.
      let settleSlow: (page: HistoryPage) => void = () => {};
      vi.mocked(bridge.history).mockReturnValueOnce(new Promise<HistoryPage>(resolve => { settleSlow = resolve; }));
      vi.mocked(bridge.history).mockResolvedValueOnce(historyPage([hit({ id: "h2", preview: "第二个词的结果" })]));
      await type(wrapper, "第一个");
      await type(wrapper, "第二个");
      expect(wrapper.get(".quick-search-text").text()).toBe("第二个词的结果");
      settleSlow(historyPage([hit({ id: "h1", preview: "第一个词的结果" })]));
      await flushPromises();
      expect(wrapper.get(".quick-search-text").text()).toBe("第二个词的结果");
    } finally {
      wrapper.unmount();
    }
  });

  it("starts empty again when it is reopened", async () => {
    // The dialog closes on the hand-over, and a palette that came back holding
    // last time's word and last time's rows is one the reader has to clear
    // before they can use it.
    const wrapper = mount(QuickSearch);
    try {
      await type(wrapper, "会议");
      (wrapper.vm as unknown as { reset: () => void }).reset();
      await flushPromises();
      expect((wrapper.get('[aria-label="搜索历史与收藏"]').element as HTMLInputElement).value).toBe("");
      expect(wrapper.findAll(".quick-search-rows li")).toHaveLength(0);
      expect(wrapper.get(".note").text()).toContain("输入关键词");
      // Back to the first scope as well: the next question is a new one.
      expect(wrapper.findAll(".quick-search-scopes button")[0].classes()).toContain("active");
    } finally {
      wrapper.unmount();
    }
  });
});
