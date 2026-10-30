<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { ArrowDownUp, RefreshCw, Search, Star, X } from "@lucide/vue";
import { bridge } from "../api/bridge";
import type { FavoriteSummary, HistoryItem, HistoryPreview, QuickPasteChooseResult } from "../api/types";
import { setLocale, t } from "../i18n";
import { shortTime, size } from "../i18n/format";
import { previewLabel, typeLabel } from "../lib/history-label";
import {
  errorCode, errorMessage, followPasteFetch, isPasteConfirmRequired, isRemoteFileRow,
  pasteFailureText, pastePrepareErrorText, type PasteFetchProgress, type PasteFetchRun,
} from "../lib/paste-transfer";
import {
  filterPasteRows, pasteDeviceChoices, type PasteDeviceChoice, type PasteDeviceFilter,
  type PasteSort, type PasteTimeFilter,
} from "../lib/quick-paste-filters";
import { applyAppearanceTheme } from "../lib/theme";

/**
 * The quick-paste overlay.
 *
 * A frameless window the host owns — label `quick-paste`, created once and
 * shown/hidden for each press — and the only thing on it is "take one of the
 * last clips and put it where the caret is".  That makes it a list with a
 * search box and a keyboard, not a second history page: it shows a page of rows,
 * never a row's own actions, and Enter is the whole of what it does.
 *
 * One kind of row can take longer than a keypress: a file that lives on another
 * device is not downloaded when its offer arrives, so pasting it starts a
 * transfer (1.3.1, see `lib/paste-transfer.ts`).  The overlay is where that has
 * to be visible — it is the surface the reader asked from — so Enter on such a
 * row turns the list into the fetch's progress panel and back: a large file asks
 * first, a running fetch can be cancelled, a failed one says why and offers to
 * try again.
 *
 * It is a window of its own for the same reason the history page is a component:
 * the overlay is filled and dismissed around a key press, and it must not share
 * a render scope (or the i18n bootstrap, or a page's in-flight reads) with the
 * main window.  That is also why it reads its own settings through
 * `quickPasteSettings` and applies the locale and the theme itself: the window
 * it runs in has no `App.vue` to have done that.  And because the host reuses
 * the window, the page has to put itself back to a fresh search on every
 * showing — see `onWindowFocus`.
 */

/** How many rows the overlay asks for.  More than fits (the window is 480–560
 *  px high and a row is ~40 px), because a wheel or an arrow key should reveal
 *  rows already read rather than start a read per scroll; fewer than the
 *  history page's page size, because this is a palette and not a browse. */
const LIMIT = 20;
/** What it asks for while a time or device filter is on.  The host sends one
 *  page and the panel narrows it here, so the page has to be wide enough that
 *  "today" is more than the first few rows of it.  100 is the host's own cap. */
const FILTER_LIMIT = 100;

/** How long the box waits after a keystroke.  The shell's search boxes wait
 *  180 ms (`SEARCH_DEBOUNCE_MS`); the overlay waits 120 ms because it is the
 *  surface a reader opens *while typing somewhere else* — the gesture is one
 *  word and a Return, and the list it asks for is the local sidecar's. */
const DEBOUNCE_MS = 120;

/** A clipboard row on one line: whitespace collapsed and cut at a column count
 *  rather than a byte count, so a Chinese preview is not sliced mid-character. */
function oneLine(raw: string): string {
  const flat = String(raw || "").replace(/\s+/g, " ").trim();
  return flat.length > 120 ? `${flat.slice(0, 120)}…` : flat;
}

/** The name of the device a remote-file row came from, as the row marks it. */
function remoteSource(row: HistoryItem): string {
  return row.source_name || row.source_app || t("未知");
}

/** One on-demand paste, from the moment its row was chosen to the moment it is
 *  pasted or given up on.  It is one object rather than a handful of refs
 *  because every field belongs to the same attempt: a retry replaces it whole,
 *  and a poll that lands after a newer attempt is dropped by identity. */
interface PasteJob {
  entryId: string;
  item: HistoryItem;
  phase: "progress" | "failed";
  progress: PasteFetchProgress | null;
  error: string;
  /** What 重试 retries: the fetch, or the paste itself when the files already
   *  arrived and only the final copy/paste failed. */
  retryKind: "fetch" | "choose";
}

const searchInput = ref<HTMLInputElement | null>(null);
const listEl = ref<HTMLElement | null>(null);
const query = ref("");
const kind = ref("all");
/** The host's order.  It is passed to `quick_paste_list` because the list is one
 *  page: sorting a page that was already cut cannot show older rows. */
const sort = ref<PasteSort>("newest");
/** The two client-side filters: "today" is a calendar question and a device is
 *  a property of the rows, neither of which the history query takes. */
const timeFilter = ref<PasteTimeFilter>("all");
const deviceFilter = ref<PasteDeviceFilter>("all");
const rows = ref<HistoryItem[]>([]);
const loading = ref(false);
/** Why the list could not be read, in the host's words when it has any. */
const failed = ref("");
/** Image rows carry the same downscaled picture the history page shows: in the
 *  list as a thumbnail, and on the pointer as a larger hover card.  Keyed by
 *  entry id and fetched only for the visible page; a failed read leaves the kind
 *  glyph in place. */
