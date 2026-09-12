import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { BridgeError, Favorite, FavoriteSummary } from "../api/types";

const emptyDraft = () => ({ title: "", content: "", group: "", position: 0 });
type Draft = ReturnType<typeof emptyDraft>;

export function createFavoritesStore(enabled: () => boolean) {
  const state = reactive({
    items: [] as FavoriteSummary[], groups: [] as string[], total: 0,
    query: "", group: "", offset: 0, limit: 30,
    loading: false, pending: false, detailLoading: false,
    editorOpen: false, selectedId: null as string | null,
    draft: emptyDraft(), dirty: false, loaded: false, stale: false,
    copiedId: null as string | null, error: null as BridgeError | null,
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
    state.editorOpen = false;
    state.selectedId = null;
    state.draft = emptyDraft();
    state.loaded = false;
    state.dirty = false;
    state.stale = false;
    state.detailLoading = false;
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
      state.draft = { ...state.draft, ...patch };
      state.dirty = true;
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
    save() {
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
      return mutate(async () => {
        const result = id === null
          ? await bridge.addFavorite(draft.title, draft.content, draft.group)
          : await bridge.updateFavorite(id, draft.title, draft.content, draft.group, draft.position);
        if (available() && editor === editorSequence) applyFavorite(result.favorite);
      });
    },
    delete(id: string) {
      if (!validId(id)) { invalid(); return Promise.resolve(false); }
      return mutate(async () => {
        const result = await bridge.deleteFavorite(id);
        if (!result.deleted) throw { code: "DELETE_FAILED", message: t("收藏未删除"), retryable: false };
        if (available() && state.selectedId === id) close();
      });
    },
    copy(id: string) {
      if (!validId(id)) { invalid(); return Promise.resolve(false); }
      return mutate(async () => {
        state.copiedId = null;
        const result = await bridge.copyFavorite(id);
        if (!result.copied) throw { code: "COPY_FAILED", message: t("收藏未复制"), retryable: false };
        if (available()) state.copiedId = id;
      });
    },
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
      state.total = 0;
      state.loading = false;
      state.copiedId = null;
    },
    dispose() { disposed = true; ++listSequence; ++detailSequence; ++editorSequence; },
  };
}
