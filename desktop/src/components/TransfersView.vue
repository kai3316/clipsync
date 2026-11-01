<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { Eraser, ExternalLink, FileDown, FileUp, FolderOpen, FolderUp, Info, RefreshCw, Pause, Play, X, Check, Trash2, Copy, Link, ChevronDown, ChevronUp } from "@lucide/vue";
import { bridge } from "../api/bridge";
import { currentLocale, t } from "../i18n";
import { openContextMenu } from "../lib/context-menu";
import { fileReachable, deviceLabel } from "../lib/device-row";
import { copyText } from "../lib/clipboard";
import { dateTime, size as formatSize, speed as formatSpeed } from "../i18n/format";
import type { Device, Transfer } from "../api/types";
import EmptyState from "./EmptyState.vue";

/** Why a finished transfer did not succeed, in the user's language.
 *
 * The row's `reason` is the transfer manager's own status string
 * (`error_disk`, `rejected`, …) whenever the bucket is a generic
 * completed/failed.  The panel kept the same map in `_transferReasonLabelKey`
 * and listed a key for `error_internal` that its catalog did not have, so that
 * one reason rendered as the raw key; here an unrecognized reason falls back to
 * the generic failure label instead — the fallback is worth more than naming a
 * cause the code cannot translate.
 */
function reasonLabel(reason: string): string {
  switch (reason) {
    case "cancelled": return t("传输已取消");
    case "rejected": return t("对方已拒绝接收");
    case "peer_offline": return t("对方设备已离线");
    case "timeout":
    case "error_timeout": return t("传输超时");
    case "error_disk": return t("接收设备磁盘空间不足");
    case "error_size_mismatch": return t("文件大小校验失败，传输可能已损坏");
    case "error_missing_chunks": return t("传输数据不完整");
    case "error_security": return t("传输因安全原因被拒绝");
    default: return t("传输失败");
  }
}

/** A finished transfer's status, as the panel's history rows showed it. */function historyStatus(item: Transfer): string {
  if (item.status === "completed") return t("已完成");
  return reasonLabel(item.reason || item.status || "");
}

/** A running transfer's status — only the states that are not self-evident.
 *
 * While a transfer is simply sending or receiving, the row's direction and
 * percentage already say everything the panel's `transferActiveStatusLabel`
 * said with them.
 */
function activeStatus(item: Transfer): string {
  if (item.status === "paused") return t("已暂停");
  if (item.status === "finalizing") return t("正在写入对方设备…");
  if (item.status === "awaiting_ack") return t("等待对方接受…");
  return "";
}

/** Whether a pending inbound row is arriving over the internet relay.
 *
 * The sidecar reads the route off the offer's own chunk size (a send bound for
 * the relay is cut to one broker message), so this is a fact about the transfer
 * rather than something the page works out.  It belongs on the pending row and
 * nowhere else: the file has not been taken while the row is pending, and the
 * accept/reject pair beside the sentence is the prompt that answers it.
 */
function relayArrival(item: Transfer): boolean {
  return item.direction === "down" && item.status === "pending" && item.relay === true;
}

/** The peers a file can be sent to: any device reachable over either route.
 *
 * Pairing is not on the list.  A file is the same kind of act a chat message is
 * — the sidecar takes one from an unpaired device on this network and prompts
 * for it only when the user has turned that off — so requiring a pairing here
 * made the same two devices able to talk and unable to send each other a file.
 * Same rule the send-URL dialog picks from (``App.vue::openSendUrlFromHost``),
 * and the same one the legacy transfers page applied: it listed connected
 * devices only, and refused to start an upload with no target chosen
 * (``transfer.select_target``) rather than sending to everyone.
 *
 * An internet-paired device is on the list too, through `relay_paired &&
 * relay_online`: the sidecar cuts the chunks to a broker message and the
 * receiving half accepts them, so the send works — it is only slower.  The page
 * used to leave those devices out and say so (`relayOnlyTargets`); with the
 * route supported, the honest thing is to offer the target and warn when it is
 * the selected one.
 */
const props = defineProps<{
  devices?: Device[];
  dropped?: string[];
  /** A device the window was told to send to, from its row's 发送文件….
   *
   * The page does not preselect on its own: a file is the one thing here the
   * user cannot take back, so the machine is named rather than assumed.  This is
   * the case where they named it — in the row's menu, before the file — and
   * arriving on the page only to be asked which device again is the question
   * they already answered.  An id that is not reachable now is ignored, which
   * leaves the picker empty and its own note to explain why. */
  preselect?: string;
  /** Say that a click did something, for the clicks this page cannot show.
   * The window owns the notice stack and hands one down; a page mounted
   * without it — a unit test — simply has nothing to report to. */
  notify?: (message: string) => void;
  /** The zip being built for a folder or multi-file send, as the sidecar
   * reports it.  The window mirrors it off the event stream because there is no
   * transfer to carry it yet: `sendFiles` does not answer until the archive is
   * whole.  Absent, null and `done` all mean "nothing is being packed". */
  packing?: {
    label: string;
    files_done: number;
    files_total: number;
    bytes_done: number;
    bytes_total: number;
    done: boolean;
  } | null;
}>();
const emit = defineEmits<{ dropped: [] }>();

