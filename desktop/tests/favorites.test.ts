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
  deleteFavorite: vi.fn(), copyFavorite: vi.fn(), exportFavorites: vi.fn(),
  reorderFavorites: vi.fn(), createFavoriteGroup: vi.fn(),
  renameFavoriteGroup: vi.fn(), deleteFavoriteGroup: vi.fn(),
} }));
const favorite: Favorite = { id: "a", title: "Alpha", content: "FULL body beyond preview",
  group: "Work", position: 2, created: 1, updated: 1 };
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
  vi.mocked(bridge.addFavorite).mockResolvedValue({ favorite });
  vi.mocked(bridge.updateFavorite).mockResolvedValue({ favorite });
  vi.mocked(bridge.deleteFavorite).mockResolvedValue({ deleted: true });
  vi.mocked(bridge.copyFavorite).mockResolvedValue({ copied: true });
  vi.mocked(bridge.reorderFavorites).mockResolvedValue({ moved: 1 });
  vi.mocked(bridge.createFavoriteGroup).mockResolvedValue({ groups: ["Work"] });
  vi.mocked(bridge.renameFavoriteGroup).mockResolvedValue({ renamed: 0, groups: ["Work"] });
  vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({ moved: 0, groups: [] });
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
    expect(bridge.addFavorite).toHaveBeenCalledExactlyOnceWith("", "New body", "");
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
    const pending = deferred<{ favorite: Favorite }>();
    vi.mocked(bridge.addFavorite).mockReturnValueOnce(pending.promise);
    const store = createFavoritesStore(() => true);
    store.create();
    store.edit({ content: "New" });
    const saving = store.save();
    await store.save();
    store.dispose();
    pending.resolve({ favorite });
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
  it("exports every favorite as Markdown and reports the written path", async () => {
    const store = createFavoritesStore(() => true);
    await store.refresh();
    vi.mocked(bridge.exportFavorites).mockResolvedValue({
      filepath: "C:/Users/test/Downloads/clipsync-favorites-20260909-120000.md",
      filename: "clipsync-favorites-20260909-120000.md", count: 3, format: "markdown",
    });
    const view = mount(FavoritesView, { props: { store, enabled: true } });
    await view.get('[aria-label="导出全部收藏"]').trigger("click");
    await flushPromises();
    expect(bridge.exportFavorites).toHaveBeenCalledExactlyOnceWith("markdown");
    expect(view.get('[role="status"]').text()).toContain("已导出 3 条收藏");
    expect(view.get('[role="status"]').text()).toContain("clipsync-favorites-20260909-120000.md");
    // An export changes nothing, so the list must not be re-read.
    expect(bridge.favorites).toHaveBeenCalledOnce();
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
    vi.mocked(bridge.deleteFavoriteGroup).mockResolvedValue({ moved: 2, groups: [] });
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
    expect(bridge.deleteFavoriteGroup).toHaveBeenCalledExactlyOnceWith("Work");
    view.unmount();
    store.dispose();
  });

});
