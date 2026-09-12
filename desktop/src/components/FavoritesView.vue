<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { Star, Search, Plus, RefreshCw, Copy, Check, Pencil, Trash2, Save, X, ChevronLeft, ChevronRight, Download } from "@lucide/vue";
import { t } from "../i18n";
import type { FavoriteSummary } from "../api/types";
import type { createFavoritesStore } from "../stores/favorites";

const props = defineProps<{ store: ReturnType<typeof createFavoritesStore>; enabled: boolean }>();
const state = props.store.state;
const busy = computed(() => !props.enabled || state.pending);
const deleting = ref<FavoriteSummary | null>(null);
const deleteDialog = ref<HTMLDialogElement | null>(null);
const discardDialog = ref<HTMLDialogElement | null>(null);
const nextEditor = ref<(() => void) | null>(null);
function navigateEditor(action: () => void) {
  if (busy.value) return;
  if (state.dirty) { nextEditor.value = action; discardDialog.value?.showModal(); }
  else action();
}
function discard() {
  const action = nextEditor.value;
  nextEditor.value = null;
  discardDialog.value?.close();
  action?.();
}
watch(deleting, async (item) => {
  await nextTick();
  if (item) deleteDialog.value?.showModal();
  else deleteDialog.value?.close();
});
async function confirmDelete() {
  if (deleting.value && await props.store.delete(deleting.value.id)) deleting.value = null;
}
const exported = ref<{ filepath: string; count: number } | null>(null);
async function exportAll() {
  exported.value = null;
  const result = await props.store.exportAll("markdown");
  if (result) exported.value = { filepath: result.filepath, count: result.count };
}
</script>