/** The machine on the other end of a finished transfer, by the name this window
 *  keeps for it.
 *
 *  A history row said what was moved and when, and nothing about where it went
 *  or came from — so a list holding records from three machines read as one
 *  machine's, and a failed row gave no clue which peer had refused.  The id is
 *  what the sidecar stores; the name is the one the picker above the list would
 *  have shown for the same device (see `deviceLabel`).  A record whose peer has
 *  since been forgotten gets nothing rather than eight characters of hex: the
 *  row is not missing information, that machine is no longer on this one.
 *  See U135. */
function peerLabel(item: Transfer): string {
  const id = String(item.peer_id || "");
  if (!id) return "";
  const known = (props.devices || []).find((device) => device.id === id);
  return known ? deviceLabel(known) : "";
}

// Nothing is preselected: a file is the one thing here the user cannot take
// back, so the machine it goes to is named rather than assumed.
const target = ref("");
const targets = computed(() =>
  (props.devices || []).filter(fileReachable));
/** Whether the selected target can only be reached over the relay.
 *
 * Its `connection_state` is the local link's, so an online value means the
 * direct route and anything else means the relay is what the send will use.
 * The sentence appears with the selection, not as a permanent note: it is about
 * the file the reader is about to send, not about the page. */
const selectedTarget = computed(() =>
  targets.value.find((device) => device.id === target.value));
const selectedOverRelay = computed(() =>
  !!selectedTarget.value && selectedTarget.value.connection_state !== "online");
// The row's 发送文件…, applied once per arrival: the value only changes when
// another device is named, so staying on the page does not re-target anything.
watch(() => props.preselect, (id) => {
  if (!id) return;
  if (targets.value.some((device) => device.id === id)) target.value = id;
}, { immediate: true });
/** Why the picker is empty again, when the reader did fill it in.
 *
 * A device that stops being reachable takes its own option out of the list, and
 * a `<select>` whose value matches no option draws nothing at all — so the
 * picker read 选择接收设备 while the page still held a machine, and 发送文件
 * stayed enabled over a target the reader could no longer see and had not
 * chosen for the file they are about to pick.  The machine is named rather than
 * dropped silently: a picker that empties itself with no word reads as one that
 * was never filled in. */
const droppedTarget = ref("");
/* ── A failed record's own account of itself ──────────────────────────────
 *
 * A failed row said what went wrong in its badge and offered 重试, and nothing
 * else: no peer, no time of its own, no path — the four things anyone asking
 * "why did this not arrive?" needs in order to ask a second person.  The block
 * below is that answer, on a toggle rather than always: four lines per failed
 * row would drown the list, and most rows nobody ever asks about.
 * See U138.
 */
const detailsId = ref("");
function toggleDetails(item: Transfer) {
  detailsId.value = detailsId.value === item.id ? "" : item.id;
}
/** Which machine this record names, by the list's own name for it. */
function peerName(item: Transfer) {
  const device = (props.devices || []).find((entry) => entry.id === item.peer_id);
  return device ? deviceLabel(device) : (item.peer_id || t("未知设备"));
}
/** The block, as one piece of text — the same lines on screen and on the
 *  clipboard, so a report pasted into a message cannot disagree with the one the
 *  reader was looking at. */
function detailLines(item: Transfer) {
  return [
    `${t("文件")}：${item.filename || t("未知文件")}`,
    `${t("方向")}：${item.direction === "up" ? t("发送") : t("接收")}`,
    `${t("对端")}：${peerName(item)}`,
    `${t("大小")}：${formatSize(item.size || 0)}`,
    `${t("时间")}：${dateTime(item.timestamp || 0)}`,
    `${t("状态")}：${historyStatus(item)}`,
    reasonLabel(item.reason || "") ? `${t("原因")}：${reasonLabel(item.reason || "")}` : "",
    item.path ? `${t("路径")}：${item.path}` : "",
  ].filter(Boolean);
}
watch(targets, (list) => {
  // A device list nobody has read is not evidence: a target held across a read
  // that has not answered yet is one the reader still chose, and letting it go
  // on the strength of an empty list would be worse than leaving it standing.
  if (!target.value || !(props.devices || []).length) return;
  if (list.some((device) => device.id === target.value)) return;
  const gone = (props.devices || []).find((device) => device.id === target.value);
  target.value = "";
  droppedTarget.value = gone ? deviceLabel(gone) : "";
});
// Choosing again is the answer to the note, so the note goes with it.
watch(target, (value) => { if (value) droppedTarget.value = ""; });
/** Why the picker has nothing in it, when it has nothing in it. */
const targetPlaceholder = computed(() => {
  if (targets.value.length) return t("选择接收设备");
  return (props.devices || []).length ? t("没有可用设备") : t("没有已连接的设备");
});

