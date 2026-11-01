import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { BridgeError, Favorite, FavoriteSummary } from "../api/types";

const emptyDraft = () => ({ title: "", content: "", group: "", position: 0 });
type Draft = ReturnType<typeof emptyDraft>;

export function createFavoritesStore(
  enabled: () => boolean,
  /** Say that a click did something, for the clicks the pane cannot show.
   * Optional so the pane's own tests can build the store without a window
   * behind it; the application store passes its own `toast`. */
  report: (message: string) => void = () => {},
) {
  const state = reactive({
    items: [] as FavoriteSummary[], groups: [] as string[], total: 0,
    groupCounts: {} as Record<string, number>, libraryTotal: 0,
    query: "", group: "", offset: 0, limit: 30,
    loading: false, pending: false, detailLoading: false,
    editorOpen: false, selectedId: null as string | null,
    draft: emptyDraft(), dirty: false, loaded: false, stale: false,
    copiedId: null as string | null, error: null as BridgeError | null,
    /** The favourite whose body the last save matched, waiting for the reader
     *  to say whether the second copy is wanted.  Set by `save`, cleared by
     *  `dismissDuplicate` or by the confirming save. */
    duplicate: null as { id: string; title: string; group: string } | null,
    /** Whether the list is in its selection mode, and what is ticked in it.
     *
     * Selection exists for the one job a list of thirty rows cannot do one row
     * at a time: tidying up.  It is a mode rather than a check box on every row
     * all the time, because a check box beside every title costs every reader
     * room for a job most of them are not doing — and it puts a second target
     * next to the row's own click. */
    selecting: false,
    selected: [] as string[],
  });
  let disposed = false;
  let listSequence = 0;
  let detailSequence = 0;
  let editorSequence = 0;
  const available = () => !disposed && enabled();
  const validId = (id: string) => !!id.trim() && id.length <= 64;
  function error(value: unknown) {
    state.error = value && typeof value === "object" && "code" in value
      ? value as BridgeError
      : { code: "FAVORITES_FAILED", message: t("收藏操作失败，请重试"), retryable: true };
  }
  function invalid() {
    error({ code: "INVALID_FAVORITE", message: t("收藏字段超出限制或记录无效"), retryable: false });
  }
  function applyFavorite(favorite: Favorite) {
    state.selectedId = favorite.id;
    state.draft = { title: favorite.title, content: favorite.content,
      group: favorite.group, position: favorite.position };
    state.loaded = true;
    state.dirty = false;
    state.stale = false;
  }
  async function open(id: string) {
    if (!available() || state.pending) return;
    if (!validId(id)) { invalid(); return; }
    const request = ++detailSequence;
    ++editorSequence;
    state.editorOpen = true;
    state.selectedId = id;
    state.draft = emptyDraft();
    state.dirty = false;
    state.loaded = false;
    state.stale = false;
    state.detailLoading = true;
    state.error = null;
    // Opening another row is an answer to the duplicate question as well: the
    // draft it was about is on its way out, so the question goes with it.
    state.duplicate = null;
    try {
      const result = await bridge.favorite(id);
      if (!available() || request !== detailSequence || state.dirty) return;
      if (result.favorite.id !== id) { invalid(); return; }
      applyFavorite(result.favorite);
    } catch (value) {
      if (available() && request === detailSequence) error(value);
    } finally {
      if (available() && request === detailSequence) state.detailLoading = false;
    }
  }
  async function refresh() {
    if (!available()) return;
    const request = ++listSequence;
    state.loading = true;
    try {
      const page = await bridge.favorites(state.query, state.group, state.offset, state.limit);
      if (!available() || request !== listSequence) return;
      state.items = page.items;
      state.groups = page.groups;
      state.total = page.total;
      state.groupCounts = page.group_counts ?? {};
      state.libraryTotal = page.library_total ?? page.total;
      // A tick on a row that is no longer on screen is a tick nobody can
      // check: the selection is about the page in front of the reader, and the
      // page is what the delete is about to act on.
      if (state.selected.length) {
        const present = new Set(page.items.map((item) => item.id));
        state.selected = state.selected.filter((id) => present.has(id));
      }
      // A deleted last row can leave the current page outside the result set.
      if (state.offset && state.offset >= page.total) {
        state.offset = Math.max(0, Math.floor((page.total - 1) / state.limit) * state.limit);
        await refresh();
        return;
      }
      if (state.stale && !state.dirty && state.selectedId && !state.pending) await open(state.selectedId);
    } catch (value) {
      if (available() && request === listSequence) error(value);
    } finally {
      if (available() && request === listSequence) state.loading = false;
    }
  }
  function close() {
    ++detailSequence;
    ++editorSequence;
    state.duplicate = null;
    state.editorOpen = false;
    state.selectedId = null;
    state.draft = emptyDraft();
    state.loaded = false;
    state.dirty = false;
    state.stale = false;
    state.detailLoading = false;
  }
  /** Move one row to another's place, as a drag does.
   *
   * Only the ids go: this list is one page of the library — and, under a
   * search or a group, part of the page — so where a row lands among the rows
   * that are not on screen is the host's to decide.  It decides by handing the
   * moved rows back the position slots they already held, in the new order, so
   * a row the reader cannot see can neither be pushed aside nor landed on.
   */
  async function reorder(fromId: string, toId: string): Promise<boolean> {
    if (!available() || state.pending) return false;
    const from = state.items.findIndex((item) => item.id === fromId);
    const to = state.items.findIndex((item) => item.id === toId);
    if (from < 0 || to < 0 || from === to) return false;
    const order = state.items.map((item) => item.id);
    const [moved] = order.splice(from, 1);
    order.splice(to, 0, moved);
    // Paint the new order before the host confirms it, and put it back if the
    // write is refused: a drag that visibly snapped back would read as a
    // dropped gesture rather than a rejected one.
    const previous = state.items.map((item) => item.id);
    const byId = new Map(state.items.map((item) => [item.id, item]));
    state.items = order.map((id) => byId.get(id)!);
    const ok = await mutate(async () => {
      await bridge.reorderFavorites(order);
    });
    if (!ok) {
      const restored = new Map(state.items.map((item) => [item.id, item]));
      state.items = previous.map((id) => restored.get(id) ?? byId.get(id)!);
    }
    return ok;
  }

  /** Move one row a step up or down, for a pointer that cannot drag. */
  function step(index: number, direction: number): Promise<boolean> {
    const item = state.items[index];
    const neighbour = state.items[index + direction];
    if (!item || !neighbour) return Promise.resolve(false);
    return reorder(item.id, neighbour.id);
  }

  function groupName(name: string): string | null {
    const value = name.trim();
    if (!value || value.length > 128) {
      if (value.length > 128) invalid();
      return null;
    }
    return value;
  }

  async function mutate(run: () => Promise<void>) {
    if (!available() || state.pending) return false;
    state.pending = true;
    state.error = null;
    try {
      await run();
      if (!available()) return false;
      await refresh();
      return true;
    } catch (value) {
      if (available()) error(value);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }
  /** Make a group that has nothing in it yet, and select it — selecting it is
   * the point, because the next favourite added is how it gets its first item. */
  function createGroup(name: string): Promise<boolean> {
    const value = groupName(name);
    if (value === null) return Promise.resolve(false);
    return mutate(async () => {
      const result = await bridge.createFavoriteGroup(value);
      if (!available()) return;
      state.groups = result.groups;
      state.group = value;
    });
  }

  function renameGroup(name: string, renameTo: string): Promise<boolean> {
    const value = groupName(renameTo);
    if (!name || value === null) return Promise.resolve(false);
    return mutate(async () => {
      const result = await bridge.renameFavoriteGroup(name, value);
      if (!available()) return;
      state.groups = result.groups;
      if (state.group === name) state.group = value;
    });
  }

  /** Delete a group.  Its favourites stay, unfiled — the legacy panel's rule,
   * and the one the dialog promises — unless ``purge``, which is the reader
   * saying "and the things in it too". */
  function deleteGroup(name: string, purge = false): Promise<boolean> {
    if (!name) return Promise.resolve(false);
    return mutate(async () => {
      const result = await bridge.deleteFavoriteGroup(name, purge);
      if (!available()) return;
      state.groups = result.groups;
      if (state.group === name) state.group = "";
      // What the editor is open on may be one of the rows that just went with
      // the group: an editor left open on it would offer to save a favourite
      // that no longer exists.  The ids are in the answer precisely so this is
      // a question with an answer rather than a guess.
      const removed = result.removed_ids || [];
      if (state.selectedId && removed.includes(state.selectedId)) close();
    });
  }

  return {
    state, refresh, open,
    close() { if (!state.pending) close(); },
    create() {
      if (!available() || state.pending) return;
      close();
      state.editorOpen = true;
      state.loaded = true;
      state.draft.group = state.group;
      state.error = null;
    },
    edit(patch: Partial<Draft>) {
      if (!available() || state.pending || !state.editorOpen || !state.loaded || state.detailLoading) return;
      ++detailSequence;
      // Changing the draft answers "create it again?" by making it a different
      // favourite: the question was about the body that matched, and that body
      // is no longer the one 保存 would send.
      state.duplicate = null;
      state.draft = { ...state.draft, ...patch };
      state.dirty = true;
    },
    /** Put the duplicate question away without answering it.  The draft stays
     *  exactly as it was, so the reader can change the body and save again. */
    dismissDuplicate() {
      state.duplicate = null;
    },
    search(query: string, group = state.group) {
      if (!available() || state.pending) return;
      if (query.length > 512 || group.length > 128) { invalid(); return; }
      state.query = query;
      state.group = group;
      state.offset = 0;
      void refresh();
    },
    page(direction: number) {
      if (!available() || state.loading || state.pending) return;
      state.offset = Math.max(0, Math.min(
        Math.max(0, Math.ceil(state.total / state.limit) - 1) * state.limit,
        state.offset + direction * state.limit));
      void refresh();
    },
  /** Save the editor.
   *
   * `force` is the reader's answer to "a favourite with this body is already
   * here — again?".  Without it the sidecar writes nothing and hands back the
   * row it matched, which lands in `state.duplicate` for the pane to ask about:
   * the same snippet under two titles is a thing people do deliberately, so
   * this must be a question rather than a silent merge or a silent refusal.
   */
  save(force = false) {
    if (!available() || state.pending || !state.loaded || state.detailLoading || !state.editorOpen) return Promise.resolve(false);
    const id = state.selectedId;
    const draft = { ...state.draft };
    const editor = editorSequence;
    if ((id !== null && !validId(id)) || draft.title.length > 256 ||
      draft.content.length > 65536 || draft.group.length > 128 ||
      !Number.isSafeInteger(draft.position) || draft.position < 0) {
      invalid();
      return Promise.resolve(false);
    }
    state.duplicate = null;
    return mutate(async () => {
      const result = id === null
        ? await bridge.addFavorite(draft.title, draft.content, draft.group, force)
        : await bridge.updateFavorite(id, draft.title, draft.content, draft.group, draft.position);
      if (!available() || editor !== editorSequence) return;
      if (result.favorite) applyFavorite(result.favorite);
      else if ("duplicate" in result && result.duplicate) state.duplicate = result.duplicate;
    });
  },
    delete(id: string) {
      if (!validId(id)) { invalid(); return Promise.resolve(false); }
      return mutate(async () => {
        const result = await bridge.deleteFavorite(id);
        if (!result.deleted) throw { code: "DELETE_FAILED", message: t("收藏未删除"), retryable: false };
        if (available() && state.selectedId === id) close();
        if (available()) report(t("已删除这条收藏"));
      });
    },
    /** Enter or leave the list's selection mode.
     *
     * Leaving forgets the ticks: a selection the reader cannot see must not be
     * waiting for them the next time they open it. */
    selectMode(on: boolean) {
      if (!available() || state.pending) return;
      state.selecting = on;
      if (!on) state.selected = [];
    },
    toggleSelect(id: string) {
      if (!available() || !state.selecting || !validId(id)) return;
      state.selected = state.selected.includes(id)
        ? state.selected.filter((each) => each !== id)
        : [...state.selected, id];
    },
    /** Tick everything on the page, or untick it when it is already all ticked.
     *
     * One button, because "select all" and "select none" are the two halves of
     * the same question and a second button would be a second thing to read in
     * a bar that has three. */
    selectPage() {
      if (!available() || !state.selecting || state.pending) return;
      const ids = state.items.map((item) => item.id);
      state.selected = ids.every((id) => state.selected.includes(id)) ? [] : ids;
    },
    /** Delete everything ticked, in one call rather than one per row. */
    deleteSelected(): Promise<boolean> {
      if (!available() || state.pending || !state.selected.length) return Promise.resolve(false);
      const ids = [...state.selected];
      return mutate(async () => {
        const result = await bridge.deleteFavorites(ids);
        if (!available()) return;
        // An editor left open on a row that just went would offer to save a
        // favourite that no longer exists; `ids` is why this is a question
        // with an answer rather than a guess.
        if (state.selectedId && result.ids.includes(state.selectedId)) close();
        state.selecting = false;
        state.selected = [];
        report(t("已删除 {count} 条收藏", { count: result.deleted }));
      });
    },
    copy(id: string) {
      if (!validId(id)) { invalid(); return Promise.resolve(false); }
      return mutate(async () => {
        state.copiedId = null;
        const result = await bridge.copyFavorite(id);
        if (!result.copied) throw { code: "COPY_FAILED", message: t("收藏未复制"), retryable: false };
        if (available()) state.copiedId = id;
        // The tick on the row is the only other sign, and it is in a list the
        // reader has already looked away from.
        if (available()) report(t("已复制收藏内容"));
      });
    },
    reorder, step, createGroup, renameGroup, deleteGroup,
    async exportAll(format = "markdown") {
      // Export writes a file but changes no stored favourite, so it must not
      // go through mutate() — that would refresh the list for nothing.
      if (!available() || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.exportFavorites(format);
        return available() ? result : null;
      } catch (value) {
        if (available()) error(value);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    /** Add the favourites in a file the reader picks.
     *
     * The dialog belongs to the view — it is a question, and a question with a
     * cancel button is not a failure — so a picker closed empty answers `null`
     * and says nothing.  The write goes through `mutate`: the list has to be
     * re-read afterwards, and a refused file has to land in the error band
     * like any other refusal.
     */
    async importAll(): Promise<{ added: number; skipped: number; total: number } | null> {
      if (!available() || state.pending) return null;
      let picked: string | null = null;
      try {
        picked = await bridge.chooseFile("favorites");
      } catch (value) {
        if (available()) error(value);
        return null;
      }
      const path = picked;
      if (!path) return null;
      let outcome = { added: 0, skipped: 0, total: 0 };
      const ok = await mutate(async () => {
        outcome = await bridge.importFavorites(path);
      });
      return ok ? outcome : null;
    },
    invalidate() {
      ++listSequence;
      ++detailSequence;
      state.detailLoading = false;
      if (state.editorOpen && state.selectedId) state.stale = true;
    },
    reset() {
      ++listSequence;
      close();
      state.items = [];
      state.groups = [];
      state.groupCounts = {};
      state.total = 0;
      state.libraryTotal = 0;
      state.loading = false;
      state.copiedId = null;
      state.selecting = false;
      state.selected = [];
    },
    dispose() { disposed = true; ++listSequence; ++detailSequence; ++editorSequence; },
  };
}
