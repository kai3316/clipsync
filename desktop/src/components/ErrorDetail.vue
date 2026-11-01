<script setup lang="ts">
/**
 * The folded half of an error message.
 *
 * An error band says two things today: what happened, and what the sidecar
 * called it (`SIDECAR_UNAVAILABLE`, `ACTION_REJECTED`, `INVALID_SELECTION`).
 * Only the first is something a reader can act on, and it was being read as the
 * second line of every error this window shows.  The token still matters in
 * exactly one situation — a bug report — so it stays one click away rather than
 * gone, together with a button that puts it on the clipboard.  Retyping
 * `SIDECAR_UNAVAILABLE` from a screenshot is how reports lose the one detail
 * that identifies the failure.
 *
 * The copy confirmation is local rather than the window's toast: this widget is
 * used from panes that sit inside dialogs, and a toast stacking up behind a
 * modal is a confirmation nobody sees.
 */
import { onBeforeUnmount, ref } from "vue";
import { t } from "../i18n";

const props = defineProps<{ code: string; message?: string }>();

const copied = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

async function copy() {
  try {
    await navigator.clipboard.writeText(props.message ? `${props.code}\n${props.message}` : props.code);
    copied.value = true;
    clearTimeout(timer);
    timer = setTimeout(() => { copied.value = false; }, 2000);
  } catch { /* A refused clipboard leaves the code selectable on screen. */ }
}

onBeforeUnmount(() => clearTimeout(timer));
</script>

<template>
  <details class="error-detail">
    <summary>{{ t("详情") }}</summary>
    <div class="error-detail-body">
      <code>{{ code }}</code>
      <button type="button" class="link-button" @click="copy">{{ copied ? t("已复制") : t("复制诊断信息") }}</button>
    </div>
  </details>
</template>