const active = ref<Transfer[]>([]);
/** The finished transfers, as the panel's history card listed them. */
const history = ref<Transfer[]>([]);
/** How many records the card draws before it offers the rest.
 *
 *  `transfers.list` answers with every record there has ever been, and the list
 *  only grows: a machine that has moved a few hundred files renders all of them
 *  on every tick, and the row a reader wants is the one they remember rather
 *  than the one at the bottom.  Fifty is more than a screenful, so the cut is
 *  not visible on a machine that has just started — and the line under the list
 *  is what says how many are held back.  No interface change: the fold is the
 *  front end's, so 清除传输历史 still clears everything it counts.  See U134. */
const HISTORY_PAGE = 50;
const historyExpanded = ref(false);
const historyShown = computed(() => (
  historyExpanded.value ? history.value : history.value.slice(0, HISTORY_PAGE)
));
const historyHeldBack = computed(() => Math.max(0, history.value.length - HISTORY_PAGE));
/** The line the panel put under that card's own header: how many records there
 *  are, how they ended, and how much has moved through.
 *
 *  Each part is left out when it has nothing to say, which is the panel's own
 *  rule (`dashboard.py::_refresh_transfers`) — a list with no failures does not
 *  report zero failures.  A record counts as a success by the same test the row
 *  itself uses (:func:`historyStatus`), so the line and the rows under it cannot
 *  disagree about what happened. */
const historyStats = computed(() => {
  const records = history.value;
  if (!records.length) return "";
  const succeeded = records.filter((item) => item.status === "completed").length;
  const parts = [t("已完成 {count} 个", { count: records.length })];
  if (succeeded) parts.push(t("{count} 成功", { count: succeeded }));
  if (records.length - succeeded) parts.push(t("{count} 失败", { count: records.length - succeeded }));
  const bytes = records.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
  if (bytes > 0) parts.push(formatSize(bytes));
  return parts.join(" · ");
});
const busy = ref(false);
const error = ref("");
const refreshError = ref("");
/** The one line the page reports, out of the two it can fail in.
 *
 * A failed action and a failed poll are different things, and they were drawn
 * as two bands one above the other — the reader saw the same refusal twice in
 * two shapes, and a screen reader announced both.  The newest one wins and the
 * line says which it was, because what the reader has to do about it differs:
 * a refused action is something they just did, a failed poll is the page
 * running out of touch with the runtime. */
const errorAt = ref(0);
const refreshErrorAt = ref(0);
watch(error, (value) => { if (value) errorAt.value = Date.now(); });
watch(refreshError, (value) => { if (value) refreshErrorAt.value = Date.now(); });
const errorLine = computed(() => {
  const action = error.value ? { at: errorAt.value, label: t("操作失败"), message: error.value } : null;
  const poll = refreshError.value
    ? { at: refreshErrorAt.value, label: t("刷新失败"), message: refreshError.value }
    : null;
  if (action && poll) return action.at >= poll.at ? action : poll;
  return action || poll;
});
const speed = ref<Record<string, any>>({});
/** The finished speed test's reading, or the failure it actually was.
 *
 *  The panel drew three outcomes out of one result, not two
 *  (`dashboard.py::_refresh_transfers`): while the chunks are going it counted
 *  them, a measurement above zero was the number **plus a word** for how good
 *  that is, and anything else was the failure it says.  The last one matters
 *  here: this manager ends every run at `done`, a run whose peer never echoed
 *  included, and reports 0 MB/s for it — so a zero is not a reading, and
 *  printing it as one would be the one thing the panel refused to do.
 *
 *  The thresholds are the panel's own: above 10 MB/s fast, above 2 good, below
 *  that slow. */
const speedReading = computed<{ mbps: number | null; label: string; tone: string } | null>(() => {
  const state = String(speed.value.state || "");
  if (state !== "done" && state !== "acknowledged") return null;
  const mbps = Number(speed.value.result_mbps) || 0;
  if (mbps <= 0) return { mbps: null, label: "", tone: "" };
  if (mbps > 10) return { mbps, label: t("快速"), tone: "fast" };
  if (mbps > 2) return { mbps, label: t("良好"), tone: "good" };
  return { mbps, label: t("慢"), tone: "slow" };
});
/** What the last bulk action did, reported rather than assumed: "cancel all"
 * counts what it actually cancelled, and clearing history counts what it
 * actually deleted.  One line for both, because it is one question — what did
 * that button just do — and only one of them can have been the last. */
const bulkMessage = ref("");
/** The archive the sidecar is building, if one is being built.
 *
 * A folder or a multi-file pick is zipped before a transfer exists, and the
 * page could only say 正在打包 and wait — which on a few gigabytes is
 * indistinguishable from a hang.  The sidecar reports the totals before it
 * starts and how many members are in (see `_PackProgress`), and the window
 * hands them down; this is the line for them.  Nothing here is optimistic: the
 * numbers are the archive's own, and `done` is what takes the line away.
 */