const rowPreviews = ref<Record<string, HistoryPreview>>({});
const thumbPending = new Set<string>();
const thumbFailed = new Set<string>();
function isImageItem(item: HistoryItem) {
  const kind = (item.content_type || "").toUpperCase();
  return kind === "IMAGE_PNG" || kind === "IMAGE" || kind === "IMAGE_EMF";
}
function thumbFor(item: HistoryItem): string {
  return rowPreviews.value[item.id]?.image || "";
}
async function loadRowThumb(item: HistoryItem) {
  if (rowPreviews.value[item.id] || thumbPending.has(item.id) || thumbFailed.has(item.id)) return;
  thumbPending.add(item.id);
  try {
    const card = await bridge.previewHistoryEntry(item.id);
    if (card.kind === "image" && card.image) {
      rowPreviews.value = { ...rowPreviews.value, [item.id]: card };
    } else {
      thumbFailed.add(item.id);
    }
  } catch {
    thumbFailed.add(item.id);
  } finally {
    thumbPending.delete(item.id);
  }
}
function loadRowThumbs(items: HistoryItem[]) {
  for (const item of items) {
    if (isImageItem(item)) void loadRowThumb(item);
  }
}
/** The enlarged picture the pointer is resting on.
 *
 *  The history page opens a card under the row for the same reason: a 40px
 *  thumbnail says "there is a picture" and nothing more, while the card says
 *  what it is before the reader pastes it.  The list is short and the pointer
 *  is already on the row, so the card follows the pointer instead of taking a
 *  column away from the list.  A short delay keeps a sweep down the list from
 *  flashing a card on every image it crosses. */
const HOVER_PREVIEW_DELAY_MS = 160;
const hoverItem = ref<HistoryItem | null>(null);
const hoverPoint = ref({ x: 0, y: 0 });
let hoverTimer: ReturnType<typeof setTimeout> | undefined;
const hoverCard = computed<HistoryPreview | null>(() => {
  const item = hoverItem.value;
  if (!item) return null;
  const card = rowPreviews.value[item.id];
  return card && card.kind === "image" && card.image ? card : null;
});
const hoverStyle = computed(() => {
  const card = hoverCard.value;
  if (!card) return {};
  const boxWidth = Math.min(card.width || 320, 340) + 24;
  const boxHeight = Math.min(card.height || 240, 240) + 42;
  const left = Math.min(hoverPoint.value.x + 16, window.innerWidth - boxWidth - 8);
  const top = Math.min(hoverPoint.value.y + 16, window.innerHeight - boxHeight - 8);
  return { left: `${Math.max(8, left)}px`, top: `${Math.max(8, top)}px` };
});
function cancelHover() {
  clearTimeout(hoverTimer);
  hoverItem.value = null;
}
function onRowEnter(item: HistoryItem, event: MouseEvent) {
  cancelHover();
  if (!isImageItem(item) || !thumbFor(item)) return;
  hoverPoint.value = { x: event.clientX, y: event.clientY };
  hoverTimer = setTimeout(() => {
    hoverItem.value = item;
  }, HOVER_PREVIEW_DELAY_MS);
}
function onRowMove(event: MouseEvent) {
  if (!hoverItem.value) return;
  hoverPoint.value = { x: event.clientX, y: event.clientY };
}
/** Why the last pick did not land: the clipboard holds the clip, the paste into
 *  the focused application did not.  The overlay stays up to say so. */
const pasteError = ref("");
/** A pick is in flight.  Enter is the only way to make one, and holding it down
 *  is one clipboard write followed by nine more — so the flag is the lock, not
 *  just the spinner. */
const busy = ref(false);
/** Which row Enter would take, and which row the pointer last crossed. */
const selected = ref(0);
/** The fetch this overlay is watching, or null when the list is what is drawn. */
const job = ref<PasteJob | null>(null);
/** The remote-file row waiting for the reader's answer to "this is large". */
const confirmRow = ref<HistoryItem | null>(null);
/** The size behind that question, when the row's offer could be read: the
 *  host's refusal carries no byte count (`PASTE_CONFIRM_REQUIRED` drops the
 *  sidecar's `details` on the way through Tauri), so the preview read is the
 *  only source.  A folder or an unreadable preview leaves it 0 and the panel
 *  asks without a number. */
const confirmSize = ref(0);
let fetchRun: PasteFetchRun | undefined;
/** Whether the first read has landed.  Until it has, the window's opening focus
 *  is not a re-opening: `initialise` is already loading the list. */
let initialised = false;
/** When the last pick was refused, for the reason `onWindowFocus` skips it. */
let pasteFailedAt = 0;
/** A slow answer that lands after a newer one must not paint over it; the same
 *  counter drops the answer a reset invalidated. */
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
/** The delayed action of a row click, cleared by a keyboard choice or a close. */
let rowClickTimer: ReturnType<typeof setTimeout> | undefined;

/** The five kinds, in the history page's chip order and with its ids: the host
 *  filters `quick_paste_list` by the same words `list_history` takes, and a
 *  palette that spelled them differently would be a second vocabulary. */
const kindChips = computed<Array<{ id: string; label: string }>>(() => [
  { id: "all", label: t("全部") },
  { id: "text", label: t("文本") },
  { id: "image", label: t("图片") },
  { id: "file", label: t("文件") },
  { id: "link", label: t("链接") },
]);

const term = computed(() => query.value.trim());

/** 收藏 mode: the saved library instead of the newest clips.  The rows are
 *  mapped onto the same shape the list already draws — a favourite has a title
 *  where a history row has its preview, and an image favourite carries the
 *  picture the library already downscaled — so the cursor, Enter, the hover card
 *  and the empty state all keep working without a second list. */
