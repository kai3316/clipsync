<script setup lang="ts">
/**
 * The AI page: what each configured tool keeps on this machine, the same
 * question asked of a paired device, and the pull that moves one to the other.
 *
 * Extracted from `App.vue` as one page, not as "a file split" -- see U03.  The
 * point is the re-render boundary: this page holds a JSON editor, so every
 * keystroke in it used to re-run the diff of the window's whole 3 400-line
 * template.  Everything the page owns lives in here now, and the three things
 * it does not arrive across the seam below.
 *
 * The seam is deliberately small and wide instead of many narrow props: the
 * store is handed down the way `FavoritesView` takes it (this app's stores are
 * per-window factories, not singletons), and the two chords the window owns --
 * Ctrl+S and the way into the page -- reach in through `enter()` and
 * `saveIfDirty()`.  Nothing else in the window reads this page's state.
 */
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import {
  AlertCircle, Check, ChevronLeft, ChevronRight, FileUp, FolderOpen, LogOut,
  RefreshCw, RotateCcw, Save, Search, Trash2, Wand2, X,
} from "@lucide/vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import { type createApplicationStore } from "../stores/application";
import { aiCompareState, aiDiffCounts, aiEntryKey, buildAiLocalIndex } from "../lib/aiconfig-diff";
import { aiItemCount, aiTreeGroups, type AiGroup, type AiNode, type AiRow } from "../lib/aiconfig-tree";
import { aiTargets, type AiTarget } from "../lib/aiconfig-targets";
import { deviceLabel } from "../lib/device-row";
import { announce } from "../lib/status";
import { tabKeydown } from "../lib/tabs";
import { useDialog } from "../lib/use-dialog";
import AppDialog from "./AppDialog.vue";

const props = defineProps<{
  /** The window's store.  Handed down rather than imported: `createApplicationStore`
   * builds a new one per call, so a component that called it would hold a second,
   * empty copy of the window's state. */
  store: ReturnType<typeof createApplicationStore>;
}>();
const store = props.store;
const state = store.state;

const aiTools = ref<Array<{ key: string; label: string; paths: string[] }>>([]);
const aiEnabled = ref<string[]>([]);
const aiCustomPaths = ref("");
/** What the folder picker just did, said beside the button that opened it —
 * the paths are a form field, and the box it changed is a few lines above. */
const aiCustomPathNote = ref("");
const aiLocalItems = ref<Array<Record<string, any>>>([]);
/** The watch roots the last walk was pointed at, and what each one produced.
 *
 * Kept because the walk's answer is the only place the difference between the
 * two kinds of empty root can be seen: a directory that is on disk and holds
 * nothing readable, and a path that is not on disk at all — a path the reader
 * typed wrong, which is the one of the two they can do something about. */
const aiLocalRoots = ref<Array<{ tool: string; path: string; kind: string; count: number; exists: boolean }>>([]);
const aiLocalBusy = ref(false);
/** Watch roots doing nothing, and which of the two kinds of nothing it is. */
const aiRootIssues = computed<Array<{ path: string; reason: string }>>(() => {
  const rows = aiLocalRoots.value.filter(root => root.tool === "custom");
  return [
    ...rows.filter(row => !row.exists).map(row => ({ path: row.path, reason: t("目录不存在") })),
    ...rows.filter(row => row.exists && !row.count).map(row => ({ path: row.path, reason: t("没有可同步的文件") })),
  ];
});
/** Paths the reader typed that the walk has no root for.
 *
 * A stored path that duplicates a built-in entry is dropped rather than walked
 * twice, so it stays in the box below and collects nothing — and nothing on
 * screen said why.  The walk's roots are the only record of which paths were
 * taken up, so this is the stored list less those. */
const aiCustomStored = ref<string[]>([]);
const aiCoveredPaths = computed(() => {
  const walked = new Set(aiLocalRoots.value.filter(root => root.tool === "custom").map(root => root.path));
  return aiCustomStored.value.filter(path => !walked.has(path));
});
const aiMessage = ref("");
const aiSelectedItem = ref<Record<string, any> | null>(null);
const aiEditorContent = ref("");
const aiSavedContent = ref("");
const aiEditorDirty = computed(() => !!aiSelectedItem.value && aiEditorContent.value !== aiSavedContent.value);
/** A JSON file that no longer parses is refused here instead of saved.  The
 * sidecar would land it happily and the tool would fail at its next start,
 * with no line to point at — the window can tell a broken object from a whole
 * one, so it does.  TOML and YAML have no parser in the window: those still go
 * to the sidecar, which answers with the line it refused. */
const aiEditorError = ref("");
function aiEditorIsJson(item: Record<string, any> | null) {
  return /\.json$/i.test(String(item?.rel_path || ""));
}
function aiJsonProblem(content: string) {
  try { JSON.parse(content); return ""; }
  catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    // "… in JSON at position 42": the offset is the only part of the engine's
    // wording that is stable across versions, so the line and column are
    // counted here rather than parsed out of the sentence.
    const position = /position (\d+)/.exec(message);
    if (!position) return t("这不是有效的 JSON，保存前请先修正。");
    const offset = Math.min(Number(position[1]), content.length);
    const before = content.slice(0, offset);
    const line = before.split("\n").length;
    const column = offset - before.lastIndexOf("\n");
    return t("这不是有效的 JSON：第 {line} 行第 {column} 列附近有问题，保存前请先修正。", { line, column });
  }
}
/** What is wrong with the text on screen right now.  A JSON problem is
 * recomputed as the reader types, so the message goes away the moment the file
 * parses again; a refusal from the sidecar names something the window cannot
 * see, and stands until the next save attempt. */
