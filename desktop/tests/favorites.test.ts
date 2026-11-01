import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { contextMenuState, runContextMenuItem } from "../src/lib/context-menu";
import { bridge } from "../src/api/bridge";
import { createFavoritesStore } from "../src/stores/favorites";
import FavoritesView from "../src/components/FavoritesView.vue";
import type { Favorite, FavoritesPage } from "../src/api/types";

vi.mock("../src/api/bridge", () => ({ bridge: {
  favorites: vi.fn(), favorite: vi.fn(), addFavorite: vi.fn(), updateFavorite: vi.fn(),
  deleteFavorite: vi.fn(), deleteFavorites: vi.fn(), copyFavorite: vi.fn(),
  exportFavorites: vi.fn(),
  importFavorites: vi.fn(), chooseFile: vi.fn(),
  reorderFavorites: vi.fn(), createFavoriteGroup: vi.fn(),
  renameFavoriteGroup: vi.fn(), deleteFavoriteGroup: vi.fn(),
} }));
const favorite: Favorite = { id: "a", title: "Alpha", content: "FULL body beyond preview",
  group: "Work", position: 2, created: 1, updated: 1 };
/** What `addFavorite` answers: the new row, or — when the body is already in
 *  the library — the row it matched and no new row at all. */
type Added = {
  favorite: Favorite | null;
  duplicate?: { id: string; title: string; group: string } | null;
};
const page: FavoritesPage = { items: [{ id: "a", title: "Alpha", preview: "truncated",
  group: "Work", position: 2, created: 1, updated: 1 }], total: 1, offset: 0,
  groups: ["Work"], session_id: "s", seq: 0 };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(bridge.favorites).mockResolvedValue(page);
  vi.mocked(bridge.favorite).mockResolvedValue({ favorite });
  vi.mocked(bridge.addFavorite).mockResolvedValue({ favorite, duplicate: null });
  vi.mocked(bridge.updateFavorite).mockResolvedValue({ favorite });
  vi.mocked(bridge.deleteFavorite).mockResolvedValue({ deleted: true });
  vi.mocked(bridge.deleteFavorites).mockResolvedValue({ deleted: 0, ids: [] });
  vi.mocked(bridge.copyFavorite).mockResolvedValue({ copied: true });
  vi.mocked(bridge.reorderFavorites).mockResolvedValue({ moved: 1 });
  vi.mocked(bridge.createFavoriteGroup).mockResolvedValue({ groups: ["Work"] });
  vi.mocked(bridge.renameFavoriteGroup).mockResolvedValue({ renamed: 0, groups: ["Work"] });
  vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({ moved: 0, removed: 0, removed_ids: [], groups: [] });
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

