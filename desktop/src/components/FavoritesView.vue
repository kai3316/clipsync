<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { Star, Search, Plus, RefreshCw, Copy, Check, Pencil, Trash2, Save, X, ChevronLeft, ChevronRight, ChevronUp, ChevronDown, Download, Upload, GripVertical, FolderPlus, MoreHorizontal, ListChecks } from "@lucide/vue";
import { t } from "../i18n";
import type { FavoriteSummary } from "../api/types";
import type { createFavoritesStore } from "../stores/favorites";
import { openContextMenu } from "../lib/context-menu";
import ErrorDetail from "./ErrorDetail.vue";
import EmptyState from "./EmptyState.vue";
import { tabKeydown } from "../lib/tabs";
import { SEARCH_DEBOUNCE_MS } from "../lib/search-box";
import { previewText, shortTime } from "../i18n/format";

const props = defineProps<{
  store: ReturnType<typeof createFavoritesStore>;
  enabled: boolean;
  /** Say that a click did something, for the clicks this pane cannot show.
   * The window owns the notice stack and hands one down. */
  notify?: (message: string) => void;
}>();
const state = props.store.state;
/** Favourites written before pictures were stored kept the history placeholder
 *  ("[Image]") as both title and body.  Treat that as a picture row so the
 *  placeholder is never read as content; the thumbnail is drawn when the store
 *  has one. */
const IMAGE_WORD = t("图片");
const IMAGE_PLACEHOLDERS = new Set(["[Image]", "Image", `[${IMAGE_WORD}]`, IMAGE_WORD]);
function isImageFavorite(item: FavoriteSummary) {
  return !!item.image || IMAGE_PLACEHOLDERS.has(item.title || "") || IMAGE_PLACEHOLDERS.has(item.preview || "");
}
/** A picture's stored title came from the history placeholder; the thumbnail
 *  is the content itself, so the placeholder must not be read aloud as the
 *  row's name. */
function favoriteTitle(item: FavoriteSummary) {
  if (isImageFavorite(item) && (!item.title || IMAGE_PLACEHOLDERS.has(item.title))) {
    return t("图片");
  }
  return item.title || t("未命名");
}
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
/** The strip's ids, for the arrow keys: the labels are translated, the ids are
 *  not, and a tab's position is a fact about the strip rather than about the
 *  language it is read in. */
const FAVORITES_TABS: readonly FavoritesTab[] = ["browse", "edit"];
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

/* ── The duplicate question ──────────────────────────────────────────── */

/** Say "yes, a second copy": the save runs again with `force`, which tells the
 *  host to write the row the first attempt held back.
 *
 * The body is unchanged — any edit to it would have put this question away —
 * so this is the same favourite the reader was already saving, twice. */
async function saveAnyway() {
  await props.store.save(true);
}
/** Say "no" by going to read the one that is already there.
 *
 * `open` puts the question away itself, and it may put a discard dialog in
 * between: the draft on screen is about to be replaced, and losing it silently
 * is the worse of the two answers. */