const aiEditorProblem = computed(() => {
  if (!aiSelectedItem.value) return "";
  if (aiEditorIsJson(aiSelectedItem.value)) return aiJsonProblem(aiEditorContent.value);
  return aiEditorError.value;
});
/** What the reader asked for with changes unsaved in the editor: another file
 * to open, or no file at all — putting the editor away.  Either way the
 * question asked first is the same one, so the answer shares one dialog. */
const aiDiscardTarget = ref<{ item: Record<string, any> | null } | null>(null);
const aiDiscardDialog = useDialog(aiDiscardTarget);
function requestAiRead(item: Record<string, any>) {
  if (aiMutationBusy.value) return;
  if (aiEditorDirty.value) aiDiscardTarget.value = { item };
  else void readAiLocal(item);
}
/** The editor's way out.  It is a panel the reader opened on a row, and a
 * panel with no way to put it down stays under everything they do next — the
 * local list, the other devices — until they open some other file. */
function requestAiClose() {
  if (aiEditorDirty.value) aiDiscardTarget.value = { item: null };
  else closeAiEditor();
}
function closeAiEditor() {
  // An in-flight read must not reopen the file that was just put away.
  ++aiReadGeneration;
  aiSelectedItem.value = null;
  aiEditorContent.value = "";
  aiSavedContent.value = "";
  aiEditorError.value = "";
}
async function discardAiChanges() {
  const target = aiDiscardTarget.value;
  aiDiscardTarget.value = null;
  if (target?.item) await readAiLocal(target.item);
  else closeAiEditor();
}
let aiReadGeneration = 0;
const aiMutationBusy = ref(false);
const aiTrashItem = ref<Record<string, any> | null>(null);
const aiTrashDialog = useDialog(aiTrashItem);
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
/** One peer file the reader asked to look at, before deciding to pull it.
 *
 * Kept apart from `aiRemoteMessage`: the message is the card's own report on
 * what it just did, and a preview is a file somebody else wrote. */
const aiPreview = ref<{ path: string; content: string; loading: boolean; truncated: boolean } | null>(null);
// A preview is a file on the peer the picker names, so changing the peer makes
// it a file from somewhere else — and one the list no longer offers.  Turning
// the page does the same thing closer to home: the row the reader clicked is
// no longer on screen, and a panel quoting a row nobody can see is a panel
// with nothing to check it against.
watch([aiPeerId, aiRemotePage], () => { aiPreview.value = null; });
const aiRemoteMessage = ref("");
const aiRemoteWaiting = ref(false);
/** Keys of the remote entries ticked for a batch pull.  Kept as keys rather than
 * as row indexes so a refresh that reorders or shortens the list cannot silently
 * move a tick onto a different file: a key that is no longer present is dropped. */
const aiRemoteSelected = ref<string[]>([]);
const aiPullMode = ref<"copy" | "overwrite" | "append">("copy");
/** What the chosen pull mode does, in one line.  The modes are saved between
 * sessions, so this is also the only place a reader is told what the mode they
 * came back to means. */
const aiPullModeNote = computed(() => aiPullMode.value === "overwrite"
  ? t("覆盖会用对方版本替换本机同名文件；本机原文件另存为 .bak。")
  : aiPullMode.value === "append"
    ? t("追加把对方内容接到本机文件末尾，本机原有内容保留；两端都必须是文本文件。")
    : t("副本不改动本机已有文件：本机没有的直接写入，已有的另存为带设备名的副本。"));
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
/** The three strategies, each with the consequence of choosing it.
 *
 * Three radios and three names was the whole of the decision: 「覆盖本机文件」
 * does not say that the replaced files come back as `.bak`, and 「另存为副本」
 * does not say the disk gains a second copy of everything that differs.  The
 * reader is choosing between outcomes, not between labels, and the one strategy
 * that can lose something was the least explained of the three.
 */
