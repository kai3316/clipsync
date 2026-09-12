<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { Eraser, ExternalLink, FileDown, FileUp, FolderOpen, FolderUp, RefreshCw, Pause, Play, X, Check, Trash2 } from "@lucide/vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import { dateTime, size as formatSize, speed as formatSpeed } from "../i18n/format";
import type { Device, Transfer } from "../api/types";

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

/** A finished transfer's status, as the panel's history rows showed it. */
function historyStatus(item: Transfer): string {
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

/** The peers a file can be sent to: paired and currently reachable.
 *
 * Same rule the send-URL dialog picks from (``App.vue::openSendUrlFromHost``),
 * and the same one the legacy transfers page applied: it listed connected
 * devices only, and refused to start an upload with no target chosen
 * (``transfer.select_target``) rather than sending to everyone.
 */
const props = defineProps<{ devices?: Device[]; dropped?: string[] }>();
const emit = defineEmits<{ dropped: [] }>();
const targets = computed(() =>
  (props.devices || []).filter((device) => device.paired && device.connection_state === "online"));
// Nothing is preselected: a file is the one thing here the user cannot take
// back, so the machine it goes to is named rather than assumed.
const target = ref("");

const active = ref<Transfer[]>([]);
const history = ref<Transfer[]>([]);
const busy = ref(false);
const error = ref("");
const refreshError = ref("");
const speed = ref<Record<string, any>>({});
/** What the last bulk action did, reported rather than assumed: "cancel all"
 * counts what it actually cancelled, and clearing history counts what it
 * actually deleted.  One line for both, because it is one question — what did
 * that button just do — and only one of them can have been the last. */
const bulkMessage = ref("");
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
async function poll() {
  if (disposed) return;
  if (!busy.value) await refresh();
  if (!disposed) timer = setTimeout(poll, 1000);
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
onMounted(() => { void poll(); });
onUnmounted(() => {
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
        <option value="">{{ targets.length ? t("选择接收设备") : t("没有已连接的设备") }}</option>
        <option v-for="device in targets" :key="device.id" :value="device.id">{{ device.name }}</option>
      </select>
      <button class="primary" :disabled="busy || !target" @click="chooseAndSend('file')"><FileUp :size="17" />{{ t("发送文件") }}</button>
      <!-- The panel's second send button.  A folder leaves as one archive, and
           so do several files picked at once, so neither is a second transfer
           path — the sidecar archives and the same send carries it. -->
      <button :disabled="busy || !target" @click="chooseAndSend('folder')"><FolderUp :size="17" />{{ t("发送文件夹") }}</button>
      <button class="icon-button" :title="t('刷新')" :aria-label="t('刷新传输')" :disabled="busy" @click="refresh"><RefreshCw :size="18" /></button>
      <button :disabled="busy || speed.state === 'sending'" @click="speedTest">{{ t("速度测试") }}</button>
      <!-- Cancels every row at once; the sidecar reads the live list, so a
           transfer that arrived since the last poll is included. -->
      <button class="danger" :disabled="busy || !active.length" @click="cancelAll"><X :size="17" />{{ t("全部取消") }}</button>
    </div>
    <!-- A drop answers the picker, not the target: the files it brought are
         shown with the one question it left open — which machine — and nothing
         goes out until that is answered.  The names are the OS's own paths' last
         segments, because that is what the reader recognises. -->
    <div v-if="staged.length" class="staged-drop" role="status">
      <FileDown :size="17" />
      <div class="staged-drop-main">
        <strong>{{ t("已拖入 {count} 个文件", { count: staged.length }) }}</strong>
        <span class="muted small staged-drop-names" :title="staged.join('\n')">{{ staged.map(baseName).join(" · ") }}</span>
      </div>
      <button class="icon-button" :disabled="busy" :aria-label="t('清除')" :title="t('清除')" @click="staged = []"><X :size="16" /></button>
      <button class="primary" :disabled="busy || !target" @click="sendStaged"><FileUp :size="17" />{{ t("发送文件") }}</button>
    </div>
    <p v-if="bulkMessage" class="muted small bulk-status" role="status">{{ bulkMessage }}</p>
    <div class="speed-test" role="status">
      <span v-if="speed.state === 'sending'">{{ t("测速中 {sent}/{total}", { sent: speed.chunks_sent || 0, total: speed.total_chunks || 0 }) }}</span>
      <span v-else-if="speed.state === 'done'">{{ t("速度 {mbps} MB/s", { mbps: speed.result_mbps }) }}</span>
      <span v-else>{{ t("点击测试局域网速度") }}</span>
    </div>
    <p v-if="error" role="alert" class="error-band">{{ error }}</p>
    <p v-if="refreshError" role="alert" class="error-band">{{ refreshError }}</p>
    <div class="transfer-list"><h2>{{ t("进行中的传输") }}</h2>
      <div v-if="!active.length" class="empty"><FileUp :size="34" /><p>{{ t("暂无进行中的传输") }}</p></div>
      <article v-for="item in active" :key="item.id" class="transfer-row">
        <div class="transfer-main"><strong>{{ item.filename || t("未知文件") }}</strong><span class="muted small">{{ [item.direction === "up" ? t("发送") : t("接收"), activeStatus(item), formatSize(item.size), formatSpeed(item.speed), item.eta].filter(Boolean).join(" · ") }}</span></div>
        <progress :value="item.progress || 0" max="100" :aria-label="t('{name} 进度', { name: item.filename || t('未知文件') })" /><span class="small">{{ item.progress || 0 }}%</span>
        <template v-if="item.direction === 'down' && item.status === 'pending'">
          <button class="icon-button" :disabled="busy" :aria-label="t('接受文件')" :title="t('接受文件')" @click="action('accept', item.id)"><Check :size="16" /></button>
          <button class="icon-button" :disabled="busy" :aria-label="t('拒绝文件')" :title="t('拒绝文件')" @click="action('reject', item.id)"><X :size="16" /></button>
        </template>
        <template v-else>
          <button v-if="item.status === 'paused'" class="icon-button" :disabled="busy" :aria-label="t('恢复传输')" :title="t('恢复传输')" @click="action('resume', item.id)"><Play :size="16" /></button>
          <button v-else-if="['sending', 'receiving', 'awaiting_retransmit'].includes(item.status)" class="icon-button" :disabled="busy" :aria-label="t('暂停传输')" :title="t('暂停传输')" @click="action('pause', item.id)"><Pause :size="16" /></button>
          <button class="icon-button" :disabled="busy" :aria-label="t('取消传输')" :title="t('取消传输')" @click="action('cancel', item.id)"><X :size="16" /></button>
        </template>
      </article>
    </div>
    <div class="transfer-list transfer-list--history">
      <!-- The header carries the one action that is about the whole list rather
           than a row: the legacy panel put its 清除 button in this same header,
           right-aligned, and it went on the history card only — a running
           transfer is not a record, so clearing records cannot touch one. -->
      <div class="transfer-list-header">
        <h2>{{ t("传输历史") }}</h2>
        <button class="danger" :disabled="busy || !history.length" @click="clearOpen = true"><Eraser :size="16" />{{ t("清除传输历史") }}</button>
      </div>
      <div v-if="!history.length" class="empty"><p>{{ t("暂无传输记录") }}</p></div>
      <article v-for="item in history" :key="item.id" class="transfer-row">
        <!-- The panel's history rows put the reason in the badge, where the
             native row's slot beside the name used to say only that it was not
             completed: the reason is what the user wants from a failed row. -->
        <div class="transfer-main">
          <strong>{{ item.filename || t("未知文件") }}</strong>
          <span class="small" :class="item.status === 'completed' ? 'transfer-ok' : 'transfer-failed'">{{ [historyStatus(item), formatSize(item.size), dateTime(item.timestamp || 0)].filter(Boolean).join(" · ") }}</span>
        </div>
        <Check v-if="item.status === 'completed'" class="transfer-ok" :size="17" />
        <template v-if="item.direction !== 'up' && item.path">
          <button class="icon-button" :disabled="busy" :aria-label="t('打开文件')" :title="t('打开文件')" @click="action('open', item.id)"><ExternalLink :size="16" /></button>
          <button class="icon-button" :disabled="busy" :aria-label="t('打开所在文件夹')" :title="t('打开所在文件夹')" @click="action('reveal', item.id)"><FolderOpen :size="16" /></button>
        </template>
        <button v-if="item.status !== 'completed' && item.direction === 'up' && item.path" class="icon-button" :aria-label="t('重试传输')" :title="t('重试传输')" @click="action('retry', item.id)"><RefreshCw :size="16" /></button>
        <button class="icon-button" :aria-label="t('删除传输记录')" :title="t('删除传输记录')" @click="action('delete', item.id)"><Trash2 :size="16" /></button>
      </article>
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
      <p v-if="error" role="alert">{{ error }}</p>
      <div class="modal-actions"><button autofocus @click="clearOpen = false">{{ t("取消") }}</button><button class="danger" :disabled="busy || !history.length" @click="clearHistory">{{ t("清除传输历史") }}</button></div>
    </dialog>
  </section>
</template>
