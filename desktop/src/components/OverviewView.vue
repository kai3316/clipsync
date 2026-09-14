<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  Activity, Check, ClipboardType, Copy, FileText, FileUp, Image as ImageIcon, Link, Monitor,
  Pencil, Pin, QrCode, RefreshCw, SendHorizontal,
} from "@lucide/vue";
import { bridge } from "../api/bridge";
import type { DiagnosticStatus, Overview, OverviewRecentItem } from "../api/types";
import type { createApplicationStore } from "../stores/application";
import { t } from "../i18n";
import { previewText } from "../i18n/format";
import { copyText } from "../lib/clipboard";

/**
 * The dashboard: what this machine is doing right now.
 *
 * The legacy window opened on this page and the phone's panel still does, and
 * it is the one page that answers without being asked — so it is drawn from a
 * read of its own (`overview.get`) rather than from the history and device
 * lists the other pages keep, and it is the sidebar's first row.
 *
 * Four of the things it can do belong to the window rather than to this page:
 * starting and stopping the phone service, renaming this device, the two
 * dialogs behind 显示二维码 and 发送链接到设备, and the three pages its rows
 * lead to.  Those are handed up as events; the three switches that are plain
 * writes to the runtime go through the store, which is where every other page
 * reaches it too.
 */
const props = defineProps<{
  store: ReturnType<typeof createApplicationStore>;
  enabled: boolean;
  /** Whether the runtime is syncing.  The window's bottom bar reads the same
   * field for its own switch: two switches over one fact have to be one fact,
   * or the page and the bar disagree the moment a timed pause expires. */
  syncRunning: boolean;
  /** The phone service's stored switch, and the address that reaches it.  Both
   * come from the settings page's own read of the service — this page toggles
   * it but does not own it. */
  companionOn: boolean;
  companionUrl: string | null;
  /** Milliseconds left on a timed pause, and whether one is being armed or
   * cleared right now.  The deadline and its countdown are the bottom bar's,
   * which is where the pause presets live when this page is not on screen. */
  pauseLeftMs: number;
  pauseBusy: boolean;
}>();

const emit = defineEmits<{
  (event: "pause", minutes: number): void;
  (event: "resume"): void;
  (event: "companion", enabled: boolean): void;
  (event: "rename", name: string): void;
  (event: "qr"): void;
  (event: "send-url"): void;
  (event: "devices"): void;
  (event: "history"): void;
  (event: "diagnostics"): void;
  /** The right-click menu on a feed row.  It is raised rather than built here
   * because it is the history page's own menu: the row is the same row, and a
   * menu written again on this page would be a second set of conditions to keep
   * in step with the first. */
  (event: "row-menu", payload: MouseEvent, item: OverviewRecentItem): void;
}>();

const state = props.store.state;
const busy = computed(() => !props.enabled || state.pending);
const deviceId = computed(() => state.status?.device_id || "");
const deviceName = computed(() => state.status?.device_name || t("此设备"));

/* ── The read ─────────────────────────────────────────────────────────── */

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

/** Network health is a read of its own — the diagnostics suite, not this
 * page's counters — because the chip is a summary of checks the runtime only
 * runs when asked.  Kept to the cadence the legacy panel used, and skipped
 * while the window is hidden: a report nobody is looking at is a suite of
 * socket probes run for nobody. */
const health = ref<DiagnosticStatus | "">("");
const healthAvailable = computed(() => !!state.status?.capabilities?.includes("diagnostics.report"));

async function loadHealth() {
  if (!props.enabled || disposed || !healthAvailable.value) return;
  try {
    const report = await bridge.diagnosticsReport();
    if (!disposed) health.value = report?.summary || "";
  } catch {
    // A report that cannot be read leaves the chip saying so rather than
    // showing a verdict it does not have.
    if (!disposed) health.value = "";
  }
}

let overviewTimer: ReturnType<typeof setInterval> | undefined;
let healthTimer: ReturnType<typeof setInterval> | undefined;
/** Whether this page is on screen to be read at all.
 *
 * A window that is minimized or occluded is not, and the two periodic reads
 * below are socket work whose answer nobody would see — they wait for the
 * window to come back, and the first tick after it does redraws the page.
 *
 * Focus is deliberately not consulted: a dashboard kept open beside the work is
 * usually not the focused window, and that is exactly when it is being watched.
 */