const aiMigrateStrategies = computed<Array<{
  value: "skip" | "copy" | "overwrite"; label: string; note: string;
}>>(() => [
  {
    value: "skip",
    label: t("只补缺失（默认）"),
    note: t("本机已有的配置一个都不动，只把缺的补上。"),
  },
  {
    value: "copy",
    label: t("全部拉取，已有的另存为副本"),
    note: t("本机已有的同名文件保留，拉下来的另存一份。文件会变多，但谁都不会被覆盖。"),
  },
  {
    value: "overwrite",
    label: t("全部拉取，覆盖本机文件"),
    note: t("本机的同名文件会被替换，替换前留一份 .bak。这是三个选项里唯一会改动已有内容的。"),
  },
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
/** Those targets, grouped under the tool that owns them, for the checklist.
 *
 * 「开始迁移（12）」 was a number with nothing to check it against, and a
 * migration that pulls the wrong files writes over configuration nobody can
 * reconstruct from memory.  The names are the point of the step before the
 * pull — grouped rather than flat, because a tool's ten entries are one
 * decision ("do I want this program's settings?") while ten rows are ten.
 */
const aiMigrateGroups = computed(() => {
  const groups: Array<{ tool: string; targets: AiTarget[] }> = [];
  for (const target of aiMigrateTargets.value) {
    const last = groups[groups.length - 1];
    if (last && last.tool === target.tool) last.targets.push(target);
    else groups.push({ tool: target.tool, targets: [target] });
  }
  return groups.map((group) => ({
    tool: group.tool,
    targets: group.targets,
    files: group.targets.reduce((total, target) => total + target.items.length, 0),
  }));
});
const aiMigrateSummary = computed(() => {
  const count = aiMigrateTargets.value.length;
  if (!count) return t("没有需要迁移的配置项");
  if (aiMigrateStrategy.value === "skip") return t("将补齐本机缺少的 {count} 个配置项", { count });
  if (aiMigrateStrategy.value === "copy") return t("将拉取 {count} 个不同的配置项，本机已有的另存为副本", { count });
  return t("将覆盖 {count} 个不同的配置项，被覆盖的原文件保留为 .bak", { count });
});
async function openAiMigrate() {
  // Awaits the tick itself rather than going through `useDialog`: the read that
  // fills the wizard has to see the dialog's own state, and the flag is raised
  // by the caller (a device button, the tray command) rather than by the dialog.
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
const aiPullProgress = ref<{ peerId: string; mode: string; total: number; done: number; failed: number;
  /** One line per file that did not land: "<rel_path>：<why>".  Kept so the
   * completion message can say why instead of only how many — five files
   * failing on every retry with nothing naming the reason is what a bare count
   * looked like. */
  failures: string[];
  /** What this pull asked for, and which of those did not land.  A retry is a
   * second pull of the wrong subset if the failed ones are only remembered by
   * their `rel_path：reason` line: the entries themselves are what the sidecar
   * takes, so they are kept as they were sent. */
  items: Array<Record<string, any>>;
  failedItems: Array<Record<string, any>> } | null>(null);
/** The failed part of the last pull, kept for one more try.  Copy mode retries
 * straight away; overwrite and append ask again, because the second request
 * lands on files the first one may already have touched. */
const aiPullRetry = ref<{ items: Array<Record<string, any>>; mode: string } | null>(null);
/** How the last pull ended, for the line that reports it: empty while a pull is
 * running or before one has. */
const aiPullOutcome = ref<"" | "done" | "failed">("");
const aiPullDialog = useDialog(aiPullPending);
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

const AI_SECTIONS = ["tools", "local", "peer"] as const;
type AiSection = (typeof AI_SECTIONS)[number];
const aiTab = ref<AiSection>("tools");
const aiSections = computed<Array<{ id: AiSection; label: string }>>(() => [
  { id: "tools", label: t("AI 工具") },
  { id: "local", label: t("本机配置") },
  { id: "peer", label: t("其他设备") },
]);

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
    aiTools.value = (Array.isArray(profiles.tools) ? profiles.tools as Array<Record<string, any>> : [])
      .map(tool => ({
        key: String(tool?.key || ""),
        label: String(tool?.label || ""),
        // A directory is named as one, so a whole skills folder does not read
        // as the single file beside it.
        paths: (Array.isArray(tool?.entries) ? tool.entries as Array<Record<string, any>> : [])
          .filter(entry => String(entry?.path || ""))
          .map(entry => `${String(entry?.path)}${entry?.kind === "dir" ? "/" : ""}`),
      }));
    aiEnabled.value = Array.isArray(profiles.enabled) ? profiles.enabled as string[] : [];
    aiCustomPaths.value = Array.isArray(profiles.custom_paths) ? (profiles.custom_paths as string[]).join("\n") : "";
    aiProfilesSaved.value = aiProfilesFormSnapshot();
    aiProfilesLoaded.value = true;
  } catch (error) { store.setError(error); }
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
    // The tools and the paths are what the walk reads, so a list already on
    // screen is now describing a machine that no longer exists: ticking a tool
    // and saving it used to leave the old rows up, as though nothing had been
    // enabled.  The walk is the card's own button's to ask for on the way in;
    // a save is not the way in, it is the reader changing what a walk would
    // find — and what it found is the only thing that can say whether the
    // paths they just typed are real.
    await refreshAiLocal();
  } catch (error) { store.setError(error); }
  finally { aiProfilesBusy.value = false; }
}
/** Add one directory to the custom-path box instead of typing it.
 *
 * The paths are what the walk reads, so a typo in one of them is a directory
 * that silently collects nothing — the picker cannot typo.  Appending rather
 * than replacing keeps the box editable by hand as well; a path already in it
 * is said out loud rather than added twice, because two identical lines are
 * one directory walked twice. */
async function browseAiCustomPath() {
  if (aiProfilesBusy.value) return;
  try {
    const picked = await bridge.chooseFolder();
    if (!picked) return;
    const lines = aiCustomPaths.value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
    if (lines.includes(picked)) {
      aiCustomPathNote.value = t("这个目录已经在列表里：{path}", { path: picked });
      return;
    }
    lines.push(picked);
    aiCustomPaths.value = lines.join("\n");
    aiCustomPathNote.value = t("已加入 {path}：保存后才会参与扫描。", { path: picked });
  } catch (error) { store.setError(error); }
}