const favoritesMode = ref(false);
const favoriteIds = new Map<string, string>();
function favoriteToRow(favorite: FavoriteSummary): HistoryItem {
  const id = `fav:${favorite.id}`;
  favoriteIds.set(id, favorite.id);
  if (favorite.image) {
    rowPreviews.value = {
      ...rowPreviews.value,
      [id]: { kind: "image", image: favorite.image, width: 0, height: 0, files: [], total: 0 },
    };
  }
  return {
    id,
    timestamp: Math.round((favorite.updated || favorite.created || 0) * 1000),
    preview: favorite.title || favorite.preview,
    content_type: favorite.image ? "IMAGE_PNG" : "TEXT",
    pinned: false,
    source_name: favorite.group || "",
    source_app: "",
    source_title: "",
    paste_count: 0,
  };
}

function toggleFavorites() {
  if (busy.value || job.value) return;
  favoritesMode.value = !favoritesMode.value;
  if (favoritesMode.value) {
    // The history filters mean nothing to a library that has its own groups.
    kind.value = "all";
    timeFilter.value = "all";
    deviceFilter.value = "all";
    sort.value = "newest";
  }
  cancelHover();
  focusSearch();
  void searchNow();
}

/** The rows on screen: the host's page, narrowed by the two client filters.
 *  Everything below (cursor, Enter, double click, empty state) reads this and
 *  not `rows`, so the row under the cursor is always the row that is drawn. */
const visibleRows = computed(() => (
  favoritesMode.value ? rows.value : filterPasteRows(rows.value, {
    time: timeFilter.value,
    device: deviceFilter.value,
  })
));

/** The host page the search asks for.  A filter gets a wider page because it is
 *  narrowing that page here; no filter gets the palette's usual twenty. */
const fetchLimit = computed(() => (
  timeFilter.value !== "all" || deviceFilter.value !== "all" ? FILTER_LIMIT : LIMIT
));

/** The four time windows, in widening order.  Ids and labels only; the clock
 *  comparison lives in `lib/quick-paste-filters.ts` so it can be tested. */
const timeChips = computed<Array<{ id: PasteTimeFilter; label: string }>>(() => [
  { id: "all", label: t("全部") },
  { id: "hour", label: t("最近 1 小时") },
  { id: "today", label: t("今天") },
  { id: "week", label: t("最近 7 天") },
]);

/** The device options the current result set can offer.  Rebuilt from the host's
 *  page rather than the filtered one, so narrowing the time window does not drop
 *  the device a row came from out of the list. */
const deviceChoices = computed<PasteDeviceChoice[]>(() => pasteDeviceChoices(rows.value));

/** The device filter's one-word form for its option. */
function deviceChoiceLabel(choice: PasteDeviceChoice): string {
  if (choice.id === "all") return t("全部设备");
  if (choice.local) return t("本机");
  return choice.name;
}

/** Flip newest/oldest.  A re-read rather than a reversal of the rows already
 *  here: the oldest rows of the whole history are the point of the choice. */
function toggleSort() {
  if (busy.value || job.value) return;
  sort.value = sort.value === "newest" ? "oldest" : "newest";
  focusSearch();
  void searchNow();
}

/** The row's kind as a glyph: the history page's chip glyphs, because they are
 *  the same five words and the eye has already learned them there. */
function kindGlyph(row: HistoryItem): string {
  const kindName = String(row.content_type || "").toUpperCase();
  if (kindName === "URL" || kindName === "LINK") return "🔗";
  if (kindName === "FILE" || kindName === "FILE_REMOTE") return "📄";
  if (kindName.startsWith("IMAGE")) return "🖼";
  return "📝";
}

/** What the row's second line says: its kind, where it came from, and when. */
function rowMeta(row: HistoryItem): string {
  return [typeLabel(row), row.source_name, shortTime(row.timestamp)]
    .filter(Boolean).join(" · ");
}

/** Nothing to show, said in the reader's own terms: a query that missed is not
 *  an empty history, and a type, time or device filter that emptied the list is
 *  neither — the sentence names which of them did it. */
const filtersActive = computed(() => timeFilter.value !== "all" || deviceFilter.value !== "all");
const emptyTitle = computed(() => {
  if (term.value) return t("没有找到“{query}”", { query: term.value });
  if (favoritesMode.value) return t("收藏里还没有内容");
  if (filtersActive.value) return t("没有符合当前筛选条件的记录");
  if (kind.value !== "all") {
    const label = kindChips.value.find((chip) => chip.id === kind.value)?.label || kind.value;
    return t("还没有{type}", { type: label });
  }
  return t("还没有可粘贴的记录");
});
const emptyHint = computed(() => (
  favoritesMode.value
    ? t("在收藏库中添加内容后，它会出现在这里。")
    : (term.value || kind.value !== "all" || filtersActive.value)
      ? t("换个关键词或筛选条件试试。")
      : t("复制内容后，它会出现在这里。")
));

/* ── The transfer panel ───────────────────────────────────────────────── */

const transferTitle = computed(() => (job.value?.phase === "failed"
  ? t("没能取回文件")
  : t("正在从另一台设备取回文件…")));
/** The row's name while the list has not answered yet, the transfer's own name
 *  once it has.  A paste of several files names the first and says how many. */