describe("independent favorites", () => {
  it("gates native access on capability availability", async () => {
    const store = createFavoritesStore(() => false);
    await store.refresh();
    await store.open("a");
    store.create();
    await store.copy("a");
    expect(bridge.favorites).not.toHaveBeenCalled();
    expect(bridge.favorite).not.toHaveBeenCalled();
    expect(bridge.copyFavorite).not.toHaveBeenCalled();
    expect(store.state.editorOpen).toBe(false);
  });
  it("uses full detail, never the list preview, when saving edits and position", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    await store.open("a");
    store.edit({ title: "Edited", position: 7 });
    await store.save();
    expect(bridge.updateFavorite).toHaveBeenCalledExactlyOnceWith("a", "Edited",
      favorite.content, "Work", 7);
    expect(bridge.addFavorite).not.toHaveBeenCalled();
  });
  it("ignores old detail after creating a new draft", async () => {
    const old = deferred<{ favorite: Favorite }>();
    vi.mocked(bridge.favorite).mockReturnValueOnce(old.promise);
    const store = createFavoritesStore(() => true);
    const opening = store.open("a");
    store.create();
    store.edit({ content: "New body" });
    old.resolve({ favorite });
    await opening;
    expect(store.state.selectedId).toBeNull();
    expect(store.state.draft.content).toBe("New body");
    await store.save();
    expect(bridge.addFavorite).toHaveBeenCalledExactlyOnceWith("", "New body", "", false);
  });
  it("ignores stale list responses after search/group change", async () => {
    const old = deferred<FavoritesPage>();
    vi.mocked(bridge.favorites).mockReturnValueOnce(old.promise);
    const store = createFavoritesStore(() => true);
    const loading = store.refresh();
    store.search("new", "Work");
    await flushPromises();
    old.resolve({ ...page, items: [], total: 0 });
    await loading;
    expect(store.state.items).toEqual(page.items);
    expect(bridge.favorites).toHaveBeenLastCalledWith("new", "Work", 0, 30);
  });
  it("does not replay failed writes and keeps error visible after refresh", async () => {
    const store = createFavoritesStore(() => true);
    await store.open("a");
    store.edit({ content: "Edited" });
    vi.mocked(bridge.updateFavorite).mockRejectedValueOnce({ code: "REQUEST_TIMEOUT", message: "Unknown result", retryable: false });
    expect(await store.save()).toBe(false);
    store.invalidate();
    await store.refresh();
    expect(bridge.updateFavorite).toHaveBeenCalledOnce();
    expect(store.state.error?.code).toBe("REQUEST_TIMEOUT");
    expect(store.state.draft.content).toBe("Edited");
  });
  it("blocks duplicate writes and ignores completion after disposal", async () => {
    const pending = deferred<Added>();
    vi.mocked(bridge.addFavorite).mockReturnValueOnce(pending.promise);
    const store = createFavoritesStore(() => true);
    store.create();
    store.edit({ content: "New" });
    const saving = store.save();
    await store.save();
    store.dispose();
    pending.resolve({ favorite, duplicate: null });
    await saving;
    expect(bridge.addFavorite).toHaveBeenCalledOnce();
    expect(bridge.favorites).not.toHaveBeenCalled();
    expect(store.state.selectedId).toBeNull();
  });
  it("validates editor limits", async () => {
    const store = createFavoritesStore(() => true);
    store.create();
    store.edit({ content: "x".repeat(65537) });
    expect(await store.save()).toBe(false);
    expect(store.state.error?.code).toBe("INVALID_FAVORITE");
    expect(bridge.addFavorite).not.toHaveBeenCalled();
  });
  it("returns to the previous page when deletion removes the last page", async () => {
    const store = createFavoritesStore(() => true);
    store.state.offset = 30;
    vi.mocked(bridge.favorites).mockResolvedValueOnce({ ...page, items: [], total: 30, offset: 30 })
      .mockResolvedValueOnce({ ...page, total: 30 });
    await store.delete("a");
    expect(store.state.offset).toBe(0);
    expect(bridge.favorites).toHaveBeenNthCalledWith(1, "", "", 30, 30);
    expect(bridge.favorites).toHaveBeenNthCalledWith(2, "", "", 0, 30);
    expect(store.state.loading).toBe(false);
  });
  it("asks before discarding a dirty draft for a new favorite", async () => {
    const store = createFavoritesStore(() => true);
    await store.open("a");
    store.edit({ content: "Unsaved" });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await view.get('[aria-label="新建收藏"]').trigger("click");
    expect(store.state.selectedId).toBe("a");
    expect(store.state.draft.content).toBe("Unsaved");
    expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
    await view.get('[aria-labelledby="favorite-discard-title"] .danger').trigger("click");
    expect(store.state.selectedId).toBeNull();
    expect(store.state.draft.content).toBe("");
    view.unmount();
    store.dispose();
  });
  it("renders preview as text, copies, fetches detail, and confirms deletion", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    store.state.items[0] = { ...page.items[0], preview: "<img src=x>" };
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    expect(view.find(".favorite-summary img").exists()).toBe(false);
    await view.get('[aria-label="复制收藏 Alpha"]').trigger("click");
    await flushPromises();
    expect(bridge.copyFavorite).toHaveBeenCalledExactlyOnceWith("a");
    await view.get('[aria-label="编辑收藏 Alpha"]').trigger("click");
    await flushPromises();
    expect((view.get("#favorite-content").element as HTMLTextAreaElement).value).toBe(favorite.content);
    // The editor is the page's other tab now, so the row that opened it is not
    // on screen with it — the reader walks back the way they came.
    await view.findAll(".page-tabs button")[0].trigger("click");
    await view.get('[aria-label="删除收藏 Alpha"]').trigger("click");
    expect(bridge.deleteFavorite).not.toHaveBeenCalled();
    await view.get('[aria-labelledby="favorite-delete-title"] .danger').trigger("click");
    await flushPromises();
    expect(bridge.deleteFavorite).toHaveBeenCalledExactlyOnceWith("a");
    expect(store.state.editorOpen).toBe(false);
    view.unmount();
    store.dispose();
  });
  it("asks the library once the typing stops, and keeps what was typed", async () => {
    // Three letters used to be three reads of the list, each one replacing the
    // rows under the reader's eyes while they were still typing.  What the box
    // holds cannot wait for that: the words are the reader's, not the list's.
    const store = createFavoritesStore(() => true);
    await store.refresh();
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      const box = view.get('[aria-label="搜索收藏"]');
      for (const value of ["A", "Al", "Alp"]) await box.setValue(value);
      // The × is on the field with the first letter, not a read later.
      expect(view.find(".search-clear").exists()).toBe(true);
      expect((box.element as HTMLInputElement).value).toBe("Alp");
      // Nothing has gone out yet, and the refresh above is still the only read.
      expect(bridge.favorites).toHaveBeenCalledOnce();
      // Real time, because the case needs the promises the read is made of too.
      await new Promise((done) => { setTimeout(done, 260); });
      expect(bridge.favorites).toHaveBeenCalledTimes(2);
      expect(vi.mocked(bridge.favorites).mock.calls.at(-1)![0]).toBe("Alp");
    } finally {
      view.unmount();
      store.dispose();
    }
  });
  it("exports every favorite as Markdown and reports the written path", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    vi.mocked(bridge.exportFavorites).mockResolvedValue({
      filepath: "C:/Users/test/Downloads/clipsync-favorites-20260909-120000.md",
      filename: "clipsync-favorites-20260909-120000.md", count: 3, format: "markdown",
    });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await view.get('[aria-label="导出全部收藏"]').trigger("click");
    // The button asks which format now, so the write is the menu entry's.
    expect(contextMenuState().items.map((item) => item.id)).toEqual(["markdown", "json"]);
    await runContextMenuItem(contextMenuState().items[0]);
    await flushPromises();
    expect(bridge.exportFavorites).toHaveBeenCalledExactlyOnceWith("markdown");
    expect(view.get('[role="status"]').text()).toContain("已导出 3 条收藏");
    expect(view.get('[role="status"]').text()).toContain("clipsync-favorites-20260909-120000.md");
    // An export changes nothing, so the list must not be re-read.
    expect(bridge.favorites).toHaveBeenCalledOnce();
    view.unmount();
    store.dispose();
  });
  it("puts the exported path on the clipboard from the line that shows it", async () => {
    // U159: the path was the only output and could not be selected, so the one
    // thing a reader wants to do with it — paste it somewhere — meant retyping
    // a namespaced file name from a 12px line.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const store = createFavoritesStore(() => true);
    await store.refresh();
    const filepath = "C:/Users/test/Downloads/clipsync-favorites-20260909-120000.md";
    vi.mocked(bridge.exportFavorites).mockResolvedValue({
      filepath, filename: "clipsync-favorites-20260909-120000.md", count: 3, format: "markdown",
    });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await view.get('[aria-label="导出全部收藏"]').trigger("click");
    await runContextMenuItem(contextMenuState().items[0]);
    await flushPromises();
    await view.get('[aria-label="复制导出路径"]').trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledExactlyOnceWith(filepath);
    // The button says what happened, in the same place the click was.
    expect(view.get(".export-line__copy").text()).toContain("已复制路径");
    view.unmount();
    store.dispose();
  });
  it("keeps the export failure visible and reports no path", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    vi.mocked(bridge.exportFavorites).mockRejectedValueOnce({
      code: "EXPORT_FAILED", message: "Could not write the export file", retryable: true,
    });
    expect(await store.exportAll()).toBeNull();
    expect(store.state.error?.code).toBe("EXPORT_FAILED");
    store.dispose();
  });
});