async function refreshAiLocal() {
  if (aiLocalBusy.value) return;
  aiLocalBusy.value = true;
  try {
    const result = await bridge.aiLocal("listing");
    aiLocalItems.value = Array.isArray(result.entries) ? result.entries as Array<Record<string, any>> : [];
    // The roots the walk was pointed at, kept beside the entries it came back
    // with: a root that collected nothing is the list's own explanation, and
    // the count alone cannot say which kind of nothing it was.
    aiLocalRoots.value = (Array.isArray(result.roots) ? result.roots as Array<Record<string, any>> : []).map(root => ({
      tool: String(root?.tool || ""),
      path: String(root?.path || ""),
      kind: String(root?.kind || ""),
      count: Number(root?.count || 0),
      exists: root?.exists === true,
    }));
    // The paths as stored, which is not always the paths as walked: the two
    // lists are what says a typed path was dropped.
    aiCustomStored.value = Array.isArray(result.custom_paths) ? result.custom_paths.map(String) : [];
    aiLocalPage.value = 0;
    aiMessage.value = t("已读取 {count} 个配置项", { count: aiItemCount(aiLocalItems.value) });
  } catch (error) { store.setError(error); }
  finally { aiLocalBusy.value = false; }
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
    } else if (!answered) {
      // Nothing came back, and an empty list is not an answer: offline, an older
      // build that cannot be asked, and a peer that simply did not reply all
      // drew the same blank page.  The device row already knows which of the
      // first two it is, so it is asked rather than the sidecar.
      const peer = state.devices.find(device => device.id === peerId);
      aiRemoteMessage.value = !peer
        ? t("这台设备已不在列表里，无法读取。")
        : peer.connection_state !== "online"
          ? t("对方当前离线，读不到它的配置；等它上线再试。")
          : t("对方没有返回配置清单——可能是它的版本较旧，或还没开启 AI 配置。");
    }
  } catch (error) {
    if (generation === aiInventoryGeneration) store.setError(error);
  }
}
async function previewAiRemote(item: Record<string, any>) {
  const peerId = aiPeerId.value;
  if (!state.devices.some(device => device.id === peerId && device.paired)) return;
  const generation = ++aiRemoteGeneration;
  const path = String(item.rel_path || "");
  // Its own place on the page, not the card's status line: what comes back is
  // up to 64 KiB of somebody's configuration, and that line is the card's own
  // report on what it just did — a count one moment, a pull's outcome the next.
  // Cleared first so a second preview says it is loading rather than showing
  // the previous file's text.
  aiPreview.value = { path, content: "", loading: true, truncated: false };
  try {
    const result = await bridge.aiPreview(peerId, String(item.tool || ""), String(item.root || ""), path);
    if (generation !== aiRemoteGeneration) return;
    aiPreview.value = {
      path, content: String(result.content || t("空文件")), loading: false, truncated: !!result.truncated,
    };
  } catch (error) {
    if (generation === aiRemoteGeneration) {
      aiPreview.value = null;
      store.setError(error);
    }
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
  // A new request replaces the last one's leftovers: what failed before is only
  // worth retrying while it is the newest answer.
  aiPullRetry.value = null;
  try {
    const payload = items.map(item => ({
      tool: item.tool, root: item.root || "", rel_path: item.rel_path, is_dir: !!item.is_dir,
    }));
    const result = await bridge.aiPull(peerId, payload, mode);
    if (generation !== aiRemoteGeneration) return;
    const errors = Array.isArray(result.errors) ? result.errors.map(String) : [];
    const requested = Number(result.requested || 0);
    aiPullOutcome.value = "";
    // A peer that refused the whole batch refused every entry in it, and the
    // count is the entries rather than the number of refusal messages it
    // managed to word — otherwise 「0 成功、1 失败」 stands for two files.
    const refused = requested ? errors.length : items.length;
    // What the peer accepted is the number of events to expect.  A request it
    // refused outright failed here rather than by never arriving, so those are
    // counted into the same report as the files that fail to land.
    aiPullProgress.value = requested || errors.length
      ? {
          peerId, mode, total: requested + refused, done: 0, failed: refused,
          failures: errors.map(aiPullErrorText),
          // A peer that refused the whole batch refused every entry in it; a
          // partial refusal names no entries, so only the events can say which
          // files failed — the rest stay out of the retry rather than risk
          // landing twice in append mode.
          items,
          failedItems: errors.length && !requested ? items.slice() : [],
        }
      : null;
    aiRemoteMessage.value = t("已发送 {count} 个拉取请求，等待文件接收", { count: requested }) +
      (errors.length ? t("；部分请求失败：{errors}", { errors: errors.map(aiPullErrorText).join("，") }) : "");
    if (aiPullProgress.value && aiPullProgress.value.done + aiPullProgress.value.failed >= aiPullProgress.value.total) {
      finishAiPull();
    }
  } catch (error) {
    if (generation === aiRemoteGeneration) store.setError(error);
  }
}

/** Two entries name the same file: the triple the sidecar identifies it by. */
function sameAiPullItem(left: Record<string, any>, right: Record<string, any>) {
  return String(left.tool || "") === String(right.tool || "") &&
    String(left.root || "") === String(right.root || "") &&
    String(left.rel_path || "") === String(right.rel_path || "");
}
/** Try the failed part of the last pull again.
 *
 * Copy mode writes a new copy and replaces nothing, so it goes straight out.
 * The other two land on files the first attempt may already have touched, so
 * they ask again — the same question the first attempt asked. */
function retryAiPull() {
  const retry = aiPullRetry.value;
  if (!retry || !retry.items.length) return;
  aiPullRetry.value = null;
  if (retry.mode === "copy") void pullAiRemote(retry.items, "copy");
  else aiPullPending.value = aiPullAbout(retry.items, retry.mode);
}
/** Ends the wait for a pull and says how it went.
 *
 * Both halves are reported, because they are different failures: a file the
 * peer would not send is a refusal, and a file that was sent and did not land
 * is a write this machine could not do.  The message names which. */
function finishAiPull() {  const pull = aiPullProgress.value;
  if (!pull) return;
  aiPullProgress.value = null;
  if (pull.failed) {
    aiPullOutcome.value = "failed";
    aiRemoteMessage.value = t("拉取完成：{ok} 个成功、{failed} 个失败", { ok: pull.done, failed: pull.failed }) +
      // The reasons, not just the count: without them the same files fail on
      // every retry and nothing on screen says what would have to change.
      (pull.failures.length ? t("：{reasons}", { reasons: pull.failures.join("；") }) : "");
    // The part worth trying again, kept only while the failure is the last
    // thing that happened.
    aiPullRetry.value = pull.failedItems.length ? { items: pull.failedItems.slice(), mode: pull.mode } : null;
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
    // And keep the entry itself, so the retry knows what it is retrying —
    // matched by its path, because that is the one field the event carries.
    for (const item of pull.items) {
      if (String(item.rel_path || "") !== String(data.rel_path || "")) continue;
      if (!pull.failedItems.some(entry => sameAiPullItem(entry, item))) pull.failedItems.push(item);
    }
  } else pull.done += 1;
  if (pull.done + pull.failed >= pull.total) finishAiPull();
});

