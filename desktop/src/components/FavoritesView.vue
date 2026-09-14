<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { Star, Search, Plus, RefreshCw, Copy, Check, Pencil, Trash2, Save, X, ChevronLeft, ChevronRight, ChevronUp, ChevronDown, Download, GripVertical, FolderPlus, MoreHorizontal } from "@lucide/vue";
import { t } from "../i18n";
import type { FavoriteSummary } from "../api/types";
import type { createFavoritesStore } from "../stores/favorites";
import { openContextMenu } from "../lib/context-menu";
import { previewText, shortTime } from "../i18n/format";

const props = defineProps<{
  store: ReturnType<typeof createFavoritesStore>;
  enabled: boolean;
  /** Say that a click did something, for the clicks this pane cannot show.
   * The window owns the notice stack and hands one down. */
  notify?: (message: string) => void;
}>();
const state = props.store.state;
const busy = computed(() => !props.enabled || state.pending);

/* ── The page's two jobs ─────────────────────────────────────────────── */

/** Which one is on screen: the library, or the editor opened out of it.
 *
 * The page used to be a single grid of three columns — the groups rail, the
 * list, and the editor beside them — and below about 1400px all three were
 * squeezed: the rail took its 200px, the editor its 260, and the list got what
 * was left.  The editor is the one of the three that is a place of its own
 * rather than a way of reading the library, so it is the one that moved to a
 * tab, where it gets the width of the page.
 *
 * The tab does not follow `state.editorOpen`: an editor can be left open with
 * an unsaved draft, and the page opens on the library every time so that the
 * strip always starts where the reader left the list. */
type FavoritesTab = "browse" | "edit";
const tab = ref<FavoritesTab>("browse");
const sections = computed<Array<{ id: FavoritesTab; label: string }>>(() => [
  { id: "browse", label: t("收藏列表") },
  { id: "edit", label: t("编辑器") },
]);

const deleting = ref<FavoriteSummary | null>(null);
const deleteDialog = ref<HTMLDialogElement | null>(null);
const discardDialog = ref<HTMLDialogElement | null>(null);
const nextEditor = ref<(() => void) | null>(null);
function navigateEditor(action: () => void) {
  if (busy.value) return;
  if (state.dirty) { nextEditor.value = action; discardDialog.value?.showModal(); }
  else action();
}
/** Open, create or close the editor, and follow it to its tab.
 *
 * Every control that touches the editor goes through here, because the editor
 * is on the other tab: a row's 铅笔 that filled in a form the reader cannot see
 * would read as a button that did nothing.  The switch is inside the action
 * rather than beside it so that it happens when the action does — a dirty
 * draft puts the discard dialog in between, and answering it with 继续编辑 must
 * leave the reader where they were. */
