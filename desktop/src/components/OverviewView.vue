<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  Activity, AlertCircle, ArrowRight, ClipboardPaste, Copy, Download, Keyboard, Monitor,
  Pencil, QrCode, RefreshCw, SendHorizontal, Settings as SettingsIcon, Upload,
} from "@lucide/vue";
import { bridge } from "../api/bridge";
import type { DiagnosticStatus, Overview } from "../api/types";
import type { createApplicationStore } from "../stores/application";
import { t } from "../i18n";
import { copyText } from "../lib/clipboard";
import { pairingInFlight } from "../lib/device-row";
import { announce } from "../lib/status";

/**
 * The overview: this machine, its usual switches, and the actions reached most.
 *
 * It used to be a dashboard — four counters, a network ring, a repeat of the
 * device list and a feed of the newest clips.  On an ordinary day most of those
 * numbers were 0, the ring was a drawing of the same numbers, the device list
 * was the devices page a click away, and the feed was the history page again.
 * What is left is the part a reader opens the window *for*: what this machine
 * is and where it can be reached, the four switches that are flipped often
 * enough to belong here rather than behind Settings, and one row of buttons to
 * the things one does from a clipboard manager.
 *
 * Nothing on this page is a clipboard list: the history page owns those rows,
 * their actions and their menu.  The page is a hub, not a second copy of it.
 *
 * The reads are the page's own (`overview.get`, plus the diagnostics summary
 * for its chip) rather than the lists the other pages keep, because this is the
 * page that answers before it is asked.
 */
const props = defineProps<{
  store: ReturnType<typeof createApplicationStore>;
  enabled: boolean;
  /** Whether the runtime is syncing.  The bottom bar reads the same field for
   * its own state: two readings of one fact have to be one fact, or the page
   * and the bar disagree the moment a timed pause expires. */
  syncRunning: boolean;
  /** The phone service's stored switch.  This page flips it but does not own
   * it — the devices page and the settings form draw the same field.  It is the
   * LAN phone page, not the internet relay: that one sends traffic through a
   * third-party broker and is only turned on from its own page. */
  companionOn: boolean;
  /** Milliseconds left on a timed pause, and whether one is being armed or
   * cleared right now.  The deadline lives in the window (`App.vue` reads it
   * from the runtime), which is also where the tray's presets land. */
  pauseLeftMs: number;
  pauseBusy: boolean;
}>();

const emit = defineEmits<{
  (event: "pause", minutes: number): void;
  (event: "resume"): void;
  (event: "companion", enabled: boolean): void;
  (event: "rename", name: string): void;
  (event: "qr"): void;
  (event: "devices"): void;
  (event: "diagnostics"): void;
  (event: "quick-paste"): void;
  (event: "send-files"): void;
  (event: "import-history"): void;
  (event: "export-history", format: string): void;
  (event: "settings-hotkeys"): void;
  (event: "check-update"): void;
  /** Open the settings page's update card. */
  (event: "settings-update"): void;
  /** The companion (phone page) page, for the switch's 去开启. */
  (event: "companion-page"): void;
  /** 更多设置: the window's whole configuration page. */
  (event: "settings"): void;
}>();

const state = props.store.state;
const busy = computed(() => !props.enabled || state.pending);
const deviceId = computed(() => state.status?.device_id || "");
const deviceName = computed(() => state.status?.device_name || t("此设备"));

/* ── The reads ────────────────────────────────────────────────────────── */

const overview = ref<Overview | null>(null);
const failed = ref("");
const loading = ref(false);
let disposed = false;

async function load() {
  if (!props.enabled || disposed) return;
  loading.value = true;
  try {
    const data = await bridge.overview();
    if (disposed) return;
    overview.value = data;
    failed.value = "";
  } catch (error: any) {
    if (!disposed) failed.value = error?.message || t("读取概览失败");
  } finally {
    if (!disposed) loading.value = false;
  }
}

/** Network health is a read of its own — the diagnostics suite, not this page's
 * counters — because the chip is a summary of checks the runtime only runs when
 * asked.  Kept to a cadence the report can carry, and skipped while the window
 * is hidden: a report nobody is looking at is a suite of socket probes run for
 * nobody. */
const health = ref<DiagnosticStatus | "">("");
const healthAvailable = computed(() => !!state.status?.capabilities?.includes("diagnostics.report"));