async function openAiLocal(item: Record<string, any>) {
  try { await bridge.aiLocal("open", String(item.tool || ""), String(item.root || ""), String(item.rel_path || "")); }
  catch (error) { store.setError(error); }
}
async function readAiLocal(item: Record<string, any>) {
  if (aiMutationBusy.value) return;
  const generation = ++aiReadGeneration;
  aiSelectedItem.value = null;
  aiEditorContent.value = "";
  aiEditorError.value = "";
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
    if (generation === aiReadGeneration) store.setError(error);
  }
}
async function saveAiLocal() {
  const item = aiSelectedItem.value;
  if (!item || aiMutationBusy.value) return;
  const content = aiEditorContent.value;
  if (aiEditorIsJson(item)) {
    const problem = aiJsonProblem(content);
    if (problem) { aiMessage.value = problem; announce(problem); return; }
  }
  aiMutationBusy.value = true;
  try {
    await bridge.aiLocal("save", String(item.tool), String(item.root || ""), String(item.rel_path), content);
    aiSavedContent.value = content;
    aiEditorError.value = "";
    aiMessage.value = t("配置已保存");
    await refreshAiLocal();
  } catch (error) {
    // The sidecar says what it refused and where; keep that line beside the
    // editor, because the error band scrolls away from the file it is about.
    const message = (error as { message?: string } | null)?.message;
    aiEditorError.value = message ? String(message) : t("保存失败");
    store.setError(error);
  }
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
  } catch (error) { store.setError(error); }
  finally { aiMutationBusy.value = false; }
}

/* ── The seams the window keeps ───────────────────────────────────────── */

/**
 * The way into the page, called by the window's own `openAiPage`.
 *
 * The page stays mounted (the window shows and hides it), so a visit is not a
 * mount: the two reads a visit makes -- the form and the picker's cached counts
 * -- are asked for here.  The inventory is deliberately not read: it walks every
 * configured tool's directories, and that is the card's button.
 */
function enter() {
  void loadAiProfiles();
  void primeAiInventories();
}

/**
 * Ctrl+S from the window's global chord handler.
 *
 * Returns whether the chord was taken, because the handler has to decide
 * `preventDefault` before it awaits anything.  The three conditions are the ones
 * the handler used to spell out here: a save is owed only when the form is
 * loaded, dirty and not already saving -- otherwise the chord belongs to
 * whatever else wants it.
 */
function saveIfDirty(): boolean {
  if (!aiProfilesDirty.value || aiProfilesBusy.value || !aiProfilesLoaded.value) return false;
  void saveAiProfiles();
  return true;
}

/**
 * Cancel whatever this page still has in flight.
 *
 * The window used to bump these three counters in its own `onUnmounted`, when
 * the page was a template block and its reads were the window's.  They live
 * here now, so the bump does too: a read that lands after the page is gone must
 * not write into it.
 */
onUnmounted(() => {
  ++aiInventoryGeneration;
  ++aiReadGeneration;
  ++aiRemoteGeneration;
});

defineExpose({ enter, saveIfDirty });
</script>

