<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import {
  Check, ChevronLeft, ChevronRight, ClipboardPaste, Copy, ExternalLink, Globe,
  History, Pin, PinOff, Plug, RefreshCw, Search, SendHorizontal, Smartphone, Star,
  Trash2, X,
} from "@lucide/vue";
import { bridge } from "../api/bridge";
import type { HistoryItem, HistoryPreview, HistoryPreviewFile } from "../api/types";
import { t } from "../i18n";
import { dateTime, dayLabel, isPlaceholderPreview, size } from "../i18n/format";
import { createApplicationStore } from "../stores/application";
import { createHistoryActions } from "../lib/history-actions";
import { highlightSegments } from "../lib/highlight";
import { previewLabel, typeLabel } from "../lib/history-label";
import { SEARCH_DEBOUNCE_MS } from "../lib/search-box";
import { announce } from "../lib/status";

/**
 * The history page: the list, its toolbar, its pager, and the card under a row.
 *
 * It is a component for one reason and not for tidiness.  Every clip a peer
 * pushes, every tick of a checkbox and every hover that opens a preview redraws
 * whatever holds `state.history` in its render scope — and that used to be
 * `App.vue`, which meant the sidebar, the eight pages behind it and the settings
 * form were all re-rendered by a checkbox.  Split here, the re-render stops at
 * this subtree.  It is the boundary the audit asked for (U03): not "one file per
 * page", but "one component per thing that redraws on its own".
 *
 * What stays in the window is what the window owns:
 *
 * - the row menu, because the overview's feed raises the same menu over the same
 *   rows, so it cannot belong to one page (see `historyMenu` in `App.vue`);
 * - the confirms that a row action opens, because they are the window's dialogs
 *   and one of them — 删除 — is also opened by a key on the keyboard cursor;
 * - the translate dialog, which reads the language pair the settings page owns;
 * - the window's chords and the keyboard cursor.  `↑ ↓ Enter Delete Space` still
 *   arrive from `App.vue`'s one handler, which knows which page is in front;
 *   this component exposes the five things they reach for.
 *
 * `store` is a prop rather than a module import: `createApplicationStore()` is a
 * factory, so a second call would be a second window's state (`FavoritesView`
 * has the same shape and for the same reason).
 */
const props = defineProps<{
  store: ReturnType<typeof createApplicationStore>;
  /** The host answers: `state.status.health === "ready"`.  A prop because the
   *  lock is the window's — the unlock form sits above every page. */
  ready: boolean;
  /** Desktop or browser preview, which changes one empty-state sentence. */
  native: boolean;
  /** A clip's text is being read back for the translator, in the window's
   *  dialog.  The row's 翻译 button stands down while it is, as it did when the
   *  flag lived here. */
  translating: boolean;
}>();

const store = props.store;
const state = store.state;
// The row actions the menu also uses.  Shared rather than copied: see
// `lib/history-actions.ts` for why they are neither here nor in `App.vue`.
const {
  isWebLink, openHistoryLink, isRemoteFile, remoteFileActionable,
} = createHistoryActions(store);

/** What this page asks the window to do, and all it should have to ask for.
 *  Each one is something the window owns: a dialog, the menu, or a confirmation
 *  that belongs to the record rather than to the list. */
const emit = defineEmits<{
  menu: [at: MouseEvent, item: HistoryItem];
  translate: [item: HistoryItem];
  delete: [item: HistoryItem];
  "batch-delete": [ids: string[]];
  /** 取回并粘贴: the row's own button on a file that lives on another device.
   *  The window owns the fetch (the progress bar and the paste that follows), so
   *  the page only names the row. */
  "paste-to-app": [item: HistoryItem];
}>();

/** The button's tooltip on a remote-file row: what the click does, and which
 *  machine it fetches from.  The same wording as the menu entry, because it is
 *  the same action. */
function pasteRemoteTitle(item: HistoryItem): string {
  return t("从 {name} 取回这个文件并粘贴", { name: item.source_name || t("未知") });
}

/** The search box, so Ctrl+F has something to focus and Esc something to empty. */
const searchInput = ref<HTMLInputElement | null>(null);

/** The line under the preview: what kind of clip it is, and whether it is kept.
 *
 * The kind is left out when the line above already says it.  An image has no
 * text of its own, so the sidecar's "[Image]" previews as 图片 — the same word
 * the kind gives, on the line directly above this one.  Nothing is lost by
 * staying quiet there: the word is still on screen, and the reader is already
 * looking at it.
 */
function typeLine(item: HistoryItem) {
  const label = typeLabel(item);
  return [label === previewLabel(item) ? "" : label, item.pinned ? t("已置顶") : ""].filter(Boolean).join(" · ");
}

/** The page's rows, cut into the days they arrived on.
 *
 *  A clipboard is a record of *when*: every row carried an absolute `09-14
 *  10:32`, and forty of them in a column say nothing about where one sitting
 *  ended and the next began.  Runs are taken in the order the rows already
 *  come, so the 最新/最旧 sort still decides which end the days start from,
 *  and `index` stays the row's place in the whole page — the keyboard cursor
 *  counts rows, not groups.
 *
 *  A page boundary can repeat a day's heading (the calendar day straddles two
 *  pages), which is the honest reading: what is under the heading is what this
 *  page holds. */