async function loadHealth() {
  if (!props.enabled || disposed || !healthAvailable.value) return;
  try {
    // Through the store, which keeps the report for a minute: the chip is how a
    // reader opens the diagnostics card, and the card would otherwise run the
    // same suite again for the verdict this line just took.
    const report = await props.store.diagnostics();
    if (!disposed) health.value = report?.summary || "";
  } catch {
    // A report that cannot be read leaves the chip saying so rather than
    // showing a verdict it does not have.
    if (!disposed) health.value = "";
  }
}

/** How often the health chip asks for a fresh verdict: a whole diagnostics
 * report is socket work, and network health does not turn over faster than
 * this.  The store holds the answer for a minute, so a chip pressed just after
 * a tick opens the report it already has. */
const HEALTH_POLL_MS = 120_000;
let overviewTimer: ReturnType<typeof setInterval> | undefined;
let healthTimer: ReturnType<typeof setInterval> | undefined;

/** Whether this page is on screen to be read at all: a window that is minimized
 * or occluded runs no probes for a verdict nobody would see. */
function awake() {
  return !document.hidden;
}
onMounted(() => {
  void load();
  void loadHealth();
  overviewTimer = setInterval(() => { if (awake()) void load(); }, 5000);
  healthTimer = setInterval(() => { if (awake()) void loadHealth(); }, HEALTH_POLL_MS);
});
/** The reads wait on `enabled`, and the window mounts this page before it has
 * heard from the sidecar at all — so the first read follows that flag in rather
 * than waiting for the next turn of a five-second clock. */
watch(() => props.enabled, (on) => {
  if (!on) return;
  void load();
  void loadHealth();
});
/** Whether the runtime is still coming up, rather than not being there at all.
 * The window mounts this page with no status at all, and the shell calls that
 * 正在启动 — so the page says the same thing instead of reporting a verdict it
 * does not have yet. */
const starting = computed(() =>
  !state.error && (!state.status || state.status.health === "starting"));
onUnmounted(() => {
  disposed = true;
  if (overviewTimer) clearInterval(overviewTimer);
  if (healthTimer) clearInterval(healthTimer);
});

/* ── The switches ─────────────────────────────────────────────────────── */

/** A switch goes back to the truth it was drawn from before its write is sent:
 * the browser has already flipped the box under the reader's finger, and a
 * refused write would otherwise leave it flipped.  The read that follows puts
 * it where the runtime says it is. */
/** Say a refused switch out loud.  The store's failure band is at the top of the
 * window and can be scrolled out of sight from here, so the refusal also goes
 * to the status bar — the same place every other click's answer lands. */
function reportSwitchFailure() {
  announce(state.error?.message || t("操作没有生效，请稍后重试"));
}
async function toggleSync(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = props.syncRunning;
  if (busy.value) return;
  await props.store.setSyncEnabled(desired);
  if (state.error) reportSwitchFailure();
  await load();
}
async function toggleDiscovery(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = !!overview.value?.discovering;
  if (busy.value) return;
  const before = state.error;
  const result = await props.store.setDiscoveryEnabled(desired);
  // A null answer is either a refusal the store reported (a new error) or a
  // guard that never asked the runtime at all; only the first has a message.
  if (!result) announce(state.error && state.error !== before
    ? state.error.message : t("操作没有生效，请稍后重试"));
  await load();
}
async function toggleVisibility(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = !!overview.value?.visible;
  if (busy.value) return;
  const before = state.error;
  const result = await props.store.setDiscoveryVisible(desired);
  if (!result) announce(state.error && state.error !== before
    ? state.error.message : t("操作没有生效，请稍后重试"));
  await load();
}
function toggleCompanion(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = props.companionOn;
  emit("companion", desired);
}

/** The pause presets, as a computed rather than a constant: their labels are
 * translated, and a list built once at setup would keep whichever language the
 * window was in when the page was first opened. */
const pausePresets = computed(() => [
  { minutes: 15, label: t("15 分钟") },
  { minutes: 30, label: t("30 分钟") },
  { minutes: 60, label: t("1 小时") },
]);
const pauseLeftMinutes = computed(() => Math.max(1, Math.ceil(props.pauseLeftMs / 60000)));