describe("favorites groups and order", () => {
  const listed: FavoritesPage = {
    items: [
      { id: "a", title: "A", preview: "a", group: "Work", position: 0, created: 3, updated: 3 },
      { id: "b", title: "B", preview: "b", group: "Work", position: 1, created: 2, updated: 2 },
      { id: "c", title: "C", preview: "c", group: "", position: 2, created: 1, updated: 1 },
    ],
    total: 3, offset: 0, groups: ["Later", "Work"],
    group_counts: { Later: 0, Work: 2 }, library_total: 3,
    session_id: "s", seq: 0,
  };

  async function loaded() {
    vi.mocked(bridge.favorites).mockResolvedValue(listed);
    const store = createFavoritesStore(() => true);
    await store.refresh();
    return store;
  }

  it("shows the new order at once, then lets the host place it", async () => {
    const store = await loaded();
    const write = deferred<{ moved: number }>();
    vi.mocked(bridge.reorderFavorites).mockReturnValueOnce(write.promise);
    const moving = store.reorder("a", "c");
    // Painted before the write lands: a row that only moves after a round trip
    // reads as a dropped drag on a slow connection.
    expect(store.state.items.map((item) => item.id)).toEqual(["b", "c", "a"]);
    write.resolve({ moved: 3 });
    expect(await moving).toBe(true);
    expect(bridge.reorderFavorites).toHaveBeenCalledExactlyOnceWith(["b", "c", "a"]);
    store.dispose();
  });

  it("puts the old order back when the host refuses the move", async () => {
    const store = await loaded();
    vi.mocked(bridge.reorderFavorites).mockRejectedValueOnce({
      code: "NOT_FOUND", message: "No favorites in the order exist", retryable: false,
    });
    expect(await store.reorder("a", "c")).toBe(false);
    expect(store.state.items.map((item) => item.id)).toEqual(["a", "b", "c"]);
    expect(store.state.error?.code).toBe("NOT_FOUND");
    store.dispose();
  });

  it("reorders the part of the list a group shows without reaching past it", async () => {
    // A filtered list is part of the order, not a different order: only the
    // rows on screen are sent, and the host hands them back the slots they
    // already held, so the row the filter hid is neither pushed aside nor
    // landed on.
    const store = await loaded();
    vi.mocked(bridge.favorites).mockResolvedValue({
      ...listed, items: listed.items.slice(0, 2), total: 2, groups: ["Work"],
    });
    store.search("", "Work");
    await flushPromises();
    expect(store.state.items.map((item) => item.id)).toEqual(["a", "b"]);
    await store.reorder("a", "b");
    expect(bridge.reorderFavorites).toHaveBeenCalledExactlyOnceWith(["b", "a"]);
    store.dispose();
  });

  it("refuses a nameless group rather than creating one called nothing", async () => {
    const store = await loaded();
    expect(await store.createGroup("   ")).toBe(false);
    expect(await store.renameGroup("Work", "  ")).toBe(false);
    expect(await store.createGroup("x".repeat(129))).toBe(false);
    expect(bridge.createFavoriteGroup).not.toHaveBeenCalled();
    expect(bridge.renameFavoriteGroup).not.toHaveBeenCalled();
    expect(store.state.error?.code).toBe("INVALID_FAVORITE");
    store.dispose();
  });

  it("creates a group from the sidebar and selects it", async () => {
    const store = await loaded();
    vi.mocked(bridge.createFavoriteGroup).mockResolvedValue({ groups: ["Later", "Trip", "Work"] });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await flushPromises();
    await view.get('[aria-label="新建分组"]').trigger("click");
    const field = view.get('[aria-label="新分组名称"]');
    await field.setValue("Trip");
    await field.trigger("keydown.enter");
    await flushPromises();
    expect(bridge.createFavoriteGroup).toHaveBeenCalledExactlyOnceWith("Trip");
    expect(bridge.favorites).toHaveBeenLastCalledWith("", "Trip", 0, 30);
    view.unmount();
    store.dispose();
  });

  it("renames and deletes a group from its own right-click menu", async () => {
    const store = await loaded();
    vi.mocked(bridge.renameFavoriteGroup).mockResolvedValue({ renamed: 2, groups: ["Job"] });
    vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({ moved: 2, removed: 0, removed_ids: [], groups: [] });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await flushPromises();
    // The menu itself is drawn once, at the window's root, so a page hands it
    // entries rather than markup; what is asserted here is the entries.
    await view.findAll(".group-row")[2].trigger("contextmenu");
    expect(contextMenuState().items.map((item) => item.label)).toEqual(["重命名", "删除分组"]);
    expect(contextMenuState().items[1].danger).toBe(true);
    const renaming = runContextMenuItem(contextMenuState().items[0]);
    await flushPromises();
    expect(contextMenuState().open).toBe(false);
    const field = view.get('[aria-label="重命名分组 Work"]');
    await field.setValue("Job");
    await field.trigger("keydown.enter");
    await flushPromises();
    expect(bridge.renameFavoriteGroup).toHaveBeenCalledExactlyOnceWith("Work", "Job");

    await renaming;
    await view.findAll(".group-row")[2].trigger("contextmenu");
    const deleting = runContextMenuItem(contextMenuState().items[1]);
    await flushPromises();
    await deleting;
    await nextTick();
    // Deleting a group keeps its favourites, and the dialog says so.
    expect(view.text()).toContain("收藏本身不会被删除");
    expect(view.text()).toContain("2 条收藏会移到未分组");
    await view.get("#favorite-group-delete-title")
      .element.closest("dialog")!.querySelector(".danger")!
      .dispatchEvent(new MouseEvent("click"));
    await flushPromises();
    expect(bridge.deleteFavoriteGroup).toHaveBeenCalledExactlyOnceWith("Work", false);
    view.unmount();
    store.dispose();
  });

  it("takes the group's favourites with it only when the box is ticked", async () => {
    // U160: the dialog promised the favourites were kept, and there was no way
    // to ask for the other thing — so cleaning up a group meant deleting it,
    // then finding its rows in 未分组 and deleting them one by one.
    const store = await loaded();
    vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({
      moved: 0, removed: 2, removed_ids: ["fav-1", "fav-2"], groups: [] });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await flushPromises();
    await view.findAll(".group-row")[2].trigger("contextmenu");
    const deleting = runContextMenuItem(contextMenuState().items[1]);
    await flushPromises();
    await deleting;
    await nextTick();
    const dialog = view.get("#favorite-group-delete-title").element.closest("dialog")!;

    // Unticked, the sentence is still the promise it has always been.
    expect(view.text()).toContain("收藏本身不会被删除");
    const box = dialog.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(box.disabled).toBe(false);
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    await nextTick();
    // Ticked, it is the other sentence — a promise of keeping them cannot stay
    // on screen while they are being deleted.
    expect(view.text()).toContain("都会被删除，此操作不可撤销");
    expect(view.text()).not.toContain("收藏本身不会被删除");
    expect(dialog.querySelector(".danger")!.textContent!.trim()).toBe("删除分组和收藏");

    dialog.querySelector<HTMLElement>(".danger")!.click();
    await flushPromises();
    expect(bridge.deleteFavoriteGroup).toHaveBeenCalledExactlyOnceWith("Work", true);
    view.unmount();
    store.dispose();
  });

  it("closes the editor when the favourite it holds went with the group", async () => {
    const store = await loaded();
    vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({
      moved: 0, removed: 1, removed_ids: [store.state.items[0].id], groups: [] });
    await store.open(store.state.items[0].id);
    await flushPromises();
    expect(store.state.editorOpen).toBe(true);
    await store.deleteGroup("Work", true);
    await flushPromises();
    // An editor left open on a row that no longer exists offers to save it.
    expect(store.state.editorOpen).toBe(false);
    store.dispose();
  });

  it("lists a library's entries without turning each one into a heading", async () => {
    // A library of thirty entries was thirty `<h2>`s, on the same level as the
    // cards around them — so the heading list a reader jumps by was the list of
    // rows, and the sections that hold them were lost among the rows.  The
    // title is a span now, on the class the type rule names.  See U78.
    const store = createFavoritesStore(() => true);
    vi.mocked(bridge.favorites).mockResolvedValue({
      ...page,
      items: [
        { ...page.items[0], id: "a", title: "Alpha" },
        { ...page.items[0], id: "b", title: "Beta" },
        { ...page.items[0], id: "c", title: "Gamma" },
      ],
      total: 3,
    });
    await store.refresh();
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await flushPromises();
    expect(view.findAll(".favorite-row").length).toBe(3);
    // Three rows, and not one of them is a heading: the heading list is the
    // page's own structure again.
    expect(view.findAll(".favorite-row h2").length).toBe(0);
    expect(view.findAll(".favorite-summary h2").length).toBe(0);
    const titles = view.findAll(".favorite-row-title");
    expect(titles.map(node => node.text())).toEqual(["Alpha", "Beta", "Gamma"]);
    expect(titles[0].element.tagName).toBe("SPAN");
    view.unmount();
    store.dispose();
  });

});