const transferName = computed(() => {
  const current = job.value;
  if (!current) return "";
  const progress = current.progress;
  if (progress?.filename) {
    return progress.files > 1
      ? t("{name} 等 {count} 个文件", { name: progress.filename, count: progress.files })
      : progress.filename;
  }
  return oneLine(previewLabel(current.item));
});
const transferPercent = computed(() => Math.min(100, Math.max(0, job.value?.progress?.progress || 0)));
const transferSpeed = computed(() => {
  const speed = job.value?.progress?.speed || 0;
  return speed > 0 ? t("{rate}/秒", { rate: size(speed) }) : "";
});
const transferEta = computed(() => job.value?.progress?.eta || "");
const transferFiles = computed(() => job.value?.progress?.files || 0);
const transferFailed = computed(() => job.value?.phase === "failed");

/* ── The reads ────────────────────────────────────────────────────────── */

/** Read the list for the current query and filter.  Every answer carries its
 *  generation: a word typed while the previous read was in flight must win, and
 *  an empty box is a read too (the newest rows), not "no query". */
async function searchNow() {
  clearTimeout(timer);
  const mine = ++generation;
  loading.value = true;
  failed.value = "";
  pasteError.value = "";
  try {
    let items: HistoryItem[];
    if (favoritesMode.value) {
      const page = await bridge.quickPasteFavorites(term.value, fetchLimit.value);
      if (mine !== generation) return;
      favoriteIds.clear();
      items = page.items.map(favoriteToRow);
    } else {
      const page = await bridge.quickPasteList(term.value, kind.value, fetchLimit.value, sort.value);
      if (mine !== generation) return;
      items = page.items;
      loadRowThumbs(items);
    }
    rows.value = items;
    selected.value = 0;
  } catch (error) {
    if (mine !== generation) return;
    rows.value = [];
    failed.value = errorMessage(error) || t("读取剪贴板历史失败");
  } finally {
    if (mine === generation) loading.value = false;
  }
}

/** A word, not a keystroke: the box is typed into with an IME, and each
 *  composition event is not a query anyone asked for.  A filter chip is a
 *  decision rather than typing, so it searches at once. */
function scheduleSearch() {
  clearTimeout(timer);
  timer = setTimeout(() => void searchNow(), DEBOUNCE_MS);
}

function pickKind(id: string) {
  if (kind.value === id || busy.value || job.value) return;
  kind.value = id;
  focusSearch();
  void searchNow();
}

/** The time chip a pointer picked.  Client-side, but the page under it may have
 *  to be re-read wider (see `fetchLimit`), and the caret belongs back in the
 *  box either way. */
function pickTime(id: PasteTimeFilter) {
  if (busy.value || job.value) return;
  timeFilter.value = id;
  focusSearch();
}

/** The device picker changed.  Same caret rule as the chips. */
function pickDevice() {
  focusSearch();
}

function clearQuery() {
  query.value = "";
  void nextTick(() => searchInput.value?.focus());
}

/** Keep the search box the window's keyboard focus while the pointer and the
 *  controls move under the reader.  The box is focused when the overlay opens
 *  and the gesture is "type a word, press Enter"; a filter chip that took the
 *  focus would end the word at the first click.  The chip still takes its click
 *  — this only puts the caret back where the next keystroke belongs. */
function focusSearch() {
  void nextTick(() => searchInput.value?.focus());
}

/** Move the keyboard cursor, wrapping at both ends.  A palette with twenty rows
 *  is a list you cycle rather than a form you tab through. */
function move(delta: number) {
  const count = visibleRows.value.length;
  if (!count) return;
  selected.value = (selected.value + delta + count) % count;
}

/* ── Choosing a row ───────────────────────────────────────────────────── */

/** A click is a choice, not only a cursor move.
 *
 *  The action waits one double-click interval: a reader whose hand still
 *  double-clicks would otherwise paste on the first click and land the second
 *  one in whatever application the closed overlay revealed.  A single click
 *  pays the same short pause, and both gestures end as exactly one paste.
 */
function pickOnClick(index: number) {
  if (busy.value || job.value || confirmRow.value) return;
  selected.value = index;
  cancelHover();
  clearTimeout(rowClickTimer);
  rowClickTimer = setTimeout(() => { void pick(index, false); }, 180);
}

/** Take the row at `index`.  A local clip is one host call — copy, hide, paste.
 *  A remote file is a fetch first: its bytes are still on the other machine, so
 *  the list becomes that fetch's progress panel. */
function pick(index: number, plain: boolean) {
  if (busy.value || job.value || confirmRow.value) return;
  const item = visibleRows.value[index];
  if (!item) return;
  if (plain || !isRemoteFileRow(item)) {
    void choose(item, plain);
    return;
  }
  void startFetch(item);
}

/** The one host call for a clip that is already here: copy it, hide this window,
 *  hand the focus back and type the paste chord.  The overlay is opened to paste,
 *  so that is unconditional — the direct `paste_N` shortcuts are the ones the
 *  自动粘贴 switch governs.  On failure the clipboard write already happened, so
 *  the sentence says so and the overlay stays up to show it. */
async function choose(item: HistoryItem, plain: boolean) {
  if (busy.value) return;
  busy.value = true;
  pasteError.value = "";
  try {
    await pasteChosen(item.id, plain);
  } finally {
    busy.value = false;
  }
}

