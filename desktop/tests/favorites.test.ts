import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { bridge } from "../src/api/bridge";
import { createFavoritesStore } from "../src/stores/favorites";
import FavoritesView from "../src/components/FavoritesView.vue";
import type { Favorite, FavoritesPage } from "../src/api/types";

vi.mock("../src/api/bridge", () => ({ bridge: {
  favorites: vi.fn(), favorite: vi.fn(), addFavorite: vi.fn(), updateFavorite: vi.fn(),
  deleteFavorite: vi.fn(), copyFavorite: vi.fn(), exportFavorites: vi.fn(),
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
  it("ignores an old detail response after another selection", async () => {
    const old = deferred<{ favorite: Favorite }>();
    vi.mocked(bridge.favorite).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ favorite: { ...favorite, id: "b", content: "B body" } });
    const store = createFavoritesStore(() => true);
    const opening = store.open("a");
    await store.open("b");
    store.edit({ content: "B edited" });
    old.resolve({ favorite });
    await opening;
    expect(store.state.selectedId).toBe("b");
    expect(store.state.draft.content).toBe("B edited");
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
  it("preserves edited content on invalidation while refreshing list snapshots", async () => {
    const store = createFavoritesStore(() => true);
    await store.open("a");
    store.edit({ content: "Unsaved" });
    store.invalidate();
    await store.refresh();
    expect(store.state.draft.content).toBe("Unsaved");
    expect(store.state.stale).toBe(true);
    expect(bridge.favorite).toHaveBeenCalledOnce();
    expect(bridge.favorites).toHaveBeenCalledOnce();
  });
  it("reloads pristine detail following invalidation", async () => {
    const store = createFavoritesStore(() => true);
    await store.open("a");
    vi.mocked(bridge.favorite).mockResolvedValueOnce({ favorite: { ...favorite, content: "Remote edit" } });
    store.invalidate();
    await store.refresh();
    expect(store.state.draft.content).toBe("Remote edit");
    expect(store.state.stale).toBe(false);
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
  it.each([{ title: "x".repeat(257) }, { content: "x".repeat(65537) },
    { group: "x".repeat(129) }, { position: -1 }, { position: NaN }])(
    "validates editor limits", async (patch) => {
      const store = createFavoritesStore(() => true);
      store.create();
      store.edit(patch);
      expect(await store.save()).toBe(false);
      expect(store.state.error?.code).toBe("INVALID_FAVORITE");
      expect(bridge.addFavorite).not.toHaveBeenCalled();
    });
  it("rejects invalid IDs and oversized queries", async () => {
    const store = createFavoritesStore(() => true);
    await store.open("x".repeat(65));
    await store.delete("");
    await store.copy(" ");
    store.search("x".repeat(513));
    store.search("", "x".repeat(129));
    expect(bridge.favorite).not.toHaveBeenCalled();
    expect(bridge.deleteFavorite).not.toHaveBeenCalled();
    expect(bridge.copyFavorite).not.toHaveBeenCalled();
    expect(bridge.favorites).not.toHaveBeenCalled();
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
  it("does not apply detail or list responses after disposal", async () => {
    const detail = deferred<{ favorite: Favorite }>();
    const list = deferred<FavoritesPage>();
    vi.mocked(bridge.favorite).mockReturnValueOnce(detail.promise);
    vi.mocked(bridge.favorites).mockReturnValueOnce(list.promise);
    const store = createFavoritesStore(() => true);
    const opening = store.open("a");
    const loading = store.refresh();
    store.dispose();
    detail.resolve({ favorite });
    list.resolve(page);
    await Promise.all([opening, loading]);
    expect(store.state.loaded).toBe(false);
    expect(store.state.items).toEqual([]);
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
