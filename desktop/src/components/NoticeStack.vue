<script setup lang="ts">
import { X } from "@lucide/vue";
import { t } from "../i18n";
import { createApplicationStore } from "../stores/application";

// The store keys a notice by its runtime event name (machine-readable and
// stable across languages); the legacy app toasted a translated title, so the
// mapping lives here, next to the catalog it translates.
//
// The `ui.*` keys are not events: they are the window's own answers to its own
// buttons — a copy, a refresh, a save — which the store raises for the clicks
// that change nothing a reader can see. They name the page the click was on,
// the same way an event names the subsystem it came from.
const props = defineProps<{ store: ReturnType<typeof createApplicationStore> }>();

function title(name: string): string {
  switch (name) {
    case "ui.overview": return t("概览");
    case "ui.history": return t("剪贴板历史");
    case "ui.favorites": return t("收藏库");
    case "ui.transfers": return t("文件传输");
    case "ui.chat": return t("附近聊天");
    case "ui.devices": return t("设备");
    case "ui.ai": return t("AI 配置");
    case "ui.settings": return t("设置");
    case "ui.sync": return t("剪贴板同步");
    case "runtime.error": return t("错误");
    case "pairing.request": return t("配对请求");
    case "pairing.failed": return t("配对请求");
    case "transfer.request": return t("文件传输");
    case "chat.message": return t("附近聊天");
    case "chat.connect_timeout": return t("附近聊天");
    case "url.received": return t("收到网址");
    case "device.connected": return t("设备已连接");
    case "device.disconnected": return t("设备已断开");
    case "device.connection_rejected": return t("连接设备");
    case "device.connection_unreachable": return t("连接设备");
    case "sync.redacted": return t("剪贴板同步");
    // A download this window asked for.  Under 文件传输 rather than 剪贴板历史:
    // what failed is a transfer, and the row that started it is on another page.
    case "clip.file.denied": return t("文件传输");
    case "device.security_alert": return t("设备身份变更");
    default: return name;
  }
}
</script>

<template>
  <!-- The legacy webview showed these as toasts; they expire on their own (the
       store owns the timers), so this is a polite live region, not a dialog.
       The group is what fades one in: `appear` covers the first, which is the
       one that arrives along with the stack itself. -->
  <TransitionGroup v-if="props.store.state.notices.length" tag="ul" name="notice" appear
    class="notice-stack" role="status" aria-live="polite">
    <li v-for="notice in props.store.state.notices" :key="notice.id" class="notice strip">
      <div>
        <strong>{{ title(notice.title) }}</strong>
        <p class="small">{{ notice.message }}</p>
      </div>
      <button class="icon-button" :aria-label="t('关闭')" :title="t('关闭')" @click="props.store.dismissNotice(notice.id)">
        <X :size="16" />
      </button>
    </li>
  </TransitionGroup>
</template>
