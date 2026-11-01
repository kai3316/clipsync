<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { Search } from "@lucide/vue";
import { bridge } from "../api/bridge";
import type { FavoriteSummary, HistoryItem } from "../api/types";
import { t } from "../i18n";
import { previewLabel } from "../lib/history-label";

/**
 * One box that looks in both lists.
 *
 * The window has three searches already — the history page's, the library's,
 * and the settings rail's — and each of them only knows about the page it sits
 * on: a reader who half-remembers a snippet cannot tell whether it was a clip
 * they never kept or a favourite they saved, so the first thing the search asks
 * them is a question they cannot answer.  This asks for the word instead, and
 * puts the two lists behind a scope switch: one box, two answers, and the same
 * interfaces the two pages already use (the audit's instruction was to reuse
 * them rather than build a unified query).
 *
 * What it deliberately does **not** do is become the list.  It shows a handful
 * of rows to confirm the reader is looking in the right place, and one button
 * (or Enter) hands the word over to that page's own search, which is where the
 * full set, the filters and the actions live.  A palette that grew paging,
 * filters and per-row actions would be the second copy of both pages.
 *
 * The AI page and the device list are out of scope on purpose: neither is a
 * keyword index — one is a file tree, the other a live presence list — and
 * searching them would mean inventing a query for each.
 */
type Scope = "history" | "favorites";

/** How many rows the palette shows, and how long it waits for typing to stop.
 *  Six because the point is to say which list holds the word, not to be the
 *  list: past a handful of rows the answer stops being "it's here" and starts
 *  being the page. */
const ROWS = 6;
const DEBOUNCE_MS = 200;

const emit = defineEmits<{ open: [scope: Scope, query: string]; close: [] }>();

const query = ref("");
const scope = ref<Scope>("history");
const rows = ref<Array<{ id: string; text: string; detail: string }>>([]);
const total = ref(0);
const loading = ref(false);
const failed = ref("");
/** Which answer is the current one: a slow request that lands after a newer one
 *  must not paint over it, and the same counter drops the answer a reset
 *  invalidated. */
let generation = 0;
let timer: ReturnType<typeof setTimeout> | undefined;

const term = computed(() => query.value.trim());
/** The two scopes, named the way the two pages are named: the switch says where
 *  the box looks, and a scope called something the sidebar does not call it is a
 *  second name for a page the reader already knows. */
const scopes = computed<Array<{ id: Scope; label: string }>>(() => [
  { id: "history", label: t("剪贴板历史") },
  { id: "favorites", label: t("收藏库") },
]);
const scopeName = computed(() => (scope.value === "history" ? t("剪贴板历史") : t("收藏库")));
const openLabel = computed(() => t("在{scope}中查看全部 {count} 条", { scope: scopeName.value, count: total.value }));

/** A row on one line: newlines collapsed, cut at a column count rather than a
 *  byte count so a Chinese row is not sliced mid-character. */
function oneLine(raw: string): string {
  const flat = String(raw || "").replace(/\s+/g, " ").trim();
  return flat.length > 80 ? `${flat.slice(0, 80)}…` : flat;
}

async function run() {
  clearTimeout(timer);
  const mine = ++generation;
  const text = term.value;
  if (!text) {
    rows.value = [];
    total.value = 0;
    failed.value = "";
    loading.value = false;
    return;
  }
  loading.value = true;
  failed.value = "";
  try {
    if (scope.value === "history") {
      const page = await bridge.history(text, 0, ROWS, "", "");
      if (mine !== generation) return;
      rows.value = page.items.map((item: HistoryItem) => ({
        id: item.id,
        text: oneLine(previewLabel(item)),
        detail: oneLine(item.source_name || ""),
      }));
      total.value = Number(page.total || 0);
    } else {
      // No group: the count in this palette is every favourite that holds the
      // word, and handing over to a page still filtered to a group would show
      // fewer than the number the reader just read here.
      const page = await bridge.favorites(text, "", 0, ROWS);
      if (mine !== generation) return;
      rows.value = page.items.map((item: FavoriteSummary) => ({
        id: item.id,
        text: oneLine(item.title) || oneLine(item.preview) || "—",
        detail: oneLine(item.group),
      }));
      total.value = Number(page.total || 0);
    }
  } catch (error: any) {
    if (mine !== generation) return;
    rows.value = [];
    total.value = 0;
    failed.value = error?.message || t("搜索失败，请重试");
  } finally {
    if (mine === generation) loading.value = false;
  }
}

/** A word, not a keystroke: the box is typed into with an IME on Chinese, and
 *  each composition event is not a query anyone asked for. */
function schedule() {
  clearTimeout(timer);
  if (!term.value) {
    // Emptying the box is not a query with no results, it is no query: the
    // hint comes back rather than "nothing found".
    rows.value = [];
    total.value = 0;
    failed.value = "";
    loading.value = false;
    return;
  }
  timer = setTimeout(() => void run(), DEBOUNCE_MS);
}

/** A switch is a decision rather than typing, so it searches at once. */
function pick(id: Scope) {
  if (scope.value === id) return;
  scope.value = id;
  if (term.value) void run();
}

/** Enter, and the button under the rows: the word goes to the page it belongs
 *  to, which shows the full set with the actions that page has. */
function handOver() {
  if (!term.value) return;
  emit("open", scope.value, term.value);
}

watch(query, schedule);
onUnmounted(() => clearTimeout(timer));

/** Called when the dialog closes: a palette that reopens holding last time's
 *  word and last time's rows is one the reader has to clear before using. */
function reset() {
  clearTimeout(timer);
  ++generation;
  query.value = "";
  scope.value = "history";
  rows.value = [];
  total.value = 0;
  failed.value = "";
  loading.value = false;
}

defineExpose({ reset });
</script>

<template>
  <div class="quick-search">
    <label class="search quick-search-box">
      <Search :size="17" />
      <input
        v-model="query"
        type="search"
        class="search-input"
        autofocus
        :aria-label="t('搜索历史与收藏')"
        :placeholder="t('搜索历史与收藏…')"
        @keydown.enter.prevent="handOver"
      />
    </label>
    <div class="quick-search-scopes" role="group" :aria-label="t('搜索范围')">
      <button
        v-for="option in scopes"
        :key="option.id"
        type="button"
        :aria-pressed="scope === option.id"
        :class="{ active: scope === option.id }"
        @click="pick(option.id)"
      >{{ option.label }}</button>
    </div>
    <p v-if="!term" class="note">{{ t("输入关键词，在剪贴板历史和收藏里一起找。") }}</p>
    <p v-else-if="loading" class="muted" role="status">{{ t("正在搜索…") }}</p>
    <p v-else-if="failed" class="modal-error small" role="alert">{{ failed }}</p>
    <p v-else-if="!rows.length" class="note" role="status">
      {{ t("没有找到“{query}”", { query: term }) }}
    </p>
    <template v-else>
      <p class="quick-search-count" role="status">{{ t("找到 {count} 条", { count: total }) }}</p>
      <!-- Previews, not links: every row would land on the same page in the
           same state, so a row that acted on its own would be promising a
           difference the palette cannot deliver.  The one way out is the button
           below, which is what a reader picking any of these rows wants. -->
      <ul class="quick-search-rows">
        <li v-for="row in rows" :key="row.id">
          <span class="quick-search-text">{{ row.text }}</span>
          <span v-if="row.detail" class="quick-search-detail">{{ row.detail }}</span>
        </li>
      </ul>
      <button type="button" class="quick-search-open" @click="handOver">{{ openLabel }}</button>
    </template>
  </div>
</template>