describe("favorites duplicate check", () => {
  const matched = { id: "a", title: "Alpha", group: "Work" };

  it("asks instead of writing a second copy, and writes it only when told", async () => {
    // U156: the same snippet saved twice used to be two rows and no comment.
    const store = createFavoritesStore(() => true);
    await store.refresh();
    store.create();
    store.edit({ content: favorite.content });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      await view.findAll(".page-tabs button")[1].trigger("click");
      vi.mocked(bridge.addFavorite).mockResolvedValueOnce({ favorite: null, duplicate: matched });
      await view.get(".favorite-editor").trigger("submit");
      await flushPromises();
      // Submitted without `force`: the form's handler must not hand the save
      // its submit event, which is truthy and would read as "yes, again".
      expect(bridge.addFavorite).toHaveBeenLastCalledWith("", favorite.content, "", false);
      // Nothing was written and the draft is still the reader's.
      expect(store.state.selectedId).toBeNull();
      expect(store.state.draft.content).toBe(favorite.content);
      expect(store.state.dirty).toBe(true);
      // The question names the row it matched, because "a duplicate exists" is
      // not something a reader can act on.
      expect(view.get(".duplicate-line").text()).toContain("已存在相似条目，仍要新建？");
      expect(view.get(".duplicate-line").text()).toContain("Alpha");

      vi.mocked(bridge.addFavorite).mockResolvedValueOnce({ favorite, duplicate: null });
      await view.get(".duplicate-line .primary").trigger("click");
      await flushPromises();
      expect(bridge.addFavorite).toHaveBeenLastCalledWith("", favorite.content, "", true);
      expect(store.state.duplicate).toBeNull();
      expect(store.state.selectedId).toBe("a");
      expect(view.find(".duplicate-line").exists()).toBe(false);
    } finally {
      view.unmount();
      store.dispose();
    }
  });

  it("goes to the row that matched, asking about the draft first", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    store.create();
    store.edit({ content: favorite.content });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      await view.findAll(".page-tabs button")[1].trigger("click");
      vi.mocked(bridge.addFavorite).mockResolvedValueOnce({ favorite: null, duplicate: matched });
      await view.get(".favorite-editor").trigger("submit");
      await flushPromises();
      await view.get(".duplicate-line__actions button").trigger("click");
      // Answering "no" by reading the other one replaces this draft, so the
      // page asks about that in the way it asks about every replaced draft.
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
      await view.get('[aria-labelledby="favorite-discard-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.favorite).toHaveBeenCalledExactlyOnceWith("a");
      expect(store.state.selectedId).toBe("a");
      expect(store.state.duplicate).toBeNull();
    } finally {
      view.unmount();
      store.dispose();
    }
  });

  it("puts the question away when the draft changes or the editor closes", async () => {
    const store = createFavoritesStore(() => true);
    store.create();
    store.edit({ content: "Same body" });
    vi.mocked(bridge.addFavorite).mockResolvedValue({ favorite: null, duplicate: matched });
    await store.save();
    expect(store.state.duplicate?.id).toBe("a");
    // A changed body is no longer the one that matched, so the question the
    // answer would apply to is gone.
    store.edit({ content: "Same body, but edited" });
    expect(store.state.duplicate).toBeNull();
    await store.save();
    expect(store.state.duplicate?.id).toBe("a");
    store.close();
    expect(store.state.duplicate).toBeNull();
    store.dispose();
  });
});