<template>
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
  <section class="ai-page">
          <!-- Three questions, one at a time: which tools this machine reads
               and writes, what is in the directories they keep, and the same
               second question asked of a paired device.  They were three cards
               in one scroll, so the peer's list — the half the page exists for,
               since it is the one with 拉取 on it — sat below everything this
               machine has. -->
          <div class="page-tabs" role="tablist" :aria-label="t('AI 配置分区')">
            <button v-for="section in aiSections" :key="section.id" type="button" role="tab"
              :id="`ai-tab-${section.id}`" :data-tab-id="section.id"
              :aria-selected="aiTab === section.id" :aria-controls="`ai-panel-${section.id}`"
              :tabindex="aiTab === section.id ? 0 : -1"
              :class="{ 'page-tab--active': aiTab === section.id }"
              @click="aiTab = section.id"
              @keydown="tabKeydown($event, AI_SECTIONS, aiTab, (id) => { aiTab = id as AiSection })">{{ section.label }}</button>
          </div>

          <div v-if="aiTab === 'tools'" class="page-col" role="tabpanel" id="ai-panel-tools" aria-labelledby="ai-tab-tools">
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
                  <!-- What ticking it means.  A checkbox that only names a tool
                       asks the reader to know which directories that tool keeps
                       — and a path they do not have, or do not want shared, is
                       a reason not to tick it that nothing here was showing. -->
                  <small v-if="tool.paths.length" class="ai-tool-paths">{{ tool.paths.join("、") }}</small>
                </label>
                <p v-if="!aiProfilesLoaded" class="note setting-block setting-block--card">{{ t("正在读取本机可用的 AI 工具…") }}</p>
                <p v-else-if="!aiTools.length" class="note setting-block setting-block--card">{{ t("本机没有可同步的 AI 工具。") }}</p>
                <label class="setting">
                  <span class="setting-name">{{ t("自定义配置路径") }}</span>
                  <span class="setting-control"><textarea v-model="aiCustomPaths" rows="3" :placeholder="t('每行一个路径')" @input="aiCustomPathNote = ''"></textarea></span>
                </label>
                <p class="note setting-note">{{ t("勾选要同步的 AI 工具；工具不认识的目录写在下面，每行一个路径。") }}</p>
                <!-- A path is a thing to pick, not a thing to type: the picker
                     is the same folder chooser the rest of the window uses, and
                     what it returns is appended as one more line. -->
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="aiProfilesBusy" @click="browseAiCustomPath"><FolderOpen :size="16" />{{ t("浏览目录…") }}</button>
                  <span v-if="aiCustomPathNote" class="note" role="status">{{ aiCustomPathNote }}</span>
                  <span v-else class="note">{{ t("选一个目录加进上面的列表，不用手打路径。") }}</span>
                </div>
                <!-- What ticking does and what it does not.  The card is a form
                     with a save button and the word 同步 all over it, so it
                     reads as the switch that keeps two machines' config in
                     step — it is not: nothing here moves a file by itself, and
                     a reader who saved and waited was waiting for a pull they
                     had not been told to make. -->
                <p class="note setting-note">{{ t("这里只决定哪些文件参与：配置不会自动同步。要拿到另一台电脑上的配置，去「其他设备」对比后拉取。") }}</p>
                <!-- What the last walk found, for the paths above.  A directory
                     that is not there collects nothing, which looks exactly
                     like a directory that is empty — and only one of those is
                     a path the reader can go and fix. -->
                <p v-if="aiRootIssues.length" class="note setting-note ai-root-issue" role="status">{{ t("{count} 个路径没读到内容：{list}", { count: aiRootIssues.length, list: aiRootIssues.map(issue => `${issue.path}（${issue.reason}）`).join("、") }) }}</p>
                <!-- Kept in the box, dropped from the walk: a path that is one
                     of a ticked tool's own directories is already synced as
                     that tool, and walking it again would advertise every file
                     under it twice. -->
                <p v-if="aiCoveredPaths.length" class="note setting-note" role="status">{{ t("这些路径已由勾选的工具包含，不会重复同步：{list}", { list: aiCoveredPaths.join("、") }) }}</p>
                <div class="setting-actions setting-actions--card">
                  <button type="button" class="primary" :disabled="aiProfilesBusy || !aiProfilesLoaded" @click="saveAiProfiles"><Save :size="16" />{{ t("保存 AI 配置") }}</button>
                  <span v-if="aiProfilesDirty" class="settings-save-status" role="status">{{ t("有未保存的更改") }}</span>
                  <span v-else-if="aiProfilesLoaded" class="settings-save-status" role="status">{{ t("所有更改都已保存") }}</span>
                  <!-- Saving walks, and what the walk found is on the next tab —
                       so the button that changed everything is also the one
                       that says where the answer went, with the count so the
                       reader knows whether to go. -->
                  <button v-if="aiLocalRoots.length" type="button" @click="aiTab = 'local'">{{ t("查看本机配置（{count}）", { count: aiItemCount(aiLocalItems) }) }}</button>
                </div>
              </section>
          </div>

          <div v-else-if="aiTab === 'local'" class="page-col" role="tabpanel" id="ai-panel-local" aria-labelledby="ai-tab-local">
              <section class="settings-section">
                <h2>{{ t("本机配置") }}</h2>
                <!-- The inventory this machine's own tools keep.  It is walked
                     by the button rather than on the way in: the walk reads
                     every configured directory on disk, which is not a status
                     to fetch quietly behind a page the reader may only be
                     passing through, and the line under the button is what the
                     last walk found. -->
                <div class="setting-actions setting-actions--card">
                  <button type="button" :disabled="aiLocalBusy" @click="refreshAiLocal"><RefreshCw :size="16" :class="{ spinning: aiLocalBusy }" />{{ t("读取本机配置") }}</button>
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
                <!-- A walk that came back with nothing, which is not the same
                     thing as no walk having happened — and it is the answer
                     most machines give the first time, because a tool writes
                     its config when it is first used, not when it is
                     installed.  `roots` is what says a walk happened: it is
                     filled by the walk, and only by the walk. -->
                <p v-else-if="aiLocalRoots.length" class="note setting-block">{{ t("本机还没有可同步的配置项：勾选的工具可能尚未安装，或还没有生成配置文件。") }}</p>
                <nav v-if="aiLocalPageCount > 1" :aria-label="t('本地配置分页')" class="pagination">
                  <button type="button" class="icon-button" :aria-label="t('上一页本地配置')" :title="t('上一页')" :disabled="aiLocalPage === 0" @click="aiLocalPage--"><ChevronLeft :size="16" /></button>
                  <span>{{ aiLocalPage + 1 }} / {{ aiLocalPageCount }}</span>
                  <button type="button" class="icon-button" :aria-label="t('下一页本地配置')" :title="t('下一页')" :disabled="aiLocalPage + 1 >= aiLocalPageCount" @click="aiLocalPage++"><ChevronRight :size="16" /></button>
                </nav>
                <div v-if="aiSelectedItem" class="ai-editor">
                  <!-- The file's name and the way out of it on one line: the
                       editor covers the card below the list, and a reader who
                       opened a file to look at it has to be able to put it
                       down again. -->
                  <div class="ai-editor__head">
                    <strong>{{ aiSelectedItem.rel_path }}</strong>
                    <span v-if="aiEditorDirty" class="muted">{{ t("未保存") }}</span>
                    <button type="button" class="icon-button icon-button--sm" :aria-label="t('关闭编辑器')" :title="t('关闭')" @click="requestAiClose"><X :size="15" /></button>
                  </div>
                  <textarea id="ai-editor-text" v-model="aiEditorContent" :disabled="aiMutationBusy" rows="8" maxlength="262144" :aria-label="t('AI 配置编辑器')" :aria-invalid="aiEditorProblem ? 'true' : 'false'" :aria-describedby="aiEditorProblem ? 'ai-editor-hint ai-editor-error' : 'ai-editor-hint'"></textarea>
                  <!-- Said before the save rather than after it: a JSON file is
                       checked here, a TOML or YAML one is checked by the tool
                       that loads it. -->
                  <p id="ai-editor-hint" class="note ai-editor__hint">{{ aiEditorIsJson(aiSelectedItem) ? t("这是 JSON 文件：保存前会先校验格式，不成立就不写入。") : t("保存后由对应的工具读取；它拒绝时会在这里说明是哪一行。") }}</p>
                  <p v-if="aiEditorProblem" id="ai-editor-error" class="field-error" role="alert">{{ aiEditorProblem }}</p>
                  <div class="ai-editor__actions">
                    <button type="button" class="primary" :disabled="aiMutationBusy" @click="saveAiLocal"><Save :size="16" />{{ t("保存配置") }}</button>
                  </div>
                </div>
              </section>
          </div>

          <div v-else class="page-col" role="tabpanel" id="ai-panel-peer" aria-labelledby="ai-tab-peer">
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
                    <option v-for="device in state.devices.filter(device => device.paired && !device.relay)" :key="device.id" :value="device.id">{{ deviceLabel(device) }}{{ aiPeerDiffSuffix(device.id) }}</option>
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
                      <option value="copy">{{ t("创建副本（原有的另存为副本）") }}</option>
                      <option value="overwrite">{{ t("覆盖本机配置（用对方版本替换）") }}</option>
                      <option value="append">{{ t("追加到本机配置（接在文件末尾）") }}</option>
                    </select></span>
                  </label>
                  <!-- What the chosen mode will actually do to this machine's
                       files, said before the pull rather than after it.  The
                       three names were four characters each and the choice is
                       remembered, so a pull could arrive in 覆盖 without the
                       reader ever having been told what 覆盖 means.  Amber, not
                       red: it is a note about a mode, not a failure. -->
                  <p class="note setting-block ai-pull-mode" :class="{ 'ai-pull-mode--overwrite': aiPullMode === 'overwrite' }" role="status">{{ aiPullModeNote }}</p>
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
                    <!-- One more try without selecting the same rows again: what
                         failed is still known, and it is the only part worth
                         re-asking for. -->
                    <button v-if="aiPullRetry" type="button" class="ai-pull-retry" @click="retryAiPull"><RotateCcw :size="14" />{{ t("重试失败的 {count} 项", { count: aiPullRetry.items.length }) }}</button>
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
                  <!-- A file the reader looked at before deciding to pull it.
                       Its own block rather than the status line above: what
                       arrives is up to 64 KiB of somebody's configuration, and
                       that line is the card's own report — the remote count,
                       then how a pull ended.  It sits below the pager so the
                       list and its pages stay one thing, and so a panel that
                       comes and goes does not push the pager out from under
                       the reader's cursor. -->
                  <nav v-if="aiRemotePageCount > 1" :aria-label="t('远程配置分页')" class="pagination">
                    <button type="button" class="icon-button" :aria-label="t('上一页远程配置')" :title="t('上一页')" :disabled="aiRemotePage === 0" @click="aiRemotePage--"><ChevronLeft :size="16" /></button>
                    <span>{{ aiRemotePage + 1 }} / {{ aiRemotePageCount }}</span>
                    <button type="button" class="icon-button" :aria-label="t('下一页远程配置')" :title="t('下一页')" :disabled="aiRemotePage + 1 >= aiRemotePageCount" @click="aiRemotePage++"><ChevronRight :size="16" /></button>
                  </nav>
                  <div v-if="aiPreview" class="ai-preview setting-block" role="group" :aria-label="t('远程配置预览')">
                    <div class="ai-preview__head">
                      <strong>{{ aiPreview.path }}</strong>
                      <span v-if="aiPreview.truncated" class="muted">{{ t("内容已截断") }}</span>
                      <button type="button" class="icon-button icon-button--sm" :aria-label="t('关闭预览')" :title="t('关闭')" @click="aiPreview = null"><X :size="15" /></button>
                    </div>
                    <pre v-if="!aiPreview.loading" class="ai-preview__body">{{ aiPreview.content }}</pre>
                    <p v-else class="muted ai-preview__body"><RefreshCw :size="14" class="spinning" />{{ t("正在读取…") }}</p>
                  </div>
                </div>
                <p v-else class="note setting-block setting-block--card">{{ t("选择一台已配对设备后，可以对比并拉取它的 AI 工具配置。") }}</p>
              </section>
          </div>
        
