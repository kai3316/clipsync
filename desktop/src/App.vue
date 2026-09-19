<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  History, Monitor, Search, RefreshCw, Pin, Trash2, ChevronLeft, ChevronRight, Eraser, FileDown, Download,
  ShieldCheck, ShieldOff, LockKeyhole, AlertCircle, X, LogOut, Circle, Copy, Check, Link, Unlink, PinOff, Star, Settings as SettingsIcon, Save, FileUp, FolderOpen, MessageCircle, Plug, PlugZap, RotateCcw, Activity, Fingerprint, Globe, SendHorizontal, Stethoscope, Wrench, Info, QrCode, ExternalLink, Wand2, Sparkles, Clock, Smartphone, Pencil,
} from "@lucide/vue";
import logo from "../../assets/icon.svg";
import { bridge, inDesktop } from "./api/bridge";
import type { Device, DeviceCertificate, DeviceProbeResult, DiagnosticAction, DiagnosticCheck, DiagnosticItem, DiagnosticsReport, HistoryItem, HistoryPreview, HistoryPreviewFile, RelayTestResult } from "./api/types";
import { LOCALES, LOCALE_NAMES, currentLocale, setLocale, t } from "./i18n";
import { dateTime, isPlaceholderPreview, previewText, size } from "./i18n/format";
import { createApplicationStore } from "./stores/application";
import { aiCompareState, aiEntryKey, aiDiffCounts, buildAiLocalIndex } from "./lib/aiconfig-diff";
import { aiItemCount, aiTreeGroups, type AiGroup, type AiNode, type AiRow } from "./lib/aiconfig-tree";
import { aiTargets, type AiTarget } from "./lib/aiconfig-targets";
import { formatPairingCode, isPairingCodeComplete } from "./lib/pairing-code";
import { openContextMenu, type ContextMenuItem } from "./lib/context-menu";
import { copyText } from "./lib/clipboard";
import { announce, clearStatus, statusMessage } from "./lib/status";
import { deliveryIcon, deliveryLabel } from "./stores/delivery";
import FavoritesView from "./components/FavoritesView.vue";
import TransfersView from "./components/TransfersView.vue";
import ChatView from "./components/ChatView.vue";
import OverviewView from "./components/OverviewView.vue";
import NoticeStack from "./components/NoticeStack.vue";
import ContextMenu from "./components/ContextMenu.vue";

const store = createApplicationStore();
const { state } = store;
/** Which page is on screen.  Named by the same list the sidebar draws from, so
 * a page cannot exist in one and not the other.  The window opens on the
 * overview, which is where the legacy window opened too: it is the one page
 * that answers "what is this machine doing" without being asked. */
