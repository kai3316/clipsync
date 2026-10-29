<script setup lang="ts">
import { X } from "@lucide/vue";
import { t } from "../i18n";
import { createApplicationStore, type NoticeAction } from "../stores/application";

// The store keys a notice by its runtime event name (machine-readable and
// stable across languages); the legacy app toasted a translated title, so the
// mapping lives here, next to the catalog it translates.
//
// The `ui.*` keys are not events: they are the window's own answers to its own
// buttons — a copy, a refresh, a save — which the store raises for the clicks
// that change nothing a reader can see. They name the page the click was on,
// the same way an event names the subsystem it came from.
const props = defineProps<{ store: ReturnType<typeof createApplicationStore> }>();

/** Let the countdown go, but only when focus has really left the notice.
 *  `focusout` also fires when the close button inside it takes focus, and
 *  resuming there would restart a clock the reader is still standing on. */
function focusLeft(id: number, event: FocusEvent) {
  const next = event.relatedTarget as Node | null;
  const here = event.currentTarget as HTMLElement | null;
  if (next && here?.contains(next)) return;
  props.store.resumeNotice(id);
}

/** Run a notice's button, and take the notice down first.
 *
 *  Closing before the call is deliberate: every button on the stack is an
 *  answer that ends the question, and one left behind waiting for a round trip
 *  invites a second click on an invitation that is already answered.  The store
 *  owns what the button does — it is the same command the conversation's own
 *  banner runs — so a failure still lands on the error band and still says why.
 */
async function runAction(id: number, action: NoticeAction) {
  props.store.dismissNotice(id);
  await action.run();
}

/** The area an event belongs to, keyed by its name's first segment.  The table
 *  below is kept by hand, so an event can ship before it has a line here; a
 *  notice headed `log.rotated` would be a wire name on screen, and naming the
 *  subsystem it came from is the closest true thing left to say. */
const AREA_BY_PREFIX: [prefix: string, label: () => string][] = [
  ["device.", () => t("设备")],
  ["pairing.", () => t("配对请求")],
  ["netpair.", () => t("互联网配对")],
  ["update.", () => t("软件更新")],
  ["transfer.", () => t("文件传输")],
  ["file.", () => t("文件传输")],
  ["clip.", () => t("剪贴板同步")],
  ["chat.", () => t("附近聊天")],
  ["log.", () => t("设备日志")],
  ["sync.", () => t("剪贴板同步")],
  ["runtime.", () => t("错误")],
  ["url.", () => t("收到网址")],
];

/** Warn once per name: a stack can raise the same unmapped event on every
 *  poll, and a console full of one line is a console nobody reads. */
const warned = new Set<string>();

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
    case "ui.hotkey": return t("全局快捷键");
    case "runtime.error": return t("错误");
    case "pairing.request": return t("配对请求");
    case "pairing.failed": return t("配对请求");
    case "transfer.request": return t("文件传输");
    case "chat.message": return t("附近聊天");
    // An invitation, which is the one chat notice that asks a question: the
    // title names the area and the sentence names the device, so the two
    // buttons below it are read against the answer they belong to.
    case "chat.invite": return t("附近聊天");
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
    // Raised by the window itself after a repair pass, and named as a key like
    // every event above: this table is what words a notice, so a title that
    // arrived already translated could not be re-worded here.
    case "ui.data_recovery": return t("数据修复");
    // The two answers to an update this window asked a device for, or that
    // a peer offered: which device spoke and what it will do.  Both arrive
    // long after the click, and the title has to name the area rather than
    // show the wire name `update.peer_unavailable` on screen.
    case "update.peer_unavailable":
    case "update.peer_notice": return t("软件更新");
    // A log this window asked a device for: it arrived, the device keeps
    // its log to itself, or it could not be filed here.  All three are the
    // same row's answer, and a notice with no title is a machine string.
    case "log.collected":
    case "log.failed":
    case "log.unavailable": return t("设备日志");
    // An internet pairing completing on the other device, minutes after the
    // code was submitted from this one; the card re-reads its peers, and
    // this is what tells the reader which area moved.
    case "netpair.peer.changed": return t("互联网配对");
    // The three ways an internet pairing stops moving: a wait nobody answered,
    // a generated code that outlived its window, and two devices holding
    // different encryption passwords.  All are the same card's business, and a
    // notice that showed `netpair.waiting_failed` would be a wire name.
    case "netpair.waiting_failed":
    case "netpair.code.expired":
    case "netpair.decrypt_failed": return t("互联网配对");
    // A dial that started and then failed.  Under 连接设备, beside the refusal
    // it is the other half of: one is the peer saying no, this is the call not
    // reaching it.
    case "device.connect_failed": return t("连接设备");
    // A pairing request that ended without pairing — the row keeps the word for
    // a minute, and this is the same news said where the reader was looking.
    case "pairing.resolved": return t("配对请求");
    // The window's own key for the non-destructive identity alert: the device's
    // certificate is the pinned one, so there is nothing to trust or unpair and
    // it must not be titled like the certificate-change dialog.
    case "ui.device_identity": return t("设备身份未能验证");
    default: return areaTitle(name);
  }
}

/** Last resort for a name with no line above: name the area, never the wire
 *  name, and say so once in the console so the next build can add the line. */
function areaTitle(name: string): string {
  const dot = name.indexOf(".");
  const prefix = dot > 0 ? name.slice(0, dot + 1) : "";
  const area = AREA_BY_PREFIX.find(([head]) => head === prefix);
  if (import.meta.env.DEV && !warned.has(name)) {
    warned.add(name);
    console.warn(`[notices] no title for "${name}"; add it to NoticeStack's table`);
  }
  return area ? area[1]() : t("通知");
}
</script>

<template>
  <!-- The legacy webview showed these as toasts; they expire on their own (the
       store owns the timers), so this is a polite live region, not a dialog.
       The group is what fades one in: `appear` covers the first, which is the
       one that arrives along with the stack itself. -->
  <TransitionGroup v-if="props.store.state.notices.length" tag="ul" name="notice" appear
    class="notice-stack" role="status" aria-live="polite">
    <!-- A notice being read stops its own clock: hover, or tab to its close
         button, and the six seconds wait.  The two requests never start one —
         see `ACTION_NOTICES` — so they are still on screen when the reader comes
         back from the other window. -->
    <li v-for="notice in props.store.state.notices" :key="notice.id" class="notice strip"
      @mouseenter="props.store.pauseNotice(notice.id)" @mouseleave="props.store.resumeNotice(notice.id)"
      @focusin="props.store.pauseNotice(notice.id)" @focusout="focusLeft(notice.id, $event)">
      <div>
        <strong>{{ title(notice.title) }}</strong>
        <p class="small">{{ notice.message }}</p>
        <!-- The answers, where the question appeared.  Only a request carries
             them, and a request is exactly the notice that must not expire: the
             stack's own clock is what `actions` replaces. -->
        <div v-if="notice.actions?.length" class="notice-actions">
          <button v-for="action in notice.actions" :key="action.label" type="button"
            :class="{ primary: action.primary }" @click="runAction(notice.id, action)">{{ action.label }}</button>
        </div>
      </div>
      <button class="icon-button" :aria-label="t('关闭')" :title="t('关闭')" @click="props.store.dismissNotice(notice.id)">
        <X :size="16" />
      </button>
    </li>
  </TransitionGroup>
</template>