const packingActive = computed(() => {
  const pack = props.packing;
  return !!pack && !pack.done && pack.files_total > 0;
});
const packingTotal = computed(() => (packingActive.value ? props.packing!.files_total : 0));
const packingDone = computed(() =>
  Math.min(Math.max(0, props.packing?.files_done || 0), packingTotal.value));
const packingPercent = computed(() =>
  (packingTotal.value ? Math.round((packingDone.value / packingTotal.value) * 100) : 0) + "%");
const packingLine = computed(() => {
  if (!packingActive.value) return "";
  // Before the first member is in, the useful sentence is what the whole job is
  // — count and size — because that is what says whether to wait or go make
  // coffee.  After that it is the count, the same "N/M" the doc asks for.
  if (!packingDone.value) {
    return t("正在打包 {count} 个文件 · 共 {size}", {
      count: packingTotal.value,
      size: formatSize(props.packing?.bytes_total || 0),
    });
  }
  return t("已打包 {done}/{total}", { done: packingDone.value, total: packingTotal.value });
});
/** Whether the clear-history confirm is open.  History is a record the user may
 * still want, and the legacy panel asked before clearing it (`ask_yesno` with
 * its own `transfers.clear_title` / `transfers.clear_confirm`), so this asks
 * too — with a dialog rather than the native message box, like every other
 * destructive action in this window. */
const clearOpen = ref(false);
const clearDialog = ref<HTMLDialogElement | null>(null);
/** Files dragged onto the window, held until the reader names a machine.
 *
 * A drop answers the file picker and nothing else: it says what to send, not
 * where, and the machine is the one step in a send that cannot be taken back —
 * so a drop lands here, on the same picker-and-target pair the button uses,
 * rather than going out on its own.  The paths come from the window
 * (``App.vue``), which clears them as soon as they are here, so coming back to
 * this page does not re-stage a drop that has already been answered.
 */
const staged = ref<string[]>([]);
/** What the file is called, which is the last segment of a path on either
 * separator — the drop hands us the OS's own spelling. */
function baseName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}
// `immediate`, because the first visit is the common one: the window stages the
// paths and opens this page in the same breath, so the drop is already on the
// prop by the time this component exists.
watch(() => props.dropped, (paths) => {
  if (!paths?.length) return;
  staged.value = [...paths];
  bulkMessage.value = "";
  error.value = "";
  emit("dropped");
}, { immediate: true });
/** Send the dropped files the way the button sends picked ones: one call, and
 * the sidecar puts several paths in one archive rather than sending N times. */
async function sendStaged() {
  if (disposed || busy.value || !target.value || !staged.value.length) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try {
    if (staged.value.length > 1) bulkMessage.value = t("正在打包 {count} 个文件…", { count: staged.value.length });
    await bridge.sendFiles(staged.value, target.value);
    staged.value = [];
    bulkMessage.value = "";
    await refresh();
  } catch (reason: any) { error.value = reason?.message || t("文件发送失败"); }
  finally { busy.value = false; }
}
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
let refreshGeneration = 0;

async function refresh() {
  if (disposed) return;
  const generation = ++refreshGeneration;
  try {
    const result = await bridge.transfers();
    if (disposed || generation !== refreshGeneration) return;
    active.value = result.active;
    history.value = result.history;
    speed.value = result.speed_test || {};
    refreshError.value = "";
  } catch (reason: any) {
    if (!disposed && generation === refreshGeneration) refreshError.value = reason?.message || t("无法读取传输状态");
  }
}
/** Whether this window is on screen at all.
 *
 * The poll below reads the whole page once a second — the transfers, the
 * history, the speed test — and a window that is minimized or behind another
 * one has nobody looking at any of it.  The overview keeps its own two timers
 * to the same rule and for the same reason; this page was the one that did
 * not.  Focus is not consulted: a window kept open beside the work is usually
 * not the focused one, and that is exactly when it is being watched. */
let awake = true;
function wake() {
  awake = !document.hidden;
  // Coming back is the moment the rows are looked at again, so the page is
  // read then rather than a second later.
  if (awake) void refresh();
}
async function poll() {
  if (disposed) return;
  if (awake && !busy.value) await refresh();
  if (!disposed) timer = setTimeout(poll, 1000);
}
/** The toolbar's refresh, which says what it found.
 *
 * A refresh is the one click allowed to change nothing: the rows come back as
 * they were and the spinner is over before the eye reaches it. The poller runs
 * the same read once a second and must stay silent, which is why the report
 * sits on the button rather than in `refresh`.
 */