describe("favorites export formats and import", () => {
  it("offers JSON as the format that can come back", async () => {
    // U153: Markdown is for reading, but a library that cannot return cannot
    // move between machines — so the one button asks which of the two.
    const store = createFavoritesStore(() => true);
    await store.refresh();
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      await view.get('[aria-label="导出全部收藏"]').trigger("click");
      const items = contextMenuState().items;
      expect(items.map((item) => item.id)).toEqual(["markdown", "json"]);
      expect(items[1].label).toContain("JSON");
      await runContextMenuItem(items[1]);
      await flushPromises();
      expect(bridge.exportFavorites).toHaveBeenCalledExactlyOnceWith("json");
    } finally {
      view.unmount();
      store.dispose();
    }
  });

  it("imports the file the reader picked and says what landed", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    vi.mocked(bridge.chooseFile).mockResolvedValue("D:/downloads/clipsync-favorites.json");
    vi.mocked(bridge.importFavorites).mockResolvedValue({ added: 3, skipped: 2, total: 5 });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      await view.get('[aria-label="导入收藏"]').trigger("click");
      await flushPromises();
      // The dialog is filtered to this project's own JSON, because that is the
      // only shape the sidecar reads.
      expect(bridge.chooseFile).toHaveBeenCalledExactlyOnceWith("favorites");
      expect(bridge.importFavorites)
        .toHaveBeenCalledExactlyOnceWith("D:/downloads/clipsync-favorites.json");
      const line = view.get(".export-line").text();
      expect(line).toContain("已导入 3 条收藏");
      expect(line).toContain("跳过 2 条");
      // The list is re-read: the rows the import added are in it.
      expect(bridge.favorites).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      store.dispose();
    }
  });

  it("says nothing when the picker is closed, and shows a refused file", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      // Cancelling is not a failure: no note, no red band, no request.
      vi.mocked(bridge.chooseFile).mockResolvedValueOnce(null);
      await view.get('[aria-label="导入收藏"]').trigger("click");
      await flushPromises();
      expect(bridge.importFavorites).not.toHaveBeenCalled();
      expect(view.find(".export-line").exists()).toBe(false);
      expect(store.state.error).toBeNull();

      // A file that is not ours reaches the reader as a sentence about the
      // file, not as "something was invalid".
      vi.mocked(bridge.chooseFile).mockResolvedValueOnce("D:/notes.json");
      vi.mocked(bridge.importFavorites).mockRejectedValueOnce({
        code: "INVALID_ARGUMENT",
        message: "这不是 ClipSync 导出的收藏文件。", retryable: false,
      });
      await view.get('[aria-label="导入收藏"]').trigger("click");
      await flushPromises();
      expect(store.state.error?.message).toContain("不是 ClipSync 导出的收藏文件");
      expect(view.find(".export-line").exists()).toBe(false);
    } finally {
      view.unmount();
      store.dispose();
    }
  });
});

