<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  History, Monitor, Search, RefreshCw, Pin, Trash2, ChevronLeft, ChevronRight, Eraser, FileDown, Download, Upload,
  ShieldCheck, ShieldAlert, ShieldOff, Eye, EyeOff, LockKeyhole, AlertCircle, X, Circle, Copy, Check, Link, CloudOff, Laptop, PinOff, Star, Settings as SettingsIcon, Save, FileUp, FolderOpen, MessageCircle, Plug, PlugZap, RotateCcw, Activity, ArrowUpDown, Loader2, Fingerprint, Globe, SendHorizontal, Stethoscope, Wrench, Info, QrCode, ExternalLink, Sparkles, Clock, Smartphone, Pencil, Pause, MoreHorizontal, Keyboard, ClipboardPaste, Plus, Play, Square, Heart, ChevronDown, ChevronUp,
} from "@lucide/vue";
import logo from "../../assets/icon.svg";
import { bridge, inDesktop } from "./api/bridge";
import type { Device, DeviceCertificate, DeviceProbeResult, DiagnosticAction, DiagnosticCheck, DiagnosticItem, DiagnosticsReport, HistoryItem, HistoryPreview, HistoryPreviewFile, RelayTestResult } from "./api/types";
import { LOCALES, LOCALE_NAMES, currentLocale, setLocale, t } from "./i18n";
import { dateTime, dayLabel, isPlaceholderPreview, previewText, shortTime, size } from "./i18n/format";
import { createApplicationStore } from "./stores/application";
import { highlightSegments } from "./lib/highlight";
import { requestAttention } from "./lib/attention";
import { formatPairingCode, isPairingCodeComplete } from "./lib/pairing-code";
import { PAIRING_LIVE_STATUSES, chatReachable, deviceLabel, deviceRank, deviceStatus, fileReachable, pairingInFlight, platformLabel, renameWritesAlias } from "./lib/device-row";
import { openContextMenu, type ContextMenuItem } from "./lib/context-menu";
import { copyText } from "./lib/clipboard";
import { applyAppearanceTheme } from "./lib/theme";
import {
  errorCode, errorMessage, followPasteFetch, isPasteConfirmRequired, isRemoteFileRow,
  pasteFailureText, pastePrepareErrorText, type PasteFetchProgress, type PasteFetchRun,
} from "./lib/paste-transfer";
import { SEARCH_DEBOUNCE_MS } from "./lib/search-box";
import { announce, clearStatus, statusMessage } from "./lib/status";
import { useDialog } from "./lib/use-dialog";
import { deliveryIcon, deliveryLabel } from "./stores/delivery";
import FavoritesView from "./components/FavoritesView.vue";
import HistoryView from "./components/HistoryView.vue";
import TransfersView from "./components/TransfersView.vue";
import ChatView from "./components/ChatView.vue";
import OverviewView from "./components/OverviewView.vue";
import NoticeStack from "./components/NoticeStack.vue";
import QuickSearch from "./components/QuickSearch.vue";
import AiView from "./components/AiView.vue";
import AppDialog from "./components/AppDialog.vue";
// The row's two names, shared with the quick search: one rule for what a
// clip with no text of its own is called.
import { previewLabel, typeLabel } from "./lib/history-label";
import { createHistoryActions } from "./lib/history-actions";
import ContextMenu from "./components/ContextMenu.vue";
import ErrorDetail from "./components/ErrorDetail.vue";

const store = createApplicationStore();
const { state } = store;
/** Which page is on screen.  Named by the same list the sidebar draws from, so
 * a page cannot exist in one and not the other.  The window opens on the
 * overview, which is where the legacy window opened too: it is the one page
 * that answers "what is this machine doing" without being asked. */
const tab = ref<(typeof PAGES)[number]>("overview");
/** The window's one scrolling region.  Every page renders inside it, so its
 * scroll position outlives the page that set it. */
const contentEl = ref<HTMLElement | null>(null);
/** The page's own `<h1>`, focused when the page changes so the change is read. */
const pageHeadingEl = ref<HTMLElement | null>(null);
/** The failure band at the top of the window.
 *
 * A settings save that is refused can be refused on the tenth card, forty rows
 * below the line that says why.  The band is a sibling of the page, not of the
 * card, so nothing scrolls it into view by itself; a save that fails scrolls it
 * in and puts the caret on it (`focusSettingsError`), which is also how a screen
 * reader gets told without hunting. */
const errorBandEl = ref<HTMLElement | null>(null);
async function focusSettingsError() {
  await nextTick();
  const band = errorBandEl.value;
  if (!band) return;
  band.scrollIntoView({ block: "nearest" });
  band.focus({ preventScroll: true });
}
/** Entering a page starts at its heading.
 *
 * `.content` is a single element reused by all eight pages: a position left on
 * the history page was simply inherited by whatever came next, so opening
 * Settings from row 30 of a long history landed the reader halfway down a card
 * — occasionally next to the factory-reset button.  Every page opens at its own
 * title instead.  After `nextTick`, because the new page has to be the one
 * being measured: the old page's height is what the element still has when the
 * switch happens. */
watch(tab, () => {
  void nextTick(() => {
    if (contentEl.value) contentEl.value.scrollTop = 0;
    // The heading is where a page begins, and focusing it is how a screen reader
    // learns that it did: without this the focus stayed on the sidebar button and
    // the new `<h1>` was never read, so every page change was silent.  `-1`
    // because it is not in the tab order — this is a stop, not a stop along the
    // way — and `preventScroll` because the scroll above is the deliberate one.
    // Only when the focus is still on the way in — the sidebar, or nowhere —
    // rather than somewhere the new page was asked to put it: Ctrl+F arrives as
    // a page change plus a focus, and taking the focus back from the search box
    // it just opened would leave the reader typing into a heading.
    const active = document.activeElement as HTMLElement | null;
    if (!active || active === document.body || active.closest(".sidebar")) {
      pageHeadingEl.value?.focus({ preventScroll: true });
    }
    if (typeof document !== "undefined") document.title = `${heading.value} · ClipSync`;
  });
});
const settings = ref<Record<string, any>>({});
// A computed, not a plain array: the labels are translated, so they must
// re-evaluate when the locale changes.
// Each category carries an example of what it catches.  Six bare names left the
// reader guessing at the boundary — whether 电子邮箱 covers a bare address or
// only a mailto link, whether 密码 means "any line containing the word".  The
// default stays all-off: turning a category on changes what leaves this machine,
// which is not a decision a first run should make on the reader's behalf.
const filterCategories = computed<Array<[string, string, string]>>(() => [
  ["ssn", t("身份证号"), t("18 位居民身份证号，如 11010519491231002X")],
  ["phone", t("手机号"), t("中国大陆 11 位手机号，如 138 0013 8000")],
  ["credit_card", t("银行卡号"), t("16–19 位银联/银行卡号，如 6222 0212 3456 7890")],
  ["api_key", t("API 密钥"), t("常见令牌前缀，如 sk-…、ghp_…、AKIA…")],
  ["email", t("电子邮箱"), t("形如 name@example.com 的地址")],
  ["private_key", t("私钥"), t("-----BEGIN … PRIVATE KEY----- 开头的整段")],
  ["password", t("密码"), t("形如 password=… / 密码：… 的赋值行")],
]);
const settingsBusy = ref(false);
const settingsSaved = ref(false);
/** When the last save landed, and whether the last attempt failed.
 *
 * 「所有更改都已保存」 answers the question the bar was put there for, but not the
 * one right after it: *when*.  A reader who made a change, looked away and came
 * back had no way to tell whether the line was written a second ago or before
 * they left.  The failure half matters more: a save that was refused used to
 * leave the bar saying nothing new — the red band at the top of the window was
 * the only sign, and it can be scrolled out of sight from card ten. */
const settingsSavedAt = ref(0);
const settingsSaveFailed = ref(false);
const settingsSavedTime = computed(() => {
  if (!settingsSavedAt.value) return "";
  return new Date(settingsSavedAt.value)
    .toLocaleTimeString(currentLocale.value, { hour: "2-digit", minute: "2-digit" });
});
/** The relay broker test: busy, its answer, and its failure.
 *
 * The answer is the sidecar's whole payload rather than a sentence, because the
 * reader needs the per-broker rows -- which endpoint, how fast, and why not --
 * and the sentence is only the line above them. */
const relayTestBusy = ref(false);
const relayTestResult = ref<RelayTestResult | null>(null);
const relayTestError = ref("");
/** The form as the sidecar last confirmed it, serialized — `null` until the
 * first load.  What the save bar compares against to say whether anything on
 * the page is still unsaved.  A string rather than a copy of the object,
 * because the form is one flat record of primitives and arrays and a deep
 * watch would fire on every keystroke to compare it anyway. */
const savedSettings = ref<string | null>(null);
// The log levels the sidecar accepts (settings_window.log_levelOptions in the
// legacy panel offers the same four).
const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
// The settings page's rail: one jump per card, in the order the cards appear,
// gathered into the four questions a reader arrives with — what this device is,
// what it talks to, what it keeps, and what it is running.
// A flat run of cards was a list to scan; a group is a list to aim at.  Each
// id is the `settings-<id>` anchor its card carries.
//
// There were five questions until the phone service moved to the devices page:
// it was the only thing on this page that *ran* on this machine, and the group
// it anchored kept one member afterwards — the translation card, which is a
// service this machine calls rather than one it hosts, and so belongs with what
// it talks to.  That leaves four groups over twelve cards, and no group holding
// a single entry.
const settingsGroups = computed<Array<{ label: string; items: Array<{ id: string; label: string }> }>>(() => [
  { label: t("通用"), items: [
    { id: "general", label: t("常规") },
    { id: "history", label: t("剪贴板历史") },
    // 「粘贴与录制」 sits with the history card rather than in a group of its own:
    // what is kept and what happens when a clip is taken back are the same
    // question asked at two ends, and this group is the one about them.
    { id: "recording", label: t("粘贴与录制") },
    // The file limits are the third question about a clipboard: what may leave
    // it, what may arrive, and how large either may be (1.3.1).
    { id: "files", label: t("文件") },
  ] },
  { label: t("连接"), items: [
    { id: "sync", label: t("同步") },
    { id: "discovery", label: t("局域网发现") },
    { id: "advanced", label: t("网络与高级") },
    { id: "translation", label: t("翻译") },
  ] },
  { label: t("数据"), items: [
    { id: "security", label: t("安全") },
    { id: "backup", label: t("数据备份") },
  ] },
  { label: t("系统"), items: [
    { id: "update", label: t("软件更新") },
    { id: "diagnostics", label: t("诊断与维护") },
  ] },
]);
/** The settings card the rail is pointing at, and the only one on screen.
 *
 * The page is ten cards long and each of them is a form's worth of
 * controls, so a single column of them was a page the reader had to scroll to
 * reach anything on — and the Save bar at the very end of it, ten cards
 * away from the field that was just changed.  A mature settings window shows
 * one category at a time and puts the way to the others beside it; this is that
 * window: the rail picks a card, the card is what the page is.
 *
 * It is also the whole of the rail's state now.  What it replaced was an
 * `IntersectionObserver` folding the cards' positions into "which one is the
 * reader looking at" — machinery a scrolling page needs and a switching one
 * does not, for an answer that here is exactly the button that was clicked.
 */
const settingsSection = ref("general");
/** What the reader has typed into the rail's search box. */
const settingsQuery = ref("");
/** Which cards hold that query, and how many places in each.
 *
 * Measured by a pass over the rendered cards rather than kept in a table beside
 * them: the legacy Settings window collected its own widgets' text for the same
 * search, and a hand-written index would be one more thing to keep in step with
 * a page whose cards go on changing.
 */
const settingsMatches = ref<Record<string, number>>({});
const settingsSearching = computed(() => settingsQuery.value.trim().length > 0);
/** Whether the sync card's relay boxes are unfolded.
 *
 * Six fields and two paragraphs of explanation sat between the two switches a
 * reader opens that card for and the rest of it, so the common settings were
 * pushed off the first screen by the ones almost nobody changes.  They fold;
 * the switch that turns internet sync on does not.  A search opens the fold on
 * its own, because a match inside a closed box is a match nobody can see.
 * See U186. */
const relayAdvancedOpen = ref(false);
const relayShown = computed(() => relayAdvancedOpen.value || settingsSearching.value);
/** How many cards the query reaches. */
const settingsMatchTotal = computed(() => Object.keys(settingsMatches.value).length);
/** How many of a group's cards hold the query.  The name over a run of chips
 * needs it for the same reason the chips do: while a query is up, a name left
 * bright over chips the query dimmed would point at nothing. */
function settingsGroupMatches(group: { items: Array<{ id: string }> }): number {
  return group.items.reduce((total, item) => total + (settingsMatches.value[item.id] || 0), 0);
}
/** The form the search reads.  A template ref rather than a `document` lookup:
 * the settings page is one tab of several, and in a test it is not attached to
 * the document at all. */
const settingsForm = ref<HTMLFormElement | null>(null);
/** The library pane, so Ctrl+F can reach the search box it carries: the chord
 * asks about the page being read, and that page has one of its own. */
const favoritesView = ref<InstanceType<typeof FavoritesView> | null>(null);
/** The history page, so the window's chords can reach its box and its card.
 *  The page decides what a keystroke does there; `App.vue` only decides when. */
const historyView = ref<InstanceType<typeof HistoryView> | null>(null);
/** The rail's own search box, so Ctrl+F has something to reach while this page
 * is the one on screen. */
const settingsSearchInput = ref<HTMLInputElement | null>(null);
/** Whether a card is on screen: the one the rail points at, or — while a query
 * is up — every card that answered it.
 *
 * A search is the one time the page shows more than one card, and it shows
 * exactly the cards that hold the query.  That is the same trade the search has
 * always made, stated the other way round: it never hides a match, and with one
 * card on screen by default "showing the matches" and "hiding the rest" are the
 * same act.  The rail still counts each card's places, so a match the reader
 * cannot see right now is still a card they can open.
 */
function showSettingsCard(id: string): boolean {
  if (!settingsSearching.value) return settingsSection.value === id;
  // A query nothing holds shows the card the switcher was on rather than an
  // empty page under a rail that already says nothing matched: the reader is
  // told the query missed, and is left where they were.
  if (!settingsMatchTotal.value) return settingsSection.value === id;
  return !!settingsMatches.value[id];
}
/** Open a card from the rail.
 *
 * Opening one ends a search, because the rail stays a list of every card while
 * a query is up — including the ones it dimmed — and an entry that could not be
 * opened would be a control that does nothing.  The reader who wanted the card
 * they clicked gets it; the reader who wanted the query has the box in front of
 * them and the counts on the rail.
 */
function openSettingsCard(id: string) {
  settingsSection.value = id;
  if (settingsSearching.value) clearSettingsSearch();
}
/** Open the settings page on its diagnostics card.
 *
 * The overview's network-health chip reports a verdict; the checks behind that
 * verdict live on the card, and the chip is the only place they are reachable
 * from one click.  Nothing is read here — the card's own button runs the
 * suite, and a report read on the way in would be a second copy of what the
 * chip already has. */
function openDiagnosticsCard() {
  void openSettings();
  openSettingsCard("diagnostics");
}
/** 查看原因, from a relay that is not up.
 *
 * The devices page says 本机中继：离线 and every device under it reads 离线, and
 * there was nothing to press: the only control that can tell a broker that is
 * down from a network that blocks it is the test on the settings page's own
 * card, four chips away in the strip.  It is run on the way in,
 * because the reader who pressed 查看原因 wants the answer, not the address.
 * See U126. */
function openRelayDiagnostics() {
  void openSettings();
  openSettingsCard("advanced");
  void testRelayBrokers();
}
/** What a page does on the way in.
 *
 * The settings page's own follow-up is the search pass: the cards are rendered
 * fresh each time the page opens, so the marks the search put on them went with
 * the last one; the query is kept, the pass is not.  Nothing to redo when
 * nothing is typed — and the page's other state is the form's, which is not
 * rebuilt when the reader walks away from it.
 *
 * The devices page's is the state of the two cards that carry the ways another
 * machine joins this one: the peers the relay is holding messages for, and
 * whether the phone service is up.  They are read on the way in rather than
 * kept behind a button, because a card that reads 已停止 until somebody presses
 * 读取手机服务状态 is reporting on the page rather than on the service — and
 * the rows the service's own answer gates (its address, its QR code) are rows
 * a reader cannot find at all until an unrelated button is pressed.
 *
 * The overview carries the phone service's own read of itself for the same
 * reason, and reads nothing else here: its counters are its own, and it reads
 * them when it is mounted rather than when the page is chosen.
 */
watch(tab, async (page) => {
  if (page === "overview") { void refreshCompanion(); return; }
  if (page === "devices") {
    await nextTick();
    await Promise.all([refreshInternetPairing(), refreshCompanion()]);
    return;
  }
  if (page !== "settings") return;
  await nextTick();
  if (settingsSearching.value) applySettingsSearch(settingsQuery.value);
});
/** The settings search, the way the legacy window's own search box worked: it
 * marked what it found and it never hid anything.
 *
 * A filtered-away field is a field the reader cannot find a second time, and a
 * settings page is exactly where a reader goes looking for something they
 * cannot name — so the query says which cards hold it and how much of each,
 * and leaves every card where it was.
 *
 * The form's text is read once per search rather than once per keystroke.
 *
 * The pass below reads `textContent` on every node in the form — three to five
 * thousand of them across ten cards — and it ran on every input event, which
 * with an IME means every composition keypress.  The names and labels in the
 * form did not change between those events; only the query did.  The index is
 * built when a search starts and dropped when it ends, which is precisely the
 * window in which the text cannot have changed.
 */
type SettingsTextEntry = { node: Element; text: string };
let settingsTextIndex: Map<Element, SettingsTextEntry[]> | null = null;
/** The node's own text, and whether a child carries it too.
 *
 * The deepest element only: a card whose text matched because a row inside it
 * did is the same match twice, and a nested card would otherwise report a
 * number several times its size. */
function settingsIndexFor(card: Element): SettingsTextEntry[] {
  settingsTextIndex ??= new Map();
  const cached = settingsTextIndex.get(card);
  if (cached) return cached;
  const entries = Array.from(card.querySelectorAll("*"))
    .map((node) => ({ node, text: (node.textContent || "").toLowerCase() }));
  settingsTextIndex.set(card, entries);
  return entries;
}
function settingsHits(card: Element, query: string): Element[] {
  const matching = new Set<Element>();
  for (const entry of settingsIndexFor(card)) {
    if (entry.text.includes(query)) matching.add(entry.node);
  }
  const hits: Element[] = [];
  for (const node of matching) {
    let carriesItBelow = false;
    for (const child of Array.from(node.children)) {
      if (matching.has(child)) { carriesItBelow = true; break; }
    }
    if (!carriesItBelow) hits.push(node);
  }
  return hits;
}
/** Count the query in every card, and mark the places it was found.
 *
 * The pass reads the whole form, hidden cards included: a card the switcher is
 * not showing is still a card, and its count is what tells the reader there is
 * something to open.
 */
function applySettingsSearch(query: string) {
  const form = settingsForm.value;
  if (!form) { settingsMatches.value = {}; return; }
  for (const marked of Array.from(form.querySelectorAll(".settings-hit"))) {
    marked.classList.remove("settings-hit");
  }
  const text = query.trim().toLowerCase();
  const counts: Record<string, number> = {};
  if (!text) { settingsMatches.value = counts; return; }
  for (const card of Array.from(form.querySelectorAll(".settings-section"))) {
    const hits = settingsHits(card, text);
    if (!hits.length) continue;
    counts[card.id.replace(/^settings-/, "")] = hits.length;
    for (const hit of hits) hit.classList.add("settings-hit");
  }
  settingsMatches.value = counts;
}
/** Enter: apply what is typed and open the first card that holds it, in the
 * rail's own order — the reason the counts are on the rail at all.  It ends the
 * search like a rail click does, so the reader lands on the card rather than on
 * a page still showing every match. */
function jumpToSettingsMatch() {
  if (!settingsSearching.value) return;
  applySettingsSearch(settingsQuery.value);
  for (const group of settingsGroups.value) {
    for (const section of group.items) {
      if (settingsMatches.value[section.id]) { openSettingsCard(section.id); return; }
    }
  }
}
/** The clear button and Escape:  a query that cannot be dropped is a page the
 * reader has to reload to get back. */
function clearSettingsSearch() {
  settingsQuery.value = "";
  applySettingsSearch("");
}
/** A word, not a keystroke: the field is typed into with an IME on Chinese and
 * each composition event is not a query anyone asked for.  The pause after a
 * word is what the reader is waiting for; Enter and the rail still apply
 * immediately, because those are decisions rather than typing. */
let settingsSearchTimer: ReturnType<typeof setTimeout> | undefined;
watch(settingsQuery, (query) => {
  clearTimeout(settingsSearchTimer);
  settingsSearchTimer = setTimeout(() => {
    // The form's text is re-read at the start of every search: an index kept
    // across a locale switch would answer in the language the form no longer
    // speaks.
    if (query.trim()) settingsTextIndex = null;
    applySettingsSearch(query);
  }, 150);
}, { flush: "post" });
onUnmounted(() => clearTimeout(settingsSearchTimer));
/** Clamp a number input to the range the sidecar enforces.
 *
 * The form's min/max attributes only mark the field invalid; a cleared input
 * yields "", and the sidecar rejects out-of-range values with a validation
 * error, so normalize before sending — the server still guards the same bounds.
 */
function clampNumber(value: unknown, fallback: number, min: number, max: number): number {
  // A cleared input yields "" — Number("") is 0, which would silently clamp to
  // the minimum, so treat anything blank as "not provided" and keep the loaded
  // value.
  if (value === "" || value === null || value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}
/** The brokers a multi-line field stands for: one per line or comma, trimmed.
 *
 * Named once because two callers read the same field for different reasons --
 * the save that writes it and the test that probes it -- and a test that
 * disagreed with the save about what the list is would be testing something
 * other than what gets stored.
 */
function brokerList(value: unknown): string[] {
  return String(value || "").split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
}
/** The relay's per-message ceiling, in the three units it is spoken in.
 *
 * The sidecar stores bytes and enforces 32 KiB – 1 MiB (config._FIELD_RANGES,
 * mirrored by the RPC validator, the web API's _RANGE_LIMITS and the backup
 * schema); the row is typed in kilobytes, because that is the unit a broker's
 * price list uses — "≤ 64 KB per message" — and the only unit this number is
 * ever read in.  256 KiB is what the public brokers carry and what the app
 * assumed before the setting existed, so it is the fallback for a sidecar too
 * old to send the field at all.
 */
const DEFAULT_RELAY_MAX_MESSAGE_BYTES = 256 * 1024;
const MIN_RELAY_MAX_MESSAGE_KB = 32;
const MAX_RELAY_MAX_MESSAGE_KB = 1024;
/* The other half of the same setting: how fast those chunks may be offered.
 *
 * A relay that refuses an oversized message also quietly drops most of a burst
 * sent too soon after the last one — measured against the shipped relay, 24
 * chunks back to back arrived 2 strong while the same chunks offered at ~86
 * KB/s arrived 40/40 — so a file transfer paces itself at this rate, and backs
 * further off by itself whenever chunks come back unconfirmed.  This number is
 * the ceiling it starts from and never exceeds; 64 KiB/s (512 kbit/s) is what
 * the shipped relay carries.  Kilobytes per second in the box, bytes per second
 * on disk, like the row above.
 *
 * It is a setting rather than a constant because the pace a relay will carry is
 * a property of that relay, which no broker publishes: a faster private relay
 * wants a bigger number here, and the transfer still finds its own level.
 */
const DEFAULT_RELAY_MAX_BYTES_PER_SECOND = 64 * 1024;
const MIN_RELAY_RATE_KB = 4;
const MAX_RELAY_RATE_KB = 2048;
/** The recording rules' one number: how large a non-file clip may be before it
 *  is dropped, in megabytes, and the bounds the sidecar's `FIELD_RULES` put on
 *  it (0 = no limit).  Eight megabytes is the shipped default — a screenshot or
 *  a long article fits, a mislabelled archive does not — and a config that
 *  predates the field reads as that default rather than as "no limit", which is
 *  a policy the form would be claiming the reader chose.  See
 *  `buildSettingsForm` and `saveSettings`.
 */
const DEFAULT_MAX_NON_FILE_PASTE_MB = 8;
const MIN_MAX_NON_FILE_PASTE_MB = 0;
const MAX_MAX_NON_FILE_PASTE_MB = 1024;
/** The three file-transfer limits (1.3.1), in megabytes.
 *
 *  `max_file_mb` is the policy ceiling on one file (`0` = whatever the protocol
 *  allows), `confirm_large_file_mb` is the threshold above which a paste asks
 *  before it transfers (`0` = never ask), and `relay_paste_max_mb` caps an
 *  on-demand paste that has to travel over the internet relay (`0` = no extra
 *  cap).  The 65536 upper bound is the sidecar's typo guard past the protocol's
 *  own ceiling (`FIELD_RULES`), not a limit this form invents.
 */
const DEFAULT_MAX_FILE_MB = 2048;
const DEFAULT_CONFIRM_LARGE_FILE_MB = 512;
const DEFAULT_RELAY_PASTE_MAX_MB = 50;
const MAX_FILE_MB = 65536;
// Security: the encryption password keeps the rules the pairing passphrase had
// (settings_window.password_rule_* in the legacy panel; the sidecar enforces
// the same rules server-side through netpair_passphrase_error).  The password
// is submitted with the settings form — exactly like the web panel — and the
// field is cleared once the save confirms.
const securityPassword = ref("");
const securityPasswordConfirm = ref("");
const securityBusy = ref(false);
const passwordRules = computed<Array<[string, boolean]>>(() => {
  const pw = securityPassword.value;
  return [
    [t("长度至少 12 位"), pw.length >= 12],
    [t("包含大写字母"), /\p{Lu}/u.test(pw)],
    [t("包含小写字母"), /\p{Ll}/u.test(pw)],
    [t("包含数字"), /\p{N}/u.test(pw)],
    [t("包含特殊字符"), /[^\p{L}\p{N}\s]/u.test(pw)],
  ];
});
const passwordValid = computed(() => securityPassword.value.length > 0 && passwordRules.value.every(([, met]) => met));
const passwordMismatch = computed(() => securityPasswordConfirm.value.length > 0 && securityPasswordConfirm.value !== securityPassword.value);
/** Whether the two password boxes show their text.  One switch for both: they
 *  hold the same secret, and showing one while masking the other helps nobody. */
const securityPasswordShown = ref(false);
const passwordBlocked = computed(() => securityPassword.value.length > 0 && (!passwordValid.value || passwordMismatch.value));
/** Why 保存设置 will not go, said where the button is.
 *
 * The rule it is breaking lives on the 安全 card and the button rides the foot
 * of every card, so a reader who typed a password and then carried on to
 * another section met a greyed-out button beside a page that still said
 * 有未保存的更改 — two facts with nothing tying them together, and the thing
 * to fix was off screen.  The mismatch is the sharper of the two answers and
 * is given first: it is a difference between two boxes rather than a list of
 * rules the reader has to go back and read. */
const passwordBlockedReason = computed(() => {
  if (!passwordBlocked.value) return "";
  return passwordMismatch.value
    ? t("「安全」里两次输入的加密密码不一致，改好之前不能保存。")
    : t("「安全」里的加密密码还不满足下面的要求，改好之前不能保存。");
});
const clearPasswordOpen = ref(false);
const factoryResetOpen = ref(false);
const recoverOpen = ref(false);
// The sidecar reports DATA_INVALID when it refuses to start on its own data
// directory; nothing else it can report is repaired the same way.
const dataInvalid = computed(() => state.error?.code === "DATA_INVALID");
const recoverDialog = useDialog(recoverOpen);
async function confirmRecover() {
  if (state.pending) return;
  recoverOpen.value = false;
  await store.recoverData();
}
/** The other way the sidecar fails to start: another instance is holding the
 * data directory.  Its answer names the cause but not the culprit, and the
 * window cannot enumerate processes — so it explains how to find one instead of
 * guessing, and does not end it on the reader's behalf: ending the wrong
 * process is worse than the wait. */
const dataInUse = computed(() => state.error?.code === "DATA_IN_USE");
const dataInUseOpen = ref(false);
const dataInUseDialog = useDialog(dataInUseOpen);
const clearPasswordDialog = useDialog(clearPasswordOpen);
const factoryResetDialog = useDialog(factoryResetOpen);
async function clearPassword() {
  if (securityBusy.value) return;
  clearPasswordOpen.value = false;
  securityBusy.value = true;
  try {
    // An empty password means "unchanged", so clearing needs the explicit
    // action key (same payload the web panel sends).
    const result = await bridge.updateSettings({ password: "", clear_password: true }) as { password_set?: boolean };
    // Saved by its own call, not by the page's save button, so the form follows
    // without the bar offering to save it again.
    foldSavedField("password_set", result.password_set === true);
    securityPassword.value = "";
    securityPasswordConfirm.value = "";
  } catch (error) { store.setError(error); }
  finally { securityBusy.value = false; }
}
async function confirmFactoryReset() {
  if (securityBusy.value || !factoryResetConfirmed.value) return;
  factoryResetOpen.value = false;
  securityBusy.value = true;
  try {
    // The host deletes every data file, clears the webview's own storage and
    // relaunches: nothing after this call is guaranteed to run.
    await bridge.factoryReset();
  } catch (error) {
    securityBusy.value = false;
    store.setError(error);
  }
}
/** The word that has to be typed before the reset will fire.
 *
 * It is the verb the button carries, in the reader's language, rather than the
 * device name: a name is something they would have to go and look up, and a
 * confirmation that has to be looked up is one people copy from the sentence
 * above it without reading either.  "Delete" is already the reader's own word
 * for what is about to happen, so typing it is a decision rather than a
 * transcription exercise.
 */
const factoryResetWord = computed(() => t("删除"));
const factoryResetConfirm = ref("");
const factoryResetConfirmed = computed(() =>
  factoryResetConfirm.value.trim().toLocaleLowerCase() === factoryResetWord.value.toLocaleLowerCase());
function closeFactoryReset() {
  factoryResetOpen.value = false;
}
/** Put a backup on disk before the reset wipes the data.
 *
 * The dialog offers this because it is the last moment it can be offered: after
 * the reset the history, the favourites and the pairings are gone, and a reader
 * who wanted a copy of them had no way to say so from here. */
const factoryResetBackupBusy = ref(false);
const factoryResetBackupMessage = ref("");
async function backupBeforeReset() {
  if (factoryResetBackupBusy.value) return;
  factoryResetBackupBusy.value = true;
  factoryResetBackupMessage.value = "";
  try {
    const result = await bridge.createBackup();
    factoryResetBackupMessage.value = t("备份已创建：{path}", { path: result.backup_path });
    void refreshBackups();
  } catch (error) { store.setError(error); }
  finally { factoryResetBackupBusy.value = false; }
}
// A half-typed word must not survive the dialog being dismissed and reopened:
// the next open has to ask again, not find the answer already in the box.
watch(factoryResetOpen, (open) => {
  if (open) {
    factoryResetConfirm.value = "";
    factoryResetBackupMessage.value = "";
  }
});
const companion = ref<Awaited<ReturnType<typeof bridge.companionStatus>> | null>(null);
const companionPort = ref(8080);
const companionBusy = ref(false);
let companionGeneration = 0;
const companionRotatePending = ref(false);
const companionRotateDialog = useDialog(companionRotatePending);
async function rotateCompanionToken() {
  if (!companionRotatePending.value || companionBusy.value || !companion.value?.running) return;
  companionRotatePending.value = false;
  await configureCompanion(true, true);
}
const companionClearPending = ref(false);
const companionClearDialog = useDialog(companionClearPending);
/**
 * Clear the companion's access token — the legacy web panel's 清除访问令牌.
 *
 * It asks first, the way rotating does, because it is the one companion action
 * that lowers a guard rather than moving it: with the token gone the service
 * lets in every device that can reach the port, and the only thing standing
 * between a phone and the clipboard history is the network boundary.
 */
async function clearCompanionToken() {
  if (!companionClearPending.value || companionBusy.value || !companion.value?.running) return;
  companionClearPending.value = false;
  await configureCompanion(true, false, true);
  if (companion.value && !companion.value.access_url) announce(t("访问令牌已清除，任何能访问该端口的设备都可以直接连接"));
}
/** A poll must not overwrite a field the reader is in the middle of typing. */
function companionFormFocused(): boolean {
  const active = document.activeElement as HTMLElement | null;
  if (!active || !active.closest(".companion-page")) return false;
  return ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
}
async function refreshCompanion() {
  // Nothing to read on a runtime with no phone service: the read would come
  // back refused, and the window would carry an error band for a card that is
  // not even on screen.
  if (!ready.value || !state.status?.capabilities?.includes("companion.status")) return;
  const generation = ++companionGeneration;
  try {
    const result = await bridge.companionStatus();
    if (generation !== companionGeneration) return;
    companion.value = result;
    companionPort.value = companion.value.port;
    companionRemotePage.value = companion.value.remote.page_url || COMPANION_REMOTE_PAGE_DEFAULT;
    companionRemoteMode.value = companion.value.remote.mode;
    // A status read must not eat what the user is halfway through typing.
    if (!companionRemoteBrokersDirty.value) {
      companionRemoteBrokers.value = (companion.value.remote.brokers ?? []).join(", ");
    }
  } catch (error) {
    if (generation === companionGeneration) store.setError(error);
  }
}
async function configureCompanion(enabled: boolean, rotateToken = false, clearToken = false) {
  if (companionBusy.value) return;
  const generation = ++companionGeneration;
  companionBusy.value = true;
  try {
    // Only the port field itself applies the typed port.  Rotating and clearing
    // act on the running service, so they keep the port it is running on rather
    // than a number the user may have typed and not applied.
    const port = enabled && !rotateToken && !clearToken ? companionPort.value : companion.value?.port ?? companionPort.value;
    const result = await bridge.configureCompanion(enabled, port, rotateToken, clearToken);
    if (generation !== companionGeneration) return;
    companion.value = result;
    companionPort.value = companion.value.port;
    companionRemotePage.value = companion.value.remote.page_url || COMPANION_REMOTE_PAGE_DEFAULT;
    companionRemoteMode.value = companion.value.remote.mode;
    // A status read must not eat what the user is halfway through typing.
    if (!companionRemoteBrokersDirty.value) {
      companionRemoteBrokers.value = (companion.value.remote.brokers ?? []).join(", ");
    }
  } catch (error) {
    if (generation !== companionGeneration) return;
    store.setError(error);
    await refreshCompanion();
  } finally { companionBusy.value = false; }
}

/* Remote phone access: the same page carried over an MQTT tunnel when the
 * phone is not on this network.  It is a separate switch from the LAN
 * companion above because it sends this machine's traffic to a third-party
 * broker (encrypted) and has its own page URL and QR code. */
const companionRemotePage = ref("https://clipsync-companion.pages.dev/");
/** The static phone page is deployed by the project; the reader never has to
 *  know or type its address.  A custom value already in the config still wins,
 *  but a fresh install starts on the published page. */
const COMPANION_REMOTE_PAGE_DEFAULT = "https://clipsync-companion.pages.dev/";
const companionRemoteMode = ref<"readonly" | "full">("readonly");
/* The relay brokers remote access tries, as the user edits them.  The default
 * list lives in the sidecar and comes back with every status read, so
 * "restore defaults" is the same list the tunnel itself would fall back to. */
const companionRemoteBrokers = ref("");
const companionRemoteBrokerDraft = ref("");
const companionRemoteBrokersDirty = ref(false);
const companionRemoteBusy = ref(false);
const companionRemoteQrOpen = ref(false);
const companionRemoteDialog = useDialog(companionRemoteQrOpen);
const companionRemoteQrBusy = ref(false);
const companionRemoteQrImage = ref("");
const companionRemoteQrUrl = ref("");
const companionRemoteQrMessage = ref("");

function remoteLastSeenLabel(): string {
  const seen = companion.value?.remote?.last_seen;
  return seen ? new Date(seen * 1000).toLocaleString() : t("暂无");
}

/* One broker per line or comma-separated — both are what a pasted list looks
 * like, and neither costs the other anything. */
function parseRemoteBrokers(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

/** The relay list as the list box draws it.  The form still stores one string
 * because that is what the save carries; the list is its parsed view. */
const companionRemoteBrokerList = computed(() => parseRemoteBrokers(companionRemoteBrokers.value));

function addCompanionRemoteBroker(): void {
  const broker = companionRemoteBrokerDraft.value.trim();
  if (!broker || companionRemoteBusy.value) return;
  companionRemoteBrokerDraft.value = "";
  if (companionRemoteBrokerList.value.includes(broker)) return;
  companionRemoteBrokers.value = [...companionRemoteBrokerList.value, broker].join("\n");
  companionRemoteBrokersDirty.value = true;
}

function removeCompanionRemoteBroker(index: number): void {
  const next = companionRemoteBrokerList.value.filter((_, at) => at !== index);
  companionRemoteBrokers.value = next.join("\n");
  companionRemoteBrokersDirty.value = true;
}

function restoreRemoteBrokers(): void {
  const defaults = companion.value?.remote?.default_brokers ?? [];
  // Restoring an empty list would empty the field, and an empty field disables
  // the save button — so the one control that could repair the state is the one
  // that caused it, and remote access stays off with no way to turn it back on.
  if (!defaults.length) return;
  companionRemoteBrokers.value = defaults.join("\n");
  companionRemoteBrokerDraft.value = "";
  companionRemoteBrokersDirty.value = true;
}

/** The switch above the remote QR is the same write as the old buttons: it
 *  saves the current page/permissions/brokers when turning on, and only closes
 *  the relay when turning off. */
function toggleCompanionRemote(event: Event) {
  void configureCompanionRemote((event.target as HTMLInputElement).checked);
}
async function configureCompanionRemote(enabled?: boolean, pageUrl?: string, rotateToken = false) {
  if (companionRemoteBusy.value) return;
  companionRemoteBusy.value = true;
  try {
    // Only a save carries the broker list.  Turning remote access off, or
    // rotating its token, is not a statement about which servers to use, and
    // sending a half-typed list along with them would quietly rewrite it.
    const brokers = enabled === false || rotateToken ? undefined : parseRemoteBrokers(companionRemoteBrokers.value);
    const result = await bridge.companionRemoteConfigure({
      ...(enabled === undefined ? {} : { enabled }),
      ...(pageUrl === undefined ? { pageUrl: companionRemotePage.value.trim() } : { pageUrl }),
      mode: companionRemoteMode.value,
      ...(brokers && brokers.length ? { brokers } : {}),
      ...(rotateToken ? { rotateToken: true } : {}),
    });
    if (companion.value) companion.value = { ...companion.value, remote: result };
    companionRemotePage.value = result.page_url;
    companionRemoteMode.value = result.mode;
    // Every save answers with the brokers the sidecar actually holds, so the
    // field is resynced even when this particular save carried no list --
    // turning remote access off, or saving the permissions with the relay box
    // cleared.  Leaving it dirty in those cases pinned a stale list in place
    // with no remaining polled status able to correct it.
    companionRemoteBrokersDirty.value = false;
    if (result.brokers && result.brokers.length) {
      companionRemoteBrokers.value = result.brokers.join(", ");
    }
    announce(rotateToken ? t("远程令牌已更换，旧二维码已失效") : enabled === false ? t("远程访问已关闭") : t("远程访问设置已保存"));
  } catch (error) {
    store.setError(error);
  } finally { companionRemoteBusy.value = false; }
}

/** Read the relay QR without opening its dialog; the companion card draws it
 * inline and the dialog is the enlarged copy.
 *
 * This one works even while the LAN service is stopped: the relay page still
 * opens, it just reports that the computer's companion is not running. */
async function loadCompanionRemoteQrPreview() {
  if (!companion.value?.remote?.enabled || companionRemoteQrBusy.value) return;
  companionRemoteQrBusy.value = true;
  companionRemoteQrMessage.value = "";
  try {
    const result = await bridge.companionRemoteQr(companionRemotePage.value.trim() || undefined);
    companionRemoteQrUrl.value = result.url || "";
    if (result.ok && result.qr) companionRemoteQrImage.value = result.qr;
    else {
      companionRemoteQrImage.value = "";
      const messages: Record<string, string> = {
        REMOTE_DISABLED: t("请先开启远程访问"),
        REMOTE_PAGE_NOT_SET: t("请先填写手机页面地址并保存"),
        REMOTE_TOKEN_MISSING: t("访问令牌已清除，无法生成远程二维码"),
        QR_UNAVAILABLE: t("二维码不可用"),
      };
      companionRemoteQrMessage.value = messages[result.error || ""] || t("二维码不可用");
    }
  } catch (error) { store.setError(error); }
  finally { companionRemoteQrBusy.value = false; }
}
async function openCompanionRemoteQr() {
  companionRemoteQrOpen.value = true;
  void loadCompanionRemoteQrPreview();
}
/** Keep both inline codes in step with the page and the service.  The reader
 * should not have to press anything to see the code; a code that went stale
 * after a token rotation would be worse than none. */
watch(
  () => [
    tab.value,
    companion.value?.running,
    companion.value?.access_url,
    companion.value?.remote?.enabled,
    companion.value?.remote?.page_url,
  ] as const,
  () => {
    if (tab.value !== "companion" || !companion.value) return;
    if (companion.value.running) void loadCompanionQrPreview();
    else { qrImage.value = ""; qrMessage.value = ""; }
    if (companion.value.remote?.enabled) void loadCompanionRemoteQrPreview();
    else { companionRemoteQrImage.value = ""; companionRemoteQrMessage.value = ""; }
  },
  { immediate: true },
);
const settingsLoaded = ref(false);
let settingsLoading = false;
const savedAutoStart = ref(false);
// Same idea as savedAutoStart: sending encryption_enabled on every save would
// rebuild the live encryption manager (and re-derive the relay channels) each
// time, so it only travels when the toggle actually changed.
const savedEncryptionEnabled = ref(false);
/** The stored 自动粘贴 switch, for the global hotkeys.
 *
 * Not `settings.value.auto_paste_enabled`: that form is only filled the first
 * time the settings page is opened, and a shortcut has to do the same thing on
 * a machine nobody has visited the page on.  Kept from the same read that hands
 * the saved bindings to the host (`applySavedHotkeys`), re-read after a save and
 * when another surface announces the field — and `null` until the first read
 * lands, in which case the press asks for it itself (`autoPasteNow`): a paste
 * that silently did not happen is worse than one settings read.
 */
const savedAutoPasteEnabled = ref<boolean | null>(null);
const backups = ref<Array<Record<string, unknown>>>([]);
/** A backup's age and size, the line the legacy list put under each name.
 *
 * The host hands back `date` as its own formatted string and `size` in bytes;
 * the legacy panel printed those bytes raw, which on a backup of any size is a
 * nine-digit number a reader cannot compare at a glance, so the size is folded
 * into the largest unit that keeps it short.
 */
function backupMeta(item: Record<string, unknown>): string {
  const bytes = Number(item.size);
  let size = "";
  if (Number.isFinite(bytes) && bytes >= 0) {
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    size = bytes < 1024 ? `${bytes} B` : `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
  }
  return [item.date, size].filter(Boolean).map(String).join(" · ");
}
const backupMessage = ref("");
const restorePath = ref<string | null>(null);
const restoreBusy = ref(false);
/** Which of the backup card's long calls is running, and since when.
 *
 *  Each of them is one blocking call in the sidecar: no progress events, no
 *  fraction, nothing to cancel.  So what the window can honestly say is that
 *  something is happening, which one it is, and how long ago the click was —
 *  a backup over a large history takes tens of seconds, and until this the card
 *  showed nothing at all between the click and the result, which is
 *  indistinguishable from a button that did nothing.  A fraction would be a
 *  promise the sidecar cannot keep; a clock is not a promise at all.  See U23.
 */
const backupTask = ref<"" | "backup" | "restore" | "import" | "export">("");
const backupElapsed = ref(0);
let backupStartedAt = 0;
let backupTicker: ReturnType<typeof setInterval> | undefined;

function startBackupTask(kind: typeof backupTask.value) {
  endBackupTask();
  backupTask.value = kind;
  backupStartedAt = Date.now();
  backupElapsed.value = 0;
  backupTicker = setInterval(() => {
    backupElapsed.value = Math.max(0, Math.round((Date.now() - backupStartedAt) / 1000));
  }, 1000);
}
function endBackupTask() {
  if (backupTicker) clearInterval(backupTicker);
  backupTicker = undefined;
  backupTask.value = "";
  backupElapsed.value = 0;
}
// The clock is the card's own; a window closed mid-backup must not leave a
// timer running behind it.
onUnmounted(() => { if (backupTicker) clearInterval(backupTicker); });
/** The line the card shows while one of them is in flight. */
const backupProgress = computed(() => {
  if (!backupTask.value) return "";
  const what = backupTask.value === "backup" ? t("正在创建备份")
    : backupTask.value === "restore" ? t("正在恢复备份")
      : backupTask.value === "import" ? t("正在导入历史") : t("正在导出历史");
  return `${what}… ${t("已用时 {seconds} 秒", { seconds: backupElapsed.value })}。${t("这一步要读写整个数据目录，可能要几十秒；完成前请先不要关窗。")}`;
});
const restoreDialog = useDialog(restorePath);
const translationText = ref("");
const translationResult = ref("");
const translationSource = ref("auto");
const translationTarget = ref("en");
const translationBusy = ref(false);
const translationKey = ref("");
const translationKeyBusy = ref(false);
async function saveTranslationKey(clear = false) {
  if (translationKeyBusy.value || (!clear && !translationKey.value.trim())) return;
  translationKeyBusy.value = true;
  try {
    const result = await bridge.updateSettings(clear
      ? { clear_translate_key: true }
      : { set_translate_key: translationKey.value }) as { translate_key_set: boolean };
    foldSavedField("translate_key_set", result.translate_key_set);
    translationKey.value = "";
  } catch (error) { store.setError(error); }
  finally { translationKeyBusy.value = false; }
}
// Language names stay in their own language, except the two the catalog can
// translate — a computed keeps those reactive to the locale.
const translationLanguages = computed<Array<[string, string]>>(() => [
  ["en", "English"], ["zh", t("中文")], ["ja", t("日本語")], ["ko", "한국어"],
  ["fr", "Français"], ["de", "Deutsch"], ["es", "Español"], ["pt", "Português"],
  ["ru", "Русский"], ["ar", "العربية"], ["hi", "हिन्दी"],
]);
let translationGeneration = 0;
watch([translationText, translationSource, translationTarget], () => {
  ++translationGeneration;
  translationResult.value = "";
}, { flush: "sync" });
// Translating a clip from the history list. The list ships a truncated preview
// and copying the row would overwrite whatever the user is holding, so the text
// is read back over IPC (`history.text`) and shown read-only — the native
// counterpart of the legacy context menu's translate action. The language pair
// is shared with the settings box above, because that is a preference; the text
// and the result are not, or a clip's translation would appear under a textarea
// still holding the snippet the user typed.
const translateItemOpen = ref(false);
const translateItemText = ref("");
const translateItemTruncated = ref(false);
const translateItemResult = ref("");
const translateItemReading = ref(false);
const translateItemBusy = ref(false);
let translateItemGeneration = 0;
const translateItemDialog = useDialog(translateItemOpen, () => {
  // Invalidates an in-flight translation, so a late response cannot land in a
  // dialog the user has already dismissed.
  ++translateItemGeneration;
});
watch([translationSource, translationTarget], () => {
  if (!translateItemOpen.value) return;
  ++translateItemGeneration;
  translateItemResult.value = "";
}, { flush: "sync" });
async function openTranslateItem(item: HistoryItem) {
  if (translateItemReading.value) return;
  translateItemReading.value = true;
  try {
    const read = await bridge.readHistoryText(item.id);
    const text = String(read.text || "");
    if (!text) {
      // An image has no text of its own, and its preview is a label like
      // "[Image]" — opening an empty translator over that would be noise.
      announce(t("这条记录没有可翻译的文本"));
      return;
    }
    translateItemText.value = text;
    translateItemTruncated.value = !!read.truncated;
    translateItemResult.value = "";
    translateItemOpen.value = true;
  } catch (error) {
    store.setError(error);
  } finally { translateItemReading.value = false; }
}
const {
  isWebLink, openHistoryLink, isRemoteFile, isRichText, remoteFileActionable,
  remoteFileTitle, downloadRemoteFile,
} = createHistoryActions(store);

/** The fetch behind a remote-file paste, while the main window is drawing its
 *  bar.  One at a time: a second paste started from the menu would put a second
 *  set of transfers on the wire for files that are already coming. */
interface MainPasteFetch {
  entryId: string;
  item: HistoryItem;
  progress: PasteFetchProgress | null;
}
const pasteFetch = ref<MainPasteFetch | null>(null);
let pasteFetchRun: PasteFetchRun | undefined;
/** How long a headless paste shortcut waits before giving up.  The sidecar has
 *  its own stall timer for a fetch nobody is watching; this is the window's
 *  promise to answer the key press eventually even if that never arrives. */
const PASTE_HOTKEY_TIMEOUT_MS = 5 * 60 * 1000;

/** Copy-and-paste, or fetch-and-paste for a file that still lives elsewhere.
 *
 * The row menu's one action that ends outside the window.  For a local clip the
 * copy has to land before the paste is asked for, or the window behind would
 * receive whatever the clipboard held before.  For a remote file there is
 * nothing local to copy yet: the fetch runs with a bar at the bottom of the
 * window, and the paste is handed over once the files are on the clipboard. */
async function pasteToCurrentApp(item: HistoryItem) {
  if (isRemoteFile(item)) { await startPasteFetch(item); return; }
  if (!(await store.copy(item, false))) return;
  try {
    await bridge.hideMainAndPaste();
  } catch {
    store.toast("ui.history", t("已复制，请手动按 {shortcut} 粘贴", { shortcut: PASTE_SHORTCUT }));
  }
}

/** Begin an on-demand paste of a remote-file row.
 *
 * The sidecar's `PASTE_CONFIRM_REQUIRED` is a question, not a failure: it opens
 * the confirmation dialog and the answer calls this again with
 * `confirmed: true`.  The other refusals happen before anything moves, so they
 * are said in a toast and no bar appears. */
async function startPasteFetch(item: HistoryItem, confirmed = false) {
  if (pasteFetch.value) {
    store.toast("ui.transfers", t("已经在取回一个文件，请等它完成或先取消"));
    return;
  }
  try {
    await bridge.preparePasteEntry(item.id, confirmed);
  } catch (error) {
    if (isPasteConfirmRequired(error)) { void askLargePasteConfirm(item); return; }
    const message = pastePrepareErrorText(errorCode(error), errorMessage(error));
    store.toast("ui.transfers", message);
    return;
  }
  beginPasteFetch(item);
}

/** The large-file question, with the size the sidecar withheld.
 *
 * `PASTE_CONFIRM_REQUIRED` refuses before any bytes move and the host's error
 * carries only its code and message, so the size is read from the row's offer
 * instead — the same `preview_history_entry` read the hover card makes.  A
 * folder offer has size 0 by design (its archive is built later) and an
 * unreadable preview answers nothing at all, so both fall back to the question
 * without a number.
 */
const pasteConfirmSize = ref(0);
async function askLargePasteConfirm(item: HistoryItem) {
  pasteConfirmItem.value = item;
  pasteConfirmSize.value = 0;
  try {
    const preview = await bridge.previewHistoryEntry(item.id);
    const files = preview?.files || [];
    const total = files.reduce((sum, file) => sum + (Number(file.size) || 0), 0);
    if (!files.some((file) => file.kind === "dir") && total > 0) pasteConfirmSize.value = total;
  } catch { /* the question keeps its general wording */ }
}

function beginPasteFetch(item: HistoryItem) {
  // The reactive object is the one fetch identity: the callbacks compare
  // `pasteFetch.value` with it so an answer belonging to a newer fetch is
  // dropped, and mutating the proxy is what repaints the bar.
  pasteFetch.value = { entryId: item.id, item, progress: null };
  const current = pasteFetch.value;
  pasteFetchRun = followPasteFetch(item.id, {
    onProgress: (progress) => {
      if (pasteFetch.value === current) current.progress = progress;
    },
    onReady: (paths) => { void finishPasteFetch(current, paths); },
    onFailed: (reason) => {
      if (pasteFetch.value !== current) return;
      stopPasteFetch();
      store.toast("ui.transfers", pasteFailureText(reason));
    },
  });
}

/** The files are here and the row is local.  The window steps out of the way
 *  and the host types the paste chord; a refused chord leaves the files on the
 *  clipboard with the same "press it yourself" sentence the hotkeys use. */
async function finishPasteFetch(current: MainPasteFetch, paths: string[]) {
  if (pasteFetch.value !== current) return;
  stopPasteFetch();
  try {
    await bridge.hideMainAndPaste();
    store.toast("ui.transfers", t("已取回 {count} 个文件并粘贴", { count: paths.length }));
  } catch {
    store.toast("ui.transfers", t("已取回 {count} 个文件，已复制，请手动粘贴", { count: paths.length }));
  }
}

function stopPasteFetch() {
  pasteFetchRun?.stop();
  pasteFetchRun = undefined;
  pasteFetch.value = null;
}

/** What the bottom bar shows while a fetch runs: its name (the transfer's own
 *  once the list has answered, the row's preview before), the bar, the rate and
 *  the estimate.  The percent is shown as a whole number — a percentage with a
 *  decimal is a precision nobody reads. */
const pasteFetchPercent = computed(() => Math.min(100, Math.max(0, pasteFetch.value?.progress?.progress || 0)));
const pasteFetchName = computed(() => {
  const current = pasteFetch.value;
  if (!current) return "";
  const progress = current.progress;
  if (progress?.filename) {
    return progress.files > 1
      ? t("{name} 等 {count} 个文件", { name: progress.filename, count: progress.files })
      : progress.filename;
  }
  return previewLabel(current.item);
});
const pasteFetchSpeed = computed(() => {
  const speed = pasteFetch.value?.progress?.speed || 0;
  return speed > 0 ? t("{rate}/秒", { rate: size(speed) }) : "";
});
const pasteFetchEta = computed(() => pasteFetch.value?.progress?.eta || "");

async function cancelPasteFetch() {
  const running = pasteFetchRun;
  stopPasteFetch();
  await running?.cancel();
  store.toast("ui.transfers", t("已取消取回文件"));
}

/** The large-file question's answer: the sidecar remembers a "yes" for this
 *  attempt only, so the fetch is prepared again with it. */
const pasteConfirmItem = ref<HistoryItem | null>(null);
const pasteConfirmDialog = useDialog(pasteConfirmItem);
async function confirmLargePaste() {
  const item = pasteConfirmItem.value;
  pasteConfirmItem.value = null;
  if (!item) return;
  await startPasteFetch(item, true);
}

/** A paste shortcut that landed on a remote file: fetch it quietly, then paste.
 *
 * There is no bar for this path — the reader pressed a key in another
 * application and is not looking at this window — so the fetch runs headless and
 * only the outcome is said.  The quick-paste overlay is the path with a visible
 * progress panel; that split is deliberate rather than a missing piece. */
async function pasteRemoteFileFromHotkey(item: HistoryItem) {
  try {
    await bridge.preparePasteEntry(item.id);
  } catch (error) {
    if (isPasteConfirmRequired(error)) {
      store.toast("ui.hotkey", t("文件较大，请在快速粘贴窗口中确认后取回"));
      return;
    }
    store.toast("ui.hotkey", pastePrepareErrorText(errorCode(error), errorMessage(error)));
    return;
  }
  await new Promise<void>((resolve) => {
    let settled = false;
    const finish = (say: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      run?.stop();
      run = undefined;
      store.toast("ui.hotkey", say);
      resolve();
    };
    let run: PasteFetchRun | undefined;
    const timeout = setTimeout(() => {
      if (settled) return;
      settled = true;
      void run?.cancel();
      run = undefined;
      store.toast("ui.hotkey", t("取回文件超时，请到快速粘贴窗口重试"));
      resolve();
    }, PASTE_HOTKEY_TIMEOUT_MS);
    run = followPasteFetch(item.id, {
      onProgress: () => { /* nothing to draw: the key was pressed in another app */ },
      onReady: (paths) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        run = undefined;
        const count = paths.length;
        void (async () => {
          if (await autoPasteNow()) {
            try {
              await bridge.injectPaste();
              store.toast("ui.hotkey", t("已取回 {count} 个文件并粘贴", { count }));
            } catch {
              store.toast("ui.hotkey", t("已复制，请按 {shortcut} 手动粘贴", { shortcut: PASTE_SHORTCUT }));
            }
          } else {
            store.toast("ui.hotkey", t("已取回 {count} 个文件，已复制到剪贴板", { count }));
          }
          resolve();
        })();
      },
      onFailed: (reason) => finish(pasteFailureText(reason)),
    });
  });
}

/** The menu on a history row, in the legacy menu's own order.
 *
 * Every entry here is the row's own button under a different name — the menu
 * is not a second set of capabilities, it is the same set reachable without
 * hunting along the row.  That is why the conditions match the buttons' too:
 * 翻译 stands down while a translate is already in flight, 打开链接 appears
 * only for something the sidecar would actually open, and a row with no id
 * cannot be pinned or deleted.  The one exception is 「粘贴到当前应用」, which is
 * a *combination* of 复制 with the OS paste the hotkeys already do: the row has
 * no button for it because its click is the row's own copy, and the paste lands
 * in whichever application the reader meant to reach.
 *
 * It takes the point rather than the event, because the history page's own
 * rows raise the same menu from the keyboard and a key press has no
 * coordinates.
 */
function historyMenu(at: { clientX: number; clientY: number }, item: HistoryItem) {
  const hasId = !!item.id.trim();
  openContextMenu(at, [
    {
      id: "copy", label: t("复制"), icon: Copy,
      shortcut: MODIFIER.label === "Cmd" ? "⌘C" : "Ctrl+C",
      // A file that lives on another device has nothing here to put on the
      // clipboard, and the sidecar says so rather than quietly leaving the
      // previous clipboard in place.  Offering it anyway would be a menu entry
      // whose only outcome is an error.
      disabled: isRemoteFile(item),
      run: () => store.copy(item),
    },
    // Copy-and-paste in one entry, for the reader whose destination is already
    // focused behind this window.  On a remote file there is nothing on this
    // machine to copy yet, so the same entry starts the fetch and pastes once
    // the files are here — the reader asked to paste, and which half needed the
    // wire is an implementation detail they should not have to choose.
    {
      id: "paste-to-app", label: t("粘贴到当前应用"), icon: ClipboardPaste,
      shortcut: PASTE_SHORTCUT, disabled: !hasId,
      run: () => pasteToCurrentApp(item),
    },
    // Only a clip that actually carries a second, formatted form: for plain
    // text this entry and 复制 would be the same call under two names, and an
    // image has no text form to fall back to at all.
    isRichText(item)
      ? {
          id: "copy-plain", label: t("复制为纯文本"), icon: Copy,
          disabled: isRemoteFile(item),
          run: () => store.copyPlain(item),
        }
      : null,
    // The explicit download, kept as the row's secondary entry: a reader who
    // wants the file on this machine without pasting it has no other one.  Its
    // tooltip keeps what the row's old download button said about the route and
    // reachability, because the menu is the only place that action lives now.
    isRemoteFile(item)
      ? {
          id: "download-file", label: t("仅下载到本机"), icon: Download,
          title: remoteFileTitle(item),
          // Same rule as before: anything the sidecar could actually be asked
          // for stays clickable, and the request answering `NOT_CONNECTED` is
          // what tells the reader the device is away.
          disabled: !remoteFileActionable(item),
          run: () => downloadRemoteFile(item),
        }
      : null,
    {
      id: "pin", label: item.pinned ? t("取消置顶") : t("置顶"),
      icon: item.pinned ? PinOff : Pin, disabled: !hasId,
      run: () => store.pin(item),
    },
    {
      id: "favorite", label: t("加入收藏夹"), icon: Star, disabled: !hasId,
      run: () => addToFavorites(item),
    },
    {
      id: "translate", label: t("翻译"), icon: Globe, disabled: translateItemReading.value,
      run: () => openTranslateItem(item),
    },
    isWebLink(item)
      ? { id: "open-link", label: t("在浏览器中打开链接"), icon: ExternalLink, run: () => openHistoryLink(item) }
      : null,
    {
      id: "delete", label: t("删除"), icon: Trash2, divider: true, danger: true,
      shortcut: MODIFIER.label === "Cmd" ? "⌘D" : "Del", disabled: !hasId,
      // The row's own trash opens this same confirm, and Delete on the keyboard
      // cursor does too: one record is not worth wiping on a stray click.
      run: () => { deleteItem.value = item; },
    },
    {
      id: "details", label: t("查看详情"), icon: Info, divider: true,
      // The legacy 查看详情 was a toast, not a dialog: type, source and id on
      // one line, which is everything a history row knows that the row itself
      // does not already show.
      run: () => announce(t("类型：{type} | 来源：{source} | ID：{id}", {
        type: typeLabel(item), source: item.source_name || t("未知"), id: hasId ? item.id : "N/A",
      })),
    },
  ]);
}

/** Put one row in the favourites, and say how many landed.
 *
 * The store's batch path is the only one there is (`favorites.batch_add`), so
 * a single row goes through it the way the toolbar's button sends a selection;
 * the count it answers with is what the report uses, because a favourite that
 * was already there is not one that was added.
 */
async function addToFavorites(item: HistoryItem) {
  const added = await store.batchFavorite([item.id], "", true);
  if (added) store.toast("ui.favorites", t("已加入收藏夹 {count} 条", { count: added }));
}

/** The right-click menu on a device row.
 *
 * 连接/断开 follow the row's own button and its rule: only one of them is ever
 * offered, and neither is offered for a device this machine is not paired
 * with.  打开聊天 is the one entry here the row has no button for — the chat
 * page lists nearby devices of its own, but reaching a device from the device
 * list was the legacy card's own action and there is no reason to make the
 * reader walk to another page to start a conversation with the row in front of
 * them.  重命名 is the write the row has no editable control of its own for; it
 * was a second entry point when the row carried a 设备备注 field, and with that
 * field gone it is the only one.
 *
 * A device paired by internet code has none of the local half of that: there is
 * no link to dial and no certificate pinned to revoke, and its name is the
 * alias the pairing card writes rather than a note on a saved peer.  Its menu is
 * therefore built from its own entries — talk to it, rename it, break the
 * pairing — instead of a set of local ones with holes cut in it.
 */

/** The entry a device with no update action would have had, named.
 *
 * The sidecar's `update_blocked` carries the direction in its first half, so
 * the reader sees the action they came looking for — 发送更新 on a device this
 * build is ahead of, 获取更新 on one it is behind — rather than nothing at all,
 * which is what every blocked row used to look like. */
function blockedUpdateLabel(device: Device) {
  return String(device.update_blocked || "").startsWith("fetch") ? t("获取更新") : t("发送更新");
}

/** And why it is dimmed, in words.  The code says which of the three it is; the
 * sentence is the shell's, because the shell is where the two languages live.
 * None of the three is about which application the peer runs — the sidecar
 * stopped deciding on that, so there is nothing here to explain about it. */
function blockedUpdateReason(device: Device) {
  const cause = String(device.update_blocked || "").split(":")[1];
  if (cause === "too_old") return t("对方版本过旧，本机不能直接给它发送安装包；先让它自己检查更新升级一次");
  // Neutral about the direction, because this cause is not the send one's
  // alone: a device that is *ahead* on another platform dims the fetch entry
  // with it, and a sentence about what this machine has to send would answer a
  // question the reader did not ask.
  if (cause === "other_platform") return t("对方与本机不是同一个平台，双方的安装包都用不了");
  return t("两台设备版本相同，没有需要发送的安装包");
}

/** The version disagreement as one sentence, or an empty string when there is
 *  none to report.
 *
 *  The row used to say this twice, in two icons the reader had to tell apart by
 *  arrow direction, and the sentence was in neither of them.  It is here because
 *  a row of a device list is read at a glance and an arrow is not a word: 发送更新
 *  and 获取更新 differ by the direction the installer travels, and both travel
 *  over the same link in the same kind of blob.
 *
 *  Empty for a peer that advertises no version — an older build sends none, and
 *  the chip above says nothing about it either; the two agree.
 */
function deviceVersionNote(device: Device) {
  // A peer that advertises no version still gets the line when the sidecar has
  // decided a direction for it: the sidecar knows the two builds disagree even
  // when the peer did not say what it runs, and losing the entry because the
  // version chip has nothing to show would turn the same row into a dead end.
  const version = device.version;
  if (device.update_available) {
    if (!version) {
      return device.update_cached
        ? t("版本不一致：该设备比本机旧")
        : t("版本不一致：该设备比本机旧；本机没有可发送的安装包，可以让它自己检查更新");
    }
    return device.update_cached
      ? t("版本不一致：该设备是 {version}，比本机旧", { version })
      : t("版本不一致：该设备是 {version}，比本机旧；本机没有可发送的安装包，可以让它自己检查更新", { version });
  }
  if (device.update_fetchable) {
    return version
      ? t("版本不一致：该设备是 {version}，比本机新", { version })
      : t("版本不一致：该设备比本机新");
  }
  return "";
}

/** A device row's menu, on the two routes it can hold.
 *
 * 打开聊天 is drawn on both, and always: a conversation is the one thing every
 * device in this list can be offered, the chat page's own 附近设备 list offers it
 * to any of them, and the row's button only appears while the device answers —
 * so the menu is where the entry lives when it cannot be reached, dimmed with
 * the reason rather than missing.
 *
 * 移除设备 is drawn on both as well.  The sidecar's own removal is not a LAN
 * operation: it archives the device, ends a local pairing *and* drops an
 * internet one, so a row whose only route is the relay was the one device in
 * the list that could not be taken out of it — 解除互联网配对 leaves nothing
 * archived and forgets the alias the user gave it. */
function deviceMenu(event: MouseEvent, device: Device) {
  const reachable = chatReachable(device);
  const chat: ContextMenuItem = {
    id: "chat", label: t("打开聊天"), icon: MessageCircle,
    disabled: !reachable,
    title: reachable ? undefined : t("{name} 当前不在线", { name: deviceLabel(device) }),
    run: () => chatWith(device),
  };
  if (device.relay) {
    openContextMenu(event, [
      chat,
      {
        id: "send-file", label: t("发送文件…"), icon: Upload,
        disabled: busy.value || !fileReachable(device),
        title: fileReachable(device)
          ? t("把文件发给这台设备：在传输页选好文件再发送")
          : t("{name} 当前不在线，无法接收文件", { name: deviceLabel(device) }),
        run: () => sendFileTo(device),
      },
      { id: "rename", label: t("重命名"), icon: Pencil, run: () => renameRelayDevice(device) },
      { id: "copy-id", label: t("复制设备 ID"), icon: Copy, run: () => copyText(device.id) },
      { id: "unpair", label: t("解除互联网配对"), icon: CloudOff, divider: true, danger: true, run: () => { relayUnpairDevice.value = device; } },
      { id: "forget", label: t("移除设备"), icon: Trash2, danger: true, run: () => { forgetDevice.value = device; } },
    ]);
    return;
  }
  const connected = device.connection_state === "online";
  // This row can answer for two pairings at once — see `relayPairing` — so the
  // entries below are drawn per route and not per row.  连接/断开 belongs to the
  // local pairing; 解除互联网配对 reaches the device over the relay and is owed
  // to a reader whose only pairing with it is the code.  打开聊天 and 移除设备
  // are drawn on every row, on reachability and on the device itself rather
  // than on either route; see `deviceMenu`'s own note above.
  const relay = relayPairing(device);
  openContextMenu(event, [
    device.paired
      ? {
          id: connected ? "disconnect" : "connect",
          label: connected ? t("断开连接") : t("连接"),
          icon: connected ? PlugZap : Plug,
          // A device that is not reachable cannot be dialed, which is the rule
          // the row's own connect button already applies — see `canDial`.  A
          // dial already on its way for this device refuses a second one: the
          // RPC returns as soon as the dial *starts*, so `busy` alone left the
          // entry live for the whole 12-second window and every click started
          // another dial.
          disabled: !connected && (!canDial(device) || dialingId.value === device.id),
          run: () => (connected ? store.disconnect(device) : dialDevice(device, () => store.connect(device))),
        }
      : null,
    chat,
    // The row's entry for the thing most readers come to a device for after
    // talking to it.  Not a tenth icon: the row was already ten wide (U111), and
    // a menu is reachable with no second button through 更多操作.
    {
      id: "send-file", label: t("发送文件…"), icon: Upload,
      disabled: busy.value || !fileReachable(device),
      title: fileReachable(device)
        ? t("把文件发给这台设备：在传输页选好文件再发送")
        : t("{name} 当前不在线，无法接收文件", { name: deviceLabel(device) }),
      run: () => sendFileTo(device),
    },
    // The two probes that left the row: both report rather than act, and both
    // were the entries a reader scanned past in a line of ten icons.
    { id: "test", label: t("测试连接"), icon: Activity, disabled: busy.value || !!probeBusyId.value, run: () => testConnection(device) },
    // Kept on the menu even when the engine is stopped, dimmed and saying why:
    // the row's own button used to carry that explanation, and a menu that
    // simply omitted the entry would leave the reader looking for a control
    // they can see on every other device.
    {
      id: "send-url", label: t("发送网址"), icon: Globe,
      disabled: busy.value || !sendUrlAvailable.value,
      title: sendUrlAvailable.value ? undefined : t("同步引擎未运行，推送文本与发送网址不可用。"),
      run: () => openSendUrl(device),
    },
    { id: "rename", label: t("重命名"), icon: Pencil, run: () => renameDeviceRow(device) },
    { id: "copy-id", label: t("复制设备 ID"), icon: Copy, run: () => copyText(device.id) },
    // The update group: what the row's own two buttons do, plus the version
    // they are about, plus — when neither is on offer — the entry that would
    // have been there, dimmed, saying why.  A device that advertises no version
    // gets none of it: the field is what every line here is read off, and the
    // sidecar leaves the reason empty for exactly that peer.
    device.version
      ? {
          id: "version", label: t("版本 {version}", { version: device.version }),
          icon: Info, divider: true, disabled: true,
          // The sentence the row's version chip shows, so the two agree.
          title: device.update_available
            ? t("该设备版本较旧，可以发送更新")
            : device.update_fetchable ? t("从该设备获取新版本安装包并安装") : t("对方软件版本"),
          // Nothing to run: the row is the version itself, and `disabled` is
          // what keeps it from looking like an action.
          run: () => {},
        }
      : null,
    device.update_available
      ? {
          id: "send-update", label: t("发送更新"), icon: FileUp,
          // The row's button, its rule and its two sentences: what this machine
          // can send is the installer its own upgrade kept.
          disabled: busy.value || !!updateBusyId.value || !device.update_cached || !canDial(device),
          title: device.update_cached
            ? t("把本机的安装包发送给该设备")
            : t("本机还没有安装包可发送：本机只保留自己升级时下载的那一个，对方可自行检查更新"),
          run: () => offerDeviceUpdate(device),
        }
      : null,
    device.update_fetchable
      ? {
          id: "fetch-update", label: t("获取更新"), icon: Download,
          disabled: busy.value || !!fetchBusyId.value || !canDial(device),
          title: t("从该设备获取新版本安装包并安装"),
          run: () => fetchDeviceUpdate(device),
        }
      : null,
    // The debug entry, offered on every reachable row rather than only on rows
    // the sidecar has an opinion about: which device is misbehaving is exactly
    // what one does not know yet, so the button that asks a device to explain
    // itself cannot be gated on the list's own guess.  No pairing is needed, and
    // the answer is the peer's decision — it hands over a redacted copy only if
    // its own 允许其他设备取走本机日志 switch is on, and says so when it is not.
    canDial(device)
      ? {
          id: "fetch-log", label: t("获取日志"), icon: FileDown, divider: true,
          disabled: busy.value || !!logBusyId.value,
          title: t("请该设备把它的日志发过来，存到「下载/ClipSync-logs」；对方关闭日志共享时会说明"),
          run: () => fetchDeviceLog(device),
        }
      : null,
    device.update_blocked
      ? {
          id: "update-blocked", label: blockedUpdateLabel(device),
          icon: String(device.update_blocked).startsWith("fetch") ? Download : FileUp,
          disabled: true, title: blockedUpdateReason(device), run: () => {},
        }
      : null,
    relay.paired
      ? { id: "relay-unpair", label: t("解除互联网配对"), icon: CloudOff, divider: true, danger: true, run: () => { relayUnpairDevice.value = device; } }
      : null,
    // 移除设备 is drawn on every row, paired or not: the sidecar implements the
    // same removal for a row this machine merely knows (it archives what is on
    // record, so a discovered-only card can be dismissed from the list), and the
    // row's own trash already offered it.  The menu used to require `paired`,
    // which is why the two ways to reach one action disagreed about when it
    // existed.
    { id: "forget", label: t("移除设备"), icon: Trash2, divider: !relay.paired, danger: true, run: () => { forgetDevice.value = device; } },
  ]);
}

/** Rename an internet-paired device from its own row.
 *
 * The alias, not a note: an internet peer has no saved LAN peer for a note to
 * live on, and the name the row shows is already the alias.  Both entry points
 * — the row's menu and the pairing card — open the same dialog on the same
 * field, so there is one answer to "what is this device called".
 */
function renameRelayDevice(device: Device) {
  renameTarget.value = { kind: "internet", peerId: device.id };
  renameValue.value = device.alias || "";
}

/** 重命名 on a device row, which writes one field or the other.
 *
 *  A note lives on a saved local peer; a device paired by code has no such peer,
 *  so the note call answers NOT_FOUND and the row's name is its alias — the field
 *  the pairing card's own rename writes.  Which of the two a row uses is
 *  `renameWritesAlias`, which asks whether the row holds an *internet* pairing
 *  rather than whether it is paired: a device this machine has dialed but not
 *  paired is in `config.peers` and can hold a note, and it used to be sent to the
 *  alias call, where the backend answered NOT_FOUND. */
/** Open a row's menu from its own button rather than from a right click.
 *
 * Anchored under the button that opened it and right-aligned with it: a menu
 * that appeared at the pointer's last resting place would look like it arrived
 * from nowhere, since the pointer is already on a button.
 */
function openDeviceMenuHere(event: MouseEvent, device: Device) {
  const rect = (event.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  deviceMenu({ clientX: rect ? rect.right - 8 : 0, clientY: rect ? rect.bottom : 0 } as MouseEvent, device);
}
function renameDeviceRow(device: Device) {
  if (renameWritesAlias(device)) {
    renameRelayDevice(device);
    return;
  }
  renameTarget.value = { kind: "device", id: device.id };
  renameValue.value = device.note || "";
}

/** The longest the window waits for an invite's own answer, in ms.
 *
 * The sidecar answers an invite at once now — the dial it starts belongs to its
 * own thread — so this only fires when the engine itself has stopped answering.
 * It is here so the button cannot stay disabled forever when that happens, which
 * is what "the button is sometimes stuck" was: nothing in this window ever gave
 * up waiting. */
const CHAT_INVITE_ANSWER_MS = 8000;

/** *work*, or a rejection once *ms* have passed.
 *
 * The timer is cleared on the way out, so a fast answer leaves nothing pending —
 * which matters in the component tests, where a stray timer outlives the test. */
async function withAnswerTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(t("连接超时，请重试"))), ms);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/** Open a conversation with a device from its own row.
 *
 * Same call the chat page's 附近设备 list makes, and the same answer: a session
 * id means the link was up and the conversation is open, anything else means the
 * invite is still dialing — in which case the chat page is where it will
 * appear, and it is opened either way so the reader can watch it arrive.
 *
 * The row says so at once and blocks its other chat buttons until the answer
 * comes back.  Before that, the invite was the one row action with no busy state:
 * clicking it looked like nothing had happened for as long as the sidecar took —
 * and because the sidecar was waiting out a dial it did not have to wait for, that
 * could be the whole connect timeout.
 */
/** The device row a conversation was opened from, if any.
 *
 * Held here rather than in the chat page because it is the window's own
 * history: the page is reached from a row on another page, and a reader who
 * came that way has one question when they are done — how to get back to the
 * list they were working in. */
const chatOrigin = ref<{ id: string; label: string } | null>(null);
function backToDevicesFromChat() {
  chatOrigin.value = null;
  openPage("devices");
}
async function chatWith(device: Device) {
  if (chatConnectingId.value) return;
  chatConnectingId.value = device.id;
  announce(t("正在连接 {name}…", { name: deviceLabel(device) }));
  try {
    const result = await withAnswerTimeout(
      bridge.inviteChat(device.id, deviceLabel(device)),
      CHAT_INVITE_ANSWER_MS,
    );
    chatSessionToOpen.value = result?.chat_session_id || "";
    chatOrigin.value = { id: device.id, label: deviceLabel(device) };
    openPage("chat");
  } catch (error: any) {
    announce(error?.message || t("发送邀请失败"));
  } finally {
    chatConnectingId.value = "";
  }
}

async function runTranslateItem() {
  const text = translateItemText.value.trim();
  if (!text || translateItemBusy.value) return;
  const generation = ++translateItemGeneration;
  translateItemBusy.value = true;
  try {
    const result = await bridge.translate(text, translationTarget.value, translationSource.value);
    if (generation !== translateItemGeneration) return;
    translateItemResult.value = String(result.translated || "");
  } catch (error) {
    if (generation === translateItemGeneration) store.setError(error);
  } finally { translateItemBusy.value = false; }
}
/** The tools the sidecar knows, with the paths each one reads.
 *
 * The paths come back with the list and used to be dropped on the floor, which
 * left the checkbox answering "sync Claude Code?" and nothing on the card
 * saying what that would mean — a reader deciding whether to tick a tool had
 * only its name to go on. */
watch(() => state.updatePeerEvent, (answer) => {
  // A device's answer to the update exchange, replacing the note the click left
  // on its row. The store worded it; this only puts it where the click was.
  if (!answer || !answer.device_id) return;
  updateNotes.value = { ...updateNotes.value, [answer.device_id]: answer.message };
});
watch(() => state.logEvent, (answer) => {
  // The same, for the log requests: the note left by the click said the ask was
  // away, and this is what came back. It also counts the device off the collect
  // run, which is what opens the folder once the last one has answered.
  if (!answer || !answer.device_id) return;
  logNotes.value = { ...logNotes.value, [answer.device_id]: answer.message };
  settleCollectedLog(answer.device_id);
});
/** The pairing card's own view of the sidecar's status.
 *
 * `peers` are devices the pairing actually completed; `waiting` are codes
 * entered here whose partner has not answered yet — a provisional 4-char tag,
 * not a device, which is why the two are drawn apart.
 */
const internetPairing = ref<{ generated_code?: string | null; pending_ttl?: number | null; relay?: string; relay_broker?: string; enabled?: boolean; peers: Array<any>; waiting?: Array<any> }>({ peers: [] });
const internetPairingCode = ref("");
/** This machine's own relay link, in the runtime's words (off/connecting/
 * online/error).  Reported beside the peer list because a list that reads 离线
 * throughout means one thing when we are on the relay and another when we are
 * not — and only this separates the two.
 *
 * The sidecar's own transition event outranks the status read, and deliberately:
 * both name the same value, but the event is published as the relay changes
 * while the snapshot only says what was true when it was read — so the read is
 * the answer until the relay says otherwise, and the line follows the link
 * rather than waiting for a 刷新. */
const relayState = computed(() => store.state.relayState || String(internetPairing.value.relay || ""));
const relayStateLabel = computed(() => {
  const state = relayState.value;
  if (state === "online") return t("在线");
  if (state === "connecting") return t("连接中");
  if (state === "error") return t("错误");
  if (state === "off") return t("已关闭");
  // A state this window has not been taught: said as the sidecar says it,
  // rather than folded into one of the four and read as something it is not.
  return state;
});
const internetPairingMessage = ref("");
/** Whether that line is a refusal rather than a note.  The two used to share
 * one muted paragraph, so a rejected code and a generated one read alike. */
const internetPairingFailed = ref(false);
/** Codes entered here that the other machine has not answered yet.  The row is
 * the whole of the "did it connect?" answer in that window, so it is read from
 * the status rather than kept beside it: it appears when the sidecar records
 * the entry and goes away by itself when the partner's reply replaces the tag
 * with a real device. */
const internetPairingWaiting = computed<Array<any>>(() => internetPairing.value.waiting || []);
/** When the card's status snapshot was read, for the two countdowns.
 *
 * The code's remaining life arrives as a number of seconds (`pending_ttl`) and
 * the entered-code wait's as a deadline, so both need the clock the snapshot was
 * taken at.  The 5-second poll while this tab is open refreshes it. */
const internetPairingFetchedAt = ref(Date.now());
/** A whole code, however the reader has typed or pasted it: see
 * `lib/pairing-code.ts` for why the field is not simply a text box. */
const internetPairingComplete = computed(() => isPairingCodeComplete(internetPairingCode.value));
/** The code box, so a wait that went wrong can point at the thing to redo. */
const internetPairingInputEl = ref<HTMLInputElement | null>(null);
function focusInternetPairingCode() {
  void nextTick(() => {
    const box = internetPairingInputEl.value;
    if (!box) return;
    box.scrollIntoView({ block: "nearest" });
    box.focus({ preventScroll: true });
    box.select();
  });
}
/** Seconds left before the generated code can no longer be typed, or 0.
 *
 * Read off `pending_ttl` from the snapshot rather than counted locally: the
 * sidecar's deadline is the one that decides, and a window that counted down on
 * its own would be wrong after a laptop sleep. */
const generatedCodeSecondsLeft = computed(() => {
  const ttl = Number(internetPairing.value.pending_ttl);
  if (!Number.isFinite(ttl) || ttl <= 0) return 0;
  const elapsed = (Date.now() - internetPairingFetchedAt.value) / 1000;
  return Math.max(0, Math.round(ttl - Math.max(0, elapsed)));
});
/** The same countdown for one entered-code wait, from its own deadline. */
function waitSecondsLeft(row: Record<string, any>): number {
  const deadline = Number(row?.deadline);
  if (!Number.isFinite(deadline) || deadline <= 0) return 0;
  return Math.max(0, Math.round(deadline - Date.now() / 1000));
}
/** Waits this card has been told are over (timed out), by provisional peer id.
 *
 * The event is the word that a wait failed; the status poll is what removes the
 * row.  Between the two the row is still on screen, and it must not keep saying
 * "waiting" after the sidecar has given up on it.  Cleared when the status no
 * longer lists the row, and when a new code is submitted. */
const netpairWaitFailed = ref<Record<string, boolean>>({});
function clearNetpairWaitFailed(keep: Set<string>) {
  const next: Record<string, boolean> = {};
  for (const key of Object.keys(netpairWaitFailed.value)) {
    if (keep.has(key)) next[key] = true;
  }
  netpairWaitFailed.value = next;
}
/** The field reformats as the reader types, so the box shows the code in the
 * same shape as the one this machine generated.  The element's own value is
 * written too: with `:value` bound, Vue skips the DOM write when the formatted
 * string has not changed, and the box would otherwise keep whatever the reader
 * typed that a code cannot hold. */
function setInternetPairingCode(event: Event) {
  const input = event.target as HTMLInputElement;
  const formatted = formatPairingCode(input.value);
  internetPairingCode.value = formatted;
  if (input.value !== formatted) input.value = formatted;
}
// Relay delivery receipts, folded from the event stream by the store so a
// 已送达 stamp survives leaving and re-entering this panel.
const delivery = store.delivery;
/** The words beside a peer's newest send result, empty when there is none. */
function deliveryText(peerId: string) {
  const status = delivery.lastStatus(String(peerId));
  return status ? deliveryLabel(status) : "";
}
/** The glyph for that same result, in the window's own icon set. */
const deliveryGlyphs = { check: Check, x: X, clock: Clock };
function deliveryGlyph(peerId: string) {
  const status = delivery.lastStatus(String(peerId));
  return status ? deliveryIcon(status) : null;
}
const password = ref("");
const deleteItem = ref<HistoryItem | null>(null);
const batchDeleteIds = ref<string[] | null>(null);
const clearHistoryOpen = ref(false);
const revokeDevice = ref<Device | null>(null);
const forgetDevice = ref<Device | null>(null);
/** The internet-paired device a 解除互联网配对 is being confirmed for.  It has
 * its own dialog rather than sharing 撤销信任's: that one names a pinned
 * certificate this machine holds and the pairing repository it drops, and an
 * internet peer has neither — what ends is a code pairing, and the sentence
 * that says so is a different sentence. */
const relayUnpairDevice = ref<Device | null>(null);
const purgeDevice = ref<Device | null>(null);
const probeResults = ref<Record<string, DeviceProbeResult>>({});
/** What the rename dialog is open on: a device on this machine's list, whose
 * alias is rewritten from the context menu (the row's name *is* the alias, and
 * the menu entry is the write with a dialog in front of it, which is what the
 * legacy 重命名 was), or a peer on the internet-pairing page,
 * whose local alias is rewritten from the peer line.  One dialog serves both
 * because the field and the two buttons are the same; only the call 保存 makes
 * differs.  The internet page used to ask with `window.prompt`, which neither
 * WKWebView nor WebKitGTK implements — on the macOS and Linux bundles the click
 * opened nothing at all, so the peer could not be renamed there at all. */
type RenameTarget =
  | { kind: "device"; id: string }
  | { kind: "internet"; peerId: string };
const renameTarget = ref<RenameTarget | null>(null);
const renameValue = ref("");
/** The conversation the chat page should open when it next mounts, handed over
 * by 打开聊天 on a device row.  The chat page owns which session is selected —
 * it has to, since it is the one polling the list — so this is a request rather
 * than a selection. */
const chatSessionToOpen = ref("");
/** The device whose 打开聊天 is waiting for its answer, empty when none is.
 *
 * The row buttons disable on it and the icon on that row spins, which is the
 * pattern 测试连接 already uses (`probeBusyId`).  It exists because the invite had
 * none: the button stayed live and looked dead for as long as the sidecar took to
 * answer, so a second click sent a second invite. */
const chatConnectingId = ref("");
const probeBusyId = ref("");
/** The device this window has just asked its engine to dial, empty when none.
 *
 * The connect/join RPC answers as soon as the dial *starts*, so `busy` (the
 * store's request flag) is true for a fraction of the dial.  The row then sits
 * on 连接中 for up to twelve seconds while the transport tries, and a click in
 * that window used to start a second full dial to the same peer.  This stays set
 * for exactly that window: until the row stops saying a dial is in flight, until
 * a `device.connect_failed`/`device.connected` refresh settles it, or until the
 * local answer window (`DIAL_ANSWER_MS`) runs out. */
const dialingId = ref("");
/** The local answer window for a dial, in ms.
 *
 * The backend's own window is `PAIRING_SEND_WAIT` (12 s, `lan.py`), and the
 * transport's dial timeout is 10 s; this is the point at which the window says
 * "it is taking too long" rather than leaving the reader looking at 连接中 with
 * nothing that explains it.  The engine still owns the outcome — this only
 * stops the button from being held forever, and the eventual
 * `device.connect_failed`/`device.connected` still lands on its own. */
const DIAL_ANSWER_MS = 10_000;
let dialAnswerTimer: ReturnType<typeof setTimeout> | undefined;
/** Whether the dial-busy state may be cleared by the device list yet.
 *
 * The `action` that starts the dial refreshes the list before it answers, so
 * the row can still be `offline` for the instant between the click and that
 * refresh.  Clearing on the first list update would drop the busy state before
 * the dial was even visible; it is armed only after the call has answered. */
let dialBusyArmed = false;
function clearDialing() {
  clearTimeout(dialAnswerTimer);
  dialAnswerTimer = undefined;
  dialBusyArmed = false;
  dialingId.value = "";
}
/** Settle the busy state from the device list: a row that is no longer being
 *  dialled (and is not waiting on a pairing answer) has an outcome. */
function updateDialingFromDevices(devices: Device[]) {
  if (!dialingId.value || !dialBusyArmed) return;
  const row = devices.find((device) => device.id === dialingId.value);
  if (!row) { clearDialing(); return; }
  if (row.connection_state !== "connecting" && !pairingInFlight(row)) clearDialing();
}
watch(() => state.devices, (devices) => updateDialingFromDevices(devices));
/** Run one dial and hold the row's buttons until it has an answer.
 *
 * `run` is the store's own action, so it keeps owning the busy flag, the
 * refresh and the error band; this only adds the "a dial is in flight" fact the
 * engine cannot answer in advance. */
async function dialDevice(device: Device, run: () => Promise<boolean>) {
  if (dialingId.value) return;
  dialingId.value = device.id;
  dialBusyArmed = false;
  let started = false;
  try {
    started = await run();
  } catch {
    started = false;
  }
  if (!started) { clearDialing(); return; }
  dialBusyArmed = true;
  updateDialingFromDevices([...state.devices]);
  clearTimeout(dialAnswerTimer);
  dialAnswerTimer = setTimeout(() => {
    dialAnswerTimer = undefined;
    // Another row may have taken over in the meantime; this timer belongs to
    // the dial it was armed for.
    if (dialingId.value !== device.id) return;
    // The engine may still be working, so the row's own state is left alone;
    // what ends here is the button being held and the silence.
    const name = deviceLabel(device);
    clearDialing();
    announce(t("连接 {name} 仍未完成，设备可能不在线", { name }));
  }, DIAL_ANSWER_MS);
}
const devicesRefreshing = ref(false);
/** The device an update offer is being sent to, and what came back. */
const updateBusyId = ref("");
const updateNotes = ref<Record<string, string>>({});
/** The device this machine is asking for a newer installer. */
const fetchBusyId = ref("");
/** The device this machine is asking for its log, and what came back.
 *
 * Kept apart from the update notes because the two are answers to different
 * questions, and a device can be mid-way through both. */
const logBusyId = ref("");
const logNotes = ref<Record<string, string>>({});
/** Whether the 收集所有设备日志 click is still asking.  The request is done the
 *  moment every device has been dialed (the logs land afterwards, on their own),
 *  so this is short — it is what keeps a second click from doubling the asks. */
const collectingLogs = ref(false);
const certificates = ref<DeviceCertificate[] | null>(null);
const certDialog = ref<HTMLDialogElement | null>(null);
const certAlertDialog = ref<HTMLDialogElement | null>(null);
/** Whether the certificate prompt is on screen.
 *
 * Tracked rather than read back off the node: `open` is the DOM's flag, and a
 * stubbed `showModal` (the jsdom tests) never sets it — which is also why the
 * flag follows the dialog's own close event instead of being written twice.
 */
const certPromptOpen = ref(false);
/** The peer a certificate-change prompt is about, named as well as it can be. */
const certAlertName = computed(() =>
  state.certAlert ? state.certAlert.name || state.certAlert.device_id : "");
/** Show the prompt while the alert stands, and close it once it is answered.
 *
 * The dialog is not dismissible (no close button, and Escape is prevented): the
 * transport stops reconnecting to a peer whose pin it refused, so a dismissed
 * prompt would leave that device silently unreachable until the app restarts.
 * A later alert replaces the message in place, which is how "trust again"
 * becomes available once the device has connected with its new certificate.
 *
 * One of the few dialogs that does not go through `useDialog`: it is driven by
 * an alert object rather than by a flag it owns, and it refuses to close on the
 * user's behalf, so the "is it up" guard is real here (`showModal()` on an
 * already-open dialog throws) rather than the tick the composable supplies.
 */
watch(() => state.certAlert, async (alert) => {
  await nextTick();
  const dialog = certAlertDialog.value;
  if (!dialog) return;
  if (alert && !certPromptOpen.value) {
    certPromptOpen.value = true;
    dialog.showModal();
  } else if (!alert && certPromptOpen.value) {
    dialog.close();
  }
});
async function acceptCertAlert() {
  if (state.certAlert) await store.retrust(state.certAlert.device_id);
}
async function rejectCertAlert() {
  // The alert is the only place this device may be known; unpairing needs
  // nothing but its id.
  if (state.certAlert) await store.keepUnpaired(state.certAlert.device_id);
}
const sendUrlDevice = ref<Device | null>(null);
const sendUrlValue = ref("");
const sendUrlBusy = ref(false);
// Who the dialog can send to. A row's button targets exactly that device; a
// request from the phone offers every connected peer, like the legacy picker.
const sendUrlCandidates = ref<Device[]>([]);
const sendUrlDeviceId = computed({
  get: () => sendUrlDevice.value?.id || "",
  set: (id: string) => {
    sendUrlDevice.value = sendUrlCandidates.value.find((device) => device.id === id) || null;
  },
});
const pushTextOpen = ref(false);
const pushTextValue = ref("");
const pushTextBusy = ref(false);
const logsOpen = ref(false);
const logLines = ref<string[]>([]);
/** The subset of `logLines` that reports a failure — what the dialog opens on. */
const logProblems = ref<string[]>([]);
const logView = ref<"problems" | "all">("problems");
const logCount = ref(200);
const logsBusy = ref(false);
const logsExporting = ref(false);
const logExportMessage = ref("");
const aboutOpen = ref(false);
const aboutBusy = ref(false);
const aboutMessage = ref("");
const qrOpen = ref(false);
const qrBusy = ref(false);
const qrImage = ref("");
const qrUrl = ref("");
const qrMessage = ref("");
const qrShareBusy = ref(false);
const qrShareMessage = ref("");
let offMenuAction: (() => void) | undefined;
let offFileDrop: (() => void) | undefined;
let offHotkey: (() => void) | undefined;
/** Whether files are being dragged over the window at this instant.
 *
 * The window is the drop target, so the whole window has to say so: a file
 * dragged in from the desktop can land anywhere on it, and a hint drawn on one
 * card would be a target the reader cannot aim at. */
const dropActive = ref(false);
/** The paths the OS handed this window, on their way to the page that can send
 *  them.
 *
 * Held here rather than sent from here: a drop names files but not a machine,
 * and whichever page takes them is where the recipient is decided — the
 * transfers page's device picker, or the conversation the chat page has open.
 * Cleared by that page once it has them, so a later visit does not re-stage a
 * finished drop. */
const droppedPaths = ref<string[]>([]);
/** The chat page could not take a drop: hand it to the transfers page.
 *
 * `paths` is the array the page was shown, so it is usually already the staged
 * one.  Re-assigning the same reference would re-trigger the page's watcher on
 * the way out, so only a different array is stored. */
function chatDropFallback(paths: string[]) {
  if (!paths.length) return;
  if (droppedPaths.value !== paths) droppedPaths.value = paths;
  openPage("transfers");
}
/** The device a row's 发送文件… named, for the transfers page to start on.
 *
 * Preselecting was deliberately not done on that page — a file is the one thing
 * here that cannot be taken back, so the machine it goes to is named rather than
 * assumed.  This is the other half of that rule: the reader named it first, in
 * the row's own menu, and the page then starts on it instead of asking twice.
 * Cleared when the page is left, so a later visit does not carry the last send's
 * target into the next one. */
const transferPreselect = ref("");
const restartOpen = ref(false);
const diagnosticsOpen = ref(false);
const diagnosticsReport = ref<DiagnosticsReport | null>(null);
const diagnosticsBusy = ref(false);
const diagnosticsRepairBusy = ref(false);
const updateChecking = ref(false);
const autoUpdateCheck = ref(true);
const autoUpdateCheckBusy = ref(false);
// The lifecycle lives in the store (fed by `update.state` events) so the
// panel and the event handler cannot disagree about the phase.
const updateState = computed(() => store.state.update);
const updateAvailable = computed(() => store.state.updateCheck?.available === true);
const updateLatest = computed(() => store.state.updateCheck?.latest || "");
// Whether the card offers the install this application performs itself. The
// host answers it by matching this machine's bundle against the release
// manifest, so it is a different question from "is there a newer version" --
// and only a *definite* no takes the install away. The host leaves the field
// off when it could not read the manifest at all, which says nothing about what
// this build can do; reading that as "no" is what turned a dropped connection
// into an instruction to replace the application by hand, when the same click
// would have installed it once the network was back.
const updateInstallable = computed(() => store.state.updateCheck?.installable !== false);
// Set when the install was attempted and the archive turned out to be one this
// build cannot replace itself with -- a `.dmg`, a `.deb`, an AppImage: files a
// person opens. The host answers that with the folder revealed rather than with
// an error, so without this the click ended in silence over a card still saying
// the update was ready. Kept apart from `updateInstallable`, which is the
// manifest's answer before anything was tried, because the two arrive from
// different places and either one is reason enough to say the rest is manual.
const updateReadyManual = ref(false);
// Said on the window's own status line rather than left to the card, because
// the card is not where the reader is looking: the update that installs without
// any click here is the one a peer sent, and a window that disappears into an
// installer with nothing said reads as a crash. The card draws the same
// sentence for the installs the reader did ask for.
watch(() => updateState.value.phase, (phase, previous) => {
  if (phase === "installing" && previous !== "installing") {
    announce(t("正在安装更新，完成后应用会自动重启。"));
  }
});
// Fixed group order, mirroring the legacy diagnostics panel: a group missing
// from the payload is skipped rather than rendered empty.
const diagnosticGroupOrder = ["system", "network", "internet", "filesystem"];
const discoveryState = ref<{ enabled: boolean; visible: boolean } | null>(null);
const discoveryBusy = ref(false);
/** The live rows, ordered the way the tray orders its own list.
 *
 * The sidecar hands its rows over in device-id order, which is stable but says
 * nothing: a machine that just appeared sat wherever its id happened to fall,
 * so the reader had to scan the whole list to find the device they came for.
 * `deviceRank` is the tray's ranking — what is up now, then a handshake on its
 * way, then the devices this machine knows and cannot reach, then the ones
 * merely seen — and within a rank the names are read the way a reader reads
 * them rather than the way a byte comparison does, which for a list of Chinese
 * names is not the same order at all.
 *
 * Sorted by what each row *shows* — a device the user renamed sorts under the
 * name they gave it, not under the one its owner published.  That is the one
 * place this list can differ from the tray, which has no room for the label.
 */
const activeDevices = computed(() => state.devices
  .filter((device) => !device.archived)
  .sort((left, right) =>
    deviceRank(left) - deviceRank(right)
    || deviceLabel(left).localeCompare(deviceLabel(right), undefined, { numeric: true, sensitivity: "base" })
    || left.id.localeCompare(right.id)));
const archivedDevices = computed(() => state.devices.filter((device) => device.archived));
/** The rows this page's LAN list owns.  Internet-paired peers have their own
 * card above; drawing them in both places was two copies of one device with
 * two different sets of actions. */
const lanDevices = computed(() => activeDevices.value.filter((device) => !device.relay));
const native = inDesktop();
const ready = computed(() => state.status?.health === "ready");
/** The static boot frame leaves only once the window has something to show.
 *
 * It is outside `#app`, so it covers the shell while the sidecar is still
 * starting; a status (ready or locked) or a startup error is the first point at
 * which the window can render a real page or a real failure. */
watch(
  () => [state.status, state.error] as const,
  ([status, error]) => {
    if (status || error) document.getElementById("boot")?.remove();
  },
  { immediate: true },
);
let themeGeneration = 0;
function applyTheme(mode: unknown) {
  ++themeGeneration;
  applyAppearanceTheme(mode);
}
/** The theme the sidecar has, as opposed to the one on screen.
 *
 * They are the same value except during the moment a pick is in flight, and
 * again after one failed: the window then wears a theme nothing stored, and
 * this is what it is put back to. */
const storedAppearance = ref<string>("system");
/** Pick a theme: it takes effect now, and it is stored now.
 *
 * A dropdown that colours the window only after 保存 asks the reader to imagine
 * the result, and the theme is the one setting where seeing it *is* the
 * decision.  Applied and persisted together rather than previewed and then
 * rolled back on leaving the page: a preview that snaps back is a choice the
 * reader made and then lost, and it would leave the window wearing a theme the
 * config does not have — the exact state the rollback exists to prevent.
 *
 * The write is per pick rather than per keystroke (a select), and it is small:
 * one field, applied live by the sidecar's own settings-change path. */
let appearanceGeneration = 0;
async function previewAppearance() {
  const mode = String(settings.value.appearance_mode || "system");
  applyTheme(mode);
  const generation = ++appearanceGeneration;
  try {
    await bridge.updateSettings({ appearance_mode: mode });
    if (generation !== appearanceGeneration) return;
    storedAppearance.value = mode;
    // Keeps the form's own bookkeeping in step: the field is saved, not
    // pending (and the snapshot does not carry it either way).
    foldSavedField("appearance_mode", mode);
  } catch (error) {
    if (generation !== appearanceGeneration) return;
    // Refused, so the window goes back to what is stored rather than wearing a
    // colour the config does not have.
    settings.value.appearance_mode = storedAppearance.value;
    applyTheme(storedAppearance.value);
    store.setError(error);
  }
}
/** Honour the saved "Enable animations" preference.
 *
 * The shell keeps its motion to the few seconds-long transitions it actually
 * needs — a notice appearing, a progress bar filling — so this class is what
 * makes the setting mean anything here.  Anything that reads as "working"
 * (the spinners) is deliberately not part of it: freezing one turns a busy app
 * into a hung one, which is the opposite of what turning motion off is for.
 */
function applyMotion(enabled: unknown) {
  document.documentElement.classList.toggle("no-motion", enabled === false);
}
/** The animation switch, stored where it is flipped.
 *
 * Same model as the theme and the language, because it is the same kind of
 * setting: a property of how the window looks, which the reader can see the
 * moment they change it.  It used to apply live and persist only on 保存, so a
 * reader who flipped it and walked away had motion off until the next start and
 * a save bar still offering to save it — a save that would then write a value
 * the window had already been showing.  See U183. */
let motionGeneration = 0;
async function previewMotion() {
  const enabled = settings.value.ui_animation_enabled !== false;
  applyMotion(enabled);
  const generation = ++motionGeneration;
  try {
    await bridge.updateSettings({ ui_animation_enabled: enabled });
    if (generation !== motionGeneration) return;
    foldSavedField("ui_animation_enabled", enabled);
  } catch (error) {
    if (generation !== motionGeneration) return;
    // Refused, so the window goes back to what is stored rather than keeping a
    // motion setting the config does not have.
    settings.value.ui_animation_enabled = !enabled;
    applyMotion(settings.value.ui_animation_enabled);
    store.setError(error);
  }
}
// Live, so flipping the switch on the settings page is visible where it is
// flipped rather than after a restart.
watch(() => settings.value.ui_animation_enabled, applyMotion);
watch(ready, async (isReady) => {
  const generation = ++themeGeneration;
  if (!isReady) return;
  try {
    const loaded = await bridge.settings();
    if (generation === themeGeneration) {
      storedAppearance.value = String(loaded.settings.appearance_mode || "system");
      applyTheme(storedAppearance.value);
    }
    applyMotion(loaded.settings.ui_animation_enabled);
    // Language applies even if the theme generation moved on: the shell must
    // render in the saved language either way.
    setLocale(loaded.settings.language);
    // Seeded here as well as by the settings form: a pause armed before this
    // window opened (or restored from disk on a restart) still counts down, and
    // a user who never opens the settings page would otherwise never see it.
    pauseUntil.value = Number(loaded.settings.timed_pause_until || 0);
    maybePromptLanguage(loaded.settings.language_chosen);
    // The phone service is read here as well as on the way into the two pages
    // that show it, because the window opens on one of them: a switch drawn
    // from a status nobody has read yet would show the service as off until
    // the reader happened to visit the devices page.
    void refreshCompanion();
  }
  catch { /* Settings can be retried by opening the settings view. */ }
});
// The first-run onboarding: shown once per session and only while the sidecar
// says no language has ever been chosen.  Choices are drafts until 完成:
// language and theme preview immediately, and neither is written until that
// one explicit action.  Closing the process before then leaves the flag False,
// so the next launch asks again.
const languagePromptOpen = ref(false);
let languagePrompted = false;
const languageDialog = useDialog(languagePromptOpen);
const onboardingLanguage = ref("");
const onboardingTheme = ref("system");
const onboardingStep = ref(1);
const onboardingBusy = ref(false);
const onboardingError = ref("");
function maybePromptLanguage(chosen: unknown) {
  if (languagePrompted || chosen !== false) return;
  languagePrompted = true;
  onboardingLanguage.value = "";
  onboardingTheme.value = String(storedAppearance.value || "system");
  onboardingStep.value = 1;
  onboardingError.value = "";
  languagePromptOpen.value = true;
}
/** A picker choice previews the shell without persisting it. */
function selectOnboardingLanguage(code: string) {
  onboardingLanguage.value = code;
  setLocale(code);
}
function selectOnboardingTheme(mode: string) {
  onboardingTheme.value = mode;
  applyTheme(mode);
}
/** The one explicit action that ends onboarding and stores both choices. */
async function finishOnboarding() {
  if (!onboardingLanguage.value || onboardingBusy.value) return;
  onboardingBusy.value = true;
  onboardingError.value = "";
  try {
    await bridge.updateSettings({
      language: onboardingLanguage.value,
      appearance_mode: onboardingTheme.value,
    });
    setLocale(onboardingLanguage.value);
    storedAppearance.value = onboardingTheme.value;
    applyTheme(onboardingTheme.value);
    if (settingsLoaded.value) {
      foldSavedField("language", onboardingLanguage.value);
      foldSavedField("appearance_mode", onboardingTheme.value);
    }
    // Closing the dialog is the last thing here: a failed write leaves the
    // choices on screen rather than dropping the reader into a shell that did
    // not save what the button promised.
    languagePromptOpen.value = false;
  } catch (error) {
    onboardingError.value = String((error as { message?: string })?.message || t("保存失败"));
  } finally {
    onboardingBusy.value = false;
  }
}
/** Switch the shell immediately and persist the choice from Settings. */
async function applyLanguage(code: string) {
  setLocale(code);
  // A choice made anywhere (picker or settings) retires the picker.
  languagePrompted = true;
  try {
    // The sidecar flips `language_chosen` when `language` is updated, so this
    // is what stops the picker from returning next launch.
    await bridge.updateSettings({ language: code });
    if (settingsLoaded.value) foldSavedField("language", code);
  } catch (error) { store.setError(error); }
}
/** The first-run picker shows every option in BOTH languages. */
const languageChoices = computed(() => [
  { code: "zh-CN", native: LOCALE_NAMES["zh-CN"], other: "Simplified Chinese" },
  { code: "zh-TW", native: LOCALE_NAMES["zh-TW"], other: "Traditional Chinese" },
  { code: "en", native: "English", other: "English" },
]);
const themeChoices = computed(() => [
  { id: "system", label: t("跟随系统") },
  { id: "light", label: t("浅色") },
  { id: "dark", label: t("深色") },
  { id: "sepia", label: t("暖色") },
  { id: "midnight", label: t("午夜") },
  { id: "contrast", label: t("高对比") },
]);
const overviewAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("overview.get"));
const favoritesAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("favorites.list"));
const syncLabel = computed(() => state.status?.sync_state === "running" ? t("同步运行中")
  : state.status?.sync_state === "paused" ? t("同步已暂停") : t("同步引擎未启动"));
/** Whether copies are currently going nowhere.  The status bar is where this is
 * said, and it used to say it in the same 11px grey as the version number: the
 * one line in the window that answers "I copied it and nothing happened" read
 * like a footnote.  Amber and a pause mark, in the same bar and at the same
 * size — a banner over the content would say it twice. */
const syncStopped = computed(() => !!state.status && state.status.sync_state !== "running");
// The timed pause, as the dashboard's quick controls had it: the deadline the
// runtime armed (epoch seconds, in the settings payload this shell already
// fetches), re-read on a 15 s tick so the remaining minutes count down, and
// refetched once when it passes so a deadline the host has already cleared
// stops being shown.  The resume itself happens in the host — it publishes the
// change, and the store refreshes the status on any event.
const nowTick = ref(Date.now());
const pauseBusy = ref(false);
/** The deadline the runtime armed, in epoch seconds.
 *
 * It arrives in the settings payload, which is where the shell reads it from,
 * but it is not a field the settings page edits — a pause is armed by a button
 * on the overview and cleared by the runtime when it passes.  Held here rather
 * than in `settings` because that object is what the save bar compares: a
 * deadline written into the form made the bar claim an unsaved change to a
 * form nobody had touched, and with no card to mark, since the control that
 * armed it is on another page. */
const pauseUntil = ref(0);
let pauseExpiredSyncing = false;
let pauseTickTimer: ReturnType<typeof setInterval> | undefined;
const pauseUntilMs = computed(() => Number(pauseUntil.value || 0) * 1000);
const pauseLeftMs = computed(() => Math.max(0, pauseUntilMs.value - nowTick.value));
// Whole minutes left, rounded up: a small remainder still reads 1 so the label
// never flashes "0" in the second before the host resumes.
const pauseLeftMinutes = computed(() => Math.max(1, Math.ceil(pauseLeftMs.value / 60000)));
const sendUrlAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("url.send"));
const discoveryAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("discovery.status"));
const pushTextAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("clipboard.push"));
const diagnosticsAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("diagnostics.report"));
/** Why the button above is greyed out, when it is.
 *
 * Both of its reasons are invisible from here: one is the runtime not being
 * up, which this card says nowhere, and the other is a sidecar too old to
 * answer the call, which is a version the window never shows.  A dead button
 * on a card whose whole subject is "find out what is wrong" is the one place
 * that cannot be left unexplained.
 */
const diagnosticsUnavailable = computed(() => {
  if (diagnosticsAvailable.value) return "";
  return ready.value
    ? t("当前版本的服务端不支持诊断。")
    : t("同步引擎未运行，无法运行诊断。");
});
const updateAvailableForUi = computed(() => ready.value && !!state.status?.capabilities?.includes("update.status"));
// The devices page's one line about the engine it needs.  Two of that page's
// controls go grey without it — the toolbar's push button and the send-URL
// button on every paired row — and they read two different capability strings,
// "clipboard.push" and "url.send".  The sidecar grants both from the one
// condition (the runtime is running or paused), so the line below is written
// once for both and appears when either is missing; two lines saying the same
// sentence one after the other is what this avoids.
const devicesEngineStopped = computed(() => !pushTextAvailable.value || !sendUrlAvailable.value);
const diagnosticGroups = computed(() => {
  const report = diagnosticsReport.value;
  if (!report) return [];
  return diagnosticGroupOrder
    .filter((id) => report.groups?.[id])
    .map((id) => ({ id, label: report.groups[id].label_text || id, items: report.groups[id].items || [] }));
});
const diagnosticsOverview = computed(() => {
  const report = diagnosticsReport.value;
  if (!report) return "";
  const parts = [t("版本 {version}", { version: report.version })];
  if (report.lan_ip) parts.push(t("本机地址 {ip}", { ip: report.lan_ip }));
  parts.push(t("已连接 {count} 台", { count: report.connected_count }),
    t("已配对 {count} 台", { count: report.paired_count }));
  parts.push(report.web_companion_running
    ? t("网页伴侣运行中（端口 {port}）", { port: report.web_port })
    : t("网页伴侣未开启"));
  return parts.join(" · ");
});
// The local-network permission repair has no v2 group item of its own; it hangs
// off the flat `permissions` check, exactly as the legacy panel's flat list does.
const permissionsRepairCheck = computed(() =>
  diagnosticsReport.value?.checks?.find((check) => check.id === "permissions" && !check.ok) || null);
/** The once-only note about what the window's × does.
 *
 * `CloseRequested` hides the window to the tray and keeps syncing — which is the
 * point of the application and is said nowhere.  A reader who presses ×
 * reasonably believes it has exited, and then sees its clipboard still going out.
 * The note is put in front of them at the start rather than in a dialog on the
 * first close: a modal arriving mid-task is the worse interruption, and by then
 * the belief is already formed.
 *
 * Remembered in `localStorage` because it is a fact about this installation and
 * nothing else reads storage here — the sidecar owns every real setting.  Only in
 * the desktop window: a browser tab has no tray.
 */
const busy = computed(() => state.pending || state.refreshing || !ready.value);
/** Anything that makes a history row's buttons unusable right now. */
const historyBusy = computed(() => busy.value || state.loading);
/** Say so when a press was dropped, and shake the control it came from.
 *
 * The store answers one write at a time and drops the second press; that is
 * right, but it used to be silent.  Two surfaces answer together because either
 * alone is missable: the control itself moves, and the status bar — the part of
 * the window that stays on screen — says what is happening.  The shake is
 * dropped under `prefers-reduced-motion` (see the `nudge` rule in styles.css),
 * and the status line still says it, so nothing depends on the animation. */
let nudgeTimer: ReturnType<typeof setTimeout> | undefined;
/** Ask for the taskbar's attention when a request arrives while the window is
 *  away.  The stack is on screen but nobody is looking at it: a pairing request,
 *  an incoming file and an invitation to chat all have a deadline, and the phone
 *  page, the tray and the relay all keep working while this window is hidden. */
watch(() => state.notices.length, () => {
  const last = state.notices[state.notices.length - 1];
  if (!last) return;
  if (last.title === "pairing.request" || last.title === "transfer.request"
    || last.title === "chat.invite") void requestAttention();
});
watch(() => state.refused, () => {
  const target = document.activeElement;
  if (target instanceof HTMLElement && target !== document.body) {
    target.classList.remove("nudge");
    void target.offsetWidth;              // restart the animation on a second drop
    target.classList.add("nudge");
    clearTimeout(nudgeTimer);
    nudgeTimer = setTimeout(() => target.classList.remove("nudge"), 420);
  }
  announce(t("正在处理上一步…"), 1800);
});
/* The three panes that own their own buttons report through one of these: the
 * notice stack is the window's, and which page a message belongs to is the
 * window's to say — the pane only knows what happened, not whose colour the
 * notice should carry. */
const notifyFavorites = (message: string) => store.toast("ui.favorites", message);
const notifyTransfers = (message: string) => store.toast("ui.transfers", message);
const notifyChat = (message: string) => store.toast("ui.chat", message);
async function refreshDevices() {
  // A scan is a blocking round on the sidecar (announce, query, wait out the
  // settle window — about 1.5 s), so a second click used to queue another whole
  // round and the button looked dead in between.  The flag is the button's own;
  // the store's `busy` cannot cover the round because the read has not started
  // yet.
  if (devicesRefreshing.value) return;
  devicesRefreshing.value = true;
  try {
    // Before the read, not after: the list the store is about to read is the one
    // this asks for. A device that is on the network but has not been heard from
    // for a while — the thing this button exists for — is not on the list until
    // somebody asks, and that is what the scan does: it announces this machine,
    // queries the LAN and waits for the answers. The store's own read would only
    // report what the last background round happened to leave behind.
    const scan = await bridge.scanDevices();
    await store.refresh();
    // The scan answers with the discovery switches as well, and a scan that
    // could not run (browsing switched off) is an empty round, not a network
    // without devices.  Saying "refreshed · N devices" over that would be a
    // reader believing a switch that is off was just exercised.
    if (scan && scan.enabled === false) {
      store.toast("ui.devices", t("局域网发现已关闭，未扫描网络（已重新读取 {count} 台设备）", {
        count: activeDevices.value.length,
      }));
    } else {
      // The rows above 已移除的设备, which is the list this page is showing: a
      // removed device is not one a refresh just went and found.
      store.toast("ui.devices", t("已刷新 · {count} 台设备", { count: activeDevices.value.length }));
    }
  } catch (reason: any) {
    // A scan that could not run at all used to be an unhandled rejection: the
    // click produced neither a row change nor a word.
    store.fail(reason?.message || t("刷新设备失败"));
  } finally {
    devicesRefreshing.value = false;
  }
}
const batchDeleteValid = computed(() => !!batchDeleteIds.value?.length &&
  batchDeleteIds.value.every((id) => state.selectedIds.includes(id)));
async function confirmBatchDelete() {
  const ids = batchDeleteIds.value;
  if (!ids) return;
  if (await store.batchDelete(ids)) {
    // The dialog closes on the same click that commits, so without the count
    // the reader cannot tell whether three rows went or thirty.
    store.toast("ui.history", t("已删除 {count} 条", { count: ids.length }));
    batchDeleteIds.value = null;
  }
}
const batchDeleteDialog = useDialog(batchDeleteIds);
// A page turn or a new search is a different set of rows, so a confirm left
// open over the old ones is dropped rather than applied to whatever is under it.
watch(() => [state.query, state.offset], () => { batchDeleteIds.value = null; }, { flush: "sync" });
/** Empty the history, and say how much went. */
async function confirmClearHistory() {
  const cleared = await store.clearHistory();
  if (cleared) {
    clearHistoryOpen.value = false;
    store.toast("ui.history", t("已清空 {count} 条历史记录", { count: cleared }));
  }
}
const clearHistoryDialog = useDialog(clearHistoryOpen);
onUnmounted(() => {
  clearStatus();
  clearTimeout(dialAnswerTimer);
  clearTimeout(nudgeTimer);
});
async function pauseSync(minutes: number) {
  if (pauseBusy.value) return;
  pauseBusy.value = true;
  try {
    const result = await bridge.pauseSync(minutes);
    // The sidecar armed the auto-resume timer and reports the deadline it
    // armed, so the countdown is the runtime's number rather than one this
    // window guessed from the duration it asked for.
    pauseUntil.value = Number(result?.until || 0);
    nowTick.value = Date.now();
    await store.refresh();
    store.toast("ui.sync", t("同步已暂停 {minutes} 分钟", { minutes }));
  }
  catch (reason: any) { store.fail(reason?.message || t("暂停同步失败")); }
  finally { pauseBusy.value = false; }
}
async function resumeSync() {
  if (pauseBusy.value) return;
  pauseBusy.value = true;
  try {
    await bridge.resumeSync();
    pauseUntil.value = 0;
    nowTick.value = Date.now();
    await store.refresh();
    store.toast("ui.sync", t("同步已恢复"));
  }
  catch (reason: any) { store.fail(reason?.message || t("恢复同步失败")); }
  finally { pauseBusy.value = false; }
}
function pairingPending(device: Device) {
  return !device.paired && PAIRING_LIVE_STATUSES.includes(String(device.pairing_status || ""));
}
/** Whether a click on this row can place a call to that device.
 *
 * `dialable` is the sidecar's own answer, and the only one that can be right:
 * it counts the address a peer's call taught the transport and the one written
 * down the last time this machine could reach it, so a device that has gone
 * quiet while an address is still on file reads 离线 and can be dialed at the
 * same time.  Gating a button on the connection state instead is what left it
 * disabled on exactly the devices whose only way back was to be called.
 *
 * The field is absent only on a sidecar older than it, where the old reading —
 * a connection that is not there — is all there is to go on.
 *
 * The two update entries are gated on it too, and they were the worst case of
 * the old reading.  Both directions ride the LAN link and nothing else — the
 * sidecar drops an ``update_offer`` that arrives over the relay — so gating them
 * on 在线 looked right, but the runtime only ever dials *paired* peers: an
 * unpaired device is sighted and never connected, its state never reads 在线, and
 * the 发送更新 button beside its version chip was disabled for good — which is
 * what made a push look like something a pairing was needed for first.  Both
 * ends of the offer have always supported an unpaired peer: it dials with
 * auto-pairing switched off, and the receiving side checks what arrives against
 * the published release digest — so the sighting was the only thing standing in
 * the way, and `dialable` counts the sighting.
 *
 * What the old reading did get right is that a live mDNS record outlives the
 * connection: a peer whose port is blocked, or one that has just left, keeps
 * naming the version it runs.  `dialable` is that same judgment made by the
 * sidecar, which is the side that knows whether an address is still on file, so
 * the click still ends in a dial and 无法连接到该设备 rather than a dead button.
 */
function canDial(device: Device) {
  return device.dialable ?? device.connection_state !== "offline";
}
/** The line under a row's heading.
 *
 * The device id, and — when the heading is a name the user gave the device
 * rather than the one it goes by — the name the peer published about itself as
 * well.  A reader who renamed a device here should still be able to match the
 * row against the tray, the chat list and the pairing card, which call it by
 * its own name, and this is the one line where the two can meet. */
function deviceSubtitle(device: Device) {
  if (deviceLabel(device) === device.name) return device.id;
  return [device.name, device.id].filter(Boolean).join(" · ");
}
/** What a confirm dialog calls the machine it is about.
 *
 * `deviceLabel` needs a device, and these four dialogs are in the DOM from the
 * first render — the ref is null until one of them is opened on a row.  The
 * name is the one this machine keeps, because the dialogs are the last thing
 * read before a pairing is dropped or a device is forgotten for good, and a
 * machine called by a name the reader never typed is not a machine they can
 * be sure they meant. */
function deviceNamed(device: Device | null): string {
  return device ? deviceLabel(device) : "";
}
function pairingLabel(device: Device) {
  if (device.paired) return t("已配对");
  // Paired, just not here: a device holding a code pairing and no LAN pairing
  // is not an unpaired device, and 未配对 beside an 互联网·在线 chip said it was.
  // The 本地 chip beside this one still carries the local half of the answer.
  if (relayPairing(device).paired) return t("互联网配对");
  // A request that ended without pairing.  Read from the store's short-lived
  // record rather than from the row: the row's status for an ended request is
  // the terminal one until a new request replaces it, and rendering it from
  // there would leave 已取消 on a device the user unpaired months ago.
  const ended = store.pairingEndedStatus(device.id);
  if (ended === "expired") return t("已过期");
  if (ended === "cancelled") return t("已取消");
  // 已过期 is its own word, not 已取消: a request that ran out of time was not
  // refused by anybody, and the two ask the reader for different things — try
  // again deliberately, rather than go looking for a fault on the other end.
  return ({ pending: t("等待确认"), peer_confirmed: t("对方已确认"), confirmed_waiting: t("等待对方确认"),
    cancelled: t("已取消"), expired: t("已过期") } as Record<string, string>)[device.pairing_status] || t("未配对");
}
/** Whether this row should draw the outcome of a pairing that just ended.
 *
 * `pairingLabel` only reaches the screen for a live handshake (or this short
 * record); a request that expired used to make the confirm card vanish with no
 * word anywhere, so the row keeps the word for a minute. */
function pairingEndedLabel(device: Device) {
  const status = store.pairingEndedStatus(device.id);
  if (status === "expired") return t("已过期");
  if (status === "cancelled") return t("已取消");
  return "";
}
/** A device's local link, as the chip under its name shows it.
 *
 * A paired peer that has gone away is retried, and while it is, the transport
 * flips the connection state between 连接中 and 离线 on every attempt — a chip
 * that flickered between two words said less than 离线 did, because it never
 * said the app was still trying.  The attempt count is the whole difference
 * between a retry in flight and a peer that is gone, so it replaces the state
 * while there is one, and it is the one reading the legacy panel and the
 * phone's device page both use.
 *
 * The count stands alone rather than after 本地·: a route name in front of it
 * read as a third route beside 本地 and 互联网, when it is a state of the first
 * one. */
function connectionLabel(device: Device) {
  if (device.reconnecting) {
    const attempt = device.reconnect_attempt || 0;
    const max = device.reconnect_max || 0;
    // A ceiling of zero is no ceiling — the same reading `max_attempts` gets in
    // the transport — so the count stands alone rather than as "2/0", which
    // would name a limit of zero attempts for a retry that is on its second.
    return max ? t("重连中 {attempt}/{max}", { attempt, max }) : t("重连中 {attempt}", { attempt });
  }
  return ({ discovered: t("已发现"), connecting: t("连接中"), online: t("在线"), offline: t("离线") } as Record<string, string>)[device.connection_state] || t("连接状态未知");
}
/** The local chip's words, which during a retry are the count alone. */
function localChannelLabel(device: Device) {
  return device.reconnecting ? connectionLabel(device) : `${t("本地")}·${connectionLabel(device)}`;
}
/** The local chip's colour, which during a retry is the in-flight one rather
 *  than the 离线 it would otherwise read as. */
function localChannelState(device: Device) {
  return device.reconnecting ? "connecting" : (device.connection_state || "offline");
}

/** The state chip's words: one word for each state in `device-row.ts`.
 *
 * 正在同步 is the one that says content moves.  The other five are about a link or a pairing, and
 * each is distinct: 发现 and 连接中 are not 离线, because the device is there — one has been seen
 * and not dialed, the other is being dialed — and 离线 is a claim about the device rather than
 * about the link.
 *
 * The statuses a handshake passes through are not here — see `pairingInFlight`,
 * which the row shows beside this chip while one is live, because a request
 * waiting on an answer is something to act on rather than a state the device has
 * settled into. */
function deviceStatusLabel(device: Device) {
  return ({
    syncing: t("正在同步"),
    connected: t("已连接"),
    paired: t("已配对"),
    connecting: t("连接中"),
    discovered: t("发现"),
    offline: t("离线"),
  } as Record<string, string>)[deviceStatus(device)] || t("离线");
}
/** The state chip's colour, reusing the channel shades the two chips it
 *  replaces already used. */
function deviceStatusState(device: Device) {
  return ({
    syncing: "paired",
    connected: "online",
    paired: "paired",
    // The in-flight shade, which `localChannelState` already uses for a retry and
    // which is what separates "the link is being made" from both ends.
    connecting: "connecting",
    discovered: "connecting",
    offline: "offline",
  } as Record<string, string>)[deviceStatus(device)] || "offline";
}
/** The state chip's icon: the pairing shield where a pairing is half of what
 *  the word means, the plug where a link is what it is about.
 *
 * A positive list rather than "connected or offline gets the plug", which is how the first version
 * was written: the two words added for 连接中 and 发现 would have fallen into the shield by not
 * being named, and the shield claims a pairing on a row that has none. */
function deviceStatusIcon(device: Device) {
  const status = deviceStatus(device);
  // 正在同步 and 已配对 are the two whose word contains the pairing.
  if (status === "syncing" || status === "paired") return ShieldCheck;
  return Plug;
}
/** What the one word cannot carry: which route it is reachable on, and what a
 *  retry is doing.  Both used to be a chip of their own. */
function deviceStatusTitle(device: Device) {
  if (device.relay) return `${t("互联网配对")} · ${connectionLabel(device)}`;
  return localChannelLabel(device);
}

/** A device's internet pairing, joined to the device row by device id.
 *
 * The relay's peer list and this machine's device list are two views of the
 * same machines, keyed by the same device id, and the row showed only the
 * local half of it: 已配对 · 在线 said nothing about *which* link that 在线 was
 * — a peer sitting on the relay with no route on this network read exactly
 * like a peer that is here, and the reader had to open the pairing card and
 * match device ids by eye to find out.  This is that join, per row. */
function relayPeerFor(device: Device) {
  return internetPairing.value.peers.find(peer => String(peer.peer_id) === device.id) || null;
}
/** This device's internet pairing: whether it holds one, whether the relay sees
 *  it up, and when it was last heard from.
 *
 * The row carries all three — on every row, not only the ones the pairing drew,
 * because a device can be paired by code *and* on this network and then one row
 * answers for both joins.  The pairing card's list is the fallback for a row
 * without the fields: an archived row, which the sidecar leaves out of the join
 * because it is no longer a device, or a sidecar older than them.
 *
 * Reading the row rather than the card is also what keeps this fresh: the card
 * is re-read when it changes, while these rows are rebuilt several times a
 * second, so a peer whose relay link has gone quiet stops reading 在线 without
 * anything having to be published for it.
 */
function relayPairing(device: Device) {
  if (typeof device.relay_paired === "boolean") {
    return {
      paired: device.relay_paired,
      online: device.relay_online === true,
      last_seen: Number(device.last_seen || 0),
    };
  }
  const peer = relayPeerFor(device);
  return { paired: !!peer, online: peer?.online === true, last_seen: Number(peer?.last_seen || 0) };
}
/** Whether the relay sees this device up, for a row that holds an internet
 *  pairing at all.
 *
 * There is no third state to name here any more: the chip is drawn only on a
 * row that has an internet pairing (see the device row), so 未配对 is the
 * absence of the chip rather than a word in it.  It used to be a word — on
 * every row, beside a local 未配对 that is about a different pairing, which put
 * the same answer twice on a device that had simply never been paired by code.
 *
 * A device paired by code carries the relay's own view of itself on its own row
 * — it is drawn from the pairing, and the page re-reads its rows on every
 * change — so that reading wins over the join below, which is refreshed only
 * when the pairing card is and would be the stale one of the two.
 */
function relayChannel(device: Device): "online" | "offline" {
  return relayPairing(device).online ? "online" : "offline";
}
function relayChannelLabel(device: Device) {
  return relayChannel(device) === "online" ? t("在线") : t("离线");
}
/** When this device was last heard from over the relay, as a line for the row's
 * tooltip: the fact a bare 离线 leaves out, and the one that separates "away
 * for a minute" from "gone since Tuesday". */
function relayLastSeen(device: Device) {
  const seen = relayPairing(device).last_seen;
  if (!seen) return "";
  return t("最后在线 {when}", { when: dateTime(seen) });
}
/**
 * Turn this device's share of the clipboard on or off.
 *
 * The refresh is not decoration: the row's checkbox is drawn from
 * `device.syncs_to`, which the *sidecar* answers, and it stores the exceptions
 * rather than the answers.  Writing the box's own state optimistically would
 * leave the UI showing a setting the config does not hold whenever the two
 * disagree -- and they can, because the id the row carries may be the hashed
 * mDNS form while the switch is written under the real one.
 *
 * The checkbox is put back from the refreshed list rather than left where the
 * click put it, so a failure does not leave a switch that looks on and is not.
 */
async function toggleDeviceSync(device: Device, event: Event) {
  const enabled = (event.target as HTMLInputElement).checked;
  try { await bridge.setDeviceSync(device.id, enabled); await store.refresh(); }
  catch (reason: any) {
    (event.target as HTMLInputElement).checked = device.syncs_to !== false;
    store.fail(reason?.message || t("设置剪贴板同步失败"));
  }
}
/** Rename *this* machine, from the overview's own card.
 *
 * One field rather than the settings form's save: that form is only hydrated
 * once its page has been opened, and this card is not on it — and the name is
 * a fact about the device that the sidebar's footer and every peer's device
 * list also show, so the status is re-read here to put it everywhere at once.
 * The settings page takes its own copy from the backend the next time it is
 * opened, which is why nothing else has to be told. */
async function renameThisDevice(name: string) {
  try {
    await bridge.updateSettings({ device_name: name });
    await store.refresh();
    store.toast("ui.devices", t("设备名称已更新"));
  } catch (reason: any) {
    // store.fail, not a bare string: the band renders .message/.code/.retryable.
    store.fail(reason?.message || t("重命名失败"));
  }
}
/** Confirm the alias the rename dialog collected.  A device's name goes through
 * the backend's one user-editable name per device — the same alias the row
 * shows — rather than inventing a second one; a peer on the
 * internet-pairing page has its own local alias and its own call. */
async function confirmRename() {
  const target = renameTarget.value;
  const alias = renameValue.value.trim();
  if (!target) return;
  try {
    if (target.kind === "device") {
      await bridge.setDeviceNote(target.id, alias);
      await store.refresh();
    } else {
      await bridge.renameInternetPeer(target.peerId, alias);
      await refreshInternetPairing();
    }
    renameTarget.value = null;
    // The device list is one page of many, so its rename keeps the toast that
    // names the page it happened on.  The internet peer is renamed on the page
    // the reader is standing on and the line under the pointer changes with it,
    // so that one reports on the always-visible status line instead.
    if (target.kind === "device") store.toast("ui.devices", t("已重命名为 {name}", { name: alias }));
    else announce(t("已重命名为 {name}", { name: alias }));
  } catch (reason: any) {
    // store.fail, not a bare string: the band renders .message/.code/.retryable.
    store.fail(reason?.message || t("重命名失败"));
  }
}
const renameDialog = useDialog(renameTarget);
async function confirmRevoke() {
  const current = state.devices.find((device) => device.id === revokeDevice.value?.id && device.paired);
  if (current && await store.unpair(current)) revokeDevice.value = null;
}
const revokeDialog = useDialog(revokeDevice);
async function confirmForget() {
  const target = forgetDevice.value;
  if (target && await store.forget(target)) forgetDevice.value = null;
}
/** Break an internet pairing from the device list.
 *
 * The write is the pairing card's own `unpairInternet`, so the two places a
 * pairing can be ended end it the same way — including the call that drops the
 * peer's queued relay frames, and the re-read that takes the row off the list.
 * The dialog closes only on success, and a refusal is said on the status line:
 * closing it either way would leave the device on screen with nothing said
 * about why the click did nothing.
 */
async function confirmRelayUnpair() {
  const target = relayUnpairDevice.value;
  if (!target) return;
  if (await unpairInternet(target.id)) relayUnpairDevice.value = null;
  else announce(t("解除互联网配对失败"));
}
const relayUnpairDialog = useDialog(relayUnpairDevice);
async function confirmPurge() {
  const target = purgeDevice.value;
  if (target && await store.purge(target)) purgeDevice.value = null;
}
const forgetDialog = useDialog(forgetDevice);
const purgeDialog = useDialog(purgeDevice);
function channelLabel(channel: string) {
  return channel === "lan" ? t("局域网") : t("中继");
}
function probeErrorLabel(error: string | null) {
  return ({ timeout: t("超时"), send_failed: t("发送失败"), relay_offline: t("中继离线") } as Record<string, string>)[error || ""] || t("失败");
}
function probeLabel(result: DeviceProbeResult) {
  if (!result.results.length) return t("无可用通道");
  return result.results.map((row) => row.ok
    ? `${channelLabel(row.channel)} ${Math.round(row.latency_ms ?? 0)}ms`
    : `${channelLabel(row.channel)} ${probeErrorLabel(row.error)}`).join(" · ");
}
async function testConnection(device: Device) {
  if (probeBusyId.value) return;
  probeBusyId.value = device.id;
  try {
    const result = await store.probe(device);
    if (result) probeResults.value = { ...probeResults.value, [device.id]: result };
  } finally {
    probeBusyId.value = "";
  }
}
/** Offer this build to a device that is on an older one.
 *
 * Needs no pairing: the peer answers with a request for the installer, and the
 * bytes it receives are checked against the published release digest on its
 * side before they can be installed.  The row says what happened either way —
 * a click that produced no traffic must not look like one that did.
 *
 * What is sent is the installer this machine's own upgrade kept, so the click
 * either transfers the file or comes back with a reason.  It never downloads:
 * fetching the release here to hand to a device that can fetch it itself is the
 * same file from the same place, twice, on the machine that does not need it —
 * and a machine with nothing kept answers `update.no_asset` instead, which the
 * row shows as it stands.
 */
async function offerDeviceUpdate(device: Device) {
  if (updateBusyId.value) return;
  updateBusyId.value = device.id;
  try {
    const { reason } = await bridge.offerDeviceUpdate(device.id);
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: offerNote(device, reason),
    };
  } catch (error: any) {
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: error?.message || t("发送更新失败"),
    };
  } finally {
    updateBusyId.value = "";
  }
}
/** What the device row says after a 发送更新 click, per the peer's answer.
 *
 * The click used to report that a frame had left this machine, which was also
 * true of an offer the peer threw away — the button said 已把更新发送给 X and
 * nothing on the other device ever moved.  The sidecar now waits for the answer
 * (`lan.py::_await_offer_answer`), so this can report what actually happened:
 * the peer asking for the asset is the only outcome that is a send. */
function offerNote(device: Device, reason: string) {
  const name = deviceLabel(device);
  switch (reason) {
    case "accepted":
      return t("已把更新发送给 {name}", { name });
    case "not_newer":
      return t("{name} 的版本不比本机旧，不需要这个更新", { name });
    case "other_platform":
      return t("{name} 与本机不是同一个平台，装不了这个安装包", { name });
    case "no_asset":
      return t("对方没有可以发送的安装包");
    default:
      return t("已通知 {name}，但对方没有回应", { name });
  }
}
/** Ask a device that is on a newer build to send this one its installer.
 *
 * The direction the feature is meant to run in, and the reason the older
 * device's list carries a button at all: this side is the one that is behind.
 * The blob is verified against the published release digest on arrival and then
 * staged, and a staged archive is installed by itself — so this function owes
 * the row a sentence for the wait and nothing else.  It used to say the install
 * was the reader's to do from the update page, which stopped being true when
 * both hosts started applying what the sidecar stages.
 */
async function fetchDeviceUpdate(device: Device) {
  if (fetchBusyId.value) return;
  fetchBusyId.value = device.id;
  // Said before the request goes out, not after it comes back.  The ask is answered by the peer,
  // which means the round trip is however long that device takes -- and a click with nothing on
  // screen for that long reads as a button that did not work, which is how it was reported: the
  // click appeared to start a transfer and then nothing happened, so it was clicked several more
  // times.  The note is replaced by the real answer below.
  updateNotes.value = {
    ...updateNotes.value,
    [device.id]: t("正在向 {name} 索取安装包…", { name: deviceLabel(device) }),
  };
  try {
    await bridge.fetchDeviceUpdate(device.id);
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: t("已向 {name} 索取安装包，收到后会自动安装并重启", { name: deviceLabel(device) }),
    };
  } catch (error: any) {
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: error?.message || t("获取更新失败"),
    };
  } finally {
    fetchBusyId.value = "";
  }
}
/** Ask one device for its log.
 *
 * Needs no pairing, and the peer only answers if its own 允许其他设备取走本机日志
 * switch is on — in which case what arrives is a redacted copy.  The request
 * returns as soon as it is away; the file lands later and the row is told when
 * it does, so the note left here is the promise, not the receipt.
 */
async function fetchDeviceLog(device: Device) {
  if (logBusyId.value) return;
  logBusyId.value = device.id;
  try {
    await bridge.collectDeviceLog(device.id);
    logNotes.value = {
      ...logNotes.value,
      [device.id]: t("已向 {name} 索取日志，收到后放进「下载/ClipSync-logs」", { name: deviceLabel(device) }),
    };
  } catch (error: any) {
    logNotes.value = {
      ...logNotes.value,
      [device.id]: error?.message || t("索取日志失败"),
    };
  } finally {
    logBusyId.value = "";
  }
}
/** The devices a 收集所有设备日志 click is still waiting on.
 *
 * The click is over the moment every device has been dialed; the logs land
 * afterwards, one at a time over the next seconds.  This is what turns those
 * arrivals back into the one thing the click promised: when the last of them is
 * answered — with a log, with a refusal, or with a failure — the folder they
 * went into is opened, once, already full.
 *
 * A device that never answers keeps the list non-empty and nothing opens; that
 * is the one case the reader has to press the button for, and the button is
 * beside the click that started it. */
const collectPending = ref<string[]>([]);
/** Ask every device on the network for its log, in one click.
 *
 * One dial per device, all of them at once: the sidecar answers with which
 * devices it asked, and each log is announced as it lands.
 */
async function collectAllLogs() {
  if (collectingLogs.value) return;
  collectingLogs.value = true;
  try {
    const result = await bridge.collectAllLogs();
    logNotes.value = {};
    collectPending.value = result.devices.map((device) => device.device_id);
    announce(result.requested
      ? t("已向 {count} 台设备索取日志", { count: result.requested })
      : t("没有可索取日志的设备"));
  } catch (error: any) {
    announce(error?.message || t("收集日志失败"));
  } finally {
    collectingLogs.value = false;
  }
}
/** Take one device off the pending list; open the folder when it empties. */
function settleCollectedLog(deviceId: string) {
  if (!collectPending.value.includes(deviceId)) return;
  collectPending.value = collectPending.value.filter((id) => id !== deviceId);
  if (!collectPending.value.length) void openLogsFolder();
}
async function openLogsFolder() {
  try {
    await bridge.openLogsFolder();
  } catch (error: any) {
    announce(error?.message || t("打开日志文件夹失败"));
  }
}
/** A fingerprint in groups of four, which is the unit it is checked in.
 *
 *  The dialog showed `fingerprint_short` and nothing else, and a short value
 *  cannot be compared character by character with the one on the other screen —
 *  which is the only reason the dialog exists at all.  The full value is already
 *  in what the sidecar sends; grouping it is what makes 64 hex characters a
 *  thing a reader can follow line by line instead of losing their place in.
 *  See U122.
 */
function fingerprintGroups(value: string) {
  const hex = String(value || "").replace(/[^0-9a-fA-F]/g, "");
  if (!hex) return "";
  return (hex.match(/.{1,4}/g) || []).join(" ");
}

/** Every pinned fingerprint as one block of text, for pasting somewhere it can
 *  be compared against — the other machine's own copy, or a message to whoever
 *  is standing at it. */
function allFingerprints() {
  return (certificates.value || [])
    .map(item => `${item.device_name || item.device_id}  ${fingerprintGroups(item.fingerprint) || item.fingerprint_short || t("未固定")}`)
    .join("\n");
}

async function showCertificates() {
  const items = await store.certificates();
  if (!items) return;
  certificates.value = items;
  await nextTick();
  certDialog.value?.showModal();
}
function openSendUrl(device: Device) {
  sendUrlCandidates.value = [device];
  sendUrlValue.value = "";
  sendUrlDevice.value = device;
}
/** Open the send-URL dialog for a request that arrived from the phone.
 *
 * Legacy resolved the target itself: no connected peer → toast, exactly one →
 * send without asking, several → pick. Here the dialog's device select is that
 * picker, preselecting the only candidate.
 */
function openSendUrlFromHost() {
  if (!sendUrlAvailable.value) { announce(t("发送网址不可用")); return; }
  const candidates = state.devices.filter((device) => device.paired && device.connection_state === "online");
  if (!candidates.length) { announce(t("没有已连接的设备可以发送。")); return; }
  sendUrlCandidates.value = candidates;
  sendUrlValue.value = "";
  sendUrlDevice.value = candidates[0];
}
const sendUrlDialog = useDialog(sendUrlDevice, () => {
  // Leaving the dialog forgets who it was pointed at: the picker is rebuilt per
  // request, and a stale candidate list would offer a device this request has
  // nothing to do with.
  sendUrlCandidates.value = [];
});
/** The address as the sidecar will read it: a bare host gets a scheme.
 *
 * People type `example.com`, and a URL without one is not a URL — so the check
 * below is made against what would actually be sent, not against the keystrokes.
 * A scheme the sidecar refuses (`javascript:`, `file:`) is still refused here.
 */
const sendUrlNormalized = computed(() => {
  const raw = sendUrlValue.value.trim();
  if (!raw) return "";
  return /^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`;
});
/** What is wrong with the address as typed, or "" when it can be sent.
 *
 * The field is `type="url"` but sits in no `<form>`, so the browser's own check
 * never ran: `example.com` and `javascript:…` both travelled to the sidecar and
 * came back as a 400 on the band at the top of the window — a long way from the
 * box that caused it. */
const sendUrlProblem = computed(() => {
  if (!sendUrlValue.value.trim()) return "";
  try {
    const url = new URL(sendUrlNormalized.value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return t("只支持 http 或 https 开头的网址");
    return "";
  } catch {
    return t("请输入完整网址，例如 https://example.com");
  }
});
const sendUrlValid = computed(() => !!sendUrlValue.value.trim() && !sendUrlProblem.value);
async function confirmSendUrl() {
  const device = sendUrlDevice.value;
  const url = sendUrlNormalized.value;
  if (!device || !url || sendUrlProblem.value) return;
  sendUrlBusy.value = true;
  try {
    // Keep the dialog open on failure so the typed URL is not lost.
    if (await store.sendUrl(device, url)) {
      sendUrlDevice.value = null;
      store.toast("ui.devices", t("已发送网址到 {name}", { name: deviceLabel(device) }));
    }
  } finally {
    sendUrlBusy.value = false;
  }
}
function openPushText() {
  pushTextValue.value = "";
  pushTextOpen.value = true;
}
const pushTextDialog = useDialog(pushTextOpen);
async function confirmPushText() {
  const text = pushTextValue.value.trim();
  if (!text) return;
  pushTextBusy.value = true;
  try {
    // Keep the dialog open on failure so the typed text is not lost.
    const result = await store.pushText(text);
    if (result) {
      pushTextOpen.value = false;
      store.toast("ui.devices", result.sent
        ? t("已推送到本机剪贴板并同步到所有设备")
        : t("已推送到本机剪贴板（同步未开启，未广播）"));
    }
  } finally {
    pushTextBusy.value = false;
  }
}
const logsDialog = useDialog(logsOpen);
async function loadLogs(lines = logCount.value) {
  logsBusy.value = true;
  try {
    const result = await bridge.readLogs(lines);
    logLines.value = result.logs;
    logProblems.value = result.problems ?? [];
    logCount.value = lines;
  } catch (error) { store.setError(error); }
  finally { logsBusy.value = false; }
}
async function openLogs() {
  logsOpen.value = true;
  logExportMessage.value = "";
  await loadLogs();
  // Open on the lines worth reading, and fall back to the whole log when there
  // is nothing to read for: an empty "problems" tab over a log full of lines
  // reads as "no logs", which is the state this view used to be stuck in.
  logView.value = logProblems.value.length ? "problems" : "all";
}
const shownLogLines = computed(() =>
  logView.value === "problems" ? logProblems.value : logLines.value);
function logExportFilename() {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `clipsync_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.log`;
}
async function exportLogs() {
  logsExporting.value = true;
  logExportMessage.value = "";
  try {
    // The host opens a native save dialog; the sidecar copies the log there.
    const result = await bridge.exportLogs(logExportFilename());
    if (!result.cancelled) logExportMessage.value = t("已导出：{path}", { path: result.path });
  } catch (error) { store.setError(error); }
  finally { logsExporting.value = false; }
}
const qrDialog = useDialog(qrOpen);
/** Read the same-network QR without opening its dialog: the companion card draws
 * it inline, and the dialog is only the enlarged copy. */
async function loadCompanionQrPreview(force = false) {
  if ((!force && !companion.value?.running) || qrBusy.value) return;
  qrBusy.value = true;
  qrMessage.value = "";
  try {
    const result = await bridge.companionQr();
    qrUrl.value = result.url || "";
    if (result.ok && result.qr) qrImage.value = result.qr;
    else {
      qrImage.value = "";
      qrMessage.value = result.error === "COMPANION_NOT_RUNNING" ? t("手机服务未启动") : t("二维码不可用");
    }
  } catch (error) { store.setError(error); }
  finally { qrBusy.value = false; }
}
async function openCompanionQr() {
  qrShareMessage.value = "";
  qrOpen.value = true;
  void loadCompanionQrPreview(true);
}
/** Hand a local file to the phone, the legacy 发送文件到手机按钮.
 *
 * The file is copied into the directory the phone's Files tab lists, so it is
 * downloadable without pairing — the same one-way push the legacy Tk dialog
 * made, offered here from the QR dialog because that is the surface the phone
 * is reachable from.  The host only picks the path; the sanitising, the
 * collision naming and the copy belong to the sidecar, beside the directory it
 * owns.
 */
async function shareFileToPhone() {
  if (qrShareBusy.value) return;
  qrShareBusy.value = true;
  qrShareMessage.value = "";
  try {
    const path = await bridge.chooseFile("any");
    if (!path) return;
    const result = await bridge.shareFileToPhone(path);
    qrShareMessage.value = t("已发送到手机：{name}", { name: result.name });
  } catch (error) { store.setError(error); }
  finally { qrShareBusy.value = false; }
}
// The phone's panel has no window of its own: its QR and send-URL buttons
// arrive as events (see the store) and are answered by this window.
watch(() => state.hostRequest, (request) => {
  if (!request) return;
  if (request.kind === "qr") void openCompanionQr();
  else openSendUrlFromHost();
});
/** The window's one cross-list search.  See `QuickSearch.vue` and U50.
 *
 * The flag is the dialog's open state, and the dialog owns the palette: closing
 * it resets the palette, so reopening it is an empty box rather than the last
 * question with the last answer under it. */
const quickSearchOpen = ref(false);
const quickSearch = ref<InstanceType<typeof QuickSearch> | null>(null);
const quickSearchDialog = useDialog(quickSearchOpen, () => quickSearch.value?.reset());
/** Ctrl/Cmd+K: the chord every application with a search box in it answers to,
 * and this one had three boxes and no way to reach any of them by word. */
function openQuickSearch() {
  quickSearchOpen.value = true;
}
/** Hand the word over to the page that holds it.
 *
 * The palette found it; the page is where the rest of it is — the full set, the
 * filters, and the row actions — so the hand-over sets that page's own search
 * rather than keeping the answer in a dialog that is about to close.  The
 * history page's box is fed the same way a keystroke in it would be (the write
 * lock and the debounce are its own business), and the library's search is
 * given no group: the count in the palette was every favourite that holds the
 * word, and a page still filtered to a group would show fewer than that.
 */
function quickSearchHandOver(scope: "history" | "favorites", query: string) {
  const term = query.trim();
  quickSearchOpen.value = false;
  if (scope === "favorites") {
    openPage("favorites");
    store.favorites.search(term, "");
    return;
  }
  openPage("history");
  // The page owns its box, so the word is handed over once the page has been
  // drawn: `openPage` only sets the flag, and the component mounts after it.
  if (term) void nextTick(() => historyView.value?.search(term));
}

const aboutDialog = useDialog(aboutOpen);
/** The chords this window answers to, in one place they can be read.
 *
 * Every one of them already had a home — `aria-keyshortcuts` on the nav rows,
 * a tooltip on each, the list's own keys nowhere at all — which is a shortcut
 * only for a reader who already knows it exists.  A panel that takes a global
 * chord of its own would be a new thing to learn, for four lines of text; a
 * folded block in 关于 costs nothing until someone opens it. */
const shortcutRows = computed(() => [
  { keys: `${MODIFIER.label}+1 … ${MODIFIER.label}+8`, label: t("切换左侧页面") },
  { keys: `${MODIFIER.label}+F`, label: t("在当前页搜索") },
  { keys: `${MODIFIER.label}+K`, label: t("搜索全部历史与收藏") },
  { keys: `${MODIFIER.label}+,`, label: t("打开设置") },
  { keys: `${MODIFIER.label}+S`, label: t("保存在设置页或 AI 配置页上改过的东西") },
  { keys: t("↑ ↓"), label: t("在历史列表中移动") },
  { keys: "Enter", label: t("复制选中的记录") },
  { keys: t("Delete"), label: t("删除选中的记录") },
  { keys: `${MODIFIER.label}+A`, label: t("全选当前页的记录") },
  { keys: "Esc", label: t("清空搜索框") },
]);
function openAbout() {
  aboutMessage.value = "";
  aboutOpen.value = true;
}
async function openAboutLink(target: "homepage" | "releases" | "sponsor") {
  aboutBusy.value = true;
  aboutMessage.value = "";
  try {
    const result = await bridge.openAboutLink(target);
    aboutMessage.value = t("已在浏览器打开：{url}", { url: result.url });
  } catch (error) { store.setError(error); }
  finally { aboutBusy.value = false; }
}
/** The Sponsors link leaves the window: it is the one page the app does not
 *  draw, and opening it in the browser keeps the wallet/payment surface out of
 *  the clipboard process entirely. */
async function openSponsor() {
  try {
    await bridge.openAboutLink("sponsor");
  } catch (error) { store.setError(error); }
}
const restartDialog = useDialog(restartOpen);
async function confirmRestart() {
  restartOpen.value = false;
  // The host exits this process: nothing after the call is guaranteed to run.
  await bridge.restartApp();
}
const diagnosticsDialog = useDialog(diagnosticsOpen);
function diagnosticLabel(item: DiagnosticItem) {
  return item.label_text || item.id;
}
/** Whether a group passed, warned or failed, from its own checks.
 *
 * A group with nothing in it is a warning, not a pass: an unreadable section
 * is not the same as a healthy one, and the legacy panel read it the same way
 * (`diagnostics-panel.js`, `groupStatus`). */
function diagnosticVerdict(group: { items: DiagnosticItem[] }) {
  if (!group.items.length) return "warn";
  if (group.items.some((item) => item.status === "fail")) return "fail";
  if (group.items.some((item) => item.status === "warn")) return "warn";
  return "ok";
}
function diagnosticVerdictLabel(status: string) {
  return ({ ok: t("正常"), warn: t("警告"), fail: t("失败") } as Record<string, string>)[status] || status;
}
/** The mark beside a check: the same three the legacy panel drew. */
function diagnosticGlyph(status: string) {
  return ({ ok: "✓", warn: "!", fail: "✕" } as Record<string, string>)[status] || "·";
}
function diagnosticDetail(item: DiagnosticItem) {
  return item.detail_text || item.detail || "";
}
function diagnosticHint(item: DiagnosticItem) {
  return item.hint_text || item.hint || "";
}
function diagnosticsCheckText(check: DiagnosticCheck) {
  return check.detail_text || check.detail || "";
}
function diagnosticsSummaryLabel(status: string) {
  return ({ ok: t("全部正常"), warn: t("存在警告"), fail: t("存在故障") } as Record<string, string>)[status] || status;
}
/** What the last copy said, for the line above the dialog's buttons. */
const diagnosticsCopied = ref("");
/** The report as one block of text.
 *
 * A bug report used to be a screenshot of this dialog, retyped: the version,
 * the OS, and seven checks that the window was already holding as strings.  The
 * text is the dialog's own rows in the dialog's own order — same labels, same
 * glyphs, same hints — so what is copied and what is read cannot drift apart. */
function diagnosticsText(): string {
  const report = diagnosticsReport.value;
  if (!report) return "";
  const lines = [
    t("ClipSync 诊断报告"),
    `${t("时间")}：${new Date().toLocaleString()}`,
    `${t("版本")}：${report.version}（${report.os}）`,
    `${t("总体状态")}：${diagnosticsSummaryLabel(report.summary)}`,
    `${t("概览")}：${diagnosticsOverview.value}`,
  ];
  for (const group of diagnosticGroups.value) {
    lines.push("", `${group.label} — ${diagnosticVerdictLabel(diagnosticVerdict(group))}`);
    for (const item of group.items) {
      const detail = diagnosticDetail(item);
      lines.push(`${diagnosticGlyph(item.status)} ${diagnosticLabel(item)}${detail ? `：${detail}` : ""}`);
      const hint = diagnosticHint(item);
      if (hint) lines.push(`    ${hint}`);
    }
  }
  return lines.join("\n");
}
async function copyDiagnostics() {
  const copied = await copyText(diagnosticsText());
  diagnosticsCopied.value = copied ? t("诊断结果已复制，可以直接粘贴到反馈里。") : "";
}
async function refreshDiagnostics(force = false) {
  if (diagnosticsBusy.value) return;
  diagnosticsBusy.value = true;
  try {
    // Not forced on open: a report younger than a minute is still the answer,
    // and the whole suite — every socket probe and, on macOS, a firewall
    // subprocess — is only worth running again when the reader asks for a new
    // one (see `rerunDiagnostics`).
    const report = await store.diagnostics(force);
    if (report) diagnosticsReport.value = report;
  } finally { diagnosticsBusy.value = false; }
}
async function openDiagnostics() {
  if (!diagnosticsAvailable.value) return;
  diagnosticsOpen.value = true;
  diagnosticsReport.value = null;
  diagnosticsCopied.value = "";
  await refreshDiagnostics();
}
/** 重新检测, which reports itself.
 *
 * Each check reads the machine afresh and most of them come back the same, so
 * the scan is worth a word — including the count, because how many of the
 * checks passed is the answer the button was pressed for. */
async function rerunDiagnostics() {
  await refreshDiagnostics(true);
  const checks = diagnosticsReport.value?.checks || [];
  // A pending check has not had its chance yet, so it is neither a pass nor a
  // failure and is left out of both halves of the count — the same reading
  // `summarize` gives it, so this line cannot contradict the verdict above it.
  const settled = checks.filter((check) => !check.pending);
  store.toast("ui.settings", t("已重新检测 · {ok}/{total} 项通过", {
    ok: settled.filter((check) => check.ok).length,
    total: settled.length,
  }));
}
async function repairDiagnostics(kind: DiagnosticAction) {
  if (diagnosticsRepairBusy.value) return;
  diagnosticsRepairBusy.value = true;
  try {
    const result = await store.repairDiagnostics(kind);
    if (!result) return;
    if (result.ok === false) {
      store.setError({ code: "DIAG_REPAIR_FAILED", message: result.error || t("无法打开系统设置"), retryable: false });
      return;
    }
    store.toast("ui.settings", kind === "firewall" ? t("已请求修复防火墙规则") : t("已打开本地网络权限设置"));
    // The elevated rule may land a moment later, so re-scan after a pause.
    if (kind === "firewall") setTimeout(() => { if (diagnosticsOpen.value) void refreshDiagnostics(); }, 2500);
  } finally { diagnosticsRepairBusy.value = false; }
}
/** Which step the update was on when it failed.
 *
 * 「更新失败」 alone named neither the step nor a way out: a check that could not
 * reach the API and an install that was refused by the host read the same, and
 * both left the card with nothing to press.
 */
const updateAttempt = ref<"check" | "download" | "install">("check");
const updateStageLabel = computed(() => ({
  check: t("检查更新"), download: t("下载更新"), install: t("安装更新"),
} as Record<string, string>)[updateAttempt.value]);

async function checkForUpdate() {
  updateAttempt.value = "check";
  if (updateChecking.value || updateState.value.phase === "downloading"
    || updateState.value.phase === "installing") return;
  updateChecking.value = true;
  try {
    const result = await store.checkUpdate();
    // "unknown" and "up to date" must not look alike: only a real answer with
    // no newer release claims the app is current.  And "unknown" must say what
    // it was: the reply carries the check's own reason, and without it a
    // blocked API host, a rate limit and a dead network are one sentence.
    if (!result) announce(t("无法连接更新服务器"));
    else if (!result.available) {
      if (result.latest) announce(t("已是最新版本"));
      else announce(t("检查更新失败") + (result.error ? `：${result.error}` : ""));
    }
  } finally { updateChecking.value = false; }
}
async function startUpdateDownload() {
  updateAttempt.value = "download";
  // Returns immediately; the panel renders progress from `update.state` events.
  const result = await store.downloadUpdate();
  if (!result || !result.ok) {
    announce(t("更新下载失败") + (result?.error ? `：${result.error}` : ""));
  }
}
async function installUpdate() {
  updateAttempt.value = "install";
  // A successful install replaces this process, so a reply is already a
  // refusal. The host publishes the failing phase before it returns, and the
  // card renders that, so the only case left to speak to is "nothing to do".
  const result = await store.installUpdate();
  if (!result) announce(t("更新安装失败"));
  else if (!result.installed) announce(t("已是最新版本"));
}
async function installReadyUpdate() {
  updateAttempt.value = "install";
  // The install for an archive that is already on disk: this machine's own
  // verified download, or the one a peer sent. The host prefers the manifest's
  // payload when it has one and installs the staged file when it does not -- so
  // no fetching is promised here, and the two answers that come back are the
  // ones worth naming. A `.dmg` or a `.deb` is a file a person opens, and this
  // build can only hand it over: the sentence stays on the card, under the path
  // it is about, rather than on a status line that expires in five seconds.
  const result = await store.installReadyUpdate();
  if (!result) announce(t("更新安装失败"));
  else if (!result.installed) {
    updateReadyManual.value = result.reason === "manual";
    if (!updateReadyManual.value) announce(t("已是最新版本"));
  }
}
async function openUpdateFolder() {
  const result = await store.openUpdateFolder();
  if (!result || !result.ok) announce(result?.error || t("无法打开所在文件夹"));
}
async function toggleAutoUpdateCheck(next: boolean) {
  if (autoUpdateCheckBusy.value) return;
  autoUpdateCheckBusy.value = true;
  try {
    const response = await bridge.updateSettings({ auto_update_check: next }) as
      { updated?: Record<string, unknown> } | undefined;
    const updated = response?.updated;
    if (updated) Object.assign(settings.value, updated);
    // Prefer what was persisted over what was clicked.
    autoUpdateCheck.value = typeof updated?.auto_update_check === "boolean"
      ? updated.auto_update_check : next;
  } catch {
    // Stay truthful to the persisted setting rather than the click.
    autoUpdateCheck.value = !next;
    announce(t("保存设置失败"));
  } finally { autoUpdateCheckBusy.value = false; }
}
async function loadDiscovery() {
  if (!discoveryAvailable.value) return;
  const state = await store.discovery();
  if (state) discoveryState.value = state;
}
async function toggleDiscovery(enabled: boolean) {
  if (discoveryBusy.value) return;
  discoveryBusy.value = true;
  try {
    const state = await store.setDiscoveryEnabled(enabled);
    if (state) discoveryState.value = state;
  } finally {
    discoveryBusy.value = false;
  }
}
async function toggleVisibility(hidden: boolean) {
  if (discoveryBusy.value) return;
  discoveryBusy.value = true;
  try {
    const state = await store.setDiscoveryVisible(!hidden);
    if (state) discoveryState.value = state;
  } finally {
    discoveryBusy.value = false;
  }
}
const health = computed(() => !native ? t("浏览器预览") : state.error ? t("连接异常")
  : state.status?.health === "locked" ? t("等待解锁") : ready.value ? t("已连接") : t("正在启动"));

async function unlock() {
  const value = password.value;
  password.value = "";
  await store.unlock(value);
}
async function confirmDelete() {
  if (deleteItem.value) await store.delete(deleteItem.value);
  deleteItem.value = null;
}
const deleteDialog = useDialog(deleteItem);
/** The sidebar, top to bottom: what Ctrl+1…8 counts.  The numbers belong to the
 * rows the user can see, which is why this list has to stay in the order the
 * template renders them and not in whatever order would read better here. */
const PAGES = ["overview", "history", "devices", "favorites", "transfers", "chat", "ai", "settings", "companion", "internet"] as const;
/** How the chords are *named* — the tooltip and `aria-keyshortcuts`.  The
 * handler takes Ctrl or Cmd on either platform; a Mac user hunting for ⌘ should
 * not be told Ctrl. */
const MODIFIER = navigator.userAgent.includes("Mac") ? { label: "Cmd", key: "Meta" } : { label: "Ctrl", key: "Control" };
/** The chord the OS itself pastes with, as a reader would press it.
 *
 * Used in the two sentences that mean "the clipboard has it, the paste did not
 * happen": an injected paste that the OS refused, and the row menu's
 * paste-elsewhere when the host could not type the chord.  It is not a hotkey
 * binding — those are the reader's own and are read from the form — it is the
 * system's, which is why it is named from `MODIFIER.label` and not from config. */
const PASTE_SHORTCUT = MODIFIER.label === "Cmd" ? "Cmd+V" : "Ctrl+V";
/** The digit each page answers to, derived from `PAGES` so the two cannot drift:
 * the handler matches the key the user pressed against this, and the tooltip and
 * `aria-keyshortcuts` are built from the same index. */
const CHORD_DIGITS = PAGES.map((_, index) => String(index + 1));
function pageChord(index: number): string { return `${MODIFIER.key}+${CHORD_DIGITS[index]}`; }
function pageTitle(label: string, index: number): string { return `${label} · ${MODIFIER.label}+${CHORD_DIGITS[index]}`; }
/** Switch pages the way the sidebar row does: the settings row hydrates the
 * page as well as switching to it, and a chord that skipped that would land on
 * an empty form.  The AI page is the same shape for the same reason — its
 * tool list and its two fields are a read of their own, and neither page's
 * state lives in the page's markup. */
/** What the skip link lands on: the page's own heading, which is already the
 *  thing a keyboard reader wants to start from, and which `openPage` moves the
 *  focus to when the sidebar switches pages. */
function focusMain() {
  pageHeadingEl.value?.focus({ preventScroll: false });
}

function openPage(page: (typeof PAGES)[number]) {
  if (page === "settings") { void openSettings(); return; }
  if (page === "ai") { void openAiPage(); return; }
  if (page === "companion") void refreshCompanion();
  if (page === "internet") void refreshInternetPairing();
  // Leaving the transfers page takes the row's chosen target with it: the
  // preselection belongs to the click that named the device, not to the page.
  if (page !== "transfers") transferPreselect.value = "";
  tab.value = page;
}
/** The row's 发送文件…: name the device, then let the page ask for the files.
 *
 * Kept off the row itself — it is a tenth icon on a line that had ten — and put
 * in the menu both rows already share, which is where a reader who has picked a
 * device out of a list looks for "do something with it". */
function sendFileTo(device: Device) {
  transferPreselect.value = device.id;
  openPage("transfers");
}
const historyListEl = ref<HTMLElement | null>(null);
/** What the header says about the page under it.  Both lines were one nested
 * ternary, which had to be read from the inside out to find a single page and
 * which the seventh page only made longer.  Each entry is a function because
 * the labels are translated and the counts are live: a computed that called
 * them once would freeze both.  The AI page's summary is a sentence rather than
 * a count because its inventory is a disk walk: zero files here would be a
 * count the page has not established. */
const PAGE_HEADINGS: Record<(typeof PAGES)[number], () => string> = {
  overview: () => t("概览"),
  history: () => t("剪贴板历史"),
  devices: () => t("设备"),
  favorites: () => t("收藏库"),
  transfers: () => t("文件传输"),
  chat: () => t("附近聊天"),
  ai: () => t("AI 配置"),
  settings: () => t("设置"),
  companion: () => t("手机伴侣"),
  internet: () => t("互联网同步（中继）"),
};
const PAGE_SUMMARIES: Record<(typeof PAGES)[number], () => string> = {
  // A sentence rather than a count: the overview is a hub, and the numbers it
  // once summarised are the pages it links to.
  overview: () => t("本机状态与常用操作"),
  history: () => t("{count} 条记录", { count: state.total }),
  // The rows this page is about, which is the list it draws under the heading:
  // the archived tail has its own heading and its own count to be read under,
  // and the toast a refresh raises names the same number this does — a header
  // saying 5 台设备 over three rows was the refresh and the line above it
  // disagreeing about what had just been read.
  devices: () => t("{count} 台局域网设备", { count: lanDevices.value.length }),
  // The library, not the page of it on screen and not the group being read:
  // `total` is what the current group and search matched, so a subtitle reading
  // it said 4 条收藏 the moment 工作 was picked — the header contradicting the
  // rail beside it.  The list's own count is the pager's job, and it names the
  // window it is counting.
  favorites: () => t("{count} 条收藏", { count: store.favorites.state.libraryTotal }),
  transfers: () => t("文件发送与接收"),
  chat: () => t("与附近设备进行会话"),
  ai: () => t("读取、编辑本机与已配对设备的 AI 工具配置"),
  settings: () => t("本地配置"),
  companion: () => t("手机访问与远程中继"),
  internet: () => t("跨网络配对与设备连接"),
};
const heading = computed(() => PAGE_HEADINGS[tab.value]());
const summary = computed(() => PAGE_SUMMARIES[tab.value]());

/** True when the keystroke came from somewhere the reader is typing.  Every
 * list shortcut stands down for these: Ctrl+A in the search box means "select
 * this text", not "select every record". */
function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.isContentEditable;
}
/** Keep the row the keyboard cursor sits on inside the scrolling list.  Moving
 * the cursor moves no focus, so nothing scrolls it into view on its own. */
function scrollKbdIntoView() {
  const row = document.querySelector(".history-row--kbd");
  // Guarded because a DOM without layout has no `scrollIntoView` at all --
  // the legacy panel's `_scrollKbdItem` carried the same check.
  if (row?.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}

/** The window's own chords: the history list's keys first, then the page
 * numbers, the search boxes and preferences.  The page numbers are the desktop
 * convention rather than a ported behaviour — the hotkey module is OS-wide and
 * excluded by the user's scope exception — but ↑/↓/Enter/Delete and Ctrl+A
 * *are* ported: the legacy panel drove its list entirely from the keyboard
 * (`app.js:669`, `app.js:701-724`) and the new page shipped without it, which
 * was a regression rather than a simplification.  Esc is deliberately absent
 * here: a native `<dialog>` already owns it, and it works even where this
 * would not. */
function onShortcut(event: KeyboardEvent) {
  // An open dialog owns the keyboard, so nothing behind it moves.
  if (document.querySelector("dialog[open]")) return;

  // Ctrl/Cmd+S saves the form in front of the reader.  A desktop app that shows
  // a save bar and then ignores the shortcut for it is a shortcut people press
  // twice and then stop trusting.  Only where there is a form to save: on the
  // history list it is left to the web view.
  if ((event.ctrlKey || event.metaKey) && !event.altKey && (event.key === "s" || event.key === "S")) {
    if (tab.value === "settings" && settingsLoaded.value) {
      event.preventDefault();
      if (settingsDirty.value && !settingsBusy.value && !passwordBlocked.value) void saveSettings();
      return;
    }
    // The AI page owns its form now (see `AiView`), so it answers whether the
    // chord was taken: `preventDefault` has to be decided here, before anything
    // is awaited, and only when a save is actually owed.
    if (tab.value === "ai" && aiView.value?.saveIfDirty()) {
      event.preventDefault();
      return;
    }
  }


  // Ctrl/Cmd+K searches both lists from wherever the reader is.  It sits above
  // everything that follows because it is the one chord that is about the whole
  // window rather than the page in front of it; the dialog guard at the top has
  // already said that an open dialog owns the keyboard, so this cannot open a
  // second palette on top of the first.
  if ((event.ctrlKey || event.metaKey) && !event.altKey && (event.key === "k" || event.key === "K")) {
    event.preventDefault();
    openQuickSearch();
    return;
  }

  // Esc lets a pinned card go before it does anything else: the card is the
  // thing the reader is looking at, and it is the reason they pressed Esc.
  // The page owns the card now, so it answers whether there was one to let go.
  if (event.key === "Escape" && historyView.value?.releasePinned()) {
    event.preventDefault();
    return;
  }

  // Esc empties the search box the reader is standing in.  The × beside it
  // does the same thing, and every other search field in the window already
  // clears itself this way — the settings one on its own input.
  if (event.key === "Escape" && historyView.value?.clearSearchIfFocused()) {
    event.preventDefault();
    return;
  }

  // Ctrl/Cmd+A takes the visible page.  History only: the favourites page has
  // no multi-select of its own for a selection to land in, so the chord would
  // mean nothing there and is left to the web view.
  if ((event.ctrlKey || event.metaKey) && !event.altKey && (event.key === "a" || event.key === "A")) {
    if (tab.value !== "history" || isTypingTarget(event.target)) return;
    event.preventDefault();
    store.selectAllVisible(true);
    return;
  }

  // ↑/↓ walk a cursor down the history list, Enter copies the row under it and
  // Delete removes it.  These are bare keys, so they have to be handled before
  // the modifier guard below.  The two action keys stand down while focus sits
  // on a control, or a focused row button would act and be acted on at once —
  // the legacy handler drew the same line (`app.js:703-704`).
  if (!event.ctrlKey && !event.metaKey && !event.altKey && tab.value === "history" && !isTypingTarget(event.target)) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      store.kbdStep(event.key === "ArrowDown" ? 1 : -1);
      void nextTick(scrollKbdIntoView);
      return;
    }
    // Space is the keyboard's double click: the same card, held open.
    if (event.key === " " && state.kbdIndex >= 0) {
      const onAControl = !!(event.target as HTMLElement | null)?.closest?.('button, a, select, [role="button"]');
      if (!onAControl && state.history[state.kbdIndex]) {
        event.preventDefault();
        historyView.value?.pinKbdRow();
        return;
      }
    }
    const actionKey = event.key === "Enter" || event.key === "Delete" || event.key === "Backspace";
    const onControl = !!(event.target as HTMLElement | null)?.closest?.('button, a, select, [role="button"]');
    const item = state.history[state.kbdIndex];
    if (actionKey && !onControl && state.kbdIndex >= 0 && item) {
      event.preventDefault();
      if (event.key === "Enter") void store.copy(item);
      // The row's own trash opens this same dialog, so Delete confirms too
      // rather than wiping a record on one stray keypress.
      else deleteItem.value = item;
      return;
    }
  }

  if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
  // Ctrl+F goes to the search box.  The web view has no find bar of its own, so
  // this is not taking the chord from anything.  Which box depends on the page:
  // the settings page has one of its own, and a reader standing on it means the
  // settings in front of them rather than the history they cannot see.
  if (event.key === "f" || event.key === "F") {
    event.preventDefault();
    // Which box a find reaches is the page being read: the settings page and
    // the library each have one of their own, and the chord is only the way to
    // the history list from a page that has none.
    if (tab.value === "settings") {
      void nextTick(() => {
        settingsSearchInput.value?.focus();
        // The word already in the box is the one a find bar offers to replace,
        // not to type after: coming back to a search and typing over it is the
        // same two words typed twice otherwise.
        settingsSearchInput.value?.select();
      });
      return;
    }
    if (tab.value === "favorites") {
      void nextTick(() => favoritesView.value?.focusSearch());
      return;
    }
    openPage("history");
    void nextTick(() => historyView.value?.focusSearch());
    return;
  }
  if (event.key === ",") { event.preventDefault(); openPage("settings"); return; }
  const index = CHORD_DIGITS.indexOf(event.key);
  if (index < 0) return;
  event.preventDefault();
  openPage(PAGES[index]);
}
/** How often the sidebar's unread count is read while the chat page is closed.
 *
 * The panel read it on its shared refresh, which was five seconds idle and
 * under a second while a transfer or the chat panel was live; the difference
 * was never the point — the count is a nudge, not a receipt, and anything
 * faster would be a poll nothing on screen changes for. */
const CHAT_UNREAD_POLL_MS = 5000;
const chatUnread = ref(0);
let chatUnreadTimer: ReturnType<typeof setInterval> | undefined;
/** How often the open internet card re-reads its peers' 在线 state.
 *
 * The sidecar's own online window is 90 s, so this only has to be quick enough
 * that a chip does not visibly lag the truth it is reporting; the read is local
 * and returns a handful of rows. */
const NETPAIR_STATUS_POLL_MS = 5000;
let netpairStatusTimer: ReturnType<typeof setInterval> | undefined;
/** The phone service's relay state is live while the page is open: the desktop
 * tunnel retries on its own, and the card used to keep the sentence it read
 * when the page was entered until the reader pressed Save. */
const COMPANION_STATUS_POLL_MS = 5000;
let companionStatusTimer: ReturnType<typeof setInterval> | undefined;
/** The unread total across every conversation, as the sidebar shows it.
 *
 * The reads here are the chat page's own (`bridge.chatSessions`), counted the
 * same way its session rows count them, so the badge and the list can never
 * disagree about how many messages are waiting. */
async function refreshChatUnread() {
  try {
    const page = await bridge.chatSessions();
    chatUnread.value = (page.sessions || [])
      .reduce((total, session) => total + (Number(session.unread) || 0), 0);
  } catch {
    // The chat page's own poll is where a failure is actionable, and a bad
    // count in a sidebar is worse than none: leave the last good one standing.
  }
}
/** What the two polls below owe the reader when the window comes back.
 *
 * Both stand down while it is hidden — the chat badge and the internet card's
 * online states are on screen nowhere else — so both are read the moment it is
 * visible again rather than at the end of the next interval.
 */
function onVisibility() {
  if (document.hidden) return;
  if (tab.value !== "chat") void refreshChatUnread();
  if (tab.value === "internet") void refreshInternetPairing();
  if (tab.value === "companion" || tab.value === "overview") void refreshCompanion();
}
onMounted(() => {
  store.start();
  void refreshChatUnread();
  // Every timer below skips its work while the window is not on screen, and
  // skipping has to be paid for on the way back: a reader returning to a
  // minimized window should not wait out an interval to see the count or the
  // online states that changed while it was away.  A visible window is the
  // only case either read is for.
  document.addEventListener("visibilitychange", onVisibility);
  // The sidebar counts the unread chat for the same reason the panel put a
  // count on its own 附近聊天 row: the cue has to reach a reader who is on
  // another page, where every other sign of a message — the bubble, the
  // session's own count — is behind a tab they have to open first.
  //
  // It stands down while the chat page is open, where the conversation list
  // shows the same numbers a second time and polls them faster.  What that
  // page cannot see is its own exit, so leaving it takes one reading on the
  // way out — the counts it cleared are the ones the badge would otherwise
  // still be showing.
  chatUnreadTimer = setInterval(() => {
    if (!document.hidden && tab.value !== "chat") void refreshChatUnread();
  }, CHAT_UNREAD_POLL_MS);
  // The internet card's per-device 在线/离线 is a reading rather than an event,
  // and the card treated it as one.  ``paired_peers`` decides ``online`` when
  // it is read, from a 90-second window over the last frame heard from each
  // peer, and the only things that re-read it were the card's own 刷新 button
  // and the ``netpair.peer.changed`` event — which fires when a pairing is made
  // or broken, and never when the window lapses or when a peer that had gone
  // quiet speaks again.  Both directions were therefore stuck for as long as
  // the page stayed open: a peer that came back stayed 离线, and one that went
  // away stayed 在线.  The expiry has no event to ride at all, since silence is
  // what it is made of, so it is a poll — on the tab that draws the answer, and
  // nowhere else.  It re-reads the ledgers with the peers, which is what keeps
  // the 待补发 chips on the same rows in step.
  netpairStatusTimer = setInterval(() => {
    if (!document.hidden && tab.value === "internet") {
      void refreshInternetPairing();
    }
  }, NETPAIR_STATUS_POLL_MS);
  companionStatusTimer = setInterval(() => {
    if (document.hidden) return;
    if (tab.value !== "companion" && tab.value !== "overview") return;
    if (tab.value === "companion" && companionFormFocused()) return;
    void refreshCompanion();
  }, COMPANION_STATUS_POLL_MS);
  watch(tab, (page, previous) => {
    if (previous === "chat" && page !== "chat") void refreshChatUnread();
  });
  // The dashboard ticked its pause countdown every 15 s; the interval is the
  // same here because the label is whole minutes.  Only the deadline is refetched
  // — merging the whole settings payload would overwrite a half-edited form.
  pauseTickTimer = setInterval(() => {
    nowTick.value = Date.now();
    const until = Number(pauseUntil.value || 0) * 1000;
    if (until <= 0 || nowTick.value < until || pauseExpiredSyncing) return;
    pauseExpiredSyncing = true;
    void bridge.settings()
      .then((loaded) => {
        pauseUntil.value = Number(loaded.settings.timed_pause_until || 0);
      })
      .catch(() => { /* The next tick asks again. */ })
      .finally(() => { pauseExpiredSyncing = false; });
  }, 15000);
  // The native tray's entries that open one of this window's own surfaces ask
  // for it here: the tray has no dialogs of its own, and these are the same
  // buttons — the same handlers, not a second implementation of each.
  void bridge.onMenuAction((action) => {
    switch (action) {
      case "about": void openAbout(); break;
      case "qr": void openCompanionQr(); break;
      case "send-url": openSendUrlFromHost(); break;
      case "settings": void openSettings(); break;
      case "export-logs": void openLogs(); break;
      case "check-update": void checkForUpdate(); break;
    }
  })
    .then((off) => { offMenuAction = off; })
    .catch((error) => { store.setError(error); });
  // A file dropped on the window is the transfers page's own 发送文件 intent
  // with the picker already answered, so the paths are handed to that page and
  // it is opened — the device is still named there, because a send is the one
  // thing in this window the user cannot take back.
  //
  // **Unless the chat page is the page on screen.**  A drop onto a conversation
  // is an attachment to that conversation, and the chat page can send one
  // (`bridge.sendChatFile`, the same call its paperclip makes).  Moving the
  // reader to the transfers page for it was the reported bug: the file was
  // staged for a device picker while the conversation they were having
  // disappeared.  So the chat page is handed the paths and decides: it sends
  // them, or emits `fallback` when there is no live conversation, and only then
  // is the transfers page opened.  Nothing is dropped on the floor either way.
  //
  // Four events, one gesture: `enter` and `over` are the files being held over
  // the window and raise the hint, `leave` is the gesture ending somewhere else,
  // and `drop` is it ending on the window — both endings take the hint down, and
  // only the one that carried paths has anything to send.  The OS's `leave`
  // never carries any, and a `drop` with none (some platforms report one) is not
  // a drop: opening a page and asking which device to send nothing to would be a
  // question with no answer.
  void bridge.onFileDrop((event) => {
    dropActive.value = event.type === "enter" || event.type === "over";
    const paths = event.type === "drop" ? (event.paths || []).filter((path) => !!path) : [];
    if (!paths.length) return;
    droppedPaths.value = paths;
    // The chat page takes them from `droppedPaths` itself; every other page
    // still gets the transfers page it always did.
    if (tab.value === "chat") return;
    openPage("transfers");
  })
    .then((off) => { offFileDrop = off; })
    .catch(() => { /* A window that cannot be a drop target is not an error. */ });
  // A global hotkey the host recognised.  `show_window` is not among them: the
  // host raises the window itself, so anything that arrives here is something
  // this page has to do.
  void bridge.onHotkey((action) => { void runHotkey(action); })
    .then((off) => { offHotkey = off; })
    .catch(() => { /* A host without hotkeys is not an error. */ });
  window.addEventListener("keydown", onShortcut);
});
onUnmounted(() => {
  if (pauseTickTimer) clearInterval(pauseTickTimer);
  if (chatUnreadTimer) clearInterval(chatUnreadTimer);
  if (netpairStatusTimer) clearInterval(netpairStatusTimer);
  if (companionStatusTimer) clearInterval(companionStatusTimer);
  document.removeEventListener("visibilitychange", onVisibility);
  window.removeEventListener("keydown", onShortcut);
  // The AI page's own three counters went with it (`AiView` cancels its own
  // in-flight reads when it unmounts); these four belong to the window.
  ++themeGeneration;
  ++companionGeneration;
  ++translationGeneration;
  offMenuAction?.();
  offFileDrop?.();
  offHotkey?.();
  store.dispose();
});
/** Everything the save button will write, as one string.  The password is in it
 * as a yes/no rather than as its value — the field is cleared the moment it is
 * stored, so "a password is typed" is the part that can be unsaved, and the
 * secret itself has no business in a comparable snapshot.  The translation key
 * is not here because it has its own button and its own request.
 *
 * Nor are the AI tools, which this form no longer carries: they are the AI
 * page's form, with the AI page's button under them.  A field in a snapshot
 * that a page does not draw is a Save bar that goes on saying there is
 * something pending without saying where — the same defect the per-card marks
 * exist to answer, one page over. */
function settingsFormSnapshot(): string {
  // The three view settings are stored the moment they are picked (see
  // `previewAppearance`, `applyLanguage`, `previewMotion`), so they are dropped
  // from the snapshot: a Save bar offering to save them would be offering to
  // save something already saved.  That is the whole of the rule the window
  // follows — a setting the reader can see the effect of applies at once, a
  // setting that changes what the app does waits for the bar, and a setting
  // that destroys something asks first.  Dropped here rather than compared
  // field by field so every other field keeps comparing whole, and a value
  // typed and typed back stays "saved state" for the same reason it did before.
  // `language` used to be left in, which is how changing the language put a
  // Save bar over a form whose only edit was already written — see U183.
  const { appearance_mode: _theme, ui_animation_enabled: _motion, language: _lang, ...rest } = settings.value;
  return JSON.stringify([
    rest,
    securityPassword.value.length > 0,
  ]);
}
/** Whether the card actually holds the saved state.  Comparing the whole form
 * rather than setting a flag per field means a value typed and typed back is
 * saved state again, which is what the user sees.  It can read dirty when the
 * sidecar normalized what it stored — a port below its floor comes back as the
 * floor while the box still shows what was typed — and that is the truthful
 * answer: what is stored is not what is on screen. */
const settingsDirty = computed(() =>
  savedSettings.value !== null && settingsFormSnapshot() !== savedSettings.value);

/** The fields that cannot be saved as typed, and why.
 *
 * Every one of these used to be clamped on the way out: a port below 1024 came
 * back as 1024, an over-long number as the ceiling, and the page then went on
 * showing what the reader had typed beside a bar saying everything was saved —
 * so the number on screen and the number in force were two different numbers and
 * nothing said so.  Clamping silently is the wrong side of that trade: a value
 * the sidecar will not take is a value the reader has to change, and the place
 * to say so is under the box.
 *
 * Three fields to start with — the port, the data directory and the two broker
 * lists — because they are the ones typed by hand and the ones whose failure
 * costs a restart.  The numeric relay fields keep their clamp: those are tuned
 * by nudging, the hint beside them already names the range, and refusing a save
 * over a number being one keystroke out of range was worse than the correction.
 * See U51 and U52.
 */
const settingsErrors = computed<Record<string, string>>(() => {
  if (!settingsLoaded.value) return {};
  const errors: Record<string, string> = {};
  const rawPort = settings.value.port;
  // A payload that does not carry the field at all is not a field the reader
  // typed into: nothing to refuse, and nothing to clamp either — the save sends
  // the same value back and the sidecar keeps its own.  A box that was cleared
  // is different, and is exactly what this is for.
  if (rawPort !== undefined && rawPort !== null) {
    const port = Number(rawPort);
    if (rawPort === "" || !Number.isInteger(port) || port < 1024 || port > 65535) {
      errors.port = t("TCP 端口要填 1024–65535 之间的整数；1024 以下留给系统。");
    }
  }
  const dir = String(settings.value.data_dir ?? "").trim();
  // A relative path is accepted by the sidecar and then resolved against
  // whatever the working directory happens to be, which is not a place anyone
  // means to keep their history.  Blank is fine: it means the default.
  if (dir && !/^([a-zA-Z]:[\\/]|\\\\|\/|~)/.test(dir)) {
    errors.data_dir = t("数据目录要填绝对路径（例如 D:/ClipSync）；留空则用默认位置。");
  }
  for (const [key, label] of [["relay_brokers", t("公共中继地址")], ["relay_private_brokers", t("私有中继地址")]] as const) {
    // Whitespace inside one entry is the common paste: a whole sentence, or two
    // addresses separated by a space, on a field that reads one per line.  An
    // entry with no ":" at all is a name without a port, which no broker
    // answers to.
    const bad = brokerList(settings.value[key]).filter(entry => /\s/.test(entry) || !entry.includes(":"));
    if (bad.length) errors[key] = t("{field}里有 {count} 个地址不成立：{list}。每行一个 host:port，或用逗号分隔。", { field: label, count: bad.length, list: bad.join("、") });
  }
  // The two tuned numbers keep their clamp — a box one keystroke out of range is
  // narrowed to the edge and the box is put back to the stored value, which the
  // reader sees — but a *cleared* one is not the same thing: it fell back to the
  // previous value with nothing said, so the reader who deleted the number to
  // retype it and saved by accident got the old one back and no word about it.
  // See U56.
  for (const [key, label, min, max] of [
    ["relay_max_message_kb", t("中继单条消息上限"), MIN_RELAY_MAX_MESSAGE_KB, MAX_RELAY_MAX_MESSAGE_KB],
    ["relay_max_rate_kbs", t("中继发送速率"), MIN_RELAY_RATE_KB, MAX_RELAY_RATE_KB],
  ] as const) {
    const raw = settings.value[key];
    if (raw === "" || raw === null || raw === undefined) {
      errors[key] = t("{field}不能留空：可填 {min}–{max}，留空会被当成没填、存下原来的值。", { field: label, min, max });
    }
  }
  return errors;
});
const settingsBlocked = computed(() => Object.keys(settingsErrors.value).length > 0);
/** Fill the data directory from the folder picker instead of by typing.
 *
 * The field takes an absolute path and a typo in one only shows up when the
 * next start cannot find its history, so the picker is offered beside it.  It
 * replaces what is in the box rather than appending: there is exactly one data
 * directory, and the box holds one path. */
async function browseDataDir() {
  try {
    const picked = await bridge.chooseFolder();
    if (!picked) return;
    settings.value.data_dir = picked;
  } catch (error) { store.setError(error); }
}
/** The folder received files are filed under, filled from the picker.
 *
 * Empty means the sidecar's default (the Downloads folder), which is why the
 * button is beside a box rather than instead of one — clearing the box is how
 * the reader goes back to the default. */
async function browseReceiveDir() {
  try {
    const picked = await bridge.chooseFolder();
    if (!picked) return;
    settings.value.file_receive_dir = picked;
  } catch (error) { store.setError(error); }
}

/** Entries in the process-name list that cannot match anything.
 *
 * The window cannot list the processes this machine is running — that is a new
 * host call, not a form check — so it cannot say "no such process".  What it
 * can say is that an entry is the wrong kind of thing: the filter matches a
 * process name against each pattern exactly, so a path with directories in it
 * matches no process and the rule silently does nothing, which is this field's
 * only failure mode. */
const appFilterProblems = computed(() => {
  const list = String(settings.value.app_filter_list ?? "");
  return list.split(/\r?\n/).map((entry) => entry.trim()).filter(Boolean)
    .filter((entry) => /[\\/]/.test(entry));
});
/** Where the save bar points when it is held: the first field in the way. */
function focusFirstFieldError() {
  const first = Object.keys(settingsErrors.value)[0];
  if (!first) return;
  const node = document.querySelector<HTMLElement>(`[data-settings-field="${first}"]`);
  node?.scrollIntoView({ block: "center" });
  node?.focus({ preventScroll: true });
}

/** The cards holding an edit the sidecar has not been told about yet.
 *
 * The page shows one card, so an edit made on one card is out of sight the
 * moment the reader walks to another, and the Save bar goes on saying there is
 * something pending without saying where.  What marks a card is the edit itself:
 * the control that was typed in sits inside the card's own `<section>`, whose
 * id is already the card's name (`settings-<id>`, the page's anchor), so no
 * second inventory of which setting belongs to which card has to be kept in
 * step with the template.
 *
 * The marks are read through `settingsDirty`, so a value typed back to what was
 * saved marks nothing; a card edited and then reverted while another card is
 * edited keeps its dot, which is the one case this reads wrong, and it reads
 * wrong by saying too much rather than too little.
 */
const settingsEditedCards = ref<Record<string, true>>({});
/** The form as the last change left it.
 *
 * Not every control inside a card is a form field: the translation card's API
 * key, its two language pickers and its text box are all typed in or chosen
 * inside a card and change nothing this save writes — the key has its own
 * button and its own request, and the pickers are the card's own state.  What
 * the marks are about is a change to the form, so an event that left the form
 * where it was marks no card.  Watched rather than read at load and at save,
 * because the form also changes without an event ~ the pause countdown is
 * re-read on a timer. */
let settingsEditSnapshot = settingsFormSnapshot();
watch(settingsFormSnapshot, (snapshot) => { settingsEditSnapshot = snapshot; });
/** Tie each field to the sentence that explains it.
 *
 * The notes under the boxes carry what decides whether a value is right — the
 * range, the unit, the 64 KB ceiling — and none of it reached a screen reader:
 * `title` and a sibling `<span>` are not a description.  This walks the form
 * once after it is filled and joins the two by id, which is cheaper than naming
 * an id in a hundred templates and cannot go stale when a field moves.
 */
let settingNoteSeq = 0;
function wireSettingNotes(root: HTMLElement) {
  for (const control of Array.from(root.querySelectorAll<HTMLElement>(".setting-control"))) {
    const field = control.querySelector<HTMLElement>("input, select, textarea");
    const note = control.querySelector<HTMLElement>(".note");
    if (!field || !note || field.hasAttribute("aria-describedby")) continue;
    if (!note.id) note.id = `setting-note-${++settingNoteSeq}`;
    field.setAttribute("aria-describedby", note.id);
  }
}

/** A wheel over a number field changes it, and the settings form scrolls.
 *
 * The two together meant a scroll through 网络与高级 could rewrite the port and
 * the timeouts on its way past, with the page then claiming unsaved changes the
 * reader never made.  A field that has focus is a field being edited, and there
 * the wheel keeps its native meaning; everywhere else it scrolls.
 */
function guardNumberWheel(event: WheelEvent) {
  const input = event.target as HTMLElement | null;
  if (!input || input.tagName !== "INPUT" || (input as HTMLInputElement).type !== "number") return;
  if (document.activeElement === input) return;
  event.preventDefault();
  const scroller = contentEl.value;
  if (scroller) scroller.scrollTop += event.deltaY;
}

/** Note the card a change happened in.  Nothing is marked for an event that came
 * from outside a card or that changed nothing, so a control added outside one
 * marks nothing rather than marking the wrong thing. */
function markSettingsEdited(event: Event) {
  const snapshot = settingsFormSnapshot();
  const changed = snapshot !== settingsEditSnapshot;
  settingsEditSnapshot = snapshot;
  if (!changed) return;
  const card = (event.target as Element | null)?.closest?.("section.settings-section")?.id;
  if (card?.startsWith("settings-")) settingsEditedCards.value[card.slice("settings-".length)] = true;
  // A change after a refused save is a new attempt, so the bar stops reporting
  // the old one the moment the reader starts typing again.
  settingsSaveFailed.value = false;
}
/** Whether the rail marks this card as holding an edit that is not saved. */
function cardHasEdits(id: string): boolean {
  return settingsDirty.value && !!settingsEditedCards.value[id];
}
/** Follow a field another surface just saved, without calling it a pending edit.
 *
 * The internet switch on the devices page, the translation key's own button and
 * the password's own clear each save through a call of their own, and the form
 * has to follow or a later visit to the settings page would show the old value
 * and save it back.  But what they changed is already stored: written in
 * plainly, it also moves the snapshot the save bar compares, and the bar would
 * offer to save what has just been saved — with no card to mark, because the
 * control that did it is not on this page.
 *
 * So the baseline moves with the write, and only when the form was clean: an
 * edit the reader is still holding stays theirs, and is still pending. */
function foldSavedField(key: string, value: unknown) {
  const wasClean = !settingsDirty.value;
  settings.value[key] = value;
  if (wasClean) savedSettings.value = settingsFormSnapshot();
}

/** The AI page, shown rather than mounted.
 *
 * `aiVisited` is what makes the first visit build it; after that it stays in
 * the tree and the window only hides it, so an unsaved edit in its editor
 * survives a look at another page -- which is what the reader had when the page
 * was a template block.  What a visit reads is `AiView`'s own business now
 * (`enter()`); the window only navigates.
 */
const aiVisited = ref(false);
const aiView = ref<InstanceType<typeof AiView> | null>(null);
function openAiPage() {
  tab.value = "ai";
  aiVisited.value = true;
  // `nextTick`, because on the first visit the page is not in the tree yet.
  void nextTick(() => aiView.value?.enter());
}
/** The settings form as the sidecar's payload spells it.
 *
 * One shape for both ways the page is filled — the whole-form load and the
 * field-by-field fold-in below — because a second transform for the partial
 * path is how one field ends up spelled two ways: a broker list as lines here
 * and as a list there, a byte limit as kilobytes in the box and as bytes on
 * disk, an absent boolean as a default in one place and as `undefined` in the
 * other. */
function buildSettingsForm(loaded: Record<string, any>): Record<string, any> {
  const form: Record<string, any> = { ...loaded };
  // The timed pause's deadline rides in the settings payload but is not a
  // setting: it is armed from the overview and cleared by the runtime, and this
  // page has no box for it.  Left in, it would be part of the snapshot the save
  // bar compares — so a pause armed elsewhere would read as a pending change to
  // a form nobody edited, and one this page could not put right by saving.
  delete form.timed_pause_until;
  return {
    ...form,
    app_filter_enabled: loaded.app_filter_enabled ?? false,
    app_filter_mode: loaded.app_filter_mode ?? "blacklist",
    app_filter_list: Array.isArray(loaded.app_filter_list) ? loaded.app_filter_list.join("\n") : "",
    // Redaction ships off, so a machine that has never chosen categories
    // (`null`) draws every box unchecked -- which is also what saving that
    // form writes, and the two have to agree or the first save silently turns
    // categories on that the reader never picked.
    filter_enabled_categories: loaded.filter_enabled_categories ?? [],
    relay_brokers: Array.isArray(loaded.relay_brokers) ? loaded.relay_brokers.join("\n") : (loaded.relay_brokers || ""),
    relay_private_brokers: Array.isArray(loaded.relay_private_brokers) ? loaded.relay_private_brokers.join("\n") : (loaded.relay_private_brokers || ""),
    // The broker's limit is stored in bytes and typed in kilobytes, so the
    // form gets a key of its own rather than a value whose unit depends on
    // which side of the save it is on.  Bound as `relay_max_message_kb` and
    // multiplied back on save; see the relay card's own row.
    relay_max_message_kb: clampNumber(
      Math.round(Number(loaded.relay_max_message_bytes ?? DEFAULT_RELAY_MAX_MESSAGE_BYTES) / 1024),
      DEFAULT_RELAY_MAX_MESSAGE_BYTES / 1024,
      MIN_RELAY_MAX_MESSAGE_KB,
      MAX_RELAY_MAX_MESSAGE_KB,
    ),
    relay_max_rate_kbs: clampNumber(
      Math.round(Number(loaded.relay_max_bytes_per_second ?? DEFAULT_RELAY_MAX_BYTES_PER_SECOND) / 1024),
      DEFAULT_RELAY_MAX_BYTES_PER_SECOND / 1024,
      MIN_RELAY_RATE_KB,
      MAX_RELAY_RATE_KB,
    ),
    // The bindings are edited one row at a time, so the map has to exist as an
    // object even on a config that never stored one — a `v-model` into
    // `undefined` writes nothing and the row looks editable while it is not.
    hotkeys_enabled: !!loaded.hotkeys_enabled,
    hotkeys: (loaded.hotkeys && typeof loaded.hotkeys === "object" && !Array.isArray(loaded.hotkeys))
      ? { ...(loaded.hotkeys as Record<string, string>) }
      : {},
    // The paste switch and the four recording rules (1.3.0).  Each boolean
    // ships on, so a config that predates the fields draws the boxes ticked and
    // saving it back writes what the sidecar itself would do — an absent key
    // read as `false` here would silently turn the rules off for every reader
    // who never visited this card.  The list is typed one pattern per line and
    // stored as a list, the same shape 来源应用过滤 already uses.
    auto_paste_enabled: loaded.auto_paste_enabled ?? true,
    skip_password_manager_clips: loaded.skip_password_manager_clips ?? true,
    skip_prelaunch_clipboard: loaded.skip_prelaunch_clipboard ?? true,
    max_non_file_paste_mb: clampNumber(
      loaded.max_non_file_paste_mb ?? DEFAULT_MAX_NON_FILE_PASTE_MB,
      DEFAULT_MAX_NON_FILE_PASTE_MB,
      MIN_MAX_NON_FILE_PASTE_MB,
      MAX_MAX_NON_FILE_PASTE_MB,
    ),
    source_exclusion_patterns: Array.isArray(loaded.source_exclusion_patterns)
      ? loaded.source_exclusion_patterns.join("\n")
      : (loaded.source_exclusion_patterns || ""),
    // The file-transfer limits (1.3.1).  Absent from an older config, which
    // reads as the shipped defaults — the same values the sidecar itself uses.
    max_file_mb: clampNumber(loaded.max_file_mb ?? DEFAULT_MAX_FILE_MB, DEFAULT_MAX_FILE_MB, 0, MAX_FILE_MB),
    confirm_large_file_mb: clampNumber(
      loaded.confirm_large_file_mb ?? DEFAULT_CONFIRM_LARGE_FILE_MB,
      DEFAULT_CONFIRM_LARGE_FILE_MB, 0, MAX_FILE_MB,
    ),
    relay_paste_max_mb: clampNumber(
      loaded.relay_paste_max_mb ?? DEFAULT_RELAY_PASTE_MAX_MB,
      DEFAULT_RELAY_PASTE_MAX_MB, 0, MAX_FILE_MB,
    ),
    // Stored as a path string, empty = the default Downloads folder.
    file_receive_dir: String(loaded.file_receive_dir || ""),
  };
}
/** Config names the form spells differently.
 *
 * The sidecar reports what changed under the name the config stores it by, and
 * the fields whose form key is not that name are the relay's two limits — both
 * typed in kilobytes, both stored in bytes. */
/** Every global hotkey the sidecar's config knows about, in the order the
 * settings card lists them.
 *
 * The ids and their defaults are `DEFAULT_HOTKEYS`' on the sidecar side, which
 * is where the config, the backup and the first-run values already agree about
 * them — this list only supplies the words.  `paste_1..9` is generated rather
 * than written out nine times, so a change to the count cannot leave a stale
 * row behind. */
const HOTKEY_ACTIONS: ReadonlyArray<{ id: string; label: () => string }> = [
  { id: "show_window", label: () => t("呼出窗口") },
  // Both window actions are the host's own: `show_window` raises this window
  // and `show_quick_paste` opens the overlay, neither of which can depend on
  // the webview being awake.  They are listed here because the card is the one
  // place a binding is read and edited.
  { id: "show_quick_paste", label: () => t("快速粘贴窗口") },
  { id: "toggle_monitor", label: () => t("暂停或恢复同步") },
  { id: "paste_plain", label: () => t("复制最近一条（纯文本）") },
  ...Array.from({ length: 9 }, (_, index) => ({
    id: `paste_${index + 1}`,
    label: () => t("复制第 {n} 条", { n: index + 1 }),
  })),
  // Ships unbound: which clip the "newest remote" one is depends on what has
  // arrived, so a default chord would be a key pressed for a row that may not
  // exist yet.  The row is listed so it can be bound by whoever wants it.
  { id: "paste_remote_last", label: () => t("粘贴最新远程项") },
];
/** The rows the card draws, relabelled when the language changes. */
const hotkeyRows = computed(() =>
  HOTKEY_ACTIONS.map((row) => ({ id: row.id, label: row.label() })));
/** Actions the OS would not take, by id.
 *
 * The host answers with these instead of failing the whole call: another
 * application owning `Ctrl+Shift+V` is ordinary, and what the reader needs is
 * which rows to retype — not that "hotkeys failed". */
const hotkeyRefused = ref<string[]>([]);
/** The row whose field is waiting for a chord, or "" for nobody.
 *
 * Typing `Ctrl+Shift+V` into a text box is the part of this card that fails
 * quietly: the host only finds out on save, and a binding it refuses reads as a
 * typo the reader cannot see.  Recording the chord is one keypress and cannot
 * misspell it; the box stays editable, so a chord can still be pasted. */
const hotkeyRecording = ref("");
const hotkeyNotice = ref("");
function startHotkeyRecording(id: string) {
  hotkeyRecording.value = id;
  hotkeyNotice.value = "";
  void nextTick(() => {
    document.querySelector<HTMLInputElement>(`[data-hotkey-row="${id}"]`)?.focus();
  });
}
function stopHotkeyRecording() {
  hotkeyRecording.value = "";
}
/** The name of the key itself, in the spelling the registration parses.
 *
 * `global-hotkey` takes `keyboard-types` codes — `KeyV`, `Digit1`, `F5`,
 * `Space` — so the code is what is read, not the character: the character of
 * Shift+1 is `!`, and `!` is not a key. */
function hotkeyKeyName(event: KeyboardEvent): string {
  const code = event.code || "";
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code;
  if (/^(Space|Enter|Tab|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Insert|Backquote|Minus|Equal|Comma|Period|Slash|Semicolon|Quote|BracketLeft|BracketRight|Backslash)$/.test(code)) return code;
  return "";
}
/** The chord a keypress spells.  Empty when it spells nothing usable: a key the
 *  registration has no name for, or no modifier at all — an OS-wide `V` would
 *  take that letter away from every application on the machine. */
function hotkeyChord(event: KeyboardEvent): string {
  const key = hotkeyKeyName(event);
  if (!key) return "";
  const parts: string[] = [];
  // Cmd on macOS, Win/Super elsewhere: the label is what the user pressed, the
  // word is what the host registers.
  if (event.metaKey) parts.push(MODIFIER.label === "Cmd" ? "Cmd" : "Super");
  if (event.ctrlKey) parts.push("Ctrl");
  if (event.altKey) parts.push("Alt");
  if (event.shiftKey) parts.push("Shift");
  if (!parts.length) return "";
  return [...parts, key].join("+");
}
function captureHotkey(event: KeyboardEvent, id: string) {
  if (hotkeyRecording.value !== id) return;
  // The window listens for Ctrl+1 … Ctrl+8 and Ctrl+S one level up; a chord
  // being recorded belongs to the OS, so it stops here.
  event.stopPropagation();
  if (event.key === "Escape") {
    event.preventDefault();
    stopHotkeyRecording();
    return;
  }
  // A modifier on its own is half a chord: waiting for the real key is the only
  // way Shift+… can be typed at all.
  if (["Shift", "Control", "Alt", "Meta", "CapsLock", "OS", "AltGraph"].includes(event.key)) return;
  event.preventDefault();
  const chord = hotkeyChord(event);
  if (!chord) {
    hotkeyNotice.value = t("这个键不能单独设成全局快捷键：至少要带一个修饰键（Ctrl、Alt、Shift 或 Cmd），键本身要是字母、数字、F1–F24 或方向键一类。");
    return;
  }
  settings.value.hotkeys = { ...(settings.value.hotkeys || {}), [id]: chord };
  hotkeyNotice.value = "";
  stopHotkeyRecording();
}
/** The bindings worth handing to the OS, out of whatever the form holds.
 *
 * A row the user emptied is absent rather than empty: the host unregisters
 * everything before each call, so leaving it out is how a binding is cleared. */
function hotkeyBindings(raw: unknown): Record<string, string> {
  const bindings: Record<string, string> = {};
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [action, binding] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof binding === "string" && binding.trim()) bindings[action] = binding.trim();
    }
  }
  return bindings;
}
/** Hand a set of bindings to the host, and remember what it refused. */
async function registerHotkeys(enabled: boolean, raw: unknown) {
  if (!inDesktop()) return;
  try {
    hotkeyRefused.value = await bridge.setHotkeys(enabled, hotkeyBindings(raw));
  } catch (error) {
    // A host that cannot register any of them is a warning, not a broken page:
    // everything else on it still works.
    hotkeyRefused.value = [];
    store.setError(error);
  }
}
/** Hand the form's bindings to the OS.
 *
 * Runs once the settings are known and again after every save. */
async function applyHotkeys() {
  await registerHotkeys(!!settings.value.hotkeys_enabled, settings.value.hotkeys);
}
/** The same, from what is on disk, without waiting for the page to be opened.
 *
 * The settings page is read lazily, and a shortcut that only starts working
 * after the reader has visited Settings → General once per launch is a
 * shortcut that does not work.  Registering them is the only thing this needs
 * the saved values for, so it reads them itself rather than filling a form
 * nobody is looking at.  The 自动粘贴 switch rides on the same read: it is what
 * the paste shortcuts consult before handing the OS the chord. */
async function applySavedHotkeys() {
  if (!inDesktop()) return;
  try {
    const saved = (await bridge.settings()).settings;
    savedAutoPasteEnabled.value = saved.auto_paste_enabled !== false;
    await registerHotkeys(!!saved.hotkeys_enabled, saved.hotkeys);
  } catch {
    // Silence, not an error band: a sidecar that is still starting cannot
    // answer yet, `store.start()` is the one that reports an unreachable one,
    // and the settings page's own read is where a broken read belongs.
  }
}
/** Read the stored 自动粘贴 switch on its own.
 *
 * Only the hotkey path is missing the value when it has none: the startup read
 * above is the one that normally fills it, but it runs against a sidecar that
 * may not be up yet, and a shortcut pressed in that window must not silently
 * stop pasting.  A failed read leaves the value unknown rather than false —
 * "copied, press it yourself" is the safe answer, but believing the switch is
 * off is not something this window can claim. */
async function refreshSavedAutoPaste() {
  if (!inDesktop()) return;
  try {
    const saved = (await bridge.settings()).settings;
    savedAutoPasteEnabled.value = saved.auto_paste_enabled !== false;
  } catch { /* the value stays unknown; the next press or page visit asks again */ }
}
/** Whether a paste shortcut should hand the OS the chord after copying. */
async function autoPasteNow(): Promise<boolean> {
  if (savedAutoPasteEnabled.value === null) await refreshSavedAutoPaste();
  return savedAutoPasteEnabled.value === true;
}
// Registered when the sidecar can answer, not at mount: on a cold start it is
// still coming up, and after an update relaunch it is the same again.  The
// wait is also `ready`'s other job — a sidecar that comes back gets its
// bindings handed over once more, and doing it twice costs one unregister.
watch(ready, (isReady) => {
  if (isReady) void applySavedHotkeys();
}, { immediate: true });
/** What a recognised hotkey does once the host has fired it.
 *
 * `show_window` and `show_quick_paste` never reach here — both are the host's
 * own windows, raised or created without asking the webview anything — and
 * every action that does arrive is one this window has to answer. */
async function runHotkey(action: string) {
  if (action === "toggle_monitor") {
    const running = state.status?.sync_state === "running";
    try {
      await bridge.setSyncEnabled(!running);
      await store.refresh();
      store.toast("ui.sync", running ? t("同步已暂停") : t("同步已恢复"));
    } catch (error) { store.setError(error); }
    return;
  }
  // The newest clip that arrived from another device.  Its own action rather
  // than `paste_1`: the first row is whatever was copied here last, and the
  // reader reaching for this one is continuing something they were sent.
  if (action === "paste_remote_last") {
    try {
      const page = await bridge.history("", 0, 50, "all", "newest");
      const item = page.items.find((row) => String(row.source_device || "").trim());
      if (!item) {
        store.toast("ui.hotkey", t("还没有可粘贴的远程内容"));
        return;
      }
      // The newest remote item can itself be a file that lives on the peer: the
      // same fetch-then-paste path as the numbered keys takes it, because there
      // is nothing here to copy yet.
      if (isRemoteFileRow(item)) { await pasteRemoteFileFromHotkey(item); return; }
      await bridge.copyHistory(item.id, false);
      if (await autoPasteNow()) {
        try {
          await bridge.injectPaste();
          store.toast("ui.hotkey", t("已粘贴最新远程项"));
        } catch {
          store.toast("ui.hotkey", t("已复制，请按 {shortcut} 手动粘贴", { shortcut: PASTE_SHORTCUT }));
        }
        return;
      }
      store.toast("ui.hotkey", t("已复制最新远程项"));
    } catch (error) { store.setError(error); }
    return;
  }
  const numbered = /^paste_(\d)$/.exec(action);
  const plain = action === "paste_plain";
  if (!numbered && !plain) return;
  // One-based in the binding (`Ctrl+1` is the first row), and read from the
  // same unfiltered list the history page shows, so "the third one" means the
  // same clip in both places.
  const index = plain ? 1 : Number(numbered![1]);
  const copied = plain ? t("已复制最近一条（纯文本）") : t("已复制第 {n} 条", { n: index });
  const pasted = plain ? t("已粘贴最近一条（纯文本）") : t("已粘贴第 {n} 条", { n: index });
  try {
    const page = await bridge.history("", 0, index, "all", "newest");
    const item = page.items[index - 1];
    if (!item) {
      // A shortcut for a row the history does not have — Ctrl+9 over three
      // clips, or any of them over an empty one.  It answered with nothing at
      // all, which is the one reply a reader cannot tell from a dead key: the
      // press is theirs, and the window's whole answer was silence.
      store.toast("ui.hotkey", plain
        ? t("还没有可复制的记录")
        : t("还没有第 {n} 条记录", { n: index }));
      return;
    }
    // A file that still lives on the peer has no local bytes to copy; the key
    // fetches it and pastes it once it is here.  `paste_plain` is exempt: it
    // asks for a text form, and a file row has none — the copy below answers
    // NOT_SUPPORTED, which is the honest answer for that key.
    if (!plain && isRemoteFileRow(item)) { await pasteRemoteFileFromHotkey(item); return; }
    await bridge.copyHistory(item.id, plain);
    // Copying is half of what the key is for now: with the switch on, the host
    // types the system chord into whatever application had the focus.  A failed
    // injection is still a copy — the clipboard holds the row — so it is said
    // beside the key press with the chord to press, not as the failure band.
    if (await autoPasteNow()) {
      try {
        await bridge.injectPaste();
        store.toast("ui.hotkey", pasted);
      } catch {
        store.toast("ui.hotkey", t("已复制，请按 {shortcut} 手动粘贴", { shortcut: PASTE_SHORTCUT }));
      }
      return;
    }
    store.toast("ui.hotkey", copied);
  } catch (error) {
    // `paste_plain` asks for a text form, and the newest clip may simply not
    // have one — a screenshot at the top of the history is ordinary, not a
    // fault.  Said as a toast beside the key press rather than as the failure
    // band, which is where a sidecar that stopped answering belongs.
    if ((error as { code?: string } | null)?.code === "NOT_SUPPORTED") {
      store.toast("ui.hotkey", t("这一条没有文字可以复制"));
    } else {
      store.setError(error);
    }
  }
}
const SETTINGS_FIELD_ALIASES: Record<string, string> = {
  relay_max_message_bytes: "relay_max_message_kb",
  relay_max_bytes_per_second: "relay_max_rate_kbs",
};
/** Fold the settings another surface changed into the form, by name.
 *
 * The event names what moved; the value is read back from the sidecar, so a
 * field whose spelling differs between disk and form goes through the same
 * transform a load does, and the box ends up holding what is stored rather
 * than what the event happened to carry.  A field this page does not draw — a
 * value only the snapshot shows — is skipped: there is no box to put it in.
 *
 * Only the named fields are touched, so an edit in progress on another card
 * survives a change made on another surface.  A field that is itself the change
 * is the one exception, and there the stored value is the truthful one: the
 * reader's version of it is not saved anywhere yet. */
async function foldSettingsFields(fields: string[]) {
  if (!fields.length) return;
  let loaded: Record<string, any>;
  try {
    loaded = (await bridge.settings()).settings;
  } catch {
    // The page keeps what it has, which is no worse than before the change
    // arrived; the next arrival — or opening the page — asks again.
    return;
  }
  const form = buildSettingsForm(loaded);
  const wasClean = !settingsDirty.value;
  for (const field of fields) {
    const key = SETTINGS_FIELD_ALIASES[field] || field;
    if (key in form) settings.value[key] = form[key];
  }
  // The three that are not boxes on this page: the auto-update switch is its
  // own ref, and the theme and the language are drawn by the shell.
  if (fields.includes("auto_update_check")) autoUpdateCheck.value = loaded.auto_update_check !== false;
  // The paste switch also lives outside the form for one caller: the hotkey
  // path reads it on every press, and a change announced from another surface
  // has to reach that copy as well as the checkbox it just moved.
  if (fields.includes("auto_paste_enabled")) {
    savedAutoPasteEnabled.value = loaded.auto_paste_enabled !== false;
  }
  if (fields.includes("appearance_mode")) {
    // The sidecar is the authority on what is stored, so this is the value a
    // failed pick is rolled back to.
    storedAppearance.value = String(settings.value.appearance_mode || "system");
    applyTheme(storedAppearance.value);
  }
  if (fields.includes("language")) setLocale(loaded.language);
  // A form that had nothing pending is holding stored state again, and the
  // save bar has to stop offering to save what the sidecar just said out loud.
  // One that did have something pending keeps its own snapshot: the pending
  // edit is still pending, and the fields folded in above are not it.
  if (wasClean) savedSettings.value = settingsFormSnapshot();
}
async function openSettings() {
  tab.value = "settings";
  void loadDiscovery();
  // Hydrate the live phase so a download that began before this window opened
  // still renders (the sidecar owns the state; this is a read-only mirror).
  void store.loadUpdateStatus();
  if (settingsLoading) return;
  if (!settingsLoaded.value) return void hydrateSettings();
  const pending = store.consumeSettingsFields();
  // This page holds an unsaved edit the moment it has one, so re-opening it
  // cannot simply re-read the form.  But a change made while the reader was on
  // another page — a rename from the overview card, a save from the phone — is
  // a change this form is now wrong about, and the one it would otherwise save
  // back over the top of.  With nothing pending here the page is re-read whole,
  // which is also how the values that live outside the form (auto-start, the
  // theme) catch up; with an edit pending, only the fields that actually moved
  // are folded in, by name.
  if (!settingsDirty.value) return void hydrateSettings();
  await foldSettingsFields(pending);
}
/** Read the whole settings form from the sidecar.
 *
 * The one place the page is filled end to end: when it opens with nothing
 * loaded, and when it is re-opened with nothing pending in it. */
async function hydrateSettings() {
  settingsLoading = true;
  try {
    const loaded = (await bridge.settings()).settings;
    autoUpdateCheck.value = loaded.auto_update_check !== false;
    settings.value = buildSettingsForm(loaded);
    // The bindings are the host's to hold, and it starts with none: until this
    // runs, the keys the user chose are stored but not listening.
    void applyHotkeys();
    if (native) {
      try { settings.value.auto_start = await bridge.autostartStatus(); }
      catch { /* Keep the persisted preference when the OS query is unavailable. */ }
    }
    savedAutoStart.value = !!settings.value.auto_start;
    savedEncryptionEnabled.value = !!settings.value.encryption_enabled;
    savedAutoPasteEnabled.value = settings.value.auto_paste_enabled !== false;
    storedAppearance.value = String(settings.value.appearance_mode || "system");
    applyTheme(storedAppearance.value);
    applyMotion(settings.value.ui_animation_enabled);
    // The relay's peer list and its per-peer ledgers are read when the devices
    // page opens, where that card is; this page has no row that shows them.
    // The AI tools are read the same way, on the AI page — this form does not
    // carry them and has no field they belong to.
    savedSettings.value = settingsFormSnapshot();
    settingsEditedCards.value = {};
    settingsLoaded.value = true;
    void nextTick(() => { if (settingsForm.value) wireSettingNotes(settingsForm.value); });
  }
  catch (error) { store.setError(error); }
  finally { settingsLoading = false; }
}
/** Fold in a settings change made while this page is on screen.
 *
 * The store keeps the names until something takes them, so an arrival while the
 * reader is on another page waits for `openSettings` above.  Here the page is in
 * front of them and the field is folded in where they can watch it move — only
 * what moved, which is what the names are for: a rename made from the phone
 * changes the name box and leaves every other box on the page as typed. */
watch(() => store.state.settingsEvent?.revision, () => {
  if (tab.value !== "settings" || !settingsLoaded.value) return;
  const fields = store.consumeSettingsFields();
  if (fields.length) void foldSettingsFields(fields);
});
/** A sync pause armed, or ended early, from another surface.
 *
 * The countdown is drawn from the deadline in the settings form, and nothing
 * re-read that form on its own: a pause armed from the phone's panel was
 * invisible here until this window's own deadline had already passed, and one
 * this window had never heard of — no deadline at all — was never picked up, so
 * the badge never appeared.  The deadline is the event's own payload, so it is
 * taken from there rather than by re-reading a form that may hold an edit.
 *
 * `pauseUntil` and not the form, for the reason it lives outside it at all:
 * a deadline folded into `settings` would be read as a change the save bar is
 * holding. */
watch(() => store.state.syncPauseUntil, (until) => {
  pauseUntil.value = until;
});
/** The two LAN switches, as the sidecar last announced them.
 *
 * Both are drawn here — the overview card's toggle and the settings page's own
 * pair — and they were read on the way into those surfaces, so a flip from the
 * phone's panel or the web dashboard, which draw the same two, was invisible
 * here until the page was re-opened. */
watch(() => store.state.discoveryState, (announced) => {
  if (announced) discoveryState.value = announced;
});
/** What a tested broker's row says beside its address.
 *
 * A reachable broker is its latency, and an unreachable one is why — the
 * probe's own words, shown as received, because they name the socket-level
 * reason and there is no shorter true sentence.  A reachable row without a
 * latency is neither of those, and says nothing rather than a latency it has
 * not got.
 */
function relayRowNote(row: RelayTestResult["results"][number]): string {
  if (!row.ok) return String(row.detail || t("不可达"));
  return typeof row.latency_ms === "number" ? t("{ms} 毫秒", { ms: row.latency_ms }) : "";
}
/** The staged brokers, both lists as one, deduplicated in order.
 *
 * The old panel probed both groups as a single list and dropped repeats, so a
 * broker staged in both fields was tested once.  Same reading here, and the
 * order is the order they were typed rather than the order the answer comes
 * back in — the rows are reported sorted, which is a different thing from which
 * brokers were asked.
 */
function stagedRelayBrokers(): string[] {
  const seen = new Set<string>();
  return [...brokerList(settings.value.relay_brokers), ...brokerList(settings.value.relay_private_brokers)]
    .filter((endpoint) => (seen.has(endpoint) ? false : (seen.add(endpoint), true)));
}
/** Probe the staged relay brokers, or the saved ones when nothing is staged.
 *
 * A test that could not say "nothing to test" would open a socket to nothing and
 * report success, which is the one answer worse than an error: the reader would
 * save a relay with no brokers believing it had been checked.
 */
async function testRelayBrokers() {
  if (relayTestBusy.value) return;
  relayTestBusy.value = true;
  relayTestError.value = "";
  relayTestResult.value = null;
  try {
    relayTestResult.value = await bridge.testRelayBrokers(stagedRelayBrokers());
  } catch (error) {
    relayTestError.value = (error as any)?.message || String(error);
  } finally {
    relayTestBusy.value = false;
  }
}
/** Put the form back to what the sidecar has saved.
 *
 * Undo is a re-read, not a stored copy: the settings payload is the single
 * truth, and the fields that apply themselves (the appearance preview, the
 * hotkey bindings, auto-start) have to be put back by the same code that filled
 * the form in the first place.
 */
async function revertSettings() {
  if (settingsBusy.value || settingsLoading || !settingsLoaded.value || !settingsDirty.value) return;
  settingsBusy.value = true;
  try {
    await hydrateSettings();
    settingsSaved.value = false;
    settingsSaveFailed.value = false;
    settingsSavedAt.value = 0;
    announce(t("已撤销未保存的更改"));
  } finally { settingsBusy.value = false; }
}
async function saveSettings() {
  if (settingsBusy.value || !settingsLoaded.value) return;
  // A field the sidecar would refuse stops the save here, with the box marked:
  // clamping it and storing something else was how the page came to show a
  // number that was not in force.  See U51/U52.
  if (settingsBlocked.value) {
    focusFirstFieldError();
    announce(t("有 {count} 个字段还不能保存", { count: Object.keys(settingsErrors.value).length }));
    return;
  }
  // The settings page's checkbox is the other door onto the same switch; the
  // save is what actually turns the relay on, so the explanation has to land
  // before it, not after.  Declared further down with the rest of the consent
  // state — hoisted, and this is the only caller that needs it from up here.
  if (settings.value.internet_sync_enabled && !internetConsentSeen()) {
    askInternetConsent("save");
    return;
  }
  settingsBusy.value = true;
  settingsSaved.value = false;
  try {
    const autoStart = !!settings.value.auto_start;
    const theme = settings.value.appearance_mode;
    // The advanced numbers, and the mDNS type, as they will be stored.
    //
    // `clampNumber` is what keeps a cleared or out-of-range box from failing the
    // whole save — the sidecar refuses anything past the bound outright — but
    // the clamped value was sent without ever being put back into the form.  A
    // box holding 9999 saved 500 and went on showing 9999 beside a bar saying
    // everything was saved, and the number on screen is the one the reader
    // believes is in force.  They are written back once a save has landed.
    const relayMaxMessageKb = clampNumber(
      settings.value.relay_max_message_kb,
      DEFAULT_RELAY_MAX_MESSAGE_BYTES / 1024,
      MIN_RELAY_MAX_MESSAGE_KB,
      MAX_RELAY_MAX_MESSAGE_KB,
    );
    const relayMaxRateKbs = clampNumber(
      settings.value.relay_max_rate_kbs,
      DEFAULT_RELAY_MAX_BYTES_PER_SECOND / 1024,
      MIN_RELAY_RATE_KB,
      MAX_RELAY_RATE_KB,
    );
    // The recording rule's size limit, clamped rather than refused: it is a
    // tuned number like the two relay ones above, and a box one keystroke out
    // of range is narrowed to the edge and then written back, so the number on
    // screen is the one the sidecar stored.
    const maxNonFilePasteMb = clampNumber(
      settings.value.max_non_file_paste_mb,
      DEFAULT_MAX_NON_FILE_PASTE_MB,
      MIN_MAX_NON_FILE_PASTE_MB,
      MAX_MAX_NON_FILE_PASTE_MB,
    );
    const storedNumbers = {
      port: clampNumber(settings.value.port, 19990, 1024, 65535),
      web_history_limit: clampNumber(settings.value.web_history_limit, 30, 1, 500),
      sync_debounce: clampNumber(settings.value.sync_debounce, 0.3, 0.05, 10),
      clipboard_poll_interval: clampNumber(settings.value.clipboard_poll_interval, 1, 0.1, 60),
      transfer_timeout: clampNumber(settings.value.transfer_timeout, 120, 5, 3600),
      max_reconnect_attempts: clampNumber(settings.value.max_reconnect_attempts, 10, 0, 100),
      // The three file limits, clamped the same way: typed numbers rather than
      // free text, and the sidecar refuses anything past its own bounds.
      max_file_mb: clampNumber(settings.value.max_file_mb, DEFAULT_MAX_FILE_MB, 0, MAX_FILE_MB),
      confirm_large_file_mb: clampNumber(
        settings.value.confirm_large_file_mb, DEFAULT_CONFIRM_LARGE_FILE_MB, 0, MAX_FILE_MB),
      relay_paste_max_mb: clampNumber(
        settings.value.relay_paste_max_mb, DEFAULT_RELAY_PASTE_MAX_MB, 0, MAX_FILE_MB),
    };
    const fileReceiveDir = String(settings.value.file_receive_dir || "").trim();
    const serviceType = String(settings.value.service_type || "").trim() || "_clipsync._tcp.local.";
    const result = await bridge.updateSettings({
      device_name: settings.value.device_name,
      appearance_mode: settings.value.appearance_mode,
      plain_text_only: settings.value.plain_text_only,
      // Applied to the live engine, not on restart: the runtime hands it
      // straight to the running chat manager.
      chat_open_to_all: settings.value.chat_open_to_all,
      // The file-transfer page's own admission rule, handed to the running
      // transfer manager on save exactly as the chat one is.
      file_open_to_all: settings.value.file_open_to_all,
      // Read at request time by the running engine, so this one also needs no
      // restart to take effect.
      log_sharing: !!settings.value.log_sharing,
      app_filter_enabled: settings.value.app_filter_enabled,
      app_filter_mode: settings.value.app_filter_mode,
      app_filter_list: String(settings.value.app_filter_list || "").split(/\r?\n/).map(v => v.trim()).filter(Boolean),
      filter_enabled_categories: settings.value.filter_enabled_categories,
      history_max_entries: settings.value.history_max_entries,
      history_max_age_days: settings.value.history_max_age_days,
      translate_url: settings.value.translate_url,
      ...(autoStart !== savedAutoStart.value ? { auto_start: autoStart } : {}),
      sound_enabled: settings.value.sound_enabled,
      ui_animation_enabled: settings.value.ui_animation_enabled,
      // The bindings the host will register.  An emptied row is dropped rather
      // than stored as "": the config's map is "action -> accelerator", and a
      // blank string is not one — the row being absent is what "not bound"
      // means to `set_hotkeys`.
      hotkeys_enabled: !!settings.value.hotkeys_enabled,
      hotkeys: Object.fromEntries(
        Object.entries(settings.value.hotkeys || {})
          .filter(([, binding]) => typeof binding === "string" && binding.trim())
          .map(([action, binding]) => [action, String(binding).trim()]),
      ),
      source_tracking_enabled: settings.value.source_tracking_enabled,
      paste_to_top: settings.value.paste_to_top,
      // What a shortcut does after it copies, and the four rules that decide
      // what the recorder keeps at all (1.3.0).  The pattern list is split the
      // way the process-name list below is: one substring per line, padding and
      // empty lines dropped, because the sidecar matches the list it is given.
      auto_paste_enabled: !!settings.value.auto_paste_enabled,
      skip_password_manager_clips: !!settings.value.skip_password_manager_clips,
      skip_prelaunch_clipboard: !!settings.value.skip_prelaunch_clipboard,
      max_non_file_paste_mb: maxNonFilePasteMb,
      source_exclusion_patterns: String(settings.value.source_exclusion_patterns || "")
        .split(/\r?\n/).map((entry) => entry.trim()).filter(Boolean),
      // The file-transfer limits (1.3.1).  Read per operation by the transfer
      // manager, so a changed limit reaches the next file rather than a restart.
      ...{
        max_file_mb: storedNumbers.max_file_mb,
        confirm_large_file_mb: storedNumbers.confirm_large_file_mb,
        relay_paste_max_mb: storedNumbers.relay_paste_max_mb,
        file_receive_dir: fileReceiveDir,
      },
      internet_sync_enabled: settings.value.internet_sync_enabled,
      relay_brokers: brokerList(settings.value.relay_brokers),
      relay_private_brokers: brokerList(settings.value.relay_private_brokers),
      relay_username: settings.value.relay_username,
      relay_password: settings.value.relay_password,
      // Typed in KB, stored in bytes.  Read when the relay is built, so a
      // changed limit reaches the chunks after a restart — the row's hint says
      // so, next to the control.
      relay_max_message_bytes: relayMaxMessageKb * 1024,
      // The pace those chunks are offered at, in KB/s in the box and bytes/s on
      // disk.  Read when a transfer is offered rather than when the relay is
      // built, so it reaches the next file sent and not one already running.
      relay_max_bytes_per_second: relayMaxRateKbs * 1024,
      // Advanced/network fields.  Numbers are clamped to the sidecar's bounds so
      // a cleared input falls back instead of failing the whole save.
      ...storedNumbers,
      service_type: serviceType,
      log_level: (LOG_LEVELS as readonly string[]).includes(settings.value.log_level) ? settings.value.log_level : "INFO",
      low_memory_mode: !!settings.value.low_memory_mode,
      retry_capture_enabled: !!settings.value.retry_capture_enabled,
      dedup_method: settings.value.dedup_method === "simple" ? "simple" : "sha256",
      data_dir: String(settings.value.data_dir || "").trim(),
      // Security.  The password travels with the form (as in the web panel) and
      // is only sent when filled; clearing it is a separate action.  The toggle
      // goes alone so an unchanged value never re-wires live encryption.
      ...(!!settings.value.encryption_enabled !== savedEncryptionEnabled.value
        ? { encryption_enabled: !!settings.value.encryption_enabled } : {}),
      ...(securityPassword.value ? { password: securityPassword.value } : {}),
    }) as { password_set?: boolean; applied?: boolean };
    if (securityPassword.value) {
      // The sidecar echoes whether a password is now stored; the typed value is
      // never sent back, so drop it from the form.
      if (typeof result.password_set === "boolean") settings.value.password_set = result.password_set;
      securityPassword.value = "";
      securityPasswordConfirm.value = "";
    }
    // Saved, but not running.  The sidecar writes the file first and applies it
    // to the live services second; when the second half failed it used to
    // answer plain success, so the panel said "saved" over a setting the running
    // app had not taken.  Both halves are said now, where the button is.
    if (result?.applied === false) {
      store.toast("ui.settings", t("设置已保存，但未能立即生效，重启后才会使用新配置"));
    }
    savedAutoStart.value = autoStart;
    savedEncryptionEnabled.value = !!settings.value.encryption_enabled;
    // The hotkeys read this copy rather than the form, so the save is where a
    // flipped switch has to reach them — after it landed, not before.
    savedAutoPasteEnabled.value = !!settings.value.auto_paste_enabled;
    // What was stored is what the boxes now hold: a form still showing the
    // number the reader typed is a form claiming a value that is not in force,
    // and the bar below it has just said everything was saved.  Before the
    // baseline below is retaken, so this is not an unsaved edit either.
    Object.assign(settings.value, storedNumbers);
    settings.value.relay_max_message_kb = relayMaxMessageKb;
    settings.value.relay_max_rate_kbs = relayMaxRateKbs;
    settings.value.max_non_file_paste_mb = maxNonFilePasteMb;
    settings.value.file_receive_dir = fileReceiveDir;
    settings.value.service_type = serviceType;
    storedAppearance.value = String(theme || "system");
    applyTheme(storedAppearance.value);
    // The bindings are the one saved value the sidecar cannot apply itself:
    // they are registered with the OS by the host, so the save has to reach it.
    void applyHotkeys();
    savedSettings.value = settingsFormSnapshot();
    settingsEditedCards.value = {};
    settingsSaved.value = true;
    settingsSavedAt.value = Date.now();
    settingsSaveFailed.value = false;
    await store.refresh();
  } catch (error) {
    // Say it where the button is, not only in the band at the top: the band can
    // be a whole card away from the field that was refused.
    settingsSaveFailed.value = true;
    store.setError(error);
    void focusSettingsError();
  }
  finally { settingsBusy.value = false; }
}
async function refreshBackups() {
  try { backups.value = (await bridge.listBackups()).backups; }
  catch (error) { store.setError(error); }
}
/** 刷新备份, which reports what the folder holds. */
async function refreshBackupsNow() {
  await refreshBackups();
  store.toast("ui.settings", t("已刷新 · {count} 个备份", { count: backups.value.length }));
}
async function refreshInternetPairing() {
  try {
    internetPairing.value = await bridge.internetPairingStatus();
    internetPairingFetchedAt.value = Date.now();
    // A wait this card was told had failed is forgotten once the status no
    // longer lists it — that is the sidecar's own removal of the row, and
    // keeping the mark longer would label the device's next wait.
    clearNetpairWaitFailed(new Set(
      (internetPairing.value.waiting || []).map((row: any) => String(row.peer_id)),
    ));
    // The ledger is per peer, so the peers list is what the send state is
    // fetched against — and a peer that has gone away takes its line with it.
    const known = new Set(internetPairing.value.peers.map((peer: any) => String(peer.peer_id)));
    for (const peerId of Object.keys(delivery.state.peers)) {
      if (!known.has(peerId)) delivery.forget(peerId);
    }
    for (const peerId of known) void delivery.load(peerId);
  }
  catch (error) { store.setError(error); }
}
/** The internet card's own refresh, and now the only one over that read.  The
 * send list had a second button asking the same question — the ledger it
 * reloads rides on this call — and the card's status is a poll while the tab is
 * open, so the button is the manual nudge rather than the way it stays current.
 */
async function refreshInternetPairingNow() {
  await refreshInternetPairing();
  store.toast("ui.settings", t("已刷新互联网配对状态"));
}
/** The internet switch, on the card it turns on.
 *
 * It is the same field the settings page saves as 互联网同步 — one switch over
 * one setting, because a second field would let the two disagree, and the one
 * on the settings page is somewhere a reader only finds after they have already
 * gone looking for the pairing card.  Saved on the spot rather than behind the
 * settings page's save button, which this page does not have: the sidecar
 * applies it to the running relay, so there is no restart to wait for either.
 */
const internetSyncBusy = ref(false);
const internetPairingEnabled = computed(() => !!internetPairing.value.enabled);

/** Whether the relay has been explained once, on this installation.
 *
 * Internet sync is the single switch in the window that sends clipboard text
 * off the LAN, and it was the one irreversible-in-spirit choice nothing
 * described: a reader could turn it on from two different pages without ever
 * being told their clipboard would traverse a third-party broker.  The relay is
 * end-to-end encrypted and the broker cannot read what it forwards, but "your
 * data leaves this network" is the reader's call to make, not a footnote.
 *
 * Asked once rather than on every enable: a prompt that returns each time is a
 * prompt people learn to dismiss without reading, and the second time they are
 * not being told anything they did not already decide.  Switching it off and on
 * again does not ask, because it is the same decision.
 *
 * Kept in `localStorage`: it is a fact about this window, and the sidecar owns
 * every real setting.  Absent storage (a browser tab, a locked-down profile)
 * reads as "not yet seen", so the
 * explanation still appears; it just appears again next launch. */
const INTERNET_CONSENT_KEY = "clipsync.internetConsentSeen";
const internetConsentOpen = ref(false);
/** What to do once the reader accepts — the switch on the device card or the
 * save button on the settings page, both of which lead here. */
const internetConsentResume = ref<"switch" | "save" | null>(null);
const internetConsentDialog = useDialog(internetConsentOpen, () => {
  // Dismissed by Esc or the backdrop: the settings form's checkbox is the only
  // thing left holding "on", and left alone it would be saved on the next 保存
  // without the explanation ever having been accepted.
  if (internetConsentResume.value === "save") settings.value.internet_sync_enabled = false;
  internetConsentResume.value = null;
});
function internetConsentSeen(): boolean {
  try { return localStorage.getItem(INTERNET_CONSENT_KEY) === "1"; } catch { return false; }
}
function markInternetConsentSeen() {
  try { localStorage.setItem(INTERNET_CONSENT_KEY, "1"); } catch { /* storage refused */ }
}
/** Ask before the first enable, then do what the caller meant to do. */
function askInternetConsent(resume: "switch" | "save"): void {
  internetConsentResume.value = resume;
  internetConsentOpen.value = true;
}
async function confirmInternetConsent() {
  const resume = internetConsentResume.value;
  markInternetConsentSeen();
  internetConsentResume.value = null;
  internetConsentOpen.value = false;
  if (resume === "save") await saveSettings();
  else if (resume === "switch") await applyInternetSync(true);
}
async function applyInternetSync(enabled: boolean) {
  if (internetSyncBusy.value) return;
  internetSyncBusy.value = true;
  try {
    await bridge.updateSettings({ internet_sync_enabled: enabled });
    // The settings page's own copy of the field follows, or switching tabs back
    // would show the old value and save it again on the next 保存 — and it
    // follows as stored state, because this button is what saved it.
    foldSavedField("internet_sync_enabled", enabled);
    await refreshInternetPairing();
  } catch (error) { store.setError(error); }
  finally { internetSyncBusy.value = false; }
}
async function toggleInternetSync(enabled: boolean, control?: HTMLInputElement | null) {
  if (internetSyncBusy.value) return;
  if (enabled && !internetConsentSeen()) {
    // The box is bound with `:checked` and no `v-model`, so the click flipped
    // the DOM without touching state — and state not changing means Vue has
    // nothing to patch back.  Put it back by hand: the dialog has not been
    // answered yet, and a ticked box behind a modal reads as "on".
    if (control) control.checked = false;
    askInternetConsent("switch");
    return;
  }
  await applyInternetSync(enabled);
}
async function generateInternetPairing() {
  try {
    const result = await bridge.generateInternetPairingCode();
    internetPairing.value.generated_code = result.code;
    internetPairingMessage.value = t("配对码已生成");
    internetPairingFailed.value = false;
  } catch (error) { store.setError(error); }
}
/** Why the sidecar refused a pairing code, in the reader's language.
 *
 * All three refusals are things the reader can act on — a mistyped code,
 * internet sync switched off, a relay that is not up yet — and the sidecar
 * answers each with a code and an English log line.  Left uncaught they landed
 * in the window's top band, so a typo in a code was reported at the top of the
 * window, in English, nowhere near the box holding it.
 */
function pairingErrorText(error: any): string {
  switch (error?.code) {
    case "INVALID_PAIRING_CODE": return t("配对码无效，请核对后重新输入。");
    case "RELAY_OFFLINE": return t("本机还未连上中继，暂时无法配对。请稍后重试。");
    case "INTERNET_SYNC_OFF": return t("互联网同步已关闭，无法配对。");
    case "SAVE_FAILED": return t("配对码未能保存，请重试。");
    default: return error?.message || t("提交配对码失败");
  }
}
/** Submit a code, and report the one thing that is true at that instant.
 *
 * Submitting is not pairing.  The code goes out on the relay and the machine
 * holding it has to answer, so what this call establishes is a *wait*: the
 * status it refreshes below carries the provisional entry, and the card shows
 * it as 等待对方确认 — which is the honest answer to "did it connect?" until
 * the partner's hello arrives and replaces it with a device.  The line this
 * used to leave behind, 已提交配对码, described the click and never changed
 * afterwards, whether the pairing completed or nobody ever answered.
 */
async function enterInternetPairing() {
  if (!internetPairingCode.value.trim()) return;
  try {
    await bridge.enterInternetPairingCode(internetPairingCode.value.trim());
    internetPairingCode.value = "";
    internetPairingMessage.value = "";
    internetPairingFailed.value = false;
    // One read, not two: this call already re-reads the peers and every
    // ledger under them, and the second one that used to follow was the same
    // request sent twice (``refreshDeliveryStatus`` was that call and nothing
    // else).  Both ``load``s are fire-and-forget, so awaiting a second round
    // did not wait for the ledgers either.
    await refreshInternetPairing();
  } catch (error) {
    internetPairingFailed.value = true;
    internetPairingMessage.value = pairingErrorText(error);
  }
}
/** End an internet pairing, and answer whether it ended.
 *
 * The answer is what the device list needs: it offers the same action from
 * outside the pairing card, where a failure reported into the card's own
 * message line would be a message on a page the reader is not looking at.  The
 * card's line is still written here — the call is the card's — and the caller
 * that has somewhere better to say it decides for itself.
 */
async function unpairInternet(peerId: string): Promise<boolean> {
  try {
    await bridge.unpairInternetPeer(peerId);
    internetPairingMessage.value = "";
    internetPairingFailed.value = false;
    await refreshInternetPairing();
    return true;
  }
  catch (error) {
    internetPairingFailed.value = true;
    internetPairingMessage.value = (error as any)?.message || t("解除互联网配对失败");
    return false;
  }
}
// One frame from the other machine is what turns a submitted code into a
// device, and it arrives whenever it arrives — seconds after the page was read,
// or minutes.  Without this the card kept whatever it saw on the way in, so a
// pairing that completed while the reader was looking at it showed nothing
// until they left the page and came back.
//
// The same channel carries the three ways a pairing *stops* moving: a wait the
// partner never answered (`netpair.waiting_failed`), a generated code that
// outlived its window (`netpair.code.expired`), and two devices that hold
// different encryption passwords (`netpair.decrypt_failed`).  The last one is
// only a notice — the pairing is still listed — but the first two change what
// the card draws, so they are applied to the local view before the re-read.
watch(() => store.state.netpairEvent?.revision, async () => {
  const update = store.state.netpairEvent;
  if (!update) return;
  const data = update.data || {};
  if (update.name === "netpair.code.expired") {
    // The code can no longer be typed, so showing it would invite a reader to
    // copy a value that will be refused; the status re-read confirms it.
    internetPairing.value.generated_code = null;
    internetPairingMessage.value = "";
    internetPairingFailed.value = false;
  }
  if (update.name === "netpair.waiting_failed") {
    const peerId = String(data.peer_id || "");
    if (peerId) netpairWaitFailed.value = { ...netpairWaitFailed.value, [peerId]: true };
  }
  if (tab.value !== "internet") return;
  await refreshInternetPairing();
});
/** Open the rename dialog on an internet-pairing peer.  The alias is this
 * machine's own name for the peer — the line the button sits on shows it — so
 * the write is local, like a device's note. */
async function renameInternet(peer: any) {
  const current = peer.alias || peer.name || "";
  renameTarget.value = { kind: "internet", peerId: peer.peer_id };
  renameValue.value = current;
}
async function createBackup() {
  if (backupTask.value) return;
  startBackupTask("backup");
  try {
    const result = await bridge.createBackup();
    backupMessage.value = t("备份已创建：{path}", { path: result.backup_path });
    await refreshBackups();
  } catch (error) { store.setError(error); }
  finally { endBackupTask(); }
}
async function openDataFolder(which: "data" | "backups") {
  try {
    const result = await bridge.openDataFolder(which);
    backupMessage.value = t("已打开：{path}", { path: result.folder });
  } catch (error) { store.setError(error); }
}
async function restoreBackup() {
  if (restoreBusy.value) return;
  try { restorePath.value = await bridge.chooseFile("backup"); }
  catch (error) { store.setError(error); }
}
/** Offer one of the listed backups for restore, the way the legacy panel did.
 *
 * The path is already in hand, so it goes straight to the same confirmation
 * dialog the picker fills in: a reader looking at the backup in the list should
 * not have to find the same file a second time in a file dialog.
 */
function restoreListedBackup(item: Record<string, unknown>) {
  const path = String(item.path || "");
  if (!path || restoreBusy.value) return;
  restorePath.value = path;
}
/** What a restore did, in the reader's words rather than the host's.
 *
 * The answer comes back as `{"config": bool, "history": n, "favorites": n,
 * "errors": [...]}` and was shown as that object, JSON and all — a reader who
 * had just asked for their history back was handed the payload instead of the
 * result.  The `errors` list is the half that matters most and the half the
 * JSON hid best: a restore that brought the rows back but could not persist
 * the settings beside them read as a success with a foreign word at the end.
 */
function restoreSummary(result: Record<string, unknown>): string {
  const parts = [t("历史 {count} 条", { count: Number(result.history) || 0 }),
    t("{count} 条收藏", { count: Number(result.favorites) || 0 })];
  if (result.config === true) parts.unshift(t("设置"));
  const line = t("恢复完成 · {parts}", { parts: parts.join("、") });
  const failed = Array.isArray(result.errors) ? result.errors.length : 0;
  return failed ? `${line}（${t("有 {count} 项没有完成", { count: failed })}）` : line;
}
/** Esc while a restore is running must not clear the dialog's state: the
 *  operation is still going and its own completion is what closes this. */
function onRestoreCancel() {
  if (restoreBusy.value) return;
  restorePath.value = null;
}
async function confirmRestoreBackup() {
  const path = restorePath.value;
  if (!path || restoreBusy.value) return;
  restoreBusy.value = true;
  backupMessage.value = "";
  startBackupTask("restore");
  try {
    const result = await bridge.restoreBackup(path);
    backupMessage.value = restoreSummary(result);
    restorePath.value = null;
    await store.refresh();
  } catch (error) { store.setError(error); }
  finally {
    // Even a partial restore can change configuration.
    settingsLoaded.value = false;
    await openSettings();
    restoreBusy.value = false;
    endBackupTask();
  }
}
/** Write the history to a file, and answer the sentence that says so.
 *
 * The answer matters to the overview, which reaches the same export from its
 * action rail: the settings card draws the sentence beside its own buttons, and
 * a page with no such card has to say it in a toast instead.  `null` on a
 * refusal, which the caller reports through the store, not as a second notice.
 */
async function exportHistory(format: string): Promise<string | null> {
  if (backupTask.value) return null;
  startBackupTask("export");
  try {
    const result = await bridge.exportHistory(format);
    backupMessage.value = t("导出完成：{name}", { name: result.filename || result.filepath });
    return backupMessage.value;
  } catch (error) { store.setError(error); return null; }
  finally { endBackupTask(); }
}
/** Read a history file the reader picked, and answer what it brought back. */
async function importHistory(): Promise<string | null> {
  // The picker is inside the try: it is a native call the host can refuse, and
  // the window's other two pickers (`shareFileToPhone`, `restoreBackup`) both
  // say so.  Left outside it, a refused dialog was an unhandled rejection and
  // the click that asked for it had no answer at all.
  if (backupTask.value) return null;
  try {
    const path = await bridge.chooseFile("history");
    if (!path) return null;
    startBackupTask("import");
    const result = await bridge.importHistory(path);
    // The count, not the object: the answer is `{"ok": true, "imported": n}`
    // and showing it whole put a payload where the reader was looking for
    // how much of their history had come back.
    backupMessage.value = t("导入完成 · {count} 条记录", { count: Number(result.imported) || 0 });
    await store.refresh();
    return backupMessage.value;
  } catch (error) { store.setError(error); return null; }
  finally { endBackupTask(); }
}
/** The overview's copies of the two file actions: same work, said in a toast.
 *
 * The settings page's backup card draws its sentence on the card; the overview
 * has no such card, and an export that only opens a save dialog or an import
 * that only changes a list behind another page has to say what it did where the
 * reader is looking.
 */
async function exportHistoryFromOverview(format: string) {
  const message = await exportHistory(format);
  if (message) store.toast("ui.history", message);
}
async function importHistoryFromOverview() {
  const message = await importHistory();
  if (message) store.toast("ui.history", message);
}
/** The overview's own openers, each the same one-liner the settings page uses:
 * the card is reached by the page then the section, and the section alone would
 * leave the reader on the overview with the setting highlighted somewhere
 * behind them. */
function openSettingsCardFromOverview(id: string) {
  void openSettings();
  openSettingsCard(id);
}
/** Open the quick-paste overlay from the overview.  One host command: the
 * window, the focus bookkeeping and the paste are all its. */
async function openQuickPasteFromOverview() {
  try { await bridge.openQuickPaste(); } catch (error) { store.setError(error); }
}
async function translateText() {
  if (!translationText.value.trim() || translationBusy.value) return;
  const generation = ++translationGeneration;
  translationBusy.value = true;
  try {
    const result = await bridge.translate(translationText.value, translationTarget.value, translationSource.value);
    if (generation !== translationGeneration) return;
    translationResult.value = String(result.translated || "");
  } catch (error) {
    if (generation === translationGeneration) store.setError(error);
  } finally { translationBusy.value = false; }
}
</script>

<template>
  <div class="application">
    <aside class="sidebar">
      <div class="brand"><img :src="logo" alt="" /><span>ClipSync</span></div>
      <nav :aria-label="t('主导航')">
        <!-- The three pages a reader lives in sit at the top: what this machine
             is doing (概览), what it has copied (剪贴板历史), and which machines
             it talks to (设备).  Everything else is a collection kept on one of
             them, or a tool that configures the window.  The visual order changed
             and the digit chords did not: `PAGES` still counts 概览/历史/设备 as
             1/2/3, so a reader's fingers keep working. -->
        <button :aria-label="t('概览')" :aria-current="tab === 'overview' ? 'page' : undefined" :title="pageTitle(t('概览'), 0)" :aria-keyshortcuts="pageChord(0)" :class="{ active: tab === 'overview' }" @click="tab = 'overview'">
          <Activity :size="18" /><span>{{ t("概览") }}</span>
        </button>
        <button :aria-label="t('剪贴板历史')" :aria-current="tab === 'history' ? 'page' : undefined" :title="pageTitle(t('剪贴板历史'), 1)" :aria-keyshortcuts="pageChord(1)" :class="{ active: tab === 'history' }" @click="tab = 'history'">
          <History :size="18" /><span>{{ t("剪贴板历史") }}</span>
        </button>
        <button :aria-label="t('设备')" :aria-current="tab === 'devices' ? 'page' : undefined" :title="pageTitle(t('设备'), 2)" :aria-keyshortcuts="pageChord(2)" :class="{ active: tab === 'devices' }" @click="tab = 'devices'">
          <Monitor :size="18" /><span>{{ t("设备") }}</span>
        </button>
        <!-- The collections kept by the history page, and the tools that talk to
             another machine or configure the window.  Two labels are the whole
             of the grouping; `role="group"` carries the name to a screen reader
             and the visible line is hidden from it, so the group is named once
             rather than twice.  See U36. -->
        <div class="nav-group" role="group" :aria-label="t('内容')">
          <p class="nav-group-label" aria-hidden="true">{{ t("内容") }}</p>
          <button :aria-label="t('收藏库')" :aria-current="tab === 'favorites' ? 'page' : undefined" :title="pageTitle(t('收藏库'), 3)" :aria-keyshortcuts="pageChord(3)" :class="{ active: tab === 'favorites' }" @click="tab = 'favorites'">
            <Star :size="18" /><span>{{ t("收藏库") }}</span>
          </button>
          <button :aria-label="t('附近聊天')" :aria-current="tab === 'chat' ? 'page' : undefined" :title="pageTitle(t('附近聊天'), 5)" :aria-keyshortcuts="pageChord(5)" :class="{ active: tab === 'chat' }" @click="tab = 'chat'">
            <MessageCircle :size="18" /><span>{{ t("附近聊天") }}</span>
            <!-- The count reaches the reader on whatever page they are on, the
                 way the panel's own chat row carried it.  The label above stays
                 the page's name alone: a screen reader announcing the name and
                 then the number would read the two as one name. -->
            <b v-if="chatUnread" class="nav-count" :aria-label="t('{count} 条未读消息', { count: chatUnread })">{{ chatUnread }}</b>
          </button>
          <button :aria-label="t('文件传输')" :aria-current="tab === 'transfers' ? 'page' : undefined" :title="pageTitle(t('文件传输'), 4)" :aria-keyshortcuts="pageChord(4)" :class="{ active: tab === 'transfers' }" @click="tab = 'transfers'">
            <FileUp :size="18" /><span>{{ t("文件传输") }}</span>
          </button>
        </div>
        <div class="nav-group" role="group" :aria-label="t('工具')">
          <p class="nav-group-label" aria-hidden="true">{{ t("工具") }}</p>
          <button :aria-label="t('手机伴侣')" :aria-current="tab === 'companion' ? 'page' : undefined" :title="pageTitle(t('手机伴侣'), 8)" :aria-keyshortcuts="pageChord(8)" :class="{ active: tab === 'companion' }" @click="openPage('companion')">
            <Smartphone :size="18" /><span>{{ t("手机伴侣") }}</span>
          </button>
          <button :aria-label="t('互联网同步')" :aria-current="tab === 'internet' ? 'page' : undefined" :title="t('互联网同步（中继）')" :class="{ active: tab === 'internet' }" @click="openPage('internet')">
            <Globe :size="18" /><span>{{ t("互联网同步") }}</span>
          </button>
          <!-- AI configuration is a workbench over files, not a preference
               card: it lives beside the device it reads and edits, not among
               the clip collections. -->
          <button :aria-label="t('AI 配置')" :aria-current="tab === 'ai' ? 'page' : undefined" :title="pageTitle(t('AI 配置'), 6)" :aria-keyshortcuts="pageChord(6)" :class="{ active: tab === 'ai' }" @click="openAiPage">
            <Sparkles :size="18" /><span>{{ t("AI 配置") }}</span>
          </button>
        </div>
        <!-- Settings is not another page of content: it is the window's own
             configuration, so it sits below the pages that hold content,
             behind a rule, the way a mature desktop sidebar carries it. -->
        <div class="sidebar-settings">
          <button :aria-label="t('设置')" :aria-current="tab === 'settings' ? 'page' : undefined" :title="pageTitle(t('设置'), 7)" :aria-keyshortcuts="pageChord(7)" :class="{ active: tab === 'settings' }" @click="openSettings">
            <SettingsIcon :size="18" /><span>{{ t("设置") }}</span>
          </button>
        </div>
      </nav>
      <div class="sidebar-footer">
        <div class="local-device"><Monitor :size="17" /><span>{{ state.status?.device_name || t('此设备') }}</span></div>
        <!-- What this window is running, in the same words the About dialog uses.
             The framework it is built with is not the reader's business, and
             "Tauri Desktop · 1.0.0" read as a build stamp rather than a product. -->
        <span v-if="state.status" class="note">{{ t("版本 {version}", { version: state.status.version }) }}</span>
        <!-- No window action here.  退出 belongs to the tray, and the window is
             decorated, so its title bar already carries minimize: a second
             control for the same thing was a duplicate of a title-bar button.
             What is left in the footer is what the footer is for — which
             machine this is, and which build of the app is on it. -->
      </div>
    </aside>

    <main id="main-content">
      <!-- Eight sidebar stops sat between the top of the window and the page.
           Hidden until it is focused, so it costs the pointer nothing. -->
      <a class="skip-link" href="#main-content" @click.prevent="focusMain">{{ t("跳到主内容") }}</a>
      <header>
        <div><h1 ref="pageHeadingEl" tabindex="-1">{{ heading }}</h1>
          <p class="muted">{{ summary }}</p>
        </div>
        <div class="header-actions">
          <button type="button" class="header-sponsor" :aria-label="t('赞助')" :title="t('赞助 ClipSync')" @click="openSponsor">
            <Heart :size="16" /><span>{{ t("赞助") }}</span>
          </button>
          <div class="connection"><Circle :size="8" :class="{ connected: ready }" fill="currentColor" />{{ health }}</div>
        </div>
      </header>

      <!-- Two refusals in a row is the point the page stopped being live: the
           list below keeps its last good contents, which look exactly like a
           list that is up to date. -->
      <p v-if="state.staleSince" class="strip strip--warn stale-band" role="status">
        <CloudOff :size="16" aria-hidden="true" />
        <span>{{ t("连不上后台服务（{time} 起）。下面显示的是当时读到的内容，可能已经过时。", { time: shortTime(Math.floor(state.staleSince / 1000)) }) }}</span>
      </p>

      <div v-if="state.error" ref="errorBandEl" role="alert" tabindex="-1" class="error-band">
        <AlertCircle :size="19" />
        <!-- What the reader is told, and what they can do about it.  The
             sidecar's own token (`SIDECAR_UNAVAILABLE`, `ACTION_REJECTED`) used
             to be the band's second line, which is the one line a person
             cannot act on: it is kept, folded, because it is exactly what a
             bug report needs and nothing else here says it. -->
        <div>
          <strong>{{ state.error.message }}</strong>
          <ErrorDetail :code="state.error.code" :message="state.error.message" />
        </div>
        <!-- A data directory the sidecar refuses to start on is the one error a
             retry cannot fix: the repair has to run outside the sidecar. -->
        <button v-if="dataInvalid" class="icon-button" :title="t('修复数据')" :aria-label="t('修复数据')" :disabled="state.pending" @click="recoverOpen = true"><Wrench :size="18" /></button>
        <!-- The band names the cause but not who is holding it; this opens the
             part that does.  A word rather than an icon, because the answer is
             a paragraph of steps and the icon would be a question mark. -->
        <button v-if="dataInUse" type="button" class="error-band__why" @click="dataInUseOpen = true">{{ t("谁占用了？") }}</button>
        <button v-if="state.error.retryable" class="icon-button" :title="t('重新连接')" :aria-label="t('重新连接')" @click="store.reconnect"><RefreshCw :size="18" /></button>
        <!-- And a way out for the errors that cannot be retried.  The band had no control at all
             for those, so a request the sidecar refused left a red line the reader could only wait
             out.  Offered only when there is
             nothing to retry: an error that *can* be retried is answered by the button beside this
             one, and a second way to dismiss it would be a second thing to explain. -->
        <button v-else class="icon-button" :title="t('关闭')" :aria-label="t('关闭提示')" @click="store.dismissError()"><X :size="18" /></button>
      </div>

      <div class="content" ref="contentEl">
        <form v-if="state.status?.health === 'locked'" class="unlock" @submit.prevent="unlock">
          <LockKeyhole :size="34" /><h2>{{ t("解锁 ClipSync") }}</h2>
          <label for="password">{{ t("加密密码") }}</label>
          <input id="password" v-model="password" type="password" autocomplete="current-password" maxlength="1024" required />
          <button class="primary" type="submit" :disabled="state.pending || !password">{{ t("解锁") }}</button>
        </form>

        <OverviewView v-else-if="tab === 'overview'"
          :store="store" :enabled="overviewAvailable" :sync-running="state.status?.sync_state === 'running'"
          :companion-on="!!companion?.enabled"
          :pause-left-ms="pauseLeftMs" :pause-busy="pauseBusy"
          @pause="pauseSync" @resume="resumeSync" @companion="configureCompanion"
          @rename="renameThisDevice" @qr="openCompanionQr"
          @devices="openPage('devices')" @diagnostics="openDiagnosticsCard"
          @quick-paste="openQuickPasteFromOverview" @send-files="openPage('transfers')"
          @import-history="importHistoryFromOverview" @export-history="exportHistoryFromOverview"
          @settings-hotkeys="openSettingsCardFromOverview('general')"
          @settings-update="openSettingsCardFromOverview('update')"
          @companion-page="openPage('companion')"
          @check-update="checkForUpdate" @settings="openSettings" />

        <HistoryView v-else-if="tab === 'history'" ref="historyView"
          :store="store" :ready="ready" :native="native" :translating="translateItemReading"
          @menu="historyMenu" @translate="openTranslateItem" @delete="deleteItem = $event"
          @batch-delete="batchDeleteIds = $event" @paste-to-app="pasteToCurrentApp" />

        <FavoritesView v-else-if="tab === 'favorites'" ref="favoritesView"
          :store="store.favorites"
          :enabled="favoritesAvailable" :notify="notifyFavorites" />

        <TransfersView
          v-else-if="tab === 'transfers'"
          :devices="state.devices"
          :dropped="droppedPaths"
          :packing="state.packing"
          :preselect="transferPreselect"
          :notify="notifyTransfers"
          @dropped="droppedPaths = []"
        />
        <!-- The receipt map comes from the store rather than a chat-local
             fetch: it is fed by the event stream, so a message that was sent
             before this tab was ever opened still shows how it ended. -->
        <template v-else-if="tab === 'chat'">
          <!-- Where this page was reached from.  A conversation is usually
               opened from a device row, and the page it lands on is the chat
               page — the device list the reader was working in is behind them
               with nothing saying which row it was or how to get back.  One
               line, and only when there is something to go back to. -->
          <div v-if="chatOrigin" class="chat-origin">
            <button type="button" class="text-button" @click="backToDevicesFromChat">
              <ChevronLeft :size="16" />{{ t("返回设备") }}
            </button>
            <span class="note">{{ t("从 {name} 的「聊天」进来", { name: chatOrigin.label }) }}</span>
          </div>
          <ChatView :receipts="delivery.state.messages"
            :open-session="chatSessionToOpen" :notify="notifyChat" :dropped="droppedPaths"
            @opened="chatSessionToOpen = ''" @selected="store.setOpenChatSession"
            @answered="store.retractInviteNotice"
            @dropped="droppedPaths = []" @fallback="chatDropFallback" @devices="openPage('devices')" />
        </template>
        <section v-else-if="tab === 'settings'" class="settings-panel">
          <div class="settings-layout">
            <!-- The legacy Settings window searched itself the same way: over
                 the text the reader can see, marking which parts of the page
                 hold it.  The box is the page's rather than the strip's — it is
                 about all ten cards, and a reader with a query in mind
                 should not have to guess which one holds it — and it sits above
                 the strip, which stays put as the card scrolls. -->
            <div class="settings-search">
              <input ref="settingsSearchInput" v-model="settingsQuery" type="search" :aria-label="t('搜索设置…')" :placeholder="t('搜索设置…')"
                @keydown.enter.prevent="jumpToSettingsMatch" @keydown.esc="clearSettingsSearch" />
              <button v-if="settingsSearching" type="button" class="settings-search-clear" @click="clearSettingsSearch">{{ t("清除搜索") }}</button>
              <p v-if="settingsSearching" class="settings-search-status" role="status">{{ settingsMatchTotal
                ? t("{count} 个分区匹配“{query}”", { count: settingsMatchTotal, query: settingsQuery.trim() })
                : t("没有匹配“{query}”的设置项", { query: settingsQuery.trim() }) }}</p>
            </div>
            <!-- One row, because a card is one click from the strip rather
                 than two.  It used to be two rows — four groups, then the open
                 group's cards — which put every card but a group's first one
                 behind its tab, and made the group names a second name for a
                 place the cards already named: 系统 held 软件更新 and 诊断与维护,
                 and neither of those words was 系统.  The four names are the
                 table of contents now, set into the strip as labels over the run
                 of chips they introduce, so the page is ten chips and every one
                 of them is one click.  `role="group"` carries each name to a
                 screen reader and the visible label is hidden from it, the way
                 the sidebar's two groups already work.  The chip's own dot is
                 also what made the group dot redundant: every card is on screen,
                 so the dot that says where an unsaved edit is no longer has to
                 be kept one level up.  See U37. -->
            <nav class="settings-nav" :aria-label="t('设置分区')">
              <div v-for="group in settingsGroups" :key="group.label" class="settings-nav-group"
                role="group" :aria-label="group.label">
                <p class="settings-nav-label" aria-hidden="true"
                  :class="{ dimmed: settingsSearching && !settingsGroupMatches(group) }">{{ group.label }}</p>
                <button v-for="section in group.items" :key="section.id" type="button"
                  :class="{
                    active: settingsSection === section.id && !settingsSearching,
                    'has-match': settingsSearching && !!settingsMatches[section.id],
                    dimmed: settingsSearching && !settingsMatches[section.id],
                  }"
                  :aria-current="settingsSection === section.id && !settingsSearching ? 'true' : undefined"
                  @click="openSettingsCard(section.id)">{{ section.label }}<span
                    v-if="settingsSearching && settingsMatches[section.id]"
                    class="settings-match-count"> · {{ settingsMatches[section.id] }}</span><span
                    v-if="cardHasEdits(section.id)" class="settings-edited" role="img"
                    :title="t('有未保存的更改')" :aria-label="t('有未保存的更改')"></span></button>
              </div>
            </nav>
            <!-- The rail answers "did I leave an edit on a card I cannot see",
                 so the form reports every edit as it happens: `input` because a
                 keystroke is itself the edit, `change` because a select and a
                 checkbox announce themselves only once they are done. -->
            <form ref="settingsForm" class="settings-form" @submit.prevent="saveSettings"
              @input="markSettingsEdited" @change="markSettingsEdited"
              @wheel="guardNumberWheel">
              <section v-show="showSettingsCard('general')" id="settings-general" class="settings-section">
                <h2>{{ t("常规") }}</h2>
                <label class="setting">
                  <span class="setting-name">{{ t("设备名称") }}</span>
                  <span class="setting-control"><input v-model="settings.device_name" maxlength="128" required /></span>
                </label>
                <!-- Two groups instead of five loose rows.  The card mixed the
                     two things people come here for — how the window looks, and
                     what it does on startup — and read as one undifferentiated
                     column under a heading that promised 常规. -->
                <fieldset>
                  <legend>{{ t("外观与语言") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("主题") }}</span>
                    <span class="setting-control"><select v-model="settings.appearance_mode" :aria-label="t('主题')" @change="previewAppearance"><option value="system">{{ t("跟随系统") }}</option><option value="light">{{ t("浅色") }}</option><option value="dark">{{ t("深色") }}</option><option value="sepia">{{ t("暖色") }}</option><option value="midnight">{{ t("午夜") }}</option><option value="contrast">{{ t("高对比") }}</option></select><span class="note setting-inline-note">{{ t("选完立即生效") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("语言") }}</span>
                    <span class="setting-control"><select :value="currentLocale" :aria-label="t('语言')" @change="applyLanguage(($event.target as HTMLSelectElement).value)"><option v-for="code in LOCALES" :key="code" :value="code">{{ LOCALE_NAMES[code] }}</option></select><span class="note setting-inline-note">{{ t("选完立即生效") }}</span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("启动与提示") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.auto_start" type="checkbox" /><span>{{ t("开机自动启动") }}</span></span>
                  </label>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.sound_enabled" type="checkbox" /><span>{{ t("启用声音") }}</span></span>
                  </label>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.ui_animation_enabled" type="checkbox" @change="previewMotion" /><span>{{ t("启用动画") }}</span><span class="note setting-inline-note">{{ t("选完立即生效") }}</span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("全局快捷键") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.hotkeys_enabled" type="checkbox" /><span>{{ t("启用全局快捷键") }}</span></span>
                  </label>
                  <p class="note setting-note">{{ t("组合键写法如 Ctrl+Shift+V。留空表示不绑定；被别的程序占用时这一行会标红。ClipSync 窗口在前台时，这些键仍归窗口自己用。") }}</p>
                  <p class="note setting-note">{{ t("不想手打就点右边的键盘按钮，然后按一次想要的组合键；Esc 取消。手打照样可以。") }}</p>
                  <!-- The bindings stay on the page when the switch is off, greyed
                       rather than gone: the rows used to vanish, so turning the
                       feature off and back on looked like it had lost every
                       binding, and the card's height jumped under the pointer. -->
                  <!-- `settings.hotkeys` is absent until the first load lands,
                       so the rows wait for it rather than indexing into it. -->
                  <template v-if="settings.hotkeys">
                  <label v-for="row in hotkeyRows" :key="row.id" class="setting hotkey-row"
                    :class="{ 'hotkey-row--refused': hotkeyRefused.includes(row.id), 'hotkey-row--off': !settings.hotkeys_enabled, 'hotkey-row--recording': hotkeyRecording === row.id }">
                    <span class="setting-name">{{ row.label }}<span v-if="hotkeyRefused.includes(row.id)" class="hotkey-row__note">{{ t("未生效") }}</span></span>
                    <span class="setting-control"><input v-model="settings.hotkeys[row.id]" class="hotkey-row__input"
                      :aria-label="row.label" maxlength="64" spellcheck="false"
                      :placeholder="hotkeyRecording === row.id ? t('按下组合键…') : t('未绑定')"
                      :data-hotkey-row="row.id" :disabled="!settings.hotkeys_enabled"
                      @keydown="captureHotkey($event, row.id)" @blur="hotkeyRecording === row.id && stopHotkeyRecording()" />
                      <button type="button" class="icon-button icon-button--inline" :disabled="!settings.hotkeys_enabled"
                        :class="{ 'is-recording': hotkeyRecording === row.id }"
                        :aria-label="t('录制 {label} 的组合键', { label: row.label })" :title="t('按一次组合键')"
                        @click="startHotkeyRecording(row.id)"><Keyboard :size="16" /></button></span>
                  </label>
                  <p v-if="hotkeyRecording" class="note setting-note hotkey-recording" role="status">{{ t("正在录制：按下想要的组合键，Esc 取消。") }}</p>
                  <p v-if="hotkeyNotice" class="note setting-note field-error" role="alert">{{ hotkeyNotice }}</p>
                  <p v-if="!settings.hotkeys_enabled" class="note setting-note">{{ t("先把上面的开关打开，这些组合键才会生效；这里填过的内容会保留。") }}</p>
                  </template>
                </fieldset>
              </section>
              <section v-show="showSettingsCard('sync')" id="settings-sync" class="settings-section">
                <h2>{{ t("同步") }}</h2>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.plain_text_only" type="checkbox" /><span>{{ t("仅同步纯文本") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.paste_to_top" type="checkbox" /><span>{{ t("复制后置顶") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.source_tracking_enabled" type="checkbox" /><span>{{ t("记录来源应用") }}</span></span>
                </label>
                <fieldset>
                  <legend>{{ t("互联网同步（中继）") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.internet_sync_enabled" type="checkbox" /><span>{{ t("启用互联网同步") }}</span></span>
                  </label>
                  <!-- Six fields and two paragraphs of explanation sat between
                       the two switches a reader came here for and the rest of
                       the card, so 仅同步纯文本 was pushed off the first screen
                       by settings almost nobody changes: the addresses and the
                       credentials default to the public relay, and the two rate
                       boxes are for a relay that is not the shipped one.  They
                       fold now, and the switch above them — the one that turns
                       the feature on — does not.  A search that reaches this
                       card opens the fold on its own: a match inside a closed
                       box is a match the reader cannot see.  See U186. -->
                  <p v-if="!relayShown" class="note setting-note">{{ t("地址和凭据都不用改：留空就用公共中继。要接私有中继、或中继限速时再展开。") }}</p>
                  <button type="button" class="settings-fold" :aria-expanded="relayShown" aria-controls="settings-relay"
                    @click="relayAdvancedOpen = !relayAdvancedOpen">
                    <component :is="relayShown ? ChevronUp : ChevronDown" :size="15" />
                    {{ relayShown ? t("收起中继的高级设置") : t("高级：中继地址与限速") }}
                  </button>
                  <div v-show="relayShown" id="settings-relay" class="settings-fold__body">
                  <!-- "One address per line" is an instruction a single-line
                       input cannot follow: the list a reader types here is
                       split on newlines (and commas), so it gets the same
                       multi-line control the process-name list in the next card
                       already uses, and the hint the legacy panel put under each. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("公共中继地址") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.relay_brokers" rows="3" :aria-label="t('公共中继地址')" :placeholder="t('每行一个地址')"
                        data-settings-field="relay_brokers" :aria-invalid="!!settingsErrors.relay_brokers"
                        :aria-describedby="settingsErrors.relay_brokers ? 'settings-error-relay-brokers' : undefined"></textarea>
                      <span class="note">{{ t("兜底通道：私有中继不可达时启用，并作为镜像让落在不同中继的设备仍能互通。公共中继接受任何客户端，也绝不会拿到你的中继密码。") }}</span>
                      <span v-if="settingsErrors.relay_brokers" id="settings-error-relay-brokers" class="field-error" role="alert">{{ settingsErrors.relay_brokers }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("私有中继地址") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.relay_private_brokers" rows="3" :aria-label="t('私有中继地址')" :placeholder="t('每行一个地址')"
                        data-settings-field="relay_private_brokers" :aria-invalid="!!settingsErrors.relay_private_brokers"
                        :aria-describedby="settingsErrors.relay_private_brokers ? 'settings-error-relay-private' : undefined"></textarea>
                      <span class="note">{{ t("主通道：剪贴板数据优先走这些端点，用下方用户名和密码登录。留空则只使用上面的公共中继。") }}</span>
                      <span v-if="settingsErrors.relay_private_brokers" id="settings-error-relay-private" class="field-error" role="alert">{{ settingsErrors.relay_private_brokers }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("中继用户名") }}</span>
                    <span class="setting-control"><input v-model="settings.relay_username" autocomplete="off" /></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("中继密码") }}</span>
                    <span class="setting-control"><input v-model="settings.relay_password" type="password" autocomplete="new-password" :placeholder="t('留空表示不修改')" /></span>
                  </label>
                  <!-- The broker's own limit, not this app's: a broker that
                       refuses a message over its ceiling drops it silently, so
                       a file chunk sized past it never arrives while the sender
                       has already counted it sent.  Set to what the relay in
                       use allows and every chunk is cut to fit under it — the
                       reason a free tier (64 KB per message) needs this at all,
                       after a chunk that only fits a 256 KB broker stalled. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("单条消息大小上限") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.relay_max_message_kb" :aria-label="t('单条消息大小上限')" type="number" :min="MIN_RELAY_MAX_MESSAGE_KB" :max="MAX_RELAY_MAX_MESSAGE_KB" step="1"
                        data-settings-field="relay_max_message_kb" :aria-invalid="!!settingsErrors.relay_max_message_kb"
                        :aria-describedby="settingsErrors.relay_max_message_kb ? 'settings-error-relay-kb' : undefined" />
                      <span class="note">{{ t("单位 KB。填中继服务器允许的单条消息上限，传输文件时每个数据块都会按它切分——超出会被服务器直接丢弃。免费中继常见 64 KB，公共中继为 256 KB。修改后需重启生效。") }}</span>
                      <span v-if="settingsErrors.relay_max_message_kb" id="settings-error-relay-kb" class="field-error" role="alert">{{ settingsErrors.relay_max_message_kb }}</span>
                    </span>
                  </label>
                  <!-- The rate half of the same problem.  A relay that keeps a
                       message under its ceiling still drops most of a burst
                       sent too fast, and reports nothing when it does — so a
                       file transfer paces itself at this ceiling, confirms
                       every chunk with the other device, and slows down by
                       itself when chunks go missing.  This is where a faster
                       private relay is told it can carry more; a slower one is
                       found on its own. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("发送速率上限") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.relay_max_rate_kbs" :aria-label="t('发送速率上限')" type="number" :min="MIN_RELAY_RATE_KB" :max="MAX_RELAY_RATE_KB" step="1"
                        data-settings-field="relay_max_rate_kbs" :aria-invalid="!!settingsErrors.relay_max_rate_kbs"
                        :aria-describedby="settingsErrors.relay_max_rate_kbs ? 'settings-error-relay-rate' : undefined" />
                      <span class="note">{{ t("单位 KB/s。中继单块没超限也可能丢弃：连发太快时，服务器会悄悄丢掉其中大部分。传输文件时按这个速率匀速发送，逐块确认对方已收到，丢块时自动降速重传。免费中继约 64 KB/s，私有中继更快可调高。修改后对新发起的传输生效。") }}</span>
                      <span v-if="settingsErrors.relay_max_rate_kbs" id="settings-error-relay-rate" class="field-error" role="alert">{{ settingsErrors.relay_max_rate_kbs }}</span>
                    </span>
                  </label>
                  <!-- The panel's own 测试 button.  Last in the card, because it
                       is about the four fields above it rather than a fifth
                       thing to fill in: the relay reads as one group of
                       settings, and then one control that asks whether the
                       brokers in it answer. -->
                  <div class="setting-block relay-test-block">
                    <button type="button" :disabled="relayTestBusy" @click="testRelayBrokers">
                      <PlugZap :size="15" />{{ relayTestBusy ? t("正在测试…") : t("测试中继连接") }}
                    </button>
                    <p v-if="relayTestError" class="note" role="alert">{{ relayTestError }}</p>
                    <div v-else-if="relayTestResult" class="relay-test" role="status">
                      <p class="note">{{ t("{total} 个中继中 {reachable} 个可达", { total: relayTestResult.total, reachable: relayTestResult.reachable }) }}</p>
                      <ul class="relay-test-list">
                        <li v-for="row in relayTestResult.results" :key="row.endpoint" :class="row.ok ? 'relay-test--ok' : 'relay-test--fail'">
                          <span class="relay-test-mark" aria-hidden="true">{{ row.ok ? "✓" : "✕" }}</span>
                          <span class="relay-test-endpoint">{{ row.endpoint }}</span>
                          <span class="note">{{ relayRowNote(row) }}</span>
                        </li>
                      </ul>
                    </div>
                  </div>
                  </div>
                </fieldset>
              </section>
              <section v-show="showSettingsCard('history')" id="settings-history" class="settings-section">
                <h2>{{ t("剪贴板历史") }}</h2>
                <label class="setting">
                  <span class="setting-name">{{ t("历史记录上限") }}</span>
                  <span class="setting-control"><input v-model.number="settings.history_max_entries" type="number" min="10" max="10000" step="1" /></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("历史保留天数（0 为不限）") }}</span>
                  <span class="setting-control"><input v-model.number="settings.history_max_age_days" type="number" min="0" max="36500" step="any" /></span>
                </label>
                <!-- The third bound on this same list, and the one bound on it
                     that another surface reads: the phone's web page lists this
                     many of the newest records, where the two rows above decide
                     what this machine keeps.  It was the last field of the
                     settings page's Phone Companion card — the service's own
                     controls left that card for the devices page, and what was
                     left was one number under a heading about a phone, with a
                     note under it saying where the rest had gone.  The hint is
                     what says which surface it bounds, since the card's heading
                     is this machine's history and this is the web page's. -->
                <label class="setting">
                  <span class="setting-name">{{ t("显示历史条数") }}</span>
                  <span class="setting-control"><input v-model.number="settings.web_history_limit" :aria-label="t('显示历史条数')" type="number" min="1" max="500" step="1" /><span class="note">{{ t("网页上显示最近多少条剪贴板记录（1–500）") }}</span></span>
                </label>
                <fieldset>
                  <legend>{{ t("敏感内容过滤") }}</legend>
                  <!-- The card ships with nothing ticked, and that is the whole
                       answer to "is this on?": saying it once, here, is
                       cheaper than six empty boxes the reader has to
                       interpret. -->
                  <p class="note setting-note">{{ t("勾选的类别在同步到其他设备前会被替换为 [FILTERED]。本机记录不受影响，也不会被改写；默认全部关闭。") }}</p>
                  <label v-for="[key, label, example] in filterCategories" :key="key" class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.filter_enabled_categories" type="checkbox" :value="key" /><span>{{ label }}<span class="note filter-example">{{ example }}</span></span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("来源应用过滤") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.app_filter_enabled" type="checkbox" :aria-label="t('启用应用过滤')" /><span>{{ t("启用应用过滤") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("过滤模式") }}</span>
                    <span class="setting-control"><select v-model="settings.app_filter_mode" :disabled="!settings.app_filter_enabled" :aria-label="t('应用过滤模式')"><option value="blacklist">{{ t("排除名单中的应用") }}</option><option value="whitelist">{{ t("仅允许名单中的应用") }}</option></select></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("进程名列表") }}</span>
                    <span class="setting-control"><textarea v-model="settings.app_filter_list" :disabled="!settings.app_filter_enabled" rows="4" :aria-label="t('应用过滤进程列表')" :aria-describedby="appFilterProblems.length ? 'app-filter-issues app-filter-what' : 'app-filter-what'" placeholder="chrome.exe&#10;secret*"></textarea></span>
                  </label>
                  <!-- What this field matches against, and that a mistake in
                       it is silent: fnmatch compares a process name to each
                       line, so a line that is a path matches nothing and the
                       rule simply never fires. -->
                  <p id="app-filter-what" class="note setting-note">{{ t("每行一条，按名称精确匹配（可用 * 和 ?）：Windows 与 Linux 写进程名（chrome.exe、firefox），macOS 写应用名（Google Chrome）。写错不会报错，只会匹配不上。") }}</p>
                  <p v-if="appFilterProblems.length" id="app-filter-issues" class="note setting-note app-filter-issue" role="status">{{ t("{count} 条带着目录，匹配不到任何进程：{list}。这里要的是进程名，不是路径。", { count: appFilterProblems.length, list: appFilterProblems.join("、") }) }}</p>
                </fieldset>
              </section>
              <!-- What a clip does on the way back out, and what the recorder
                   refuses to keep in the first place (1.3.0).  A card of its own
                   rather than rows added to 剪贴板历史: what is kept and what
                   happens when a clip is pasted are the two ends of the same
                   pipeline, and the history card is already the longest one on
                   the page.  The order is the pipeline's: paste first, then the
                   rules that decide what gets recorded. -->
              <section v-show="showSettingsCard('recording')" id="settings-recording" class="settings-section">
                <h2>{{ t("粘贴与录制") }}</h2>
                <fieldset>
                  <legend>{{ t("粘贴") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.auto_paste_enabled" type="checkbox" /><span>{{ t("自动粘贴到当前应用") }}</span></span>
                  </label>
                  <p class="note setting-note">{{ t("打开后，按快捷键取回历史会直接粘贴到当前应用；关掉则只复制到剪贴板。快速粘贴窗口不受这个开关影响。") }}</p>
                </fieldset>
                <fieldset>
                  <legend>{{ t("录制规则") }}</legend>
                  <!-- Each rule ships on, which the short notes say rather than
                       needing the reader to have read the config: the boxes
                       arrive ticked on a machine that never touched this card. -->
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.skip_password_manager_clips" type="checkbox" /><span>{{ t("密码管理器内容不记录") }}</span></span>
                  </label>
                  <p class="note setting-note">{{ t("密码管理器会给不想被记录的内容打上标记；打开后这类内容不进历史、也不同步。") }}</p>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.skip_prelaunch_clipboard" type="checkbox" /><span>{{ t("跳过启动前复制的内容") }}</span></span>
                  </label>
                  <p class="note setting-note">{{ t("每次启动时，剪贴板里已经存在的内容只跳过第一次，避免把上次遗留的内容带进历史。") }}</p>
                  <label class="setting">
                    <span class="setting-name">{{ t("非文件类单条大小上限") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.max_non_file_paste_mb" :aria-label="t('非文件类单条大小上限')"
                        type="number" :min="MIN_MAX_NON_FILE_PASTE_MB" :max="MAX_MAX_NON_FILE_PASTE_MB" step="0.1" />
                      <span class="note">{{ t("单位 MB，0 表示不限制；只计算文本、图片、链接这类非文件内容。") }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("来源应用排除") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.source_exclusion_patterns" rows="3"
                        :aria-label="t('来源应用排除列表')" :placeholder="t('每行一个子串')"></textarea>
                      <span class="note">{{ t("每行一个子串；来源应用名包含其中任意一段（不区分大小写）就不记录、也不同步。") }}</span>
                    </span>
                  </label>
                </fieldset>
              </section>
              <!-- The file limits (1.3.1): what a file clipboard may move, what
                   a paste asks about, and what an on-demand pull may cost over
                   the relay.  These are policy numbers rather than protocol ones
                   — the protocol's own hard ceiling is not a setting — so each
                   row says what its number counts. -->
              <section v-show="showSettingsCard('files')" id="settings-files" class="settings-section">
                <h2>{{ t("文件") }}</h2>
                <fieldset>
                  <legend>{{ t("大小上限") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("单个文件大小上限") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.max_file_mb" :aria-label="t('单个文件大小上限')"
                        type="number" min="0" :max="MAX_FILE_MB" step="1" />
                      <span class="note">{{ t("单位 MB；0 表示只受协议上限约束，默认 2048。超过这个大小的文件发送和接收都会被拒绝。") }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("粘贴前确认的文件大小") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.confirm_large_file_mb" :aria-label="t('粘贴前确认的文件大小')"
                        type="number" min="0" :max="MAX_FILE_MB" step="1" />
                      <span class="note">{{ t("单位 MB；粘贴的远程文件超过这个大小时会先确认，0 表示不确认。") }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("中继取回文件上限") }}</span>
                    <span class="setting-control">
                      <input v-model.number="settings.relay_paste_max_mb" :aria-label="t('中继取回文件上限')"
                        type="number" min="0" :max="MAX_FILE_MB" step="1" />
                      <span class="note">{{ t("单位 MB；不在同一网络时，按需取回超过这个大小的文件会被拒绝，0 表示不限制。") }}</span>
                    </span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("接收位置") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("文件接收目录") }}</span>
                    <span class="setting-control">
                      <input v-model="settings.file_receive_dir" :aria-label="t('文件接收目录')" :placeholder="t('留空使用默认的下载目录')" />
                      <button type="button" class="icon-button icon-button--inline" :aria-label="t('选择接收目录')"
                        :title="t('选择接收目录')" @click="browseReceiveDir"><FolderOpen :size="15" /></button>
                      <span class="note">{{ t("收到的文件保存在这里；留空使用默认的下载目录。") }}</span>
                    </span>
                  </label>
                </fieldset>
              </section>
              <!-- No notification card: this window raises no OS notification, so
                   the switches that used to live here would control nothing. The
                   rows the window does show — the status strip, the pairing card —
                   are the notifications. -->
              <section v-show="showSettingsCard('discovery')" id="settings-discovery" class="settings-section">
                <h2>{{ t("局域网发现") }}</h2>
                <label class="setting setting--check">
                  <span class="setting-control"><input type="checkbox" :aria-label="t('启用局域网发现')"
                    :checked="!!discoveryState?.enabled" :disabled="!discoveryAvailable || discoveryBusy"
                    @change="toggleDiscovery(($event.target as HTMLInputElement).checked)" /><span>{{ t("启用局域网发现") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input type="checkbox" :aria-label="t('隐藏本机')"
                    :checked="discoveryState ? !discoveryState.visible : false" :disabled="!discoveryAvailable || discoveryBusy"
                    @change="toggleVisibility(($event.target as HTMLInputElement).checked)" /><span>{{ t("隐藏本机（附近设备看不到此设备）") }}</span></span>
                </label>
                <p v-if="!discoveryAvailable" class="note setting-note">{{ t("同步引擎未运行，无法修改发现设置。") }}</p>
                <!-- Who may reach this device is the same question the two rows
                     above ask, so the answer sits with them.  Unlike them it is
                     an ordinary saved setting, applied to the live chat engine
                     the moment it is saved. -->
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.chat_open_to_all" type="checkbox" /><span>{{ t("任何人可直接发来消息和文件") }}</span></span>
                </label>
                <p class="note setting-note">{{ t("关掉以后，附近设备要先经过你同意，才能发消息和文件给你。") }}</p>
                <!-- The file-transfer page's own copy of that rule.  Two rows
                     rather than one because they answer two questions: chat is
                     a conversation someone wants, and an unsolicited file is
                     not the same act.  Paired devices are prompted either way. -->
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.file_open_to_all" type="checkbox" /><span>{{ t("任何人可直接向我发送文件") }}</span></span>
                </label>
                <p class="note setting-note">{{ t("关掉以后，别人发来的文件会先问你是否接收。聊天的收发不受这个开关影响。") }}</p>
              </section>
              <section v-show="showSettingsCard('advanced')" id="settings-advanced" class="settings-section">
                <h2>{{ t("网络与高级") }}</h2>
                <!-- The legacy window put this remark under its own title rather
                     than under the last row of the page: it is about every field
                     here, and it read as a footnote to whichever row happened to
                     be above it. -->
                <p class="note setting-note">{{ t("部分更改将在重启后生效。") }}</p>
                <!-- The legacy Advanced window split these fields into four
                     cards of its own.  They are the same four groups, named the
                     way it named them — the fields it held keep their own
                     places, and the ones that postdate it join by subject:
                     dedup, low-memory and capture retry are all things the
                     clipboard does, so they sit with the other clipboard rows. -->
                <fieldset>
                  <legend>{{ t("剪贴板与同步") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("同步发送间隔（秒）") }}</span>
                    <span class="setting-control"><input v-model.number="settings.sync_debounce" :aria-label="t('同步发送间隔（秒）')" type="number" min="0.05" max="10" step="0.05" /><span class="note">{{ t("连续复制时，两次同步之间至少隔这么久。调大可以减少发送次数（0.05–10 秒）。") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("检查剪贴板的间隔（秒）") }}</span>
                    <span class="setting-control"><input v-model.number="settings.clipboard_poll_interval" :aria-label="t('检查剪贴板的间隔（秒）')" type="number" min="0.1" max="60" step="0.1" /><span class="note">{{ t("多久检查一次剪贴板有没有新内容。调大会更省电，但新内容出现得慢（0.1–60 秒）。") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("去重方式") }}</span>
                    <span class="setting-control"><select v-model="settings.dedup_method" :aria-label="t('去重方式')"><option value="sha256">{{ t("SHA-256（内容哈希）") }}</option><option value="simple">{{ t("简单（快速）") }}</option></select></span>
                  </label>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.low_memory_mode" :aria-label="t('低内存模式（更少预览、更慢轮询）')" type="checkbox" /><span>{{ t("低内存模式（更少预览、更慢轮询）") }}</span></span>
                  </label>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.retry_capture_enabled" :aria-label="t('重试剪贴板捕获')" type="checkbox" /><span>{{ t("重试剪贴板捕获") }}</span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("文件传输") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("接收目录") }}</span>
                    <span class="setting-control"><input v-model="settings.file_receive_dir" class="setting-wide" :aria-label="t('接收目录')" maxlength="4096" :placeholder="t('留空使用默认')" /></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("传输超时（秒）") }}</span>
                    <span class="setting-control"><input v-model.number="settings.transfer_timeout" :aria-label="t('传输超时（秒）')" type="number" min="5" max="3600" step="1" /><span class="note">{{ t("文件传输超过此时间视为失效（5–3600 秒）") }}</span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("连接") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("TCP 端口") }}</span>
                    <span class="setting-control"><input v-model.number="settings.port" :aria-label="t('TCP 端口')" type="number" min="1024" max="65535" step="1"
                      data-settings-field="port" :aria-invalid="!!settingsErrors.port"
                      :aria-describedby="settingsErrors.port ? 'settings-error-port' : undefined" /><span class="note">{{ t("（1024–65535，需重启）") }}</span></span>
                    <span v-if="settingsErrors.port" id="settings-error-port" class="field-error" role="alert">{{ settingsErrors.port }}</span>
                  </label>
                  <!-- This hint describes the service-type row, not the card, and
                       it used to sit above the port row instead — where it read as
                       a description of the card and explained the wrong field. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("局域网发现标识") }}</span>
                    <span class="setting-control"><input v-model="settings.service_type" :aria-label="t('局域网发现标识')" maxlength="128" /><span class="note">{{ t("同一标识的设备才能互相发现。保持默认即可，改了要重启。") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("最大重连次数") }}</span>
                    <span class="setting-control"><input v-model.number="settings.max_reconnect_attempts" :aria-label="t('最大重连次数')" type="number" min="0" max="100" step="1" /></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("日志与通知") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("日志级别") }}</span>
                    <span class="setting-control"><select v-model="settings.log_level" :aria-label="t('日志级别')"><option v-for="level in LOG_LEVELS" :key="level" :value="level">{{ level }}</option></select></span>
                  </label>
                </fieldset>
                <!-- No group for this one.  The legacy window never offered an
                     editable data directory — it had a 显示数据目录 button in its
                     own About window — so the row is the shell's, and a legend
                     invented for it here would be a guess rather than a port. -->
                <label class="setting">
                  <span class="setting-name">{{ t("数据目录") }}</span>
                  <span class="setting-control"><input v-model="settings.data_dir" class="setting-wide" :aria-label="t('数据目录')" maxlength="4096" :placeholder="t('留空使用默认')"
                    data-settings-field="data_dir" :aria-invalid="!!settingsErrors.data_dir"
                    :aria-describedby="settingsErrors.data_dir ? 'settings-error-data-dir' : undefined" /><span class="note">{{ t("配置 / 历史 / 收藏的存储位置。重启后生效。") }}</span></span>
                  <span v-if="settingsErrors.data_dir" id="settings-error-data-dir" class="field-error" role="alert">{{ settingsErrors.data_dir }}</span>
                </label>
                <!-- A directory is something to pick: a mistyped absolute path is
                     only discovered when the next start fails to find its
                     history.  Whether it is writable is the sidecar's to find
                     out on save — this only removes the typing. -->
                <div class="setting-actions setting-actions--card">
                  <button type="button" @click="browseDataDir"><FolderOpen :size="16" />{{ t("选择目录…") }}</button>
                  <span class="note">{{ t("选好的目录会填进上面；能不能写入要等保存后侧车试过才知道。") }}</span>
                </div>
              </section>
              <section v-show="showSettingsCard('translation')" id="settings-translation" class="settings-section">
                <h2>{{ t("翻译") }}</h2>
                <label class="setting">
                  <span class="setting-name">{{ t("翻译服务地址") }}</span>
                  <span class="setting-control"><input v-model="settings.translate_url" class="setting-wide" type="url" maxlength="2048" placeholder="https://example.com/translate" /></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("翻译 API 密钥") }}</span>
                  <span class="setting-control"><input v-model="translationKey" class="setting-wide" type="password" autocomplete="new-password" maxlength="4096" :aria-label="t('翻译 API 密钥')" :disabled="translationKeyBusy" /><span class="note">{{ settings.translate_key_set ? t('密钥已设置') : t('未设置密钥') }}</span></span>
                </label>
                <div class="setting-actions">
                  <button type="button" :disabled="translationKeyBusy || !translationKey.trim()" @click="saveTranslationKey(false)"><Save :size="16" />{{ t("保存密钥") }}</button>
                  <button type="button" :disabled="translationKeyBusy || !settings.translate_key_set" @click="saveTranslationKey(true)"><Trash2 :size="16" />{{ t("清除密钥") }}</button>
                </div>
                <label class="setting">
                  <span class="setting-name">{{ t("源语言") }}</span>
                  <span class="setting-control"><select v-model="translationSource" :aria-label="t('翻译源语言')"><option value="auto">{{ t("自动检测") }}</option><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("目标语言") }}</span>
                  <span class="setting-control"><select v-model="translationTarget" :aria-label="t('翻译目标语言')"><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("翻译文本") }}</span>
                  <span class="setting-control"><textarea v-model="translationText" maxlength="5000" rows="3" :placeholder="t('输入需要翻译的文本')"></textarea></span>
                </label>
                <div class="setting-actions">
                  <button type="button" @click="translateText" :disabled="!translationText.trim() || translationBusy">{{ t("翻译") }}</button>
                </div>
                <p v-if="translationResult" class="translation-result setting-block">{{ translationResult }}</p>
              </section>
              <section v-show="showSettingsCard('security')" id="settings-security" class="settings-section">
                <h2>{{ t("安全") }}</h2>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.encryption_enabled" :aria-label="t('启用端到端加密')" type="checkbox" /><span>{{ t("启用端到端加密") }}</span></span>
                </label>
                <p class="note setting-note">{{ t("加密设备之间的剪贴板、文件与聊天流量。设置密码后，重启应用需要输入密码解锁。") }}</p>
                <label class="setting password-row">
                  <span class="setting-name">{{ settings.password_set ? t("更换加密密码") : t("设置加密密码") }}</span>
                  <!-- A password typed into a masked box with no way to look at it
                       is a password retyped three times: once here, once in the
                       confirmation, and once more after the mismatch. -->
                  <span class="setting-control"><input v-model="securityPassword" :aria-label="t('加密密码')"
                    :type="securityPasswordShown ? 'text' : 'password'" autocomplete="new-password" maxlength="200" :disabled="securityBusy" />
                    <button type="button" class="icon-button icon-button--inline" :disabled="securityBusy"
                      :aria-label="securityPasswordShown ? t('隐藏密码') : t('显示密码')"
                      :title="securityPasswordShown ? t('隐藏密码') : t('显示密码')"
                      @click="securityPasswordShown = !securityPasswordShown"><EyeOff v-if="securityPasswordShown" :size="16" /><Eye v-else :size="16" /></button></span>
                </label>
                <label class="setting password-row">
                  <span class="setting-name">{{ t("确认加密密码") }}</span>
                  <span class="setting-control"><input v-model="securityPasswordConfirm" :aria-label="t('确认加密密码')"
                    :type="securityPasswordShown ? 'text' : 'password'" autocomplete="new-password" maxlength="200" :disabled="securityBusy"
                    :aria-invalid="passwordMismatch" aria-describedby="password-match" />
                    <!-- Said as it is typed, not after a refused save: the
                         mismatch was reported on a separate line under the rules
                         list, which on a tall card is off screen when the second
                         box has focus. -->
                    <span v-if="securityPasswordConfirm" id="password-match" class="password-match" :class="{ 'password-match--ok': !passwordMismatch }">
                      <Check v-if="!passwordMismatch" :size="14" /><X v-else :size="14" />
                      {{ passwordMismatch ? t("两次输入不一致") : t("两次输入一致") }}
                    </span></span>
                </label>
                <ul v-if="securityPassword" class="password-rules setting-block">
                  <li v-for="[rule, met] in passwordRules" :key="rule" :class="{ met }">{{ rule }}</li>
                </ul>
                <p class="note setting-note">{{ t("密码会随“保存设置”一起提交，并同时用作设备配对的通道密钥；剪贴板历史的密钥在重启后更新。") }}</p>
                <p class="muted setting-block">{{ settings.password_set ? t("已设置加密密码") : t("未设置加密密码") }}</p>
                <!-- What the state above means for the files on this machine.  The sentence
                     further up describes *traffic* encryption, and the local files are not
                     mentioned anywhere else -- so "encryption is on" reasonably read as "my
                     files are encrypted", which without a password is not the case: the key
                     is derived from the device fingerprint, and that is stored in plaintext
                     in config.json beside the database. -->
                <p v-if="settings.password_set" class="note setting-note">{{ t("本机文件已加密：剪贴板历史与设备私钥用该密码存放。") }}</p>
                <!-- A warning, not a footnote.  This is the one sentence on the
                     page that says the files are readable by anyone who can open
                     them, and it used to be grey 12px type in a card of grey
                     12px type. -->
                <div v-else class="strip strip--warn" role="note">
                  <ShieldAlert :size="16" aria-hidden="true" />
                  <div><strong>{{ t("本机文件未加密") }}</strong>
                    <p class="note">{{ t("没有设置加密密码时，密钥由公开的设备指纹派生：任何能读取本机文件的程序或人都能解开剪贴板历史与设备私钥。") }}</p></div>
                </div>
                <div v-if="settings.password_set" class="setting-actions">
                  <button type="button" class="danger-outline" :disabled="securityBusy" @click="clearPasswordOpen = true"><Trash2 :size="16" />{{ t("清除加密密码") }}</button>
                </div>
                <h3>{{ t("危险区域") }}</h3>
                <p class="note setting-note">{{ t("清空历史只会删除剪贴板记录；恢复出厂设置还会删除收藏、配对和设备身份，并重新启动应用。两者都无法撤销。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="danger-outline" :disabled="historyBusy || !state.total" @click="clearHistoryOpen = true"><Eraser :size="16" />{{ t("清空全部历史") }}</button>
                  <button type="button" class="danger-outline" :disabled="securityBusy" @click="factoryResetOpen = true"><Trash2 :size="16" />{{ t("恢复出厂设置") }}</button>
                </div>
              </section>
              <section v-show="showSettingsCard('backup')" id="settings-backup" class="settings-section">
                <h2>{{ t("数据备份") }}</h2>
                <!-- While one of the six runs, the card says which and for how
                     long.  They are six buttons in one row, each of which looks
                     exactly like the others and does nothing visible for tens of
                     seconds -- see U23 -- so the answer to "is it working?" is a
                     line here, next to the buttons, rather than a spinner on one
                     of them that the reader has already looked away from. -->
                <p v-if="backupTask" class="note setting-block setting-block--card backup-progress" role="status">
                  <Loader2 :size="14" class="spinning" />{{ backupProgress }}
                </p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="primary" :disabled="!!backupTask" @click="createBackup"><Save :size="17" />{{ t("创建备份") }}</button>
                  <button type="button" :disabled="restoreBusy || !!backupTask" @click="restoreBackup">{{ t("选择备份恢复") }}</button>
                  <button type="button" :disabled="!!backupTask" @click="importHistory">{{ t("导入历史") }}</button>
                  <button type="button" :disabled="!!backupTask" @click="exportHistory('json')">{{ t("导出 JSON") }}</button>
                  <button type="button" :disabled="!!backupTask" @click="exportHistory('csv')">{{ t("导出 CSV") }}</button>
                  <button type="button" :disabled="!!backupTask" @click="exportHistory('markdown')">{{ t("导出 Markdown") }}</button>
                </div>
                <div class="setting-actions setting-actions--card">
                  <button type="button" @click="openDataFolder('data')"><FolderOpen :size="17" />{{ t("打开数据文件夹") }}</button>
                  <button type="button" @click="openDataFolder('backups')"><FolderOpen :size="17" />{{ t("打开备份文件夹") }}</button>
                </div>
                <p v-if="backupMessage" class="muted setting-block setting-block--card">{{ backupMessage }}</p>
                <!-- The list had no name and no way to ask it again: the refresh
                     button sat in the row of create/export actions, several
                     blocks above the list it refreshes. -->
                <div class="card-sub card-sub--row">
                  <h3>{{ t("可用备份") }}</h3>
                  <button type="button" class="icon-button" :title="t('刷新备份')" :aria-label="t('刷新备份')" @click="refreshBackupsNow"><RefreshCw :size="17" /></button>
                </div>
                <ul v-if="backups.length" class="setting-block backup-list"><li v-for="item in backups" :key="String(item.path)">
                  <span class="backup-name">{{ item.filename || item.path }}</span>
                  <span class="note">{{ backupMeta(item) }}</span>
                  <!-- One button per row would otherwise be one repeated label
                       to a screen reader; the name says which backup this one
                       restores. -->
                  <button type="button" :disabled="restoreBusy" :aria-label="t('恢复备份 {name}', { name: item.filename || item.path })" @click="restoreListedBackup(item)">{{ t("恢复备份") }}</button>
                </li></ul>
                <p v-else class="muted setting-block setting-block--card">{{ t("暂无备份") }}</p>
              </section>
              <section v-show="showSettingsCard('update')" id="settings-update" class="settings-section">
                <h2>{{ t("软件更新") }}</h2>
                <label class="setting setting--check">
                  <span class="setting-control"><input type="checkbox" v-model="autoUpdateCheck"
                    :disabled="autoUpdateCheckBusy" :aria-label="t('自动检查更新')"
                    @change="toggleAutoUpdateCheck(($event.target as HTMLInputElement).checked)" /><span>{{ t("自动检查更新") }}<span class="note setting-inline-note">{{ t("改完立即生效，不用按「保存设置」") }}</span></span></span>
                </label>
                <p class="note setting-note">{{ t("每约 6 小时检查一次 GitHub 是否有新版本，同时留意同一网络、同一平台的其他设备；发现更新的版本后会在「软件更新」里提示，点「下载并安装」才会下载、校验并安装。关闭后后台不再发起任何更新请求。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="!updateAvailableForUi || updateChecking
                      || updateState.phase === 'downloading' || updateState.phase === 'installing'"
                    @click="checkForUpdate">
                    <RefreshCw :size="17" :class="{ spinning: updateChecking }" />{{ updateChecking ? t('正在检查…') : t('立即检查更新') }}
                  </button>
                </div>
                <p v-if="updateAvailable" class="update-available setting-block setting-block--card" role="status">{{ t("发现新版本：") }}{{ updateLatest }}</p>
                <div v-if="updateAvailable && updateState.phase === 'idle'" class="setting-actions setting-actions--card">
                  <button v-if="updateInstallable" type="button" class="primary" @click="installUpdate">{{ t("下载并安装") }}</button>
                  <button v-else type="button" class="primary" @click="startUpdateDownload">{{ t("下载更新") }}</button>
                </div>
                <div v-if="updateState.phase === 'downloading'" class="update-progress setting-block" role="progressbar"
                  :aria-valuenow="Math.round(updateState.fraction * 100)" aria-valuemin="0" aria-valuemax="100">
                  <div class="update-progress__bar" :style="{ width: (updateState.fraction * 100) + '%' }"></div>
                  <span class="update-progress__label">{{ t("正在下载…") }} {{ Math.round(updateState.fraction * 100) }}%</span>
                </div>
                <p v-if="updateState.phase === 'installing'" class="update-available setting-block setting-block--card" role="status">{{ t("正在安装更新，完成后应用会自动重启。") }}</p>
                <template v-if="updateState.phase === 'ready'">
                  <p class="update-available setting-block setting-block--card" role="status">{{ t("新版本 {version} 已就绪", { version: updateState.version }) }}</p>
                  <!-- Which check settled it, and only when it was the weaker
                       one: an archive from a peer on this network is held to
                       the published release digest whenever the release server
                       can be reached, and to the sending device's own digest
                       when it cannot. The second case is what the peer path
                       exists for — refusing there would leave the feature
                       unable to do the one thing it is for — but a reader who
                       is about to restart into a new build is owed the
                       difference, and it is not visible anywhere else. -->
                  <p v-if="updateState.verified === 'peer_verified'" class="note setting-note">{{ t("已核对来源设备声明的摘要：本机连不上发布服务器，无法与官方摘要比对。") }}</p>
                  <p v-if="!updateInstallable || updateReadyManual" class="note setting-note">{{ t("请退出当前应用，然后用下方文件替换旧版本。剪贴板历史与设备仍保留在本机。") }}</p>
                  <p class="update-ready-path selectable setting-block">{{ updateState.path }}</p>
                  <div class="setting-actions setting-actions--card">
                    <!-- The archive is on disk and already checked — this
                         machine's own download, or the one a peer sent, held to
                         a digest before it was staged (the line above says which
                         one when it was not the release's). So the install reads
                         what is here rather than fetching the same release a
                         second time, which is the whole of what the peer path is
                         for: a machine that cannot reach the release endpoint.
                         What that install still cannot do is
                         replace this build with a file a person opens, and the
                         host says so instead of failing — the sentence above
                         and the folder beside it are the rest of that answer. -->
                    <button v-if="updateInstallable" type="button" class="primary" @click="installReadyUpdate">{{ t("立即安装") }}</button>
                    <button type="button" @click="openUpdateFolder">{{ t("打开所在文件夹") }}</button>
                  </div>
                </template>
                <!-- The raw string is usually an English fragment from the
                     updater or from the transport underneath it, and it was
                     being read as the second line of the message — the one line
                     a reader cannot act on.  What to do is said in their own
                     language; the fragment is folded, one click away, because it
                     is exactly what a report needs. -->
                <div v-if="updateState.phase === 'failed'" class="update-failed setting-block setting-block--card" role="alert">
                  <p>{{ t("更新失败") }}<span class="note"> · {{ t("失败于：{stage}", { stage: updateStageLabel }) }}</span></p>
                  <p class="note">{{ t("可以稍后重试；网络受限时检查代理设置。") }}</p>
                  <!-- Two ways out, because there are two different problems: a
                       check that cannot reach the server wants the release page
                       opened by hand, and a download that stopped halfway wants
                       another try.  The card used to say 更新失败 and stop. -->
                  <div class="setting-actions">
                    <button v-if="updateAttempt === 'download' && updateAvailable" type="button" @click="startUpdateDownload">{{ t("再试一次") }}</button>
                    <button type="button" @click="checkForUpdate">{{ t("重新检查") }}</button>
                    <button type="button" @click="openAboutLink('releases')"><ExternalLink :size="16" />{{ t("手动下载") }}</button>
                  </div>
                  <ErrorDetail v-if="updateState.error" :code="String(updateState.error)" />
                </div>
                <p v-if="updateInstallable" class="note setting-note">{{ t("下载并安装最新版本，完成后应用会自动重启。") }}</p>
                <p v-else class="note setting-note">{{ t("自动下载最新版本，下载完成后提示你手动替换旧版本。") }}</p>
              </section>
              <section v-show="showSettingsCard('diagnostics')" id="settings-diagnostics" class="settings-section">
                <h2>{{ t("诊断与维护") }}</h2>
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="!diagnosticsAvailable || diagnosticsBusy"
                    :title="diagnosticsUnavailable || undefined" @click="openDiagnostics"><Stethoscope :size="17" />{{ t("运行诊断") }}</button>
                  <button type="button" @click="openLogs"><FileUp :size="17" />{{ t("查看日志") }}</button>
                  <button type="button" @click="openAbout"><Info :size="17" />{{ t("关于") }}</button>
                  <button type="button" @click="restartOpen = true"><RotateCcw :size="17" />{{ t("重启应用") }}</button>
                </div>
                <p v-if="diagnosticsUnavailable" class="note setting-note">{{ diagnosticsUnavailable }}</p>
                <!-- The debugging half of the card: one row that says what this
                     machine will hand out, and two buttons that ask the other
                     machines for theirs.  The switch is an ordinary saved
                     setting; the asks are actions, and they are not gated on
                     the switch — what this machine shares has nothing to do with
                     whose logs it may read. -->
                <fieldset>
                  <legend>{{ t("设备日志") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.log_sharing" type="checkbox" /><span>{{ t("允许其他设备取走本机日志") }}</span></span>
                  </label>
                  <p class="note setting-note">{{ t("开着时，网络上任何设备（无需配对）向你索取日志都会得到一份，其中已隐去本机路径、令牌和配对密码；关着时一律回复无法提供。") }}</p>
                  <div class="setting-actions setting-actions--card">
                    <button type="button" :disabled="collectingLogs" @click="collectAllLogs"><FileDown :size="17" />{{ t("收集所有设备日志") }}</button>
                    <button type="button" @click="openLogsFolder"><FolderOpen :size="17" />{{ t("打开日志文件夹") }}</button>
                  </div>
                  <p class="note setting-note">{{ t("收来的日志存在「下载/ClipSync-logs」，文件名带设备名和收取时间。") }}</p>
                </fieldset>
              </section>
              <div class="settings-save">
                <button type="button" :disabled="settingsBusy || !settingsLoaded || !settingsDirty" :title="t('放弃未保存的更改，恢复为已保存的设置')" @click="revertSettings">{{ t("撤销") }}</button>
                <button class="primary" type="submit" :disabled="settingsBusy || !settingsLoaded || passwordBlocked || settingsBlocked"
                  :title="passwordBlockedReason || (settingsBlocked ? t('有字段还不能保存，改好之后才能保存') : undefined)"><Save :size="17" />{{ settingsSaved ? t('已保存') : t('保存设置') }}</button>
                <span v-if="passwordBlocked" class="settings-save-block" role="status">{{ passwordBlockedReason }}</span>
                <!-- A field the sidecar would refuse, said at the foot of the
                     page as well as under the box: the box is on one card, the
                     button is under all of them, and an invalid field on a card
                     the reader has walked away from would otherwise hold the
                     save with nothing on screen saying why. -->
                <span v-else-if="settingsBlocked" class="settings-save-block" role="status">
                  {{ t("有 {count} 个字段还不能保存", { count: Object.keys(settingsErrors).length }) }}
                  <button type="button" class="settings-save-jump" @click="focusFirstFieldError">{{ t("去修改") }}</button>
                </span>
                <span v-if="settingsSaveFailed" class="settings-save-status settings-save-status--failed" role="alert">{{ t("保存失败，更改还没有写入") }}</span>
                <span v-else-if="settingsDirty" class="settings-save-status" role="status">{{ t("有未保存的更改") }}</span>
                <span v-else-if="settingsSaved" class="settings-save-status" role="status">{{ settingsSavedTime ? t("所有更改都已保存 · {time}", { time: settingsSavedTime }) : t("所有更改都已保存") }}</span>
              </div>
            </form>
          </div>
        </section>
        <section v-else-if="tab === 'companion'" class="settings-panel companion-page">
            <section class="settings-section companion-panel">
              <div class="companion-head">
                <div>
                  <p class="note">{{ t("让手机通过浏览器访问这台电脑的剪贴板、文件和聊天。") }}</p>
                </div>
                <div class="companion-head-actions">
                  <span v-if="companion" class="companion-status" :class="{ 'companion-status--on': companion.running }">{{ companion.running ? t("正在运行") : companion.state === 'stopping' ? t("正在停止") : t("已停止") }}</span>
                  <button v-if="companion" type="button" class="companion-power" :class="companion.running ? 'danger' : 'primary'"
                    :disabled="companionBusy || companion.state === 'stopping' || (!companion.running && (!Number.isInteger(companionPort) || companionPort < 1 || companionPort > 65535))"
                    @click="configureCompanion(!companion.running)">
                    <Square v-if="companion.running" :size="16" /><Play v-else :size="16" />
                    {{ companion.running ? t("停止服务") : companion.state === 'stopping' ? t("正在停止") : t("启动服务") }}
                  </button>
                  <button type="button" class="icon-button" :title="t('刷新服务状态')" :aria-label="t('刷新服务状态')" :disabled="companionBusy" @click="refreshCompanion"><RefreshCw :size="17" /></button>
                </div>
              </div>
              <p v-if="!companion" class="note" role="status">{{ t("正在读取手机服务状态…") }}</p>
              <template v-if="companion">
                <div class="companion-grid">
                  <section class="companion-card">
                    <h3>{{ t("同一网络") }}</h3>
                    <p class="note">{{ t("手机和电脑在同一个 Wi-Fi 时，扫二维码就能直接打开。") }}</p>
                    <label class="setting">
                      <span class="setting-name">{{ t("手机服务端口") }}</span>
                      <span class="setting-control"><input v-model.number="companionPort" :aria-label="t('手机服务端口')" type="number" min="1" max="65535" step="1" :disabled="companionBusy" /></span>
                    </label>
                    <p v-if="companion.running && !companion.access_url" class="setting-block setting-block--card">{{ t("访问令牌已清除，任何能访问该端口的设备都可以直接连接") }}</p>
                    <label v-if="companion.running && (companion.access_url || companion.url)" class="setting">
                      <span class="setting-name">{{ t("手机访问地址") }}</span>
                      <span class="setting-control"><input :value="companion.access_url || companion.url || ''" class="setting-wide" style="width: 100%" readonly :aria-label="t('手机访问地址')" /></span>
                    </label>
                    <div class="companion-actions">
                      <button type="button" :disabled="companionBusy || !companion.running" @click="companionRotatePending = true"><RefreshCw :size="16" />{{ t("更换访问令牌") }}</button>
                      <button type="button" class="danger-outline" :disabled="companionBusy || !companion.running || !companion.access_url" @click="companionClearPending = true"><ShieldOff :size="16" />{{ t("清除访问令牌") }}</button>
                    </div>
                    <aside class="companion-qr">
                      <template v-if="companion.running">
                        <img v-if="qrImage" :src="qrImage" :alt="t('手机 Companion 二维码')" class="companion-qr__image" width="160" height="160" />
                        <p v-else class="note companion-qr__hint" role="status">{{ qrBusy ? t("正在生成二维码…") : (qrMessage || t("二维码不可用")) }}</p>
                        <button type="button" class="text-button companion-qr__zoom" :disabled="qrBusy" @click="openCompanionQr">{{ t("放大二维码") }}</button>
                      </template>
                      <p v-else class="note companion-qr__hint">{{ t("启动服务后显示二维码") }}</p>
                    </aside>
                  </section>
                  <section class="companion-card">
                    <h3>{{ t("远程访问") }}</h3>
                    <p class="note">{{ t("手机不在同一网络时，通过中继访问；流量端到端加密，但会经过第三方 MQTT 服务器。") }}</p>
                    <p v-if="companion.remote.enabled" class="companion-status" :class="{ 'companion-status--on': companion.remote.connected }">{{ companion.remote.connected ? t('中继已连接：{broker}', { broker: companion.remote.broker }) : t('中继未连接（会自动重试）') }}<template v-if="companion.remote.phone_online"> · {{ t("手机在线") }}</template><template v-if="companion.remote.last_seen"> · {{ t("最近访问：{time}", { time: remoteLastSeenLabel() }) }}</template></p>
                    <ErrorDetail v-if="companion.remote.error" :code="String(companion.remote.error)" />
                    <p v-if="companion.remote.enabled && companion.remote.companion_running === false" class="setting-block setting-block--card">{{ t("手机 Companion 已停止：远程二维码仍然可用，但手机只会看到「电脑端伴侣没有在运行」。先在上面点「启动服务」。") }}</p>
                    <label class="setting">
                      <span class="setting-name">{{ t("远程权限") }}</span>
                      <span class="setting-control">
                        <select v-model="companionRemoteMode" :aria-label="t('远程权限')" :disabled="companionRemoteBusy">
                          <option value="readonly">{{ t("只读（推荐）") }}</option>
                          <option value="full">{{ t("完整（允许推送、删除、发送聊天）") }}</option>
                        </select>
                      </span>
                    </label>
                    <div class="setting companion-brokers">
                      <span class="setting-name">{{ t("中继服务器") }}</span>
                      <div class="setting-control">
                        <ul class="broker-list" :aria-label="t('按顺序尝试的中继服务器')">
                          <li v-for="(broker, index) in companionRemoteBrokerList" :key="`${broker}-${index}`" class="broker-list__item">
                            <span class="broker-url" :title="broker">{{ broker }}</span>
                            <button type="button" class="icon-button icon-button--inline" :aria-label="t('移除中继 {broker}', { broker })" :title="t('移除中继')" :disabled="companionRemoteBusy" @click="removeCompanionRemoteBroker(index)"><X :size="14" /></button>
                          </li>
                        </ul>
                        <p v-if="!companionRemoteBrokerList.length" class="note">{{ t("还没有中继服务器，添加一个后再保存。") }}</p>
                        <div class="broker-add">
                          <input v-model="companionRemoteBrokerDraft" :aria-label="t('添加中继服务器地址')" :placeholder="(companion.remote.default_brokers || [])[0] || 'wss://broker.example.com:8084/mqtt'" :disabled="companionRemoteBusy" @keydown.enter.prevent="addCompanionRemoteBroker" />
                          <button type="button" :disabled="companionRemoteBusy || !companionRemoteBrokerDraft.trim()" @click="addCompanionRemoteBroker"><Plus :size="16" />{{ t("加入") }}</button>
                        </div>
                      </div>
                    </div>
                    <p class="note">{{ t("按顺序尝试，某个中继不可用时自动换下一个；列表顺序就是尝试顺序。公共中继免费且不保证可用，不稳定时可以换成自己的服务器。") }}</p>
                    <div class="companion-actions">
                      <button type="button" :disabled="companionRemoteBusy || !(companion.remote.default_brokers || []).length" @click="restoreRemoteBrokers">{{ t("恢复默认中继") }}</button>
                      <button type="button" class="danger-outline" :disabled="companionRemoteBusy || !companion.remote.enabled" @click="configureCompanionRemote(undefined, undefined, true)">{{ t("更换远程令牌") }}</button>
                      <button type="button" class="primary" :disabled="companionRemoteBusy" @click="configureCompanionRemote(companion.remote.enabled)">{{ t("保存设置") }}</button>
                    </div>
                    <aside class="companion-qr">
                      <div class="companion-qr__head">
                        <span class="companion-qr__label">{{ companion.remote.enabled ? t("远程访问已开启") : t("远程访问已关闭") }}</span>
                        <label class="switch" :class="{ 'switch--on': companion.remote.enabled, 'switch--busy': companionRemoteBusy }">
                          <input type="checkbox" class="switch-input" :checked="companion.remote.enabled" :disabled="companionRemoteBusy"
                            :aria-label="t('远程访问')" @change="toggleCompanionRemote" />
                          <span class="switch-track"><span class="switch-thumb"></span></span>
                        </label>
                      </div>
                      <template v-if="companion.remote.enabled">
                        <img v-if="companionRemoteQrImage" :src="companionRemoteQrImage" :alt="t('远程访问二维码')" class="companion-qr__image" width="160" height="160" />
                        <p v-else class="note companion-qr__hint" role="status">{{ companionRemoteQrBusy ? t("正在生成二维码…") : (companionRemoteQrMessage || t("二维码不可用")) }}</p>
                        <button type="button" class="text-button companion-qr__zoom" :disabled="companionRemoteQrBusy" @click="openCompanionRemoteQr">{{ t("放大二维码") }}</button>
                      </template>
                      <p v-else class="note companion-qr__hint">{{ t("开启远程访问后显示二维码") }}</p>
                    </aside>
                  </section>
                </div>
              </template>
            </section>
        </section>

        <section v-else-if="tab === 'internet'" class="device-list internet-page">
          <div class="internet-page-inner">
            <section class="settings-section internet-card">
              <!-- The title and the switch for the whole feature on one line,
                   and the switch is a switch: this is the one setting on the
                   card that turns everything else on it on, and as a checkbox
                   in a list of rows it read like one more preference among the
                   ones it governs.  Everything below needs the relay, so with
                   this off the card had nothing to say about why: its buttons
                   answered, its list stayed empty, and the only way to find out
                   was the settings page, which carries the same switch under
                   another name. -->
              <div class="internet-head card-head">
                <h2>{{ t("配对与连接") }}</h2>
                <div class="internet-head-actions card-head-actions">
                  <!-- Unconditional, and outside the relay-state line below:
                       that line only exists once the relay has reported a state,
                       and "the relay has not reported" is exactly when a reader
                       wants to ask it again. -->
                  <button type="button" class="icon-button" :title="t('刷新')" :aria-label="t('刷新互联网配对')" @click="refreshInternetPairingNow"><RefreshCw :size="17" /></button>
                  <label class="switch" :class="{ 'switch--on': internetPairingEnabled, 'switch--busy': internetSyncBusy }">
                    <input class="switch-input" type="checkbox" :aria-label="t('启用互联网同步')"
                      :checked="internetPairingEnabled" :disabled="internetSyncBusy"
                      @change="toggleInternetSync(($event.target as HTMLInputElement).checked, $event.target as HTMLInputElement)" />
                    <span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span>
                    <span class="switch-text">{{ internetPairingEnabled ? t("已开启") : t("已关闭") }}</span>
                  </label>
                </div>
              </div>
              <p class="note setting-note">{{ t("关闭后设备之间不再通过中继配对或同步，剪贴板也不离开局域网。") }}</p>
              <!-- This machine's own link to the relay, above the peers rather
                   than beside them.  Every other 在线 in this card is the
                   relay's view of *another* device, so when our link is down
                   the whole list reads 离线 and nothing on screen says whether
                   they are away or we are.  It is the first line of the card
                   for that reason. -->
              <p v-if="internetPairingEnabled && relayState" class="setting-block setting-block--card relay-state" :class="`relay-state--${relayState}`">
                <Activity :size="14" />{{ t("本机中继") }}：<strong>{{ relayStateLabel }}</strong>
                <span v-if="relayState === 'online' && internetPairing.relay_broker" class="relay-broker"
                  :title="t('当前中继：{broker}', { broker: internetPairing.relay_broker })">
                  <Link :size="12" />{{ internetPairing.relay_broker }}
                </span>
                <span v-if="relayState !== 'online' && relayState !== 'connecting'" class="note">{{ t("对方在线与否以中继连接为准；本机中继不可用时，所有设备都会显示为离线。") }}</span>
                <!-- The only way out of a page that says 离线 everywhere: the test
                     that can tell a broker that is down from a network that
                     blocks it lives on the settings page's advanced card.  One
                     button, here, is the difference between knowing and guessing.
                     See U126. -->
                <button v-if="relayState !== 'online' && relayState !== 'connecting'" type="button" class="relay-state__why"
                  :disabled="relayTestBusy" @click="openRelayDiagnostics">
                  <Stethoscope :size="14" />{{ relayTestBusy ? t("正在测试…") : t("查看原因") }}
                </button>
              </p>
              <!-- One act, two machines, and the card drew it as two unrelated
                   rows — a button pair, then a labelled text box — so a reader
                   had to work out that the code they generate is the code the
                   other side types.  The two halves sit side by side now, each
                   saying what it is for in the direction it is used. -->
              <div class="pair-grid">
                <section class="pair-half">
                  <h3 class="pair-half-title"><Link :size="15" />{{ t("本机配对码") }}</h3>
                  <p class="note">{{ t("把这串码给对方，让它在自己的设备上输入。") }}</p>
                  <p v-if="internetPairing.generated_code" class="pair-code">
                    <code>{{ internetPairing.generated_code }}</code>
                    <button type="button" class="icon-button" :title="t('复制')" :aria-label="t('复制配对码')"
                      @click="copyText(String(internetPairing.generated_code))"><Copy :size="16" /></button>
                  </p>
                  <p v-else class="pair-code pair-code--empty">{{ t("尚未生成") }}</p>
                  <!-- The code has a deadline and used to vanish on the poll that
                       noticed it, with nothing said: this is that deadline, read
                       from the sidecar's own `pending_ttl` so a sleeping laptop
                       does not make the number wrong. -->
                  <p v-if="internetPairing.generated_code && generatedCodeSecondsLeft > 0" class="note setting-note">
                    {{ t("配对码约 {seconds} 秒后过期，过期后需要重新生成", { seconds: generatedCodeSecondsLeft }) }}</p>
                  <p v-else-if="internetPairing.generated_code" class="note setting-note pairing-message--failed">
                    {{ t("配对码已过期，请重新生成") }}</p>
                  <div class="setting-actions">
                    <button type="button" :disabled="!internetPairingEnabled || internetSyncBusy" @click="generateInternetPairing">
                      {{ internetPairing.generated_code ? t("重新生成") : t("生成配对码") }}
                    </button>
                  </div>
                </section>
                <section class="pair-half">
                  <h3 class="pair-half-title"><Globe :size="15" />{{ t("输入对方配对码") }}</h3>
                  <p class="note">{{ t("对方生成了一串码，输在这里提交，两台设备即通过中继配对。") }}</p>
                  <!-- The field shows the code in the shape this machine generates
                       one — upper case, four at a time — and takes a paste in any
                       shape of it.  A plain text box let a reader type the code
                       they were shown, in the case they were shown it, and watch
                       the box render it differently from the screen they were
                       reading it off; see `lib/pairing-code.ts`. -->
                  <input ref="internetPairingInputEl" class="pair-input"
                    :value="internetPairingCode" maxlength="14" autocomplete="off" spellcheck="false"
                    autocapitalize="characters" :placeholder="t('XXXX-XXXX-XXXX')" :disabled="!internetPairingEnabled"
                    :aria-label="t('输入对方配对码')" @input="setInternetPairingCode($event)" />
                  <div class="setting-actions">
                    <button type="button" @click="enterInternetPairing" :disabled="!internetPairingComplete || !internetPairingEnabled">{{ t("提交配对码") }}</button>
                  </div>
                  <!-- A refusal rather than a note: it names what to do about it,
                       and it stays beside the box that caused it. -->
                  <p v-if="internetPairingMessage" class="setting-block pairing-message"
                    :class="{ 'pairing-message--failed': internetPairingFailed }" role="status">{{ internetPairingMessage }}</p>
                </section>
              </div>
              <!-- Codes entered here whose partner has not answered yet.  The
                   status keeps them out of the peer list on purpose — a 4-char
                   tag cannot be reached, sent to or renamed, and listing it as a
                   device showed a phantom nobody could remove — but that is not
                   a reason to show nothing, which is what the card did: between
                   submitting a code and the partner's reply, the page had no
                   answer to "did it connect?" at all.  It is shown as what it
                   is, next to the peers rather than among them. -->
              <ul v-if="internetPairingWaiting.length" class="setting-block peer-list peer-list--waiting" role="status">
                <li v-for="row in internetPairingWaiting" :key="row.peer_id">
                  <div class="peer-line">
                    <span v-if="netpairWaitFailed[String(row.peer_id)]" class="pairing-wait pairing-message--failed">
                      <ShieldOff :size="14" />{{ row.name ? t("{name} 没有确认配对码，本次配对已超时", { name: row.name }) : t("对方没有确认配对码，本次配对已超时") }}
                    </span>
                    <span v-else class="pairing-wait" :title="row.since ? t('提交于 {when}', { when: dateTime(row.since) }) : ''">
                      <Clock :size="14" />{{ row.name ? t("等待 {name} 确认…", { name: row.name }) : t("等待对方确认…") }}
                    </span>
                    <button type="button" class="text-button" @click="unpairInternet(row.peer_id)">{{ t("取消") }}</button>
                    <!-- The wait offered one thing to do, and it was to give up.
                         A code that was mistyped or that the other side never saw
                         reads exactly like one still being answered, so the way
                         back to the box is here — nothing is resent, because only
                         the other side knows whether it ever saw this code. -->
                    <button v-if="netpairWaitFailed[String(row.peer_id)]" type="button" class="text-button"
                      @click="focusInternetPairingCode">{{ t("重新输入") }}</button>
                  </div>
                  <p v-if="netpairWaitFailed[String(row.peer_id)]" class="note">{{ t("配对码已提交，但对方没有确认；请重新输入配对码，或让对方重新生成。") }}</p>
                  <p v-else-if="waitSecondsLeft(row) > 0" class="note">
                    {{ t("若对方未确认，本次等待约 {seconds} 秒后结束。", { seconds: waitSecondsLeft(row) }) }}</p>
                  <p v-else class="note">{{ t("配对码已提交，对方通过中继确认后即会出现在上方设备列表中。") }}</p>
                </li>
              </ul>
              <!-- The paired devices, drawn as the rows on the other tab are —
                   an icon, a name, the route chips, then the one action that
                   belongs to the row.  They were a column of text buttons with
                   a bare 在线 beside them, which is the shape the device list
                   deliberately stopped using: a paired device is a device, and
                   it reads as one in both places now. -->
              <div class="internet-peers-head card-sub card-sub--row">
                <h3>{{ t("互联网配对的设备") }}</h3>
                <!-- Drawn only when there is something to say.  A permanent
                     待投递消息：0 above rows that already say nothing was there
                     in every state of the card, and its zero was the one the
                     store's own ``shown()`` refuses to draw: 0 before any
                     ledger has answered and 0 when nothing is queued are not
                     the same fact, and the line could not tell them apart.  The
                     per-row 待补发 chips carry the detail, and the poll that
                     keeps those live keeps this in step too — which is what the
                     separate 刷新投递状态 button did, over the same read as the
                     card's own 刷新, so it went with the unconditional line. -->
                <span v-if="delivery.total > 0" class="note">{{ t("待投递消息：") }}{{ delivery.total }}</span>
              </div>
              <!-- With the switch off the rows are still drawn, and their 在线
                   chips are the last thing the relay said before it stopped —
                   which is not a claim about now.  The list says so instead of
                   pretending either way. -->
              <p v-if="!internetPairingEnabled && internetPairing.peers.length" class="note setting-note">
                {{ t("互联网同步已关闭：下面这些设备现在都联系不上，在线状态是关闭前最后一次读取的结果；打开开关后恢复。") }}</p>
              <ul class="peer-list peer-list--devices" :class="{ 'peer-list--stale': !internetPairingEnabled }">
                <li v-for="peer in internetPairing.peers" :key="peer.peer_id" class="internet-peer">
                  <Monitor :size="20" class="device-icon" />
                  <div class="device-identity">
                    <span class="device-name">{{ peer.alias || peer.name || peer.peer_id }}</span>
                    <!-- The id is the one string a bug report needs and the one
                         thing on this row that could not be taken away: it is
                         four characters of tag inside a 32-character hex key,
                         which is unreadable to retype and impossible to select
                         with the row's own copy-free text. -->
                    <span class="note peer-id">{{ peer.peer_id }}
                      <button type="button" class="icon-button icon-button--inline" :title="t('复制设备 ID')"
                        :aria-label="t('复制设备 ID')" @click="copyText(String(peer.peer_id))"><Copy :size="12" /></button>
                    </span>
                  </div>
                  <span class="device-channels">
                    <span class="channel channel--paired"><ShieldCheck :size="12" />{{ t("已配对") }}</span>
                    <span class="channel" :class="`channel--${peer.online ? 'online' : 'offline'}`"
                      :title="peer.last_seen ? t('最后在线 {when}', { when: dateTime(peer.last_seen) }) : t('尚未连接过')">
                      <Globe :size="12" />{{ t("互联网") }}·{{ peer.online ? t("在线") : t("离线") }}
                    </span>
                    <!-- 离线 alone left "away since breakfast" and "never seen"
                         looking the same, though the relay reports when it last
                         heard from the peer. -->
                    <span v-if="peer.last_seen && !peer.online" class="channel channel--offline">
                      <Clock :size="12" />{{ t("最后在线 {when}", { when: dateTime(peer.last_seen) }) }}
                    </span>
                    <span v-if="delivery.pending(String(peer.peer_id)) > 0" class="channel channel--pending"
                      :title="t('对方离线时内容暂存，上线后自动补发')">{{ t("待补发 {count}", { count: delivery.pending(String(peer.peer_id)) }) }}</span>
                    <!-- How the newest send to this device ended, a chip like
                         the rest rather than a line of its own: it is one more
                         fact about the route, and it is hidden until the ledger
                         has answered so a backend that cannot report never
                         reads as "nothing sent". -->
                    <span v-if="delivery.shown(String(peer.peer_id)) && deliveryGlyph(peer.peer_id)"
                      class="channel delivery-result" :class="`delivery-result--${delivery.lastStatus(String(peer.peer_id))}`">
                      <component :is="deliveryGlyphs[deliveryGlyph(peer.peer_id)!]" :size="12" />{{ deliveryText(peer.peer_id) }}
                    </span>
                  </span>
                  <div class="row-actions">
                    <button type="button" class="icon-button" :title="t('打开聊天')" :aria-label="t('打开聊天')" @click="chatWith({ id: String(peer.peer_id), name: peer.alias || peer.name || String(peer.peer_id) } as Device)"><MessageCircle :size="17" /></button>
                    <button type="button" class="icon-button" :title="t('重命名')" :aria-label="t('重命名')" @click="renameInternet(peer)"><Pencil :size="17" /></button>
                    <button type="button" class="icon-button" :title="t('解除互联网配对')" :aria-label="t('解除互联网配对')" @click="unpairInternet(peer.peer_id)"><CloudOff :size="17" /></button>
                  </div>
                </li>
              </ul>
              <p v-if="internetPairingEnabled && !internetPairing.peers.length" class="note setting-note">{{ t("还没有互联网配对的设备。上面两个方向任选一个，两台设备就能隔着网络配对。") }}</p>
            </section>
          </div>
        </section>

        <section v-else-if="tab === 'devices'" class="device-list" :aria-busy="state.refreshing || state.pending">

          <!-- Three questions, one at a time.  Which machines this one knows,
               how a machine that is not on this network becomes one, and the
               phone service that lets a phone in: as a single scroll the second
               and third sat behind the whole of the first, so the reader who
               came for a pairing code had to pass a device list twice over. -->
            <div class="device-section-head">
              <div class="device-section-title">
                <h2>{{ t("局域网设备") }}</h2>
                <p class="note">{{ t("同一网络里的设备；互联网配对的设备在「互联网同步」页管理。") }}</p>
              </div>
            <div class="device-toolbar">
              <!-- The one control in this row that acts on nothing in the list: it
                   pushes text to this machine's clipboard and on to every paired
                   device, which is a sentence an icon cannot say.  The legacy panel
                   had it as a labelled button for the same reason.

                   A disabled button that never says why is the same defect as a
                   hidden one: without the capability the button is grey for good,
                   so the line beside it says which capability is missing.  It is
                   the store's own report, not an inference from the engine's
                   state — this page has no engine state of its own to read.

                   It names two controls rather than one, because the second is in
                   the rows below rather than in this row: the send-URL button on
                   every paired device.  That button reads a different capability
                   from this one, but both come from the sidecar's one condition,
                   so the sentence is written once, here, and the row button is
                   described by it — a title on a disabled control is not read
                   everywhere, and the row is where the reader is looking. -->
              <span v-if="devicesEngineStopped" id="devices-engine-note" class="note">{{ t("同步引擎未运行，推送文本与发送网址不可用。") }}</span>
              <button :aria-label="t('推送文本')" :title="t('推送文本到剪贴板并同步')" :disabled="busy || !pushTextAvailable" @click="openPushText"><SendHorizontal :size="17" />{{ t("推送文本") }}</button>
              <button class="icon-button" :aria-label="t('查看证书指纹')" :title="t('证书指纹')" :disabled="busy" @click="showCertificates"><Fingerprint :size="18" /></button>
              <button class="icon-button" :aria-label="t('刷新设备')" :title="t('刷新设备')" :disabled="busy || devicesRefreshing" @click="refreshDevices"><RefreshCw :size="18" :class="{ spinning: state.refreshing || devicesRefreshing }" /></button>
            </div>
            </div>
            <!-- No row for this machine.  There was one, at the head of the
                 list, holding the two facts a peer needs in order to pair with
                 *this* one — the name it answers to and the device id the
                 sidecar knows it by — and it read as a device among devices:
                 the same row shape, the same chips, sorted into the same list,
                 when it is the machine the reader is sitting at and not
                 something on the other end of a link.  Everything it said is on
                 the overview's own card, which is where this machine's facts
                 belong and where its name is edited, and the id there is
                 selectable text rather than a copy button away.  The count in
                 the header follows the rows that are left: see `devices`. -->
            <div v-if="!lanDevices.length" class="empty empty--page">
              <Monitor :size="36" />
              <h2>{{ state.refreshing ? t('正在读取设备') : t('暂无设备') }}</h2>
              <!-- What the reader does next.  A page that says 暂无设备 and stops
                   leaves the one question it raises — how does a device get
                   here? — to be answered by the two other tabs, which is where
                   the two ways in already live. -->
              <p class="note">{{ t("同一网络里的设备打开 ClipSync 后会自己出现在这里；不在同一网络的设备，在“互联网同步（中继）”里用配对码添加。") }}</p>
            </div>
            <article v-for="device in lanDevices" :key="device.id" class="device-row"
              @contextmenu.prevent="deviceMenu($event, device)">
              <Monitor :size="25" class="device-icon" />
              <!-- The heading is the name this list calls the device — the one
                   the user gave it, when there is one; the sub-line keeps the
                   name the peer goes by, so nothing is lost to it (see
                   `deviceLabel` and `deviceSubtitle`). -->
              <div class="device-identity"><span class="device-name">{{ deviceLabel(device) }}</span>
                <!-- Naming a device is the first thing anyone does after pairing,
                     and it lived only in the context menu: no pencil, no visible
                     entry, and a touch screen has no second button. -->
                <button v-if="device.paired || relayPairing(device).paired" class="icon-button icon-button--sm rename-device"
                  :aria-label="t('重命名 {name}', { name: deviceLabel(device) })" :title="t('重命名')" :disabled="busy"
                  @click.stop="renameDeviceRow(device)"><Pencil :size="14" /></button>
                <span class="note">{{ deviceSubtitle(device) }}</span></div>
              <!-- A device paired by internet code gets its own set, because
                   almost every button in the other one names something a code
                   pairing does not have: no certificate to revoke, no address
                   to dial, no update to offer (a peer with no local sighting
                   advertises no version).  What the relay *can* carry is a
                   conversation, a URL and a reachability test, and 打开聊天 is
                   a button here rather than only a menu entry because a device
                   that cannot be talked to from its own row is the thing this
                   list was missing.  It is drawn on the same rule the local row
                   uses — whether the device answers — so a relay that is down
                   leaves the entry to the menu, which says why. -->
              <div v-if="device.relay" class="row-actions">
                <!-- Talk to this device is the one entry here that is a
                     *conversation* rather than a piece of maintenance, so it is
                     the one written out.  Ten same-sized grey icons in a line
                     gave nothing to read and nothing to aim at; the two the row
                     keeps as words are the two a reader comes to a device row
                     for.  See U111. -->
                <button v-if="chatReachable(device)" :disabled="busy || !!chatConnectingId" @click="chatWith(device)"><MessageCircle :size="16" :class="{ spinning: chatConnectingId === device.id }" />{{ t("聊天") }}</button>
                <!-- The probes that only report live on the row's menu now, and
                     this button is how that menu is reached without a right
                     click — which a touch screen never had. -->
                <button class="icon-button" :aria-label="t('更多操作')" :title="t('更多操作')" :disabled="busy" @click="openDeviceMenuHere($event, device)"><MoreHorizontal :size="18" /></button>
                <!-- The cloud, not the generic broken link the local row's
                     撤销信任 wears: this entry ends the pairing that runs over the
                     relay, and the two icons sat side by side on a row holding
                     both pairings with nothing to tell them apart. -->
                <button class="icon-button" :aria-label="t('中继：解除配对')" :title="t('中继：解除配对')" :disabled="busy" @click="relayUnpairDevice = device"><CloudOff :size="18" /></button>
                <!-- Removal, which the sidecar implements for exactly this row
                     too: it archives the device and ends both routes, where the
                     entry above leaves no archive to restore from and forgets
                     the alias the user chose.  Without it, a device reachable
                     only over the internet could not be taken out of the list
                     at all. -->
                <button class="icon-button icon-button--danger" :aria-label="t('全部：移除设备')" :title="t('全部：移除设备')" :disabled="busy" @click="forgetDevice = device"><Trash2 :size="18" /></button>
              </div>
              <div v-else class="row-actions">
                <button v-if="device.paired" class="icon-button" :aria-label="t('局域网：撤销信任')" :title="t('局域网：撤销信任')" :disabled="busy" @click="revokeDevice = device"><ShieldOff :size="18" /></button>
                <!-- Drawn on whether a dial can be placed, not on the connection
                     there happens to be — see `canDial`. -->
                <!-- A pairing starts with a call to the device, so a device
                     that cannot be called cannot be paired with — and the one
                     button in this row that explains that is the button itself,
                     dimmed.  It says which of the two it is: a device that is
                     merely not answering right now, or one the engine is
                     holding out of reach. -->
                <button v-else-if="!pairingPending(device)" :disabled="busy || !canDial(device) || dialingId === device.id"
                  :title="dialingId === device.id
                    ? t('正在连接 {name}…', { name: deviceLabel(device) })
                    : canDial(device) ? t('与该设备配对') : t('该设备当前不可达，无法发起配对；等它上线或检查两端的网络连接')"
                  @click="dialDevice(device, () => store.startPairing(device))"><Link :size="17" :class="{ spinning: dialingId === device.id }" />{{ t("配对") }}</button>
                <!-- Written out for the same reason the relay row's chat is: the
                     row is a list of devices, and the two things one does with a
                     device — pick up the link, type at it — were two of ten
                     identical grey squares.  See U111. -->
                <button v-if="device.paired && device.connection_state !== 'online'" :title="dialingId === device.id ? t('正在连接 {name}…', { name: deviceLabel(device) }) : t('连接设备')" :disabled="busy || dialingId === device.id" @click="dialDevice(device, () => store.connect(device))"><Plug :size="16" :class="{ spinning: dialingId === device.id }" />{{ t("连接设备") }}</button>
                <button v-if="device.connection_state === 'online'" :disabled="busy" @click="store.disconnect(device)"><PlugZap :size="16" />{{ t("断开连接") }}</button>
                <!-- The conversation, offered on whether the device answers
                     rather than on which pairing it holds.  It used to be drawn
                     only for a device paired by code and *not* on this network,
                     which left the commonest row in the list — paired and
                     online, the device on the desk next to this one — with no
                     way to open a chat from it at all, while the chat page
                     offered the same device a conversation.  A device that is
                     merely discovered is offered it too, because talking to a
                     device without pairing is what the chat page does; one
                     that is not reachable is left to the menu, which says why
                     (a dimmed icon in a row of live ones says nothing). -->
                <button v-if="chatReachable(device)" :disabled="busy || !!chatConnectingId" @click="chatWith(device)"><MessageCircle :size="16" :class="{ spinning: chatConnectingId === device.id }" />{{ t("聊天") }}</button>
                <button class="icon-button" :aria-label="t('更多操作')" :title="t('更多操作')" :disabled="busy" @click="openDeviceMenuHere($event, device)"><MoreHorizontal :size="18" /></button>
                <button v-if="relayPairing(device).paired" class="icon-button" :aria-label="t('中继：解除配对')" :title="t('中继：解除配对')" :disabled="busy" @click="relayUnpairDevice = device"><CloudOff :size="18" /></button>
                <!-- Last, and set apart from the four above it.  It used to sit
                     between 获取更新 and the row's other repair entries — same
                     grey, same size, same distance — on the reasoning that every
                     entry here is equally a button.  They are not: this one
                     archives the device, and the one beside it downloaded a file.
                     See U112. -->
                <button class="icon-button icon-button--danger" :aria-label="t('全部：移除设备')" :title="t('全部：移除设备')" :disabled="busy" @click="forgetDevice = device"><Trash2 :size="18" /></button>
              </div>
              <!-- A row of its own, made cheap: the class trims the padding that made a
                   one-line sentence cost 50px.  Placing it on the chips' line instead
                   rendered it through the sync switch. -->
              <p v-if="probeResults[device.id]" class="note device-full device-probe" role="status">{{ t("连接测试：") }}{{ probeLabel(probeResults[device.id]) }}</p>
              <!-- The row's two update buttons, folded into one line that says
                   what is between the two machines and then offers the one thing
                   there is to do about it.
                   They were two identical grey icons pointing opposite ways —
                   an up arrow and a down arrow, side by side, on the same row,
                   telling the reader nothing about which one applied to whom —
                   and they were the two entries that made the row ten wide.  The
                   direction is now in the sentence, where it cannot be misread,
                   and the version chip above is what the sentence is read off.
                   A row that advertises no version gets neither, as before: an
                   older peer sends none, and "unknown" is not "out of date". -->
              <p v-if="deviceVersionNote(device)" class="note device-full device-version-note" role="status">
                <ArrowUpDown :size="14" class="device-version-note__icon" />
                <span>{{ deviceVersionNote(device) }}</span>
                <!-- Only when there is something to send.  The sentence says so
                     when there is not — a dimmed button whose reason lives in a
                     tooltip is the thing this audit keeps finding. -->
                <button v-if="device.update_available && device.update_cached"
                  :disabled="!!updateBusyId || !canDial(device)"
                  :title="t('把本机的安装包发送给该设备')" @click="offerDeviceUpdate(device)">
                  <FileUp :size="15" :class="{ spinning: updateBusyId === device.id }" />{{ t("发送更新") }}
                </button>
                <button v-else-if="device.update_fetchable" :disabled="!!fetchBusyId || !canDial(device)"
                  :title="t('从该设备获取新版本安装包并安装')" @click="fetchDeviceUpdate(device)">
                  <Download :size="15" :class="{ spinning: fetchBusyId === device.id }" />{{ t("获取更新") }}
                </button>
              </p>
              <p v-if="updateNotes[device.id]" class="note device-full" role="status">{{ updateNotes[device.id] }}</p>
              <p v-if="logNotes[device.id]" class="note device-full" role="status">{{ logNotes[device.id] }}</p>
              <!-- The chips under the name.  One word for the device's state —
                   正在同步 / 已连接 / 已配对 / 离线 — and then one chip per fact the
                   word cannot carry.
                   The state used to be two chips, a pairing one and a local-route
                   one, so a device that was paired *and* connected read as two
                   words the reader had to combine, and 已配对 beside 离线 named a
                   state without saying whether anything was flowing.  Which route
                   it is reachable on, and whether a retry is in flight, moved into
                   the state chip's tooltip: 本地·在线 and 重连中 2/5 were never
                   things a reader scanned the list for.
                   The internet chip stays its own: the relay's view of the same
                   device is a separate link's fact, and a row can hold both. -->
              <span class="device-channels">
                <!-- A handshake on its way is not one of the four states: it is
                     an answer the user owes somebody, so it is shown beside the
                     state rather than as one. -->
                <span v-if="pairingInFlight(device)" class="channel channel--connecting">
                  <ShieldCheck :size="12" />{{ pairingLabel(device) }}
                </span>
                <!-- A request that ended without pairing, kept for a minute so
                     the card's disappearance is not the only word on it.  The
                     outcome arrives as `pairing.resolved`, not as a row status:
                     the row's own terminal status can be months old after an
                     unpair, and would then label every unpaired device. -->
                <span v-else-if="pairingEndedLabel(device)" class="channel channel--offline" role="status">
                  <ShieldOff :size="12" />{{ pairingEndedLabel(device) }}
                </span>
                <span class="channel" :class="`channel--${deviceStatusState(device)}`"
                  :title="deviceStatusTitle(device)">
                  <component :is="deviceStatusIcon(device)" :size="12" />{{ deviceStatusLabel(device) }}
                </span>
                <!-- Only on a row that holds an internet pairing.  Every row
                     used to carry 互联网·未配对, which put 未配对 on one row twice
                     with two meanings — the chip beside it is the *local*
                     pairing — and told the reader nothing to act on: no
                     internet pairing is the ordinary state of a device this
                     machine knows on its own network, and the card above is
                     where one is made.  Paired, the chip stays: 在线/离线 is
                     the half of a device's reach no other line here reports. -->
                <span v-if="relayPairing(device).paired" class="channel" :class="`channel--${relayChannel(device)}`"
                  :title="relayLastSeen(device) || t('互联网配对')">
                  <Globe :size="12" />{{ t("互联网") }}·{{ relayChannelLabel(device) }}
                </span>
                <span v-if="delivery.pending(device.id) > 0" class="channel channel--pending"
                  :title="t('对方离线时内容暂存，上线后自动补发')">{{ t("待补发 {count}", { count: delivery.pending(device.id) }) }}</span>
                <!-- Which machine this is, of the two facts a sighting carries
                     that nothing else on the row reported: the platform and the
                     architecture.  It is what the update entries beside it are
                     decided on — the sidecar refuses a send across platforms —
                     and what 对方与本机不是同一个平台 leaves the reader to guess at,
                     on a row that until now named no platform anywhere.  The
                     architecture rides in the tooltip rather than beside it:
                     Windows/amd64 and Windows/arm64 are a distinction the
                     installer cares about and the reader rarely does.

                     Nothing is drawn when the device advertises nothing, which
                     is what a peer with no local sighting does. -->
                <span v-if="device.platform" class="channel"
                  :title="device.arch ? t('{platform} · {arch}', { platform: platformLabel(device.platform), arch: device.arch }) : t('对方平台')">
                  <Laptop :size="12" />{{ platformLabel(device.platform) }}
                </span>
              </span>
              <!-- Whether this machine's clipboard goes to this device.
                   On the row rather than in settings, because the question
                   ("which of these gets what I copy?") occurs to a user while
                   they are looking at the list of their devices, and a switch
                   they cannot find is a switch that does not exist.  Off stops
                   both routes -- the local link and the relay -- so it means what
                   it appears to mean. -->
              <label v-if="device.paired && !device.relay" class="device-sync"
                :title="t('关闭后，本机复制的内容不会发送到这台设备（局域网与中继都不发）。')">
                <input type="checkbox" :checked="device.syncs_to !== false"
                  :disabled="busy" :aria-label="t('向此设备同步剪贴板')"
                  @change="toggleDeviceSync(device, $event)" />
                <span>{{ t("同步剪贴板") }}</span>
              </label>
              <div v-if="pairingPending(device)" class="pairing-controls">
                <!-- The code is meant to be read off this screen and compared
                     with the one the other machine is showing, which is fine
                     for a reader standing at both and impossible for one
                     talking to somebody at the other end on the phone.  The
                     copy button is what the internet pairing card's own code
                     has, on the same reasoning. -->
                <p v-if="device.pairing_code">{{ t("配对码：") }}<strong>{{ device.pairing_code }}</strong>
                  <button type="button" class="icon-button" :title="t('复制')" :aria-label="t('复制配对码')"
                    @click="copyText(String(device.pairing_code))"><Copy :size="15" /></button>
                </p>
                <p v-if="device.sas">{{ t("安全代码：") }}<strong>{{ device.sas }}</strong></p>
                <p class="note">{{ t("请核对两台设备上的代码，仅在一致时确认。") }}</p>
                <p class="note">{{ t("请求超过五分钟未处理会过期，设备行会显示“已过期”，重新配对即可。") }}</p>
                <div class="pairing-actions">
                  <button :disabled="busy || !device.pairing_code || device.pairing_status === 'confirmed_waiting'" @click="store.confirmPairing(device)"><Check :size="17" />{{ t("确认配对") }}</button>
                  <button :disabled="busy" @click="store.rejectPairing(device)"><X :size="17" />{{ t("拒绝") }}</button>
                </div>
              </div>
            </article>
            <!-- The devices that have been taken away close the same tab as the
                 ones that are here: they are the tail of one list, not a
                 question of their own, and the two ways a device that is not on
                 this network becomes one are that question and have tabs. -->
            <section v-if="archivedDevices.length" class="archived-devices">
              <h2 class="card-sub">{{ t("已移除的设备") }}</h2>
              <!-- What 移除 did, and what it did not.  The list showed a date and
                   two buttons and nothing that said whether a removed device was
                   still on record — so the only way to find out was to press 彻底
                   删除 and see what the dialog said.  Nothing here is purged on a
                   timer: an archived row stays until someone deletes it, which is
                   what makes 恢复 a promise rather than a hope.  See U118. -->
              <p class="note archived-note">{{ t("移除只是从这台机器的列表里收起来：记录仍在本机，也不会自动清理，随时可以恢复。彻底删除才会把它从本机抹掉。") }}</p>
              <article v-for="device in archivedDevices" :key="device.id" class="device-row">
                <Monitor :size="25" class="device-icon" />
                <div class="device-identity"><span class="device-name">{{ deviceLabel(device) }}</span><span class="note">{{ deviceSubtitle(device) }}</span></div>
                <div class="row-actions">
                  <button :disabled="busy" @click="store.restore(device)"><RotateCcw :size="17" />{{ t("恢复") }}</button>
                  <button class="danger-outline" :disabled="busy" @click="purgeDevice = device"><Trash2 :size="17" />{{ t("彻底删除") }}</button>
                </div>
                <!-- The same second line the live rows carry, holding the one
                     fact an archived row has instead of a route: when it was
                     taken away.  Written as the same chip so a reader scanning
                     the column of second lines reads dates in the same place a
                     live row's routes are. -->
                <span class="device-channels">
                  <span class="channel">{{ t("移除于") }} {{ dateTime(device.removed_at || 0) }}</span>
                </span>
              </article>
            </section>



        </section>
        <!-- The AI page is drawn outside the chain above because it is shown
             rather than mounted: `v-show` keeps its state -- an edit in its
             editor, the device it was looking at -- across a visit to another
             page, which a `v-else-if` branch would throw away.  Nothing in the
             chain matches while it is on screen, so nothing renders beside it.
             See U03. -->
        <AiView v-if="aiVisited" v-show="tab === 'ai'" ref="aiView" :store="store" />
      </div>
      <!-- A paste of a remote file is a transfer the reader can watch from
           anywhere in the window: their paste came back later than the click,
           and the row it came from may be a page away.  Fixed above the status
           bar, with the one action that ends it. -->
      <div v-if="pasteFetch" class="paste-fetch" role="status" aria-live="polite">
        <RefreshCw :size="15" class="spinning" aria-hidden="true" />
        <span class="paste-fetch-name">{{ pasteFetchName }}</span>
        <progress class="paste-fetch-progress" :value="pasteFetchPercent" max="100" aria-hidden="true"></progress>
        <span class="note paste-fetch-percent">{{ Math.round(pasteFetchPercent) }}%</span>
        <span v-if="pasteFetchSpeed" class="note">{{ pasteFetchSpeed }}</span>
        <span v-if="pasteFetchEta" class="note">{{ t("剩余 {eta}", { eta: pasteFetchEta }) }}</span>
        <button class="text-button" type="button" @click="cancelPasteFetch">{{ t("取消取回") }}</button>
      </div>
      <div class="bottom-status"><ShieldCheck :size="14" />{{ t("本地数据") }}
        <span role="status" class="sync-state" :class="{ 'sync-state--stopped': syncStopped && !state.pending }">
          <Pause v-if="syncStopped && !state.pending" :size="12" />{{ state.pending ? t('正在处理') : (state.status?.runtime_error || syncLabel) }}</span>
        <!-- Status only, by the user's request: this bar used to carry the
             sync switch and the timed-pause presets too, and every one of
             them already sits on the overview page's 快速设置 card.  Two
             copies of one control, one screen apart, is what made the bar
             read as cluttered.  What stays is what the bar alone can say --
             which state sync is in, and, when a pause is armed, how long is
             left of it.  The controls themselves are on 概览, and the tray
             keeps its own 15/30/60 submenu. -->
        <template v-if="pauseLeftMs > 0">
          <span class="pause-status">⏸ {{ t("已暂停 · 剩余 {minutes} 分钟", { minutes: pauseLeftMinutes }) }}</span>
        </template>
        <!-- Legacy showed its notices as one floating toast rather than per
             page, and the footer is the surface that stays on screen: a message
             about a pause started here, or about a page the user has since left,
             is still readable. -->
        <span v-if="statusMessage()" class="note status-message" role="status">{{ statusMessage() }}</span>
        <!-- Something the application cannot do here, said where the user is
             already looking.  It is deliberately not a toast: a toast is
             dismissible and gone, and this condition does not fix itself -- on
             Linux without `xclip`/`wl-paste` every copy is simply never seen,
             which looks exactly like an application that is working. -->
        <span
          v-for="warning in state.status?.warnings || []"
          :key="warning.code"
          class="capability-warning"
          role="alert"
          :title="warning.guidance_text || warning.guidance"
        >⚠ {{ warning.detail_text || warning.detail }}</span>
        <!-- The notice stack is placed against this bar rather than against the
             window: the stylesheet measures it from the bar's top edge, because
             the bar is the only element that knows where its own top edge is.
             It is a child here and out of this row's flow — nothing makes room
             for it, and the bar is as tall as it was without it. -->
        <NoticeStack :store="store" />
      </div>
    </main>

    <!-- The sidecar's "this paste is large, ask first" question.  It is a
         question rather than a refusal: 继续 prepares the same paste again
         with the answer attached, and 取消 leaves the history row as it was. -->
    <AppDialog ref="pasteConfirmDialog" heading-id="paste-confirm-title"
      :title="t('取回文件并粘贴？')"
      @close="pasteConfirmItem = null" @cancel="pasteConfirmItem = null">
      <p v-if="pasteConfirmSize > 0">{{ t("文件约 {size}，粘贴前需先从另一台设备传输。继续？", { size: size(pasteConfirmSize) }) }}</p>
      <p v-else>{{ t("这些文件在另一台设备上，粘贴前需要先传输过来。文件较大时可能要等上一会儿。") }}</p>
      <p v-if="pasteConfirmItem" class="note">{{ previewLabel(pasteConfirmItem) }}</p>
      <template #actions>
        <button @click="pasteConfirmItem = null">{{ t("取消") }}</button>
        <button class="primary" @click="confirmLargePaste">{{ t("继续") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="sendUrlDialog" heading-id="send-url-title" :title='t("发送网址到 {name}", { name: deviceNamed(sendUrlDevice) })' @close="sendUrlDevice = null" @cancel="sendUrlDevice = null">
      <p class="muted">{{ t("对方将在默认浏览器中打开该 http(s) 网址。") }}</p>
      <label v-if="sendUrlCandidates.length > 1">{{ t("目标设备") }}
        <select v-model="sendUrlDeviceId" :aria-label="t('目标设备')">
          <option v-for="candidate in sendUrlCandidates" :key="candidate.id" :value="candidate.id">{{ deviceLabel(candidate) }}</option>
        </select>
      </label>
      <label>{{ t("网址") }}<input v-model="sendUrlValue" type="url" maxlength="2048" :aria-label="t('要发送的网址')" placeholder="https://example.com" autofocus
        :aria-invalid="!!sendUrlProblem" :aria-describedby="sendUrlProblem ? 'send-url-problem' : undefined" /></label>
      <!-- Beside the box, not on the band at the top of the window: the answer
           belongs where the mistake is. -->
      <p v-if="sendUrlProblem" id="send-url-problem" class="modal-error small" role="status">{{ sendUrlProblem }}</p>
      <!-- The focus starts where the typing does.  On a dialog whose only job is
           one field, 取消 as the first stop meant two Tabs before the box could
           be typed in, and Enter on arrival dismissed the dialog. -->
      <template #actions>
        <button @click="sendUrlDevice = null">{{ t("取消") }}</button><button :disabled="sendUrlBusy || !sendUrlValid" @click="confirmSendUrl">{{ t("发送") }}</button></template>
    </AppDialog>
    <AppDialog ref="quickSearchDialog" heading-id="quick-search-title" :title='t("搜索")' wide
      @close="quickSearchOpen = false" @cancel="quickSearchOpen = false">
      <QuickSearch ref="quickSearch" @open="quickSearchHandOver" @close="quickSearchOpen = false" />
    </AppDialog>
    <AppDialog ref="pushTextDialog" heading-id="push-text-title" :title='t("推送文本")' @close="pushTextOpen = false" @cancel="pushTextOpen = false">
      <p class="muted">{{ t("文本会写入本机剪贴板；同步开启时会同时发送到所有设备。") }}</p>
      <label>{{ t("文本") }}<textarea v-model="pushTextValue" rows="5" maxlength="100000" :aria-label="t('要推送的文本')" autofocus></textarea></label>
      <template #actions>
        <button @click="pushTextOpen = false">{{ t("取消") }}</button><button :disabled="pushTextBusy || !pushTextValue.trim()" @click="confirmPushText">{{ t("推送") }}</button></template>
    </AppDialog>
    <AppDialog ref="translateItemDialog" heading-id="translate-item-title" :title='t("翻译记录")' @close="translateItemOpen = false" @cancel="translateItemOpen = false">
      <pre class="translate-source" tabindex="0" :aria-label="t('记录原文')">{{ translateItemText }}</pre>
      <p v-if="translateItemTruncated" class="note translate-truncated">{{ t("记录过长，只读取并翻译了前 {count} 个字符", { count: translateItemText.length }) }}</p>
      <label>{{ t("源语言") }}<select v-model="translationSource" :aria-label="t('翻译源语言')"><option value="auto">{{ t("自动检测") }}</option><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></label>
      <label>{{ t("目标语言") }}<select v-model="translationTarget" :aria-label="t('翻译目标语言')"><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></label>
      <p v-if="translateItemResult" class="translation-result">{{ translateItemResult }}</p>
      <template #actions>
        <button autofocus @click="translateItemOpen = false">{{ t("关闭") }}</button><button :disabled="translateItemBusy" @click="runTranslateItem">{{ t("翻译") }}</button></template>
    </AppDialog>
    <AppDialog ref="logsDialog" heading-id="logs-title" :title='t("应用日志")' @close="logsOpen = false" @cancel="logsOpen = false">
      <label>{{ t("显示行数") }}
        <select :value="logCount" :aria-label="t('日志行数')" :disabled="logsBusy" @change="loadLogs(Number(($event.target as HTMLSelectElement).value))">
          <option :value="200">200</option><option :value="500">500</option><option :value="1000">1000</option>
        </select>
      </label>
      <div class="log-filters" role="group" :aria-label="t('按级别筛选日志')">
        <button type="button" class="chip" :class="{ 'chip--active': logView === 'problems' }"
          :aria-pressed="logView === 'problems'" :disabled="logsBusy" @click="logView = 'problems'">
          {{ t("问题") }}<span class="chip-count">{{ logProblems.length }}</span>
        </button>
        <button type="button" class="chip" :class="{ 'chip--active': logView === 'all' }"
          :aria-pressed="logView === 'all'" :disabled="logsBusy" @click="logView = 'all'">
          {{ t("全部") }}<span class="chip-count">{{ logLines.length }}</span>
        </button>
      </div>
      <p v-if="logsBusy" class="muted">{{ t("正在读取日志…") }}</p>
      <pre v-else-if="shownLogLines.length" class="log-view" :aria-label="t('日志内容')">{{ shownLogLines.join("\n") }}</pre>
      <p v-else class="muted">{{ logView === "problems" ? t("没有需要排查的日志") : t("暂无日志") }}</p>
      <p v-if="logExportMessage" role="status" class="note">{{ logExportMessage }}</p>
      <template #actions>
        <button type="button" :disabled="logsExporting" @click="exportLogs">
          <Save :size="17" />{{ logsExporting ? t("正在导出…") : t("导出日志…") }}
        </button>
        <button autofocus @click="logsOpen = false">{{ t("关闭") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="qrDialog" heading-id="qr-title" :title='t("网页二维码")' @close="qrOpen = false" @cancel="qrOpen = false">
      <p class="muted">{{ t("用手机相机扫描，打开手机 Companion 页面。") }}</p>
      <p v-if="qrBusy" class="muted">{{ t("正在生成二维码…") }}</p>
      <img v-else-if="qrImage" :src="qrImage" :alt="t('手机 Companion 二维码')" class="qr-image" width="220" height="220" />
      <p v-else class="muted" role="status">{{ qrMessage || t("二维码不可用") }}</p>
      <p v-if="qrUrl" class="note selectable">{{ qrUrl }}</p>
      <p v-if="qrShareMessage" role="status" class="note">{{ qrShareMessage }}</p>
      <template #actions>
        <button type="button" :disabled="qrShareBusy" @click="shareFileToPhone">
          <SendHorizontal :size="17" />{{ qrShareBusy ? t("正在发送…") : t("发送文件到手机") }}
        </button>
        <button autofocus @click="qrOpen = false">{{ t("关闭") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="companionRemoteDialog" heading-id="remote-qr-title" :title='t("远程访问二维码")' @close="companionRemoteQrOpen = false" @cancel="companionRemoteQrOpen = false">
      <p class="muted">{{ t("手机不在同一网络时（4G/5G 或其他 Wi-Fi），用相机扫描这个二维码。") }}</p>
      <p v-if="companionRemoteQrBusy" class="muted">{{ t("正在生成二维码…") }}</p>
      <img v-else-if="companionRemoteQrImage" :src="companionRemoteQrImage" :alt="t('远程访问二维码')" class="qr-image" width="220" height="220" />
      <p v-else class="muted" role="status">{{ companionRemoteQrMessage || t("二维码不可用") }}</p>
      <p v-if="companionRemoteQrUrl" class="note selectable">{{ companionRemoteQrUrl }}</p>
      <template #actions>
        <button autofocus @click="companionRemoteQrOpen = false">{{ t("关闭") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="aboutDialog" heading-id="about-title" :title='t("关于 ClipSync")' @close="aboutOpen = false" @cancel="aboutOpen = false">
      <p class="about-version">ClipSync {{ state.status ? state.status.version : "…" }}</p>
      <p class="muted">{{ t("跨平台剪贴板共享，支持 Windows、macOS 和 Linux。") }}</p>
      <p class="muted">{{ t("在设备之间实时共享剪贴板内容与文件。") }}</p>
      <p v-if="aboutMessage" role="status" class="note">{{ aboutMessage }}</p>
      <details class="shortcut-list">
        <summary>{{ t("快捷键") }}</summary>
        <dl>
          <template v-for="row in shortcutRows" :key="row.label">
            <dt><kbd>{{ row.keys }}</kbd></dt>
            <dd>{{ row.label }}</dd>
          </template>
        </dl>
      </details>
      <template #actions>
        <button type="button" :disabled="aboutBusy" @click="openAboutLink('homepage')"><Link :size="17" />{{ t("项目主页") }}</button>
        <button type="button" :disabled="aboutBusy" @click="openAboutLink('releases')"><Globe :size="17" />{{ t("最新版本") }}</button>
        <button autofocus @click="aboutOpen = false">{{ t("关闭") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="diagnosticsDialog" heading-id="diagnostics-title" :title='t("系统诊断")' wide @close="diagnosticsOpen = false" @cancel="diagnosticsOpen = false">
      <p v-if="diagnosticsBusy" class="muted">{{ t("正在检测…") }}</p>
      <template v-else-if="diagnosticsReport">
        <p role="status">{{ t("总体状态：") }}<strong :class="`diag-summary--${diagnosticsReport.summary}`">{{ diagnosticsSummaryLabel(diagnosticsReport.summary) }}</strong></p>
        <p class="note">{{ diagnosticsOverview }}</p>
        <ul class="diag-list">
          <li v-for="group in diagnosticGroups" :key="group.id">
            <h3>{{ group.label }}<!-- The count and the verdict sit on the heading so a reader can
              triage the whole report before opening anything: which section is
              failing, and how much of it there is. --><span class="diag-count">{{ group.items.length }}</span><span class="diag-verdict" :class="`diag-summary--${diagnosticVerdict(group)}`">{{ diagnosticVerdictLabel(diagnosticVerdict(group)) }}</span></h3>
            <ul>
              <li v-for="item in group.items" :key="item.id" :class="`diag-${item.status}`">
                <span class="diag-item"><span class="diag-mark" aria-hidden="true">{{ diagnosticGlyph(item.status) }}</span><span class="diag-label">{{ diagnosticLabel(item) }}</span>{{ diagnosticDetail(item) }}</span>
                <span v-if="diagnosticHint(item)" class="note">{{ diagnosticHint(item) }}</span>
                <button v-if="item.id === 'firewall' && item.status !== 'ok'" type="button" class="status-action" :disabled="diagnosticsRepairBusy" @click="repairDiagnostics('firewall')"><Wrench :size="14" />{{ t("修复防火墙") }}</button>
              </li>
            </ul>
          </li>
        </ul>
        <template v-if="permissionsRepairCheck">
          <p class="note">{{ diagnosticsCheckText(permissionsRepairCheck) }}</p>
          <p v-if="permissionsRepairCheck.guidance_text || permissionsRepairCheck.guidance" class="note">{{ permissionsRepairCheck.guidance_text || permissionsRepairCheck.guidance }}</p>
          <div class="actions">
            <button type="button" :disabled="diagnosticsRepairBusy" @click="repairDiagnostics('local_network')"><Wrench :size="15" />{{ t("打开本地网络权限") }}</button>
          </div>
        </template>
      </template>
      <p v-else class="muted">{{ t("暂时无法获取诊断信息") }}</p>
      <p v-if="diagnosticsCopied" class="note" role="status">{{ diagnosticsCopied }}</p>
      <template #actions>
        <button v-if="diagnosticsReport" type="button" @click="copyDiagnostics"><Copy :size="15" />{{ t("复制诊断结果") }}</button>
        <button autofocus :disabled="diagnosticsBusy" @click="rerunDiagnostics">{{ t("重新检测") }}</button>
        <button @click="diagnosticsOpen = false">{{ t("关闭") }}</button>
      </template>
    </AppDialog>
    <AppDialog ref="restartDialog" heading-id="restart-title" :title="t('重启 ClipSync？')" @close="restartOpen = false" @cancel="restartOpen = false">
      <p>{{ t("应用会退出并自动重新启动，同步会短暂中断。") }}</p>
      <template #actions>
        <button autofocus @click="restartOpen = false">{{ t("取消") }}</button><button class="danger" @click="confirmRestart">{{ t("重启") }}</button></template>
    </AppDialog>
    <AppDialog ref="clearPasswordDialog" heading-id="clear-password-title" :title="t('清除加密密码？')" @close="clearPasswordOpen = false" @cancel="clearPasswordOpen = false">
      <p>{{ t("清除后重启不再需要密码，加密流量改用设备身份派生密钥。") }}</p>
      <template #actions>
        <button autofocus :disabled="securityBusy" @click="clearPasswordOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="securityBusy" @click="clearPassword">{{ t("清除") }}</button></template>
    </AppDialog>
    <dialog ref="factoryResetDialog" aria-labelledby="factory-reset-title" class="modal" @close="closeFactoryReset" @cancel="closeFactoryReset">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="closeFactoryReset"><X :size="18" /></button>
      <h2 id="factory-reset-title">{{ t("恢复出厂设置？") }}</h2>
      <p>{{ t("全部历史、收藏、配对和设备身份都会被删除，应用随即重新启动。此操作无法撤销。") }}</p>
      <div class="factory-backup">
        <p class="note">{{ t("想留一份副本的话，现在导出备份，重置后还能恢复回来。") }}</p>
        <button type="button" :disabled="factoryResetBackupBusy || securityBusy" @click="backupBeforeReset">
          <Save :size="16" />{{ factoryResetBackupBusy ? t("正在备份…") : t("先创建备份") }}
        </button>
        <p v-if="factoryResetBackupMessage" class="note" role="status">{{ factoryResetBackupMessage }}</p>
      </div>
      <!-- The word, typed: a button that has to be reached is a button people
           reach for, and this one deletes the whole library in one request. -->
      <label class="setting">
        <span class="setting-name">{{ t("输入「{word}」以确认", { word: factoryResetWord }) }}</span>
        <span class="setting-control">
          <input v-model="factoryResetConfirm" type="text" autocomplete="off" autocapitalize="off" spellcheck="false"
            :aria-label="t('输入「{word}」以确认', { word: factoryResetWord })" :placeholder="factoryResetWord" autofocus />
        </span>
      </label>
      <div class="modal-actions"><button :disabled="securityBusy" @click="closeFactoryReset">{{ t("取消") }}</button><button class="danger" :disabled="securityBusy || !factoryResetConfirmed" @click="confirmFactoryReset">{{ securityBusy ? t('正在重置') : t('确认重置') }}</button></div>
    </dialog>
    <dialog ref="dataInUseDialog" aria-labelledby="data-in-use-title" class="modal" @close="dataInUseOpen = false" @cancel="dataInUseOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="dataInUseOpen = false"><X :size="18" /></button>
      <h2 id="data-in-use-title">{{ t("数据目录被占用") }}</h2>
      <p>{{ t("后台进程起不来，是因为同一个数据目录正被另一份 ClipSync 用着。最常见的来源是旧版还开着：关掉窗口不等于退出，它会留在托盘里继续同步。") }}</p>
      <!-- Where to look, per platform.  The window cannot list the processes on
           this machine, so the steps replace a list it does not have. -->
      <ul class="data-in-use-steps">
        <li>{{ t("Windows：在托盘里找到 ClipSync 图标选「退出」；或用任务管理器「详细信息」，找进程名里带 clipsync 的那几个。") }}</li>
        <li>{{ t("macOS：在菜单栏图标里退出；或在「活动监视器」里搜 ClipSync。") }}</li>
        <li>{{ t("Linux：运行 ps -ef | grep -i clipsync，看还有哪些进程在跑。") }}</li>
      </ul>
      <p class="note">{{ t("这里不会替你结束进程：结束错了可能把正在写的数据弄坏。确认旧实例都退出之后，点「重新连接」再试。") }}</p>
      <div class="modal-actions">
        <button autofocus @click="dataInUseOpen = false">{{ t("关闭") }}</button>
        <button class="primary" @click="dataInUseOpen = false; store.reconnect">{{ t("重新连接") }}</button>
      </div>
    </dialog>
    <AppDialog ref="recoverDialog" heading-id="recover-title" :title='t("修复数据目录？")' @close="recoverOpen = false" @cancel="recoverOpen = false">
      <p>{{ t("剪贴板历史、设备身份或配置已损坏，应用无法启动。修复会把这些文件改名移到一旁（不会删除），并在原位置新建配置。") }}</p>
      <p>{{ t("移开的文件可以手动改回原名恢复。") }}</p>
      <!-- Where they go, and a way to take a copy first.  The dialog described
           the repair to someone whose app will not start: the one thing left to
           do before pressing it is to keep what is there now. -->
      <p class="note">{{ t("移开的文件留在同一个数据目录里，文件名后面加上 .broken 和时间戳。") }}</p>
      <div class="setting-actions">
        <button type="button" :disabled="state.pending" @click="openDataFolder('data')"><FolderOpen :size="16" />{{ t("先打开数据文件夹留一份") }}</button>
      </div>
      <template #actions>
        <button autofocus :disabled="state.pending" @click="recoverOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="state.pending" @click="confirmRecover">{{ state.pending ? t('正在修复') : t('确认修复') }}</button></template>
    </AppDialog>
    <AppDialog ref="restoreDialog" heading-id="restore-title" :title='t("确认恢复备份？")' @close="restorePath = null" :prevent-cancel="restoreBusy" @cancel="onRestoreCancel">
      <p>{{ restorePath }}</p>
      <p>{{ t("此操作会修改当前数据。恢复过程中请勿退出应用。") }}</p>
      <template #actions>
        <button autofocus :disabled="restoreBusy" @click="restorePath = null">{{ t("取消") }}</button><button class="danger" :disabled="restoreBusy || !restorePath" @click="confirmRestoreBackup">{{ restoreBusy ? t('正在恢复') : t('确认恢复') }}</button></template>
    </AppDialog>
    <AppDialog ref="companionRotateDialog" heading-id="companion-rotate-title" :title="t('更换手机访问令牌？')" @close="companionRotatePending = false" @cancel="companionRotatePending = false">
      <p>{{ t("旧访问链接将失效，手机需要使用新地址重新连接。") }}</p>
      <template #actions>
        <button autofocus @click="companionRotatePending = false">{{ t("取消") }}</button><button class="danger" :disabled="companionBusy || !companionRotatePending || !companion?.running" @click="rotateCompanionToken">{{ t("更换令牌") }}</button></template>
    </AppDialog>
    <AppDialog ref="companionClearDialog" heading-id="companion-clear-title" :title="t('清除手机访问令牌？')" @close="companionClearPending = false" @cancel="companionClearPending = false">
      <p>{{ t("清除后手机不再需要令牌，任何能访问该端口的设备都可以直接连接。") }}</p>
      <template #actions>
        <button autofocus @click="companionClearPending = false">{{ t("取消") }}</button><button class="danger" :disabled="companionBusy || !companionClearPending || !companion?.running" @click="clearCompanionToken">{{ t("清除令牌") }}</button></template>
    </AppDialog>
    <dialog ref="deleteDialog" aria-labelledby="delete-title" class="modal" @close="deleteItem = null" @cancel="deleteItem = null">
        <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="deleteItem = null"><X :size="18" /></button>
        <h2 id="delete-title">{{ t("删除这条记录？") }}</h2><p class="muted">{{ t("记录将从本地历史中移除，此操作无法撤销。") }}</p>
        <div class="modal-actions"><button @click="deleteItem = null">{{ t("取消") }}</button><button class="danger" :disabled="state.pending" @click="confirmDelete">{{ t("删除") }}</button></div>
    </dialog>
    <dialog ref="batchDeleteDialog" aria-labelledby="batch-delete-title" class="modal" @close="batchDeleteIds = null" @cancel="batchDeleteIds = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="batchDeleteIds = null"><X :size="18" /></button>
      <h2 id="batch-delete-title">{{ t("删除所选的 {count} 条记录？", { count: batchDeleteIds?.length || 0 }) }}</h2>
      <p class="muted">{{ t("所选记录将从本地历史中移除，此操作无法撤销。") }}</p>
      <p v-if="state.error" class="modal-error small" role="alert" :title="state.error.code">{{ state.error.message }}</p>
      <p v-if="batchDeleteIds && !batchDeleteValid" role="status">{{ t("所选记录已变化，请取消并重新选择。") }}</p>
      <div class="modal-actions"><button autofocus @click="batchDeleteIds = null">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy || !batchDeleteValid" @click="confirmBatchDelete">{{ t("删除所选记录") }}</button></div>
    </dialog>
    <dialog ref="clearHistoryDialog" aria-labelledby="clear-history-title" class="modal" @close="clearHistoryOpen = false" @cancel="clearHistoryOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="clearHistoryOpen = false"><X :size="18" /></button>
      <h2 id="clear-history-title">{{ t("清空全部历史记录？") }}</h2>
      <p class="muted">{{ t("共 {count} 条记录将被移除，此操作无法撤销。", { count: state.total }) }}</p>
      <p v-if="state.error" class="modal-error small" role="alert" :title="state.error.code">{{ state.error.message }}</p>
      <div class="modal-actions"><button autofocus @click="clearHistoryOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy" @click="confirmClearHistory">{{ t("清空历史") }}</button></div>
    </dialog>
    <dialog ref="revokeDialog" aria-labelledby="revoke-title" class="modal" @close="revokeDevice = null" @cancel="revokeDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="revokeDevice = null"><X :size="18" /></button>
      <h2 id="revoke-title">{{ t("撤销设备信任？") }}</h2>
      <p class="muted">{{ t("{name} 将无法继续同步，重新连接需要双方再次配对。", { name: deviceNamed(revokeDevice) }) }}</p>
      <!-- Three sentences the dialog used to leave out, and the three questions
           anyone about to press 撤销信任 actually has: what happens to what is
           still waiting, what happens to what is already here, and what it takes
           to get the device back. -->
      <p v-if="revokeDevice && delivery.pending(revokeDevice.id) > 0" class="note">
        {{ t("还有 {count} 条内容在等它上线后补发；撤销后不再补发。", { count: delivery.pending(revokeDevice.id) }) }}</p>
      <p class="note">{{ t("本机保留着它的剪贴板历史与聊天记录，撤销只断开同步。") }}</p>
      <p class="note">{{ t("要重新连上，得两台设备再配对一次。") }}</p>
      <div class="modal-actions"><button autofocus @click="revokeDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === revokeDevice?.id && device.paired)" @click="confirmRevoke">{{ t("撤销信任") }}</button></div>
    </dialog>
    <!-- What ends here is a code pairing and nothing else: no certificate is
         dropped (an internet peer never had one pinned) and the device is not
         archived (it was never in the removed-device list to be recovered
         from).  The sentence says what actually goes away — the two machines
         stop reaching each other over the relay — rather than borrowing the
         LAN wording for a pairing that was never LAN. -->
    <dialog ref="relayUnpairDialog" aria-labelledby="relay-unpair-title" class="modal" @close="relayUnpairDevice = null" @cancel="relayUnpairDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="relayUnpairDevice = null"><X :size="18" /></button>
      <h2 id="relay-unpair-title">{{ t("解除互联网配对？") }}</h2>
      <p class="muted">{{ t("{name} 将不再通过中继与这台设备同步，恢复需要重新配对一次。", { name: deviceNamed(relayUnpairDevice) }) }}</p>
      <div class="modal-actions"><button autofocus @click="relayUnpairDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy" @click="confirmRelayUnpair">{{ t("解除互联网配对") }}</button></div>
    </dialog>
    <dialog ref="forgetDialog" aria-labelledby="forget-title" class="modal" @close="forgetDevice = null" @cancel="forgetDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="forgetDevice = null"><X :size="18" /></button>
      <h2 id="forget-title">{{ t("移除这台设备？") }}</h2>
      <p class="muted">{{ t("{name} 将停止同步并从此列表消失，可在“已移除的设备”中恢复。", { name: deviceNamed(forgetDevice) }) }}</p>
      <div class="modal-actions"><button autofocus @click="forgetDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === forgetDevice?.id && !device.archived)" @click="confirmForget">{{ t("移除设备") }}</button></div>
    </dialog>
    <dialog ref="purgeDialog" aria-labelledby="purge-title" class="modal" @close="purgeDevice = null" @cancel="purgeDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="purgeDevice = null"><X :size="18" /></button>
      <h2 id="purge-title">{{ t("彻底删除这台设备？") }}</h2>
      <p class="muted">{{ t("{name} 的移除记录将被永久删除，此操作无法撤销。", { name: deviceNamed(purgeDevice) }) }}</p>
      <div class="modal-actions"><button autofocus @click="purgeDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === purgeDevice?.id && device.archived)" @click="confirmPurge">{{ t("彻底删除") }}</button></div>
    </dialog>
    <!-- The context menu 重命名 entry. The row showed a 设备备注 field writing this
         same alias until it was removed; the dialog is now the only way in — and
         it was always a way in rather than a second setting. -->
    <dialog ref="renameDialog" aria-labelledby="rename-title" class="modal" @close="renameTarget = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="renameTarget = null"><X :size="18" /></button>
      <h2 id="rename-title">{{ t("重命名设备") }}</h2>
      <p class="note">{{ t("名称只保存在这台设备上，对方看到的仍是自己的名字。") }}</p>
      <input :value="renameValue" maxlength="512" :aria-label="t('设备名称')" autofocus
        @input="renameValue = ($event.target as HTMLInputElement).value" @keydown.enter.prevent="confirmRename" />
      <div class="modal-actions"><button @click="renameTarget = null">{{ t("取消") }}</button><button class="primary" :disabled="!renameValue.trim()" @click="confirmRename">{{ t("保存") }}</button></div>
    </dialog>
    <!-- A device whose certificate no longer matches its pin was refused, so
         this is the only place the user hears about it — and the answer is what
         either re-pins the certificate or unpairs the device. -->
    <AppDialog ref="certAlertDialog" heading-id="cert-alert-title" :title='t("设备身份变更")' @close="certPromptOpen = false" :closable="false" prevent-cancel>
      <p>{{ t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: certAlertName }) }}</p>
      <p class="note">{{ t("这是您信任的设备吗？") }}</p>
      <p v-if="!state.certAlert?.can_trust" class="note">{{ t("该设备需要再次连接后才能信任新证书。") }}</p>
      <p v-if="state.error" class="modal-error small" role="alert">{{ state.error.message }}</p>
      <!-- Gated on `pending`, not on `busy`: the store's action refuses while a
           request is in flight, and a snapshot refresh must never hold up the
           answer to a security prompt. -->
      <template #actions>
        <button :disabled="state.pending" @click="rejectCertAlert">{{ t("保持不配对") }}</button>
        <button autofocus :disabled="state.pending || !state.certAlert?.can_trust" @click="acceptCertAlert">{{ t("重新信任") }}</button>
      </template>
    </AppDialog>
    <dialog ref="certDialog" aria-labelledby="cert-title" class="modal modal--wide" @close="certificates = null" @cancel="certificates = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="certDialog?.close()"><X :size="18" /></button>
      <h2 id="cert-title">{{ t("已固定证书指纹") }}</h2>
      <p class="note">{{ t("同步与聊天只接受指纹一致的设备，发现不一致请撤销信任后重新配对。") }}</p>
      <!-- The fingerprint is shown whole and in groups of four: it exists to be
           read off this screen and compared with the other machine's, and a
           short form makes that comparison impossible.  The short form stays
           beside it as the secondary value the device list already shows. -->
      <ul v-if="certificates?.length" class="cert-list">
        <li v-for="item in certificates" :key="item.device_id">
          <strong>{{ item.device_name || item.device_id }}</strong>
          <span class="note">{{ item.paired ? t("已配对") : t("未配对") }}</span>
          <code v-if="fingerprintGroups(item.fingerprint)" class="fingerprint fingerprint--full"
            :title="t('完整指纹：与对方屏幕上的逐个字符核对')">{{ fingerprintGroups(item.fingerprint) }}</code>
          <code v-else class="fingerprint">{{ item.fingerprint_short || t("未固定") }}</code>
          <button v-if="fingerprintGroups(item.fingerprint)" class="icon-button icon-button--inline"
            :aria-label="t('复制 {name} 的指纹', { name: item.device_name || item.device_id })"
            :title="t('复制完整指纹')"
            @click="copyText(fingerprintGroups(item.fingerprint))"><Copy :size="14" /></button>
          <span v-if="item.fingerprint_short && fingerprintGroups(item.fingerprint)" class="note">{{ t("短号 {short}", { short: item.fingerprint_short }) }}</span>
        </li>
      </ul>
      <p v-else class="muted">{{ t("暂无已固定的设备证书") }}</p>
      <div class="modal-actions">
        <button v-if="certificates?.length" :disabled="!certificates?.length" @click="copyText(allFingerprints())">{{ t("复制全部指纹") }}</button>
        <button autofocus @click="certDialog?.close()">{{ t("关闭") }}</button>
      </div>
    </dialog>
    <!-- First-run language picker. Deliberately bilingual: it must be readable
         before any language has been chosen, so its text is not translated
         (its catalog entries map to themselves). -->
    <!-- The one switch in this window that sends clipboard text off the LAN,
         explained before it is used rather than after the fact.  Both doors
         onto it — this card and the settings page — arrive here; see
         `INTERNET_CONSENT_KEY`. -->
    <AppDialog ref="internetConsentDialog" heading-id="internet-consent-title" :title='t("开启互联网同步？")' @close="internetConsentOpen = false" @cancel="internetConsentOpen = false">
      <p class="muted">{{ t("开启后剪贴板内容会经互联网在设备之间同步，数据由第三方中继服务器转发，不再只在这台路由器下的局域网里。") }}</p>
      <p class="muted">{{ t("中继负责转发端到端加密后的数据，无法解密内容，也不会拿到你的中继密码。配对仍需双方确认。") }}</p>
      <p class="muted">{{ t("随时可以关掉这个开关：关掉后设备之间不再通过中继配对或同步，剪贴板不再离开局域网。") }}</p>
      <template #actions>
        <button autofocus @click="internetConsentOpen = false">{{ t("取消") }}</button><button class="primary" @click="confirmInternetConsent">{{ t("我了解，开启") }}</button></template>
    </AppDialog>
    <!-- First-run onboarding.  Language and theme are both chosen here and
         previewed live, but nothing is stored until 完成 is pressed.  It is
         deliberately not a two-click dismissal: a picker that closed on the
         first language click skipped the rest of the setup and made the
         language look like the only thing to decide. -->
    <dialog ref="languageDialog" aria-labelledby="onboarding-title" class="modal onboarding" @cancel.prevent>
      <div class="onboarding-top">
        <div class="onboarding-brand"><img :src="logo" alt="" /><span>ClipSync</span></div>
        <span class="onboarding-progress">{{ onboardingStep }} / 2</span>
      </div>

      <template v-if="onboardingStep === 1">
        <p class="onboarding-kicker">{{ t("首次设置") }}</p>
        <h2 id="onboarding-title">{{ t("欢迎使用 ClipSync") }}</h2>
        <p class="onboarding-lead">{{ t("先选好语言和外观，再开始使用。两项之后都能在设置里修改。") }}</p>

        <section class="onboarding-section">
          <div class="onboarding-section-head">
            <h3><span class="onboarding-step">1</span>{{ t("选择语言") }}</h3>
            <p>{{ t("界面会立即切换，选你觉得顺手的。") }}</p>
          </div>
          <div class="onboarding-languages">
            <button v-for="choice in languageChoices" :key="choice.code" type="button" :data-locale="choice.code"
              class="onboarding-language" :class="{ selected: onboardingLanguage === choice.code }"
              :aria-pressed="onboardingLanguage === choice.code" @click="selectOnboardingLanguage(choice.code)">
              <span class="onboarding-language-copy"><strong>{{ choice.native }}</strong><span>{{ choice.other }}</span></span>
              <Check v-if="onboardingLanguage === choice.code" :size="17" />
            </button>
          </div>
        </section>

        <section class="onboarding-section">
          <div class="onboarding-section-head">
            <h3><span class="onboarding-step">2</span>{{ t("选择外观") }}</h3>
            <p>{{ t("点一下即可预览，进入软件后再慢慢调也可以。") }}</p>
          </div>
          <div class="onboarding-themes">
            <button v-for="theme in themeChoices" :key="theme.id" type="button" :data-theme-choice="theme.id"
              class="onboarding-theme" :class="{ selected: onboardingTheme === theme.id }"
              :aria-pressed="onboardingTheme === theme.id" @click="selectOnboardingTheme(theme.id)">
              <span class="theme-swatch" :class="`theme-swatch--${theme.id}`" aria-hidden="true"><i></i><i></i><i></i></span>
              <span class="onboarding-theme-label">{{ theme.label }}</span>
              <Check v-if="onboardingTheme === theme.id" :size="15" />
            </button>
          </div>
        </section>

        <footer class="onboarding-foot">
          <p class="note">{{ t("选好语言后，点击下一步。") }}</p>
          <p v-if="onboardingError" class="modal-error" role="alert">{{ onboardingError }}</p>
          <button class="primary onboarding-next" :disabled="!onboardingLanguage || onboardingBusy" @click="onboardingStep = 2">
            {{ t("下一步") }}
          </button>
        </footer>
      </template>

      <template v-else>
        <p class="onboarding-kicker">{{ t("首次设置") }}</p>
        <h2 id="onboarding-title">{{ t("三步开始") }}</h2>
        <p class="onboarding-lead">{{ t("按下面的顺序做，完成后主界面就会显示设备和剪贴板历史。") }}</p>

        <ol class="onboarding-steps">
          <li>
            <span class="onboarding-step">1</span>
            <div><strong>{{ t("在另一台设备上装 ClipSync") }}</strong><span>{{ t("同一个局域网里最省事：装好后两台会互相发现。") }}</span></div>
          </li>
          <li>
            <span class="onboarding-step">2</span>
            <div><strong>{{ t("配对") }}</strong><span>{{ t("在这台设备上显示二维码让对方扫，或输入对方的配对码；确认之后两台才算互相信任。") }}</span></div>
          </li>
          <li>
            <span class="onboarding-step">3</span>
            <div><strong>{{ t("复制一次试试") }}</strong><span>{{ t("在任何应用里复制一段文字，两台设备的历史里都会出现同一条。") }}</span></div>
          </li>
        </ol>

        <p class="onboarding-note">{{ t("这台设备还没有配对的机器，也没有任何历史——上面三步做完，这一页就会显示它们。") }}</p>
        <div class="onboarding-tip">
          <Info :size="17" />
          <p>{{ t("关闭这个窗口不会退出 ClipSync：它会留在托盘里继续同步剪贴板。要真正退出，请用托盘菜单里的「退出」。") }}</p>
        </div>

        <footer class="onboarding-foot onboarding-foot--split">
          <button type="button" class="text-button" :disabled="onboardingBusy" @click="onboardingStep = 1">{{ t("上一步") }}</button>
          <div class="onboarding-finish-group">
            <p v-if="onboardingError" class="modal-error" role="alert">{{ onboardingError }}</p>
            <button class="primary onboarding-finish" :disabled="onboardingBusy" @click="finishOnboarding">
              {{ onboardingBusy ? t("正在保存…") : t("完成，进入 ClipSync") }}
            </button>
          </div>
        </footer>
      </template>
    </dialog>
    <!-- The whole window is the drop target, so the hint is drawn over the
         whole window: a file dragged in from the desktop can land anywhere on
         it, and a hint drawn on one card would be a target the reader cannot
         aim at.  It never takes a pointer event, so the drop still lands on the
         window however the veil is drawn. -->
    <div v-if="dropActive" class="drop-veil" aria-hidden="true">
      <div class="drop-veil-card">
        <FileDown :size="28" />
        <strong>{{ t("松开后在“文件传输”页选择设备") }}</strong>
        <span class="note">{{ t("拖放不会自动发送，设备仍由您指定") }}</span>
      </div>
    </div>
    <!-- One menu for the whole window, after every dialog so it paints above
         the page and below the modals. -->
    <ContextMenu />
  </div>
</template>