<template>
  <section class="favorites-view" :aria-label="t('收藏库')">
    <div v-if="!enabled" class="empty"><Star :size="36" /><h2>{{ t("收藏库暂不可用") }}</h2></div>
    <template v-else>
      <div class="toolbar">
        <label class="search"><Search :size="17" /><input :aria-label="t('搜索收藏')" :placeholder="t('搜索收藏')"
          :value="state.query" maxlength="512" :disabled="busy"
          @input="store.search(($event.target as HTMLInputElement).value)" /></label>
        <select :aria-label="t('筛选收藏分组')" :value="state.group" :disabled="busy"
          @change="store.search(state.query, ($event.target as HTMLSelectElement).value)">
          <option value="">{{ t("全部分组") }}</option>
          <option v-for="group in [...new Set([...state.groups, state.group])].filter(Boolean)" :key="group" :value="group">{{ group }}</option>
        </select>
        <button class="icon-button" :aria-label="t('刷新收藏')" :title="t('刷新收藏')" :disabled="busy || state.loading" @click="store.refresh"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
        <button :aria-label="t('新建收藏')" :disabled="busy" @click="navigateEditor(store.create)"><Plus :size="18" />{{ t("新建") }}</button>
        <button class="icon-button" :aria-label="t('导出全部收藏')" :title="t('将全部收藏导出为 Markdown 文件')" :disabled="busy || !state.total" @click="exportAll"><Download :size="18" /></button>
      </div>
      <p v-if="exported" class="muted small" role="status">{{ t("已导出 {count} 条收藏 → {path}", { count: exported.count, path: exported.filepath }) }}</p>
      <div v-if="state.error" class="error-band" role="alert">{{ state.error.message }} ({{ state.error.code }})</div>
      <div class="favorites-workspace" :class="{ 'has-editor': state.editorOpen }">
        <section class="favorites-list" :aria-label="t('收藏条目')" :aria-busy="state.loading">
          <div class="list-heading"><span>{{ t("{count} 条收藏", { count: state.total }) }}</span></div>
          <div v-if="!state.items.length" class="empty"><Star :size="30" /><h2>{{ state.loading ? t('正在读取收藏') : t('暂无收藏') }}</h2></div>
          <article v-for="item in state.items" :key="item.id" class="favorite-row" :class="{ selected: state.selectedId === item.id }">
            <div class="favorite-summary"><h2>{{ item.title || t('未命名') }}</h2><p>{{ item.preview }}</p>
              <span class="muted small">{{ item.group || t('未分组') }} · {{ t("位置 {position}", { position: item.position }) }}</span></div>
            <div class="row-actions">
              <button class="icon-button" :aria-label="t('编辑收藏 {title}', { title: item.title })" :title="t('编辑收藏')" :disabled="busy" @click="navigateEditor(() => store.open(item.id))"><Pencil :size="17" /></button>
              <button class="icon-button" :aria-label="t('复制收藏 {title}', { title: item.title })" :title="t('复制收藏')" :disabled="busy" @click="store.copy(item.id)"><Check v-if="state.copiedId === item.id" :size="17" /><Copy v-else :size="17" /></button>
              <button class="icon-button" :aria-label="t('删除收藏 {title}', { title: item.title })" :title="t('删除收藏')" :disabled="busy" @click="deleting = item"><Trash2 :size="17" /></button>
            </div>
          </article>
          <footer class="pagination">
            <span>{{ state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)}` : '0' }} / {{ state.total }}</span>
            <button class="icon-button" :aria-label="t('收藏上一页')" :title="t('上一页')" :disabled="busy || state.loading || !state.offset" @click="store.page(-1)"><ChevronLeft :size="18" /></button>
            <button class="icon-button" :aria-label="t('收藏下一页')" :title="t('下一页')" :disabled="busy || state.loading || state.offset + state.limit >= state.total" @click="store.page(1)"><ChevronRight :size="18" /></button>
          </footer>
        </section>
        <form v-if="state.editorOpen" class="favorite-editor" :aria-label="t('收藏编辑器')" :aria-busy="state.detailLoading || state.pending" @submit.prevent="store.save">
          <div class="editor-heading"><h2>{{ state.selectedId ? t('编辑收藏') : t('新建收藏') }}{{ state.dirty ? ' *' : '' }}</h2>
            <button class="icon-button" type="button" :aria-label="t('关闭收藏编辑器')" :title="t('关闭编辑器')" :disabled="busy" @click="navigateEditor(store.close)"><X :size="18" /></button></div>
          <p v-if="state.detailLoading" role="status">{{ t("正在读取完整内容") }}</p>
          <p v-if="state.stale" role="status">{{ t("收藏已在其他位置更新。") }}</p>
          <button v-if="state.selectedId && (!state.loaded || state.stale)" type="button" :disabled="busy || state.detailLoading"
            @click="navigateEditor(() => store.open(state.selectedId!))"><RefreshCw :size="17" />{{ t("重新读取") }}</button>
          <fieldset :disabled="busy || state.detailLoading || !state.loaded">
            <label for="favorite-title">{{ t("标题") }}</label>
            <input id="favorite-title" :value="state.draft.title" maxlength="256" @input="store.edit({ title: ($event.target as HTMLInputElement).value })" />
            <label for="favorite-group">{{ t("分组") }}</label>
            <input id="favorite-group" :value="state.draft.group" maxlength="128" list="favorite-groups" @input="store.edit({ group: ($event.target as HTMLInputElement).value })" />
            <datalist id="favorite-groups"><option v-for="group in state.groups" :key="group" :value="group" /></datalist>
            <template v-if="state.selectedId">
              <label for="favorite-position">{{ t("位置") }}</label>
              <input id="favorite-position" type="number" min="0" step="1" :value="state.draft.position" @input="store.edit({ position: ($event.target as HTMLInputElement).valueAsNumber })" />
            </template>
            <label for="favorite-content">{{ t("内容") }}</label>
            <textarea id="favorite-content" :value="state.draft.content" maxlength="65536" rows="14" @input="store.edit({ content: ($event.target as HTMLTextAreaElement).value })" />
            <button class="primary" type="submit" :disabled="!state.dirty"><Save :size="17" />{{ state.pending ? t('正在保存') : t('保存') }}</button>
          </fieldset>
        </form>
      </div>
    </template>
    <dialog ref="deleteDialog" class="modal" aria-labelledby="favorite-delete-title" @close="deleting = null" @cancel="deleting = null">
      <h2 id="favorite-delete-title">{{ t("删除收藏？") }}</h2><p>{{ t("{title} 将被永久删除。", { title: deleting?.title || t('未命名') }) }}</p>
      <p v-if="state.error" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <div class="modal-actions"><button autofocus @click="deleting = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !deleting" @click="confirmDelete">{{ t("删除收藏") }}</button></div>
    </dialog>
    <dialog ref="discardDialog" class="modal" aria-labelledby="favorite-discard-title" @close="nextEditor = null" @cancel="nextEditor = null">
      <h2 id="favorite-discard-title">{{ t("放弃未保存的修改？") }}</h2><p>{{ t("当前收藏的修改尚未保存。") }}</p>
      <div class="modal-actions"><button autofocus @click="discardDialog?.close()">{{ t("继续编辑") }}</button><button class="danger" :disabled="busy" @click="discard">{{ t("放弃修改") }}</button></div>
    </dialog>
  </section>
</template>