function openDuplicate() {
  const other = state.duplicate;
  if (!other || busy.value) return;
  editHere(() => void props.store.open(other.id));
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
/** How long the copy button keeps saying 已复制路径.  Long enough to read, short
 *  enough that it is an offer again by the time the reader wants it twice. */
const COPIED_HOLD_MS = 2000;
/** Whether the last copy still reads as "just copied".
 *
 * The path is long and the only other sign it went somewhere is a toast that
 * says nothing about *what* was copied, so the button says so itself for a
 * moment and goes back to offering. */
const exportCopied = ref(false);
let exportCopiedTimer: ReturnType<typeof setTimeout> | undefined;
async function copyExportPath() {
  if (!exported.value) return;
  try {
    await navigator.clipboard.writeText(exported.value.filepath);
    exportCopied.value = true;
    clearTimeout(exportCopiedTimer);
    exportCopiedTimer = setTimeout(() => { exportCopied.value = false; }, COPIED_HOLD_MS);
  } catch {
    say(t("无法复制到剪贴板"));
  }
}
onUnmounted(() => clearTimeout(exportCopiedTimer));
async function exportAll(format: "markdown" | "json") {
  exported.value = null;
  exportCopied.value = false;
  const result = await props.store.exportAll(format);
  if (result) exported.value = { filepath: result.filepath, count: result.count };
}
/** The export button opens a menu rather than writing one format.
 *
 * Markdown is for reading and for handing to something else; JSON is the one
 * that can come back, which is what makes 导入 possible at all.  Picking the
 * format is a two-choice question, so it is a menu rather than a second
 * button — the toolbar has four controls and a fifth would make the row read
 * as a launcher. */
function exportMenu(event: MouseEvent) {
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
  // Anchored to the button, not to the pointer: a menu opened with the
  // keyboard carries no coordinates, and one at (0, 0) would appear over the
  // sidebar instead of under the control that asked for it.
  openContextMenu({ clientX: Math.round(box.left), clientY: Math.round(box.bottom + 4) }, [
    { id: "markdown", label: t("导出为 Markdown"), disabled: busy.value || !state.libraryTotal,
      run: () => exportAll("markdown") },
    { id: "json", label: t("导出为 JSON（可再导入）"), disabled: busy.value || !state.libraryTotal,
      run: () => exportAll("json") },
  ]);
}

/* ── Importing a library ─────────────────────────────────────────────── */

/** What the last import did, or `null` before one has run.
 *
 * A toast cannot carry this: the interesting half is the count that was
 * *skipped*, and a sentence that disappears after four seconds is the wrong
 * place for a number a reader may want to check against the file they picked.
 */
const imported = ref<{ added: number; skipped: number; total: number } | null>(null);
/** The one sentence an import earns, which is different for each of four
 *  outcomes: nothing was in the file, everything was already here, some of it
 *  was, or all of it was. */
const importReport = computed(() => {
  const result = imported.value;
  if (!result) return "";
  if (!result.total) return t("这个文件里没有收藏");
  if (!result.added) return t("没有新增：{count} 条收藏都已经在库里", { count: result.skipped });
  if (!result.skipped) return t("已导入 {count} 条收藏", { count: result.added });
  return t("已导入 {added} 条收藏，跳过 {skipped} 条已在库中的", {
    added: result.added, skipped: result.skipped,
  });
});
async function importNow() {
  imported.value = null;
  // A picker closed with nothing chosen answers `null` and says nothing: the
  // cancel button is not a failure, and an error band for it would be a lie.
  const result = await props.store.importAll();
  if (result) imported.value = result;
}

/* ── Selecting several rows ──────────────────────────────────────────── */

/** Whether every row on this page is ticked — what turns the all-rows button
 *  into its opposite half. */
const allPicked = computed(() => state.items.length > 0
  && state.items.every((item) => state.selected.includes(item.id)));
const batchDeleteDialog = ref<HTMLDialogElement | null>(null);

/** Ask before deleting a selection.
 *
 * The row's own delete asks; a selection is the same act N times over, and the
 * one thing a reader cannot do is see what they ticked a moment after the bar
 * that said how many. */
function askDeleteSelected() {
  if (busy.value || !state.selected.length) return;
  batchDeleteDialog.value?.showModal();
}
async function confirmDeleteSelected() {
  batchDeleteDialog.value?.close();
  await props.store.deleteSelected();
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
/** Whether the group's favourites go with it.  Reset every time the dialog
 *  opens: a tick left over from the last group deleted would take rows with it
 *  that this reader never chose. */
const purgeGroup = ref(false);

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

/** Empty the search box from the field itself, keeping the caret where the
 *  reader already was: clearing is the start of the next search, not the end
 *  of the last one. */
const searchInput = ref<HTMLInputElement | null>(null);
/** What the box holds, which is not quite what the list was asked about: the
 * words land in the box on the keypress and in `state.query` a moment later,
 * and keeping both is what puts the × beside the first letter. */
const searchText = ref(state.query);
let searchTimer: ReturnType<typeof setTimeout> | undefined;
/** Ask once the typing stops, and keep asking if a write is still open.
 *
 * `store.search` answers nothing while a write is in flight — the rows a new
 * query would filter are the ones that write is about to replace — so a search
 * typed during one would otherwise be dropped, leaving the box holding words
 * the list was never asked about.  The wait re-arms instead of giving up. */
function queueSearch(value: string) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (state.pending) { queueSearch(value); return; }
    props.store.search(value, state.group);
  }, SEARCH_DEBOUNCE_MS);
}
function typeSearch(value: string) {
  searchText.value = value;
  queueSearch(value);
}
function clearSearch() {
  clearTimeout(searchTimer);
  searchText.value = "";
  props.store.search("", state.group);
  // A write in flight answers nothing, and a cleared box over a list that is
  // still filtered is worse than waiting: ask again once it is done.
  if (state.query) queueSearch("");
  searchInput.value?.focus();
}
onUnmounted(() => clearTimeout(searchTimer));
/** The window's Ctrl/Cmd+F lands here, the way it lands on the settings
 * search when the reader is on that page: a page that has a box of its own
 * should not send the chord to another page's. */