const networkLabel = computed(() => {
  const kind = overview.value?.network_type;
  if (kind === "ethernet") return t("以太网");
  if (kind === "lan") return t("局域网");
  return "Wi-Fi";
});
const healthLabel = computed(() => {
  if (health.value === "ok") return t("网络正常");
  if (health.value === "warn") return t("网络需注意");
  if (health.value === "fail") return t("网络异常");
  return t("检查网络");
});
const devicesOnline = computed(() => overview.value?.connected_count || 0);
/** What the device slot says, and its three cases are three next steps: devices
 * here, devices paired but away, or nothing paired yet.  A zero is said as a way
 * to pair rather than as a number to look at. */
const deviceLine = computed(() => {
  const paired = overview.value?.paired_count || 0;
  if (devicesOnline.value > 0) return t("{count} 台设备在线", { count: devicesOnline.value });
  if (paired > 0) return t("已配对 {count} 台 · 当前离线", { count: paired });
  return t("还没有配对设备 · 去配对");
});

/* ── This machine ─────────────────────────────────────────────────────── */

const renaming = ref(false);
const nameValue = ref("");
const nameInput = ref<HTMLInputElement | null>(null);
/** Escape takes the editor away, and the blur that follows must not commit what
 * the reader just cancelled. */
let cancelled = false;

async function startRename() {
  renaming.value = true;
  nameValue.value = deviceName.value;
  cancelled = false;
  await nextTick();
  nameInput.value?.focus();
  nameInput.value?.select();
}
function saveName() {
  if (cancelled) return;
  const name = nameValue.value.trim();
  renaming.value = false;
  if (!name || name === deviceName.value) return;
  emit("rename", name);
}
function cancelRename() {
  cancelled = true;
  renaming.value = false;
  void nextTick(() => { cancelled = false; });
}

/** The address other machines on this network reach this one at, while the
 * phone service is serving it: `ip:port`, which is what a reader reads out or
 * pastes into a browser.  Empty when there is nothing listening. */
const address = computed(() => {
  const o = overview.value;
  return o && o.local_ip ? `${o.local_ip}:${o.port}` : "";
});
async function copyDeviceId() {
  if (deviceId.value) await copyText(deviceId.value);
}
async function copyAddress() {
  if (address.value) await copyText(address.value);
}

/* ── Things that need the reader ──────────────────────────────────────── */

type AttentionAction = "devices" | "settings-update";
type AttentionRow = { id: string; text: string; action: AttentionAction; actionLabel: string };
/** A line, not a block, and only when there is something to say: a newer build
 * or a pairing waiting on this window.  The list is built from payloads the page
 * already has — an empty list renders nothing at all, which is the point.  An
 * offline device is not repeated here: the status line above already says how
 * many are away. */
const attention = computed<AttentionRow[]>(() => {
  const rows: AttentionRow[] = [];
  const o = overview.value;
  if (!o) return rows;
  if (state.updateCheck?.available) {
    rows.push({
      id: "update", text: t("有新版本 {version} 可用", { version: state.updateCheck.latest || "" }),
      action: "settings-update", actionLabel: t("查看更新"),
    });
  }
  const pending = state.devices.filter((device) => !device.archived && pairingInFlight(device)).length;
  if (pending > 0) {
    rows.push({
      id: "pairing", text: t("有 {count} 个配对请求等待确认", { count: pending }),
      action: "devices", actionLabel: t("去处理"),
    });
  }
  return rows;
});
function runAttention(row: AttentionRow) {
  if (row.action === "devices") emit("devices");
  else emit("settings-update");
}
</script>