function editHere(action: () => void) {
  navigateEditor(() => { action(); tab.value = "edit"; });
}
function closeEditor() {
  navigateEditor(() => { props.store.close(); tab.value = "browse"; });
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

/* ── Groups ──────────────────────────────────────────────────────────── */

const renaming = ref("");
const renameValue = ref("");
const renameInput = ref<HTMLInputElement | null>(null);
const creating = ref(false);
const newGroupName = ref("");
const newGroupInput = ref<HTMLInputElement | null>(null);
const groupToDelete = ref("");
const groupDeleteDialog = ref<HTMLDialogElement | null>(null);

/** Only one group renames at a time, but its input sits inside a `v-for`, so
 * the element arrives through a function ref rather than a string one, which
 * Vue would collect into an array. */
function setRenameInput(element: unknown) {
  renameInput.value = (element as HTMLInputElement | null) ?? null;
}
async function renameInputShown() {
  await nextTick();
  renameInput.value?.focus();
  renameInput.value?.select();
}
async function newGroupInputShown() {
  await nextTick();
  newGroupInput.value?.focus();
}

function selectGroup(group: string) {
  if (busy.value) return;
  props.store.search(state.query, group);
}

function startRename(group: string) {
  renaming.value = group;
  renameValue.value = group;
  void renameInputShown();
}
/** The library's own refresh.
 *
 * The list usually comes back as it was and the spinner is gone before the eye
 * reaches it, so the click would otherwise have nothing to show for itself. */
async function refreshNow() {
  await props.store.refresh();
  props.notify?.(state.libraryTotal
    ? t("已刷新 · {count} 条收藏", { count: state.libraryTotal })
    : t("已刷新 · 收藏库是空的"));
}
/** Report a write the pane cannot show on its own.
 *
 * Every one of these is a menu entry or a dialog that closes, and the thing it
 * changed is somewhere the reader is not looking — a group name in the rail, an
 * order among rows.  The window turns them into notices; without a window to
 * report to there is nothing to say them to. */
function say(message: string) {
  props.notify?.(message);
}
async function saveRename() {
  const from = renaming.value;
  const to = renameValue.value.trim();
  renaming.value = "";
  if (!from || !to || from === to) return;
  if (await props.store.renameGroup(from, to)) say(t("分组已重命名为 {name}", { name: to }));
}
function cancelRename() {
  renaming.value = "";
}

async function createGroup() {
  const name = newGroupName.value.trim();
  creating.value = false;
  newGroupName.value = "";
  if (!name) return;
  if (await props.store.createGroup(name)) say(t("已新建分组 {name}", { name }));
}

watch(groupToDelete, async (name) => {
  await nextTick();
  if (name) groupDeleteDialog.value?.showModal();
  else groupDeleteDialog.value?.close();
});
async function confirmDeleteGroup() {
  const name = groupToDelete.value;
  groupToDelete.value = "";
  if (name && await props.store.deleteGroup(name)) say(t("已删除分组 {name}", { name }));
}

function groupMenu(event: MouseEvent, group: string) {
  openContextMenu(event, [
    { id: "rename", label: t("重命名"), icon: Pencil, disabled: busy.value,
      run: () => startRename(group) },
    null,
    { id: "delete", label: t("删除分组"), icon: Trash2, danger: true, divider: true,
      disabled: busy.value, run: () => { groupToDelete.value = group; } },
  ]);
}

/* ── Reordering ──────────────────────────────────────────────────────── */

const dragging = ref("");
const dragOver = ref("");

/** Touch and other coarse pointers cannot start an HTML5 drag, so a phone or
 * a tablet gets the arrows the legacy panel gave them instead. */
const coarse = ref(
  typeof window !== "undefined" && !!window.matchMedia
    && window.matchMedia("(pointer: coarse)").matches);

function dragStart(event: DragEvent, item: FavoriteSummary) {
  if (busy.value) { event.preventDefault(); return; }
  dragging.value = item.id;
  dragOver.value = "";
  // Firefox will not start a drag without payload, and the payload is a
  // courtesy to anything else that accepts a drop.
  event.dataTransfer?.setData("text/plain", item.id);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}
function dragOverRow(item: FavoriteSummary) {
  if (dragging.value && dragging.value !== item.id) dragOver.value = item.id;
}
function dragEnd() {
  dragging.value = "";
  dragOver.value = "";
}
async function drop(item: FavoriteSummary) {
  const from = dragging.value;
  dragEnd();
  if (!from || from === item.id) return;
  await report(await props.store.reorder(from, item.id));
}
async function step(index: number, direction: number) {
  await report(await props.store.step(index, direction));
}
/** Say whether the new order was kept.
 *
 * The row is already in its new place by the time the answer arrives — the
 * store paints the move first and puts it back if the write is refused — so
 * silence would leave the reader believing a dropped drag was saved. */
function report(saved: boolean) {
  say(saved ? t("顺序已保存") : t("保存顺序失败"));
}
</script>

<template>
  <section class="favorites-view" :aria-label="t('收藏库')">
    <div v-if="!enabled" class="empty empty--page"><Star :size="36" /><h2>{{ t("收藏库暂不可用") }}</h2></div>
    <template v-else>
      <nav class="page-tabs" :aria-label="t('收藏页分区')">
        <button v-for="section in sections" :key="section.id" type="button"
          :class="{ 'page-tab--active': tab === section.id }"
          :aria-current="tab === section.id ? 'page' : undefined"
          :title="section.id === 'edit' && state.dirty ? t('编辑器里有未保存的修改') : undefined"
          @click="tab = section.id">{{ section.label }}<span
            v-if="section.id === 'edit' && state.dirty" class="page-tab-mark" aria-hidden="true"> *</span></button>
      </nav>

      <!-- Outside the panels: an error can come from either one — a refused
           group rename lands while the reader is on the library, and a refused
           save while they are on the editor. -->
      <div v-if="state.error" class="error-band" role="alert">{{ state.error.message }} ({{ state.error.code }})</div>

      <div v-if="tab === 'browse'" class="page-col">
        <div class="toolbar">
          <label class="search"><Search :size="17" /><input :aria-label="t('搜索收藏')" :placeholder="t('搜索收藏')"
            :value="state.query" maxlength="512" :disabled="busy"
            @input="store.search(($event.target as HTMLInputElement).value)" /></label>
          <button class="icon-button" :aria-label="t('刷新收藏')" :title="t('刷新收藏')" :disabled="busy || state.loading" @click="refreshNow"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
          <button :aria-label="t('新建收藏')" :disabled="busy" @click="editHere(store.create)"><Plus :size="18" />{{ t("新建") }}</button>
          <button class="icon-button" :aria-label="t('导出全部收藏')" :title="t('将全部收藏导出为 Markdown 文件')" :disabled="busy || !state.libraryTotal" @click="exportAll"><Download :size="18" /></button>
        </div>
        <p v-if="exported" class="muted small" role="status">{{ t("已导出 {count} 条收藏 → {path}", { count: exported.count, path: exported.filepath }) }}</p>
        <div class="favorites-workspace">
          <aside class="favorites-groups" :aria-label="t('收藏分组')">
            <div class="list-heading"><span>{{ t("收藏分组") }}</span>
              <button class="icon-button" :aria-label="t('新建分组')" :title="t('新建分组')" :disabled="busy || creating" @click="creating = true; newGroupInputShown()"><FolderPlus :size="16" /></button></div>
            <div class="group-line">
              <button class="group-row" :class="{ active: !state.group }" :disabled="busy" @click="selectGroup('')">
                <span class="group-name">{{ t("全部分组") }}</span><span class="group-count">{{ state.libraryTotal }}</span>
              </button>
            </div>
            <!-- 重命名 and 删除 were on this row's right-click menu and nowhere
                 else, which is to say nowhere a reader would look for them.  The
                 menu keeps both — the row is still the thing they act on — and
                 the button beside it is the way in that can be seen. -->
            <div v-for="group in state.groups" :key="group" class="group-line">
              <button class="group-row" :class="{ active: state.group === group }" :disabled="busy"
                @click="selectGroup(group)" @contextmenu.prevent="groupMenu($event, group)">
                <template v-if="renaming === group">
                  <input :ref="setRenameInput" class="group-input" :aria-label="t('重命名分组 {name}', { name: group })"
                    :value="renameValue" maxlength="128" @click.stop @input="renameValue = ($event.target as HTMLInputElement).value"
                    @keydown.enter="saveRename" @keydown.escape="cancelRename" @blur="saveRename" />
                </template>
                <template v-else>
                  <span class="group-name">{{ group }}</span>
                  <span class="group-count">{{ state.groupCounts[group] ?? 0 }}</span>
                </template>
              </button>
              <button v-if="renaming !== group" class="group-more" type="button"
                :aria-label="t('分组操作 {name}', { name: group })" :title="t('重命名或删除分组')"
                :disabled="busy" @click="groupMenu($event, group)"><MoreHorizontal :size="15" /></button>
            </div>
            <input v-if="creating" ref="newGroupInput" class="group-input" :aria-label="t('新分组名称')"
              :placeholder="t('分组名称')" :value="newGroupName" maxlength="128"
              @input="newGroupName = ($event.target as HTMLInputElement).value"
              @keydown.enter="createGroup" @keydown.escape="creating = false" @blur="createGroup" />
          </aside>
          <section class="favorites-list" :aria-label="t('收藏条目')" :aria-busy="state.loading">
            <div v-if="!state.items.length" class="empty empty--page"><Star :size="30" /><h2>{{ state.loading ? t('正在读取收藏') : t('暂无收藏') }}</h2></div>
            <article v-for="(item, index) in state.items" :key="item.id" class="favorite-row"
              :class="{ selected: state.selectedId === item.id, dragging: dragging === item.id, 'drag-over': dragOver === item.id }"
              @dragover.prevent="dragOverRow(item)" @drop.prevent="drop(item)" @dragend="dragEnd">
              <span class="drag-handle" :draggable="!busy" :aria-hidden="true"
                @dragstart="dragStart($event, item)" @dragend="dragEnd"><GripVertical :size="15" /></span>
              <div class="favorite-summary"><h2>{{ item.title || t('未命名') }}</h2><p>{{ previewText(item.preview) }}</p>
                <!-- The group, and only the group.  A zero-based position used to
                     follow it — the editor's own 位置 field, spelled out in the
                     middle of a list that already shows the order by being in it,
                     and reading as a mystery to anyone who had not opened the
                     editor.  Placement is what the handle and, on a touch screen,
                     the arrows are for. -->
                <span class="muted small">{{ item.group || t('未分组') }}<template v-if="item.created"> · {{ shortTime(item.created) }}</template></span></div>
              <div v-if="coarse" class="row-step">
                <button class="icon-button" :aria-label="t('上移')" :title="t('上移')" :disabled="busy || index === 0" @click="step(index, -1)"><ChevronUp :size="16" /></button>
                <button class="icon-button" :aria-label="t('下移')" :title="t('下移')" :disabled="busy || index === state.items.length - 1" @click="step(index, 1)"><ChevronDown :size="16" /></button>
              </div>
              <div class="row-actions">
                <button class="icon-button" :aria-label="t('编辑收藏 {title}', { title: item.title })" :title="t('编辑收藏')" :disabled="busy" @click="editHere(() => store.open(item.id))"><Pencil :size="17" /></button>
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
        </div>
      </div>

      <div v-else class="page-col">
        <form v-if="state.editorOpen" class="favorite-editor" :aria-label="t('收藏编辑器')" :aria-busy="state.detailLoading || state.pending" @submit.prevent="store.save">
          <div class="editor-heading"><h2>{{ state.selectedId ? t('编辑收藏') : t('新建收藏') }}{{ state.dirty ? ' *' : '' }}</h2>
            <button class="icon-button" type="button" :aria-label="t('关闭收藏编辑器')" :title="t('关闭编辑器')" :disabled="busy" @click="closeEditor"><X :size="18" /></button></div>
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
        <div v-else class="empty">
          <Pencil :size="30" />
          <h2>{{ t("没有正在编辑的收藏") }}</h2>
          <p>{{ t("在收藏列表里点一条右边的铅笔来编辑它，或者在这里新建一条。") }}</p>
          <button :disabled="busy" @click="editHere(store.create)"><Plus :size="18" />{{ t("新建收藏") }}</button>
        </div>
      </div>
    </template>
    <dialog ref="deleteDialog" class="modal" aria-labelledby="favorite-delete-title" @close="deleting = null" @cancel="deleting = null">
      <h2 id="favorite-delete-title">{{ t("删除收藏？") }}</h2><p>{{ t("{title} 将被永久删除。", { title: deleting?.title || t('未命名') }) }}</p>
      <p v-if="state.error" class="modal-error small" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <div class="modal-actions"><button autofocus @click="deleting = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !deleting" @click="confirmDelete">{{ t("删除收藏") }}</button></div>
    </dialog>
    <dialog ref="groupDeleteDialog" class="modal" aria-labelledby="favorite-group-delete-title" @close="groupToDelete = ''" @cancel="groupToDelete = ''">
      <h2 id="favorite-group-delete-title">{{ t("删除分组？") }}</h2>
      <p>{{ t("分组 {group} 中的 {count} 条收藏会移到未分组，收藏本身不会被删除。", { group: groupToDelete, count: state.groupCounts[groupToDelete] ?? 0 }) }}</p>
      <div class="modal-actions"><button autofocus @click="groupToDelete = ''">{{ t("取消") }}</button><button class="danger" :disabled="busy" @click="confirmDeleteGroup">{{ t("删除分组") }}</button></div>
    </dialog>
    <dialog ref="discardDialog" class="modal" aria-labelledby="favorite-discard-title" @close="nextEditor = null" @cancel="nextEditor = null">
      <h2 id="favorite-discard-title">{{ t("放弃未保存的修改？") }}</h2><p>{{ t("当前收藏的修改尚未保存。") }}</p>
      <div class="modal-actions"><button autofocus @click="discardDialog?.close()">{{ t("继续编辑") }}</button><button class="danger" :disabled="busy" @click="discard">{{ t("放弃修改") }}</button></div>
    </dialog>
  </section>
</template>