async function refreshNow() {
  await refresh();
  props.notify?.(active.value.length
    ? t("已刷新 · {count} 个进行中的传输", { count: active.value.length })
    : t("已刷新 · 暂无进行中的传输"));
}
/** Send what the picker returned — one file, several files, or one folder.
 *
 * The three differ in the picker and in what happens between the pick and the
 * transfer: anything past a single file is archived by the sidecar before
 * anything goes out, and on a large folder that gap is long enough that the
 * page has to say so — the legacy panel said it with a progress dialog
 * (`创建压缩包` / `正在压缩：{name}`) and this says it on the report line the
 * page already has.  A single file is the one pick that is not archived, so it
 * gets no archiving line: there is nothing to pack.
 */
async function chooseAndSend(kind: "file" | "folder") {
  if (disposed || busy.value || !target.value) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try {
    // The file picker is multi-select, the way the legacy 发送文件 dialog was:
    // several picks become one archive and one transfer rather than N sends.
    const picked = kind === "folder"
      ? [await bridge.chooseFolder()]
      : await bridge.chooseFiles();
    const paths = picked.filter((path): path is string => !!path);
    if (!paths.length || disposed) return;
    if (paths.length > 1) bulkMessage.value = t("正在打包 {count} 个文件…", { count: paths.length });
    else if (kind === "folder") bulkMessage.value = t("正在打包文件夹…");
    await bridge.sendFiles(paths, target.value);
    bulkMessage.value = "";
    await refresh();
  }
  catch (reason: any) { error.value = reason?.message || t("文件发送失败"); }
  finally { busy.value = false; }
}
async function action(name: string, id: string) {
  if (disposed || busy.value) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try { await bridge.transferAction(name, id); await refresh(); }
  catch (reason: any) { error.value = reason?.message || t("操作失败"); }
  finally { busy.value = false; }
}
/** The right-click menu on a transfer record.
 *
 * The legacy menu's six entries, each offered under the same condition its
 * button on the row is, so the two can never disagree about what a given row
 * can do.  The two that have no button — 复制文件名 and 复制文件路径 — are the
 * reason this menu is worth having on this page at all: the name of a file that
 * arrived is the thing a reader most often needs somewhere else, and the path
 * is the thing a support conversation always ends up asking for.
 */
function rowMenu(event: MouseEvent, item: Transfer) {
  const openable = item.direction !== "up" && !!item.path;
  openContextMenu(event, [
    openable
      ? { id: "open", label: t("打开文件"), icon: ExternalLink, run: () => action("open", item.id) }
      : null,
    item.path
      ? { id: "reveal", label: t("打开所在文件夹"), icon: FolderOpen, run: () => action("reveal", item.id) }
      : null,
    {
      id: "copy-name", label: t("复制文件名"), icon: Copy, disabled: !item.filename,
      run: () => copyText(item.filename || ""),
    },
    item.path
      ? { id: "copy-path", label: t("复制文件路径"), icon: Link, run: () => copyText(item.path || "") }
      : null,
    retryable(item)
      ? { id: "retry", label: t("重试传输"), icon: RefreshCw, run: () => action("retry", item.id) }
      : null,
    {
      id: "delete", label: t("删除传输记录"), icon: Trash2, divider: true, danger: true,
      run: () => action("delete", item.id),
    },
  ]);
}
/** Whether a finished record can be sent again — the same test the row's own
 * retry button makes, in one place so the two cannot drift apart. */