<template>
  <section class="overview-view" :aria-label="t('概览')">
    <div v-if="!enabled" class="empty empty--page" role="status">
      <RefreshCw v-if="starting" class="spinning" :size="26" />
      <Activity v-else :size="36" />
      <h2>{{ starting ? t("正在读取概览") : t("概览暂不可用") }}</h2>
      <p v-if="!starting" class="note">{{ t("同步引擎未运行，无法读取概览。") }}</p>
    </div>
    <div v-else-if="!overview" class="overview-skeleton" :aria-busy="loading" role="status">
      <!-- Kept in the page's own shape while the first read is in the air: the
           status strip and the two cards stay where they will be, so the page
           does not flash an empty panel and then jump. -->
      <div class="overview-status overview-skeleton-strip" aria-hidden="true"></div>
      <div class="overview-row" aria-hidden="true">
        <div class="overview-card card overview-skeleton-card"></div>
        <div class="overview-card card overview-skeleton-card"></div>
      </div>
      <div v-if="failed" class="error-band" role="alert">
        <span>{{ failed }}</span>
        <button class="text-button" type="button" @click="load">{{ t("重试") }}</button>
      </div>
      <p v-else class="note" role="status">{{ t("正在读取概览") }}</p>
    </div>
    <template v-else>
      <div v-if="failed" class="error-band" role="alert">
        <span>{{ failed }}</span>
        <button class="text-button" type="button" @click="load">{{ t("重试") }}</button>
      </div>

      <!-- One line of what the machine is doing: sync, the network, the devices
           on it and the health verdict that opens the diagnostics card.  The
           IP is the only other thing a reader copies out of a status bar. -->
      <div class="overview-status">
        <span class="overview-dot" :class="syncRunning ? 'overview-dot--on' : 'overview-dot--off'" aria-hidden="true"></span>
        <span>{{ syncRunning ? t("同步运行中") : t("同步已暂停") }}</span>
        <span v-if="pauseLeftMs > 0" class="note">{{ t("已暂停 · 剩余 {minutes} 分钟", { minutes: pauseLeftMinutes }) }}</span>
        <span class="overview-sep" aria-hidden="true"></span>
        <span class="muted">{{ networkLabel }}<template v-if="overview.network_detail"> · {{ overview.network_detail }}</template></span>
        <span class="overview-sep" aria-hidden="true"></span>
        <!-- The device count is a way to the devices page, and its empty form is
             a way to pair.  Not a zero to look at, and not the device list that
             page already draws. -->
        <button class="text-button overview-device-line" type="button" @click="emit('devices')">
          <span class="overview-dot" :class="devicesOnline ? 'overview-dot--on' : 'overview-dot--off'" aria-hidden="true"></span>{{ deviceLine }}
        </button>
        <button class="overview-health" :class="health ? `overview-health--${health}` : ''"
          type="button" :title="t('打开网络诊断')" @click="emit('diagnostics')">
          <span class="overview-dot" :class="`overview-dot--${health || 'unknown'}`" aria-hidden="true"></span>{{ healthLabel }}
        </button>
      </div>

      <!-- Said as a line when it is true, and not drawn at all when it is not. -->
      <div v-if="attention.length" class="overview-attention" role="status">
        <AlertCircle :size="16" aria-hidden="true" />
        <ul class="overview-attention-list">
          <li v-for="row in attention" :key="row.id">
            <span>{{ row.text }}</span>
            <button class="text-button" type="button" @click="runAttention(row)">{{ row.actionLabel }}</button>
          </li>
        </ul>
      </div>

      <div class="overview-row">
        <!-- This machine's own facts: the two another machine needs in order to
             pair with it, where it answers, and the QR that reaches a phone
             without installing anything. -->
        <section class="overview-card card">
          <h2>{{ t("本机信息") }}</h2>
          <div class="overview-name">
            <Monitor :size="17" aria-hidden="true" />
            <template v-if="!renaming">
              <span class="overview-name-text">{{ deviceName }}</span>
              <button class="icon-button icon-button--sm" type="button" :aria-label="t('编辑设备名称')"
                :title="t('编辑设备名称')" :disabled="busy" @click="startRename"><Pencil :size="15" /></button>
            </template>
            <input v-else ref="nameInput" class="overview-name-input" :aria-label="t('编辑设备名称')"
              :value="nameValue" maxlength="128"
              @input="nameValue = ($event.target as HTMLInputElement).value"
              @keydown.enter="saveName" @keydown.escape="cancelRename" @blur="saveName" />
          </div>
          <dl class="overview-facts">
            <dt>{{ t("设备 ID") }}</dt>
            <dd class="overview-id">
              <span class="overview-mono">{{ deviceId }}</span>
              <button class="icon-button icon-button--sm" type="button" :aria-label="t('复制设备 ID')"
                :title="t('复制设备 ID')" :disabled="!deviceId" @click="copyDeviceId"><Copy :size="13" /></button>
            </dd>
            <dt>{{ t("平台") }}</dt>
            <dd>{{ overview.platform }} · v{{ overview.version }}</dd>
            <dt>{{ t("本地地址") }}</dt>
            <dd class="overview-id">
              <template v-if="address">
                <span class="overview-mono">{{ address }}</span>
                <button class="icon-button icon-button--sm" type="button" :aria-label="t('复制本地地址')"
                  :title="t('复制本地地址')" @click="copyAddress"><Copy :size="13" /></button>
              </template>
              <template v-else>
                <span class="note">{{ t("手机伴侣未启用") }}</span>
                <button class="text-button" type="button" @click="emit('companion-page')">{{ t("去开启") }}</button>
              </template>
            </dd>
          </dl>
          <div class="overview-actions">
            <button class="overview-button" type="button" :disabled="busy" @click="emit('qr')">
              <QrCode :size="16" />{{ t("显示二维码") }}
            </button>
          </div>
        </section>

        <!-- The four switches a reader flips often enough that they should not be
             three clicks into Settings, and the timed pause the tray also offers.
             Everything more involved stays on the settings page. -->
        <section class="overview-card card">
          <h2>{{ t("快速设置") }}</h2>
          <label class="overview-switch">
            <span>{{ t("剪贴板同步") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('剪贴板同步')" :checked="syncRunning"
              :disabled="busy" @change="toggleSync" />
          </label>
          <label class="overview-switch">
            <span>{{ t("局域网发现") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('局域网发现')" :checked="!!overview.discovering"
              :disabled="busy || !overview" @change="toggleDiscovery" />
          </label>
          <label class="overview-switch">
            <span>{{ t("在局域网可见") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('在局域网可见')" :checked="!!overview.visible"
              :disabled="busy || !overview" @change="toggleVisibility" />
          </label>
          <label class="overview-switch">
            <span>{{ t("手机网页访问") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('手机网页访问')" :checked="companionOn"
              :disabled="busy" @change="toggleCompanion" />
          </label>
          <p v-if="pauseLeftMs > 0" class="overview-pause">
            <span class="note">{{ t("定时暂停同步") }}</span>
            <button class="overview-button primary" type="button" :disabled="pauseBusy" @click="emit('resume')">{{ t("立即恢复") }}</button>
          </p>
          <p v-else-if="syncRunning" class="overview-pause">
            <span class="note">{{ t("定时暂停同步") }}</span>
            <button v-for="preset in pausePresets" :key="preset.minutes" class="overview-button" type="button"
              :disabled="pauseBusy" @click="emit('pause', preset.minutes)">{{ preset.label }}</button>
          </p>
          <p v-else class="overview-pause">
            <button class="overview-button" type="button" :disabled="pauseBusy" @click="emit('resume')">{{ t("恢复同步") }}</button>
          </p>
        </section>
      </div>

      <!-- The one row of actions this page exists for.  Each is the window's own
           handler, not a second implementation: 导入/导出 are the settings
           card's, 快捷键 opens the general card, 检查更新 runs the same check,
           and 更多设置 is just the settings page. -->
      <section class="overview-card card">
        <h2>{{ t("快捷操作") }}</h2>
        <div class="overview-actions">
          <button class="overview-button primary" type="button" @click="emit('quick-paste')"><ClipboardPaste :size="16" />{{ t("快速粘贴") }}</button>
          <button class="overview-button" type="button" @click="emit('send-files')"><SendHorizontal :size="16" />{{ t("发送文件") }}</button>
          <!-- The device page is one of the three primary pages, so it gets an
               explicit door here as well as the clickable count on the status
               line: the count answers "are they here", this answers "manage
               them". -->
          <button class="overview-button" type="button" @click="emit('devices')"><Monitor :size="16" />{{ t("设备管理") }}</button>
          <button class="overview-button" type="button" :disabled="busy" @click="emit('import-history')"><Upload :size="16" />{{ t("导入历史") }}</button>
          <button class="overview-button" type="button" :disabled="busy" @click="emit('export-history', 'json')"><Download :size="16" />{{ t("导出历史") }}</button>
          <button class="overview-button" type="button" @click="emit('settings-hotkeys')"><Keyboard :size="16" />{{ t("快捷键设置") }}</button>
          <button class="overview-button" type="button" @click="emit('check-update')"><RefreshCw :size="16" />{{ t("检查更新") }}</button>
          <button class="overview-button overview-more" type="button" @click="emit('settings')"><SettingsIcon :size="16" />{{ t("更多设置") }}<ArrowRight :size="15" aria-hidden="true" /></button>
        </div>
      </section>
    </template>
  </section>
</template>