/** Ask the host to copy one entry and paste it; report what it said. */
async function pasteChosen(entryId: string, plain: boolean) {
  try {
    const favoriteId = favoritesMode.value ? favoriteIds.get(entryId) : undefined;
    const result: QuickPasteChooseResult = favoriteId
      ? await bridge.quickPasteChooseFavorite(favoriteId)
      : await bridge.quickPasteChoose(entryId, plain);
    if (result && result.ok === false) {
      pasteError.value = result.message || t("已复制，但没能粘贴到当前应用");
      // The host re-shows this window to say that, and the focus it gives back
      // is not the reader re-opening the overlay — see `onWindowFocus`.
      pasteFailedAt = Date.now();
    }
  } catch (error) {
    pasteError.value = errorMessage(error) || t("粘贴失败，请重试");
  }
}

/** Start the fetch behind a remote-file row, then follow it.
 *
 * The sidecar's `PASTE_CONFIRM_REQUIRED` is its "this is large, ask first"
 * question rather than a failure, so it opens the confirmation panel and the
 * answer calls this again with `confirmed: true`; the other refusals all happen
 * before anything moves and are said where the list is, so the reader can pick
 * another row. */
async function startFetch(item: HistoryItem, confirmed = false) {
  if (busy.value || job.value) return;
  busy.value = true;
  pasteError.value = "";
  try {
    await bridge.preparePasteEntry(item.id, confirmed);
  } catch (error) {
    busy.value = false;
    if (isPasteConfirmRequired(error)) {
      await askConfirm(item);
      return;
    }
    pasteError.value = pastePrepareErrorText(errorCode(error), errorMessage(error));
    return;
  }
  busy.value = false;
  beginJob(item);
}

/** Ask the large-file question, with the offer's size when it can be read.
 *  `preview_history_entry` is a main-window command on some hosts; a refused or
 *  empty preview simply leaves the question general. */
async function askConfirm(item: HistoryItem) {
  confirmRow.value = item;
  confirmSize.value = 0;
  try {
    const preview = await bridge.previewHistoryEntry(item.id);
    const files = preview?.files || [];
    const total = files.reduce((sum, file) => sum + (Number(file.size) || 0), 0);
    if (!files.some((file) => file.kind === "dir") && total > 0) confirmSize.value = total;
  } catch { /* the general wording is enough */ }
}

function beginJob(item: HistoryItem) {
  // The reactive object is the one job identity: the callbacks below compare
  // `job.value` with it to drop a poll or an event belonging to a newer attempt,
  // and a `ref` hands back the same proxy on every read.
  job.value = {
    entryId: item.id, item, phase: "progress", progress: null, error: "", retryKind: "fetch",
  };
  followJob(job.value);
}

/** Watch one job's fetch.  Both the 400 ms transfer poll and the forwarded sidecar
 *  events run; the events are the only terminal signal. */
function followJob(current: PasteJob) {
  fetchRun?.stop();
  fetchRun = followPasteFetch(current.entryId, {
    onProgress: (progress) => {
      if (job.value === current) current.progress = progress;
    },
    onReady: (paths) => { void finishJob(current, paths); },
    onFailed: (reason) => {
      if (job.value !== current) return;
      fetchRun = undefined;
      current.phase = "failed";
      current.error = pasteFailureText(reason);
      current.retryKind = "fetch";
    },
  });
}

/** The files are here and the row is local: the same host call a local clip takes
 *  (copy, hide, paste).  A failure leaves the panel up with a retry of the paste
 *  itself — the bytes are on this machine now. */
async function finishJob(current: PasteJob, _paths: string[]) {
  if (job.value !== current) return;
  fetchRun = undefined;
  try {
    const result = await bridge.quickPasteChoose(current.entryId, false);
    if (result && result.ok === false) {
      current.phase = "failed";
      current.error = result.message || t("已复制，但没能粘贴到当前应用");
      current.retryKind = "choose";
      pasteFailedAt = Date.now();
      return;
    }
    // Success: the host hid this window on the way out.  The panel goes with it;
    // the next showing resets the list (see `onWindowFocus`).
    job.value = null;
  } catch (error) {
    current.phase = "failed";
    current.error = errorMessage(error) || t("粘贴失败，请重试");
    current.retryKind = "choose";
  }
}

/** Try the whole pick again.  A failed fetch starts a new one; a failed final
 *  paste only repeats that call. */
function retryJob() {
  const current = job.value;
  if (!current) return;
  job.value = null;
  if (current.retryKind === "choose") {
    void choose(current.item, false);
    return;
  }
  void startFetch(current.item, true);
}

/** Abandon the running fetch and go back to the list. */
async function cancelJob() {
  const current = job.value;
  if (!current) return;
  const running = fetchRun;
  job.value = null;
  fetchRun = undefined;
  if (running) await running.cancel();
}

/** The confirmation panel's two answers. */
async function confirmLarge() {
  const item = confirmRow.value;
  if (!item) return;
  confirmRow.value = null;
  await startFetch(item, true);
}
function cancelConfirm() {
  confirmRow.value = null;
}

/** Hide the overlay, and abandon any fetch it was following.
 *
 * The host answers with the hidden window; a host that has already hidden it (a
 * second Esc, the blur of a successful pick) is not an error to report.  The
 * cancel is awaited before the close: the sidecar deletes the files a
 * half-finished paste already landed, and doing that after the window is gone
 * would leave a fetch nobody is watching.  A close during a fetch is therefore
 * both a dismissal and a cancel — the reader looking at a progress panel and
 * pressing Esc is saying "stop this", not "hide it and keep downloading". */