const tab = ref<(typeof PAGES)[number]>("overview");
/** The history page's search box, so Ctrl+F has something to focus. */
const searchInput = ref<HTMLInputElement | null>(null);
const settings = ref<Record<string, any>>({});
// A computed, not a plain array: the labels are translated, so they must
// re-evaluate when the locale changes.
const filterCategories = computed<Array<[string, string]>>(() => [
  ["credit_card", t("信用卡号")], ["ssn", t("社会保障号")], ["api_key", t("API 密钥")],
  ["email", t("电子邮箱")], ["private_key", t("私钥")], ["password", t("密码")],
]);
const settingsBusy = ref(false);
const settingsSaved = ref(false);
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
// it talks to.  That leaves four groups over eleven cards, and no group holding
// a single entry.
const settingsGroups = computed<Array<{ label: string; items: Array<{ id: string; label: string }> }>>(() => [
  { label: t("通用"), items: [
    { id: "general", label: t("常规") },
    { id: "history", label: t("剪贴板历史") },
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
 * The page is eleven cards long and each of them is a form's worth of
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
/** How many cards the query reaches. */
const settingsMatchTotal = computed(() => Object.keys(settingsMatches.value).length);
/** How many of a group's cards hold the query.  The group tabs need it for the
 * same reason the cards do: while a query is up the page shows the cards that
 * answered it, and a match inside a group the reader is not looking at would
 * otherwise be a match nothing on screen points to. */
function settingsGroupMatches(group: { items: Array<{ id: string }> }): number {
  return group.items.reduce((total, item) => total + (settingsMatches.value[item.id] || 0), 0);
}
/** Whether any card in this group holds an edit the save has not written.  The
 * dot says where the edit is; across groups it is the only thing that can. */
function settingsGroupHasEdits(group: { items: Array<{ id: string }> }): boolean {
  return group.items.some(item => cardHasEdits(item.id));
}
/** The group whose cards the strip is listing.
 *
 * Derived from the card on screen rather than kept beside it, because the two
 * are one fact: a group is only ever the set its cards are in, so the strip
 * cannot say 通用 while the card below it is 安全 — that is a strip that has
 * lost the reader's place.  A query is the one thing that moves it, and moves
 * it to the group holding the first match: the page is showing the cards that
 * answered it, so the row of sections must show the one whose count is up
 * there rather than a group whose every card is dimmed.
 */
const activeSettingsGroup = computed(() => {
  if (settingsSearching.value && settingsMatchTotal.value) {
    const holding = settingsGroups.value.find(group => settingsGroupMatches(group) > 0);
    if (holding) return holding;
  }
  return settingsGroups.value.find(group => group.items.some(item => item.id === settingsSection.value))
    || settingsGroups.value[0];
});
/** The form the search reads.  A template ref rather than a `document` lookup:
 * the settings page is one tab of several, and in a test it is not attached to
 * the document at all. */
const settingsForm = ref<HTMLFormElement | null>(null);
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
/** Open a group from the strip: the first card in it, which is the one the
 * section row under the tab is listing.
 *
 * A group is a step on the way to a card rather than a card of its own, so it
 * has to land somewhere — and landing on its first card is what keeps the two
 * rows agreeing about where the reader is.  The group already holding the card
 * on screen is left alone: its sections are listed under the tab, and there is
 * nothing to move the reader to.  A query is the exception in both directions,
 * exactly as it is one row below: a group tab opens a card, and a control that
 * could not be opened would be a control that does nothing.
 */
function openSettingsGroup(group: { items: Array<{ id: string }> }) {
  if (!settingsSearching.value && group.items.some(item => item.id === settingsSection.value)) return;
  openSettingsCard(group.items[0].id);
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
 */
function settingsHits(root: Element, query: string): Element[] {
  const hits: Element[] = [];
  for (const element of Array.from(root.querySelectorAll("*"))) {
    const text = (element.textContent || "").toLowerCase();
    if (!text.includes(query)) continue;
    // The deepest element only: a card whose text matched because a row inside
    // it did is the same match twice, and a nested card would otherwise report
    // a number several times its size.
    const carriesItBelow = Array.from(element.children).some(
      (child) => (child.textContent || "").toLowerCase().includes(query),
    );
    if (!carriesItBelow) hits.push(element);
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
watch(settingsQuery, () => applySettingsSearch(settingsQuery.value), { flush: "post" });
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
const passwordBlocked = computed(() => securityPassword.value.length > 0 && (!passwordValid.value || passwordMismatch.value));
const clearPasswordOpen = ref(false);
const clearPasswordDialog = ref<HTMLDialogElement | null>(null);
const factoryResetOpen = ref(false);
const factoryResetDialog = ref<HTMLDialogElement | null>(null);
const recoverOpen = ref(false);
const recoverDialog = ref<HTMLDialogElement | null>(null);
// The sidecar reports DATA_INVALID when it refuses to start on its own data
// directory; nothing else it can report is repaired the same way.
const dataInvalid = computed(() => state.error?.code === "DATA_INVALID");
watch(recoverOpen, async (open) => {
  await nextTick();
  if (open) recoverDialog.value?.showModal();
  else recoverDialog.value?.close();
});
async function confirmRecover() {
  if (state.pending) return;
  recoverOpen.value = false;
  await store.recoverData();
}
watch(clearPasswordOpen, async (open) => {
  await nextTick();
  if (open) clearPasswordDialog.value?.showModal();
  else clearPasswordDialog.value?.close();
});
watch(factoryResetOpen, async (open) => {
  await nextTick();
  if (open) factoryResetDialog.value?.showModal();
  else factoryResetDialog.value?.close();
});
async function clearPassword() {
  if (securityBusy.value) return;
  clearPasswordOpen.value = false;
  securityBusy.value = true;
  try {
    // An empty password means "unchanged", so clearing needs the explicit
    // action key (same payload the web panel sends).
    const result = await bridge.updateSettings({ password: "", clear_password: true }) as { password_set?: boolean };
    settings.value.password_set = result.password_set === true;
    securityPassword.value = "";
    securityPasswordConfirm.value = "";
  } catch (error) { state.error = error as any; }
  finally { securityBusy.value = false; }
}
async function confirmFactoryReset() {
  if (securityBusy.value) return;
  factoryResetOpen.value = false;
  securityBusy.value = true;
  try {
    // The host deletes every data file, clears the webview's own storage and
    // relaunches: nothing after this call is guaranteed to run.
    await bridge.factoryReset();
  } catch (error) {
    securityBusy.value = false;
    state.error = error as any;
  }
}
const companion = ref<Awaited<ReturnType<typeof bridge.companionStatus>> | null>(null);
const companionPort = ref(8080);
const companionBusy = ref(false);
let companionGeneration = 0;
const companionRotatePending = ref(false);
const companionRotateDialog = ref<HTMLDialogElement | null>(null);
watch(companionRotatePending, async (pending) => {
  await nextTick();
  if (pending) companionRotateDialog.value?.showModal();
  else companionRotateDialog.value?.close();
});
async function rotateCompanionToken() {
  if (!companionRotatePending.value || companionBusy.value || !companion.value?.running) return;
  companionRotatePending.value = false;
  await configureCompanion(true, true);
}
const companionClearPending = ref(false);
const companionClearDialog = ref<HTMLDialogElement | null>(null);
watch(companionClearPending, async (pending) => {
  await nextTick();
  if (pending) companionClearDialog.value?.showModal();
  else companionClearDialog.value?.close();
});
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
  } catch (error) {
    if (generation === companionGeneration) state.error = error as any;
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
  } catch (error) {
    if (generation !== companionGeneration) return;
    state.error = error as any;
    await refreshCompanion();
  } finally { companionBusy.value = false; }
}
const settingsLoaded = ref(false);
let settingsLoading = false;
const savedAutoStart = ref(false);
// Same idea as savedAutoStart: sending encryption_enabled on every save would
// rebuild the live encryption manager (and re-derive the relay channels) each
// time, so it only travels when the toggle actually changed.
const savedEncryptionEnabled = ref(false);
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
const restoreDialog = ref<HTMLDialogElement | null>(null);
watch(restorePath, async (path) => {
  await nextTick();
  if (path) restoreDialog.value?.showModal();
  else restoreDialog.value?.close();
});
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
    settings.value.translate_key_set = result.translate_key_set;
    translationKey.value = "";
  } catch (error) { state.error = error as any; }
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
const translateItemDialog = ref<HTMLDialogElement | null>(null);
const translateItemText = ref("");
const translateItemTruncated = ref(false);
const translateItemResult = ref("");
const translateItemReading = ref(false);
const translateItemBusy = ref(false);
let translateItemGeneration = 0;
watch(translateItemOpen, async (open) => {
  await nextTick();
  if (open) translateItemDialog.value?.showModal();
  else {
    // Invalidates an in-flight translation, so a late response cannot land in
    // a dialog the user has already dismissed.
    ++translateItemGeneration;
    translateItemDialog.value?.close();
  }
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
    state.error = error as any;
  } finally { translateItemReading.value = false; }
}
/** Whether a history row is a web link, and so gets the legacy 在浏览器打开.
 *
 * Legacy decided from the row's text (`linkUrl`); the sidecar decides again
 * from the *whole* clip before opening anything, so a preview that merely looks
 * like one still opens nothing.
 */
function isWebLink(item: HistoryItem) {
  return item.content_type === "URL" || /^https?:\/\//i.test(item.preview || "");
}
async function openHistoryLink(item: HistoryItem) {
  const result = await store.openLink(item);
  if (result?.opened) store.toast("ui.history", t("已在浏览器打开：{url}", { url: result.url }));
}

/** Whether a row is a file that lives on another device.
 *
 * Such a row has no bytes here — only the name, the size and the id of the
 * entry that published it — so it is the one row whose action is 下载 rather
 * than 复制.  Reading it off `content_type` and not the preview keeps that a
 * property of the row rather than of how its name happens to look.
 */
function isRemoteFile(item: HistoryItem) {
  return (item.content_type || "").toUpperCase() === "FILE_REMOTE";
}
/** The device a remote-file row came from, when this window can see it online.
 *
 * Not a gate on the button — the sidecar is the one that knows whether the peer
 * is reachable, and it answers the click either way — but it is what the tooltip
 * says, so the reason a download is about to fail is readable before the click
 * rather than only after it.  A device this window cannot see at all counts as
 * offline: the row would not have been offered otherwise, and saying it is not
 * online is the nearest true thing left.
 */
function remoteFileDevice(item: HistoryItem) {
  const id = item.source_device || "";
  if (!id) return undefined;
  return state.devices.find((device) => device.id === id && device.connection_state === "online");
}
function remoteFileBusy(item: HistoryItem) {
  return state.remoteFilePending.includes(`${item.source_device || ""}:${item.id}`);
}
function remoteFileTitle(item: HistoryItem) {
  if (remoteFileBusy(item)) return t("已请求下载，正在等待那台设备");
  if (!remoteFileDevice(item)) return t("{name} 当前不在线", { name: item.source_name || t("未知") });
  return t("从 {name} 下载这个文件", { name: item.source_name || t("未知") });
}
async function downloadRemoteFile(item: HistoryItem) {
  if (await store.downloadRemoteFile(item)) {
    store.toast("ui.transfers", t("已请求下载，对方开始发送后可在文件传输里查看"));
  }
}

/** The right-click menu on a history row, in the legacy menu's own order.
 *
 * Every entry here is the row's own button under a different name — the menu
 * is not a second set of capabilities, it is the same set reachable without
 * hunting along the row.  That is why the conditions match the buttons' too:
 * 翻译 stands down while a translate is already in flight, 打开链接 appears
 * only for something the sidecar would actually open, and a row with no id
 * cannot be pinned or deleted.
 */
function historyMenu(event: MouseEvent, item: HistoryItem) {
  const hasId = !!item.id.trim();
  openContextMenu(event, [
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
    isRemoteFile(item)
      ? {
          id: "download-file", label: t("下载文件"), icon: Download,
          disabled: !remoteFileDevice(item) || remoteFileBusy(item),
          run: () => downloadRemoteFile(item),
        }
      : null,
    {
      id: "pin", label: item.pinned ? t("取消置顶") : t("置顶"),
      icon: item.pinned ? PinOff : Pin, disabled: !hasId,
      run: () => store.pin(item),
    },
    {
      id: "favorite", label: t("添加到收藏"), icon: Star, disabled: !hasId,
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
  const added = await store.batchFavorite([item.id]);
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
 * them.  重命名 writes the same alias the row's 设备备注 field does.
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

function deviceMenu(event: MouseEvent, device: Device) {
  if (device.relay) {
    openContextMenu(event, [
      { id: "chat", label: t("打开聊天"), icon: MessageCircle, run: () => chatWith(device) },
      { id: "rename", label: t("重命名"), icon: Pencil, run: () => renameRelayDevice(device) },
      { id: "copy-id", label: t("复制设备 ID"), icon: Copy, run: () => copyText(device.id) },
      { id: "unpair", label: t("解除互联网配对"), icon: Unlink, divider: true, danger: true, run: () => { relayUnpairDevice.value = device; } },
    ]);
    return;
  }
  const connected = device.connection_state === "online";
  // This row can answer for two pairings at once — see `relayPairing` — so the
  // entries below are drawn per route and not per row.  连接/断开 and 移除设备
  // belong to the local pairing; 打开聊天 and 解除互联网配对 reach the device over
  // the relay and are owed to a reader whose only pairing with it is the code.
  const relay = relayPairing(device);
  openContextMenu(event, [
    device.paired
      ? {
          id: connected ? "disconnect" : "connect",
          label: connected ? t("断开连接") : t("连接"),
          icon: connected ? PlugZap : Plug,
          // A device that is not reachable cannot be dialed, which is the rule
          // the row's own connect button already applies — see `canDial`.
          disabled: !connected && !canDial(device),
          run: () => (connected ? store.disconnect(device) : store.connect(device)),
        }
      : null,
    (device.paired || relay.paired) && !connected
      ? { id: "chat", label: t("打开聊天"), icon: MessageCircle, run: () => chatWith(device) }
      : null,
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
          disabled: busy.value || !!updateBusyId.value || !device.update_cached || !updateReachable(device),
          title: device.update_cached
            ? t("把本机的安装包发送给该设备")
            : t("本机还没有安装包可发送：本机只保留自己升级时下载的那一个，对方可自行检查更新"),
          run: () => offerDeviceUpdate(device),
        }
      : null,
    device.update_fetchable
      ? {
          id: "fetch-update", label: t("获取更新"), icon: Download,
          disabled: busy.value || !!fetchBusyId.value || !updateReachable(device),
          title: t("从该设备获取新版本安装包并安装"),
          run: () => fetchDeviceUpdate(device),
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
      ? { id: "relay-unpair", label: t("解除互联网配对"), icon: Unlink, divider: true, danger: true, run: () => { relayUnpairDevice.value = device; } }
      : null,
    // The divider only on the first of the two dangerous entries, so a device
    // holding both pairings gets one separator rather than two in a row.
    device.paired
      ? { id: "forget", label: t("移除设备"), icon: Trash2, divider: !relay.paired, danger: true, run: () => { forgetDevice.value = device; } }
      : null,
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

/** 重命名 on a device row, which is one field or the other depending on which
 *  pairing the row holds.
 *
 * A note lives on a saved local peer; a device paired by code has no such peer,
 * so the note call answers NOT_FOUND and the menu entry could only fail.  That
 * device's name is its alias, which is the field the pairing card's own rename
 * writes — one name per device either way, and the dialog is the same one.
 */
function renameDeviceRow(device: Device) {
  if (device.paired) {
    renameTarget.value = { kind: "device", id: device.id };
    renameValue.value = device.note || "";
    return;
  }
  renameRelayDevice(device);
}

/** Open a conversation with a device from its own row.
 *
 * Same call the chat page's 附近设备 list makes, and the same answer: a session
 * id means the link was up and the conversation is open, anything else means
 * the invite is still dialing — in which case the chat page is where it will
 * appear, and it is opened either way so the reader can watch it arrive.
 */
async function chatWith(device: Device) {
  try {
    const result = await bridge.inviteChat(device.id, device.name);
    chatSessionToOpen.value = result?.chat_session_id || "";
    openPage("chat");
  } catch (error: any) {
    announce(error?.message || t("发送邀请失败"));
  }
}

/** A row's kind in words, the way the panel's row named it.
 *
 * The wire name is not a label: a Chinese user read 图片 where this window said
 * "IMAGE". Legacy named the same kinds and left the rest as the type they were
 * (`history-item.js::typeLabel`), which is what the fallback does — HTML and RTF
 * are the format's own name in either language.
 */
function typeLabel(item: HistoryItem) {
  const kind = String(item.content_type || "").toUpperCase();
  if (kind === "TEXT") return t("文本");
  if (kind === "URL" || kind === "LINK") return t("链接");
  if (kind === "FILE") return t("文件");
  // A file on another device is a file; the row's own actions already say which
  // machine it is on, so the label does not have to.
  if (kind === "FILE_REMOTE") return t("文件");
  if (kind === "IMAGE" || kind === "IMAGE_PNG" || kind === "IMAGE_EMF") return t("图片");
  return kind || t("内容");
}

/** A row's preview as it is shown.
 *
 * Falls back to the kind in brackets for a row the sidecar sent no preview at
 * all for, which is what the legacy panel shows — the label is translated by
 * `previewText`, the bracketed kind is not, because it is a format name.
 */
function previewLabel(item: HistoryItem) {
  return previewText(item.preview) || `[${item.content_type || t("内容")}]`;
}

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
  return [label === previewLabel(item) ? "" : label, item.pinned ? t("已收藏") : ""].filter(Boolean).join(" · ");
}

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

function rememberPreview(key: string, card: HistoryPreview) {
  previewCache.set(key, card);
  // A Map iterates in insertion order, so the entry dropped is the one that has
  // been in the cache longest.
  while (previewCache.size > PREVIEW_CACHE_MAX) {
    const oldest = previewCache.keys().next().value;
    if (oldest === undefined) break;
    previewCache.delete(oldest);
  }
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
  const cached = previewCache.get(previewKey(item));
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
  if (previewCard.value?.id === item.id) previewCard.value = null;
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
    if (generation === translateItemGeneration) state.error = error as any;
  } finally { translateItemBusy.value = false; }
}
const aiTools = ref<Array<{ key: string; label: string }>>([]);
const aiEnabled = ref<string[]>([]);
const aiCustomPaths = ref("");
const aiLocalItems = ref<Array<Record<string, any>>>([]);
const aiMessage = ref("");
const aiSelectedItem = ref<Record<string, any> | null>(null);
const aiEditorContent = ref("");
const aiSavedContent = ref("");
const aiEditorDirty = computed(() => !!aiSelectedItem.value && aiEditorContent.value !== aiSavedContent.value);
const aiNextFile = ref<Record<string, any> | null>(null);
const aiDiscardDialog = ref<HTMLDialogElement | null>(null);
watch(aiNextFile, async (item) => {
  await nextTick();
  if (item) aiDiscardDialog.value?.showModal();
  else aiDiscardDialog.value?.close();
});
function requestAiRead(item: Record<string, any>) {
  if (aiMutationBusy.value) return;
  if (aiEditorDirty.value) aiNextFile.value = item;
  else void readAiLocal(item);
}
async function discardAiChanges() {
  const item = aiNextFile.value;
  aiNextFile.value = null;
  if (item) await readAiLocal(item);
}
let aiReadGeneration = 0;
const aiMutationBusy = ref(false);
const aiTrashItem = ref<Record<string, any> | null>(null);
const aiTrashDialog = ref<HTMLDialogElement | null>(null);
watch(aiTrashItem, async (item) => {
  await nextTick();
  if (item) aiTrashDialog.value?.showModal();
  else aiTrashDialog.value?.close();
});
async function confirmAiTrash() {
  if (aiTrashItem.value) await trashAiLocal(aiTrashItem.value);
}
function sameAiFile(left: Record<string, any> | null, right: Record<string, any>) {
  return !!left && left.tool === right.tool && (left.root || "") === (right.root || "") &&
    left.rel_path === right.rel_path;
}
const aiLocalPage = ref(0);
const aiRemotePage = ref(0);
const aiPeerId = ref("");
/** Every peer's last-known inventory, keyed by device id.  The card shows one
 * device at a time, but the picker has something to say about all of them — and
 * a single read already answers for every peer the runtime has heard from, so
 * keeping only the selected peer's list was throwing the rest away. */
const aiInventories = ref<Record<string, { entries: Array<Record<string, any>>; legacy: boolean }>>({});
/** The selected device's list, which is what the rows, the ticks and the diff
 * below are about.  Derived from the cache rather than stored beside the peer id
 * so that switching devices cannot leave the previous peer's rows on screen
 * under the new peer's name. */
const aiRemoteItems = computed(() => aiInventories.value[aiPeerId.value]?.entries ?? []);
/** Whether this peer speaks a pre-v3 protocol, which sends no usable root id and
 * so must be compared by path alone. */
const aiRemoteLegacy = computed(() => aiInventories.value[aiPeerId.value]?.legacy === true);
/** Whether a peer is known to be pre-v3, by id rather than by selection.
 *
 * The wizard lists every paired device, and legacy-ness is a property of the
 * inventory a peer reports, so it is simply unknown until that peer has
 * answered — which for a device nobody has read is not until it is selected.
 * What is known is honoured; what is not is discovered by the note below
 * rather than assumed either way. */
function aiPeerIsLegacy(peerId: string): boolean {
  return aiInventories.value[peerId]?.legacy === true;
}
/** Whether this peer has actually answered with an inventory.  An unread peer and
 * a peer with no config files both draw an empty list, and they are not the same
 * thing: a key that is absent means nobody has asked it yet. */
const aiRemoteRead = computed(() => aiPeerId.value in aiInventories.value);
const aiRemoteMessage = ref("");
const aiRemoteWaiting = ref(false);
/** Keys of the remote entries ticked for a batch pull.  Kept as keys rather than
 * as row indexes so a refresh that reorders or shortens the list cannot silently
 * move a tick onto a different file: a key that is no longer present is dropped. */
const aiRemoteSelected = ref<string[]>([]);
const aiPullMode = ref<"copy" | "overwrite" | "append">("copy");
/** The confirmation, for whatever is about to be written — one row's button and
 * the batch button take the same path so the warning cannot be true of one and
 * not the other.  It carries both halves on purpose: `targets` is what the
 * reader is told they are about to write, counted the way the list counts, and
 * `items` is the files the peer is actually asked for. */
const aiPullPending = ref<{
  targets: AiTarget[];
  items: Array<Record<string, any>>;
  mode: string;
} | null>(null);
const aiPullDialog = ref<HTMLDialogElement | null>(null);
/** One row per page of the remote list; the page and the select-all checkbox read
 * the same slice so "select this page" cannot mean two different sets. */
const AI_PAGE_SIZE = 40;
/** What the filter boxes hold.  The filter matches the path, not the tool: the
 * tool is named by the header over its own rows, so a reader who wants one tool
 * has the header to look at, and typing a path is what the placeholder says. */
const aiLocalQuery = ref("");
const aiRemoteQuery = ref("");
/** The folders the reader has opened, one map per list — a folder not in it is
 * folded, which is where every folder starts.  The list is a list of config
 * items, and the files inside a folder are that folder's own internals: a skill
 * is one row until the reader asks to see what is in it.
 *
 * Fold state is kept here rather than on the nodes because the tree is rebuilt
 * on every inventory read: a flag on a node would be thrown away with it, and a
 * refresh would spring every folder open under the reader. */
const aiLocalExpanded = ref<Record<string, boolean>>({});
const aiRemoteExpanded = ref<Record<string, boolean>>({});
/** The tools' own order and names, taken from the same profiles the checkboxes
 * above the list are drawn from, so the list and the checkboxes agree. */
const aiToolKeys = computed(() => aiTools.value.map(tool => tool.key));
function aiToolLabel(key: string) {
  return aiTools.value.find(tool => tool.key === key)?.label || key;
}
const aiLocalGroups = computed(() => aiTreeGroups(aiLocalItems.value, {
  expanded: aiLocalExpanded.value, query: aiLocalQuery.value,
  order: aiToolKeys.value, label: aiToolLabel,
}));
const aiRemoteGroups = computed(() => aiTreeGroups(aiRemoteItems.value, {
  expanded: aiRemoteExpanded.value, query: aiRemoteQuery.value,
  order: aiToolKeys.value, label: aiToolLabel,
}));
const aiLocalRows = computed(() => aiLocalGroups.value.flatMap(group => group.rows));
const aiRemoteRows = computed(() => aiRemoteGroups.value.flatMap(group => group.rows));
const aiLocalPageCount = computed(() => Math.max(1, Math.ceil(aiLocalRows.value.length / AI_PAGE_SIZE)));
const aiRemotePageCount = computed(() => Math.max(1, Math.ceil(aiRemoteRows.value.length / AI_PAGE_SIZE)));
const aiLocalPageItems = computed(() =>
  aiLocalRows.value.slice(aiLocalPage.value * AI_PAGE_SIZE, (aiLocalPage.value + 1) * AI_PAGE_SIZE));
const aiRemotePageItems = computed(() =>
  aiRemoteRows.value.slice(aiRemotePage.value * AI_PAGE_SIZE, (aiRemotePage.value + 1) * AI_PAGE_SIZE));
/** The group a page row belongs to, for the header drawn when the tool changes.
 * A group the row's tool is somehow missing from falls back to the row's own
 * name and no count, rather than drawing a header with nothing in it. */
function aiGroupFor(groups: AiGroup[], tool: string): AiGroup | null {
  return groups.find(group => group.key === tool) ?? null;
}
/** A row's full path.  A file row is its own entry; a folder is the path the
 * inventory listed it under, or — for one the tree built from its children —
 * the path those children hang from. */
function aiRowPath(row: AiRow): string {
  return String(row.entry?.rel_path ?? row.node?.path ?? row.label);
}
/** What a row's own buttons act on.  A folder the tree built carries no entry,
 * so it is described by where it is — which is what the runtime takes for a
 * folder either way. */
function aiRowItem(row: AiRow): Record<string, any> {
  if (row.entry) return row.entry;
  return { tool: row.tool, root: row.root, rel_path: aiRowPath(row), is_dir: true };
}
/** Whether a folder is open: the reader opened it, and a folder that is not a
 * folder is not.  The row's key is its node's key, so the fold map is looked up
 * by the row rather than through the node. */
function aiFolderOpen(row: AiRow, map: Record<string, boolean>): boolean {
  return !!(row.isDir && map[row.key]);
}
/** The map with one folder's fold turned over, as a new object: the tree is a
 * computed, so it has to see a new value to redraw. */
function toggledFolder(map: Record<string, boolean>, key: string): Record<string, boolean> {
  const next = { ...map };
  if (next[key]) delete next[key];
  else next[key] = true;
  return next;
}
function toggleAiLocalFolder(node: AiNode | null) {
  if (node) aiLocalExpanded.value = toggledFolder(aiLocalExpanded.value, node.key);
}
function toggleAiRemoteFolder(node: AiNode | null) {
  if (node) aiRemoteExpanded.value = toggledFolder(aiRemoteExpanded.value, node.key);
}
// Narrowing the list must not leave the reader on a page that no longer exists:
// the page counts in the footer are computed from the filtered list, so a page
// number past its end would render an empty list with a live "next" button.
watch([aiLocalQuery, aiRemoteQuery], () => { aiLocalPage.value = 0; aiRemotePage.value = 0; }, { flush: "sync" });
// The same has to hold for a list that shrinks under the reader — a refresh, or
// a folder folded shut — because the page counts come from the rows that are on
// screen: a page past the end would draw an empty list beside a live "previous".
watch(aiLocalPageCount, count => { if (aiLocalPage.value >= count) aiLocalPage.value = count - 1; });
watch(aiRemotePageCount, count => { if (aiRemotePage.value >= count) aiRemotePage.value = count - 1; });
/** The selection keys one row stands for: a file is itself; a folder is every
 * file beneath it under the same root.  A folder belongs to exactly one root, so
 * a folder of the same name under a sibling root is a different folder and is
 * not swept in — which is why the root is compared, not just the path prefix. */
function aiRowKeys(row: AiRow, items: Array<Record<string, any>> = aiRemoteItems.value): string[] {
  if (!row.isDir) return [aiEntryKey(row.entry)];
  const tool = row.tool || "custom";
  const root = row.root || "";
  const rel = aiRowPath(row);
  const prefix = `${rel.replace(/\/+$/, "")}/`;
  return items
    .filter(entry => !entry.is_dir &&
      (entry.tool || "custom") === tool && (entry.root || "") === root &&
      (String(entry.rel_path || "") === rel || String(entry.rel_path || "").startsWith(prefix)))
    .map(aiEntryKey);
}
/** How much of a folder is ticked: all of it, part of it, or none.  Null for a
 * folder with no files under it, which has nothing to tick either way. */
function aiFolderState(row: AiRow): "all" | "some" | "none" | null {
  const keys = aiRowKeys(row);
  if (!keys.length) return null;
  const ticked = keys.filter(key => aiRemoteSelected.value.includes(key)).length;
  if (!ticked) return "none";
  return ticked === keys.length ? "all" : "some";
}
/** A folder's box is a shortcut for the files under it, never a tick of its own:
 * ticking it ticks them, leaving the folder's own state to be read off them. */
function toggleAiRemote(row: AiRow) {
  const keys = aiRowKeys(row);
  const all = keys.length > 0 && keys.every(key => aiRemoteSelected.value.includes(key));
  aiRemoteSelected.value = all
    ? aiRemoteSelected.value.filter(key => !keys.includes(key))
    : [...new Set([...aiRemoteSelected.value, ...keys])];
}
const aiRemotePageKeys = computed(() => aiRemotePageItems.value.flatMap(row => aiRowKeys(row)));
const aiRemotePageAllSelected = computed(() =>
  aiRemotePageKeys.value.length > 0 &&
  aiRemotePageKeys.value.every(key => aiRemoteSelected.value.includes(key)));
const aiRemotePageSomeSelected = computed(() =>
  !aiRemotePageAllSelected.value &&
  aiRemotePageKeys.value.some(key => aiRemoteSelected.value.includes(key)));
/** Ticks or clears the whole page at once, leaving selections on other pages
 * alone — the box says "this page", so that is what it must do. */
/** Ticks every file this machine does not have at all.  Only the missing ones:
 * a file that exists on both sides but differs may be newer here, so choosing it
 * on the reader's behalf could overwrite their own later edit — the legacy
 * wizard's default strategy was this same one, and for the same reason. */
function selectAiRemoteMissing() {
  const keys = aiRemoteItems.value
    .filter(item => aiCompareState(aiLocalIndex.value, item, aiRemoteLegacy.value) === "missing")
    .map(aiEntryKey);
  aiRemoteSelected.value = [...new Set([...aiRemoteSelected.value, ...keys])];
}
function toggleAiRemotePage() {
  const pageKeys = aiRemotePageKeys.value;
  aiRemoteSelected.value = aiRemotePageAllSelected.value
    ? aiRemoteSelected.value.filter(key => !pageKeys.includes(key))
    : [...new Set([...aiRemoteSelected.value, ...pageKeys])];
}
/** The ticked entries themselves, in inventory order. */
const aiRemoteSelectedItems = computed(() =>
  aiRemoteItems.value.filter(item => aiRemoteSelected.value.includes(aiEntryKey(item))));
/** The ticked entries as config items: a skill the reader ticked through its
 * folder box is one item, not the twenty-one files the box stood for.  The ticks
 * stay per file — a tick is a choice about a file, and the box is only a
 * shortcut for making all of them — but everything the pull flow says out loud
 * counts these, so the button and the confirmation agree with the list. */
const aiRemoteSelectedTargets = computed(() => aiTargets(aiRemoteSelectedItems.value, () => true));
/** What a pull is about to write, in both units: the targets the reader is told
 * about, and the files the peer is asked for.  One path for the row's button and
 * the batch button, so the confirmation cannot be true of one and not the other. */
function aiPullAbout(items: Array<Record<string, any>>, mode: string) {
  const targets = aiTargets(items, () => true);
  // Every item handed in is a file the reader chose, so the fold can only leave
  // a target empty if there is nothing to write at all — and then there is no
  // confirmation to make.
  return targets.length ? { targets, items, mode } : null;
}
/** The migration wizard.  Its three strategies are the legacy wizard's, and each
 * one is a pair — which items, and how they land — that the backend already
 * implements: `mode: "copy"` writes a file this machine does not have under its
 * real name and one it does have under `<name>.from.<device>`, and
 * `mode: "overwrite"` replaces it and keeps a `.bak`.  So the wizard arranges
 * decisions the pull path can already carry out, rather than adding a path of its
 * own. */
const aiMigrateOpen = ref(false);
const aiMigrateDialog = ref<HTMLDialogElement | null>(null);
const aiMigrateStrategy = ref<"skip" | "copy" | "overwrite">("skip");
const aiMigrateStrategies = computed<Array<{ value: "skip" | "copy" | "overwrite"; label: string }>>(() => [
  { value: "skip", label: t("只补缺失（默认）") },
  { value: "copy", label: t("全部拉取，已有的另存为副本") },
  { value: "overwrite", label: t("全部拉取，覆盖本机文件") },
]);
/** Which of the peer's files each strategy would move.  Per file, because that
 * is the fact the diff knows: "this machine does not have it" is a statement
 * about a file, and so is "this machine's copy is the newer one". */
function aiMigrateQualifies(item: Record<string, any>): boolean {
  const state = aiCompareState(aiLocalIndex.value, item, aiRemoteLegacy.value);
  if (state === null || state === "same") return false;
  return aiMigrateStrategy.value !== "skip" || state === "missing";
}
/** What those files add up to, counted the way the list counts: a skill is one
 * item to migrate, and the files under it are what migrating it moves. */
const aiMigrateTargets = computed(() => aiTargets(aiRemoteItems.value, aiMigrateQualifies));
/** Whether the chosen source cannot be pulled from at all.
 *
 * A pre-v3 peer's inventory carries no root id, so its paths cannot be
 * resolved to this machine's targets — the sidecar refuses the request
 * outright.  The diff above is still true and still readable, which is why the
 * wizard keeps it on screen and says why the last step is closed instead of
 * hiding the device. */
const aiMigrateBlocked = computed(() => aiPeerIsLegacy(aiPeerId.value));
const aiMigrateItems = computed(() => aiMigrateTargets.value.flatMap(target => target.items));
const aiMigrateSummary = computed(() => {
  const count = aiMigrateTargets.value.length;
  if (!count) return t("没有需要迁移的配置项");
  if (aiMigrateStrategy.value === "skip") return t("将补齐本机缺少的 {count} 个配置项", { count });
  if (aiMigrateStrategy.value === "copy") return t("将拉取 {count} 个不同的配置项，本机已有的另存为副本", { count });
  return t("将覆盖 {count} 个不同的配置项，被覆盖的原文件保留为 .bak", { count });
});
async function openAiMigrate() {
  aiMigrateOpen.value = true;
  await nextTick();
  aiMigrateDialog.value?.showModal();
}
// The wizard decides from the diff, so it reads the inventory itself — when it
// opens with a device already chosen, and again if the device is changed in the
// dialog (which clears the list, so there is nothing to decide from until then).
// That read is also what reads the local listing, and both halves are needed to
// decide: a peer inventory the card had already cached is half of the answer, so
// it cannot stand in for the read.
watch([aiMigrateOpen, aiPeerId], () => {
  if (!aiMigrateOpen.value || !aiPeerId.value) return;
  if (aiRemoteItems.value.length && aiLocalIndex.value) return;
  void refreshAiRemote();
});
// Ticking the rows the strategy chose shows the reader what is about to move
// before anything does, and leaves the list in the state the pull came from.
// Reactive rather than set once, so it holds as the inventory lands.
watch([aiMigrateOpen, aiMigrateStrategy, aiMigrateItems], () => {
  if (!aiMigrateOpen.value) return;
  aiRemoteSelected.value = aiMigrateItems.value.map(aiEntryKey);
});
async function startAiMigration() {
  const items = aiMigrateItems.value;
  if (!items.length || aiMigrateBlocked.value) return;
  aiMigrateDialog.value?.close();
  aiMigrateOpen.value = false;
  if (aiMigrateStrategy.value === "skip" || aiMigrateStrategy.value === "copy") {
    await pullAiRemote(items, "copy");
    return;
  }
  // Overwriting replaces local files, which is the one strategy that destroys
  // something: it goes through the same confirmation as any other overwrite.
  aiPullPending.value = aiPullAbout(items, "overwrite");
}
/** Which of the peer's files differ from this machine's, per row and in total.
 * Null until the local listing has been read once: the states below would
 * otherwise call every remote file "missing" and the summary would be a lie
 * for as long as the walk takes. */
const aiLocalIndex = computed(() => (aiLocalItems.value.length ? buildAiLocalIndex(aiLocalItems.value) : null));
const aiDiff = computed(() => aiDiffCounts(aiLocalIndex.value, aiRemoteItems.value, aiRemoteLegacy.value));
/** How many files each device differs from this machine by, for the picker.
 * Null where the answer is not known — a peer nobody has read, or a local walk
 * that has not happened yet.  A zero here would be a claim that the two devices
 * agree, which is exactly the claim the legacy device bar made whenever the
 * local listing had not loaded, and it was wrong then too. */
const aiPeerDiffs = computed<Record<string, number | null>>(() => {
  const totals: Record<string, number | null> = {};
  for (const [id, inventory] of Object.entries(aiInventories.value)) {
    totals[id] = aiLocalIndex.value
      ? aiDiffCounts(aiLocalIndex.value, inventory.entries, inventory.legacy).total
      : null;
  }
  return totals;
});
/** The count a device's own option carries, empty where it is not known. */
function aiPeerDiffSuffix(peerId: string): string {
  const total = aiPeerDiffs.value[peerId];
  return typeof total === "number" ? t("（{count} 项不同）", { count: total }) : "";
}
function aiRowDiff(item: Record<string, any> | null) {
  return aiCompareState(aiLocalIndex.value, item, aiRemoteLegacy.value);
}
function aiRowDiffLabel(item: Record<string, any> | null) {
  const state = aiRowDiff(item);
  if (state === "missing") return t("缺失");
  if (state === "remote_newer") return t("对方较新");
  if (state === "local_newer") return t("本机较新");
  return "";
}

/** The peer's files inside one folder row, by path rather than by the tree.
 *
 * The badge on a folder has to survive the two shapes the list is drawn in: a
 * tree row has children to walk, and a *search* row is a flat entry with no
 * node at all.  The peer's entries are the same set either way, so the folder's
 * own subtree is taken from the inventory by path — which also means the fact a
 * folder's badge shows is the fact its files' badges show, not a second
 * opinion gathered from a different structure. */
function aiFolderEntries(row: AiRow): Array<Record<string, any>> {
  const rel = aiRowPath(row).replace(/\/+$/, "");
  const prefix = rel + "/";
  const tool = row.tool || "custom";
  return aiRemoteItems.value.filter(entry =>
    !entry?.is_dir &&
    (String(entry?.tool) || "custom") === tool &&
    String(entry?.root || "") === row.root &&
    String(entry?.rel_path || "").replace(/\/+$/, "").startsWith(prefix));
}

/** What a folder row's badge says: how many files under it are missing here,
 * newer on the peer, or newer here.
 *
 * A file's state was on the file alone, which is the one place a reader cannot
 * see it from: the folder is folded by default — that is what makes the list a
 * list of config items rather than of every file behind them — so a skill that
 * differs from the peer's looked exactly like one that does not until it was
 * opened.  Nothing is shown for the "same" files: a badge on every folder would
 * make the differing ones harder to find, which is the rule the file rows
 * already follow. */
function aiFolderDiff(row: AiRow): Array<{ state: string; label: string; count: number }> {
  if (!row.isDir) return [];
  const counts = aiDiffCounts(aiLocalIndex.value, aiFolderEntries(row), aiRemoteLegacy.value);
  const badges: Array<{ state: string; label: string; count: number }> = [];
  if (counts.missing) badges.push({ state: "missing", label: t("缺失"), count: counts.missing });
  if (counts.remote_newer) badges.push({ state: "remote_newer", label: t("对方较新"), count: counts.remote_newer });
  if (counts.local_newer) badges.push({ state: "local_newer", label: t("本机较新"), count: counts.local_newer });
  return badges;
}

/** The pull this page is waiting on.
 *
 * A pull is two steps with a gap between them: the sidecar answers with how
 * many requests the peer accepted, and the files themselves arrive afterwards,
 * one event each.  Reported from the answer alone, a pull that worked read as
 * a sent-requests line and then nothing — the files landing changed the
 * inventory silently.  This is the second half: which peer, how many were
 * asked for, and how they have come back. */
const aiPullProgress = ref<{ peerId: string; total: number; done: number; failed: number;
  /** One line per file that did not land: "<rel_path>：<why>".  Kept so the
   * completion message can say why instead of only how many — five files
   * failing on every retry with nothing naming the reason is what a bare count
   * looked like. */
  failures: string[] } | null>(null);
/** How the last pull ended, for the line that reports it: empty while a pull is
 * running or before one has. */
const aiPullOutcome = ref<"" | "done" | "failed">("");
watch(aiPullPending, async (pending) => {
  await nextTick();
  if (pending) aiPullDialog.value?.showModal();
  else aiPullDialog.value?.close();
});
function requestAiPull(item: Record<string, any>) {
  if (aiPullMode.value === "copy") void pullAiRemote([item], "copy");
  else aiPullPending.value = aiPullAbout([item], aiPullMode.value);
}
/** Pulls every ticked entry as one request, so the peer writes them in one pass
 * and the progress events share a batch id instead of arriving as N unrelated
 * writes. */
function requestAiPullBatch() {
  const items = aiRemoteSelectedItems.value;
  if (!items.length) return;
  if (aiPullMode.value === "copy") void pullAiRemote(items, "copy");
  else aiPullPending.value = aiPullAbout(items, aiPullMode.value);
}
async function confirmAiPull() {
  const pending = aiPullPending.value;
  aiPullPending.value = null;
  if (pending) await pullAiRemote(pending.items, pending.mode);
}
let aiRemoteGeneration = 0;
let aiInventoryGeneration = 0;
watch(() => state.aiInventoryEvent, (update) => {
  if (!update || !aiPeerId.value) return;
  if (update.event.type === "resync" || update.event.data?.peer_id === aiPeerId.value) {
    void refreshAiRemote(false);
  }
});
watch(() => state.updatePeerEvent, (answer) => {
  // A device's answer to the update exchange, replacing the note the click left
  // on its row. The store worded it; this only puts it where the click was.
  if (!answer || !answer.device_id) return;
  updateNotes.value = { ...updateNotes.value, [answer.device_id]: answer.message };
});
watch(aiPeerId, () => {
  aiPullPending.value = null;
  ++aiRemoteGeneration;
  ++aiInventoryGeneration;
  // The list itself is not cleared here: it belongs to the device, and the
  // picker's other legs still show what each device last told us.  What is
  // cleared is everything that was about the peer the reader has left.
  aiRemoteMessage.value = "";
  aiRemoteWaiting.value = false;
  aiRemotePage.value = 0;
  aiRemoteSelected.value = [];
  // A pull belongs to the peer it was asked of: the files of one still arriving
  // are not this page's to report once the reader is looking at another device.
  aiPullProgress.value = null;
  aiPullOutcome.value = "";
}, { flush: "sync" });
watch(() => state.devices.filter(device => device.paired).map(device => device.id), (ids) => {
  if (aiPeerId.value && !ids.includes(aiPeerId.value)) aiPeerId.value = "";
}, { flush: "sync" });
/** The pairing card's own view of the sidecar's status.
 *
 * `peers` are devices the pairing actually completed; `waiting` are codes
 * entered here whose partner has not answered yet — a provisional 4-char tag,
 * not a device, which is why the two are drawn apart.
 */
const internetPairing = ref<{ generated_code?: string | null; relay?: string; enabled?: boolean; peers: Array<any>; waiting?: Array<any> }>({ peers: [] });
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
/** A whole code, however the reader has typed or pasted it: see
 * `lib/pairing-code.ts` for why the field is not simply a text box. */
const internetPairingComplete = computed(() => isPairingCodeComplete(internetPairingCode.value));
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
const deleteDialog = ref<HTMLDialogElement | null>(null);
const batchDeleteIds = ref<string[] | null>(null);
const batchDeleteDialog = ref<HTMLDialogElement | null>(null);
const clearHistoryOpen = ref(false);
const clearHistoryDialog = ref<HTMLDialogElement | null>(null);
const revokeDevice = ref<Device | null>(null);
const revokeDialog = ref<HTMLDialogElement | null>(null);
const forgetDevice = ref<Device | null>(null);
const forgetDialog = ref<HTMLDialogElement | null>(null);
/** The internet-paired device a 解除互联网配对 is being confirmed for.  It has
 * its own dialog rather than sharing 撤销信任's: that one names a pinned
 * certificate this machine holds and the pairing repository it drops, and an
 * internet peer has neither — what ends is a code pairing, and the sentence
 * that says so is a different sentence. */
const relayUnpairDevice = ref<Device | null>(null);
const relayUnpairDialog = ref<HTMLDialogElement | null>(null);
const purgeDevice = ref<Device | null>(null);
const purgeDialog = ref<HTMLDialogElement | null>(null);
const probeResults = ref<Record<string, DeviceProbeResult>>({});
/** What the rename dialog is open on: a device on this machine's list, whose
 * alias is rewritten from the context menu (the row has its own 设备备注 field
 * for this, and the menu entry is the same write with a dialog in front of it,
 * which is what the legacy 重命名 was), or a peer on the internet-pairing page,
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
const renameDialog = ref<HTMLDialogElement | null>(null);
/** The conversation the chat page should open when it next mounts, handed over
 * by 打开聊天 on a device row.  The chat page owns which session is selected —
 * it has to, since it is the one polling the list — so this is a request rather
 * than a selection. */
const chatSessionToOpen = ref("");
const probeBusyId = ref("");
/** The device an update offer is being sent to, and what came back. */
const updateBusyId = ref("");
const updateNotes = ref<Record<string, string>>({});
/** The device this machine is asking for a newer installer. */
const fetchBusyId = ref("");
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
const sendUrlDialog = ref<HTMLDialogElement | null>(null);
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
const pushTextDialog = ref<HTMLDialogElement | null>(null);
const pushTextValue = ref("");
const pushTextBusy = ref(false);
const logsOpen = ref(false);
const logsDialog = ref<HTMLDialogElement | null>(null);
const logLines = ref<string[]>([]);
/** The subset of `logLines` that reports a failure — what the dialog opens on. */
const logProblems = ref<string[]>([]);
const logView = ref<"problems" | "all">("problems");
const logCount = ref(200);
const logsBusy = ref(false);
const logsExporting = ref(false);
const logExportMessage = ref("");
const aboutOpen = ref(false);
const aboutDialog = ref<HTMLDialogElement | null>(null);
const aboutBusy = ref(false);
const aboutMessage = ref("");
const qrOpen = ref(false);
const qrDialog = ref<HTMLDialogElement | null>(null);
const qrBusy = ref(false);
const qrImage = ref("");
const qrUrl = ref("");
const qrMessage = ref("");
const qrShareBusy = ref(false);
const qrShareMessage = ref("");
let offMenuAction: (() => void) | undefined;
let offFileDrop: (() => void) | undefined;
/** Whether files are being dragged over the window at this instant.
 *
 * The window is the drop target, so the whole window has to say so: a file
 * dragged in from the desktop can land anywhere on it, and a hint drawn on one
 * card would be a target the reader cannot aim at. */
const dropActive = ref(false);
/** The paths the OS handed this window, on their way to the transfers page.
 *
 * Held here rather than sent from here: a drop names files but not a machine,
 * and the page that owns the device picker is where the two are brought
 * together — the same rule its own 发送文件 button follows.  Cleared by the page
 * once it has them, so a later visit does not re-stage a finished drop. */
const droppedPaths = ref<string[]>([]);
const restartOpen = ref(false);
const restartDialog = ref<HTMLDialogElement | null>(null);
const diagnosticsOpen = ref(false);
const diagnosticsDialog = ref<HTMLDialogElement | null>(null);
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
// Fixed group order, mirroring the legacy diagnostics panel: a group missing
// from the payload is skipped rather than rendered empty.
const diagnosticGroupOrder = ["system", "network", "internet", "ai_config", "chat", "transfer", "filesystem"];
const discoveryState = ref<{ enabled: boolean; visible: boolean } | null>(null);
const discoveryBusy = ref(false);
const activeDevices = computed(() => state.devices.filter((device) => !device.archived));
const archivedDevices = computed(() => state.devices.filter((device) => device.archived));
const native = inDesktop();
const ready = computed(() => state.status?.health === "ready");
let themeGeneration = 0;
function applyTheme(mode: unknown) {
  ++themeGeneration;
  document.documentElement.dataset.theme = mode === "dark" || mode === "light" ? mode : "system";
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
// Live, so flipping the switch on the settings page is visible where it is
// flipped rather than after a restart.
watch(() => settings.value.ui_animation_enabled, applyMotion);
watch(ready, async (isReady) => {
  const generation = ++themeGeneration;
  if (!isReady) return;
  try {
    const loaded = await bridge.settings();
    if (generation === themeGeneration) applyTheme(loaded.settings.appearance_mode);
    applyMotion(loaded.settings.ui_animation_enabled);
    // Language applies even if the theme generation moved on: the shell must
    // render in the saved language either way.
    setLocale(loaded.settings.language);
    // Seeded here as well as by the settings form: a pause armed before this
    // window opened (or restored from disk on a restart) still counts down, and
    // a user who never opens the settings page would otherwise never see it.
    settings.value.timed_pause_until = Number(loaded.settings.timed_pause_until || 0);
    maybePromptLanguage(loaded.settings.language_chosen);
    // The phone service is read here as well as on the way into the two pages
    // that show it, because the window opens on one of them: a switch drawn
    // from a status nobody has read yet would show the service as off until
    // the reader happened to visit the devices page.
    void refreshCompanion();
  }
  catch { /* Settings can be retried by opening the settings view. */ }
});
// The first-run picker: shown once per session and only while the sidecar says
// no language has ever been chosen.  Dismissing it leaves the flag False, so it
// returns on the next launch — the legacy onboarding behaves the same way.
const languageDialog = ref<HTMLDialogElement | null>(null);
const languagePromptOpen = ref(false);
let languagePrompted = false;
watch(languagePromptOpen, async (open) => {
  await nextTick();
  if (open) languageDialog.value?.showModal();
  else languageDialog.value?.close();
});
function maybePromptLanguage(chosen: unknown) {
  if (languagePrompted || chosen !== false) return;
  languagePrompted = true;
  languagePromptOpen.value = true;
}
/** Switch the shell immediately and persist the choice. */
async function applyLanguage(code: string) {
  setLocale(code);
  // A choice made anywhere (picker or settings) retires the picker.
  languagePrompted = true;
  try {
    // The sidecar flips `language_chosen` when `language` is updated, so this
    // is what stops the picker from returning next launch.
    await bridge.updateSettings({ language: code });
    if (settingsLoaded.value) settings.value.language = code;
  } catch (error) { state.error = error as any; }
}
async function chooseLanguage(code: string) {
  languagePromptOpen.value = false;
  await applyLanguage(code);
}
/** The first-run picker shows every option in BOTH languages. */
const languageChoices = computed(() => [
  { code: "zh-CN", native: t("简体中文"), other: "Simplified Chinese" },
  { code: "en", native: "English", other: t("英语") },
]);
const overviewAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("overview.get"));
const favoritesAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("favorites.list"));
const syncLabel = computed(() => state.status?.sync_state === "running" ? t("同步运行中")
  : state.status?.sync_state === "paused" ? t("同步已暂停") : t("同步引擎未启动"));
// The timed pause, as the dashboard's quick controls had it: the deadline the
// runtime armed (epoch seconds, in the settings payload this shell already
// fetches), re-read on a 15 s tick so the remaining minutes count down, and
// refetched once when it passes so a deadline the host has already cleared
// stops being shown.  The resume itself happens in the host — it publishes the
// change, and the store refreshes the status on any event.
const nowTick = ref(Date.now());
const pauseBusy = ref(false);
let pauseExpiredSyncing = false;
let pauseTickTimer: ReturnType<typeof setInterval> | undefined;
const pauseUntilMs = computed(() => Number(settings.value.timed_pause_until || 0) * 1000);
const pauseLeftMs = computed(() => Math.max(0, pauseUntilMs.value - nowTick.value));
// Whole minutes left, rounded up: a small remainder still reads 1 so the label
// never flashes "0" in the second before the host resumes.
const pauseLeftMinutes = computed(() => Math.max(1, Math.ceil(pauseLeftMs.value / 60000)));
const sendUrlAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("url.send"));
const discoveryAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("discovery.status"));
const pushTextAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("clipboard.push"));
const diagnosticsAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("diagnostics.report"));
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
const busy = computed(() => state.pending || state.refreshing || !ready.value);
const historyBusy = computed(() => busy.value || state.loading);
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
/* The three panes that own their own buttons report through one of these: the
 * notice stack is the window's, and which page a message belongs to is the
 * window's to say — the pane only knows what happened, not whose colour the
 * notice should carry. */
const notifyFavorites = (message: string) => store.toast("ui.favorites", message);
const notifyTransfers = (message: string) => store.toast("ui.transfers", message);
const notifyChat = (message: string) => store.toast("ui.chat", message);
async function refreshDevices() {
  await store.refresh();
  // The rows above 已移除的设备, which is the list this page is showing: a
  // removed device is not one a refresh just went and found.
  store.toast("ui.devices", t("已刷新 · {count} 台设备", { count: activeDevices.value.length }));
}
// Its own flag rather than the store's `pending`, because a merge spends most of
// its time fetching each row's full text before it ever reaches the store.
const mergeBusy = ref(false);
const visibleIds = computed(() => [...new Set(state.history.map((item) => item.id).filter((id) => id.trim()))]);
const allSelected = computed(() => !!visibleIds.value.length &&
  visibleIds.value.every((id) => state.selectedIds.includes(id)));
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
/** Pin or unpin the whole selection, reporting how many rows it covered. */
async function pinSelected(pinned: boolean) {
  const count = state.selectedIds.length;
  if (await store.batchPin(pinned)) {
    store.toast("ui.history", pinned
      ? t("已置顶 {count} 条", { count })
      : t("已取消置顶 {count} 条", { count }));
  }
}
watch(batchDeleteIds, async (ids) => {
  await nextTick();
  if (ids) batchDeleteDialog.value?.showModal();
  else batchDeleteDialog.value?.close();
});
watch(() => [state.query, state.offset], () => { batchDeleteIds.value = null; }, { flush: "sync" });
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
async function confirmClearHistory() {
  const cleared = await store.clearHistory();
  if (cleared) {
    clearHistoryOpen.value = false;
    store.toast("ui.history", t("已清空 {count} 条历史记录", { count: cleared }));
  }
}
watch(clearHistoryOpen, async (open) => {
  await nextTick();
  if (open) clearHistoryDialog.value?.showModal();
  else clearHistoryDialog.value?.close();
});
onUnmounted(() => { clearStatus(); clearTimeout(previewTimer); });
async function pauseSync(minutes: number) {
  if (pauseBusy.value) return;
  pauseBusy.value = true;
  try {
    const result = await bridge.pauseSync(minutes);
    // The sidecar armed the auto-resume timer and reports the deadline it
    // armed, so the countdown is the runtime's number rather than one this
    // window guessed from the duration it asked for.
    settings.value.timed_pause_until = Number(result?.until || 0);
    nowTick.value = Date.now();
    await store.refresh();
    store.toast("ui.sync", t("同步已暂停 {minutes} 分钟", { minutes }));
  }
  catch (reason: any) { state.error = reason?.message || t("暂停同步失败"); }
  finally { pauseBusy.value = false; }
}
async function resumeSync() {
  if (pauseBusy.value) return;
  pauseBusy.value = true;
  try {
    await bridge.resumeSync();
    settings.value.timed_pause_until = 0;
    nowTick.value = Date.now();
    await store.refresh();
    store.toast("ui.sync", t("同步已恢复"));
  }
  catch (reason: any) { state.error = reason?.message || t("恢复同步失败"); }
  finally { pauseBusy.value = false; }
}
function pairingPending(device: Device) {
  return !device.paired && ["pending", "peer_confirmed", "confirmed_waiting"].includes(device.pairing_status);
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
 */
function canDial(device: Device) {
  return device.dialable ?? device.connection_state !== "offline";
}
function pairingLabel(device: Device) {
  if (device.paired) return t("已配对");
  // Paired, just not here: a device holding a code pairing and no LAN pairing
  // is not an unpaired device, and 未配对 beside an 互联网·在线 chip said it was.
  // The 本地 chip beside this one still carries the local half of the answer.
  if (relayPairing(device).paired) return t("互联网配对");
  return ({ pending: t("等待确认"), peer_confirmed: t("对方已确认"), confirmed_waiting: t("等待对方确认"),
    cancelled: t("已取消") } as Record<string, string>)[device.pairing_status] || t("未配对");
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

/** Whether an update may be sent to this device, or asked of it.
 *
 * Both directions ride the LAN link and nothing else: the sidecar drops an
 * ``update_offer`` that arrives over the relay, and the offer it sends is a
 * bare ``send_to_peer`` with no relay fallback.  The buttons were gated on a
 * *sighting* instead, and an mDNS record keeps naming the version a device
 * runs long after it stopped being dialable — so a peer whose port is blocked,
 * or one that has just left the network, kept a live 发送更新 and answered the
 * click with a 15-second dial and 无法连接到该设备，它可能已离线。.  The row's own
 * 本地·离线 chip is the reason, and it sits beside the button. */
function updateReachable(device: Device) {
  return device.connection_state === "online";
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
async function saveDeviceNote(device: Device, event: Event) {
  const note = (event.target as HTMLInputElement).value;
  try { await bridge.setDeviceNote(device.id, note); await store.refresh(); }
  catch (reason: any) { state.error = reason?.message || t("保存设备备注失败"); }
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
    state.error = reason?.message || t("重命名失败");
  }
}
/** Confirm the alias the rename dialog collected.  A device's name goes through
 * the backend's one user-editable name per device — the alias the row's own
 * 设备备注 field writes — rather than inventing a second one; a peer on the
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
    state.error = reason?.message || t("重命名失败");
  }
}
watch(renameTarget, async (target) => {
  await nextTick();
  if (target) renameDialog.value?.showModal();
  else renameDialog.value?.close();
});
async function confirmRevoke() {
  const current = state.devices.find((device) => device.id === revokeDevice.value?.id && device.paired);
  if (current && await store.unpair(current)) revokeDevice.value = null;
}
watch(revokeDevice, async (device) => {
  await nextTick();
  if (device) revokeDialog.value?.showModal();
  else revokeDialog.value?.close();
});
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
watch(relayUnpairDevice, async (device) => {
  await nextTick();
  if (device) relayUnpairDialog.value?.showModal();
  else relayUnpairDialog.value?.close();
});
async function confirmPurge() {
  const target = purgeDevice.value;
  if (target && await store.purge(target)) purgeDevice.value = null;
}
watch(forgetDevice, async (device) => {
  await nextTick();
  if (device) forgetDialog.value?.showModal();
  else forgetDialog.value?.close();
});
watch(purgeDevice, async (device) => {
  await nextTick();
  if (device) purgeDialog.value?.showModal();
  else purgeDialog.value?.close();
});
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
    await bridge.offerDeviceUpdate(device.id);
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: t("已把更新发送给 {name}", { name: device.name }),
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
/** Ask a device that is on a newer build to send this one its installer.
 *
 * The direction the feature is meant to run in, and the reason the older
 * device's list carries a button at all: this side is the one that is behind.
 * The blob is verified against the published release digest on arrival and then
 * staged, so the update card takes over from here — this function only owes the
 * row a sentence for the wait.
 */
async function fetchDeviceUpdate(device: Device) {
  if (fetchBusyId.value) return;
  fetchBusyId.value = device.id;
  try {
    await bridge.fetchDeviceUpdate(device.id);
    updateNotes.value = {
      ...updateNotes.value,
      [device.id]: t("已向 {name} 索取安装包，收到后可在更新页安装", { name: device.name }),
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
watch(sendUrlDevice, async (device) => {
  await nextTick();
  const dialog = sendUrlDialog.value;
  if (!dialog) return;
  // The host can re-request while the dialog is already open.
  if (device) { if (!dialog.open) dialog.showModal(); }
  else { dialog.close(); sendUrlCandidates.value = []; }
});
async function confirmSendUrl() {
  const device = sendUrlDevice.value;
  const url = sendUrlValue.value.trim();
  if (!device || !url) return;
  sendUrlBusy.value = true;
  try {
    // Keep the dialog open on failure so the typed URL is not lost.
    if (await store.sendUrl(device, url)) {
      sendUrlDevice.value = null;
      store.toast("ui.devices", t("已发送网址到 {name}", { name: device.name }));
    }
  } finally {
    sendUrlBusy.value = false;
  }
}
function openPushText() {
  pushTextValue.value = "";
  pushTextOpen.value = true;
}
watch(pushTextOpen, async (open) => {
  await nextTick();
  if (open) pushTextDialog.value?.showModal();
  else pushTextDialog.value?.close();
});
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
watch(logsOpen, async (open) => {
  await nextTick();
  if (open) logsDialog.value?.showModal();
  else logsDialog.value?.close();
});
async function loadLogs(lines = logCount.value) {
  logsBusy.value = true;
  try {
    const result = await bridge.readLogs(lines);
    logLines.value = result.logs;
    logProblems.value = result.problems ?? [];
    logCount.value = lines;
  } catch (error) { state.error = error as any; }
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
  } catch (error) { state.error = error as any; }
  finally { logsExporting.value = false; }
}
watch(qrOpen, async (open) => {
  await nextTick();
  if (open) qrDialog.value?.showModal();
  else qrDialog.value?.close();
});
async function openCompanionQr() {
  qrImage.value = "";
  qrUrl.value = "";
  qrMessage.value = "";
  qrShareMessage.value = "";
  qrOpen.value = true;
  qrBusy.value = true;
  try {
    const result = await bridge.companionQr();
    qrUrl.value = result.url || "";
    if (result.ok && result.qr) qrImage.value = result.qr;
    else qrMessage.value = result.error === "COMPANION_NOT_RUNNING" ? t("手机服务未启动") : t("二维码不可用");
  } catch (error) { state.error = error as any; }
  finally { qrBusy.value = false; }
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
  } catch (error) { state.error = error as any; }
  finally { qrShareBusy.value = false; }
}
// The phone's panel has no window of its own: its QR and send-URL buttons
// arrive as events (see the store) and are answered by this window.
watch(() => state.hostRequest, (request) => {
  if (!request) return;
  if (request.kind === "qr") void openCompanionQr();
  else openSendUrlFromHost();
});
watch(aboutOpen, async (open) => {
  await nextTick();
  if (open) aboutDialog.value?.showModal();
  else aboutDialog.value?.close();
});
function openAbout() {
  aboutMessage.value = "";
  aboutOpen.value = true;
}
async function openAboutLink(target: "homepage" | "releases") {
  aboutBusy.value = true;
  aboutMessage.value = "";
  try {
    const result = await bridge.openAboutLink(target);
    aboutMessage.value = t("已在浏览器打开：{url}", { url: result.url });
  } catch (error) { state.error = error as any; }
  finally { aboutBusy.value = false; }
}
watch(restartOpen, async (open) => {
  await nextTick();
  if (open) restartDialog.value?.showModal();
  else restartDialog.value?.close();
});
async function confirmRestart() {
  restartOpen.value = false;
  // The host exits this process: nothing after the call is guaranteed to run.
  await bridge.restartApp();
}
watch(diagnosticsOpen, async (open) => {
  await nextTick();
  if (open) diagnosticsDialog.value?.showModal();
  else diagnosticsDialog.value?.close();
});
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
async function refreshDiagnostics() {
  if (diagnosticsBusy.value) return;
  diagnosticsBusy.value = true;
  try {
    const report = await store.diagnostics();
    if (report) diagnosticsReport.value = report;
  } finally { diagnosticsBusy.value = false; }
}
async function openDiagnostics() {
  if (!diagnosticsAvailable.value) return;
  diagnosticsOpen.value = true;
  diagnosticsReport.value = null;
  await refreshDiagnostics();
}
/** 重新检测, which reports itself.
 *
 * Each check reads the machine afresh and most of them come back the same, so
 * the scan is worth a word — including the count, because how many of the
 * checks passed is the answer the button was pressed for. */
async function rerunDiagnostics() {
  await refreshDiagnostics();
  const checks = diagnosticsReport.value?.checks || [];
  store.toast("ui.settings", t("已重新检测 · {ok}/{total} 项通过", {
    ok: checks.filter((check) => check.ok).length,
    total: checks.length,
  }));
}
async function repairDiagnostics(kind: DiagnosticAction) {
  if (diagnosticsRepairBusy.value) return;
  diagnosticsRepairBusy.value = true;
  try {
    const result = await store.repairDiagnostics(kind);
    if (!result) return;
    if (result.ok === false) {
      state.error = { code: "DIAG_REPAIR_FAILED", message: result.error || t("无法打开系统设置"), retryable: false };
      return;
    }
    store.toast("ui.settings", kind === "firewall" ? t("已请求修复防火墙规则") : t("已打开本地网络权限设置"));
    // The elevated rule may land a moment later, so re-scan after a pause.
    if (kind === "firewall") setTimeout(() => { if (diagnosticsOpen.value) void refreshDiagnostics(); }, 2500);
  } finally { diagnosticsRepairBusy.value = false; }
}
async function checkForUpdate() {
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
  // Returns immediately; the panel renders progress from `update.state` events.
  const result = await store.downloadUpdate();
  if (!result || !result.ok) {
    announce(t("更新下载失败") + (result?.error ? `：${result.error}` : ""));
  }
}
async function installUpdate() {
  // A successful install replaces this process, so a reply is already a
  // refusal. The host publishes the failing phase before it returns, and the
  // card renders that, so the only case left to speak to is "nothing to do".
  const result = await store.installUpdate();
  if (!result) announce(t("更新安装失败"));
  else if (!result.installed) announce(t("已是最新版本"));
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
watch(deleteItem, async (item) => {
  await nextTick();
  if (item) deleteDialog.value?.showModal();
  else deleteDialog.value?.close();
});
/** The sidebar, top to bottom: what Ctrl+1…8 counts.  The numbers belong to the
 * rows the user can see, which is why this list has to stay in the order the
 * template renders them and not in whatever order would read better here. */
const PAGES = ["overview", "history", "devices", "favorites", "transfers", "chat", "ai", "settings"] as const;
/** How the chords are *named* — the tooltip and `aria-keyshortcuts`.  The
 * handler takes Ctrl or Cmd on either platform; a Mac user hunting for ⌘ should
 * not be told Ctrl. */
const MODIFIER = navigator.userAgent.includes("Mac") ? { label: "Cmd", key: "Meta" } : { label: "Ctrl", key: "Control" };
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
function openPage(page: (typeof PAGES)[number]) {
  if (page === "settings") { void openSettings(); return; }
  if (page === "ai") { void openAiPage(); return; }
  tab.value = page;
}
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
};
const PAGE_SUMMARIES: Record<(typeof PAGES)[number], () => string> = {
  // A sentence rather than a count, like the AI page's: the overview's own
  // numbers are the page's, read when it is entered, and the header has no
  // second copy of them to keep honest.
  overview: () => t("本机状态与最近活动"),
  history: () => t("{count} 条记录", { count: state.total }),
  devices: () => t("{count} 台设备", { count: state.devices.length }),
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
};
const heading = computed(() => PAGE_HEADINGS[tab.value]());
const summary = computed(() => PAGE_SUMMARIES[tab.value]());

/** The two pages that answer more than one question, split into strips.
 *
 * 设备 and AI 配置 had both grown into a single scroll of three cards each —
 * the device list, the internet pairing panel and the phone service on one; the
 * tool list, the local inventory and a peer's inventory on the other — and the
 * reader who wants the second card has to pass the whole of the first one to
 * reach it.  A strip of tabs is the same content under one heading with the
 * other two a click away, which is what these pages are: not one long page but
 * three short ones that share a title.
 *
 * The sections are named, not indexed, so a tab and the panel it opens cannot
 * drift; the strip stays where it is while the panel under it scrolls, because
 * switching panels is how the page is navigated and the reader is usually
 * part-way down a list when they want the other one. */
const DEVICE_SECTIONS = ["devices", "internet", "phone"] as const;
const AI_SECTIONS = ["tools", "local", "peer"] as const;
type DeviceSection = (typeof DEVICE_SECTIONS)[number];
type AiSection = (typeof AI_SECTIONS)[number];
const deviceTab = ref<DeviceSection>("devices");
const aiTab = ref<AiSection>("tools");
const deviceSections = computed<Array<{ id: DeviceSection; label: string }>>(() => [
  { id: "devices", label: t("设备列表") },
  { id: "internet", label: t("互联网配对") },
  { id: "phone", label: t("手机 Companion") },
]);
const aiSections = computed<Array<{ id: AiSection; label: string }>>(() => [
  { id: "tools", label: t("AI 工具") },
  { id: "local", label: t("本机配置") },
  { id: "peer", label: t("其他设备") },
]);
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
    if (tab.value === "settings") {
      void nextTick(() => settingsSearchInput.value?.focus());
      return;
    }
    openPage("history");
    void nextTick(() => searchInput.value?.focus());
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
onMounted(() => {
  store.start();
  void refreshChatUnread();
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
    if (tab.value !== "chat") void refreshChatUnread();
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
    if (tab.value === "devices" && deviceTab.value === "internet") {
      void refreshInternetPairing();
    }
  }, NETPAIR_STATUS_POLL_MS);
  watch(tab, (page, previous) => {
    if (previous === "chat" && page !== "chat") void refreshChatUnread();
  });
  // The dashboard ticked its pause countdown every 15 s; the interval is the
  // same here because the label is whole minutes.  Only the deadline is refetched
  // — merging the whole settings payload would overwrite a half-edited form.
  pauseTickTimer = setInterval(() => {
    nowTick.value = Date.now();
    const until = Number(settings.value.timed_pause_until || 0) * 1000;
    if (until <= 0 || nowTick.value < until || pauseExpiredSyncing) return;
    pauseExpiredSyncing = true;
    void bridge.settings()
      .then((loaded) => {
        settings.value.timed_pause_until = Number(loaded.settings.timed_pause_until || 0);
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
    .catch((error) => { state.error = error as any; });
  // A file dropped on the window is the transfers page's own 发送文件 intent
  // with the picker already answered, so the paths are handed to that page and
  // it is opened — the device is still named there, because a send is the one
  // thing in this window the user cannot take back.
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
    openPage("transfers");
  })
    .then((off) => { offFileDrop = off; })
    .catch(() => { /* A window that cannot be a drop target is not an error. */ });
  window.addEventListener("keydown", onShortcut);
});
onUnmounted(() => {
  if (pauseTickTimer) clearInterval(pauseTickTimer);
  if (chatUnreadTimer) clearInterval(chatUnreadTimer);
  if (netpairStatusTimer) clearInterval(netpairStatusTimer);
  window.removeEventListener("keydown", onShortcut);
  ++aiInventoryGeneration;
  ++themeGeneration;
  ++companionGeneration;
  ++translationGeneration;
  ++aiReadGeneration;
  ++aiRemoteGeneration;
  offMenuAction?.();
  offFileDrop?.();
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
  return JSON.stringify([
    settings.value,
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
}
/** Whether the rail marks this card as holding an edit that is not saved. */
function cardHasEdits(id: string): boolean {
  return settingsDirty.value && !!settingsEditedCards.value[id];
}

/** The two fields the AI page writes, and whether they are on disk.
 *
 * They rode the settings form until the page they belong to existed, which is
 * what a field does when the page it belongs to is not there yet; a field saved
 * by a button on another page is a field whose save the reader has to go and
 * find.  The snapshot is the idiom the settings form uses, for the same reason
 * it uses it: the whole value is compared, so a field put back the way it was
 * found is saved state again rather than a pending change.
 */
const aiProfilesLoaded = ref(false);
const aiProfilesBusy = ref(false);
const aiProfilesSaved = ref<string | null>(null);
function aiProfilesFormSnapshot(): string {
  return JSON.stringify([aiEnabled.value, aiCustomPaths.value]);
}
const aiProfilesDirty = computed(() =>
  aiProfilesSaved.value !== null && aiProfilesFormSnapshot() !== aiProfilesSaved.value);
/** Read the tools and the two fields, once.  A read that failed is retried the
 * next time the page opens — the flag is set on success only — because a failed
 * read that marked itself loaded would leave the card empty for the session. */
async function loadAiProfiles() {
  if (aiProfilesLoaded.value) return;
  try {
    const profiles = await bridge.aiProfiles();
    aiTools.value = Array.isArray(profiles.tools)
      ? (profiles.tools as Array<{ key: string; label: string }>) : [];
    aiEnabled.value = Array.isArray(profiles.enabled) ? profiles.enabled as string[] : [];
    aiCustomPaths.value = Array.isArray(profiles.custom_paths) ? (profiles.custom_paths as string[]).join("\n") : "";
    aiProfilesSaved.value = aiProfilesFormSnapshot();
    aiProfilesLoaded.value = true;
  } catch (error) { state.error = error as any; }
}
async function saveAiProfiles() {
  if (aiProfilesBusy.value || !aiProfilesLoaded.value) return;
  aiProfilesBusy.value = true;
  try {
    await bridge.updateAiProfiles(
      aiEnabled.value,
      aiCustomPaths.value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean),
    );
    aiProfilesSaved.value = aiProfilesFormSnapshot();
  } catch (error) { state.error = error as any; }
  finally { aiProfilesBusy.value = false; }
}
/** What the AI page does on the way in.
 *
 * The tools and the two fields, because they are the page's own form.  The peer
 * picker's counts, because a picker that claims nothing about any device until
 * a button is pressed is a picker the reader has to press a button to read —
 * that read is the cached one, and it asks no peer to re-send anything.
 *
 * What is *not* read here is the inventory.  The other pages' entry reads are
 * state the host already holds — a service's status, a cached inventory — and
 * this one walks every configured tool's directories on disk.  A page the
 * reader may be passing through does not get to start that walk: the card's own
 * button asks for it, and the card says what the last walk found.
 */
function openAiPage() {
  tab.value = "ai";
  void loadAiProfiles();
  void primeAiInventories();
}
async function openSettings() {
  tab.value = "settings";
  void loadDiscovery();
  // Hydrate the live phase so a download that began before this window opened
  // still renders (the sidecar owns the state; this is a read-only mirror).
  void store.loadUpdateStatus();
  if (settingsLoaded.value || settingsLoading) return;
  settingsLoading = true;
  try {
    const loaded = (await bridge.settings()).settings;
    autoUpdateCheck.value = loaded.auto_update_check !== false;
    settings.value = {
      ...loaded,
      app_filter_enabled: loaded.app_filter_enabled ?? false,
      app_filter_mode: loaded.app_filter_mode ?? "blacklist",
      app_filter_list: Array.isArray(loaded.app_filter_list) ? loaded.app_filter_list.join("\n") : "",
      filter_enabled_categories: loaded.filter_enabled_categories ??
        filterCategories.value.filter(([key]) => key !== "email").map(([key]) => key),
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
    };
    if (native) {
      try { settings.value.auto_start = await bridge.autostartStatus(); }
      catch { /* Keep the persisted preference when the OS query is unavailable. */ }
    }
    savedAutoStart.value = !!settings.value.auto_start;
    savedEncryptionEnabled.value = !!settings.value.encryption_enabled;
    applyTheme(settings.value.appearance_mode);
    applyMotion(settings.value.ui_animation_enabled);
    // The relay's peer list and its per-peer ledgers are read when the devices
    // page opens, where that card is; this page has no row that shows them.
    // The AI tools are read the same way, on the AI page — this form does not
    // carry them and has no field they belong to.
    savedSettings.value = settingsFormSnapshot();
    settingsEditedCards.value = {};
    settingsLoaded.value = true;
  }
  catch (error) { state.error = error as any; }
  finally { settingsLoading = false; }
}
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
async function saveSettings() {
  if (settingsBusy.value || !settingsLoaded.value) return;
  settingsBusy.value = true;
  settingsSaved.value = false;
  try {
    const autoStart = !!settings.value.auto_start;
    const theme = settings.value.appearance_mode;
    const result = await bridge.updateSettings({
      device_name: settings.value.device_name,
      appearance_mode: settings.value.appearance_mode,
      plain_text_only: settings.value.plain_text_only,
      // Applied to the live engine, not on restart: the runtime hands it
      // straight to the running chat manager.
      chat_open_to_all: settings.value.chat_open_to_all,
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
      source_tracking_enabled: settings.value.source_tracking_enabled,
      paste_to_top: settings.value.paste_to_top,
      internet_sync_enabled: settings.value.internet_sync_enabled,
      relay_brokers: brokerList(settings.value.relay_brokers),
      relay_private_brokers: brokerList(settings.value.relay_private_brokers),
      relay_username: settings.value.relay_username,
      relay_password: settings.value.relay_password,
      // Typed in KB, stored in bytes.  Read when the relay is built, so a
      // changed limit reaches the chunks after a restart — the row's hint says
      // so, next to the control.
      relay_max_message_bytes: clampNumber(
        settings.value.relay_max_message_kb,
        DEFAULT_RELAY_MAX_MESSAGE_BYTES / 1024,
        MIN_RELAY_MAX_MESSAGE_KB,
        MAX_RELAY_MAX_MESSAGE_KB,
      ) * 1024,
      // Advanced/network fields.  Numbers are clamped to the sidecar's bounds so
      // a cleared input falls back instead of failing the whole save.
      port: clampNumber(settings.value.port, 19990, 1024, 65535),
      service_type: String(settings.value.service_type || "").trim() || "_clipsync._tcp.local.",
      web_history_limit: clampNumber(settings.value.web_history_limit, 30, 1, 500),
      sync_debounce: clampNumber(settings.value.sync_debounce, 0.3, 0.05, 10),
      clipboard_poll_interval: clampNumber(settings.value.clipboard_poll_interval, 1, 0.1, 60),
      file_receive_dir: String(settings.value.file_receive_dir || "").trim(),
      transfer_timeout: clampNumber(settings.value.transfer_timeout, 120, 5, 3600),
      max_reconnect_attempts: clampNumber(settings.value.max_reconnect_attempts, 10, 0, 100),
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
    }) as { password_set?: boolean };
    if (securityPassword.value) {
      // The sidecar echoes whether a password is now stored; the typed value is
      // never sent back, so drop it from the form.
      if (typeof result.password_set === "boolean") settings.value.password_set = result.password_set;
      securityPassword.value = "";
      securityPasswordConfirm.value = "";
    }
    savedAutoStart.value = autoStart;
    savedEncryptionEnabled.value = !!settings.value.encryption_enabled;
    applyTheme(theme);
    savedSettings.value = settingsFormSnapshot();
    settingsEditedCards.value = {};
    settingsSaved.value = true;
    await store.refresh();
  } catch (error) { state.error = error as any; }
  finally { settingsBusy.value = false; }
}
async function refreshAiLocal() {
  try {
    const result = await bridge.aiLocal("listing");
    aiLocalItems.value = Array.isArray(result.entries) ? result.entries : [];
    aiLocalPage.value = 0;
    aiMessage.value = t("已读取 {count} 个配置项", { count: aiItemCount(aiLocalItems.value) });
  } catch (error) { state.error = error as any; }
}
/** The runtime's per-peer inventory answer in the shape the picker and the list
 * read it in. */
function aiInventoryMap(peers: Record<string, any>) {
  const next: Record<string, { entries: Array<Record<string, any>>; legacy: boolean }> = {};
  for (const [id, peer] of Object.entries(peers)) {
    next[id] = {
      entries: Array.isArray(peer?.entries) ? peer.entries : [],
      legacy: peer?.legacy === true,
    };
  }
  return next;
}
/** Fills the map from one cached read, asking no peer to re-send anything.  The
 * counts in the picker are what make choosing a device possible in the first
 * place, and they cannot exist until something has asked: the legacy device bar
 * had them because its panel primed the inventory on mount, and this is that
 * same read.  Failures are swallowed — the card's own read reports its own, and
 * a picker with no counts is still a working picker. */
async function primeAiInventories() {
  if (Object.keys(aiInventories.value).length) return;
  try {
    const result = await bridge.aiInventory(false, "");
    if (result?.peers) aiInventories.value = aiInventoryMap(result.peers);
  } catch { /* The reader can still pick a device and read it. */ }
}
async function refreshAiRemote(requestRefresh = true) {
  const peerId = aiPeerId.value;
  if (!state.devices.some(device => device.id === peerId && device.paired)) return;
  const generation = ++aiInventoryGeneration;
  try {
    const result = await bridge.aiInventory(requestRefresh, peerId);
    if (generation !== aiInventoryGeneration) return;
    // A peer that reported without being asked is in this answer too, so the
    // whole map is taken, not just the peer the reader is looking at.  A host
    // that answers with no `peers` at all — an older backend, a failed read —
    // leaves what is already on screen alone rather than blanking the picker.
    const answer = (result?.peers || null) as Record<string, any> | null;
    if (answer) aiInventories.value = aiInventoryMap(answer);
    const inventory = answer?.[peerId];
    // The version badges compare this list against the local one, so the local
    // walk has to have happened at least once.  Read it here rather than making
    // the reader find the other button first — a diff that silently reports
    // nothing because half its input is missing is worse than no diff.
    if (!aiLocalItems.value.length) await refreshAiLocal();
    // The walk is awaited, so the peer may have changed under us: nothing is
    // committed for a peer the reader has already left.
    if (generation !== aiInventoryGeneration) return;
    const entries = Array.isArray(inventory?.entries) ? inventory.entries : [];
    aiRemotePage.value = 0;
    // Ticks survive a refresh only where the entry is still there: an entry the
    // peer no longer offers cannot be pulled, and leaving it ticked would promise
    // a request that the batch would then not send.
    const present = new Set(entries.map(aiEntryKey));
    aiRemoteSelected.value = aiRemoteSelected.value.filter(key => present.has(key));
    // A flag, not the rendered text: the message is translated, so comparing
    // the string would break as soon as the locale changes.  A peer that did not
    // answer gets no "read 0 items" — the empty list already says which of the
    // two empty states this is.
    const answered = inventory != null;
    if (requestRefresh) {
      aiRemoteWaiting.value = Array.isArray(result.refreshed) && result.refreshed.includes(peerId);
      aiRemoteMessage.value = aiRemoteWaiting.value
        ? t("已请求更新，等待对方返回库存")
        : answered ? t("已读取 {count} 个远程配置项", { count: aiItemCount(entries) }) : "";
    } else if (aiRemoteWaiting.value && answered) {
      aiRemoteWaiting.value = false;
      aiRemoteMessage.value = t("已读取 {count} 个远程配置项", { count: aiItemCount(entries) });
    }
  } catch (error) {
    if (generation === aiInventoryGeneration) state.error = error as any;
  }
}
async function previewAiRemote(item: Record<string, any>) {
  const peerId = aiPeerId.value;
  if (!state.devices.some(device => device.id === peerId && device.paired)) return;
  const generation = ++aiRemoteGeneration;
  try {
    const result = await bridge.aiPreview(peerId, String(item.tool || ""), String(item.root || ""), String(item.rel_path || ""));
    if (generation !== aiRemoteGeneration) return;
    aiRemoteMessage.value = String(result.content || t("空文件"));
  } catch (error) {
    if (generation === aiRemoteGeneration) state.error = error as any;
  }
}
/** Why a pull was refused, in words, for the codes whose raw form is a name
 * rather than an explanation.  A code with no entry is shown as it arrived:
 * inventing a sentence for a failure this build has never seen would describe
 * something that may not be what happened. */
function aiPullErrorText(code: string): string {
  // The one refusal a reader can act on: the peer is too old to be a source,
  // and its files stay readable — which is the whole reason the wizard offers
  // it at all rather than hiding it the way the older panel did.
  if (code === "legacy_peer_read_only") return t("该设备版本过旧，只能浏览，不能作为迁移来源");
  return code;
}

/** Why a file did not land, in words.  Same rule as above: an unknown code is
 * shown as it arrived. */
function aiLandReasonText(reason: string): string {
  if (reason === "append_not_text") return t("不是文本文件，无法追加合并");
  if (reason === "backup_failed") return t("本地文件备份失败");
  if (reason === "io_error") return t("本地文件写入失败");
  if (reason === "no_local_root") return t("本机没有与之匹配的监控目录");
  return reason;
}
async function pullAiRemote(items: Array<Record<string, any>>, mode = "copy") {
  const peerId = aiPeerId.value;
  if (!items.length) return;
  if (!state.devices.some(device => device.id === peerId && device.paired)) return;
  // Refused here as well as by the sidecar, because the reader is owed the
  // reason before the work: a selected legacy peer disables the buttons, but a
  // peer that downgrades between selection and click would otherwise reach the
  // refusal through this path with nothing but the code to show for it.
  if (aiPeerIsLegacy(peerId)) {
    aiRemoteMessage.value = aiPullErrorText("legacy_peer_read_only");
    return;
  }
  const generation = ++aiRemoteGeneration;
  try {
    const payload = items.map(item => ({
      tool: item.tool, root: item.root || "", rel_path: item.rel_path, is_dir: !!item.is_dir,
    }));
    const result = await bridge.aiPull(peerId, payload, mode);
    if (generation !== aiRemoteGeneration) return;
    const errors = Array.isArray(result.errors) ? result.errors.map(String) : [];
    const requested = Number(result.requested || 0);
    aiPullOutcome.value = "";
    // What the peer accepted is the number of events to expect.  A request it
    // refused outright failed here rather than by never arriving, so those are
    // counted into the same report as the files that fail to land.
    aiPullProgress.value = requested || errors.length
      ? {
          peerId, total: requested + errors.length, done: 0, failed: errors.length,
          failures: errors.map(aiPullErrorText),
        }
      : null;
    aiRemoteMessage.value = t("已发送 {count} 个拉取请求，等待文件接收", { count: requested }) +
      (errors.length ? t("；部分请求失败：{errors}", { errors: errors.map(aiPullErrorText).join("，") }) : "");
    if (aiPullProgress.value && aiPullProgress.value.done + aiPullProgress.value.failed >= aiPullProgress.value.total) {
      finishAiPull();
    }
  } catch (error) {
    if (generation === aiRemoteGeneration) state.error = error as any;
  }
}

/** Ends the wait for a pull and says how it went.
 *
 * Both halves are reported, because they are different failures: a file the
 * peer would not send is a refusal, and a file that was sent and did not land
 * is a write this machine could not do.  The message names which. */
function finishAiPull() {
  const pull = aiPullProgress.value;
  if (!pull) return;
  aiPullProgress.value = null;
  if (pull.failed) {
    aiPullOutcome.value = "failed";
    aiRemoteMessage.value = t("拉取完成：{ok} 个成功、{failed} 个失败", { ok: pull.done, failed: pull.failed }) +
      // The reasons, not just the count: without them the same files fail on
      // every retry and nothing on screen says what would have to change.
      (pull.failures.length ? t("：{reasons}", { reasons: pull.failures.join("；") }) : "");
  } else {
    aiPullOutcome.value = "done";
    aiRemoteMessage.value = t("拉取完成：{count} 个文件已更新", { count: pull.done });
  }
  // The files are on this machine now, so its own inventory is out of date the
  // moment they land: without this the list the diff is drawn against would
  // still say 缺失 for everything just pulled.
  if (pull.done) void refreshAiLocal();
}

// One file of a pull this page is waiting on, as the store reports it.  A file
// that arrives with no pull outstanding is left alone: it is a pull someone
// else asked for — another window's, or the peer pushing one — and counting it
// against this page's total would report a completion that has not happened.
watch(() => state.aiFileEvent, (update) => {
  const pull = aiPullProgress.value;
  if (!update || !pull) return;
  const data = update.data || {};
  if (String(data.peer_id || "") !== pull.peerId) return;
  if (data.status === "error") {
    pull.failed += 1;
    // The sidecar reports why it refused to land the file next to the status
    // ("append_not_text", "io_error", …); keep it with the file it belongs to.
    const reason = String(data.reason || "");
    pull.failures.push(
      reason ? `${String(data.rel_path || "")}：${aiLandReasonText(reason)}`
             : String(data.rel_path || ""),
    );
  } else pull.done += 1;
  if (pull.done + pull.failed >= pull.total) finishAiPull();
});

async function openAiLocal(item: Record<string, any>) {
  try { await bridge.aiLocal("open", String(item.tool || ""), String(item.root || ""), String(item.rel_path || "")); }
  catch (error) { state.error = error as any; }
}
async function readAiLocal(item: Record<string, any>) {
  if (aiMutationBusy.value) return;
  const generation = ++aiReadGeneration;
  aiSelectedItem.value = null;
  aiEditorContent.value = "";
  try {
    const result = await bridge.aiLocal("read", String(item.tool), String(item.root || ""), String(item.rel_path));
    if (generation !== aiReadGeneration) return;
    if (result.truncated) {
      aiMessage.value = t("文件超过编辑器读取上限，请使用系统编辑器打开，避免保存不完整内容。");
      return;
    }
    aiSelectedItem.value = item;
    aiEditorContent.value = String(result.content || "");
    aiSavedContent.value = aiEditorContent.value;
  } catch (error) {
    if (generation === aiReadGeneration) state.error = error as any;
  }
}
async function saveAiLocal() {
  const item = aiSelectedItem.value;
  if (!item || aiMutationBusy.value) return;
  aiMutationBusy.value = true;
  const content = aiEditorContent.value;
  try {
    await bridge.aiLocal("save", String(item.tool), String(item.root || ""), String(item.rel_path), content);
    aiSavedContent.value = content;
    aiMessage.value = t("配置已保存");
    await refreshAiLocal();
  } catch (error) { state.error = error as any; }
  finally { aiMutationBusy.value = false; }
}
async function trashAiLocal(item: Record<string, any>) {
  if (aiMutationBusy.value) return;
  aiMutationBusy.value = true;
  // An in-flight read must not reopen a file after it has been removed.
  ++aiReadGeneration;
  try {
    await bridge.aiLocal("trash", String(item.tool), String(item.root || ""), String(item.rel_path));
    if (sameAiFile(aiSelectedItem.value, item)) {
      aiSelectedItem.value = null;
      aiEditorContent.value = "";
    }
    await refreshAiLocal();
    aiMessage.value = t("配置已移入回收区");
    aiTrashItem.value = null;
  } catch (error) { state.error = error as any; }
  finally { aiMutationBusy.value = false; }
}
async function refreshBackups() {
  try { backups.value = (await bridge.listBackups()).backups; }
  catch (error) { state.error = error as any; }
}
/** 刷新备份, which reports what the folder holds. */
async function refreshBackupsNow() {
  await refreshBackups();
  store.toast("ui.settings", t("已刷新 · {count} 个备份", { count: backups.value.length }));
}
async function refreshInternetPairing() {
  try {
    internetPairing.value = await bridge.internetPairingStatus();
    // The ledger is per peer, so the peers list is what the send state is
    // fetched against — and a peer that has gone away takes its line with it.
    const known = new Set(internetPairing.value.peers.map((peer: any) => String(peer.peer_id)));
    for (const peerId of Object.keys(delivery.state.peers)) {
      if (!known.has(peerId)) delivery.forget(peerId);
    }
    for (const peerId of known) void delivery.load(peerId);
  }
  catch (error) { state.error = error as any; }
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
async function toggleInternetSync(enabled: boolean) {
  if (internetSyncBusy.value) return;
  internetSyncBusy.value = true;
  try {
    await bridge.updateSettings({ internet_sync_enabled: enabled });
    // The settings page's own copy of the field follows, or switching tabs back
    // would show the old value and save it again on the next 保存.
    settings.value.internet_sync_enabled = enabled;
    await refreshInternetPairing();
  } catch (error) { state.error = error as any; }
  finally { internetSyncBusy.value = false; }
}
async function generateInternetPairing() {
  try {
    const result = await bridge.generateInternetPairingCode();
    internetPairing.value.generated_code = result.code;
    internetPairingMessage.value = t("配对码已生成");
    internetPairingFailed.value = false;
  } catch (error) { state.error = error as any; }
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
watch(() => store.state.netpairEvent?.revision, async () => {
  if (tab.value !== "devices" || deviceTab.value !== "internet") return;
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
  try {
    const result = await bridge.createBackup();
    backupMessage.value = t("备份已创建：{path}", { path: result.backup_path });
    await refreshBackups();
  } catch (error) { state.error = error as any; }
}
async function openDataFolder(which: "data" | "backups") {
  try {
    const result = await bridge.openDataFolder(which);
    backupMessage.value = t("已打开：{path}", { path: result.folder });
  } catch (error) { state.error = error as any; }
}
async function restoreBackup() {
  if (restoreBusy.value) return;
  try { restorePath.value = await bridge.chooseFile("backup"); }
  catch (error) { state.error = error as any; }
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
async function confirmRestoreBackup() {
  const path = restorePath.value;
  if (!path || restoreBusy.value) return;
  restoreBusy.value = true;
  backupMessage.value = "";
  try {
    const result = await bridge.restoreBackup(path);
    backupMessage.value = t("恢复完成：{result}", { result: JSON.stringify(result) });
    restorePath.value = null;
    await store.refresh();
  } catch (error) { state.error = error as any; }
  finally {
    // Even a partial restore can change configuration.
    settingsLoaded.value = false;
    await openSettings();
    restoreBusy.value = false;
  }
}
async function exportHistory(format: string) {
  try {
    const result = await bridge.exportHistory(format);
    backupMessage.value = t("导出完成：{name}", { name: result.filename || result.filepath });
  } catch (error) { state.error = error as any; }
}
async function importHistory() {
  const path = await bridge.chooseFile("history");
  if (!path) return;
  try {
    const result = await bridge.importHistory(path);
    backupMessage.value = t("导入完成：{result}", { result: JSON.stringify(result) });
    await store.refresh();
  } catch (error) { state.error = error as any; }
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
    if (generation === translationGeneration) state.error = error as any;
  } finally { translationBusy.value = false; }
}
</script>

<template>
  <div class="application">
    <aside class="sidebar">
      <div class="brand"><img :src="logo" alt="" /><span>ClipSync</span></div>
      <nav :aria-label="t('主导航')">
        <!-- The dashboard is the first row and the page the window opens on,
             as it was in the legacy window: it is a read of the whole machine
             rather than of one collection, so it sits above the pages that each
             hold one. -->
        <button :aria-label="t('概览')" :title="pageTitle(t('概览'), 0)" :aria-keyshortcuts="pageChord(0)" :class="{ active: tab === 'overview' }" @click="tab = 'overview'">
          <Activity :size="18" /><span>{{ t("概览") }}</span>
        </button>
        <button :aria-label="t('剪贴板历史')" :title="pageTitle(t('剪贴板历史'), 1)" :aria-keyshortcuts="pageChord(1)" :class="{ active: tab === 'history' }" @click="tab = 'history'">
          <History :size="18" /><span>{{ t("剪贴板历史") }}</span>
        </button>
        <button :aria-label="t('设备')" :title="pageTitle(t('设备'), 2)" :aria-keyshortcuts="pageChord(2)" :class="{ active: tab === 'devices' }" @click="tab = 'devices'">
          <Monitor :size="18" /><span>{{ t("设备") }}</span>
        </button>
        <button :aria-label="t('收藏库')" :title="pageTitle(t('收藏库'), 3)" :aria-keyshortcuts="pageChord(3)" :class="{ active: tab === 'favorites' }" @click="tab = 'favorites'">
          <Star :size="18" /><span>{{ t("收藏库") }}</span>
        </button>
        <button :aria-label="t('文件传输')" :title="pageTitle(t('文件传输'), 4)" :aria-keyshortcuts="pageChord(4)" :class="{ active: tab === 'transfers' }" @click="tab = 'transfers'">
          <FileUp :size="18" /><span>{{ t("文件传输") }}</span>
        </button>
        <button :aria-label="t('附近聊天')" :title="pageTitle(t('附近聊天'), 5)" :aria-keyshortcuts="pageChord(5)" :class="{ active: tab === 'chat' }" @click="tab = 'chat'">
          <MessageCircle :size="18" /><span>{{ t("附近聊天") }}</span>
          <!-- The count reaches the reader on whatever page they are on, the
               way the panel's own chat row carried it.  The label above stays
               the page's name alone: a screen reader announcing the name and
               then the number would read the two as one name. -->
          <b v-if="chatUnread" class="nav-count" :aria-label="t('{count} 条未读消息', { count: chatUnread })">{{ chatUnread }}</b>
        </button>
        <!-- AI configuration is a page of its own rather than a card in the
             settings page: it is not a preference this machine holds but a
             workbench over files — an inventory that is walked, opened, edited,
             moved to the trash, and pulled from a paired device — and it was
             the longest card in a window whose cards are meant to be one
             question each.  It sits with the pages that hold content rather
             than behind the rule with the window's own configuration. -->
        <button :aria-label="t('AI 配置')" :title="pageTitle(t('AI 配置'), 6)" :aria-keyshortcuts="pageChord(6)" :class="{ active: tab === 'ai' }" @click="openAiPage">
          <Sparkles :size="18" /><span>{{ t("AI 配置") }}</span>
        </button>
        <!-- Settings is not another page of content: it is the window's own
             configuration, so it sits below the six pages that hold content,
             behind a rule, the way a mature desktop sidebar carries it. -->
        <div class="sidebar-settings">
          <button :aria-label="t('设置')" :title="pageTitle(t('设置'), 7)" :aria-keyshortcuts="pageChord(7)" :class="{ active: tab === 'settings' }" @click="openSettings">
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

    <main>
      <header>
        <div><h1>{{ heading }}</h1>
          <p class="muted">{{ summary }}</p>
        </div>
        <div class="connection"><Circle :size="8" :class="{ connected: ready }" fill="currentColor" />{{ health }}</div>
      </header>

      <div v-if="state.error" role="alert" class="error-band">
        <AlertCircle :size="19" />
        <div><strong>{{ state.error.message }}</strong><small>{{ state.error.code }}</small></div>
        <!-- A data directory the sidecar refuses to start on is the one error a
             retry cannot fix: the repair has to run outside the sidecar. -->
        <button v-if="dataInvalid" class="icon-button" :title="t('修复数据')" :aria-label="t('修复数据')" :disabled="state.pending" @click="recoverOpen = true"><Wrench :size="18" /></button>
        <button v-if="state.error.retryable" class="icon-button" :title="t('重新连接')" :aria-label="t('重新连接')" @click="store.reconnect"><RefreshCw :size="18" /></button>
      </div>

      <div class="content">
        <form v-if="state.status?.health === 'locked'" class="unlock" @submit.prevent="unlock">
          <LockKeyhole :size="34" /><h2>{{ t("解锁 ClipSync") }}</h2>
          <label for="password">{{ t("加密密码") }}</label>
          <input id="password" v-model="password" type="password" autocomplete="current-password" maxlength="1024" required />
          <button class="primary" type="submit" :disabled="state.pending || !password">{{ t("解锁") }}</button>
        </form>

        <OverviewView v-else-if="tab === 'overview'"
          :store="store" :enabled="overviewAvailable" :sync-running="state.status?.sync_state === 'running'"
          :companion-on="!!companion?.enabled" :companion-url="companion?.access_url || null"
          :pause-left-ms="pauseLeftMs" :pause-busy="pauseBusy"
          @pause="pauseSync" @resume="resumeSync" @companion="configureCompanion"
          @rename="renameThisDevice" @qr="openCompanionQr" @send-url="openSendUrlFromHost"
          @devices="openPage('devices')" @history="openPage('history')" @diagnostics="openDiagnosticsCard"
          @row-menu="historyMenu" />

        <template v-else-if="tab === 'history'">
          <div class="toolbar">
            <label class="search"><Search :size="17" /><input
              ref="searchInput"
              :value="state.query" :aria-label="t('搜索历史记录')" :placeholder="t('搜索历史记录')"
              :disabled="busy" @input="store.search(($event.target as HTMLInputElement).value)"
            /></label>
            <button class="icon-button" :title="t('刷新')" :aria-label="t('刷新')" :disabled="historyBusy" @click="refreshHistoryNow"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
            <button class="icon-button" :title="t('清空历史')" :aria-label="t('清空历史')" :disabled="historyBusy || !state.total" @click="clearHistoryOpen = true"><Eraser :size="18" /></button>
          </div>
          <div v-if="state.hasHistory" class="history-filters" role="group" :aria-label="t('按类型筛选历史记录')">
            <button v-for="chip in kindChips" :key="chip.id" class="chip"
              :class="{ 'chip--active': state.kind === chip.id }"
              :aria-pressed="state.kind === chip.id"
              :disabled="historyBusy || kindUnavailable(chip.id)"
              :title="kindUnavailable(chip.id) ? t('该类型暂无内容') : ''"
              @click="store.setKind(chip.id)">
              <span v-if="chip.icon" aria-hidden="true">{{ chip.icon }}</span>{{ chip.label }}<span class="chip-count">{{ kindCount(chip.id) }}</span>
            </button>
            <button class="chip chip--sort" :title="t('切换历史排序（最新 / 最旧）')" :disabled="historyBusy" @click="store.toggleSort">
              {{ state.sort === "newest" ? "↓ " : "↑ " }}{{ state.sort === "newest" ? t("最新优先") : t("最旧优先") }}
            </button>
          </div>
          <section class="history-list" :aria-label="t('历史记录')" :aria-busy="state.loading">
            <div class="list-heading">
              <label class="select-visible"><input type="checkbox" :aria-label="t('全选当前页')"
                :checked="allSelected" :indeterminate="state.selectedIds.length > 0 && !allSelected"
                :disabled="historyBusy || !visibleIds.length || visibleIds.length > 100"
                @change="store.selectAllVisible(($event.target as HTMLInputElement).checked)" />{{ t("全选当前页") }}</label>
              <span role="status">{{ t("已选择 {count} 条", { count: state.selectedIds.length }) }}</span>
              <div class="batch-actions">
                <button class="icon-button" :aria-label="t('批量收藏')" :title="t('批量收藏')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="pinSelected(true)"><Pin :size="17" /></button>
                <button class="icon-button" :aria-label="t('批量取消收藏')" :title="t('批量取消收藏')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="pinSelected(false)"><PinOff :size="17" /></button>
                <button class="icon-button" :aria-label="t('加入收藏夹')" :title="t('加入收藏夹')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="favoriteSelected"><Star :size="17" /></button>
                <button class="icon-button" :aria-label="t('合并推送到电脑')" :title="t('合并推送到电脑')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="mergePushSelected"><SendHorizontal :size="17" /></button>
                <button class="icon-button" :aria-label="t('批量删除')" :title="t('批量删除')" :disabled="historyBusy || mergeBusy || !state.selectedIds.length" @click="batchDeleteIds = [...state.selectedIds]"><Trash2 :size="17" /></button>
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
              <p class="note">{{ state.kind !== 'all' ? t("换个过滤条件，或清除过滤查看全部内容。") : (native ? t('当前数据目录中没有可显示的记录') : t('桌面窗口连接后显示本地记录')) }}</p>
            </div>
            <article v-for="(item, index) in state.history" :key="item.id" class="history-row"
              :class="{ 'history-row--kbd': state.kbdIndex === index }"
              @contextmenu.prevent="historyMenu($event, item)"
              @mouseenter="showPreview(item)" @mouseleave="hidePreview(item)"
              @focusin="showPreview(item)" @focusout="hidePreview(item)">
              <label class="history-selection"><input type="checkbox" :aria-label="t('选择记录 {id}', { id: item.id })"
                :checked="state.selectedIds.includes(item.id)"
                :disabled="historyBusy || !item.id.trim() || (!state.selectedIds.includes(item.id) && state.selectedIds.length >= 100)"
                @change="store.select(item.id, ($event.target as HTMLInputElement).checked)" /></label>
              <div class="history-content"><p>{{ previewLabel(item) }}</p>
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
              <time :datetime="new Date(item.timestamp * 1000).toISOString()">{{ dateTime(item.timestamp) }}</time>
              <div class="row-actions">
                <button v-if="!isRemoteFile(item)" class="icon-button" :aria-label="t('复制记录')" :title="t('复制记录')" :disabled="historyBusy" @click="store.copy(item)"><Check v-if="state.copiedId === item.id" :size="17" /><Copy v-else :size="17" /></button>
                <!-- The one row whose action is not 复制: the file is not on this
                     machine, so what the row offers is the ask.  It replaces
                     rather than sits beside the copy button — two buttons on one
                     row where only one of them can work is worse than one. -->
                <button v-if="isRemoteFile(item)" class="icon-button" :aria-label="t('下载文件')" :title="remoteFileTitle(item)" :disabled="historyBusy || remoteFileBusy(item)" @click="downloadRemoteFile(item)"><Check v-if="remoteFileBusy(item)" :size="17" /><Download v-else :size="17" /></button>
                <button class="icon-button" :class="{ pinned: item.pinned }" :aria-label="item.pinned ? t('取消收藏') : t('收藏')" :title="item.pinned ? t('取消收藏') : t('收藏')" :disabled="historyBusy" @click="store.pin(item)"><Pin :size="17" /></button>
                <button class="icon-button" :aria-label="t('翻译记录')" :title="t('翻译记录')" :disabled="historyBusy || translateItemReading" @click="openTranslateItem(item)"><Globe :size="17" /></button>
                <button v-if="isWebLink(item)" class="icon-button" :aria-label="t('在浏览器打开')" :title="t('在浏览器打开')" :disabled="historyBusy" @click="openHistoryLink(item)"><ExternalLink :size="17" /></button>
                <button class="icon-button" :aria-label="t('删除记录')" :title="t('删除记录')" :disabled="historyBusy" @click="deleteItem = item"><Trash2 :size="17" /></button>
              </div>
              <!-- Non-interactive, so it never blocks the row's own controls or
                   the row below it — the panel's card was `pointer-events: none`
                   for the same reason. -->
              <div v-if="previewCard?.id === item.id" class="history-preview" aria-hidden="true">
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
          </section>
          <footer class="pagination">
            <span>{{ state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)}` : '0' }} / {{ state.total }}</span>
            <button class="icon-button" :aria-label="t('上一页')" :title="t('上一页')" :disabled="state.offset === 0 || historyBusy" @click="store.page(-1)"><ChevronLeft :size="18" /></button>
            <button class="icon-button" :aria-label="t('下一页')" :title="t('下一页')" :disabled="state.offset + state.limit >= state.total || historyBusy" @click="store.page(1)"><ChevronRight :size="18" /></button>
          </footer>
        </template>

        <FavoritesView v-else-if="tab === 'favorites'" :store="store.favorites"
          :enabled="favoritesAvailable" :notify="notifyFavorites" />
        <TransfersView
          v-else-if="tab === 'transfers'"
          :devices="state.devices"
          :dropped="droppedPaths"
          :notify="notifyTransfers"
          @dropped="droppedPaths = []"
        />
        <!-- The receipt map comes from the store rather than a chat-local
             fetch: it is fed by the event stream, so a message that was sent
             before this tab was ever opened still shows how it ended. -->
        <ChatView v-else-if="tab === 'chat'" :receipts="delivery.state.messages"
          :open-session="chatSessionToOpen" :notify="notifyChat" @opened="chatSessionToOpen = ''"
          @selected="store.setOpenChatSession" />
        <!-- The AI page: the configuration files this machine's AI tools keep,
             and the same question asked of a paired device.  It was the
             settings page's longest card, and three things about it were wrong
             there: it is not a preference — it walks a disk, edits files, moves
             them to the trash and pulls them from a peer — it was the one card
             that showed two quite different lists depending on a select, and
             its two real form fields were saved by a button on a page the
             reader had to walk back to.  As a page it is laid out as the two
             things it does: the tools and their files on the left, another
             machine on the right, which is also the pair the diff line in the
             right-hand card is about. -->
        <section v-else-if="tab === 'ai'" class="ai-page">
          <!-- Three questions, one at a time: which tools this machine reads
               and writes, what is in the directories they keep, and the same
               second question asked of a paired device.  They were three cards
               in one scroll, so the peer's list — the half the page exists for,
               since it is the one with 拉取 on it — sat below everything this
               machine has. -->
          <nav class="page-tabs" :aria-label="t('AI 配置分区')">
            <button v-for="section in aiSections" :key="section.id" type="button"
              :class="{ 'page-tab--active': aiTab === section.id }"
              :aria-current="aiTab === section.id ? 'page' : undefined"
              @click="aiTab = section.id">{{ section.label }}</button>
          </nav>

          <div v-if="aiTab === 'tools'" class="page-col">
              <section class="settings-section">
                <h2>{{ t("AI 工具") }}</h2>
                <!-- Which tools are read and written on this machine, and the
                     directories they keep their configuration in.  These two
                     fields are this page's own form, and the button under them
                     is what writes it: they rode the settings page's form until
                     this page existed, and a field saved by another page's
                     button is a field whose save the reader has to go and look
                     for.  The tools are the sidecar's list, so a machine that
                     knows none of them says so rather than showing nothing. -->
                <label v-for="tool in aiTools" :key="tool.key" class="setting setting--check">
                  <span class="setting-control"><input type="checkbox" :value="tool.key" v-model="aiEnabled" /><span>{{ tool.label }}</span></span>
                </label>
                <p v-if="!aiProfilesLoaded" class="note setting-block setting-block--card">{{ t("正在读取本机可用的 AI 工具…") }}</p>
                <p v-else-if="!aiTools.length" class="note setting-block setting-block--card">{{ t("本机没有可同步的 AI 工具。") }}</p>
                <label class="setting">
                  <span class="setting-name">{{ t("自定义配置路径") }}</span>
                  <span class="setting-control"><textarea v-model="aiCustomPaths" rows="3" :placeholder="t('每行一个路径')"></textarea></span>
                </label>
                <p class="note setting-note">{{ t("勾选要同步的 AI 工具；工具不认识的目录写在下面，每行一个路径。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="primary" :disabled="aiProfilesBusy || !aiProfilesLoaded" @click="saveAiProfiles"><Save :size="16" />{{ t("保存 AI 配置") }}</button>
                  <span v-if="aiProfilesDirty" class="settings-save-status" role="status">{{ t("有未保存的更改") }}</span>
                  <span v-else-if="aiProfilesLoaded" class="settings-save-status" role="status">{{ t("所有更改都已保存") }}</span>
                </div>
              </section>
          </div>

          <div v-else-if="aiTab === 'local'" class="page-col">
              <section class="settings-section">
                <h2>{{ t("本机配置") }}</h2>
                <!-- The inventory this machine's own tools keep.  It is walked
                     by the button rather than on the way in: the walk reads
                     every configured directory on disk, which is not a status
                     to fetch quietly behind a page the reader may only be
                     passing through, and the line under the button is what the
                     last walk found. -->
                <div class="setting-actions setting-actions--card">
                  <button type="button" @click="refreshAiLocal"><RefreshCw :size="16" />{{ t("读取本机配置") }}</button>
                </div>
                <p v-if="aiMessage" class="muted setting-block setting-block--card">{{ aiMessage }}</p>
                <!-- A `div`, not a `p`: what it holds is a form control, and a
                     field inside a paragraph is markup the browser has to close
                     for itself — which is where the list's indent came from. -->
                <div v-if="aiLocalItems.length" class="setting-block"><input v-model="aiLocalQuery" type="search" class="ai-filter" :aria-label="t('搜索本机配置')" :placeholder="t('筛选路径…')" /></div>
                <ul v-if="aiLocalPageItems.length" class="ai-local-list setting-block">
                  <!-- A header wherever the tool changes, including at the top of a
                       page that continues one: the rows are paged, not the groups, so
                       a page boundary can land inside a group. -->
                  <template v-for="(row, index) in aiLocalPageItems" :key="row.key">
                    <li v-if="index === 0 || aiLocalPageItems[index - 1].tool !== row.tool" class="ai-group-head">
                      <span>{{ aiGroupFor(aiLocalGroups, row.tool)?.label || row.tool }}</span>
                      <span class="ai-group-count">{{ aiGroupFor(aiLocalGroups, row.tool)?.itemCount ?? 0 }}</span>
                    </li>
                    <li class="ai-row" :style="row.depth ? { paddingLeft: `${12 + row.depth * 18}px` } : undefined">
                      <button v-if="row.isDir" type="button" class="ai-tree-chevron" :class="{ 'ai-tree-chevron--open': aiFolderOpen(row, aiLocalExpanded) }" :aria-label="t('展开或折叠 {name}', { name: row.label })" :aria-expanded="aiFolderOpen(row, aiLocalExpanded)" @click="toggleAiLocalFolder(row.node)"><ChevronRight :size="12" /></button>
                      <span v-else class="ai-tree-chevron" aria-hidden="true"></span>
                      <!-- A folder's name is the other half of the fold control, which
                           is what a reader tries first; a file's name is plain text. -->
                      <button v-if="row.isDir" type="button" class="ai-path-btn" :title="t('展开或折叠 {name}', { name: row.label })" @click="toggleAiLocalFolder(row.node)">📁 {{ row.label }}</button>
                      <span v-else>{{ row.label }}</span>
                      <span v-if="row.rootHint" class="ai-root-hint">{{ row.rootHint }}</span>
                      <!-- Editing and trashing are offered where the inventory listed
                           the row itself: a folder the tree built from its children
                           is opened by its name, and nothing here has a file to edit
                           or a listed folder to move. -->
                      <button v-if="row.entry && !row.isDir" type="button" class="icon-button icon-button--sm" :disabled="aiMutationBusy" :aria-label="t('编辑 {path}', { path: aiRowPath(row) })" :title="t('编辑')" @click="requestAiRead(aiRowItem(row))"><FileUp :size="15" /></button>
                      <button type="button" class="icon-button icon-button--sm" :aria-label="t('打开 {path}', { path: aiRowPath(row) })" :title="t('打开')" @click="openAiLocal(aiRowItem(row))"><LogOut :size="15" /></button>
                      <button v-if="row.entry" type="button" class="icon-button icon-button--sm" :disabled="aiMutationBusy" :aria-label="t('移入回收区 {path}', { path: aiRowPath(row) })" :title="t('移入回收区')" @click="aiTrashItem = aiRowItem(row)"><Trash2 :size="15" /></button>
                    </li>
                  </template>
                </ul>
                <!-- Says so rather than showing an empty list: a filter that matched
                     nothing and an inventory that is empty look identical otherwise. -->
                <p v-else-if="aiLocalItems.length" class="note setting-block">{{ t("没有匹配的配置项") }}</p>
                <nav v-if="aiLocalPageCount > 1" :aria-label="t('本地配置分页')" class="pagination">
                  <button type="button" class="icon-button" :aria-label="t('上一页本地配置')" :title="t('上一页')" :disabled="aiLocalPage === 0" @click="aiLocalPage--"><ChevronLeft :size="16" /></button>
                  <span>{{ aiLocalPage + 1 }} / {{ aiLocalPageCount }}</span>
                  <button type="button" class="icon-button" :aria-label="t('下一页本地配置')" :title="t('下一页')" :disabled="aiLocalPage + 1 >= aiLocalPageCount" @click="aiLocalPage++"><ChevronRight :size="16" /></button>
                </nav>
                <div v-if="aiSelectedItem" class="ai-editor">
                  <strong>{{ aiSelectedItem.rel_path }}</strong>
                  <span v-if="aiEditorDirty" class="muted">{{ t("未保存") }}</span>
                  <textarea v-model="aiEditorContent" :disabled="aiMutationBusy" rows="8" maxlength="262144" :aria-label="t('AI 配置编辑器')"></textarea>
                  <button type="button" class="primary" :disabled="aiMutationBusy" @click="saveAiLocal"><Save :size="16" />{{ t("保存配置") }}</button>
                </div>
              </section>
          </div>

          <div v-else class="page-col">
              <section class="settings-section">
                <h2>{{ t("其他设备") }}</h2>
                <!-- Which device this card is about.  The local inventory has a
                     card of its own now, so the picker no longer chooses which
                     half of one card is on screen: it names the peer, and every
                     row under it is that peer's — including the ones the peer
                     reported without being asked, which is what the counts in
                     the options are. -->
                <label class="setting">
                  <span class="setting-name">{{ t("查看设备") }}</span>
                  <span class="setting-control"><select v-model="aiPeerId" class="setting-wide" :aria-label="t('AI 配置远程设备')">
                    <option value="">{{ t("请选择设备") }}</option>
                    <!-- A device paired by code is left out: this card reads a
                         peer's inventory over the LAN link, and a device that
                         has none would be an option whose every button fails. -->
                    <option v-for="device in state.devices.filter(device => device.paired && !device.relay)" :key="device.id" :value="device.id">{{ device.name }}{{ aiPeerDiffSuffix(device.id) }}</option>
                  </select></span>
                </label>
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="!aiPeerId" @click="refreshAiRemote()"><RefreshCw :size="16" />{{ t("读取远程库存") }}</button>
                  <!-- The wizard is opened with no device chosen as often as with
                       one, and says so; it reads its own inventory either way. -->
                  <button type="button" @click="openAiMigrate"><Wand2 :size="16" />{{ t("迁移向导") }}</button>
                </div>
                <!-- The peer's half.  The class styles nothing — it is the marker
                     the tests scope their row assertions with, since both lists
                     share `.ai-local-list`. -->
                <div v-if="aiPeerId" class="ai-remote">
                  <label class="setting">
                    <span class="setting-name">{{ t("拉取方式") }}</span>
                    <span class="setting-control"><select v-model="aiPullMode" :aria-label="t('AI 配置拉取方式')">
                      <option value="copy">{{ t("创建副本") }}</option>
                      <option value="overwrite">{{ t("覆盖本机配置") }}</option>
                      <option value="append">{{ t("追加到本机配置") }}</option>
                    </select></span>
                  </label>
                  <!-- How the pull is going, and how it ended.  A pull is two
                       steps: the peer accepts N requests, then the files arrive
                       one at a time.  Only the first was ever reported, so a
                       pull that worked looked exactly like one that was ignored
                       — the count below is the second half, and the finished
                       line stays until the next pull replaces it. -->
                  <p v-if="aiPullProgress" class="setting-block ai-pull-status" role="status">
                    <RefreshCw :size="14" class="spinning" />{{ t("正在接收 {done} / {total} 个文件", { done: aiPullProgress.done + aiPullProgress.failed, total: aiPullProgress.total }) }}
                  </p>
                  <p v-else-if="aiRemoteMessage" class="setting-block ai-pull-status" :class="aiPullOutcome ? `ai-pull-status--${aiPullOutcome}` : undefined" :role="aiPullOutcome ? 'status' : undefined">
                    <Check v-if="aiPullOutcome === 'done'" :size="14" /><AlertCircle v-else-if="aiPullOutcome === 'failed'" :size="14" />{{ aiRemoteMessage }}
                  </p>
                  <!-- The line the page exists for: how this machine and the
                       peer disagree, before anything is pulled. -->
                  <p v-if="aiRemoteItems.length && aiLocalIndex" class="note setting-block" :class="{ 'ai-diff-ok': aiDiff.total === 0 }">{{ aiDiff.total === 0 ? t("与对方一致") : t("与对方不同：缺失 {missing}、对方较新 {remote}、本机较新 {local}", { missing: aiDiff.missing, remote: aiDiff.remote_newer, local: aiDiff.local_newer }) }}</p>
                  <p v-else-if="aiRemoteItems.length" class="note setting-block">{{ t("尚未读取本机配置，暂时无法对比版本差异") }}</p>
                  <div v-if="aiRemoteItems.length" class="setting-block"><input v-model="aiRemoteQuery" type="search" class="ai-filter" :aria-label="t('搜索远程配置')" :placeholder="t('筛选路径…')" /></div>
                  <ul v-if="aiRemotePageItems.length" class="ai-local-list setting-block">
                    <template v-for="(row, index) in aiRemotePageItems" :key="row.key">
                      <li v-if="index === 0 || aiRemotePageItems[index - 1].tool !== row.tool" class="ai-group-head">
                        <span>{{ aiGroupFor(aiRemoteGroups, row.tool)?.label || row.tool }}</span>
                        <span class="ai-group-count">{{ aiGroupFor(aiRemoteGroups, row.tool)?.itemCount ?? 0 }}</span>
                      </li>
                      <li class="ai-row" :style="row.depth ? { paddingLeft: `${12 + row.depth * 18}px` } : undefined">
                        <button v-if="row.isDir" type="button" class="ai-tree-chevron" :class="{ 'ai-tree-chevron--open': aiFolderOpen(row, aiRemoteExpanded) }" :aria-label="t('展开或折叠 {name}', { name: row.label })" :aria-expanded="aiFolderOpen(row, aiRemoteExpanded)" @click="toggleAiRemoteFolder(row.node)"><ChevronRight :size="12" /></button>
                        <span v-else class="ai-tree-chevron" aria-hidden="true"></span>
                        <!-- A folder's box ticks the files under it rather than the
                             folder row: the box is a shortcut for them, and a folder
                             that holds nothing has nothing to shortcut to. -->
                        <input v-if="!row.isDir" type="checkbox" :aria-label="t('选择 {path}', { path: aiRowPath(row) })" :checked="aiRemoteSelected.includes(aiEntryKey(row.entry))" @change="toggleAiRemote(row)" />
                        <input v-else-if="aiFolderState(row)" type="checkbox" :aria-label="t('选择文件夹 {path} 下的全部文件', { path: aiRowPath(row) })" :checked="aiFolderState(row) === 'all'" :indeterminate="aiFolderState(row) === 'some'" @change="toggleAiRemote(row)" />
                        <FolderOpen v-else :size="15" class="ai-folder-mark" aria-hidden="true" />
                        <button v-if="row.isDir" type="button" class="ai-path-btn" :title="t('展开或折叠 {name}', { name: row.label })" @click="toggleAiRemoteFolder(row.node)">📁 {{ row.label }}</button>
                        <span v-else>{{ row.label }}</span>
                        <span v-if="row.rootHint" class="ai-root-hint">{{ row.rootHint }}</span>
                        <!-- Silence means "same": a badge on every row would make
                             the differing ones harder to find, not easier.  A
                             folder carries the same badge for its own subtree,
                             because it is folded by default — the difference a
                             skill holds was invisible until the reader opened
                             it, which is the one place they cannot see it from. -->
                        <span v-for="badge in aiFolderDiff(row)" :key="badge.state" class="ai-version" :class="`ai-version--${badge.state}`">{{ badge.label }}<span class="ai-version-count">{{ badge.count }}</span></span>
                        <span v-if="!row.isDir && aiRowDiff(row.entry) && aiRowDiff(row.entry) !== 'same'" class="ai-version" :class="`ai-version--${aiRowDiff(row.entry)}`">{{ aiRowDiffLabel(row.entry) }}</span>
                        <button v-if="row.entry && !row.isDir" type="button" class="icon-button icon-button--sm" :title="t('预览')" :aria-label="t('预览 {path}', { path: aiRowPath(row) })" @click="previewAiRemote(aiRowItem(row))"><Search :size="15" /></button>
                        <button v-if="row.entry && !row.isDir" type="button" class="icon-button icon-button--sm" :title="t('拉取')" :aria-label="t('拉取 {path}', { path: aiRowPath(row) })" @click="requestAiPull(aiRowItem(row))"><FileUp :size="15" /></button>
                      </li>
                    </template>
                  </ul>
                  <p v-else-if="aiRemoteItems.length" class="note setting-block">{{ t("没有匹配的配置项") }}</p>
                  <!-- An empty list is two different states, and the reader needs
                       to be able to tell them apart: a peer that has never
                       answered, and a peer that answered with nothing. -->
                  <p v-else-if="aiRemoteRead" class="note setting-block">{{ t("该设备还没有可同步的配置项") }}</p>
                  <p v-else class="note setting-block">{{ t("尚未读取该设备的配置") }}</p>
                  <p v-if="aiRemoteItems.length && aiRemoteLegacy" class="note setting-block" role="status">{{ t("该设备版本过旧，只能浏览，不能作为迁移来源") }}</p>
                  <div v-if="aiRemoteItems.length" class="setting-actions">
                    <label class="sync-toggle">
                      <input type="checkbox" :aria-label="t('选择本页全部远程配置')" :checked="aiRemotePageAllSelected" :indeterminate="aiRemotePageSomeSelected" @change="toggleAiRemotePage()" />
                      <span>{{ t("选择本页") }}</span>
                    </label>
                    <button type="button" :disabled="!aiDiff.missing" @click="selectAiRemoteMissing()">{{ t("选择缺失项（{count}）", { count: aiDiff.missing }) }}</button>
                    <button type="button" :disabled="aiRemoteLegacy || !aiRemoteSelectedTargets.length" @click="requestAiPullBatch()"><FileUp :size="16" />{{ t("拉取选中项（{count}）", { count: aiRemoteSelectedTargets.length }) }}</button>
                    <button type="button" :disabled="!aiRemoteSelected.length" @click="aiRemoteSelected = []">{{ t("清除选择") }}</button>
                  </div>
                  <nav v-if="aiRemotePageCount > 1" :aria-label="t('远程配置分页')" class="pagination">
                    <button type="button" class="icon-button" :aria-label="t('上一页远程配置')" :title="t('上一页')" :disabled="aiRemotePage === 0" @click="aiRemotePage--"><ChevronLeft :size="16" /></button>
                    <span>{{ aiRemotePage + 1 }} / {{ aiRemotePageCount }}</span>
                    <button type="button" class="icon-button" :aria-label="t('下一页远程配置')" :title="t('下一页')" :disabled="aiRemotePage + 1 >= aiRemotePageCount" @click="aiRemotePage++"><ChevronRight :size="16" /></button>
                  </nav>
                </div>
                <p v-else class="note setting-block setting-block--card">{{ t("选择一台已配对设备后，可以对比并拉取它的 AI 工具配置。") }}</p>
              </section>
          </div>
        </section>
        <section v-else-if="tab === 'settings'" class="settings-panel">
          <div class="settings-layout">
            <!-- The legacy Settings window searched itself the same way: over
                 the text the reader can see, marking which parts of the page
                 hold it.  The box is the page's rather than the strip's — it is
                 about all eleven cards, and a reader with a query in mind
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
            <!-- Two rows, because eleven cards answer two questions: which part
                 of the app this is about, and which card inside it.  The four
                 groups are the table of contents; the row under them is the
                 group the reader is in.  Both open cards — a group opens its
                 first — so neither row is a label with nothing to aim at, and
                 the pair is what lets the page be browsed in two steps instead
                 of eleven.  The group row stays while the section row changes
                 under it, which is what keeps the reader's place: the whole
                 strip is the same width whichever group is open. -->
            <nav class="settings-nav" :aria-label="t('设置分区')">
              <div class="settings-nav-groups">
                <button v-for="group in settingsGroups" :key="group.label" type="button"
                  :class="{
                    active: activeSettingsGroup === group,
                    'has-match': settingsSearching && settingsGroupMatches(group) > 0,
                    dimmed: settingsSearching && !settingsGroupMatches(group),
                  }"
                  :aria-current="activeSettingsGroup === group ? 'true' : undefined"
                  @click="openSettingsGroup(group)">{{ group.label }}<span
                    v-if="settingsSearching && settingsGroupMatches(group)"
                    class="settings-match-count"> · {{ settingsGroupMatches(group) }}</span><span
                    v-if="settingsGroupHasEdits(group)" class="settings-edited" role="img"
                    :title="t('有未保存的更改')" :aria-label="t('有未保存的更改')"></span></button>
              </div>
              <div v-for="group in settingsGroups" :key="group.label" class="settings-nav-group"
                v-show="activeSettingsGroup === group">
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
              @input="markSettingsEdited" @change="markSettingsEdited">
              <section v-show="showSettingsCard('general')" id="settings-general" class="settings-section">
                <h2>{{ t("常规") }}</h2>
                <label class="setting">
                  <span class="setting-name">{{ t("设备名称") }}</span>
                  <span class="setting-control"><input v-model="settings.device_name" maxlength="128" required /></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("主题") }}</span>
                  <span class="setting-control"><select v-model="settings.appearance_mode"><option value="system">{{ t("跟随系统") }}</option><option value="light">{{ t("浅色") }}</option><option value="dark">{{ t("深色") }}</option></select></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("语言") }}</span>
                  <span class="setting-control"><select :value="currentLocale" :aria-label="t('语言')" @change="applyLanguage(($event.target as HTMLSelectElement).value)"><option v-for="code in LOCALES" :key="code" :value="code">{{ LOCALE_NAMES[code] }}</option></select></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.auto_start" type="checkbox" /><span>{{ t("开机自动启动") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.sound_enabled" type="checkbox" /><span>{{ t("启用声音") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.ui_animation_enabled" type="checkbox" /><span>{{ t("启用动画") }}</span></span>
                </label>
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
                  <legend>{{ t("互联网同步") }}</legend>
                  <label class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.internet_sync_enabled" type="checkbox" /><span>{{ t("启用互联网同步") }}</span></span>
                  </label>
                  <!-- "One address per line" is an instruction a single-line
                       input cannot follow: the list a reader types here is
                       split on newlines (and commas), so it gets the same
                       multi-line control the process-name list in the next card
                       already uses, and the hint the legacy panel put under each. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("公共中继地址") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.relay_brokers" rows="3" :aria-label="t('公共中继地址')" :placeholder="t('每行一个地址')"></textarea>
                      <span class="note">{{ t("兜底通道：私有中继不可达时启用，并作为镜像让落在不同中继的设备仍能互通。公共中继接受任何客户端，也绝不会拿到你的中继密码。") }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("私有中继地址") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.relay_private_brokers" rows="3" :aria-label="t('私有中继地址')" :placeholder="t('每行一个地址')"></textarea>
                      <span class="note">{{ t("主通道：剪贴板数据优先走这些端点，用下方用户名和密码登录。留空则只使用上面的公共中继。") }}</span>
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
                      <input v-model.number="settings.relay_max_message_kb" :aria-label="t('单条消息大小上限')" type="number" :min="MIN_RELAY_MAX_MESSAGE_KB" :max="MAX_RELAY_MAX_MESSAGE_KB" step="1" />
                      <span class="note">{{ t("单位 KB。填中继服务器允许的单条消息上限，传输文件时每个数据块都会按它切分——超出会被服务器直接丢弃。免费中继常见 64 KB，公共中继为 256 KB。修改后需重启生效。") }}</span>
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
                  <label v-for="[key, label] in filterCategories" :key="key" class="setting setting--check">
                    <span class="setting-control"><input v-model="settings.filter_enabled_categories" type="checkbox" :value="key" /><span>{{ label }}</span></span>
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
                    <span class="setting-control"><textarea v-model="settings.app_filter_list" :disabled="!settings.app_filter_enabled" rows="4" :aria-label="t('应用过滤进程列表')" placeholder="chrome.exe&#10;secret*"></textarea></span>
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
                    <span class="setting-name">{{ t("同步去抖（秒）") }}</span>
                    <span class="setting-control"><input v-model.number="settings.sync_debounce" :aria-label="t('同步去抖（秒）')" type="number" min="0.05" max="10" step="0.05" /><span class="note">{{ t("两次发送同步之间的最小间隔（0.05–10 秒）") }}</span></span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("剪贴板轮询间隔（秒）") }}</span>
                    <span class="setting-control"><input v-model.number="settings.clipboard_poll_interval" :aria-label="t('剪贴板轮询间隔（秒）')" type="number" min="0.1" max="60" step="0.1" /></span>
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
                    <span class="setting-control"><input v-model.number="settings.port" :aria-label="t('TCP 端口')" type="number" min="1024" max="65535" step="1" /><span class="note">{{ t("（1024–65535，需重启）") }}</span></span>
                  </label>
                  <!-- This hint describes the service-type row, not the card, and
                       it used to sit above the port row instead — where it read as
                       a description of the card and explained the wrong field. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("mDNS 服务类型") }}</span>
                    <span class="setting-control"><input v-model="settings.service_type" :aria-label="t('mDNS 服务类型')" maxlength="128" /><span class="note">{{ t("通过 mDNS 广播的零配置服务类型。除非你知道自己在做什么，否则保持默认。重启后生效。") }}</span></span>
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
                  <span class="setting-control"><input v-model="settings.data_dir" class="setting-wide" :aria-label="t('数据目录')" maxlength="4096" :placeholder="t('留空使用默认')" /><span class="note">{{ t("配置 / 历史 / 收藏的存储位置。重启后生效。") }}</span></span>
                </label>
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
                <label class="setting">
                  <span class="setting-name">{{ settings.password_set ? t("更换加密密码") : t("设置加密密码") }}</span>
                  <span class="setting-control"><input v-model="securityPassword" :aria-label="t('加密密码')" type="password" autocomplete="new-password" maxlength="200" :disabled="securityBusy" /></span>
                </label>
                <label class="setting">
                  <span class="setting-name">{{ t("确认加密密码") }}</span>
                  <span class="setting-control"><input v-model="securityPasswordConfirm" :aria-label="t('确认加密密码')" type="password" autocomplete="new-password" maxlength="200" :disabled="securityBusy" /></span>
                </label>
                <ul v-if="securityPassword" class="password-rules setting-block">
                  <li v-for="[rule, met] in passwordRules" :key="rule" :class="{ met }">{{ rule }}</li>
                </ul>
                <p v-if="passwordMismatch" class="note setting-block">{{ t("两次输入的密码不一致。") }}</p>
                <p class="note setting-note">{{ t("密码会随“保存设置”一起提交，并同时用作设备配对的通道密钥；剪贴板历史的密钥在重启后更新。") }}</p>
                <p class="muted setting-block">{{ settings.password_set ? t("已设置加密密码") : t("未设置加密密码") }}</p>
                <div v-if="settings.password_set" class="setting-actions">
                  <button type="button" class="danger-outline" :disabled="securityBusy" @click="clearPasswordOpen = true"><Trash2 :size="16" />{{ t("清除加密密码") }}</button>
                </div>
                <h3>{{ t("危险区域") }}</h3>
                <p class="note setting-note">{{ t("恢复出厂设置会删除全部历史、收藏、配对和设备身份，并重新启动应用。此操作无法撤销。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="danger-outline" :disabled="securityBusy" @click="factoryResetOpen = true"><Trash2 :size="16" />{{ t("恢复出厂设置") }}</button>
                </div>
              </section>
              <section v-show="showSettingsCard('backup')" id="settings-backup" class="settings-section">
                <h2>{{ t("数据备份") }}</h2>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="primary" @click="createBackup"><Save :size="17" />{{ t("创建备份") }}</button>
                  <button type="button" :disabled="restoreBusy" @click="restoreBackup">{{ t("选择备份恢复") }}</button>
                  <button type="button" @click="importHistory">{{ t("导入历史") }}</button>
                  <button type="button" @click="exportHistory('json')">{{ t("导出 JSON") }}</button>
                  <button type="button" @click="exportHistory('csv')">{{ t("导出 CSV") }}</button>
                  <button type="button" @click="exportHistory('markdown')">{{ t("导出 Markdown") }}</button>
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
                    @change="toggleAutoUpdateCheck(($event.target as HTMLInputElement).checked)" /><span>{{ t("自动检查更新") }}</span></span>
                </label>
                <p class="note setting-note">{{ t("每约 6 小时检查一次 GitHub 是否有新版本；关闭后后台不再发起任何更新请求。") }}</p>
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
                  <p v-if="!updateInstallable" class="note setting-note">{{ t("请退出当前应用，然后用下方文件替换旧版本。剪贴板历史与设备仍保留在本机。") }}</p>
                  <p class="update-ready-path selectable setting-block">{{ updateState.path }}</p>
                  <div class="setting-actions setting-actions--card">
                    <!-- A blob that arrived from a peer is staged here and checked
                         against the release digest, but only the host's own
                         verified download can replace the bundle — so the
                         install the card can actually perform is offered beside
                         the folder, rather than leaving the reader to end the
                         exchange by hand. -->
                    <button v-if="updateInstallable" type="button" class="primary" @click="installUpdate">{{ t("下载并安装") }}</button>
                    <button type="button" @click="openUpdateFolder">{{ t("打开所在文件夹") }}</button>
                  </div>
                </template>
                <p v-if="updateState.phase === 'failed'" class="update-failed setting-block setting-block--card" role="alert">
                  {{ t("更新失败") }}{{ updateState.error ? '：' + updateState.error : '' }}</p>
                <p v-if="updateInstallable" class="note setting-note">{{ t("下载并安装最新版本，完成后应用会自动重启。") }}</p>
                <p v-else class="note setting-note">{{ t("自动下载最新版本，下载完成后提示你手动替换旧版本。") }}</p>
              </section>
              <section v-show="showSettingsCard('diagnostics')" id="settings-diagnostics" class="settings-section">
                <h2>{{ t("诊断与维护") }}</h2>
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="!diagnosticsAvailable || diagnosticsBusy" @click="openDiagnostics"><Stethoscope :size="17" />{{ t("运行诊断") }}</button>
                  <button type="button" @click="openLogs"><FileUp :size="17" />{{ t("查看日志") }}</button>
                  <button type="button" @click="openAbout"><Info :size="17" />{{ t("关于") }}</button>
                  <button type="button" @click="restartOpen = true"><RotateCcw :size="17" />{{ t("重启应用") }}</button>
                </div>
              </section>
              <div class="settings-save">
                <button class="primary" type="submit" :disabled="settingsBusy || !settingsLoaded || passwordBlocked"><Save :size="17" />{{ settingsSaved ? t('已保存') : t('保存设置') }}</button>
                <span v-if="settingsDirty" class="settings-save-status" role="status">{{ t("有未保存的更改") }}</span>
                <span v-else-if="settingsSaved" class="settings-save-status" role="status">{{ t("所有更改都已保存") }}</span>
              </div>
            </form>
          </div>
        </section>
        <section v-else class="device-list" :aria-busy="state.refreshing || state.pending">
          <!-- Three questions, one at a time.  Which machines this one knows,
               how a machine that is not on this network becomes one, and the
               phone service that lets a phone in: as a single scroll the second
               and third sat behind the whole of the first, so the reader who
               came for a pairing code had to pass a device list twice over. -->
          <nav class="page-tabs" :aria-label="t('设备页分区')">
            <button v-for="section in deviceSections" :key="section.id" type="button"
              :class="{ 'page-tab--active': deviceTab === section.id }"
              :aria-current="deviceTab === section.id ? 'page' : undefined"
              @click="deviceTab = section.id">{{ section.label }}</button>
          </nav>

          <template v-if="deviceTab === 'devices'">
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
              <button class="icon-button" :aria-label="t('刷新设备')" :title="t('刷新设备')" :disabled="busy" @click="refreshDevices"><RefreshCw :size="18" :class="{ spinning: state.refreshing }" /></button>
            </div>
            <div v-if="!state.devices.length" class="empty empty--page"><Monitor :size="36" /><h2>{{ state.refreshing ? t('正在读取设备') : t('暂无设备') }}</h2></div>
            <article v-for="device in activeDevices" :key="device.id" class="device-row"
              @contextmenu.prevent="deviceMenu($event, device)">
              <Monitor :size="25" class="device-icon" />
              <div class="device-identity"><h2>{{ device.name }}</h2><span class="note">{{ device.id }}</span></div>
              <!-- A device paired by internet code gets its own set, because
                   almost every button in the other one names something a code
                   pairing does not have: no certificate to revoke, no address
                   to dial, no update to offer (a peer with no local sighting
                   advertises no version).  What the relay *can* carry is a
                   conversation, a URL and a reachability test, and 打开聊天 is
                   a button here rather than only a menu entry because a device
                   that cannot be talked to from its own row is the thing this
                   list was missing. -->
              <div v-if="device.relay" class="row-actions">
                <button class="icon-button" :aria-label="t('打开聊天')" :title="t('打开聊天')" :disabled="busy" @click="chatWith(device)"><MessageCircle :size="18" /></button>
                <button class="icon-button" :aria-label="t('测试连接')" :title="t('测试连接')" :disabled="busy || !!probeBusyId" @click="testConnection(device)"><Activity :size="18" :class="{ spinning: probeBusyId === device.id }" /></button>
                <button class="icon-button" :aria-label="t('发送网址')" :title="t('发送网址')" :aria-describedby="sendUrlAvailable ? undefined : 'devices-engine-note'" :disabled="busy || !sendUrlAvailable" @click="openSendUrl(device)"><Globe :size="18" /></button>
                <button class="icon-button" :aria-label="t('解除互联网配对')" :title="t('解除互联网配对')" :disabled="busy" @click="relayUnpairDevice = device"><Unlink :size="18" /></button>
              </div>
              <div v-else class="row-actions">
                <button v-if="device.paired" class="icon-button" :aria-label="t('撤销信任')" :title="t('撤销信任')" :disabled="busy" @click="revokeDevice = device"><Unlink :size="18" /></button>
                <!-- Drawn on whether a dial can be placed, not on the connection
                     there happens to be — see `canDial`. -->
                <button v-else-if="!pairingPending(device)" :disabled="busy || !canDial(device)" @click="store.startPairing(device)"><Link :size="17" />{{ t("配对") }}</button>
                <button v-if="device.paired && device.connection_state !== 'online'" class="icon-button" :aria-label="t('连接设备')" :title="t('连接设备')" :disabled="busy" @click="store.connect(device)"><Plug :size="18" /></button>
                <button v-if="device.connection_state === 'online'" class="icon-button" :aria-label="t('断开连接')" :title="t('断开连接')" :disabled="busy" @click="store.disconnect(device)"><PlugZap :size="18" /></button>
                <!-- The relay's own actions, on a row that has a local route as
                     well.  A device can be paired by code *and* be here, and
                     then this row is its only row: the two buttons below reach
                     it over the relay (the sidecar's own rule is
                     reachability, not the LAN pin), and the conversation and
                     the internet pairing are things only the relay knows about.
                     Without them the row kept every button gated on the LAN
                     pairing the device does not have, while the 互联网 chip
                     beside them reported it online. -->
                <button v-if="relayPairing(device).paired && !device.paired" class="icon-button" :aria-label="t('打开聊天')" :title="t('打开聊天')" :disabled="busy" @click="chatWith(device)"><MessageCircle :size="18" /></button>
                <button v-if="device.paired || relayPairing(device).paired" class="icon-button" :aria-label="t('测试连接')" :title="t('测试连接')" :disabled="busy || !!probeBusyId" @click="testConnection(device)"><Activity :size="18" :class="{ spinning: probeBusyId === device.id }" /></button>
                <button v-if="device.paired || relayPairing(device).paired" class="icon-button" :aria-label="t('发送网址')" :title="t('发送网址')" :aria-describedby="sendUrlAvailable ? undefined : 'devices-engine-note'" :disabled="busy || !sendUrlAvailable" @click="openSendUrl(device)"><Globe :size="18" /></button>
                <button v-if="relayPairing(device).paired" class="icon-button" :aria-label="t('解除互联网配对')" :title="t('解除互联网配对')" :disabled="busy" @click="relayUnpairDevice = device"><Unlink :size="18" /></button>
                <!-- Offered to a device the sidecar says this build is ahead of
                     — same platform, older version — and it needs no pairing:
                     the peer requests the installer, and its own copy is
                     checked against the published release digest before it can
                     be installed.  What it sends is the installer this machine's
                     own upgrade kept, so a machine that has none to send has
                     nothing to offer, and says why rather than reaching for the
                     network: the device being offered can download it itself. -->
                <button v-if="device.update_available" class="icon-button" :aria-label="t('发送更新')" :title="device.update_cached ? t('把本机的安装包发送给该设备') : t('本机还没有安装包可发送：本机只保留自己升级时下载的那一个，对方可自行检查更新')" :disabled="busy || !!updateBusyId || !device.update_cached || !updateReachable(device)" @click="offerDeviceUpdate(device)"><FileUp :size="18" :class="{ spinning: updateBusyId === device.id }" /></button>
                <!-- The mirror of the button above, and the direction the
                     feature is meant to run in: this device is the one behind,
                     so this is the side with a reason to click.  It asks the
                     peer for the installer it already downloaded, and the blob
                     is checked against the published release digest before it
                     can be staged.  Shown only when the peer really is ahead —
                     same platform, newer version — which is what the sidecar
                     decided for this row. -->
                <button v-if="device.update_fetchable" class="icon-button" :aria-label="t('获取更新')" :title="t('从该设备获取新版本安装包并安装')" :disabled="busy || !!fetchBusyId || !updateReachable(device)" @click="fetchDeviceUpdate(device)"><Download :size="18" :class="{ spinning: fetchBusyId === device.id }" /></button>
                <button class="icon-button" :aria-label="t('移除设备')" :title="t('移除设备')" :disabled="busy" @click="forgetDevice = device"><Trash2 :size="18" /></button>
              </div>
              <p v-if="probeResults[device.id]" class="note device-full" role="status">{{ t("连接测试：") }}{{ probeLabel(probeResults[device.id]) }}</p>
              <p v-if="updateNotes[device.id]" class="note device-full" role="status">{{ updateNotes[device.id] }}</p>
              <!-- The chips under the name, one per route rather than one
                   sentence for both.  A paired-and-online pair of words named a
                   state without
                   naming a route: a peer sitting on the relay with no way in
                   from this network read exactly like one in the next room.
                   The local chip is the connection the engine holds; the
                   internet chip is the relay's own view of the same device,
                   joined by device id.  A device with no internet pairing says
                   so rather than showing nothing, because "not paired over the
                   internet" and "paired and away" are two different answers. -->
              <span class="device-channels">
                <!-- A device paired by code says which pairing it is rather
                     than 已配对, and then not the local chip at all: 本地·离线
                     on a device that has no local route reads as a fault, and
                     there is nothing here it could ever say but that. -->
                <span v-if="device.relay" class="channel channel--paired"><ShieldCheck :size="12" />{{ t("互联网配对") }}</span>
                <template v-else>
                  <span class="channel channel--paired"><ShieldCheck :size="12" />{{ pairingLabel(device) }}</span>
                  <span class="channel" :class="`channel--${localChannelState(device)}`" :title="t('本地连接')">
                    <Plug :size="12" />{{ localChannelLabel(device) }}
                  </span>
                </template>
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
                <!-- What the device advertises about itself, and nothing when it
                     advertises nothing: an older peer sends no version at all,
                     and "unknown" is not the same as "up to date". -->
                <span v-if="device.version" class="channel"
                  :class="{ 'channel--offline': device.update_available }"
                  :title="device.update_available ? t('该设备版本较旧，可以发送更新') : t('对方软件版本')">
                  <Download :size="12" />{{ t("版本 {version}", { version: device.version }) }}
                </span>
              </span>
              <!-- A note lives on a saved LAN peer, and an internet-paired
                   device has none to hold one — its name is the alias the
                   pairing card writes, so the field would be a box that saves
                   nothing.  It gets the same dialog instead, from 重命名. -->
              <input v-if="device.paired && !device.relay" class="device-note" :value="device.note || ''" maxlength="512" :placeholder="t('设备备注')" :aria-label="t('设备备注')" @change="saveDeviceNote(device, $event)" />
              <div v-if="pairingPending(device)" class="pairing-controls">
                <p v-if="device.pairing_code">{{ t("配对码：") }}<strong>{{ device.pairing_code }}</strong></p>
                <p v-if="device.sas">{{ t("安全代码：") }}<strong>{{ device.sas }}</strong></p>
                <p class="note">{{ t("请核对两台设备上的代码，仅在一致时确认。") }}</p>
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
              <article v-for="device in archivedDevices" :key="device.id" class="device-row">
                <Monitor :size="25" class="device-icon" />
                <div class="device-identity"><h2>{{ device.name }}</h2><span class="note">{{ device.id }}</span></div>
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

          </template>

          <template v-else-if="deviceTab === 'internet'">
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
                <h2>{{ t("互联网配对") }}</h2>
                <div class="internet-head-actions card-head-actions">
                  <!-- Unconditional, and outside the relay-state line below:
                       that line only exists once the relay has reported a state,
                       and "the relay has not reported" is exactly when a reader
                       wants to ask it again. -->
                  <button type="button" class="icon-button" :title="t('刷新')" :aria-label="t('刷新互联网配对')" @click="refreshInternetPairingNow"><RefreshCw :size="17" /></button>
                  <label class="switch" :class="{ 'switch--on': internetPairingEnabled, 'switch--busy': internetSyncBusy }">
                    <input class="switch-input" type="checkbox" :aria-label="t('启用互联网同步')"
                      :checked="internetPairingEnabled" :disabled="internetSyncBusy"
                      @change="toggleInternetSync(($event.target as HTMLInputElement).checked)" />
                    <span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span>
                    <span class="switch-text">{{ internetPairingEnabled ? t("已开启") : t("已关闭") }}</span>
                  </label>
                </div>
              </div>
              <p class="note setting-note">{{ t("关闭后设备之间不再通过中继配对或同步，剪贴板也不离开局域网；这与设置页里的同名开关是同一个设置。") }}</p>
              <!-- This machine's own link to the relay, above the peers rather
                   than beside them.  Every other 在线 in this card is the
                   relay's view of *another* device, so when our link is down
                   the whole list reads 离线 and nothing on screen says whether
                   they are away or we are.  It is the first line of the card
                   for that reason. -->
              <p v-if="internetPairingEnabled && relayState" class="setting-block setting-block--card relay-state" :class="`relay-state--${relayState}`">
                <Activity :size="14" />{{ t("本机中继") }}：<strong>{{ relayStateLabel }}</strong>
                <span v-if="relayState !== 'online' && relayState !== 'connecting'" class="note">{{ t("对方在线与否以中继连接为准；本机中继不可用时，所有设备都会显示为离线。") }}</span>
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
                  <input class="pair-input"
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
                    <span class="pairing-wait" :title="row.since ? t('提交于 {when}', { when: dateTime(row.since) }) : ''">
                      <Clock :size="14" />{{ row.name ? t("等待 {name} 确认…", { name: row.name }) : t("等待对方确认…") }}
                    </span>
                    <button type="button" class="text-button" @click="unpairInternet(row.peer_id)">{{ t("撤销") }}</button>
                  </div>
                  <p class="note">{{ t("配对码已提交，对方通过中继确认后即会出现在上方设备列表中。") }}</p>
                </li>
              </ul>
              <!-- The paired devices, drawn as the rows on the other tab are —
                   an icon, a name, the route chips, then the one action that
                   belongs to the row.  They were a column of text buttons with
                   a bare 在线 beside them, which is the shape the device list
                   deliberately stopped using: a paired device is a device, and
                   it reads as one in both places now. -->
              <div class="internet-peers-head card-sub card-sub--row">
                <h3>{{ t("已配对的设备") }}</h3>
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
              <ul class="peer-list peer-list--devices">
                <li v-for="peer in internetPairing.peers" :key="peer.peer_id" class="internet-peer">
                  <Monitor :size="20" class="device-icon" />
                  <div class="device-identity">
                    <h2>{{ peer.alias || peer.name || peer.peer_id }}</h2>
                    <span class="note">{{ peer.peer_id }}</span>
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
                    <button type="button" class="icon-button" :title="t('解除互联网配对')" :aria-label="t('解除互联网配对')" @click="unpairInternet(peer.peer_id)"><Unlink :size="17" /></button>
                  </div>
                </li>
              </ul>
              <p v-if="internetPairingEnabled && !internetPairing.peers.length" class="note setting-note">{{ t("还没有互联网配对的设备。上面两个方向任选一个，两台设备就能隔着网络配对。") }}</p>
            </section>
          </template>

          <template v-else>
            <section class="settings-section">
              <h2>{{ t("手机 Companion") }}</h2>
              <div class="setting-actions setting-actions--card">
                <button type="button" :disabled="companionBusy" @click="refreshCompanion"><RefreshCw :size="16" />{{ t("读取手机服务状态") }}</button>
              </div>
              <!-- The phone service, where the phone is paired rather than where
                   the numbers about it are configured: the port it will listen
                   on, the token that lets a phone in, the address a phone opens,
                   and the QR code that carries it.  Reading the status is the
                   first button, because everything below it is what the service
                   last said. -->
              <template v-if="companion">
                <p class="setting-block setting-block--card">{{ companion.running ? t('正在运行') : companion.state === 'stopping' ? t('正在停止') : t('已停止') }}</p>
                <label class="setting">
                  <span class="setting-name">{{ t("手机服务端口") }}</span>
                  <span class="setting-control"><input v-model.number="companionPort" :aria-label="t('手机服务端口')" type="number" min="1" max="65535" step="1" :disabled="companionBusy" /></span>
                </label>
                <div class="setting-actions">
                  <button type="button" :disabled="companionBusy || !Number.isInteger(companionPort) || companionPort < 1 || companionPort > 65535" @click="configureCompanion(true)">{{ t("启动 / 应用端口") }}</button>
                  <button type="button" :disabled="companionBusy || (!companion.enabled && !companion.running)" @click="configureCompanion(false)">{{ t("停止服务") }}</button>
                  <button type="button" :disabled="companionBusy || !companion.running" @click="companionRotatePending = true"><RefreshCw :size="16" />{{ t("更换访问令牌") }}</button>
                  <button type="button" class="danger-outline" :disabled="companionBusy || !companion.running || !companion.access_url" @click="companionClearPending = true"><ShieldOff :size="16" />{{ t("清除访问令牌") }}</button>
                </div>
                <p v-if="companion.running && !companion.access_url" class="setting-block setting-block--card">{{ t("访问令牌已清除，任何能访问该端口的设备都可以直接连接") }}</p>
                <label v-if="companion.running && (companion.access_url || companion.url)" class="setting">
                  <span class="setting-name">{{ t("手机访问地址") }}</span>
                  <span class="setting-control"><input :value="companion.access_url || companion.url || ''" class="setting-wide" readonly :aria-label="t('手机访问地址')" /></span>
                </label>
                <div class="setting-actions">
                  <button type="button" :disabled="companionBusy || !companion.running" @click="openCompanionQr"><QrCode :size="16" />{{ t("显示二维码") }}</button>
                </div>
              </template>
            </section>
          </template>
        </section>
      </div>
      <div class="bottom-status"><ShieldCheck :size="14" />{{ t("本地数据") }}
        <span role="status">{{ state.pending ? t('正在处理') : (state.status?.runtime_error || syncLabel) }}</span>
        <!-- Status only, by the user's request: this bar used to carry the
             sync switch and the timed-pause presets too, and every one of
             them already sits on the overview page's 快速控制 card.  Two
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
        <!-- The notice stack is placed against this bar rather than against the
             window: the stylesheet measures it from the bar's top edge, because
             the bar is the only element that knows where its own top edge is.
             It is a child here and out of this row's flow — nothing makes room
             for it, and the bar is as tall as it was without it. -->
        <NoticeStack :store="store" />
      </div>
    </main>

    <dialog ref="sendUrlDialog" aria-labelledby="send-url-title" class="modal" @close="sendUrlDevice = null" @cancel="sendUrlDevice = null">
      <h2 id="send-url-title">{{ t("发送网址到 {name}", { name: sendUrlDevice?.name }) }}</h2>
      <p class="muted">{{ t("对方将在默认浏览器中打开该 http(s) 网址。") }}</p>
      <label v-if="sendUrlCandidates.length > 1">{{ t("目标设备") }}
        <select v-model="sendUrlDeviceId" :aria-label="t('目标设备')">
          <option v-for="candidate in sendUrlCandidates" :key="candidate.id" :value="candidate.id">{{ candidate.name }}</option>
        </select>
      </label>
      <label>{{ t("网址") }}<input v-model="sendUrlValue" type="url" maxlength="2048" :aria-label="t('要发送的网址')" placeholder="https://example.com" /></label>
      <div class="modal-actions"><button autofocus @click="sendUrlDevice = null">{{ t("取消") }}</button><button :disabled="sendUrlBusy || !sendUrlValue.trim()" @click="confirmSendUrl">{{ t("发送") }}</button></div>
    </dialog>
    <dialog ref="pushTextDialog" aria-labelledby="push-text-title" class="modal" @close="pushTextOpen = false" @cancel="pushTextOpen = false">
      <h2 id="push-text-title">{{ t("推送文本") }}</h2>
      <p class="muted">{{ t("文本会写入本机剪贴板；同步开启时会同时发送到所有设备。") }}</p>
      <label>{{ t("文本") }}<textarea v-model="pushTextValue" rows="5" maxlength="100000" :aria-label="t('要推送的文本')"></textarea></label>
      <div class="modal-actions"><button autofocus @click="pushTextOpen = false">{{ t("取消") }}</button><button :disabled="pushTextBusy || !pushTextValue.trim()" @click="confirmPushText">{{ t("推送") }}</button></div>
    </dialog>
    <dialog ref="translateItemDialog" aria-labelledby="translate-item-title" class="modal" @close="translateItemOpen = false" @cancel="translateItemOpen = false">
      <h2 id="translate-item-title">{{ t("翻译记录") }}</h2>
      <pre class="translate-source" tabindex="0" :aria-label="t('记录原文')">{{ translateItemText }}</pre>
      <p v-if="translateItemTruncated" class="note translate-truncated">{{ t("记录过长，只读取并翻译了前 {count} 个字符", { count: translateItemText.length }) }}</p>
      <label>{{ t("源语言") }}<select v-model="translationSource" :aria-label="t('翻译源语言')"><option value="auto">{{ t("自动检测") }}</option><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></label>
      <label>{{ t("目标语言") }}<select v-model="translationTarget" :aria-label="t('翻译目标语言')"><option v-for="[code, name] in translationLanguages" :key="code" :value="code">{{ name }}</option></select></label>
      <p v-if="translateItemResult" class="translation-result">{{ translateItemResult }}</p>
      <div class="modal-actions"><button autofocus @click="translateItemOpen = false">{{ t("关闭") }}</button><button :disabled="translateItemBusy" @click="runTranslateItem">{{ t("翻译") }}</button></div>
    </dialog>
    <dialog ref="logsDialog" aria-labelledby="logs-title" class="modal" @close="logsOpen = false" @cancel="logsOpen = false">
      <h2 id="logs-title">{{ t("应用日志") }}</h2>
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
      <div class="modal-actions">
        <button type="button" :disabled="logsExporting" @click="exportLogs">
          <Save :size="17" />{{ logsExporting ? t("正在导出…") : t("导出日志…") }}
        </button>
        <button autofocus @click="logsOpen = false">{{ t("关闭") }}</button>
      </div>
    </dialog>
    <dialog ref="qrDialog" aria-labelledby="qr-title" class="modal" @close="qrOpen = false" @cancel="qrOpen = false">
      <h2 id="qr-title">{{ t("网页二维码") }}</h2>
      <p class="muted">{{ t("用手机相机扫描，打开手机 Companion 页面。") }}</p>
      <p v-if="qrBusy" class="muted">{{ t("正在生成二维码…") }}</p>
      <img v-else-if="qrImage" :src="qrImage" :alt="t('手机 Companion 二维码')" class="qr-image" width="220" height="220" />
      <p v-else class="muted" role="status">{{ qrMessage || t("二维码不可用") }}</p>
      <p v-if="qrUrl" class="note selectable">{{ qrUrl }}</p>
      <p v-if="qrShareMessage" role="status" class="note">{{ qrShareMessage }}</p>
      <div class="modal-actions">
        <button type="button" :disabled="qrShareBusy" @click="shareFileToPhone">
          <SendHorizontal :size="17" />{{ qrShareBusy ? t("正在发送…") : t("发送文件到手机") }}
        </button>
        <button autofocus @click="qrOpen = false">{{ t("关闭") }}</button>
      </div>
    </dialog>
    <dialog ref="aboutDialog" aria-labelledby="about-title" class="modal" @close="aboutOpen = false" @cancel="aboutOpen = false">
      <h2 id="about-title">{{ t("关于 ClipSync") }}</h2>
      <p class="about-version">ClipSync {{ state.status ? state.status.version : "…" }}</p>
      <p class="muted">{{ t("跨平台剪贴板共享，支持 Windows、macOS 和 Linux。") }}</p>
      <p class="muted">{{ t("在设备之间实时共享剪贴板内容与文件。") }}</p>
      <p v-if="aboutMessage" role="status" class="note">{{ aboutMessage }}</p>
      <div class="modal-actions">
        <button type="button" :disabled="aboutBusy" @click="openAboutLink('homepage')"><Link :size="17" />{{ t("项目主页") }}</button>
        <button type="button" :disabled="aboutBusy" @click="openAboutLink('releases')"><Globe :size="17" />{{ t("最新版本") }}</button>
        <button autofocus @click="aboutOpen = false">{{ t("关闭") }}</button>
      </div>
    </dialog>
    <dialog ref="diagnosticsDialog" aria-labelledby="diagnostics-title" class="modal modal-wide" @close="diagnosticsOpen = false" @cancel="diagnosticsOpen = false">
      <h2 id="diagnostics-title">{{ t("系统诊断") }}</h2>
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
      <div class="modal-actions">
        <button autofocus :disabled="diagnosticsBusy" @click="rerunDiagnostics">{{ t("重新检测") }}</button>
        <button @click="diagnosticsOpen = false">{{ t("关闭") }}</button>
      </div>
    </dialog>
    <dialog ref="restartDialog" aria-labelledby="restart-title" class="modal" @close="restartOpen = false" @cancel="restartOpen = false">
      <h2 id="restart-title">{{ t("重启 ClipSync？") }}</h2>
      <p>{{ t("应用会退出并自动重新启动，同步会短暂中断。") }}</p>
      <div class="modal-actions"><button autofocus @click="restartOpen = false">{{ t("取消") }}</button><button class="danger" @click="confirmRestart">{{ t("重启") }}</button></div>
    </dialog>
    <dialog ref="clearPasswordDialog" aria-labelledby="clear-password-title" class="modal" @close="clearPasswordOpen = false" @cancel="clearPasswordOpen = false">
      <h2 id="clear-password-title">{{ t("清除加密密码？") }}</h2>
      <p>{{ t("清除后重启不再需要密码，加密流量改用设备身份派生密钥。") }}</p>
      <div class="modal-actions"><button autofocus :disabled="securityBusy" @click="clearPasswordOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="securityBusy" @click="clearPassword">{{ t("清除") }}</button></div>
    </dialog>
    <dialog ref="factoryResetDialog" aria-labelledby="factory-reset-title" class="modal" @close="factoryResetOpen = false" @cancel="factoryResetOpen = false">
      <h2 id="factory-reset-title">{{ t("恢复出厂设置？") }}</h2>
      <p>{{ t("全部历史、收藏、配对和设备身份都会被删除，应用随即重新启动。此操作无法撤销。") }}</p>
      <div class="modal-actions"><button autofocus :disabled="securityBusy" @click="factoryResetOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="securityBusy" @click="confirmFactoryReset">{{ securityBusy ? t('正在重置') : t('确认重置') }}</button></div>
    </dialog>
    <dialog ref="recoverDialog" aria-labelledby="recover-title" class="modal" @close="recoverOpen = false" @cancel="recoverOpen = false">
      <h2 id="recover-title">{{ t("修复数据目录？") }}</h2>
      <p>{{ t("剪贴板历史、设备身份或配置已损坏，应用无法启动。修复会把这些文件改名移到一旁（不会删除），并在原位置新建配置。") }}</p>
      <p>{{ t("移开的文件可以手动改回原名恢复。") }}</p>
      <div class="modal-actions"><button autofocus :disabled="state.pending" @click="recoverOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="state.pending" @click="confirmRecover">{{ state.pending ? t('正在修复') : t('确认修复') }}</button></div>
    </dialog>
    <dialog ref="aiMigrateDialog" aria-labelledby="ai-migrate-title" class="modal" @close="aiMigrateOpen = false" @cancel="aiMigrateOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="aiMigrateDialog?.close()"><X :size="18" /></button>
      <h2 id="ai-migrate-title">{{ t("AI 配置迁移向导") }}</h2>
      <p class="note">{{ t("把来源设备的配置与技能带到本机：先对比，再选冲突策略，最后一次拉取。") }}</p>
      <label class="setting">
        <span class="setting-name">{{ t("来源设备") }}</span>
        <span class="setting-control"><select v-model="aiPeerId" :aria-label="t('迁移来源设备')">
          <option value="">{{ t("选择已配对设备") }}</option>
          <option v-for="device in state.devices.filter(device => device.paired && !device.relay)" :key="device.id" :value="device.id">{{ device.name }}{{ aiPeerIsLegacy(device.id) ? t("（只能浏览）") : "" }}</option>
        </select></span>
      </label>
      <p v-if="!aiPeerId" class="note">{{ t("先选择一台已配对设备") }}</p>
      <template v-else>
        <div v-if="aiRemoteWaiting" class="note">{{ t("已请求更新，等待对方返回库存") }}</div>
        <p v-if="aiMigrateBlocked" class="note setting-note" role="status">{{ t("该设备版本过旧，只能浏览，不能作为迁移来源") }}</p>
        <label v-for="option in aiMigrateStrategies" :key="option.value" class="setting setting--check">
          <span class="setting-control"><input v-model="aiMigrateStrategy" type="radio" :value="option.value" :aria-label="option.label" /><span>{{ option.label }}</span></span>
        </label>
        <p class="note setting-note">{{ aiMigrateSummary }}</p>
        <div class="modal-actions">
          <button @click="aiMigrateDialog?.close()">{{ t("取消") }}</button>
          <button class="primary" :disabled="aiMigrateBlocked || !aiMigrateTargets.length" @click="startAiMigration">{{ t("开始迁移（{count}）", { count: aiMigrateTargets.length }) }}</button>
        </div>
      </template>
    </dialog>
    <dialog ref="aiPullDialog" aria-labelledby="ai-pull-title" class="modal" @close="aiPullPending = null" @cancel="aiPullPending = null">
      <h2 id="ai-pull-title">{{ aiPullPending?.mode === 'overwrite' ? t('覆盖本机配置？') : t('追加到本机配置？') }}</h2>
      <p v-if="aiPullPending && aiPullPending.targets.length === 1">{{ aiPullPending.targets[0].rel_path }}<span v-if="aiPullPending.targets[0].isDir" class="muted"> {{ t("（{count} 个文件）", { count: aiPullPending.targets[0].items.length }) }}</span></p>
      <template v-else-if="aiPullPending">
        <p>{{ t("将写入以下 {count} 个配置项：", { count: aiPullPending.targets.length }) }}</p>
        <ul class="ai-local-list"><li v-for="target in aiPullPending.targets" :key="target.key"><span>{{ target.tool }} / {{ target.rel_path }}</span><span v-if="target.isDir" class="muted"> {{ t("（{count} 个文件）", { count: target.items.length }) }}</span></li></ul>
      </template>
      <div class="modal-actions"><button autofocus @click="aiPullPending = null">{{ t("取消") }}</button><button class="danger" :disabled="!aiPullPending" @click="confirmAiPull">{{ t("确认拉取") }}</button></div>
    </dialog>
    <dialog ref="restoreDialog" aria-labelledby="restore-title" class="modal" @close="restorePath = null" @cancel="restoreBusy ? $event.preventDefault() : restorePath = null">
      <h2 id="restore-title">{{ t("确认恢复备份？") }}</h2>
      <p>{{ restorePath }}</p>
      <p>{{ t("此操作会修改当前数据。恢复过程中请勿退出应用。") }}</p>
      <div class="modal-actions"><button autofocus :disabled="restoreBusy" @click="restorePath = null">{{ t("取消") }}</button><button class="danger" :disabled="restoreBusy || !restorePath" @click="confirmRestoreBackup">{{ restoreBusy ? t('正在恢复') : t('确认恢复') }}</button></div>
    </dialog>
    <dialog ref="companionRotateDialog" aria-labelledby="companion-rotate-title" class="modal" @close="companionRotatePending = false" @cancel="companionRotatePending = false">
      <h2 id="companion-rotate-title">{{ t("更换手机访问令牌？") }}</h2>
      <p>{{ t("旧访问链接将失效，手机需要使用新地址重新连接。") }}</p>
      <div class="modal-actions"><button autofocus @click="companionRotatePending = false">{{ t("取消") }}</button><button class="danger" :disabled="companionBusy || !companionRotatePending || !companion?.running" @click="rotateCompanionToken">{{ t("更换令牌") }}</button></div>
    </dialog>
    <dialog ref="companionClearDialog" aria-labelledby="companion-clear-title" class="modal" @close="companionClearPending = false" @cancel="companionClearPending = false">
      <h2 id="companion-clear-title">{{ t("清除手机访问令牌？") }}</h2>
      <p>{{ t("清除后手机不再需要令牌，任何能访问该端口的设备都可以直接连接。") }}</p>
      <div class="modal-actions"><button autofocus @click="companionClearPending = false">{{ t("取消") }}</button><button class="danger" :disabled="companionBusy || !companionClearPending || !companion?.running" @click="clearCompanionToken">{{ t("清除令牌") }}</button></div>
    </dialog>
    <dialog ref="aiDiscardDialog" aria-labelledby="ai-discard-title" class="modal" @close="aiNextFile = null" @cancel="aiNextFile = null">
      <h2 id="ai-discard-title">{{ t("放弃未保存的修改？") }}</h2>
      <p>{{ aiSelectedItem?.rel_path }}</p>
      <div class="modal-actions"><button autofocus @click="aiNextFile = null">{{ t("继续编辑") }}</button><button class="danger" :disabled="!aiNextFile || aiMutationBusy" @click="discardAiChanges">{{ t("放弃修改并切换") }}</button></div>
    </dialog>
    <dialog ref="aiTrashDialog" aria-labelledby="ai-trash-title" class="modal" @close="aiTrashItem = null" @cancel="aiTrashItem = null">
      <h2 id="ai-trash-title">{{ t("移入回收区？") }}</h2>
      <p>{{ aiTrashItem?.rel_path }}</p>
      <div class="modal-actions"><button autofocus :disabled="aiMutationBusy" @click="aiTrashItem = null">{{ t("取消") }}</button><button class="danger" :disabled="aiMutationBusy || !aiTrashItem" @click="confirmAiTrash">{{ t("移入回收区") }}</button></div>
    </dialog>
    <dialog ref="deleteDialog" aria-labelledby="delete-title" class="modal" @close="deleteItem = null" @cancel="deleteItem = null">
        <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="deleteItem = null"><X :size="18" /></button>
        <h2 id="delete-title">{{ t("删除这条记录？") }}</h2><p class="muted">{{ t("记录将从本地历史中移除，此操作无法撤销。") }}</p>
        <div class="modal-actions"><button @click="deleteItem = null">{{ t("取消") }}</button><button class="danger" :disabled="state.pending" @click="confirmDelete">{{ t("删除") }}</button></div>
    </dialog>
    <dialog ref="batchDeleteDialog" aria-labelledby="batch-delete-title" class="modal" @close="batchDeleteIds = null" @cancel="batchDeleteIds = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="batchDeleteIds = null"><X :size="18" /></button>
      <h2 id="batch-delete-title">{{ t("删除所选的 {count} 条记录？", { count: batchDeleteIds?.length || 0 }) }}</h2>
      <p class="muted">{{ t("所选记录将从本地历史中移除，此操作无法撤销。") }}</p>
      <p v-if="state.error" class="modal-error small" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <p v-if="batchDeleteIds && !batchDeleteValid" role="status">{{ t("所选记录已变化，请取消并重新选择。") }}</p>
      <div class="modal-actions"><button autofocus @click="batchDeleteIds = null">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy || !batchDeleteValid" @click="confirmBatchDelete">{{ t("删除所选记录") }}</button></div>
    </dialog>
    <dialog ref="clearHistoryDialog" aria-labelledby="clear-history-title" class="modal" @close="clearHistoryOpen = false" @cancel="clearHistoryOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="clearHistoryOpen = false"><X :size="18" /></button>
      <h2 id="clear-history-title">{{ t("清空全部历史记录？") }}</h2>
      <p class="muted">{{ t("共 {count} 条记录将被移除，此操作无法撤销。", { count: state.total }) }}</p>
      <p v-if="state.error" class="modal-error small" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <div class="modal-actions"><button autofocus @click="clearHistoryOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy" @click="confirmClearHistory">{{ t("清空历史") }}</button></div>
    </dialog>
    <dialog ref="revokeDialog" aria-labelledby="revoke-title" class="modal" @close="revokeDevice = null" @cancel="revokeDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="revokeDevice = null"><X :size="18" /></button>
      <h2 id="revoke-title">{{ t("撤销设备信任？") }}</h2>
      <p class="muted">{{ t("{name} 将无法继续同步，重新连接需要双方再次配对。", { name: revokeDevice?.name }) }}</p>
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
      <p class="muted">{{ t("{name} 将不再通过中继与这台设备同步，恢复需要重新配对一次。", { name: relayUnpairDevice?.name }) }}</p>
      <div class="modal-actions"><button autofocus @click="relayUnpairDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy" @click="confirmRelayUnpair">{{ t("解除互联网配对") }}</button></div>
    </dialog>
    <dialog ref="forgetDialog" aria-labelledby="forget-title" class="modal" @close="forgetDevice = null" @cancel="forgetDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="forgetDevice = null"><X :size="18" /></button>
      <h2 id="forget-title">{{ t("移除这台设备？") }}</h2>
      <p class="muted">{{ t("{name} 将停止同步并从此列表消失，可在“已移除的设备”中恢复。", { name: forgetDevice?.name }) }}</p>
      <div class="modal-actions"><button autofocus @click="forgetDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === forgetDevice?.id && !device.archived)" @click="confirmForget">{{ t("移除设备") }}</button></div>
    </dialog>
    <dialog ref="purgeDialog" aria-labelledby="purge-title" class="modal" @close="purgeDevice = null" @cancel="purgeDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="purgeDevice = null"><X :size="18" /></button>
      <h2 id="purge-title">{{ t("彻底删除这台设备？") }}</h2>
      <p class="muted">{{ t("{name} 的移除记录将被永久删除，此操作无法撤销。", { name: purgeDevice?.name }) }}</p>
      <div class="modal-actions"><button autofocus @click="purgeDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === purgeDevice?.id && device.archived)" @click="confirmPurge">{{ t("彻底删除") }}</button></div>
    </dialog>
    <!-- The context menu 重命名 entry. The row has its own 设备备注 field
         writing this same alias, so the dialog is a second way in — not a
         second setting. -->
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
    <dialog ref="certAlertDialog" aria-labelledby="cert-alert-title" class="modal" @cancel.prevent @close="certPromptOpen = false">
      <h2 id="cert-alert-title">{{ t("设备身份变更") }}</h2>
      <p>{{ t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: certAlertName }) }}</p>
      <p class="note">{{ t("这是您信任的设备吗？") }}</p>
      <p v-if="!state.certAlert?.can_trust" class="note">{{ t("该设备需要再次连接后才能信任新证书。") }}</p>
      <p v-if="state.error" class="modal-error small" role="alert">{{ state.error.message }}</p>
      <!-- Gated on `pending`, not on `busy`: the store's action refuses while a
           request is in flight, and a snapshot refresh must never hold up the
           answer to a security prompt. -->
      <div class="modal-actions">
        <button :disabled="state.pending" @click="rejectCertAlert">{{ t("保持不配对") }}</button>
        <button autofocus :disabled="state.pending || !state.certAlert?.can_trust" @click="acceptCertAlert">{{ t("重新信任") }}</button>
      </div>
    </dialog>
    <dialog ref="certDialog" aria-labelledby="cert-title" class="modal" @close="certificates = null" @cancel="certificates = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="certDialog?.close()"><X :size="18" /></button>
      <h2 id="cert-title">{{ t("已固定证书指纹") }}</h2>
      <p class="note">{{ t("同步与聊天只接受指纹一致的设备，发现不一致请撤销信任后重新配对。") }}</p>
      <ul v-if="certificates?.length" class="cert-list">
        <li v-for="item in certificates" :key="item.device_id">
          <strong>{{ item.device_name || item.device_id }}</strong>
          <span class="note fingerprint">{{ item.fingerprint_short || t("未固定") }}</span>
          <span class="note">{{ item.paired ? t("已配对") : t("未配对") }}</span>
        </li>
      </ul>
      <p v-else class="muted">{{ t("暂无已固定的设备证书") }}</p>
      <div class="modal-actions"><button autofocus @click="certDialog?.close()">{{ t("关闭") }}</button></div>
    </dialog>
    <!-- First-run language picker. Deliberately bilingual: it must be readable
         before any language has been chosen, so its text is not translated
         (its catalog entries map to themselves). -->
    <dialog ref="languageDialog" aria-labelledby="language-title" class="modal language-picker" @close="languagePromptOpen = false" @cancel="languagePromptOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="languagePromptOpen = false"><X :size="18" /></button>
      <h2 id="language-title">ClipSync</h2>
      <p class="language-picker-lead">{{ t("选择语言 · Choose Language") }}</p>
      <p class="note">{{ t("首次使用 ClipSync，请选择界面语言") }}<br />Please choose your interface language</p>
      <div class="language-choices">
        <button v-for="choice in languageChoices" :key="choice.code" type="button" class="language-choice" @click="chooseLanguage(choice.code)">
          <strong>{{ choice.native }}</strong>
          <span class="note">{{ choice.other }}</span>
        </button>
      </div>
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