<dialog ref="aiMigrateDialog" aria-labelledby="ai-migrate-title" class="modal modal--wide" @close="aiMigrateOpen = false" @cancel="aiMigrateOpen = false">

  <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="aiMigrateDialog?.close()"><X :size="18" /></button>

  <h2 id="ai-migrate-title">{{ t("AI 配置迁移向导") }}</h2>

  <p class="note">{{ t("把来源设备的配置与技能带到本机：先对比，再选冲突策略，最后一次拉取。") }}</p>

  <label class="setting">

    <span class="setting-name">{{ t("来源设备") }}</span>

    <span class="setting-control"><select v-model="aiPeerId" :aria-label="t('迁移来源设备')">

      <option value="">{{ t("选择已配对设备") }}</option>

      <option v-for="device in state.devices.filter(device => device.paired && !device.relay)" :key="device.id" :value="device.id">{{ deviceLabel(device) }}{{ aiPeerIsLegacy(device.id) ? t("（只能浏览）") : "" }}</option>

    </select></span>

  </label>

  <p v-if="!aiPeerId" class="note">{{ t("先选择一台已配对设备") }}</p>

  <template v-else>

    <div v-if="aiRemoteWaiting" class="note">{{ t("已请求更新，等待对方返回库存") }}</div>

    <p v-if="aiMigrateBlocked" class="note setting-note" role="status">{{ t("该设备版本过旧，只能浏览，不能作为迁移来源") }}</p>

    <!-- The strategy, and what choosing it does.  See `aiMigrateStrategies`. -->

    <div class="migrate-strategies" role="radiogroup" :aria-label="t('冲突策略')">

      <label v-for="option in aiMigrateStrategies" :key="option.value" class="migrate-strategy">

        <input v-model="aiMigrateStrategy" type="radio" :value="option.value" :aria-label="option.label" />

        <span class="migrate-strategy__text">

          <strong>{{ option.label }}</strong>

          <span class="note">{{ option.note }}</span>

        </span>

      </label>

    </div>

    <p class="note setting-note">{{ aiMigrateSummary }}</p>

    <!-- What that number is made of.  Scrolls on its own: it is the one

         block here that can be long, and the buttons must stay reachable. -->

    <div v-if="aiMigrateGroups.length" class="migrate-checklist">

      <p class="note">{{ t("将处理这些配置项（{count} 个）：", { count: aiMigrateTargets.length }) }}</p>

      <div v-for="group in aiMigrateGroups" :key="group.tool" class="migrate-group">

        <p class="migrate-group__head">{{ group.tool }}<span class="muted">{{ t("{count} 项 · {files} 个文件", { count: group.targets.length, files: group.files }) }}</span></p>

        <ul><li v-for="target in group.targets" :key="target.key">{{ target.rel_path }}<span v-if="target.isDir" class="muted"> {{ t("（{count} 个文件）", { count: target.items.length }) }}</span></li></ul>

      </div>

    </div>

    <div class="modal-actions">

      <button @click="aiMigrateDialog?.close()">{{ t("取消") }}</button>

      <button class="primary" :disabled="aiMigrateBlocked || !aiMigrateTargets.length" @click="startAiMigration">{{ t("开始迁移（{count}）", { count: aiMigrateTargets.length }) }}</button>

    </div>

  </template>