async function close(restoreMain = false) {
  clearTimeout(timer);
  clearTimeout(rowClickTimer);
  cancelHover();
  const running = fetchRun;
  job.value = null;
  confirmRow.value = null;
  fetchRun = undefined;
  if (running) { try { await running.cancel(); } catch { /* already stopped */ } }
  try { await bridge.quickPasteClose(restoreMain); } catch { /* the window is already gone */ }
}

/** Esc, ↑ ↓ and Enter, listened for on the window rather than on the box: the
 *  pointer can have moved the focus to a row, and the next key still belongs to
 *  the overlay. */
function onKeydown(event: KeyboardEvent) {
  // An IME's Enter confirms a candidate and its arrows walk that candidate
  // list; none of those presses is meant for the overlay.
  if (event.isComposing) return;
  // Esc hides the window in every state.  A close button that works but an Esc
  // that only half-works is the stuck-window report the overlay opened with.
  if (event.key === "Escape") {
    event.preventDefault();
    void close(true);
    return;
  }
  if (confirmRow.value) {
    if (event.key === "Enter") { event.preventDefault(); void confirmLarge(); }
    return;
  }
  // A fetch owns the keyboard: every key but Esc is ignored, and holding Enter
  // must not start a second paste over the panel.
  if (job.value || busy.value) return;
  if (event.key === "ArrowDown") { clearTimeout(rowClickTimer); event.preventDefault(); move(1); return; }
  if (event.key === "ArrowUp") { clearTimeout(rowClickTimer); event.preventDefault(); move(-1); return; }
  if (event.key === "Enter") {
    clearTimeout(rowClickTimer);
    event.preventDefault();
    pick(selected.value, event.shiftKey);
  }
}

/** Losing the focus is the other way out — the reader clicked back into the
 *  application behind the overlay — and it hides the window from every state,
 *  the progress panel included: a panel that refuses to go away is the bug, and
 *  a fetch the reader turned away from is cancelled on the way out (see
 *  `close`). */
function onWindowBlur() {
  void close();
}

/** Put the overlay back to a fresh search.
 *
 * The host creates this window once and shows/hides it for every press, so the
 * page inside outlives each opening.  Without a reset the second opening would
 * draw the last query and the last list — and the reader who just copied
 * something and reached for Ctrl+Shift+V would have to clear the old word
 * before the new clip could be found.
 */
async function resetForOpen() {
  clearTimeout(rowClickTimer);
  cancelHover();
  favoritesMode.value = false;
  favoriteIds.clear();
  query.value = "";
  kind.value = "all";
  sort.value = "newest";
  timeFilter.value = "all";
  deviceFilter.value = "all";
  pasteError.value = "";
  job.value = null;
  confirmRow.value = null;
  clearTimeout(timer);
  await searchNow();
  await nextTick();
  searchInput.value?.focus();
}

/** The window was given the focus.
 *
 * Two cases wear the same event: the host re-showing the overlay for a new
 * press, which is a reset, and the host re-showing it because a paste failed,
 * where the error sentence *is* the news and a reset would wipe it.  The failed
 * pick's own focus arrives within a moment of the answer — that is what
 * `pasteFailedAt` marks — and a reader pressing the shortcut again that fast is
 * better served by the list still being there than by a lost sentence.
 */
function onWindowFocus() {
  if (!initialised || busy.value || job.value || confirmRow.value) return;
  if (Date.now() - pasteFailedAt < 1500) return;
  void resetForOpen();
}

/** Read the settings the host answers the overlay with, then the list.
 *
 * The language and the theme are applied before anything but the defaults is
 * painted, which is why this runs before `searchNow`: a window that listed rows
 * in Chinese for a beat and only then switched to English was a flash of the
 * wrong language, and a light window on a reader who chose a dark theme is the
 * same flash in another form.
 *
 * `quick_paste_settings` also carries `auto_paste_enabled`, which is not
 * consumed: that switch governs the hotkey path, not this window — opening the
 * overlay *is* asking for a paste, so Enter keeps its one meaning and the footer
 * does not read a switch the overlay cannot set.
 *
 * A host that cannot answer the settings call is not an error worth an overlay:
 * the list is still readable, and the failure of the list read itself is what
 * gets said if that is the sidecar being down. */
async function initialise() {
  try {
    const settings = await bridge.quickPasteSettings();
    setLocale(settings?.language);
    // The overlay is a second document, so it does not inherit the main
    // window's `data-theme`; the shared helper applies the stored choice —
    // `system` included — to this one.
    applyAppearanceTheme(settings?.appearance_mode);
  } catch { /* the default language and the OS theme are readable fallbacks */ }
  await searchNow();
  await nextTick();
  searchInput.value?.focus();
  initialised = true;
}

watch(query, scheduleSearch);
// A filter change re-arms the cursor at the top; crossing into/out of the
// filtered mode also re-reads, because the page it narrows grows from 20 to 100
// rows (see `fetchLimit`).  Between two filtered devices there is nothing to
// re-read: the page is already the wider one.
watch([timeFilter, deviceFilter], () => { selected.value = 0; });
watch(fetchLimit, () => { if (!busy.value && !job.value) void searchNow(); });
// The rows decide which devices the filter can offer; a selected device that the
// new page does not contain (it was cleared from history, or the query now
// matches nothing from it) falls back to 全部设备 rather than showing an empty
// list with no way back.
watch(deviceChoices, (choices) => {
  if (!choices.some((choice) => choice.id === deviceFilter.value)) deviceFilter.value = "all";
});
watch(selected, () => {
  void nextTick(() => {
    const row = listEl.value?.children?.[selected.value] as HTMLElement | undefined;
    // jsdom has no scrollIntoView; the browser the window really runs in does.
    row?.scrollIntoView?.({ block: "nearest" });
  });
});