function focusSearch() {
  searchInput.value?.focus();
  searchInput.value?.select();
}
defineExpose({ focusSearch });

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
  if (name) {
    purgeGroup.value = false;
    groupDeleteDialog.value?.showModal();
  } else groupDeleteDialog.value?.close();
});
async function confirmDeleteGroup() {
  const name = groupToDelete.value;
  const purge = purgeGroup.value;
  const count = state.groupCounts[name] ?? 0;
  groupToDelete.value = "";
  if (!name) return;
  if (!await props.store.deleteGroup(name, purge)) return;
  say(purge
    ? t("已删除分组 {name} 及其中 {count} 条收藏", { name, count })
    : t("已删除分组 {name}", { name }));
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

function favoriteMenu(event: MouseEvent, item: FavoriteSummary) {
  if (state.selecting) return;
  openContextMenu(event, [
    { id: "edit", label: t("编辑收藏"), icon: Pencil, disabled: busy.value,
      run: () => editHere(() => props.store.open(item.id)) },
    { id: "copy", label: t("复制收藏"), icon: Copy, disabled: busy.value,
      run: () => props.store.copy(item.id) },
    null,
    { id: "delete", label: t("删除收藏"), icon: Trash2, danger: true, divider: true,
      disabled: busy.value, run: () => { deleting.value = item; } },
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
      <!-- A tablist rather than a row of buttons: the two panels are the same
           page, and a reader should hear "tab 1 of 2, selected" rather than two
           unrelated buttons.  The arrows move between them, so the strip is one
           stop in the tab order. -->
      <div class="page-tabs" role="tablist" :aria-label="t('收藏页分区')">
        <button v-for="section in sections" :key="section.id" type="button" role="tab"
          :id="`favorites-tab-${section.id}`" :data-tab-id="section.id"
          :aria-selected="tab === section.id" :aria-controls="`favorites-panel-${section.id}`"
          :tabindex="tab === section.id ? 0 : -1"
          :class="{ 'page-tab--active': tab === section.id }"
          :title="section.id === 'edit' && state.dirty ? t('编辑器里有未保存的修改') : undefined"
          @click="tab = section.id"
          @keydown="tabKeydown($event, FAVORITES_TABS, tab, (id) => { tab = id as FavoritesTab })">{{ section.label }}<span
            v-if="section.id === 'edit' && state.dirty" class="page-tab-mark" aria-hidden="true"> *</span></button>
      </div>

      <!-- Outside the panels: an error can come from either one — a refused
           group rename lands while the reader is on the library, and a refused
           save while they are on the editor. -->
      <div v-if="state.error" class="error-band" role="alert">
        <div>
          <strong>{{ state.error.message }}</strong>
          <ErrorDetail :code="state.error.code" :message="state.error.message" />
        </div>
      </div>

      <div v-if="tab === 'browse'" class="page-col" role="tabpanel" id="favorites-panel-browse" aria-labelledby="favorites-tab-browse">
        <div class="toolbar toolbar--inset">
          <label class="search"><Search :size="17" /><input ref="searchInput" :aria-label="t('搜索收藏')" :placeholder="t('搜索收藏')"
            :value="searchText" maxlength="512" :disabled="!props.enabled"
            @input="typeSearch(($event.target as HTMLInputElement).value)"
            @keydown.esc.prevent="clearSearch"
          /><button v-if="searchText" type="button" class="search-clear" :aria-label="t('清除搜索')" :title="t('清除搜索')" @click.prevent="clearSearch"><X :size="14" /></button></label>
          <button class="icon-button" :aria-label="t('刷新收藏')" :title="t('刷新收藏')" :disabled="busy || state.loading" @click="refreshNow"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
          <button v-if="!state.selecting" class="icon-button" :aria-label="t('批量选择收藏')" :title="t('批量选择收藏')" :disabled="busy || !state.items.length" @click="props.store.selectMode(true)"><ListChecks :size="18" /></button>
          <button :aria-label="t('新建收藏')" :disabled="busy" @click="editHere(store.create)"><Plus :size="18" />{{ t("新建") }}</button>
          <button class="icon-button" :aria-label="t('导出全部收藏')" :title="t('选择导出格式')" :disabled="busy || !state.libraryTotal" @click="exportMenu"><Download :size="18" /></button>
          <!-- Import has no disabled-on-empty-list logic: an empty library is
               exactly when a reader reaches for it. -->
          <button class="icon-button" :aria-label="t('导入收藏')" :title="t('从 ClipSync 导出的 JSON 文件导入收藏')" :disabled="busy" @click="importNow"><Upload :size="18" /></button>
        </div>
        <!-- The export lands in Downloads (or the data folder when there is
             none), and the path is the only thing that says where.  Reading it
             off the screen and retyping it is not a thing anyone can do, so it
             is one click to the clipboard — the same path, ready to paste into
             a file dialog or a terminal. -->
        <div v-if="exported" class="note export-line" role="status">
          <span>{{ t("已导出 {count} 条收藏 → {path}", { count: exported.count, path: exported.filepath }) }}</span>
          <button type="button" class="export-line__copy" :aria-label="exportCopied ? t('已复制路径') : t('复制导出路径')" @click="copyExportPath">
            <component :is="exportCopied ? Check : Copy" :size="14" />{{ exportCopied ? t("已复制路径") : t("复制路径") }}
          </button>
        </div>
        <!-- The selection bar.  A line of its own rather than a replaced
             toolbar: the search box stays usable, which matters because the
             reason to select twenty rows is usually a search that found them. -->
        <div v-if="state.selecting" class="note selection-bar" role="status">
          <span>{{ state.selected.length
            ? t("已选 {count} 条", { count: state.selected.length })
            : t("勾选要处理的收藏") }}</span>
          <button type="button" class="selection-bar__pick" @click="props.store.selectPage()">
            {{ allPicked ? t("取消全选") : t("全选本页") }}</button>
          <button type="button" class="selection-bar__delete danger" :disabled="busy || !state.selected.length"
            @click="askDeleteSelected">{{ t("删除所选") }}</button>
          <button type="button" class="selection-bar__exit" @click="props.store.selectMode(false)">{{ t("退出选择") }}</button>
        </div>
        <!-- The import's own line, for the same reason: what it did is a count
             of rows that are now in a list the reader is not looking at. -->
        <div v-if="imported" class="note export-line" role="status">
          <span>{{ importReport }}</span>
        </div>
        <div class="favorites-workspace card card--flush">
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
            <!-- Loading and empty are different states that used to share this
                 one box; the loading half gets a sentence and no button, and
                 the empty half says what a favourite is and how to make one. -->
            <EmptyState v-if="!state.items.length" :icon="Star"
              :title="state.loading ? t('正在读取收藏') : t('暂无收藏')"
              :description="state.loading ? '' : t('收藏是长期保存的片段：在历史记录里点星标存一条，或者在这里直接写一条。')"
              :action="state.loading ? '' : t('新建收藏')" :disabled="busy"
              @action="editHere(store.create)" />
            <article v-for="(item, index) in state.items" :key="item.id" class="favorite-row"
              :class="{ selected: state.selectedId === item.id, dragging: dragging === item.id, 'drag-over': dragOver === item.id,
                picking: state.selecting, picked: state.selected.includes(item.id) }"
              @dragover.prevent="dragOverRow(item)" @drop.prevent="drop(item)" @dragend="dragEnd"
              @contextmenu.prevent="favoriteMenu($event, item)"
              @click="state.selecting && props.store.toggleSelect(item.id)">
              <!-- In selection mode the handle gives its place to the tick, and
                   the row's own buttons go: a row that is being collected is not
                   a row being edited, copied or deleted one at a time, and three
                   disabled buttons would be three things to read for nothing. -->
              <label v-if="state.selecting" class="favorite-pick">
                <input type="checkbox" :checked="state.selected.includes(item.id)"
                  :aria-label="t('选择 {title}', { title: favoriteTitle(item) })"
                  @click.stop @change="props.store.toggleSelect(item.id)" />
              </label>
              <span v-else class="drag-handle" :draggable="!busy" :aria-hidden="true"
                @dragstart="dragStart($event, item)" @dragend="dragEnd"><GripVertical :size="15" /></span>
              <img v-if="item.image" class="favorite-thumb" :src="item.image" alt="" loading="lazy" />
              <span v-else-if="isImageFavorite(item)" class="favorite-thumb favorite-thumb--empty" aria-hidden="true">🖼</span>
              <div class="favorite-summary"><span class="favorite-row-title">{{ favoriteTitle(item) }}</span><p v-if="!isImageFavorite(item)">{{ previewText(item.preview) }}</p>
                <!-- The group, and only the group.  A zero-based position used to
                     follow it — the editor's own 位置 field, spelled out in the
                     middle of a list that already shows the order by being in it,
                     and reading as a mystery to anyone who had not opened the
                     editor.  Placement is what the handle and, on a touch screen,
                     the arrows are for. -->
                <span class="note">{{ item.group || t('未分组') }}<template v-if="item.created"> · {{ shortTime(item.created) }}</template></span></div>
              <div v-if="coarse && !state.selecting" class="row-step">
                <button class="icon-button" :aria-label="t('上移')" :title="t('上移')" :disabled="busy || index === 0" @click="step(index, -1)"><ChevronUp :size="16" /></button>
                <button class="icon-button" :aria-label="t('下移')" :title="t('下移')" :disabled="busy || index === state.items.length - 1" @click="step(index, 1)"><ChevronDown :size="16" /></button>
              </div>
              <div v-if="!state.selecting" class="row-actions">
                <button class="icon-button" :aria-label="t('编辑收藏 {title}', { title: favoriteTitle(item) })" :title="t('编辑收藏')" :disabled="busy" @click="editHere(() => props.store.open(item.id))"><Pencil :size="17" /></button>
                <button class="icon-button" :aria-label="t('复制收藏 {title}', { title: favoriteTitle(item) })" :title="t('复制收藏')" :disabled="busy" @click="props.store.copy(item.id)"><Check v-if="state.copiedId === item.id" :size="17" /><Copy v-else :size="17" /></button>
                <button class="icon-button" :aria-label="t('删除收藏 {title}', { title: favoriteTitle(item) })" :title="t('删除收藏')" :disabled="busy" @click="deleting = item"><Trash2 :size="17" /></button>
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

      <div v-else class="page-col" role="tabpanel" id="favorites-panel-edit" aria-labelledby="favorites-tab-edit">
        <!-- `save()` rather than `save`: the handler's argument would otherwise
             be the submit event, which is truthy, which is `force` — every
             form submit would skip the duplicate check. -->
        <form v-if="state.editorOpen" class="favorite-editor settings-section" :aria-label="t('收藏编辑器')" :aria-busy="state.detailLoading || state.pending" @submit.prevent="store.save()">
          <div class="editor-heading card-head"><h2>{{ state.selectedId ? t('编辑收藏') : t('新建收藏') }}{{ state.dirty ? ' *' : '' }}</h2>
            <button class="icon-button" type="button" :aria-label="t('关闭收藏编辑器')" :title="t('关闭编辑器')" :disabled="busy" @click="closeEditor"><X :size="18" /></button></div>
          <p v-if="state.detailLoading" role="status">{{ t("正在读取完整内容") }}</p>
          <p v-if="state.stale" role="status">{{ t("收藏已在其他位置更新。") }}</p>
          <!-- The save that matched a body already in the library comes back as
               a question rather than as a second copy or a refusal: the same
               snippet under two titles is a thing readers do on purpose, and a
               silent merge would take the second title away from them.  The
               title of the row it matched is in the sentence because "there is
               already one" is not something anyone can act on without knowing
               which. -->
          <div v-if="state.duplicate" class="note duplicate-line" role="status">
            <p>{{ t("收藏库里已有一条内容完全相同的收藏：{title}", { title: state.duplicate.title || t('未命名') }) }}</p>
            <p>{{ t("已存在相似条目，仍要新建？") }}</p>
            <div class="duplicate-line__actions">
              <button type="button" :disabled="busy" @click="openDuplicate">{{ t("打开那一条") }}</button>
              <button type="button" class="primary" :disabled="busy" @click="saveAnyway">{{ t("仍然新建") }}</button>
              <button type="button" :disabled="busy" @click="store.dismissDuplicate()">{{ t("再改改") }}</button>
            </div>
          </div>
          <button v-if="state.selectedId && (!state.loaded || state.stale)" type="button" :disabled="busy || state.detailLoading"
            @click="navigateEditor(() => store.open(state.selectedId!))"><RefreshCw :size="17" />{{ t("重新读取") }}</button>
          <fieldset :disabled="busy || state.detailLoading || !state.loaded">
            <label for="favorite-title">{{ t("标题") }}</label>
            <input id="favorite-title" :value="state.draft.title" maxlength="256" @input="store.edit({ title: ($event.target as HTMLInputElement).value })" />
            <label for="favorite-group">{{ t("分组") }}</label>
            <input id="favorite-group" :value="state.draft.group" maxlength="128" list="favorite-groups" @input="store.edit({ group: ($event.target as HTMLInputElement).value })" />
            <datalist id="favorite-groups"><option v-for="group in state.groups" :key="group" :value="group" /></datalist>
            <!-- The sort number, folded away.  It is a real field — the backend
                 orders by it and the drag handle writes it — but it is not one of
                 the three things a favourite is (title, group, content), and a
                 zero-based counter sitting between 分组 and 内容 read as a fourth
                 fact the reader had to decide about.  Placement is what the
                 handle and, on a touch screen, the arrows are for; the number is
                 what they write.  See U62. -->
            <details v-if="state.selectedId" class="editor-advanced">
              <summary>{{ t("高级") }}</summary>
              <p class="note">{{ t("位置是从 0 开始的排序号：数字小的排在前面。拖动列表里的手柄会改写它。") }}</p>
              <label for="favorite-position">{{ t("位置") }}</label>
              <input id="favorite-position" type="number" min="0" step="1" :value="state.draft.position" @input="store.edit({ position: ($event.target as HTMLInputElement).valueAsNumber })" />
            </details>
            <label for="favorite-content">{{ t("内容") }}</label>
            <textarea id="favorite-content" :value="state.draft.content" maxlength="65536" rows="14" @input="store.edit({ content: ($event.target as HTMLTextAreaElement).value })" />
            <button class="primary" type="submit" :disabled="!state.dirty"><Save :size="17" />{{ state.pending ? t('正在保存') : t('保存') }}</button>
          </fieldset>
        </form>
        <div v-else class="empty">
          <Pencil :size="30" />
          <h2>{{ t("没有正在编辑的收藏") }}</h2>
          <p class="note">{{ t("在收藏列表里点一条右边的铅笔来编辑它，或者在这里新建一条。") }}</p>
          <button :disabled="busy" @click="editHere(store.create)"><Plus :size="18" />{{ t("新建收藏") }}</button>
        </div>
      </div>
    </template>
    <dialog ref="deleteDialog" class="modal" aria-labelledby="favorite-delete-title" @close="deleting = null" @cancel="deleting = null">
      <h2 id="favorite-delete-title">{{ t("删除收藏？") }}</h2><p>{{ t("{title} 将被永久删除。", { title: deleting?.title || t('未命名') }) }}</p>
      <p v-if="state.error" class="modal-error small" role="alert" :title="state.error.code">{{ state.error.message }}</p>
      <div class="modal-actions"><button autofocus @click="deleting = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !deleting" @click="confirmDelete">{{ t("删除收藏") }}</button></div>
    </dialog>
    <dialog ref="groupDeleteDialog" class="modal" aria-labelledby="favorite-group-delete-title" @close="groupToDelete = ''" @cancel="groupToDelete = ''">
      <h2 id="favorite-group-delete-title">{{ t("删除分组？") }}</h2>
      <!-- Two sentences, not one with a checkbox under it: what the dialog is
           about to do is the thing that changes when the tick goes on, and a
           promise that the favourites are kept cannot stay on screen while
           they are being deleted. -->
      <p v-if="!purgeGroup">{{ t("分组 {group} 中的 {count} 条收藏会移到未分组，收藏本身不会被删除。", { group: groupToDelete, count: state.groupCounts[groupToDelete] ?? 0 }) }}</p>
      <p v-else class="danger-text">{{ t("分组 {group} 和其中的 {count} 条收藏都会被删除，此操作不可撤销。", { group: groupToDelete, count: state.groupCounts[groupToDelete] ?? 0 }) }}</p>
      <label class="modal-check">
        <input type="checkbox" v-model="purgeGroup" :disabled="busy || !(state.groupCounts[groupToDelete] ?? 0)">
        <span>{{ t("连同其中的 {count} 条收藏一起删除", { count: state.groupCounts[groupToDelete] ?? 0 }) }}</span>
      </label>
      <div class="modal-actions"><button autofocus @click="groupToDelete = ''">{{ t("取消") }}</button><button class="danger" :disabled="busy" @click="confirmDeleteGroup">{{ purgeGroup ? t("删除分组和收藏") : t("删除分组") }}</button></div>
    </dialog>
    <dialog ref="batchDeleteDialog" class="modal" aria-labelledby="favorite-batch-delete-title">
      <h2 id="favorite-batch-delete-title">{{ t("删除所选的 {count} 条收藏？", { count: state.selected.length }) }}</h2>
      <p>{{ t("这 {count} 条收藏将被永久删除。", { count: state.selected.length }) }}</p>
      <div class="modal-actions"><button autofocus @click="batchDeleteDialog?.close()">{{ t("取消") }}</button><button class="danger" :disabled="busy" @click="confirmDeleteSelected">{{ t("删除收藏") }}</button></div>
    </dialog>
    <dialog ref="discardDialog" class="modal" aria-labelledby="favorite-discard-title" @close="nextEditor = null" @cancel="nextEditor = null">
      <h2 id="favorite-discard-title">{{ t("放弃未保存的修改？") }}</h2><p>{{ t("当前收藏的修改尚未保存。") }}</p>
      <div class="modal-actions"><button autofocus @click="discardDialog?.close()">{{ t("继续编辑") }}</button><button class="danger" :disabled="busy" @click="discard">{{ t("放弃修改") }}</button></div>
    </dialog>
  </section>
</template>