const historyGroups = computed(() => {
  const groups: { stamp: string; key: string; label: string; rows: { item: HistoryItem; index: number }[] }[] = [];
  let index = 0;
  for (const item of state.history as HistoryItem[]) {
    const date = new Date(item.timestamp * 1000);
    const stamp = isNaN(date.getTime()) ? "unknown" : `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
    const label = isNaN(date.getTime()) ? "" : dayLabel(item.timestamp);
    const last = groups[groups.length - 1];
    if (last && last.stamp === stamp) last.rows.push({ item, index: index++ });
    else groups.push({ stamp, key: `${stamp}-${groups.length}`, label, rows: [{ item, index: index++ }] });
  }
  return groups;
});

/** How many times a clip has been pasted back, as a badge worth showing.
 *
 * Nothing at zero: a badge saying "0 pastes" on every fresh clip is noise, and
 * legacy only drew it above zero. English needs both numbers spelled out, which
 * is why the singular is a key of its own.
 */
function pasteCount(item: HistoryItem) {
  const count = Number(item.paste_count || 0);
  if (!count) return "";
  return count === 1 ? t("1 次粘贴") : t("{count} 次粘贴", { count });
}
/** Which link carried a row in, as the chip's word, title and glyph.
 *
 * Three routes, not two. A clip pushed from the web panel is recorded as "web",
 * and its row's source name says only "Web" — so reading that as 本地 would tell
 * the user the phone was on this network, when the whole point of the panel is
 * that it need not be. An empty route still says nothing rather than claiming a
 * path: those rows draw no chip at all.
 */
function routeLabel(transport: string) {
  if (transport === "relay") return t("互联网");
  if (transport === "web") return t("网页");
  return t("本地");
}
function routeTitle(transport: string) {
  if (transport === "relay") return t("互联网中继");
  if (transport === "web") return t("网页推送");
  return t("本地连接");
}
function routeIcon(transport: string) {
  if (transport === "relay") return Globe;
  if (transport === "web") return Smartphone;
  return Plug;
}

/** The history page's kind chips, in the panel's own order and with its icons.
 *
 * The icons are kept here and not on the rows, where the leading column is a
 * selection checkbox: a chip is a button with room for a glyph, and the words
 * beside it already say the kind.
 */
const kindChips = computed(() => [
  { id: "all", label: t("全部"), icon: "" },
  { id: "text", label: t("文本"), icon: "📝" },
  { id: "image", label: t("图片"), icon: "🖼" },
  { id: "file", label: t("文件"), icon: "📄" },
  { id: "link", label: t("链接"), icon: "🔗" },
]);
/** A chip's badge: the sidecar counts each kind under the current search. */
function kindCount(id: string) {
  return Number(state.counts?.[id] || 0);
}
/** A chip with nothing behind it is disabled unless it is the one in effect —
 * the panel's rule, so the way back to 全部 is never the one that greys out. */
function kindUnavailable(id: string) {
  return state.kind !== id && kindCount(id) === 0;
}
/** The row whose clip is being read in full, if any.
 *
 * The row clamps its preview to three lines — the right size for scanning a
 * list, the wrong size for the one clip the user is looking for. A pasted log or
 * a long article arrives cut off, and the only ways to the rest of it were to
 * copy it somewhere or hand it to the translator. The panel answered this with a
 * card under the cursor; this is the same card, and it differs from the panel's
 * in two deliberate ways:
 *
 * - anchored to the row rather than the pointer, because a native window has no
 *   reason to chase the mouse, and scrollable rather than inert, so a clip
 *   taller than the card can still be read to the end (the panel's card was
 *   `pointer-events: none` and flipped itself above the cursor instead);
 * - no longer text only.  For a long time it was, because an image and a file
 *   row have no words to show — their "preview" is the kind's own label and a
 *   file name, both of which the row already says — so the card stayed empty
 *   for exactly the two kinds a glance is worth most for.  What it shows for
 *   those comes from the sidecar (`history.preview`), which is where the
 *   picture is downscaled and the files are stat'd: the window never reads a
 *   file or holds a full-size image, so hovering across a list costs a few
 *   kilobytes a row.
 *
 * `aria-hidden` on the card, because what it adds is purely visual: the row's
 * own paragraph carries the whole text preview in the DOM, and a picture is
 * read by looking at it.
 */
const previewCard = ref<{
  id: string;
  /** `"text"`, `"image"`, `"files"` — which of the three the card is showing. */
  kind: string;
  text: string;
  image: string;
  width: number;
  height: number;
  files: HistoryPreviewFile[];
  total: number;
} | null>(null);

/** The row whose card is held open rather than hovered.
 *
 * A hover card leaves with the pointer, so a long clip — the only kind worth a
 * card — could be read only as long as the hand stayed still.  A double click
 * (or Space on the row under the cursor) pins it: the card stays while the
 * reader scrolls, selects and copies, and Esc or a second double click lets it
 * go.  Pinning is not a second kind of preview, it is the same card with its
 * timer taken off.
 */
const previewPinned = ref("");

/** How long the pointer must rest on a row before the sidecar is asked about it.
 *
 * A sweep down the list crosses a dozen rows, and every one of them would be a
 * frame's worth of decode at the other end.  Short enough that a deliberate
 * hover never notices, long enough that a sweep is one request rather than
 * twelve.
 */
const PREVIEW_DELAY_MS = 180;

/** Cards already fetched, by the entry they were fetched for.  A row hovered
 * twice is one read.
 *
 * The key carries the row's timestamp as well as its id because the id alone
 * is not the row: `entry_id` is a plain SQLite integer with no AUTOINCREMENT,
 * and the sidecar re-derives it on load as `max(entry_id) + 1`, so a row
 * deleted as the newest one hands its id to the next clip after a restart.  A
 * card kept under the bare id would then be shown for a different clip.
 *
 * The sidecar does not push a change to a row's card, so an entry edited
 * elsewhere would keep a stale one — but nothing edits an entry's *picture*: a
 * row is replaced by a fresh row when its clip changes, and what a delete
 * leaves behind is only entries nothing will ask for again.  The cap is what
 * bounds those rather than an invalidation the window has no event for.
 */
const PREVIEW_CACHE_MAX = 64;
const previewCache = new Map<string, HistoryPreview>();

/** The cache key for one row.  Two rows that share an id are still two rows. */
function previewKey(item: HistoryItem) {
  return `${item.id}:${item.timestamp}`;
}

/** Image rows carry a small picture in the list itself, not only in the hover
 *  card: a clipboard is scanned visually, and an image row whose only content
 *  is `[Image]` until the pointer reaches it is a row the reader has to hover
 *  over to recognise.  The sidecar already downscales the preview, so this is
 *  the same read the card makes, just made for the visible page without a
 *  pointer. */
const rowThumbs = ref<Record<string, string>>({});
const thumbPending = new Set<string>();
const thumbFailed = new Set<string>();
function isImageItem(item: HistoryItem) {
  const kind = (item.content_type || "").toUpperCase();
  return kind === "IMAGE_PNG" || kind === "IMAGE" || kind === "IMAGE_EMF";
}
function thumbFor(item: HistoryItem): string {
  return rowThumbs.value[previewKey(item)] || "";
}
async function loadRowThumb(item: HistoryItem) {
  const key = previewKey(item);
  if (rowThumbs.value[key] || thumbPending.has(key) || thumbFailed.has(key)) return;
  thumbPending.add(key);
  try {
    const card = await bridge.previewHistoryEntry(item.id);
    if (card.kind === "image" && card.image) {
      rowThumbs.value = { ...rowThumbs.value, [key]: card.image };
    } else {
      thumbFailed.add(key);
    }
  } catch {
    thumbFailed.add(key);
  } finally {
    thumbPending.delete(key);
  }
}
watch(
  () => state.history.map((item) => `${item.id}:${item.timestamp}`).join("|"),
  () => {
    for (const item of state.history) {
      if (isImageItem(item)) void loadRowThumb(item);
    }
  },
  { immediate: true },
);

function rememberPreview(key: string, card: HistoryPreview) {
  previewCache.set(key, card);
  // A Map iterates in insertion order, and `recallPreview` re-inserts on every
  // hit, so the entry dropped is the one that has gone longest without being
  // looked at — the row the pointer keeps coming back to survives a run of
  // rows it never touched.
  while (previewCache.size > PREVIEW_CACHE_MAX) {
    const oldest = previewCache.keys().next().value;
    if (oldest === undefined) break;
    previewCache.delete(oldest);
  }
}

/** A hit that counts as a use.
 *
 * Reading through `get` alone left the cache first-in-first-out: an image row
 * hovered a dozen times was still the oldest entry, and the next new row
 * evicted the card the reader had just been looking at.  Re-inserting on a hit
 * makes the eviction order the reading order, which is the one that matters. */
function recallPreview(key: string): HistoryPreview | undefined {
  const card = previewCache.get(key);
  if (!card) return undefined;
  previewCache.delete(key);
  previewCache.set(key, card);
  return card;
}

/** Which row the pointer is on, so a fetch that lands late is dropped.
 * Not a ref: nothing renders from it, it only decides whether an answer that
 * arrived after the pointer moved is still worth showing.
 */
let previewWanted = "";
let previewTimer: ReturnType<typeof setTimeout> | undefined;

/** Whether a row's card has to come from the sidecar rather than from the row.
 *
 * The kind decides it, and the *presence* of a preview text must not: a file
 * row's preview is the file's own name, which looks like something to show and
 * is not — the row already says it in full, and what a card can add is the size
 * and whether the file is still there.  Reading this off `content_type` keeps
 * that a property of the row rather than of how its name happens to read.
 */
function previewable(item: HistoryItem) {
  const kind = (item.content_type || "").toUpperCase();
  return kind === "FILE" || kind === "FILE_REMOTE" || kind === "IMAGE_PNG" ||
    kind === "IMAGE" || kind === "IMAGE_EMF";
}

/** The card the row's own preview is enough for, or null when it is not.
 *
 * An image's "preview" is the label `[Image]` and a file's is a name, so for
 * those the placeholder test says nothing useful — the branch above has already
 * claimed them.  What is left is the case the card was built for: a clip whose
 * text was clamped to three lines and has more of itself to show.
 */
function textCard(item: HistoryItem) {
  const text = item.preview && !isPlaceholderPreview(item.preview) ? item.preview : "";
  return text
    ? { id: item.id, kind: "text", text, image: "", width: 0, height: 0, files: [], total: 0 }
    : null;
}

function showPreview(item: HistoryItem) {
  // A pinned card owns this space: the reader asked for that one to stay, so a
  // pointer passing over another row must not swap the card under them.
  if (previewPinned.value && previewPinned.value !== item.id) return;
  if (!previewable(item)) {
    previewCard.value = textCard(item);
    return;
  }
  // The row's own text is held back rather than shown while the read is in
  // flight: for a file row it is the name alone, so opening on it and then
  // swapping it for the list would be the card changing its mind under the
  // pointer.  It is what the card falls back to if the sidecar has nothing.
  previewWanted = item.id;
  clearTimeout(previewTimer);
  const cached = recallPreview(previewKey(item));
  if (cached) { applyPreview(item, cached); return; }
  previewTimer = setTimeout(() => void fetchPreview(item), PREVIEW_DELAY_MS);
}

async function fetchPreview(item: HistoryItem) {
  let card: HistoryPreview;
  try {
    card = await bridge.previewHistoryEntry(item.id);
  } catch {
    // The sidecar answers an empty card rather than an error for a row it
    // cannot make sense of, so a rejection means the sidecar itself is gone —
    // which the rest of the window will say in its own time.  A hover is not
    // the place for it, and there is no card to open either way.
    return;
  }
  rememberPreview(previewKey(item), card);
  applyPreview(item, card);
}

function applyPreview(item: HistoryItem, card: HistoryPreview) {
  if (previewWanted !== item.id) return;
  // An empty card is the sidecar saying it has nothing to add — a vector image
  // it cannot render, a file list it could not parse — and then the row's own
  // text is the card after all.
  if (!card.kind) {
    previewCard.value = textCard(item);
    return;
  }
  previewCard.value = {
    id: item.id,
    kind: card.kind,
    text: "",
    image: card.image,
    width: card.width,
    height: card.height,
    files: card.files,
    total: card.total,
  };
}

function hidePreview(item: HistoryItem) {
  if (previewWanted === item.id) {
    previewWanted = "";
    clearTimeout(previewTimer);
  }
  // The pinned card is not following a pointer, so leaving the row is not a
  // reason to close it.
  if (previewPinned.value === item.id) return;
  if (previewCard.value?.id === item.id) previewCard.value = null;
}

/** A pin belongs to a row, so it goes when that row does — a page turn, a new
 *  search, a delete.  Left behind, it would sit on an id that is no longer on
 *  screen and quietly refuse every hover preview on the page. */
watch(() => state.history.map(item => item.id).join("|"), () => {
  if (!previewPinned.value) return;
  if (state.history.some(item => item.id === previewPinned.value)) return;
  previewPinned.value = "";
  previewCard.value = null;
}, { flush: "post" });

/** Hold a row's card open, or let it go.
 *
 * A row with no card to hold — a short clip whose three lines are all there is —
 * is left alone rather than pinning an empty box, and double clicking it copies
 * nothing either: the gesture has to mean one thing.
 */
function togglePreviewPin(item: HistoryItem) {
  if (previewPinned.value === item.id) { previewPinned.value = ""; previewCard.value = null; return; }
  if (!previewable(item) && !textCard(item)) return;
  previewPinned.value = item.id;
  showPreview(item);
}

/** A card file's size, in the window's own units.
 *
 * `file_ref.human_size` on the sidecar writes the same string; this is the
 * window's formatter, and a size crossing the IPC as a number is what lets both
 * exist without the sidecar having to pick one language for it.
 */
function cardSize(file: HistoryPreviewFile) {
  return file.kind === "dir" ? t("文件夹") : size(file.size);
}

/** A chip's own label, for the empty state that names the kind. */
function kindLabel(id: string) {
  return kindChips.value.find((chip) => chip.id === id)?.label || id;
}
/** A read in flight, a write in flight, or a host that cannot answer at all:/n * the page's own read lock, which the pager and the boxes answer to.  The
 * window's `busy` folds in `refreshing` and `ready`, and this is that plus the
 * list's own load — see `historyWritable` for what it must not cover. */
const historyBusy = computed(() =>
  state.pending || state.refreshing || !props.ready || state.loading);
/** A *write* to the list, as opposed to a read of it.
 *
 * `historyBusy` folds in `refreshing` and `loading`, both of which are true for
 * a beat after every action and after every clip a peer pushes — so bound to the
 * row buttons, copy, pin and delete greyed out at the exact moment the reader
 * reached for them.  That is the window's busiest path, disabled by a read.  The
 * row's own actions answer to a write in flight (or a host that cannot answer at
 * all); the selection box, the batch bar and the pager keep `historyBusy`,
 * because a read in flight is about to redraw what they are pointing at. */
const historyWritable = computed(() => state.pending || !props.ready);
/**
 * The two toolbar refreshes, which say what they found.
 *
 * A refresh is the one click that is allowed to change nothing: the list comes
 * back as it was, the spinner is over before the eye reaches it, and the button
 * is left looking exactly as it did before it was pressed. That is the case a
 * reader cannot tell from a button that is broken, so each of these names the
 * list it re-read and how much is in it now.
 *
 * Both wrap the store call rather than replacing it: the history read is also
 * what the search box, the kind chips and the pager run, and the device read is
 * the poller's, so neither can report on its own without reporting for them.
 */
async function refreshHistoryNow() {
  await store.refreshHistory();
  store.toast("ui.history", t("已刷新 · {count} 条记录", { count: state.total }));
}
/** Open the host's quick-paste overlay from the page that holds the rows.
 *
 * The overlay is the OS-wide gesture's window (`Ctrl+Shift+V` by default): the
 * host creates it over whichever application has the focus, which is why the
 * sidebar — a control the main window draws about itself — is not where it can
 * live.  A reader with the history page in front of them has this button
 * instead, and it is one command: the window, the focus bookkeeping and the
 * paste are the host's.
 */
const quickPasteBusy = ref(false);
async function openQuickPasteWindow() {
  if (quickPasteBusy.value) return;
  quickPasteBusy.value = true;
  try {
    await bridge.openQuickPaste();
  } catch (error) {
    // The failure band, because the host refusing to open a window is a broken
    // window rather than an ordinary answer beside a key press.
    store.setError(error);
  } finally {
    quickPasteBusy.value = false;
  }
}
/* The search box: what it holds, and when it asks.
 *
 * Out of a search without touching the keyboard works the same way — the × is
 * on the field, so the reader who cleared a search is still searching, with an
 * empty box, rather than looking at a list with the caret nowhere. */
/** What the box holds, which is not quite what the list was last asked about:
 * the words land in the box on the keypress and in `state.query` a moment
 * later.  Keeping both is what makes the × appear with the first letter. */
const searchText = ref(state.query);
let searchTimer: ReturnType<typeof setTimeout> | undefined;
/** Ask for the words in the box once the typing stops.
 *
 * A write still in flight refuses a new query — the store drops it, and the
 * box would then be holding words the list was never asked about.  So the
 * timer re-arms instead of giving up: the write finishes, and the search
 * asked for while it ran goes out after it rather than never. */
function queueSearch(value: string) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (state.pending) { queueSearch(value); return; }
    store.search(value);
  }, SEARCH_DEBOUNCE_MS);
}
function typeSearch(value: string) {
  searchText.value = value;
  queueSearch(value);
}
function clearHistorySearch() {
  clearTimeout(searchTimer);
  searchText.value = "";
  store.search("");
  // A write in flight answers nothing, and a cleared box over a list that is
  // still filtered is worse than waiting: ask again once it is done.
  if (state.query) queueSearch("");
  searchInput.value?.focus();
}
// Its own flag rather than the store's `pending`, because a merge spends most of
// its time fetching each row's full text before it ever reaches the store.
const mergeBusy = ref(false);
const visibleIds = computed(() => [...new Set(state.history.map((item) => item.id).filter((id) => id.trim()))]);
const allSelected = computed(() => !!visibleIds.value.length &&
  visibleIds.value.every((id) => state.selectedIds.includes(id)));
/** The most a batch may name, which is the sidecar's own bound
 * (`internal/adapters/sidecar/favorites.py`: an id list of 1..100).
 *
 * Named because the reader meets it as a wall: every unticked box on the page
 * greys out at once, and nothing on the page said why — a limit that only
 * shows itself by disabling things reads as a broken page. */
const HISTORY_SELECT_LIMIT = 100;
const selectLimitReached = computed(() => state.selectedIds.length >= HISTORY_SELECT_LIMIT);
const selectLimitNote = computed(() => t("一次最多选 {count} 条", { count: HISTORY_SELECT_LIMIT }));
/** Say how many are ticked once the ticking stops.
 *
 *  The count was itself the live region, and it changed on every tick — so a
 *  reader who held shift and clicked down ten rows got ten announcements read
 *  over one another, and the last one did not finish before the next began.
 *  Counting out loud is right; counting out loud ten times is not.  The number
 *  is announced through the window's one status line, and only after the reader
 *  has stopped: 400 ms is long enough to swallow a run of clicks and short
 *  enough that the answer arrives while they are still looking at the boxes.
 *  See U68.
 */
let selectAnnounceTimer: ReturnType<typeof setTimeout> | undefined;
watch(() => state.selectedIds.length, (count) => {
  if (selectAnnounceTimer) clearTimeout(selectAnnounceTimer);
  if (!count) return;
  selectAnnounceTimer = setTimeout(() => {
    announce(t("已选择 {count} 条", { count }));
  }, 400);
});
onUnmounted(() => { if (selectAnnounceTimer) clearTimeout(selectAnnounceTimer); });
/** The row the arrow keys are on, said out loud.
 *
 * The list is a set of articles with checkboxes inside them, so the row cannot
 * be an `option` under a `listbox` — the cursor is a pointer into
 * `state.history` and the row only ever got a class for it, which is a mark on
 * screen and nothing at all in speech.  `aria-current` marks the row for a
 * reader who moves by element, and this says which one it is and what it holds:
 * a position is the one thing a row cannot say about itself.  Debounced for the
 * same reason as the count above — holding ↑ walks ten rows in a second, and ten
 * announcements queue up behind each other. */
let kbdAnnounceTimer: ReturnType<typeof setTimeout> | undefined;
watch(() => state.kbdIndex, (index) => {
  if (kbdAnnounceTimer) clearTimeout(kbdAnnounceTimer);
  const item = state.history[index];
  if (index < 0 || !item) return;
  kbdAnnounceTimer = setTimeout(() => {
    announce(t("第 {index} 条，共 {total} 条：{text}", {
      index: index + 1, total: state.history.length, text: previewLabel(item),
    }));
  }, 250);
});
onUnmounted(() => { if (kbdAnnounceTimer) clearTimeout(kbdAnnounceTimer); });
/** Pin or unpin the whole selection, reporting how many rows it covered. */
async function pinSelected(pinned: boolean) {
  const count = state.selectedIds.length;
  if (await store.batchPin(pinned)) {
    store.toast("ui.history", pinned
      ? t("已置顶 {count} 条", { count })
      : t("已取消置顶 {count} 条", { count }));
  }
}
const favoritedIds = ref<Set<string>>(new Set());
const favoritingId = ref("");

/** One-click favourite from the row.
 *
 * The row's third action used to be 翻译, which the right-click menu still
 * offers; a single click on the star adds the clip to the favourites library
 * without opening the selection toolbar. `allowUnselected` is what makes a
 * plain row click (no checkbox selection) count.
 */
async function favoriteRow(item: HistoryItem) {
  if (favoritingId.value) return;
  favoritingId.value = item.id;
  const count = await store.batchFavorite([item.id], "", true);
  favoritingId.value = "";
  if (count) {
    favoritedIds.value = new Set(favoritedIds.value).add(item.id);
    store.toast("ui.favorites", t("已加入收藏夹 {count} 条", { count }));
  }
}

async function favoriteSelected() {
  const count = await store.batchFavorite([...state.selectedIds]);
  if (count) store.toast("ui.favorites", t("已加入收藏夹 {count} 条", { count }));
}
/**
 * Merge the selected rows into one clip and push it to this machine's clipboard.
 *
 * This is the legacy web panel's 推送到电脑 (`history-panel.js`, `mergeCopySelected`),
 * and it was the one batch action the new page had lost: the toolbar could pin,
 * favorite and delete, but nothing could combine several clips into a single push.
 *
 * Two of its rules are load-bearing and are kept.  The rows merge in raw history
 * order rather than the order they were ticked, so the result reads the way the
 * list does.  And each row's FULL text is fetched first, because a list row
 * carries a truncated preview — merging previews would push cut-off clips to
 * every device.  A fetch that fails falls back to that row's preview instead of
 * dropping it, so one entry vanishing mid-flight cannot lose the other nine.
 */
async function mergePushSelected() {
  if (mergeBusy.value || historyBusy.value) return;
  const ordered = state.history.filter((item) => state.selectedIds.includes(item.id));
  if (!ordered.length) {
    announce(t("没有可合并的记录"));
    return;
  }
  mergeBusy.value = true;
  try {
    const texts: string[] = [];
    for (const item of ordered) {
      let text = item.preview;
      try {
        const detail = await bridge.readHistoryText(item.id);
        if (detail?.text) text = detail.text;
      } catch { /* the row went away; its preview is still what the list showed */ }
      texts.push(text);
    }
    const merged = texts.join("\n---\n");
    if (merged.length > 100000) {
      // The store refuses anything longer, and its own message for that is
      // about a typed-in paste, which would not explain what happened here.
      announce(t("合并后内容过长，无法推送"));
      return;
    }
    const result = await store.pushText(merged);
    if (result) store.toast("ui.history", t("已合并推送 {count} 条到本机剪贴板", { count: texts.length }));
  } finally {
    mergeBusy.value = false;
  }
}
const historyListEl = ref<HTMLElement | null>(null);
/** A click on a row's body copies it; a click on one of its own controls does not.
 *
 * `closest` walks up from whatever was actually hit, so the checkbox's label, the
 * action column's buttons and the preview card are all excluded by the same test
 * — anything inside an interactive element belongs to that element.
 */
function historyRowClick(item: HistoryItem, event: MouseEvent) {
  // The second half of a double click is the same click again, and it would be
  // dropped by the write lock and answered with a shake — for a gesture that
  // was one intention.
  if (event.detail > 1) return;
  const target = event.target as HTMLElement | null;
  if (target?.closest("button, label, input, select, textarea, a, dialog")) return;
  if (!historyWritable.value) return;
  void store.copy(item, false);
}

/** The other half of the row's two gestures: a double click pins the card.
 *
 * The row's own controls keep their double clicks — two clicks on the pin
 * button is two pins, not a preview — the same test `historyRowClick` uses.
 */
function historyRowPin(item: HistoryItem, event: MouseEvent) {
  const target = event.target as HTMLElement | null;
  if (target?.closest("button, label, input, select, textarea, a, dialog")) return;
  togglePreviewPin(item);
}
/** Turn a page, then put the reader at the top of the new one.
 *
 * The pager sits under the list, so "next" answered by redrawing the rows above
 * the viewport the reader was still looking at: the new page began off screen
 * and what they saw was row 30 of it.  The list's own top is the target, not the
 * page's — the toolbar stays where it is — and the sticky toolbar's height is
 * subtracted so the first row lands under it rather than behind it. */
function turnPage(direction: number) {
  if (historyBusy.value) return;
  store.page(direction);
  void nextTick(() => {
    const list = historyListEl.value;
    // The scroller is the window's `.content`, and this component is drawn
    // inside it: found from the list rather than passed down as a prop,
    // because a prop would only restate the element the page lives in.
    const scroller = list?.closest<HTMLElement>(".content") ?? null;
    if (!list || !scroller) return;
    const sticky = scroller.querySelector<HTMLElement>(".toolbar");
    const clearance = sticky ? sticky.offsetHeight : 0;
    const top = list.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    scroller.scrollTop = Math.max(0, top - clearance);
  });
}

/* What the window's chords and its keyboard cursor reach for, exposed rather
 * than duplicated: `App.vue` decides *when* (which page is in front, whether a
 * dialog is open, whether the keystroke came from a text field) and this
 * component decides *what* — it is the one that knows where its box and its
 * card are.  Each returns or takes nothing it does not own. */
function focusSearch() {
  searchInput.value?.focus();
  // Selected as well as focused: the word already in the box is the one a find
  // bar offers to replace, not to type after (the same rule the window's Ctrl+F
  // applied when the box was its own).
  searchInput.value?.select();
}
/** Ctrl+K's hand-over: the palette found the word, this page shows the rest. */
function search(term: string) {
  typeSearch(term);
}
/** Esc pressed with a card held open: true when there was one to let go. */
function releasePinned(): boolean {
  if (!previewPinned.value) return false;
  previewPinned.value = "";
  previewCard.value = null;
  return true;
}
/** Esc pressed inside the box: true when it had something to clear. */
function clearSearchIfFocused(): boolean {
  if (!searchInput.value || document.activeElement !== searchInput.value || !searchText.value) return false;
  clearHistorySearch();
  return true;
}
/** Space on the keyboard cursor's row: the keyboard's double click. */
function pinKbdRow() {
  const row = state.history[state.kbdIndex];
  if (row) togglePreviewPin(row);
}

/** Leaving the page takes its timers with it.  The two watches above clear their
 *  own; these are the two an unmount can otherwise outlive — a keystroke still
 *  debouncing and a hover still waiting to ask the sidecar about a row nobody
 *  will see. */
onUnmounted(() => {
  clearTimeout(searchTimer);
  clearTimeout(previewTimer);
});

defineExpose({ focusSearch, search, releasePinned, clearSearchIfFocused, pinKbdRow });
</script>

<template>
  <div class="toolbar">
    <!-- Not `busy`: that also covers `refreshing`, which is true for a
         moment after every action and every clip a peer pushes — so
         the box greyed out under the reader's cursor mid-word.  Not a
         write in flight either, now that a query asked for during one
         waits for it to finish instead of being dropped: the only
         thing worth greyed-out is a host that cannot answer at all. -->
    <label class="search"><Search :size="17" /><input
      ref="searchInput"
      :value="searchText" :aria-label="t('搜索历史记录')" :placeholder="t('搜索历史记录')"
      :disabled="!ready" @input="typeSearch(($event.target as HTMLInputElement).value)"
    /><button v-if="searchText" type="button" class="search-clear" :aria-label="t('清除搜索')" :title="t('清除搜索')" @click.prevent="clearHistorySearch"><X :size="14" /></button></label>
    <button class="icon-button" :title="t('刷新')" :aria-label="t('刷新')" :disabled="historyBusy" @click="refreshHistoryNow"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
    <!-- The overlay's door from the page that holds the rows.  Labelled rather
         than a bare glyph, because it opens a window rather than acting on this
         list, and there is no icon in the window that already means "paste
         elsewhere".  The chord is in the tooltip, not on the button: this bar is
         already dense, and the overview carries the visible hint. -->
    <button class="icon-button icon-button--labelled" :aria-label="t('粘贴面板')" :title="t('粘贴面板（Ctrl+Shift+V）')"
      :disabled="!ready || quickPasteBusy" @click="openQuickPasteWindow"><ClipboardPaste :size="17" /><span class="icon-button-label">{{ t("粘贴面板") }}</span></button>
  </div>
  <div v-if="state.hasHistory" class="history-filters" role="group" :aria-label="t('按类型筛选历史记录')">
    <!-- The same line as the search box above: a filter is a read, and
         `historyBusy` covers `loading` — so the chip a reader reached
         for right after typing a search was disabled by that search's
         own request. -->
    <button v-for="chip in kindChips" :key="chip.id" class="chip"
      :class="{ 'chip--active': state.kind === chip.id }"
      :aria-pressed="state.kind === chip.id"
      :disabled="!ready || state.pending || kindUnavailable(chip.id)"
      :title="kindUnavailable(chip.id) ? t('该类型暂无内容') : ''"
      @click="store.setKind(chip.id)">
      <span v-if="chip.icon" aria-hidden="true">{{ chip.icon }}</span>{{ chip.label }}<span class="chip-count">{{ kindCount(chip.id) }}</span>
    </button>
    <button class="chip chip--sort" :title="t('切换历史排序（最新 / 最旧）')" :disabled="!ready || state.pending" @click="store.toggleSort">
      {{ state.sort === "newest" ? "↓ " : "↑ " }}{{ state.sort === "newest" ? t("最新优先") : t("最旧优先") }}
    </button>
  </div>
  <section class="history-list" ref="historyListEl" :aria-label="t('历史记录')" :aria-busy="state.loading">
    <div class="list-heading">
      <label class="select-visible"><input type="checkbox" :aria-label="t('全选当前页')"
        :checked="allSelected" :indeterminate="state.selectedIds.length > 0 && !allSelected"
        :disabled="historyBusy || !visibleIds.length || visibleIds.length > HISTORY_SELECT_LIMIT"
        :title="visibleIds.length > HISTORY_SELECT_LIMIT ? selectLimitNote : undefined"
        :aria-describedby="visibleIds.length > HISTORY_SELECT_LIMIT || selectLimitReached ? 'history-select-limit' : undefined"
        @change="store.selectAllVisible(($event.target as HTMLInputElement).checked)" />{{ t("全选当前页") }}</label>
      <!-- How many are ticked, and — once there are as many as a batch
           may name — why no more can be.  The boxes grey out on their
           own and said nothing.
           Not a live region any more: the number is announced through
           the footer's status line once the reader stops ticking, so a
           run of clicks does not queue a run of announcements.  See U68
           and the watcher on `state.selectedIds.length`. -->
      <span id="history-select-reached">{{ t("已选择 {count} 条", { count: state.selectedIds.length })
        }}<template v-if="selectLimitReached"> · {{ selectLimitNote }}</template></span>
      <!-- The cap explains itself before it is hit.  Greying out is how
           it arrives, and a page that only says why after the fact reads
           as a broken 全选 — which is what it looked like. -->
      <span v-if="visibleIds.length > HISTORY_SELECT_LIMIT" id="history-select-limit" class="note select-limit">{{ t("本页 {rows} 条，超过一次可选的 {count} 条，请逐条勾选。", { rows: visibleIds.length, count: HISTORY_SELECT_LIMIT }) }}</span>
      <!-- Five identical grey glyphs, told apart by tooltip only, two of
           which change something outside this window.  The words are
           there and give way on a narrow window, where the tooltip and
           the aria-label are all there is room for anyway. -->
      <div class="batch-actions">
        <button class="icon-button icon-button--labelled" :aria-label="t('批量置顶')" :title="t('批量置顶')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="pinSelected(true)"><Pin :size="17" /><span class="icon-button-label">{{ t("置顶") }}</span></button>
        <button class="icon-button icon-button--labelled" :aria-label="t('批量取消置顶')" :title="t('批量取消置顶')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="pinSelected(false)"><PinOff :size="17" /><span class="icon-button-label">{{ t("取消置顶") }}</span></button>
        <button class="icon-button icon-button--labelled" :aria-label="t('加入收藏夹')" :title="t('加入收藏夹')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="favoriteSelected"><Star :size="17" /><span class="icon-button-label">{{ t("收藏") }}</span></button>
        <button class="icon-button icon-button--labelled" :aria-label="t('合并推送到电脑')" :title="t('合并推送到电脑')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="mergePushSelected"><SendHorizontal :size="17" /><span class="icon-button-label">{{ t("推送") }}</span></button>
        <button class="icon-button icon-button--labelled" :aria-label="t('批量删除')" :title="t('批量删除')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="emit('batch-delete', [...state.selectedIds])"><Trash2 :size="17" /><span class="icon-button-label">{{ t("删除") }}</span></button>
      </div>
    </div>
    <div v-if="state.loading && !state.history.length" class="empty empty--page" role="status"><RefreshCw class="spinning" :size="36" /><h2>{{ t("正在读取历史") }}</h2></div>
    <div v-else-if="!state.history.length" class="empty empty--page">
      <History :size="36" />
      <!-- A search and a chip are different answers: when a chip is on,
           the panel named the kind instead of saying there is no history
           at all, because there is — just none of that kind. -->
      <h2 v-if="state.query">{{ t("没有匹配的记录") }}</h2>
      <h2 v-else-if="state.kind !== 'all'">{{ t("还没有{type}", { type: kindLabel(state.kind) }) }}</h2>
      <h2 v-else>{{ t("暂无历史记录") }}</h2>
      <!-- What the note says follows the heading: a search that found
           nothing is not an empty data directory, and telling the reader
           about the directory when a filter is what emptied the page
           sends them looking in the wrong place. -->
      <p class="note">{{ state.query
        ? t("多个关键词用空格分隔，需同时包含；也可以只留一个词再试。")
        : (state.kind !== 'all' ? t("换个过滤条件，或清除过滤查看全部内容。") : (native ? t('当前数据目录中没有可显示的记录') : t('桌面窗口连接后显示本地记录'))) }}</p>
    </div>
    <!-- The row's own body is the copy target.  Seventy per cent of a row
         is text and none of it responded: the only way to copy was one of
         six 17px icons at its right edge, next to the bin.  The click
         ignores the checkbox and the action column — those are their own
         controls and must not also copy — and the double click keeps
         opening the preview, so the quick path and the careful one do not
         fight over the same gesture. -->
    <template v-for="group in historyGroups" :key="group.key">
      <!-- The day the rows under it arrived on.  Sticky, because the
           heading is only useful while its rows are still on screen. -->
      <h3 v-if="group.label" class="history-day">
        <span>{{ group.label }}</span>
        <span class="history-day-count">{{ group.rows.length }}</span>
      </h3>
    <article v-for="{ item, index } in group.rows" :key="item.id" class="history-row"
      :class="{ 'history-row--kbd': state.kbdIndex === index }"
      :aria-current="state.kbdIndex === index ? 'true' : undefined"
      @contextmenu.prevent="emit('menu', $event, item)"
      @click="historyRowClick(item, $event)"
      @dblclick="historyRowPin(item, $event)"
      @mouseenter="showPreview(item)" @mouseleave="hidePreview(item)"
      @focusin="showPreview(item)" @focusout="hidePreview(item)">
      <label class="history-selection"><input type="checkbox" :aria-label="t('选择记录 {id}', { id: item.id })"
        :checked="state.selectedIds.includes(item.id)"
        :disabled="historyBusy || !item.id.trim() || (!state.selectedIds.includes(item.id) && selectLimitReached)"
        :title="!state.selectedIds.includes(item.id) && selectLimitReached ? selectLimitNote : undefined"
        :aria-describedby="!state.selectedIds.includes(item.id) && selectLimitReached ? 'history-select-reached' : undefined"
        @change="store.select(item.id, ($event.target as HTMLInputElement).checked)" /></label>
      <!-- The words the query matched, marked in place.  Segments, not
           markup: the text is a clipboard's content and may be anything. -->
      <div class="history-body">
      <img v-if="thumbFor(item)" class="history-thumb" :src="thumbFor(item)" alt="" loading="lazy" />
      <span v-else-if="isImageItem(item)" class="history-thumb history-thumb--empty" aria-hidden="true">🖼</span>
      <div class="history-content"><p><template
        v-for="(segment, index) in highlightSegments(previewLabel(item), state.query)"
        :key="index"><mark v-if="segment.hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></p>
        <span v-if="typeLine(item)" class="note">{{ typeLine(item) }}</span>
        <span class="history-meta">
          <span v-if="item.source_app" class="badge" :title="item.source_app">{{ item.source_app }}</span>
          <span v-if="item.source_title" class="history-source-title" :title="item.source_title">{{ item.source_title }}</span>
          <span v-if="item.source_name" class="badge" :title="item.source_name">{{ item.source_name }}</span>
          <!-- Which link the clip came in on, right beside the name of
               the device it came from.  The name alone cannot say it: a
               peer paired both ways sends over whichever is up, so
               "Away" over the relay and "Next room" over the cable are
               the same badge without this — and a push from the panel
               is a row whose name ("Web") names no device at all. -->
          <span v-if="item.transport" class="channel history-route" :title="routeTitle(item.transport)">
            <component :is="routeIcon(item.transport)" :size="12" />
            {{ routeLabel(item.transport) }}
          </span>
          <span v-if="pasteCount(item)" class="badge badge--count" :title="pasteCount(item)">{{ pasteCount(item) }}</span>
        </span>
      </div>
      </div>
      <time :datetime="new Date(item.timestamp * 1000).toISOString()">{{ dateTime(item.timestamp) }}</time>
      <div class="row-actions">
        <button v-if="!isRemoteFile(item)" class="icon-button" :aria-label="t('复制记录')" :title="t('复制记录')" :disabled="historyWritable" @click="store.copy(item, false)"><Check v-if="state.copiedId === item.id" :size="17" /><Copy v-else :size="17" /></button>
        <!-- The one row whose action is not 复制: the file is not on this
             machine, so the row's button starts the fetch-and-paste (1.3.1).
             It replaces rather than sits beside the copy button — two buttons
             on one row where only one of them can work is worse than one.  The
             explicit download (取回但不粘贴) stays in the row's menu, where the
             action for a file wanted on disk belongs. -->
        <button v-if="isRemoteFile(item)" class="icon-button" :aria-label="t('取回并粘贴')" :title="pasteRemoteTitle(item)" :disabled="historyWritable || !remoteFileActionable(item)" @click="emit('paste-to-app', item)"><ClipboardPaste :size="17" /></button>
        <button class="icon-button" :class="{ pinned: item.pinned }" :aria-label="item.pinned ? t('取消置顶') : t('置顶')" :title="item.pinned ? t('取消置顶') : t('置顶')" :disabled="historyWritable" @click="store.pin(item)"><Pin :size="17" /></button>
        <button class="icon-button" :class="{ pinned: favoritedIds.has(item.id) }" :aria-label="t('加入收藏夹')" :title="t('加入收藏夹')" :disabled="historyWritable || favoritingId === item.id" @click="favoriteRow(item)"><Star :size="17" :fill="favoritedIds.has(item.id) ? 'currentColor' : 'none'" /></button>
        <button v-if="isWebLink(item)" class="icon-button" :aria-label="t('在浏览器打开')" :title="t('在浏览器打开')" :disabled="historyWritable" @click="openHistoryLink(item)"><ExternalLink :size="17" /></button>
        <button class="icon-button" :aria-label="t('删除记录')" :title="t('删除记录')" :disabled="historyWritable" @click="emit('delete', item)"><Trash2 :size="17" /></button>
      </div>
      <!-- Non-interactive, so it never blocks the row's own controls or
           the row below it — the panel's card was `pointer-events: none`
           for the same reason. -->
      <!-- `aria-hidden` even when pinned: the row's own text is only
           clamped by CSS, so a screen reader already has all of it. -->
      <div v-if="previewCard?.id === item.id" class="history-preview" aria-hidden="true"
        :class="{ 'history-preview--pinned': previewPinned === item.id }"
        @click.stop @dblclick.stop>
        <!-- A pinned card is a thing the reader is reading, so it takes
             its own clicks rather than letting them fall through to the
             row underneath — that row's body copies.  It says how to
             let go, because nothing on screen else does. -->
        <p v-if="previewPinned === item.id" class="history-preview-pin">{{ t("已固定 · Esc 关闭") }}</p>
        <template v-if="previewCard.text">{{ previewCard.text }}</template>
        <template v-else-if="previewCard.kind !== ''">
          <img v-if="previewCard.image" class="history-preview-image" :src="previewCard.image"
            :alt="t('预览图')" />
          <ul v-if="previewCard.files.length" class="history-preview-files">
            <li v-for="file in previewCard.files" :key="file.name + file.size">
              <span class="history-preview-name" :title="file.name">{{ file.name }}</span>
              <span class="history-preview-size muted">{{ cardSize(file) }}</span>
              <!-- The one thing a file row cannot say and a card can:
                   whether the file is still there.  It is the answer to
                   "why did this fail" that a user is most often after. -->
              <span v-if="!file.exists" class="history-preview-gone">{{ t("已不存在") }}</span>
            </li>
          </ul>
          <p v-if="previewCard.total > previewCard.files.length" class="history-preview-more muted">
            {{ t("共 {count} 个文件", { count: previewCard.total }) }}
          </p>
          <!-- The picture's own size, not the card's: a 4000 px
               screenshot shown at card width is still a 4000 px
               screenshot, and that is the part worth knowing. -->
          <p v-if="previewCard.image && previewCard.width" class="history-preview-more muted">
            {{ t("{width} × {height} 像素", { width: previewCard.width, height: previewCard.height }) }}
          </p>
        </template>
      </div>
    </article>
    </template>
  </section>
  <footer class="pagination">
    <span>{{ state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)}` : '0' }} / {{ state.total }}</span>
    <button class="icon-button" :aria-label="t('上一页')" :title="t('上一页')" :disabled="state.offset === 0 || historyBusy" @click="turnPage(-1)"><ChevronLeft :size="18" /></button>
    <button class="icon-button" :aria-label="t('下一页')" :title="t('下一页')" :disabled="state.offset + state.limit >= state.total || historyBusy" @click="turnPage(1)"><ChevronRight :size="18" /></button>
  </footer>
</template>