onMounted(() => {
  window.addEventListener("keydown", onKeydown);
  window.addEventListener("blur", onWindowBlur);
  window.addEventListener("focus", onWindowFocus);
  void initialise();
});
onUnmounted(() => {
  clearTimeout(timer);
  clearTimeout(rowClickTimer);
  fetchRun?.stop();
  window.removeEventListener("keydown", onKeydown);
  window.removeEventListener("blur", onWindowBlur);
  window.removeEventListener("focus", onWindowFocus);
});
</script>

<template>
  <div class="quick-paste" :class="{ 'quick-paste--busy': busy, 'quick-paste--transfer': !!job }"
    role="dialog" :aria-label="t('粘贴面板')" :aria-busy="busy || !!job">
    <!-- A remote file's paste is a fetch: the list becomes the panel that shows
         it, and the only ways out are cancel and, once it has failed, retry or
         close. -->
    <div v-if="job" class="quick-paste-transfer">
      <h2 class="quick-paste-transfer-title" role="status">
        <RefreshCw v-if="!transferFailed" class="spinning" :size="17" aria-hidden="true" />
        {{ transferTitle }}
      </h2>
      <p class="quick-paste-transfer-name">{{ transferName }}</p>
      <progress class="quick-paste-progress" :value="transferPercent" max="100" aria-hidden="true"></progress>
      <p class="quick-paste-transfer-stats">
        <span class="quick-paste-transfer-percent">{{ Math.round(transferPercent) }}%</span>
        <span v-if="transferFiles > 1">{{ t("{count} 个文件", { count: transferFiles }) }}</span>
        <span v-if="transferSpeed">{{ transferSpeed }}</span>
        <span v-if="transferEta">{{ t("剩余 {eta}", { eta: transferEta }) }}</span>
      </p>
      <p v-if="transferFailed" class="quick-paste-note quick-paste-error" role="alert">{{ job.error }}</p>
      <div class="quick-paste-transfer-actions">
        <template v-if="transferFailed">
          <button class="primary" type="button" @click="retryJob">{{ t("重试") }}</button>
          <button type="button" @click="close(false)">{{ t("关闭") }}</button>
        </template>
        <button v-else type="button" :disabled="busy" @click="cancelJob">{{ t("取消取回") }}</button>
      </div>
      <p class="quick-paste-transfer-hint note">
        {{ transferFailed ? t("Esc 关闭") : t("Esc 取消取回并关闭") }}
      </p>
    </div>

    <!-- The sidecar's "this is large, ask first" question.  It is shown instead
         of the list because the row is already chosen; Esc steps back. -->
    <div v-else-if="confirmRow" class="quick-paste-transfer quick-paste-confirm">
      <h2 class="quick-paste-transfer-title">{{ t("这个文件较大") }}</h2>
      <p class="quick-paste-transfer-name">{{ oneLine(previewLabel(confirmRow)) }}</p>
      <p v-if="confirmSize > 0" class="quick-paste-note">
        {{ t("文件约 {size}，粘贴前需先从另一台设备传输。继续？", { size: size(confirmSize) }) }}
      </p>
      <p v-else class="quick-paste-note">{{ t("粘贴前需要先从另一台设备传输，可能要用上一会儿。继续？") }}</p>
      <div class="quick-paste-transfer-actions">
        <button class="primary" type="button" @click="confirmLarge">{{ t("继续取回") }}</button>
        <button type="button" @click="cancelConfirm">{{ t("取消") }}</button>
      </div>
      <p class="quick-paste-transfer-hint note">{{ t("Enter 继续 · Esc 关闭") }}</p>
    </div>

    <template v-else>
      <label class="search quick-paste-search">
        <Search :size="17" />
        <input ref="searchInput" v-model="query" type="search" autofocus
          :aria-label="t('搜索剪贴板历史')" :placeholder="t('搜索剪贴板历史…')"
          aria-controls="quick-paste-list" :disabled="busy" />
        <button v-if="query" type="button" class="search-clear" :aria-label="t('清除搜索')"
          :title="t('清除搜索')" @click.prevent="clearQuery"><X :size="14" /></button>
      </label>
      <!-- The chips keep the pointer from taking the caret out of the search
           box: the gesture this window serves is one word and Return, and a chip
           that stole the focus ended the word at the first click.
           `mousedown.prevent` is what stops the focus moving (the click still
           fires); `focusSearch` puts it back for the keyboard path. -->
      <div class="quick-paste-kinds" role="group" :aria-label="t('按类型筛选')" @mousedown.prevent>
        <template v-if="!favoritesMode">
          <button v-for="chip in kindChips" :key="chip.id" type="button" class="chip quick-paste-chip"
            :class="{ 'chip--active': kind === chip.id }" :aria-pressed="kind === chip.id"
            :disabled="busy" @click="pickKind(chip.id)">{{ chip.label }}</button>
        </template>
        <!-- 收藏 switches the list to the saved library, the same rows and the
             same Enter.  It is a mode chip rather than a history filter: a
             favourite lives in its own store, and the type/time/device controls
             below have nothing to narrow there. -->
        <button type="button" class="chip quick-paste-chip quick-paste-favorites"
          :class="{ 'chip--active': favoritesMode }" :aria-pressed="favoritesMode"
          :disabled="busy" @click="toggleFavorites">
          <Star :size="13" aria-hidden="true" />{{ t("收藏") }}
        </button>
      </div>
      <!-- The second row of controls: order, then how far back, then which
           machine.  Sort is the host's (see `quickPasteList`); the other two
           narrow this page here, so their labels stay one word each and the
           device list is a select rather than a row of chips that would grow
           with every paired machine. -->
      <div v-if="!favoritesMode" class="quick-paste-filters">
        <button type="button" class="chip quick-paste-chip quick-paste-sort"
          :class="{ 'chip--active': sort === 'oldest' }" :aria-label="t('排序')"
          :aria-pressed="sort === 'oldest'" :disabled="busy" @mousedown.prevent @click="toggleSort">
          <ArrowDownUp :size="13" aria-hidden="true" />
          {{ sort === "newest" ? t("最新优先") : t("最旧优先") }}
        </button>
        <div class="quick-paste-times" role="group" :aria-label="t('按时间筛选')">
          <button v-for="chip in timeChips" :key="chip.id" type="button"
            class="chip quick-paste-chip quick-paste-time" :class="{ 'chip--active': timeFilter === chip.id }"
            :aria-pressed="timeFilter === chip.id" :disabled="busy"
            @mousedown.prevent @click="pickTime(chip.id)">{{ chip.label }}</button>
        </div>
        <!-- The select is not `mousedown.prevent`ed: opening a native picker is
             the pointer's job.  The caret comes back with `pickDevice` once the
             choice is made, which is the moment the reader wants to type again. -->
        <select v-model="deviceFilter" class="quick-paste-device" :aria-label="t('按来源设备筛选')"
          :disabled="busy" @change="pickDevice">
          <option v-for="choice in deviceChoices" :key="choice.id" :value="choice.id">
            {{ deviceChoiceLabel(choice) }}
          </option>
        </select>
      </div>
      <!-- A read failure replaces the list (there is nothing to show); a pick
           failure sits above it, because the clipboard already holds the clip and
           the row the reader chose is still there to try again.  The retry is a
           new read, which is the only thing that can fix the first kind. -->
      <template v-if="failed">
        <p class="quick-paste-note quick-paste-error" role="alert">{{ failed }}</p>
        <div class="quick-paste-transfer-actions">
          <button type="button" @click="searchNow">{{ t("重试") }}</button>
        </div>
      </template>
      <p v-else-if="pasteError" class="quick-paste-note quick-paste-error" role="alert">{{ pasteError }}</p>
      <p v-if="busy" class="quick-paste-note" role="status">{{ t("正在粘贴…") }}</p>
      <p v-else-if="loading" class="quick-paste-note" role="status">{{ t("正在搜索…") }}</p>
      <ul v-if="!failed && visibleRows.length" id="quick-paste-list" ref="listEl" class="quick-paste-list"
        role="listbox" :aria-label="t('剪贴板历史')">
        <li v-for="(row, index) in visibleRows" :id="`quick-paste-row-${index}`" :key="row.id"
          class="quick-paste-row" :class="{ 'quick-paste-row--selected': index === selected }"
          role="option" :aria-selected="index === selected"
          @mouseenter="selected = index; onRowEnter(row, $event)" @mousemove="onRowMove"
          @mouseleave="cancelHover" @click="pickOnClick(index)">
          <span v-if="thumbFor(row)" class="quick-paste-glyph quick-paste-glyph--image">
            <img :src="thumbFor(row)" alt="" draggable="false" loading="lazy" />
          </span>
          <span v-else class="quick-paste-glyph" aria-hidden="true">{{ kindGlyph(row) }}</span>
          <span class="quick-paste-body">
            <span class="quick-paste-text">{{ oneLine(previewLabel(row)) }}</span>
            <span class="quick-paste-meta">{{ rowMeta(row) }}</span>
            <!-- A file still on the other machine: pasting it starts the fetch,
                 and the row says so before the key is pressed. -->
            <span v-if="isRemoteFileRow(row)" class="quick-paste-remote">
              {{ t("来自 {name} · 粘贴时取回", { name: remoteSource(row) }) }}
            </span>
          </span>
        </li>
      </ul>
      <div v-if="!failed && !loading && !visibleRows.length" class="quick-paste-empty" role="status">
        <p class="quick-paste-empty-title">{{ emptyTitle }}</p>
        <p class="quick-paste-note">{{ emptyHint }}</p>
      </div>
      <footer class="quick-paste-footer">
        <span>{{ t("↑↓ 选择") }}</span>
        <span>{{ t("Enter 粘贴") }}</span>
        <span>{{ t("Shift+Enter 纯文本粘贴") }}</span>
        <span>{{ t("Esc 关闭") }}</span>
        <span>{{ t("单击粘贴") }}</span>
      </footer>
      <!-- The pointer's own card: follows it over an image row, so the row can
           be recognised without leaving the keyboard's list.  Pointer-events
           none because the card is a glance, not a target. -->
      <div v-if="hoverCard" class="quick-paste-preview" :style="hoverStyle" aria-hidden="true">
        <img :src="hoverCard.image" alt="" />
        <p v-if="hoverCard.width && hoverCard.height" class="quick-paste-preview__size">
          {{ hoverCard.width }} × {{ hoverCard.height }}
        </p>
      </div>
    </template>
  </div>
</template>