</dialog>

<AppDialog ref="aiPullDialog" heading-id="ai-pull-title" :title='aiPullPending?.mode === "overwrite" ? t("覆盖本机配置？") : t("追加到本机配置？")' @close="aiPullPending = null" @cancel="aiPullPending = null">

  <p v-if="aiPullPending && aiPullPending.targets.length === 1">{{ aiPullPending.targets[0].rel_path }}<span v-if="aiPullPending.targets[0].isDir" class="muted"> {{ t("（{count} 个文件）", { count: aiPullPending.targets[0].items.length }) }}</span></p>

  <template v-else-if="aiPullPending">

    <p>{{ t("将写入以下 {count} 个配置项：", { count: aiPullPending.targets.length }) }}</p>

    <ul class="ai-local-list"><li v-for="target in aiPullPending.targets" :key="target.key"><span>{{ target.tool }} / {{ target.rel_path }}</span><span v-if="target.isDir" class="muted"> {{ t("（{count} 个文件）", { count: target.items.length }) }}</span></li></ul>

  </template>

  <template #actions>

    <button autofocus @click="aiPullPending = null">{{ t("取消") }}</button><button class="danger" :disabled="!aiPullPending" @click="confirmAiPull">{{ t("确认拉取") }}</button></template>

</AppDialog>

<AppDialog ref="aiDiscardDialog" heading-id="ai-discard-title" :title="t('放弃未保存的修改？')" @close="aiDiscardTarget = null" @cancel="aiDiscardTarget = null">

  <p>{{ aiSelectedItem?.rel_path }}</p>

  <template #actions>

    <button autofocus @click="aiDiscardTarget = null">{{ t("继续编辑") }}</button><button class="danger" :disabled="!aiDiscardTarget || aiMutationBusy" @click="discardAiChanges">{{ aiDiscardTarget?.item ? t("放弃修改并切换") : t("放弃修改并关闭") }}</button></template>

</AppDialog>

<AppDialog ref="aiTrashDialog" heading-id="ai-trash-title" :title="t('移入回收区？')" @close="aiTrashItem = null" @cancel="aiTrashItem = null">

  <p>{{ aiTrashItem?.rel_path }}</p>

  <template #actions>

    <button autofocus :disabled="aiMutationBusy" @click="aiTrashItem = null">{{ t("取消") }}</button><button class="danger" :disabled="aiMutationBusy || !aiTrashItem" @click="confirmAiTrash">{{ t("移入回收区") }}</button></template>

</AppDialog>
  </section>
</template>
