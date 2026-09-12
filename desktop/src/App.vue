<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  History, Monitor, Search, RefreshCw, Pin, Trash2, ChevronLeft, ChevronRight, Eraser, FileDown,
  ShieldCheck, LockKeyhole, AlertCircle, X, LogOut, Circle, Copy, Check, Link, Unlink, PinOff, Star, Settings as SettingsIcon, Save, FileUp, FolderOpen, MessageCircle, Plug, PlugZap, RotateCcw, Activity, Fingerprint, Globe, SendHorizontal, Stethoscope, Wrench, Info, QrCode, ExternalLink, Wand2, Sparkles, Clock, Smartphone,
} from "@lucide/vue";
import logo from "../../assets/icon.svg";
import { bridge, inDesktop } from "./api/bridge";
import type { Device, DeviceCertificate, DeviceProbeResult, DiagnosticAction, DiagnosticCheck, DiagnosticItem, DiagnosticsReport, HistoryItem } from "./api/types";
import { LOCALES, LOCALE_NAMES, currentLocale, setLocale, t } from "./i18n";
import { createApplicationStore } from "./stores/application";
import { aiCompareState, aiEntryKey, aiDiffCounts, buildAiLocalIndex } from "./lib/aiconfig-diff";
import { aiItemCount, aiTreeGroups, type AiGroup, type AiNode, type AiRow } from "./lib/aiconfig-tree";
import { aiTargets, type AiTarget } from "./lib/aiconfig-targets";
import { formatPairingCode, isPairingCodeComplete } from "./lib/pairing-code";
import { deliveryIcon, deliveryLabel } from "./stores/delivery";
import FavoritesView from "./components/FavoritesView.vue";
import TransfersView from "./components/TransfersView.vue";
import ChatView from "./components/ChatView.vue";
import NoticeStack from "./components/NoticeStack.vue";

const store = createApplicationStore();
const { state } = store;
/** Which page is on screen.  Named by the same list the sidebar draws from, so
 * a page cannot exist in one and not the other. */
const tab = ref<(typeof PAGES)[number]>("history");
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
    { id: "notifications", label: t("通知") },
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
 */