function retryable(item: Transfer): boolean {
  return item.status !== "completed" && item.direction === "up" && !!item.path;
}
async function cancelAll() {
  if (disposed || busy.value) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try {
    const result = await bridge.cancelAllTransfers();
    await refresh();
    // Reported, not assumed: the sidecar counts what it actually cancelled, and
    // a transfer that finished between the last poll and the click is not one.
    bulkMessage.value = result?.cancelled
      ? t("已取消 {count} 个传输", { count: result.cancelled })
      : t("没有可取消的传输");
  } catch (reason: any) { error.value = reason?.message || t("操作失败"); }
  finally { busy.value = false; }
}
async function clearHistory() {
  if (disposed || busy.value) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try {
    const result = await bridge.clearTransferHistory();
    await refresh();
    // On success the dialog closes and the line outside reports it.  On failure
    // it stays open, so the error is readable beside the button that caused it —
    // the same rule the other dialogs in this window follow.
    clearOpen.value = false;
    bulkMessage.value = result?.cleared
      ? t("已清除 {count} 条传输记录", { count: result.cleared })
      : t("没有可清除的传输记录");
  } catch (reason: any) { error.value = reason?.message || t("操作失败"); }
  finally { busy.value = false; }
}
watch(clearOpen, async (open) => {
  await nextTick();
  if (open) clearDialog.value?.showModal();
  else clearDialog.value?.close();
});
async function speedTest() {
  if (disposed || busy.value) return;
  busy.value = true;
  ++refreshGeneration;
  error.value = "";
  bulkMessage.value = "";
  try { await bridge.startSpeedTest(); await refresh(); }
  catch (reason: any) { error.value = reason?.message || t("测速失败"); }
  finally { busy.value = false; }
}
onMounted(() => {
  document.addEventListener("visibilitychange", wake);
  void poll();
});
onUnmounted(() => {
  document.removeEventListener("visibilitychange", wake);
  disposed = true;
  ++refreshGeneration;
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <section class="transfers-view">
    <div class="toolbar">
      <!-- The target is picked before the file dialog opens: the picker is the
           one step that can be cancelled with nothing lost, and a file chosen
           for a machine that is offline would have to be chosen again. -->
      <select v-model="target" :aria-label="t('发送目标')" :disabled="busy">
        <option value="">{{ targetPlaceholder }}</option>
        <!-- The name this machine keeps for the device, not the one it
             publishes about itself: the picker names the machine a file is
             about to go to, and a reader who renamed that machine here would
             otherwise be picking one they do not recognise — which is the same
             rule the device list and the chat rail name it by. -->
        <option v-for="device in targets" :key="device.id" :value="device.id">{{ deviceLabel(device) }}</option>
      </select>
      <!-- Why these two are greyed out, when they are.  The picker beside them
           names the step in its own empty option, but a disabled button with
           no reason reads as a page that is stuck — and the target is the one
           step here the reader cannot take back, so it is asked first for a
           reason worth saying. -->
      <button class="primary" :disabled="busy || !target" :title="!target ? t('先选择接收设备') : undefined" @click="chooseAndSend('file')"><FileUp :size="17" />{{ t("发送文件") }}</button>
      <!-- The panel's second send button.  A folder leaves as one archive, and
           so do several files picked at once, so neither is a second transfer
           path — the sidecar archives and the same send carries it. -->
      <button :disabled="busy || !target" :title="!target ? t('先选择接收设备') : undefined" @click="chooseAndSend('folder')"><FolderUp :size="17" />{{ t("发送文件夹") }}</button>
      <button class="icon-button" :title="t('刷新')" :aria-label="t('刷新传输')" :disabled="busy" @click="refreshNow"><RefreshCw :size="18" /></button>
      <button :disabled="busy || speed.state === 'sending'" @click="speedTest">{{ t("速度测试") }}</button>
    </div>
    <!-- What the selected route costs.  An internet-paired device is a real
         target now — the sidecar cuts the chunks to a broker message and the
         receiver accepts them — but it is slower than the same file over the
         local network, and that is worth saying before the file is picked
         rather than after the transfer starts. -->
    <p v-if="selectedOverRelay" class="note transfers-relay-note" role="status">
      {{ t("经互联网中继传输，速度较慢。") }}
    </p>
    <p v-if="droppedTarget" class="note" role="status">
      {{ t("{name} 已无法接收文件，请重新选择接收设备。", { name: droppedTarget }) }}
    </p>
    <!-- A drop answers the picker, not the target: the files it brought are
         shown with the one question it left open — which machine — and nothing
         goes out until that is answered.  The names are the OS's own paths' last
         segments, because that is what the reader recognises. -->
    <div v-if="staged.length" class="staged-drop strip" role="status">
      <FileDown :size="17" />
      <div class="staged-drop-main">
        <strong>{{ t("已拖入 {count} 个文件", { count: staged.length }) }}</strong>
        <span class="note staged-drop-names" :title="staged.join('\n')">{{ staged.map(baseName).join(" · ") }}</span>
      </div>
      <button class="icon-button" :disabled="busy" :aria-label="t('清除')" :title="t('清除')" @click="staged = []"><X :size="16" /></button>
      <button class="primary" :disabled="busy || !target" :title="!target ? t('先选择接收设备') : undefined" @click="sendStaged"><FileUp :size="17" />{{ t("发送文件") }}</button>
    </div>
    <!-- What is being packed right now, from the sidecar's own numbers.  A
         progressbar rather than a `role="status"`: the value changes several
         times a second and a live region would read every one of them out.  The
         bar carries the same reading the text does, so neither is the only
         place it exists. -->
    <div v-if="packingLine" class="packing-status" role="progressbar"
      :aria-valuemin="0" :aria-valuemax="packingTotal" :aria-valuenow="packingDone"
      :aria-valuetext="packingLine">
      <span class="packing-status-bar" :style="{ width: packingPercent }"></span>
      <span class="packing-status-label">{{ packingLine }}</span>
    </div>
    <p v-else-if="bulkMessage" class="note bulk-status" role="status">{{ bulkMessage }}</p>
    <div class="speed-test" role="status">
      <span v-if="speed.state === 'sending'">{{ t("测速中 {sent}/{total}", { sent: speed.chunks_sent || 0, total: speed.total_chunks || 0 }) }}</span>
      <!-- The reading and the word for it, the way the panel put both beside
           each other.  The word is a chip rather than more text because it is a
           verdict on the number, not another one. -->
      <span v-else-if="speedReading && speedReading.mbps !== null">{{ t("速度 {mbps} MB/s", { mbps: speedReading.mbps }) }}<b class="speed-quality" :class="`speed-quality--${speedReading.tone}`">{{ speedReading.label }}</b></span>
      <span v-else-if="speedReading" class="speed-failed">{{ t("速度测试失败") }}</span>
      <span v-else>{{ t("点击测试局域网速度") }}</span>
    </div>
    <p v-if="errorLine" role="alert" class="error-band"><span class="error-band-label">{{ errorLine.label }}</span>{{ errorLine.message }}</p>
    <!-- 全部取消 sits on the card it acts on, the way 清除传输历史 sits on the
         history card, rather than in the send bar — which is where the controls
         that *start* work belong, and every one of them needs a target first.
         It cancels every row at once; the sidecar reads the live list, so a
         transfer that arrived since the last poll is included. -->
    <div class="transfer-list transfer-list--active card">
      <div class="transfer-list-header card-head">
        <h2>{{ t("进行中的传输") }}</h2>
        <button class="danger-outline" :disabled="busy || !active.length" @click="cancelAll"><X :size="17" />{{ t("全部取消") }}</button>
      </div>
      <div v-if="!active.length" class="empty"><FileUp :size="30" /><p class="note">{{ t("暂无进行中的传输") }}</p></div>
      <!-- Memoised on the values the row draws: the poll replaces the whole
           list once a second, and without this every row was re-patched on
           every tick — the progress bar jumped by a pixel, and the pause and
           cancel buttons beside it were rebuilt under a pointer that might be
           over one of them.  A row whose numbers did not move is left alone.
           The locale is in the list for the same reason it is in the row: a
           cached subtree keeps its text, so a language switch has to miss the
           cache. -->
      <article v-for="item in active" :key="item.id" v-memo="[currentLocale, busy, item.id, item.filename, item.direction, item.status, item.size, item.speed, item.eta, item.progress]" class="transfer-row">
        <div class="transfer-main"><strong>{{ item.filename || t("未知文件") }}</strong><span class="note">{{ [item.direction === "up" ? t("发送") : t("接收"), activeStatus(item), formatSize(item.size), formatSpeed(item.speed), item.eta].filter(Boolean).join(" · ") }}</span>
          <!-- Before the reader accepts: a relay arrival is slower than the
               same file over the same network, and the buttons beside this line
               are the accept/cancel the warning is asking about. -->
          <span v-if="relayArrival(item)" class="note transfer-relay-warning">{{ t("此文件经互联网传输，速度比同一网络慢。确定要接收吗？") }}</span>
        </div>
        <progress :value="item.progress || 0" max="100" :aria-label="t('{name} 进度', { name: item.filename || t('未知文件') })" /><span class="small">{{ item.progress || 0 }}%</span>
        <template v-if="item.direction === 'down' && item.status === 'pending'">
          <!-- 接收 / 拒绝 in words.  They were a tick and a cross, and on a row
               that already says 接收 as the direction, a tick answers the wrong
               question: it looks like "this one is fine" rather than "yes, send
               it".  The visible word is the direction's word, so the accessible
               name stays the longer 接受文件 / 拒绝文件: a reader arriving by
               name should not get two controls called 接收 on one row.  See
               U133. -->
          <button class="transfer-verdict transfer-verdict--yes" :disabled="busy" :aria-label="t('接受文件')" :title="t('接受文件')" @click="action('accept', item.id)"><Check :size="15" />{{ t("接收") }}</button>
          <button class="transfer-verdict" :disabled="busy" :aria-label="t('拒绝文件')" :title="t('拒绝文件')" @click="action('reject', item.id)"><X :size="15" />{{ t("拒绝") }}</button>
        </template>
        <template v-else>
          <button v-if="item.status === 'paused'" class="icon-button icon-button--sm" :disabled="busy" :aria-label="t('恢复传输')" :title="t('恢复传输')" @click="action('resume', item.id)"><Play :size="16" /></button>
          <button v-else-if="['sending', 'receiving', 'awaiting_retransmit'].includes(item.status)" class="icon-button icon-button--sm" :disabled="busy" :aria-label="t('暂停传输')" :title="t('暂停传输')" @click="action('pause', item.id)"><Pause :size="16" /></button>
          <button class="icon-button icon-button--sm" :disabled="busy" :aria-label="t('取消传输')" :title="t('取消传输')" @click="action('cancel', item.id)"><X :size="16" /></button>
        </template>
      </article>
    </div>
    <div class="transfer-list transfer-list--history card">
      <!-- The header carries the one action that is about the whole list rather
           than a row: the legacy panel put its 清除 button in this same header,
           right-aligned, and it went on the history card only — a running
           transfer is not a record, so clearing records cannot touch one. -->
      <div class="transfer-list-header card-head">
        <h2>{{ t("传输历史") }}</h2>
        <button class="danger-outline" :disabled="busy || !history.length" @click="clearOpen = true"><Eraser :size="16" />{{ t("清除传输历史") }}</button>
      </div>
      <!-- The panel's own summary of the list, under the header it belonged to.
           It is stated once for the card rather than counted per row, which is
           what a user checks before clearing the list. -->
      <p v-if="historyStats" class="note transfer-history-stats">{{ historyStats }}</p>
      <!-- The records are made by sending something, so the empty state is the
           place to offer that rather than a sentence about there being none. -->
      <EmptyState v-if="!history.length" :icon="FileUp" size="inset"
        :title="t('暂无传输记录')"
        :description="t('把这个文件发给一台已配对的设备，收发的每一笔都会记在这里。')"
        :action="t('发送文件')" :disabled="busy" @action="chooseAndSend('file')" />
      <article v-for="item in historyShown" :key="item.id" class="transfer-row" @contextmenu.prevent="rowMenu($event, item)">
        <!-- The panel's history rows put the reason in the badge, where the
             native row's slot beside the name used to say only that it was not
             completed: the reason is what the user wants from a failed row. -->
        <div class="transfer-main">
          <strong>{{ item.filename || t("未知文件") }}</strong>
          <span class="small" :class="item.status === 'completed' ? 'transfer-ok' : 'transfer-failed'">{{ [historyStatus(item), peerLabel(item), formatSize(item.size), dateTime(item.timestamp || 0)].filter(Boolean).join(" · ") }}</span>
        </div>
        <Check v-if="item.status === 'completed'" class="transfer-ok" :size="17" />
        <template v-if="item.direction !== 'up' && item.path">
          <button class="icon-button icon-button--sm" :disabled="busy" :aria-label="t('打开文件')" :title="t('打开文件')" @click="action('open', item.id)"><ExternalLink :size="16" /></button>
          <button class="icon-button icon-button--sm" :disabled="busy" :aria-label="t('打开所在文件夹')" :title="t('打开所在文件夹')" @click="action('reveal', item.id)"><FolderOpen :size="16" /></button>
        </template>
        <button v-if="retryable(item)" class="icon-button icon-button--sm" :aria-label="t('重试传输')" :title="t('重试传输')" @click="action('retry', item.id)"><RefreshCw :size="16" /></button>
        <!-- The fifth thing a failed row can offer, and the only one that
             answers a question rather than repeating the attempt. -->
        <button v-if="item.status !== 'completed'" class="icon-button icon-button--sm"
          :class="{ 'is-open': detailsId === item.id }"
          :aria-expanded="detailsId === item.id" :aria-controls="`transfer-detail-${item.id}`"
          :aria-label="t('详情')" :title="t('详情')" @click="toggleDetails(item)"><Info :size="16" /></button>
        <button class="icon-button icon-button--sm" :aria-label="t('删除传输记录')" :title="t('删除传输记录')" @click="action('delete', item.id)"><Trash2 :size="16" /></button>
        <!-- The account, in the row's own grid but across both columns: five
             labelled lines, then the button that copies them, because what this
             is for is being pasted into a message to somebody who can read the
             sidecar's log. -->
        <div v-if="detailsId === item.id" :id="`transfer-detail-${item.id}`" class="transfer-detail">
          <p v-for="line in detailLines(item)" :key="line" class="transfer-detail__line">{{ line }}</p>
          <button type="button" class="icon-button icon-button--inline"
            :aria-label="t('复制诊断')" :title="t('复制诊断')"
            @click="copyText(detailLines(item).join('\n'))"><Copy :size="14" />{{ t("复制诊断") }}</button>
        </div>
      </article>
      <!-- What the fold is holding back, and the one control that opens it.
           A line rather than a row: it is about the list, not about a file. -->
      <div v-if="historyHeldBack" class="transfer-more">
        <button type="button" class="text-button" :aria-expanded="historyExpanded" @click="historyExpanded = !historyExpanded">
          <component :is="historyExpanded ? ChevronUp : ChevronDown" :size="15" />
          {{ historyExpanded
            ? t("只显示最近 {count} 条", { count: HISTORY_PAGE })
            : t("显示其余 {count} 条", { count: historyHeldBack }) }}
        </button>
      </div>
    </div>
    <!-- The legacy panel asked before clearing (`transfers.clear_title` /
         `transfers.clear_confirm`), and this is a record the user may still want,
         so it asks too.  The count is the list's own — what is about to go — and
         the line the sidecar answers with afterwards is the count that actually
         went. -->
    <dialog ref="clearDialog" aria-labelledby="clear-transfers-title" class="modal" @close="clearOpen = false" @cancel="clearOpen = false">
      <button class="icon-button modal-close" :aria-label="t('关闭')" :title="t('关闭')" @click="clearOpen = false"><X :size="18" /></button>
      <h2 id="clear-transfers-title">{{ t("清除传输历史") }}</h2>
      <p class="muted">{{ t("共 {count} 条传输记录将被移除，正在进行的传输不受影响，此操作无法撤销。", { count: history.length }) }}</p>
      <p v-if="error" class="modal-error small" role="alert">{{ error }}</p>
      <div class="modal-actions"><button autofocus @click="clearOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="busy || !history.length" @click="clearHistory">{{ t("清除传输历史") }}</button></div>
    </dialog>
  </section>
</template>