describe("favorites selection", () => {
  const listed: FavoritesPage = {
    items: [
      { id: "a", title: "A", preview: "a", group: "", position: 0, created: 3, updated: 3 },
      { id: "b", title: "B", preview: "b", group: "", position: 1, created: 2, updated: 2 },
      { id: "c", title: "C", preview: "c", group: "", position: 2, created: 1, updated: 1 },
    ],
    total: 3, offset: 0, groups: [], group_counts: {}, library_total: 3,
    session_id: "s", seq: 0,
  };

  async function loaded(say = vi.fn()) {
    vi.mocked(bridge.favorites).mockResolvedValue(listed);
    const store = createFavoritesStore(() => true, say);
    await store.refresh();
    return store;
  }

  it("collects rows only while the mode is on, and forgets them when it ends", async () => {
    // U154: tidying twenty rows was twenty deletes and twenty confirmations.
    const store = await loaded();
    store.toggleSelect("a");
    expect(store.state.selected).toEqual([]);
    store.selectMode(true);
    store.toggleSelect("a");
    expect(store.state.selected).toEqual(["a"]);
    store.toggleSelect("a");
    expect(store.state.selected).toEqual([]);
    store.selectMode(false);
    expect(store.state.selecting).toBe(false);
    store.dispose();
  });

  it("turns all-of-this-page into none of it, and back", async () => {
    const store = await loaded();
    store.selectMode(true);
    store.selectPage();
    expect(store.state.selected).toEqual(["a", "b", "c"]);
    store.selectPage();
    expect(store.state.selected).toEqual([]);
    store.dispose();
  });

  it("deletes the selection in one call and closes an editor on a row that went", async () => {
    const say = vi.fn();
    const store = await loaded(say);
    vi.mocked(bridge.deleteFavorites).mockResolvedValue({ deleted: 2, ids: ["a", "b"] });
    await store.open("a");
    store.selectMode(true);
    store.toggleSelect("a");
    store.toggleSelect("b");
    const reads = vi.mocked(bridge.favorites).mock.calls.length;

    expect(await store.deleteSelected()).toBe(true);
    // One request, not one per row: that is the cost the action exists to cut.
    expect(bridge.deleteFavorites).toHaveBeenCalledExactlyOnceWith(["a", "b"]);
    expect(vi.mocked(bridge.favorites).mock.calls.length).toBe(reads + 1);
    expect(say).toHaveBeenCalledWith("已删除 2 条收藏");
    // An editor left open on one of them would offer to save a favourite that
    // no longer exists.
    expect(store.state.editorOpen).toBe(false);
    expect(store.state.selecting).toBe(false);
    expect(store.state.selected).toEqual([]);
    store.dispose();
  });

  it("forgets a tick whose row is no longer on the page", async () => {
    // A tick nobody can see is a tick nobody can check.
    const store = await loaded();
    store.selectMode(true);
    store.toggleSelect("a");
    vi.mocked(bridge.favorites).mockResolvedValueOnce({ ...listed, items: [], total: 0 });
    await store.refresh();
    expect(store.state.selected).toEqual([]);
    store.dispose();
  });

  it("ticks rows from the list and deletes them after asking", async () => {
    const store = await loaded();
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    try {
      await flushPromises();
      // No box beside every title until the mode is on: it would cost every
      // reader room for a job most of them are not doing.
      expect(view.findAll(".favorite-pick").length).toBe(0);
      await view.get('[aria-label="批量选择收藏"]').trigger("click");
      expect(view.findAll(".favorite-pick").length).toBe(3);
      expect(view.get(".selection-bar").text()).toContain("勾选要处理的收藏");
      // The whole row is the hit area: a pointer that arrives at a row to
      // collect it should not have to find a 16px box.
      await view.findAll(".favorite-row")[0].trigger("click");
      expect(store.state.selected).toEqual(["a"]);
      expect(view.get(".selection-bar").text()).toContain("已选 1 条");
      // The row's own buttons are gone in this mode: it is not a row being
      // edited, copied or deleted one at a time.
      expect(view.findAll(".row-actions").length).toBe(0);
      await view.get(".selection-bar__pick").trigger("click");
      expect(store.state.selected).toEqual(["a", "b", "c"]);

      await view.get(".selection-bar__delete").trigger("click");
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
      expect(view.text()).toContain("删除所选的 3 条收藏？");
      vi.mocked(bridge.deleteFavorites).mockResolvedValue({ deleted: 3, ids: ["a", "b", "c"] });
      await view.get('[aria-labelledby="favorite-batch-delete-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.deleteFavorites).toHaveBeenCalledExactlyOnceWith(["a", "b", "c"]);
      expect(store.state.selecting).toBe(false);
    } finally {
      view.unmount();
      store.dispose();
    }
  });
});