function awake() {
  return !document.hidden;
}
onMounted(() => {
  void load();
  void loadHealth();
  overviewTimer = setInterval(() => { if (awake()) void load(); }, 5000);
  healthTimer = setInterval(() => { if (awake()) void loadHealth(); }, 8000);
});
/** The two reads above wait on `enabled`, and the window mounts this page
 * before it has heard from the sidecar at all — so the first read has to follow
 * that flag in rather than wait for the next turn of a five-second clock.  This
 * page is the one a reader sees first, and without this it opens saying 概览暂不可用
 * on every launch, for as long as the clock takes to come round. */
watch(() => props.enabled, (on) => {
  if (!on) return;
  void load();
  void loadHealth();
});

/** Whether the runtime is still coming up, rather than not being there.
 *
 * The window mounts this page with no status at all, and the shell calls that
 * 正在启动 — so the page says the same thing instead of reporting a verdict it
 * does not have yet.  A stopped engine, or a sidecar too old to serve the
 * overview, is what the other branch is for.
 *
 * The error is the half of that the shell already reads and this page did not:
 * its own chip asks "is there a window at all, then has anything failed, then
 * how is the runtime" in that order, so a browser preview and a sidecar that
 * died both read 连接异常 or 浏览器预览 up there.  Without it the page below the
 * chip claimed to be reading for as long as the window stayed open — the one
 * outcome a reader cannot act on, and, on the landing page, the one that reads
 * as a hung application. */
const starting = computed(() =>
  !state.error && (!state.status || state.status.health === "starting"));
onUnmounted(() => {
  disposed = true;
  if (overviewTimer) clearInterval(overviewTimer);
  if (healthTimer) clearInterval(healthTimer);
});

/* ── The switches ─────────────────────────────────────────────────────── */

/** A switch goes back to the truth it was drawn from before its write is
 * sent: the browser has already flipped the box under the reader's finger,
 * and a refused write would otherwise leave it flipped.  The read that
 * follows puts it where the runtime says it is. */