watch(tab, async (page) => {
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
async function refreshCompanion() {
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
async function configureCompanion(enabled: boolean, rotateToken = false) {
  if (companionBusy.value) return;
  const generation = ++companionGeneration;
  companionBusy.value = true;
  try {
    const port = enabled && !rotateToken ? companionPort.value : companion.value?.port ?? companionPort.value;
    const result = await bridge.configureCompanion(enabled, port, rotateToken);
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
  if (result?.opened) announce(t("已在浏览器打开：{url}", { url: result.url }));
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
  if (kind === "IMAGE" || kind === "IMAGE_PNG" || kind === "IMAGE_EMF") return t("图片");
  return kind || t("内容");
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
 * - text only. The panel's card repeated the kind, the time, the source and the
 *   pin state, all of which the native row already shows beside the words, so
 *   repeating them here would be the noise rather than the preview.
 *
 * `aria-hidden` on the card, because the clamp is purely visual: the row's own
 * paragraph carries the whole preview in the DOM, so assistive tech already
 * reads all of it. This card is for the eyes that could not.
 */
const previewCard = ref<{ id: string; text: string } | null>(null);
function showPreview(item: HistoryItem) {
  if (item.preview) previewCard.value = { id: item.id, text: item.preview };
}
function hidePreview(item: HistoryItem) {
  if (previewCard.value?.id === item.id) previewCard.value = null;
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
  if (!items.length) return;
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
const aiPullProgress = ref<{ peerId: string; total: number; done: number; failed: number } | null>(null);
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
 * not — and only this separates the two. */
const relayState = computed(() => String(internetPairing.value.relay || ""));
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
const statusMessage = ref("");
let statusMessageTimer: ReturnType<typeof setTimeout> | undefined;
const revokeDevice = ref<Device | null>(null);
const revokeDialog = ref<HTMLDialogElement | null>(null);
const forgetDevice = ref<Device | null>(null);
const forgetDialog = ref<HTMLDialogElement | null>(null);
const purgeDevice = ref<Device | null>(null);
const purgeDialog = ref<HTMLDialogElement | null>(null);
const probeResults = ref<Record<string, DeviceProbeResult>>({});
const probeBusyId = ref("");
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
const favoritesAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("favorites.list"));
const syncLabel = computed(() => state.status?.sync_state === "running" ? t("同步运行中")
  : state.status?.sync_state === "paused" ? t("同步已暂停") : t("同步引擎未启动"));
const syncAvailable = computed(() => ready.value && !!state.status?.capabilities?.includes("sync.set_enabled"));
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
const visibleIds = computed(() => [...new Set(state.history.map((item) => item.id).filter((id) => id.trim()))]);
const allSelected = computed(() => !!visibleIds.value.length &&
  visibleIds.value.every((id) => state.selectedIds.includes(id)));
const batchDeleteValid = computed(() => !!batchDeleteIds.value?.length &&
  batchDeleteIds.value.every((id) => state.selectedIds.includes(id)));
async function confirmBatchDelete() {
  if (batchDeleteIds.value && await store.batchDelete(batchDeleteIds.value)) batchDeleteIds.value = null;
}
watch(batchDeleteIds, async (ids) => {
  await nextTick();
  if (ids) batchDeleteDialog.value?.showModal();
  else batchDeleteDialog.value?.close();
});
watch(() => [state.query, state.offset], () => { batchDeleteIds.value = null; }, { flush: "sync" });
function announce(message: string) {
  statusMessage.value = message;
  clearTimeout(statusMessageTimer);
  statusMessageTimer = setTimeout(() => { statusMessage.value = ""; }, 5000);
}
async function favoriteSelected() {
  const count = await store.batchFavorite([...state.selectedIds]);
  if (count) announce(t("已加入收藏夹 {count} 条", { count }));
}
async function confirmClearHistory() {
  const cleared = await store.clearHistory();
  if (cleared) {
    clearHistoryOpen.value = false;
    announce(t("已清空 {count} 条历史记录", { count: cleared }));
  }
}
watch(clearHistoryOpen, async (open) => {
  await nextTick();
  if (open) clearHistoryDialog.value?.showModal();
  else clearHistoryDialog.value?.close();
});
onUnmounted(() => clearTimeout(statusMessageTimer));
  function toggleSync(event: Event) {
  const input = event.target as HTMLInputElement;
  const enabled = input.checked;
  input.checked = state.status?.sync_state === "running";
  void store.setSyncEnabled(enabled);
}
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
    announce(t("同步已暂停 {minutes} 分钟", { minutes }));
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
    announce(t("同步已恢复"));
  }
  catch (reason: any) { state.error = reason?.message || t("恢复同步失败"); }
  finally { pauseBusy.value = false; }
}
function pairingPending(device: Device) {
  return !device.paired && ["pending", "peer_confirmed", "confirmed_waiting"].includes(device.pairing_status);
}
function pairingLabel(device: Device) {
  if (device.paired) return t("已配对");
  return ({ pending: t("等待确认"), peer_confirmed: t("对方已确认"), confirmed_waiting: t("等待对方确认"),
    cancelled: t("已取消") } as Record<string, string>)[device.pairing_status] || t("未配对");
}
function connectionLabel(value: string) {
  return ({ discovered: t("已发现"), connecting: t("连接中"), online: t("在线"), offline: t("离线") } as Record<string, string>)[value] || t("连接状态未知");
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
/** Which of the three internet-pairing states this device is in, and the words
 * for it.  `unpaired` is a state of its own rather than a missing one: a device
 * this machine only knows on the local network is not "offline over the
 * internet", it has no internet pairing at all, and those two read the same
 * unless the row says which. */
function relayChannel(device: Device): "online" | "offline" | "unpaired" {
  const peer = relayPeerFor(device);
  if (!peer) return "unpaired";
  return peer.online ? "online" : "offline";
}
function relayChannelLabel(device: Device) {
  const channel = relayChannel(device);
  if (channel === "online") return t("在线");
  if (channel === "offline") return t("离线");
  return t("未配对");
}
/** When this device was last heard from over the relay, as a line for the row's
 * tooltip: the fact a bare 离线 leaves out, and the one that separates "away
 * for a minute" from "gone since Tuesday". */
function relayLastSeen(device: Device) {
  const seen = Number(relayPeerFor(device)?.last_seen || 0);
  if (!seen) return "";
  return t("最后在线 {when}", { when: date(seen) });
}
async function saveDeviceNote(device: Device, event: Event) {
  const note = (event.target as HTMLInputElement).value;
  try { await bridge.setDeviceNote(device.id, note); await store.refresh(); }
  catch (reason: any) { state.error = reason?.message || t("保存设备备注失败"); }
}
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
      announce(t("已发送网址到 {name}", { name: device.name }));
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
      announce(result.sent
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
    logCount.value = lines;
  } catch (error) { state.error = error as any; }
  finally { logsBusy.value = false; }
}
async function openLogs() {
  logsOpen.value = true;
  logExportMessage.value = "";
  await loadLogs();
}
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
    announce(kind === "firewall" ? t("已请求修复防火墙规则") : t("已打开本地网络权限设置"));
    // The elevated rule may land a moment later, so re-scan after a pause.
    if (kind === "firewall") setTimeout(() => { if (diagnosticsOpen.value) void refreshDiagnostics(); }, 2500);
  } finally { diagnosticsRepairBusy.value = false; }
}
async function checkForUpdate() {
  if (updateChecking.value || updateState.value.phase === "downloading") return;
  updateChecking.value = true;
  try {
    const result = await store.checkUpdate();
    // "unknown" and "up to date" must not look alike: only a real answer with
    // no newer release claims the app is current.
    if (!result) announce(t("无法连接更新服务器"));
    else if (!result.available) announce(result.latest ? t("已是最新版本") : t("无法连接更新服务器"));
  } finally { updateChecking.value = false; }
}
async function startUpdateDownload() {
  // Returns immediately; the panel renders progress from `update.state` events.
  const result = await store.downloadUpdate();
  if (!result || !result.ok) {
    announce(t("更新下载失败") + (result?.error ? `：${result.error}` : ""));
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

function date(value: number) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(value * 1000);
}
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
/** The sidebar, top to bottom: what Ctrl+1…7 counts.  The numbers belong to the
 * rows the user can see, which is why this list has to stay in the order the
 * template renders them and not in whatever order would read better here. */
const PAGES = ["history", "devices", "favorites", "transfers", "chat", "ai", "settings"] as const;
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
  history: () => t("剪贴板历史"),
  devices: () => t("设备"),
  favorites: () => t("收藏库"),
  transfers: () => t("文件传输"),
  chat: () => t("附近聊天"),
  ai: () => t("AI 配置"),
  settings: () => t("设置"),
};
const PAGE_SUMMARIES: Record<(typeof PAGES)[number], () => string> = {
  history: () => t("{count} 条记录", { count: state.total }),
  devices: () => t("{count} 台设备", { count: state.devices.length }),
  favorites: () => t("{count} 条收藏", { count: store.favorites.state.total }),
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
/** The window's own chords.  Neither shell had an in-window shortcut map — the
 * hotkey module is OS-wide and excluded by the user's scope exception — so this
 * is the desktop convention rather than a ported behaviour: the page numbers,
 * the search boxes and preferences.  Esc is deliberately absent: a native
 * `<dialog>` already owns it, and it works even where this would not. */
function onShortcut(event: KeyboardEvent) {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
  // An open dialog owns the keyboard, so nothing behind it moves.
  if (document.querySelector("dialog[open]")) return;
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
onMounted(() => {
  store.start();
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
      notifications_enabled: settings.value.notifications_enabled,
      notify_transfer: settings.value.notify_transfer,
      // The host gates its own pairing and device-connect notifications on
      // these two, so they have to be settable here or a user could lose the
      // choice (legacy's panel set them; the phone panel still can).
      notify_pairing: settings.value.notify_pairing,
      notify_device_connect: settings.value.notify_device_connect,
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
      relay_brokers: String(settings.value.relay_brokers || "").split(/\r?\n|,/).map((v) => v.trim()).filter(Boolean),
      relay_private_brokers: String(settings.value.relay_private_brokers || "").split(/\r?\n|,/).map((v) => v.trim()).filter(Boolean),
      relay_username: settings.value.relay_username,
      relay_password: settings.value.relay_password,
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
async function pullAiRemote(items: Array<Record<string, any>>, mode = "copy") {
  const peerId = aiPeerId.value;
  if (!items.length) return;
  if (!state.devices.some(device => device.id === peerId && device.paired)) return;
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
      ? { peerId, total: requested + errors.length, done: 0, failed: errors.length }
      : null;
    aiRemoteMessage.value = t("已发送 {count} 个拉取请求，等待文件接收", { count: requested }) +
      (errors.length ? t("；部分请求失败：{errors}", { errors: errors.join(", ") }) : "");
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
    aiRemoteMessage.value = t("拉取完成：{ok} 个成功、{failed} 个失败", { ok: pull.done, failed: pull.failed });
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
  if (data.status === "error") pull.failed += 1;
  else pull.done += 1;
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
/** The send list's own refresh button: re-read the peers, then their ledgers. */
async function refreshDeliveryStatus() {
  await refreshInternetPairing();
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
    await refreshInternetPairing();
    await refreshDeliveryStatus();
  } catch (error) {
    internetPairingFailed.value = true;
    internetPairingMessage.value = pairingErrorText(error);
  }
}
async function unpairInternet(peerId: string) {
  try {
    await bridge.unpairInternetPeer(peerId);
    internetPairingMessage.value = "";
    internetPairingFailed.value = false;
    await refreshInternetPairing();
  }
  catch (error) {
    internetPairingFailed.value = true;
    internetPairingMessage.value = (error as any)?.message || t("解除互联网配对失败");
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
async function renameInternet(peer: any) {
  const current = peer.alias || peer.name || "";
  const name = window.prompt(t("设备别名"), current);
  if (name === null) return;
  try { await bridge.renameInternetPeer(peer.peer_id, name); await refreshInternetPairing(); }
  catch (error) { state.error = error as any; }
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
        <button :aria-label="t('剪贴板历史')" :title="pageTitle(t('剪贴板历史'), 0)" :aria-keyshortcuts="pageChord(0)" :class="{ active: tab === 'history' }" @click="tab = 'history'">
          <History :size="18" /><span>{{ t("剪贴板历史") }}</span>
        </button>
        <button :aria-label="t('设备')" :title="pageTitle(t('设备'), 1)" :aria-keyshortcuts="pageChord(1)" :class="{ active: tab === 'devices' }" @click="tab = 'devices'">
          <Monitor :size="18" /><span>{{ t("设备") }}</span>
        </button>
        <button :aria-label="t('收藏库')" :title="pageTitle(t('收藏库'), 2)" :aria-keyshortcuts="pageChord(2)" :class="{ active: tab === 'favorites' }" @click="tab = 'favorites'">
          <Star :size="18" /><span>{{ t("收藏库") }}</span>
        </button>
        <button :aria-label="t('文件传输')" :title="pageTitle(t('文件传输'), 3)" :aria-keyshortcuts="pageChord(3)" :class="{ active: tab === 'transfers' }" @click="tab = 'transfers'">
          <FileUp :size="18" /><span>{{ t("文件传输") }}</span>
        </button>
        <button :aria-label="t('附近聊天')" :title="pageTitle(t('附近聊天'), 4)" :aria-keyshortcuts="pageChord(4)" :class="{ active: tab === 'chat' }" @click="tab = 'chat'">
          <MessageCircle :size="18" /><span>{{ t("附近聊天") }}</span>
        </button>
        <!-- AI configuration is a page of its own rather than a card in the
             settings page: it is not a preference this machine holds but a
             workbench over files — an inventory that is walked, opened, edited,
             moved to the trash, and pulled from a paired device — and it was
             the longest card in a window whose cards are meant to be one
             question each.  It sits with the pages that hold content rather
             than behind the rule with the window's own configuration. -->
        <button :aria-label="t('AI 配置')" :title="pageTitle(t('AI 配置'), 5)" :aria-keyshortcuts="pageChord(5)" :class="{ active: tab === 'ai' }" @click="openAiPage">
          <Sparkles :size="18" /><span>{{ t("AI 配置") }}</span>
        </button>
        <!-- Settings is not another page of content: it is the window's own
             configuration, so it sits below the six pages that hold content,
             behind a rule, the way a mature desktop sidebar carries it. -->
        <div class="sidebar-settings">
          <button :aria-label="t('设置')" :title="pageTitle(t('设置'), 6)" :aria-keyshortcuts="pageChord(6)" :class="{ active: tab === 'settings' }" @click="openSettings">
            <SettingsIcon :size="18" /><span>{{ t("设置") }}</span>
          </button>
        </div>
      </nav>
      <div class="sidebar-footer">
        <div class="local-device"><Monitor :size="17" /><span>{{ state.status?.device_name || t('此设备') }}</span></div>
        <!-- What this window is running, in the same words the About dialog uses.
             The framework it is built with is not the reader's business, and
             "Tauri Desktop · 1.0.0" read as a build stamp rather than a product. -->
        <span v-if="state.status" class="muted small">{{ t("版本 {version}", { version: state.status.version }) }}</span>
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

        <template v-else-if="tab === 'history'">
          <div class="toolbar">
            <label class="search"><Search :size="17" /><input
              ref="searchInput"
              :value="state.query" :aria-label="t('搜索历史记录')" :placeholder="t('搜索历史记录')"
              :disabled="busy" @input="store.search(($event.target as HTMLInputElement).value)"
            /></label>
            <button class="icon-button" :title="t('刷新')" :aria-label="t('刷新')" :disabled="historyBusy" @click="store.refreshHistory"><RefreshCw :size="18" :class="{ spinning: state.loading }" /></button>
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
            <div class="list-heading selection-toolbar">
              <label class="select-visible"><input type="checkbox" :aria-label="t('全选当前页')"
                :checked="allSelected" :indeterminate="state.selectedIds.length > 0 && !allSelected"
                :disabled="historyBusy || !visibleIds.length || visibleIds.length > 100"
                @change="store.selectAllVisible(($event.target as HTMLInputElement).checked)" />{{ t("全选当前页") }}</label>
              <span role="status">{{ t("已选择 {count} 条", { count: state.selectedIds.length }) }}</span>
              <div class="batch-actions">
                <button class="icon-button" :aria-label="t('批量收藏')" :title="t('批量收藏')" :disabled="historyBusy || !state.selectedIds.length" @click="store.batchPin(true)"><Pin :size="17" /></button>
                <button class="icon-button" :aria-label="t('批量取消收藏')" :title="t('批量取消收藏')" :disabled="historyBusy || !state.selectedIds.length" @click="store.batchPin(false)"><PinOff :size="17" /></button>
                <button class="icon-button" :aria-label="t('加入收藏夹')" :title="t('加入收藏夹')" :disabled="historyBusy || !state.selectedIds.length" @click="favoriteSelected"><Star :size="17" /></button>
                <button class="icon-button" :aria-label="t('批量删除')" :title="t('批量删除')" :disabled="historyBusy || !state.selectedIds.length" @click="batchDeleteIds = [...state.selectedIds]"><Trash2 :size="17" /></button>
              </div>
            </div>
            <div v-if="state.loading && !state.history.length" class="empty" role="status"><RefreshCw class="spinning" :size="26" /><h2>{{ t("正在读取历史") }}</h2></div>
            <div v-else-if="!state.history.length" class="empty">
              <History :size="36" />
              <!-- A search and a chip are different answers: when a chip is on,
                   the panel named the kind instead of saying there is no history
                   at all, because there is — just none of that kind. -->
              <h2 v-if="state.query">{{ t("没有匹配的记录") }}</h2>
              <h2 v-else-if="state.kind !== 'all'">{{ t("还没有{type}", { type: kindLabel(state.kind) }) }}</h2>
              <h2 v-else>{{ t("暂无历史记录") }}</h2>
              <p class="muted">{{ state.kind !== 'all' ? t("换个过滤条件，或清除过滤查看全部内容。") : (native ? t('当前数据目录中没有可显示的记录') : t('桌面窗口连接后显示本地记录')) }}</p>
            </div>
            <article v-for="item in state.history" :key="item.id" class="history-row"
              @mouseenter="showPreview(item)" @mouseleave="hidePreview(item)"
              @focusin="showPreview(item)" @focusout="hidePreview(item)">
              <label class="history-selection"><input type="checkbox" :aria-label="t('选择记录 {id}', { id: item.id })"
                :checked="state.selectedIds.includes(item.id)"
                :disabled="historyBusy || !item.id.trim() || (!state.selectedIds.includes(item.id) && state.selectedIds.length >= 100)"
                @change="store.select(item.id, ($event.target as HTMLInputElement).checked)" /></label>
              <div class="history-content"><p>{{ item.preview || `[${item.content_type || t('内容')}]` }}</p>
                <span class="muted small">{{ typeLabel(item) }}<span v-if="item.pinned"> · {{ t("已收藏") }}</span></span>
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
              <time :datetime="new Date(item.timestamp * 1000).toISOString()">{{ date(item.timestamp) }}</time>
              <div class="row-actions">
                <button class="icon-button" :aria-label="t('复制记录')" :title="t('复制记录')" :disabled="historyBusy" @click="store.copy(item)"><Check v-if="state.copiedId === item.id" :size="17" /><Copy v-else :size="17" /></button>
                <button class="icon-button" :class="{ pinned: item.pinned }" :aria-label="item.pinned ? t('取消收藏') : t('收藏')" :title="item.pinned ? t('取消收藏') : t('收藏')" :disabled="historyBusy" @click="store.pin(item)"><Pin :size="17" /></button>
                <button class="icon-button" :aria-label="t('翻译记录')" :title="t('翻译记录')" :disabled="historyBusy || translateItemReading" @click="openTranslateItem(item)"><Globe :size="17" /></button>
                <button v-if="isWebLink(item)" class="icon-button" :aria-label="t('在浏览器打开')" :title="t('在浏览器打开')" :disabled="historyBusy" @click="openHistoryLink(item)"><ExternalLink :size="17" /></button>
                <button class="icon-button" :aria-label="t('删除记录')" :title="t('删除记录')" :disabled="historyBusy" @click="deleteItem = item"><Trash2 :size="17" /></button>
              </div>
              <!-- Non-interactive, so it never blocks the row's own controls or
                   the row below it — the panel's card was `pointer-events: none`
                   for the same reason. -->
              <div v-if="previewCard?.id === item.id" class="history-preview" aria-hidden="true">{{ previewCard.text }}</div>
            </article>
          </section>
          <footer class="pagination">
            <span>{{ state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)}` : '0' }} / {{ state.total }}</span>
            <button class="icon-button" :aria-label="t('上一页')" :title="t('上一页')" :disabled="state.offset === 0 || historyBusy" @click="store.page(-1)"><ChevronLeft :size="18" /></button>
            <button class="icon-button" :aria-label="t('下一页')" :title="t('下一页')" :disabled="state.offset + state.limit >= state.total || historyBusy" @click="store.page(1)"><ChevronRight :size="18" /></button>
          </footer>
        </template>

        <FavoritesView v-else-if="tab === 'favorites'" :store="store.favorites" :enabled="favoritesAvailable" />
        <TransfersView
          v-else-if="tab === 'transfers'"
          :devices="state.devices"
          :dropped="droppedPaths"
          @dropped="droppedPaths = []"
        />
        <!-- The receipt map comes from the store rather than a chat-local
             fetch: it is fed by the event stream, so a message that was sent
             before this tab was ever opened still shows how it ended. -->
        <ChatView v-else-if="tab === 'chat'" :receipts="delivery.state.messages" />
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
                <p v-if="!aiProfilesLoaded" class="muted small setting-block">{{ t("正在读取本机可用的 AI 工具…") }}</p>
                <p v-else-if="!aiTools.length" class="muted small setting-block">{{ t("本机没有可同步的 AI 工具。") }}</p>
                <label class="setting">
                  <span class="setting-name">{{ t("自定义配置路径") }}</span>
                  <span class="setting-control"><textarea v-model="aiCustomPaths" rows="3" :placeholder="t('每行一个路径')"></textarea></span>
                </label>
                <p class="muted small setting-note">{{ t("勾选要同步的 AI 工具；工具不认识的目录写在下面，每行一个路径。") }}</p>
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
                <p v-if="aiMessage" class="muted setting-block">{{ aiMessage }}</p>
                <p v-if="aiLocalItems.length" class="setting-block"><input v-model="aiLocalQuery" type="search" class="ai-filter" :aria-label="t('搜索本机配置')" :placeholder="t('筛选路径…')" /></p>
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
                      <button v-if="row.entry && !row.isDir" type="button" class="icon-button" :disabled="aiMutationBusy" :aria-label="t('编辑 {path}', { path: aiRowPath(row) })" :title="t('编辑')" @click="requestAiRead(aiRowItem(row))"><FileUp :size="15" /></button>
                      <button type="button" class="icon-button" :aria-label="t('打开 {path}', { path: aiRowPath(row) })" :title="t('打开')" @click="openAiLocal(aiRowItem(row))"><LogOut :size="15" /></button>
                      <button v-if="row.entry" type="button" class="icon-button" :disabled="aiMutationBusy" :aria-label="t('移入回收区 {path}', { path: aiRowPath(row) })" :title="t('移入回收区')" @click="aiTrashItem = aiRowItem(row)"><Trash2 :size="15" /></button>
                    </li>
                  </template>
                </ul>
                <!-- Says so rather than showing an empty list: a filter that matched
                     nothing and an inventory that is empty look identical otherwise. -->
                <p v-else-if="aiLocalItems.length" class="muted small setting-block">{{ t("没有匹配的配置项") }}</p>
                <nav v-if="aiLocalPageCount > 1" :aria-label="t('本地配置分页')" class="actions">
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
                    <option v-for="device in state.devices.filter(device => device.paired)" :key="device.id" :value="device.id">{{ device.name }}{{ aiPeerDiffSuffix(device.id) }}</option>
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
                  <p v-if="aiRemoteItems.length && aiLocalIndex" class="muted small setting-block" :class="{ 'ai-diff-ok': aiDiff.total === 0 }">{{ aiDiff.total === 0 ? t("与对方一致") : t("与对方不同：缺失 {missing}、对方较新 {remote}、本机较新 {local}", { missing: aiDiff.missing, remote: aiDiff.remote_newer, local: aiDiff.local_newer }) }}</p>
                  <p v-else-if="aiRemoteItems.length" class="muted small setting-block">{{ t("尚未读取本机配置，暂时无法对比版本差异") }}</p>
                  <p v-if="aiRemoteItems.length" class="setting-block"><input v-model="aiRemoteQuery" type="search" class="ai-filter" :aria-label="t('搜索远程配置')" :placeholder="t('筛选路径…')" /></p>
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
                        <button v-if="row.entry && !row.isDir" type="button" class="icon-button" :title="t('预览')" :aria-label="t('预览 {path}', { path: aiRowPath(row) })" @click="previewAiRemote(aiRowItem(row))"><Search :size="15" /></button>
                        <button v-if="row.entry && !row.isDir" type="button" class="icon-button" :title="t('拉取')" :aria-label="t('拉取 {path}', { path: aiRowPath(row) })" @click="requestAiPull(aiRowItem(row))"><FileUp :size="15" /></button>
                      </li>
                    </template>
                  </ul>
                  <p v-else-if="aiRemoteItems.length" class="muted small setting-block">{{ t("没有匹配的配置项") }}</p>
                  <!-- An empty list is two different states, and the reader needs
                       to be able to tell them apart: a peer that has never
                       answered, and a peer that answered with nothing. -->
                  <p v-else-if="aiRemoteRead" class="muted small setting-block">{{ t("该设备还没有可同步的配置项") }}</p>
                  <p v-else class="muted small setting-block">{{ t("尚未读取该设备的配置") }}</p>
                  <div v-if="aiRemoteItems.length" class="setting-actions">
                    <label class="sync-toggle">
                      <input type="checkbox" :aria-label="t('选择本页全部远程配置')" :checked="aiRemotePageAllSelected" :indeterminate="aiRemotePageSomeSelected" @change="toggleAiRemotePage()" />
                      <span>{{ t("选择本页") }}</span>
                    </label>
                    <button type="button" :disabled="!aiDiff.missing" @click="selectAiRemoteMissing()">{{ t("选择缺失项（{count}）", { count: aiDiff.missing }) }}</button>
                    <button type="button" :disabled="!aiRemoteSelectedTargets.length" @click="requestAiPullBatch()"><FileUp :size="16" />{{ t("拉取选中项（{count}）", { count: aiRemoteSelectedTargets.length }) }}</button>
                    <button type="button" :disabled="!aiRemoteSelected.length" @click="aiRemoteSelected = []">{{ t("清除选择") }}</button>
                  </div>
                  <nav v-if="aiRemotePageCount > 1" :aria-label="t('远程配置分页')" class="actions">
                    <button type="button" class="icon-button" :aria-label="t('上一页远程配置')" :title="t('上一页')" :disabled="aiRemotePage === 0" @click="aiRemotePage--"><ChevronLeft :size="16" /></button>
                    <span>{{ aiRemotePage + 1 }} / {{ aiRemotePageCount }}</span>
                    <button type="button" class="icon-button" :aria-label="t('下一页远程配置')" :title="t('下一页')" :disabled="aiRemotePage + 1 >= aiRemotePageCount" @click="aiRemotePage++"><ChevronRight :size="16" /></button>
                  </nav>
                </div>
                <p v-else class="muted small setting-block">{{ t("选择一台已配对设备后，可以对比并拉取它的 AI 工具配置。") }}</p>
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
                      <span class="setting-hint">{{ t("兜底通道：私有中继不可达时启用，并作为镜像让落在不同中继的设备仍能互通。公共中继接受任何客户端，也绝不会拿到你的中继密码。") }}</span>
                    </span>
                  </label>
                  <label class="setting">
                    <span class="setting-name">{{ t("私有中继地址") }}</span>
                    <span class="setting-control">
                      <textarea v-model="settings.relay_private_brokers" rows="3" :aria-label="t('私有中继地址')" :placeholder="t('每行一个地址')"></textarea>
                      <span class="setting-hint">{{ t("主通道：剪贴板数据优先走这些端点，用下方用户名和密码登录。留空则只使用上面的公共中继。") }}</span>
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
                  <span class="setting-control"><input v-model.number="settings.web_history_limit" :aria-label="t('显示历史条数')" type="number" min="1" max="500" step="1" /><span class="setting-hint">{{ t("网页上显示最近多少条剪贴板记录（1–500）") }}</span></span>
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
              <section v-show="showSettingsCard('notifications')" id="settings-notifications" class="settings-section">
                <h2>{{ t("通知") }}</h2>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.notifications_enabled" type="checkbox" /><span>{{ t("启用通知") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.notify_transfer" type="checkbox" :disabled="!settings.notifications_enabled" /><span>{{ t("文件传输通知") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.notify_pairing" type="checkbox" :disabled="!settings.notifications_enabled" /><span>{{ t("配对请求通知") }}</span></span>
                </label>
                <label class="setting setting--check">
                  <span class="setting-control"><input v-model="settings.notify_device_connect" type="checkbox" :disabled="!settings.notifications_enabled" /><span>{{ t("设备连接通知") }}</span></span>
                </label>
              </section>
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
                <p v-if="!discoveryAvailable" class="muted small setting-note">{{ t("同步引擎未运行，无法修改发现设置。") }}</p>
              </section>
              <section v-show="showSettingsCard('advanced')" id="settings-advanced" class="settings-section">
                <h2>{{ t("网络与高级") }}</h2>
                <!-- The legacy window put this remark under its own title rather
                     than under the last row of the page: it is about every field
                     here, and it read as a footnote to whichever row happened to
                     be above it. -->
                <p class="muted small setting-note">{{ t("部分更改将在重启后生效。") }}</p>
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
                    <span class="setting-control"><input v-model.number="settings.sync_debounce" :aria-label="t('同步去抖（秒）')" type="number" min="0.05" max="10" step="0.05" /><span class="setting-hint">{{ t("两次发送同步之间的最小间隔（0.05–10 秒）") }}</span></span>
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
                    <span class="setting-control"><input v-model.number="settings.transfer_timeout" :aria-label="t('传输超时（秒）')" type="number" min="5" max="3600" step="1" /><span class="setting-hint">{{ t("文件传输超过此时间视为失效（5–3600 秒）") }}</span></span>
                  </label>
                </fieldset>
                <fieldset>
                  <legend>{{ t("连接") }}</legend>
                  <label class="setting">
                    <span class="setting-name">{{ t("TCP 端口") }}</span>
                    <span class="setting-control"><input v-model.number="settings.port" :aria-label="t('TCP 端口')" type="number" min="1024" max="65535" step="1" /><span class="setting-hint">{{ t("（1024–65535，需重启）") }}</span></span>
                  </label>
                  <!-- This hint describes the service-type row, not the card, and
                       it used to sit above the port row instead — where it read as
                       a description of the card and explained the wrong field. -->
                  <label class="setting">
                    <span class="setting-name">{{ t("mDNS 服务类型") }}</span>
                    <span class="setting-control"><input v-model="settings.service_type" :aria-label="t('mDNS 服务类型')" maxlength="128" /><span class="setting-hint">{{ t("通过 mDNS 广播的零配置服务类型。除非你知道自己在做什么，否则保持默认。重启后生效。") }}</span></span>
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
                  <span class="setting-control"><input v-model="settings.data_dir" class="setting-wide" :aria-label="t('数据目录')" maxlength="4096" :placeholder="t('留空使用默认')" /><span class="setting-hint">{{ t("配置 / 历史 / 收藏的存储位置。重启后生效。") }}</span></span>
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
                  <span class="setting-control"><input v-model="translationKey" class="setting-wide" type="password" autocomplete="new-password" maxlength="4096" :aria-label="t('翻译 API 密钥')" :disabled="translationKeyBusy" /><span class="setting-hint">{{ settings.translate_key_set ? t('密钥已设置') : t('未设置密钥') }}</span></span>
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
                <p class="muted small setting-note">{{ t("加密设备之间的剪贴板、文件与聊天流量。设置密码后，重启应用需要输入密码解锁。") }}</p>
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
                <p v-if="passwordMismatch" class="muted small setting-block">{{ t("两次输入的密码不一致。") }}</p>
                <p class="muted small setting-note">{{ t("密码会随“保存设置”一起提交，并同时用作设备配对的通道密钥；剪贴板历史的密钥在重启后更新。") }}</p>
                <p class="muted setting-block">{{ settings.password_set ? t("已设置加密密码") : t("未设置加密密码") }}</p>
                <div v-if="settings.password_set" class="setting-actions">
                  <button type="button" class="danger" :disabled="securityBusy" @click="clearPasswordOpen = true"><Trash2 :size="16" />{{ t("清除加密密码") }}</button>
                </div>
                <h3>{{ t("危险区域") }}</h3>
                <p class="muted small setting-note">{{ t("恢复出厂设置会删除全部历史、收藏、配对和设备身份，并重新启动应用。此操作无法撤销。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="danger" :disabled="securityBusy" @click="factoryResetOpen = true"><Trash2 :size="16" />{{ t("恢复出厂设置") }}</button>
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
                <div class="setting-heading">
                  <h3>{{ t("可用备份") }}</h3>
                  <button type="button" class="icon-button" :title="t('刷新备份')" :aria-label="t('刷新备份')" @click="refreshBackups"><RefreshCw :size="17" /></button>
                </div>
                <ul v-if="backups.length" class="setting-block backup-list"><li v-for="item in backups" :key="String(item.path)">
                  <span class="backup-name">{{ item.filename || item.path }}</span>
                  <span class="muted small">{{ backupMeta(item) }}</span>
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
                <p class="muted small setting-note">{{ t("每约 6 小时检查一次 GitHub 是否有新版本；关闭后后台不再发起任何更新请求。") }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="!updateAvailableForUi || updateChecking || updateState.phase === 'downloading'"
                    @click="checkForUpdate">
                    <RefreshCw :size="17" :class="{ spinning: updateChecking }" />{{ updateChecking ? t('正在检查…') : t('立即检查更新') }}
                  </button>
                </div>
                <p v-if="updateAvailable" class="update-available setting-block setting-block--card" role="status">{{ t("发现新版本：") }}{{ updateLatest }}</p>
                <div v-if="updateAvailable && updateState.phase === 'idle'" class="setting-actions setting-actions--card">
                  <button type="button" class="primary" @click="startUpdateDownload">{{ t("下载更新") }}</button>
                </div>
                <div v-if="updateState.phase === 'downloading'" class="update-progress setting-block" role="progressbar"
                  :aria-valuenow="Math.round(updateState.fraction * 100)" aria-valuemin="0" aria-valuemax="100">
                  <div class="update-progress__bar" :style="{ width: (updateState.fraction * 100) + '%' }"></div>
                  <span class="update-progress__label">{{ t("正在下载…") }} {{ Math.round(updateState.fraction * 100) }}%</span>
                </div>
                <template v-if="updateState.phase === 'ready'">
                  <p class="update-available setting-block setting-block--card" role="status">{{ t("新版本 {version} 已就绪", { version: updateState.version }) }}</p>
                  <p class="muted small setting-note">{{ t("请退出当前应用，然后用下方文件替换旧版本。剪贴板历史与设备仍保留在本机。") }}</p>
                  <p class="update-ready-path selectable setting-block">{{ updateState.path }}</p>
                  <div class="setting-actions setting-actions--card">
                    <button type="button" @click="openUpdateFolder">{{ t("打开所在文件夹") }}</button>
                  </div>
                </template>
                <p v-if="updateState.phase === 'failed'" class="update-failed setting-block setting-block--card" role="alert">
                  {{ t("更新下载失败") }}{{ updateState.error ? '：' + updateState.error : '' }}</p>
                <p class="muted small setting-note">{{ t("自动下载最新版本，下载完成后提示你手动替换旧版本。") }}</p>
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
              <span v-if="devicesEngineStopped" id="devices-engine-note" class="muted small">{{ t("同步引擎未运行，推送文本与发送网址不可用。") }}</span>
              <button :aria-label="t('推送文本')" :title="t('推送文本到剪贴板并同步')" :disabled="busy || !pushTextAvailable" @click="openPushText"><SendHorizontal :size="17" />{{ t("推送文本") }}</button>
              <button class="icon-button" :aria-label="t('查看证书指纹')" :title="t('证书指纹')" :disabled="busy" @click="showCertificates"><Fingerprint :size="18" /></button>
              <button class="icon-button" :aria-label="t('刷新设备')" :title="t('刷新设备')" :disabled="busy" @click="store.refresh"><RefreshCw :size="18" :class="{ spinning: state.refreshing }" /></button>
            </div>
            <div v-if="!state.devices.length" class="empty"><Monitor :size="36" /><h2>{{ state.refreshing ? t('正在读取设备') : t('暂无设备') }}</h2></div>
            <article v-for="device in activeDevices" :key="device.id" class="device-row">
              <Monitor :size="25" class="device-icon" />
              <div class="device-identity"><h2>{{ device.name }}</h2><span class="muted small">{{ device.id }}</span></div>
              <div class="row-actions">
                <button v-if="device.paired" class="icon-button" :aria-label="t('撤销信任')" :title="t('撤销信任')" :disabled="busy" @click="revokeDevice = device"><Unlink :size="18" /></button>
                <button v-else-if="!pairingPending(device)" :disabled="busy || device.connection_state === 'offline'" @click="store.startPairing(device)"><Link :size="17" />{{ t("配对") }}</button>
                <button v-if="device.paired && device.connection_state !== 'online'" class="icon-button" :aria-label="t('连接设备')" :title="t('连接设备')" :disabled="busy" @click="store.connect(device)"><Plug :size="18" /></button>
                <button v-if="device.connection_state === 'online'" class="icon-button" :aria-label="t('断开连接')" :title="t('断开连接')" :disabled="busy" @click="store.disconnect(device)"><PlugZap :size="18" /></button>
                <button v-if="device.paired" class="icon-button" :aria-label="t('测试连接')" :title="t('测试连接')" :disabled="busy || !!probeBusyId" @click="testConnection(device)"><Activity :size="18" :class="{ spinning: probeBusyId === device.id }" /></button>
                <button v-if="device.paired" class="icon-button" :aria-label="t('发送网址')" :title="t('发送网址')" :aria-describedby="sendUrlAvailable ? undefined : 'devices-engine-note'" :disabled="busy || !sendUrlAvailable" @click="openSendUrl(device)"><Globe :size="18" /></button>
                <button class="icon-button" :aria-label="t('移除设备')" :title="t('移除设备')" :disabled="busy" @click="forgetDevice = device"><Trash2 :size="18" /></button>
              </div>
              <p v-if="probeResults[device.id]" class="muted small device-full" role="status">{{ t("连接测试：") }}{{ probeLabel(probeResults[device.id]) }}</p>
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
                <span class="channel channel--paired"><ShieldCheck :size="12" />{{ pairingLabel(device) }}</span>
                <span class="channel" :class="`channel--${device.connection_state || 'offline'}`" :title="t('本地连接')">
                  <Plug :size="12" />{{ t("本地") }}·{{ connectionLabel(device.connection_state) }}
                </span>
                <span class="channel" :class="`channel--${relayChannel(device) === 'unpaired' ? 'unpaired' : (relayChannel(device) === 'online' ? 'online' : 'offline')}`"
                  :title="relayLastSeen(device) || t('互联网配对')">
                  <Globe :size="12" />{{ t("互联网") }}·{{ relayChannelLabel(device) }}
                </span>
                <span v-if="delivery.pending(device.id) > 0" class="channel channel--pending"
                  :title="t('对方离线时内容暂存，上线后自动补发')">{{ t("待补发 {count}", { count: delivery.pending(device.id) }) }}</span>
              </span>
              <input v-if="device.paired" class="device-note" :value="device.note || ''" maxlength="512" :placeholder="t('设备备注')" :aria-label="t('设备备注')" @change="saveDeviceNote(device, $event)" />
              <div v-if="pairingPending(device)" class="pairing-controls">
                <p v-if="device.pairing_code">{{ t("配对码：") }}<strong>{{ device.pairing_code }}</strong></p>
                <p v-if="device.sas">{{ t("安全代码：") }}<strong>{{ device.sas }}</strong></p>
                <p class="muted small">{{ t("请核对两台设备上的代码，仅在一致时确认。") }}</p>
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
              <h2 class="muted small">{{ t("已移除的设备") }}</h2>
              <article v-for="device in archivedDevices" :key="device.id" class="device-row">
                <Monitor :size="25" class="device-icon" />
                <div class="device-identity"><h2>{{ device.name }}</h2><span class="muted small">{{ device.id }}</span></div>
                <div class="row-actions">
                  <button :disabled="busy" @click="store.restore(device)"><RotateCcw :size="17" />{{ t("恢复") }}</button>
                  <button class="danger" :disabled="busy" @click="purgeDevice = device"><Trash2 :size="17" />{{ t("彻底删除") }}</button>
                </div>
                <!-- The same second line the live rows carry, holding the one
                     fact an archived row has instead of a route: when it was
                     taken away.  Written as the same chip so a reader scanning
                     the column of second lines reads dates in the same place a
                     live row's routes are. -->
                <span class="device-channels">
                  <span class="channel">{{ t("移除于") }} {{ date(device.removed_at || 0) }}</span>
                </span>
              </article>
            </section>

          </template>

          <template v-else-if="deviceTab === 'internet'">
            <section class="settings-section">
              <h2>{{ t("互联网配对") }}</h2>
              <!-- This machine's own link to the relay, above the peers rather
                   than beside them.  Every other 在线 in this card is the
                   relay's view of *another* device, so when our link is down
                   the whole list reads 离线 and nothing on screen says whether
                   they are away or we are.  It is the first line of the card
                   for that reason. -->
              <p v-if="relayState" class="setting-block setting-block--card relay-state" :class="`relay-state--${relayState}`">
                <Activity :size="14" />{{ t("本机中继") }}：<strong>{{ relayStateLabel }}</strong>
                <span v-if="relayState !== 'online' && relayState !== 'connecting'" class="muted small">{{ t("对方在线与否以中继连接为准；本机中继不可用时，所有设备都会显示为离线。") }}</span>
              </p>
              <div class="setting-actions setting-actions--card">
                <button type="button" @click="generateInternetPairing">{{ t("生成配对码") }}</button>
                <button type="button" @click="refreshInternetPairing">{{ t("刷新") }}</button>
              </div>
              <p v-if="internetPairing.generated_code" class="setting-block setting-block--card">{{ t("本机配对码：") }}<strong>{{ internetPairing.generated_code }}</strong></p>
              <label class="setting">
                <span class="setting-name">{{ t("输入对方配对码") }}</span>
                <!-- The field shows the code in the shape this machine generates
                     one — upper case, four at a time — and takes a paste in any
                     shape of it.  A plain text box let a reader type the code
                     they were shown, in the case they were shown it, and watch
                     the box render it differently from the screen they were
                     reading it off; see `lib/pairing-code.ts`. -->
                <span class="setting-control"><input
                  :value="internetPairingCode" maxlength="14" autocomplete="off" spellcheck="false"
                  autocapitalize="characters" :placeholder="t('XXXX-XXXX-XXXX')"
                  :aria-label="t('输入对方配对码')" @input="setInternetPairingCode($event)" /></span>
              </label>
              <div class="setting-actions">
                <button type="button" @click="enterInternetPairing" :disabled="!internetPairingComplete">{{ t("提交配对码") }}</button>
              </div>
              <!-- A refusal rather than a note: it names what to do about it,
                   and it stays beside the box that caused it. -->
              <p v-if="internetPairingMessage" class="setting-block pairing-message"
                :class="{ 'pairing-message--failed': internetPairingFailed }" role="status">{{ internetPairingMessage }}</p>
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
                    <span class="pairing-wait" :title="row.since ? t('提交于 {when}', { when: date(row.since) }) : ''">
                      <Clock :size="14" />{{ row.name ? t("等待 {name} 确认…", { name: row.name }) : t("等待对方确认…") }}
                    </span>
                    <button type="button" class="text-button" @click="unpairInternet(row.peer_id)">{{ t("撤销") }}</button>
                  </div>
                  <p class="muted small">{{ t("配对码已提交，对方通过中继确认后即会出现在上方设备列表中。") }}</p>
                </li>
              </ul>
              <p class="muted setting-block setting-block--card">{{ t("待投递消息：") }}{{ delivery.total }}
                <button type="button" class="icon-button" :title="t('刷新投递状态')" :aria-label="t('刷新投递状态')" @click="refreshDeliveryStatus"><RefreshCw :size="15" /></button>
              </p>
              <!-- One line per peer, the way the legacy card carried it: what
                   is still queued for this device, and how its newest send
                   ended.  Both are hidden until the ledger has answered, so a
                   backend that cannot report never reads as "nothing sent". -->
              <ul class="setting-block peer-list"><li v-for="peer in internetPairing.peers" :key="peer.peer_id">
                <div class="peer-line">
                  <button type="button" class="text-button" @click="renameInternet(peer)">{{ peer.alias || peer.name || peer.peer_id }}</button>
                  <!-- 离线 alone left "away since breakfast" and "never seen"
                       looking the same, though the relay reports when it last
                       heard from the peer. -->
                  <span class="muted" :title="peer.last_seen ? t('最后在线 {when}', { when: date(peer.last_seen) }) : t('尚未连接过')">{{ peer.online ? t('在线') : t('离线') }}</span>
                  <button type="button" class="icon-button" :title="t('解除互联网配对')" :aria-label="t('解除互联网配对')" @click="unpairInternet(peer.peer_id)"><Unlink :size="16" /></button>
                </div>
                <div v-if="delivery.shown(String(peer.peer_id))" class="peer-delivery">
                  <span v-if="delivery.pending(String(peer.peer_id)) > 0" class="delivery-badge"
                    :title="t('对方离线时内容暂存，上线后自动补发')">{{ t("待补发 {count}", { count: delivery.pending(String(peer.peer_id)) }) }}</span>
                  <span v-if="deliveryGlyph(peer.peer_id)" class="delivery-result" :class="`delivery-result--${delivery.lastStatus(String(peer.peer_id))}`">
                    <component :is="deliveryGlyphs[deliveryGlyph(peer.peer_id)!]" :size="13" />{{ deliveryText(peer.peer_id) }}
                  </span>
                </div>
              </li></ul>
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
                </div>
                <label v-if="companion.running && companion.access_url" class="setting">
                  <span class="setting-name">{{ t("手机访问地址") }}</span>
                  <span class="setting-control"><input :value="companion.access_url" class="setting-wide" readonly :aria-label="t('手机访问地址')" /></span>
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
        <label class="sync-toggle"><input type="checkbox" role="switch" :aria-label="t('启用同步')"
          :checked="state.status?.sync_state === 'running'" :disabled="!syncAvailable || busy"
          @change="toggleSync" />{{ t("同步") }}</label>
        <!-- The pause presets the dashboard offered.  While a deadline is
             armed the row becomes its countdown and the way out of it; the
             presets themselves are only offered while sync is running, because
             they share this row with the plain resume a user needs after
             turning sync off from the toggle beside them. -->
        <template v-if="pauseLeftMs > 0">
          <span class="pause-status">⏸ {{ t("已暂停 · 剩余 {minutes} 分钟", { minutes: pauseLeftMinutes }) }}</span>
          <button class="status-action" :disabled="pauseBusy" @click="resumeSync">{{ t("立即恢复") }}</button>
        </template>
        <template v-else-if="state.status?.sync_state === 'running'">
          <span class="pause-label">{{ t("定时暂停同步") }}</span>
          <button class="status-action" :disabled="pauseBusy" @click="pauseSync(15)">{{ t("15 分钟") }}</button>
          <button class="status-action" :disabled="pauseBusy" @click="pauseSync(30)">{{ t("30 分钟") }}</button>
          <button class="status-action" :disabled="pauseBusy" @click="pauseSync(60)">{{ t("1 小时") }}</button>
        </template>
        <button v-else class="status-action" :disabled="pauseBusy" @click="resumeSync">{{ t("恢复同步") }}</button>
        <!-- Legacy showed its notices as one floating toast rather than per
             page, and the footer is the surface that stays on screen: a message
             about a pause started here, or about a page the user has since left,
             is still readable. -->
        <span v-if="statusMessage" class="muted small" role="status">{{ statusMessage }}</span>
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
      <p v-if="translateItemTruncated" class="muted small translate-truncated">{{ t("记录过长，只读取并翻译了前 {count} 个字符", { count: translateItemText.length }) }}</p>
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
      <p v-if="logsBusy" class="muted">{{ t("正在读取日志…") }}</p>
      <pre v-else-if="logLines.length" class="log-view" :aria-label="t('日志内容')">{{ logLines.join("\n") }}</pre>
      <p v-else class="muted">{{ t("暂无日志") }}</p>
      <p v-if="logExportMessage" role="status" class="muted small">{{ logExportMessage }}</p>
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
      <p v-if="qrUrl" class="muted small selectable">{{ qrUrl }}</p>
      <div class="modal-actions"><button autofocus @click="qrOpen = false">{{ t("关闭") }}</button></div>
    </dialog>
    <dialog ref="aboutDialog" aria-labelledby="about-title" class="modal" @close="aboutOpen = false" @cancel="aboutOpen = false">
      <h2 id="about-title">{{ t("关于 ClipSync") }}</h2>
      <p class="about-version">ClipSync {{ state.status ? state.status.version : "…" }}</p>
      <p class="muted">{{ t("跨平台剪贴板共享，支持 Windows、macOS 和 Linux。") }}</p>
      <p class="muted">{{ t("在设备之间实时共享剪贴板内容与文件。") }}</p>
      <p v-if="aboutMessage" role="status" class="muted small">{{ aboutMessage }}</p>
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
        <p class="muted small">{{ diagnosticsOverview }}</p>
        <ul class="diag-list">
          <li v-for="group in diagnosticGroups" :key="group.id">
            <h3>{{ group.label }}</h3>
            <ul>
              <li v-for="item in group.items" :key="item.id" :class="`diag-${item.status}`">
                <span class="diag-item"><span class="diag-label">{{ diagnosticLabel(item) }}</span>{{ diagnosticDetail(item) }}</span>
                <span v-if="diagnosticHint(item)" class="muted small">{{ diagnosticHint(item) }}</span>
                <button v-if="item.id === 'firewall' && item.status !== 'ok'" type="button" class="status-action" :disabled="diagnosticsRepairBusy" @click="repairDiagnostics('firewall')"><Wrench :size="14" />{{ t("修复防火墙") }}</button>
              </li>
            </ul>
          </li>
        </ul>
        <template v-if="permissionsRepairCheck">
          <p class="muted small">{{ diagnosticsCheckText(permissionsRepairCheck) }}</p>
          <p v-if="permissionsRepairCheck.guidance_text || permissionsRepairCheck.guidance" class="muted small">{{ permissionsRepairCheck.guidance_text || permissionsRepairCheck.guidance }}</p>
          <div class="actions">
            <button type="button" :disabled="diagnosticsRepairBusy" @click="repairDiagnostics('local_network')"><Wrench :size="15" />{{ t("打开本地网络权限") }}</button>
          </div>
        </template>
      </template>
      <p v-else class="muted">{{ t("暂时无法获取诊断信息") }}</p>
      <div class="modal-actions">
        <button autofocus :disabled="diagnosticsBusy" @click="refreshDiagnostics">{{ t("重新检测") }}</button>
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
      <p class="muted small">{{ t("把来源设备的配置与技能带到本机：先对比，再选冲突策略，最后一次拉取。") }}</p>
      <label class="setting">
        <span class="setting-name">{{ t("来源设备") }}</span>
        <span class="setting-control"><select v-model="aiPeerId" :aria-label="t('迁移来源设备')">
          <option value="">{{ t("选择已配对设备") }}</option>
          <option v-for="device in state.devices.filter(device => device.paired)" :key="device.id" :value="device.id">{{ device.name }}</option>
        </select></span>
      </label>
      <p v-if="!aiPeerId" class="muted small">{{ t("先选择一台已配对设备") }}</p>
      <template v-else>
        <div v-if="aiRemoteWaiting" class="muted small">{{ t("已请求更新，等待对方返回库存") }}</div>
        <label v-for="option in aiMigrateStrategies" :key="option.value" class="setting setting--check">
          <span class="setting-control"><input v-model="aiMigrateStrategy" type="radio" :value="option.value" :aria-label="option.label" /><span>{{ option.label }}</span></span>
        </label>
        <p class="muted small setting-note">{{ aiMigrateSummary }}</p>
        <div class="modal-actions">
          <button @click="aiMigrateDialog?.close()">{{ t("取消") }}</button>
          <button class="primary" :disabled="!aiMigrateTargets.length" @click="startAiMigration">{{ t("开始迁移（{count}）", { count: aiMigrateTargets.length }) }}</button>
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
      <p v-if="state.error" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <p v-if="batchDeleteIds && !batchDeleteValid" role="status">{{ t("所选记录已变化，请取消并重新选择。") }}</p>
      <div class="modal-actions"><button autofocus @click="batchDeleteIds = null">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy || !batchDeleteValid" @click="confirmBatchDelete">{{ t("删除所选记录") }}</button></div>
    </dialog>
    <dialog ref="clearHistoryDialog" aria-labelledby="clear-history-title" class="modal" @close="clearHistoryOpen = false" @cancel="clearHistoryOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="clearHistoryOpen = false"><X :size="18" /></button>
      <h2 id="clear-history-title">{{ t("清空全部历史记录？") }}</h2>
      <p class="muted">{{ t("共 {count} 条记录将被移除，此操作无法撤销。", { count: state.total }) }}</p>
      <p v-if="state.error" role="alert">{{ state.error.message }} ({{ state.error.code }})</p>
      <div class="modal-actions"><button autofocus @click="clearHistoryOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="historyBusy" @click="confirmClearHistory">{{ t("清空历史") }}</button></div>
    </dialog>
    <dialog ref="revokeDialog" aria-labelledby="revoke-title" class="modal" @close="revokeDevice = null" @cancel="revokeDevice = null">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="revokeDevice = null"><X :size="18" /></button>
      <h2 id="revoke-title">{{ t("撤销设备信任？") }}</h2>
      <p class="muted">{{ t("{name} 将无法继续同步，重新连接需要双方再次配对。", { name: revokeDevice?.name }) }}</p>
      <div class="modal-actions"><button autofocus @click="revokeDevice = null">{{ t("取消") }}</button><button class="danger" :disabled="busy || !state.devices.some(device => device.id === revokeDevice?.id && device.paired)" @click="confirmRevoke">{{ t("撤销信任") }}</button></div>
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
    <!-- A device whose certificate no longer matches its pin was refused, so
         this is the only place the user hears about it — and the answer is what
         either re-pins the certificate or unpairs the device. -->
    <dialog ref="certAlertDialog" aria-labelledby="cert-alert-title" class="modal" @cancel.prevent @close="certPromptOpen = false">
      <h2 id="cert-alert-title">{{ t("设备身份变更") }}</h2>
      <p>{{ t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: certAlertName }) }}</p>
      <p class="muted small">{{ t("这是您信任的设备吗？") }}</p>
      <p v-if="!state.certAlert?.can_trust" class="muted small">{{ t("该设备需要再次连接后才能信任新证书。") }}</p>
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
      <p class="muted small">{{ t("同步与聊天只接受指纹一致的设备，发现不一致请撤销信任后重新配对。") }}</p>
      <ul v-if="certificates?.length" class="cert-list">
        <li v-for="item in certificates" :key="item.device_id">
          <strong>{{ item.device_name || item.device_id }}</strong>
          <span class="muted small fingerprint">{{ item.fingerprint_short || t("未固定") }}</span>
          <span class="muted small">{{ item.paired ? t("已配对") : t("未配对") }}</span>
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
      <p class="muted small">{{ t("首次使用 ClipSync，请选择界面语言") }}<br />Please choose your interface language</p>
      <div class="language-choices">
        <button v-for="choice in languageChoices" :key="choice.code" type="button" class="language-choice" @click="chooseLanguage(choice.code)">
          <strong>{{ choice.native }}</strong>
          <span class="muted small">{{ choice.other }}</span>
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
        <span class="muted small">{{ t("拖放不会自动发送，设备仍由您指定") }}</span>
      </div>
    </div>
  </div>
</template>