async function toggleSync(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = props.syncRunning;
  if (busy.value) return;
  await props.store.setSyncEnabled(desired);
  await load();
}
async function toggleDiscovery(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = !!overview.value?.discovering;
  if (busy.value) return;
  await props.store.setDiscoveryEnabled(desired);
  await load();
}
async function toggleVisibility(event: Event) {
  const input = event.target as HTMLInputElement;
  const desired = input.checked;
  input.checked = !!overview.value?.visible;
  if (busy.value) return;
  await props.store.setDiscoveryVisible(desired);
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

/* ── This device ──────────────────────────────────────────────────────── */

const renaming = ref(false);
const nameValue = ref("");
const nameInput = ref<HTMLInputElement | null>(null);
/** Escape takes the editor away, and the blur that follows must not commit
 * what the reader just cancelled. */
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

/** The address the phone opens.  The token-carrying URL when the service is
 * running — that is the one the QR encodes and the one that works without the
 * phone being told a token — and otherwise the plain page address, which is
 * what the reader is being shown. */
const address = computed(() => (overview.value ? `${overview.value.local_ip}:${overview.value.port}` : ""));
async function copyAddress() {
  const value = props.companionUrl
    || (overview.value?.local_ip ? `http://${overview.value.local_ip}:${overview.value.port}/mobile.html` : "");
  if (value) await copyText(value);
}

/* ── Counters, ring and feed ──────────────────────────────────────────── */

const stats = computed(() => {
  const o = overview.value;
  return {
    connected: o?.connected_count || 0,
    paired: o?.paired_count || 0,
    history: o?.history_count || 0,
    today: o?.history_today || 0,
    images: o?.history_images || 0,
    pinned: o?.history_pinned || 0,
    transfers: o?.active_transfers || 0,
    completed: o?.transfer_completed || 0,
  };
});

/** A paired device that is not connected right now is the ring's middle band,
 * so the two counts are made to describe different machines before they are
 * drawn: `paired_count` already contains every connected one. */
const ring = computed(() => {
  const connected = stats.value.connected;
  const paired = Math.max(0, stats.value.paired - connected);
  const discovered = overview.value?.discovered_count || 0;
  return { connected, paired, discovered, total: connected + paired + discovered || 1 };
});
const ringStyle = computed(() => {
  const r = ring.value;
  const connected = (r.connected / r.total) * 100;
  const paired = (r.paired / r.total) * 100;
  const discovered = (r.discovered / r.total) * 100;
  return {
    background: "conic-gradient("
      + `var(--state-connected) 0% ${connected}%,`
      + `var(--state-paired) ${connected}% ${connected + paired}%,`
      + `var(--state-discovered) ${connected + paired}% ${connected + paired + discovered}%,`
      + `transparent ${connected + paired + discovered}% 100%)`,
  };
});

/** Put a feed row's clip back on the clipboard.
 *
 * The row's whole job now, and the history page's own action — `store.copy` is
 * what a history row's 复制 runs, so the copy is the same one down to the
 * clipboard write, and it sets the store's `copiedId`, which is what lets the
 * row show the same tick the history rows show.
 */
function copyRecent(item: OverviewRecentItem) {
  void props.store.copy(item);
}

function typeIcon(type: string) {
  const kind = String(type || "").toUpperCase();
  if (kind === "URL" || kind === "LINK") return Link;
  if (kind === "FILE") return FileUp;
  if (kind.startsWith("IMAGE")) return ImageIcon;
  return kind === "TEXT" || !kind ? ClipboardType : FileText;
}

/** Uptime in the legacy panel's own compact form — `2d 4h`, `3h 12m`, `9m` —
 * which is unit-short and needs no translation. */
function uptime(seconds: number) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${total % 60}s`;
}

function ago(timestamp: number) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(timestamp || 0)));
  if (seconds < 60) return t("刚刚");
  if (seconds < 3600) return t("{minutes} 分钟前", { minutes: Math.floor(seconds / 60) });
  if (seconds < 86400) return t("{hours} 小时前", { hours: Math.floor(seconds / 3600) });
  return t("{days} 天前", { days: Math.floor(seconds / 86400) });
}

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
const connectedNames = computed(() => (overview.value?.connected_names || []).slice(0, 8));
</script>

<template>
  <section class="overview-view" :aria-label="t('概览')">
    <div v-if="!enabled" class="empty empty--page" role="status">
      <RefreshCw v-if="starting" class="spinning" :size="26" />
      <Activity v-else :size="36" />
      <h2>{{ starting ? t("正在读取概览") : t("概览暂不可用") }}</h2>
      <p v-if="!starting" class="muted">{{ t("同步引擎未运行，无法读取概览。") }}</p></div>
    <div v-else-if="!overview" class="empty empty--page">
      <Activity :size="30" /><h2>{{ loading ? t("正在读取概览") : t("概览暂不可用") }}</h2>
      <p v-if="failed" class="muted small">{{ failed }}</p>
    </div>
    <template v-else>
      <div v-if="failed" class="error-band" role="alert">{{ failed }}</div>

      <!-- Status bar: one line of what the machine is doing, and at the far end
           whether anything is wrong.  It used to carry the connected count and
           the uptime too, which the 此设备 card below already writes in its own
           chips — the same two numbers twice on one screen, which is most of
           what made this page read as cluttered.  What is left is the state and
           the network, and nothing here is said twice anywhere else. -->
      <div class="overview-status">
        <span class="overview-dot" :class="syncRunning ? 'overview-dot--on' : 'overview-dot--off'" aria-hidden="true"></span>
        <span>{{ syncRunning ? t("同步运行中") : t("同步已暂停") }}</span>
        <span class="overview-sep" aria-hidden="true"></span>
        <span class="muted">{{ networkLabel }}<template v-if="overview.network_detail"> · {{ overview.network_detail }}</template></span>
        <span class="overview-gap"></span>
        <button class="overview-health" :class="health ? `overview-health--${health}` : ''"
          :title="t('打开网络诊断')" @click="emit('diagnostics')">
          <span class="overview-dot" :class="`overview-dot--${health || 'unknown'}`" aria-hidden="true"></span>{{ healthLabel }}
        </button>
        <span v-if="overview.local_ip" class="muted small">{{ overview.local_ip }}:{{ overview.port }}</span>
      </div>

      <div class="overview-row">
        <section class="overview-card">
          <h2>{{ t("此设备") }}</h2>
          <div class="overview-name">
            <Monitor :size="17" aria-hidden="true" />
            <template v-if="!renaming">
              <span class="overview-name-text">{{ deviceName }}</span>
              <button class="icon-button" :aria-label="t('编辑设备名称')" :title="t('编辑设备名称')"
                :disabled="busy" @click="startRename"><Pencil :size="15" /></button>
            </template>
            <input v-else ref="nameInput" class="overview-name-input" :aria-label="t('编辑设备名称')"
              :value="nameValue" maxlength="128"
              @input="nameValue = ($event.target as HTMLInputElement).value"
              @keydown.enter="saveName" @keydown.escape="cancelRename" @blur="saveName" />
          </div>
          <dl class="overview-facts">
            <dt>{{ t("设备 ID") }}</dt>
            <dd class="overview-mono">{{ deviceId }}</dd>
            <dt>{{ t("平台") }}</dt>
            <dd>{{ overview.platform }} · v{{ overview.version }}</dd>
          </dl>
          <p class="overview-chips">
            <span class="overview-chip"><span class="overview-dot"
              :class="stats.connected ? 'overview-dot--on' : 'overview-dot--off'" aria-hidden="true"></span>
              {{ t("已连接 {count} 台", { count: stats.connected }) }}</span>
            <span class="overview-chip">{{ t("运行时间") }} {{ uptime(overview.uptime_seconds) }}</span>
          </p>
          <p v-if="overview.web_enabled && overview.local_ip" class="overview-address">
            <span class="muted small">{{ t("本地地址") }}</span>
            <code>{{ address }}</code>
            <button class="overview-button" :disabled="busy" @click="copyAddress"><Copy :size="15" />{{ t("复制地址") }}</button>
          </p>
        </section>

        <section class="overview-card">
          <h2>{{ t("快速控制") }}</h2>
          <label class="overview-switch">
            <span>{{ t("剪贴板同步") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('剪贴板同步')" :checked="syncRunning"
              :disabled="busy" @change="toggleSync" />
          </label>
          <label class="overview-switch">
            <span>{{ t("发现") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('发现')" :checked="!!overview.discovering"
              :disabled="busy || !overview" @change="toggleDiscovery" />
          </label>
          <label class="overview-switch">
            <span>{{ t("可见性") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('可见性')" :checked="!!overview.visible"
              :disabled="busy || !overview" @change="toggleVisibility" />
          </label>
          <label class="overview-switch">
            <span>{{ t("远程访问") }}</span>
            <input type="checkbox" role="switch" :aria-label="t('远程访问')" :checked="companionOn"
              :disabled="busy" @change="toggleCompanion" />
          </label>
          <p v-if="pauseLeftMs > 0" class="overview-pause">
            <span class="muted small">{{ t("已暂停 · 剩余 {minutes} 分钟", { minutes: pauseLeftMinutes }) }}</span>
            <button class="overview-button" :disabled="pauseBusy" @click="emit('resume')">{{ t("立即恢复") }}</button>
          </p>
          <p v-else-if="syncRunning" class="overview-pause">
            <span class="muted small">{{ t("定时暂停同步") }}</span>
            <button v-for="preset in pausePresets" :key="preset.minutes" class="overview-button"
              :disabled="pauseBusy" @click="emit('pause', preset.minutes)">{{ preset.label }}</button>
          </p>
          <p v-else class="overview-pause">
            <button class="overview-button" :disabled="pauseBusy" @click="emit('resume')">{{ t("恢复同步") }}</button>
          </p>
        </section>
      </div>

      <div class="overview-stats">
        <article class="overview-stat">
          <Activity :size="16" aria-hidden="true" /><strong>{{ stats.connected }}</strong>
          <span>{{ t("已连接") }}</span><small>{{ stats.paired }} {{ t("已信任") }}</small>
        </article>
        <article class="overview-stat">
          <ClipboardType :size="16" aria-hidden="true" /><strong>{{ stats.history }}</strong>
          <span>{{ t("历史记录") }}</span><small>+{{ stats.today }} {{ t("今天") }}</small>
        </article>
        <article class="overview-stat">
          <ImageIcon :size="16" aria-hidden="true" /><strong>{{ stats.images }}</strong>
          <span>{{ t("图片") }}</span><small>{{ stats.pinned }} {{ t("已置顶") }}</small>
        </article>
        <article class="overview-stat">
          <FileUp :size="16" aria-hidden="true" /><strong>{{ stats.transfers }}</strong>
          <span>{{ t("传输") }}</span><small>{{ stats.completed }} {{ t("已完成") }}</small>
        </article>
      </div>

      <div class="overview-row">
        <section class="overview-card">
          <h2>{{ t("网络地图") }}</h2>
          <div class="overview-ring-wrap">
            <!-- The legend is the drawing's own text: it carries all three
                 numbers, so the ring itself is decoration and says nothing. -->
            <div class="overview-ring" :style="ringStyle" aria-hidden="true">
              <span class="overview-ring-center">
                <strong>{{ ring.connected }}</strong><small>{{ t("已连接") }}</small>
              </span>
            </div>
            <ul class="overview-legend">
              <li><span class="overview-swatch overview-swatch--connected" aria-hidden="true"></span>{{ ring.connected }} {{ t("已连接") }}</li>
              <li><span class="overview-swatch overview-swatch--paired" aria-hidden="true"></span>{{ ring.paired }} {{ t("已配对（离线）") }}</li>
              <li><span class="overview-swatch overview-swatch--discovered" aria-hidden="true"></span>{{ ring.discovered }} {{ t("已发现") }}</li>
            </ul>
          </div>
        </section>

        <section class="overview-card">
          <h2>{{ t("已连接设备") }}<span v-if="connectedNames.length" class="badge badge--count">{{ connectedNames.length }}</span></h2>
          <div v-if="connectedNames.length" class="overview-chips">
            <button v-for="name in connectedNames" :key="name" class="overview-chip overview-chip--link"
              :title="t('设备')" @click="emit('devices')">
              <span class="overview-dot overview-dot--on" aria-hidden="true"></span>{{ name }}
            </button>
          </div>
          <p v-else class="muted small">{{ t("还没有已连接的设备——复制内容并配对一台设备即可开始同步。") }}</p>
          <div class="overview-actions">
            <button class="overview-button" :disabled="busy" @click="emit('qr')"><QrCode :size="16" />{{ t("显示二维码") }}</button>
            <button class="overview-button" :disabled="busy" @click="emit('send-url')"><SendHorizontal :size="16" />{{ t("发送链接到设备") }}</button>
          </div>
        </section>
      </div>

      <section class="overview-card">
        <!-- The way into the history is on the card, not on its rows.  A row
             used to be a link to the history page, which cost the reader a page
             change for the thing they most often wanted — the clip itself —
             and left the right-click doing nothing at all.  The row copies
             now, and 查看全部 is the route it gave up. -->
        <div class="overview-card-head">
          <h2>{{ t("最近活动") }}</h2>
          <button class="overview-button" @click="emit('history')">{{ t("查看全部") }}</button>
        </div>
        <ul v-if="overview.recent_items.length" class="overview-feed">
          <li v-for="item in overview.recent_items" :key="item.id">
            <button class="overview-feed-row" type="button" :title="t('复制到剪贴板')"
              @click="copyRecent(item)" @contextmenu.prevent="emit('row-menu', $event, item)">
              <component :is="typeIcon(item.content_type)" :size="15" aria-hidden="true" />
              <span class="overview-feed-text">{{ previewText(item.preview) || t("（无内容）") }}</span>
              <!-- The mark's box is here whether or not there is a mark in it.  A
                   `v-if` with no `v-else` leaves no element behind, and the grid
                   hands that column to the next child — the age — which is how
                   the age ended up 14px wide and standing on end. -->
              <span class="overview-feed-mark">
                <Check v-if="state.copiedId === item.id" :size="13" class="overview-feed-copied" :aria-label="t('已复制')" />
                <Pin v-else-if="item.pinned" :size="13" :aria-label="t('已置顶')" />
              </span>
              <span class="muted small overview-feed-age">{{ ago(item.timestamp) }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="muted small">{{ t("复制文本、图片和文件时，剪贴板活动将显示在这里。") }}</p>
      </section>
    </template>
  </section>
</template>
