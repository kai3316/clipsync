<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { Bell, BellOff, Check, Clock, Copy, MessageCircle, Paperclip, RefreshCw, Send, X } from "@lucide/vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import { systemText } from "../i18n/chat";
import { shortTime, size } from "../i18n/format";
import { openContextMenu } from "../lib/context-menu";
import { announce } from "../lib/status";
import { copyText } from "../lib/clipboard";
import { chatReceipt, deliveryIcon, deliveryLabel, type DeliveryStatus } from "../stores/delivery";
import type { ChatEntry, ChatSession, Device } from "../api/types";

/** Relay receipts by msg_id, owned by the application store.
 *
 * Optional so the view still renders on its own (a test mount, or a host that
 * never reports delivery): with no map every bubble simply has no pill.
 */
const props = withDefaults(defineProps<{
  /** Say that a click did something, for the clicks this page cannot show.
   * The window owns the notice stack and hands one down; a page mounted
   * without it — a unit test — simply has nothing to report to. */
  notify?: (message: string) => void;
  receipts?: Record<string, DeliveryStatus>;
  /** A conversation to open on arrival, handed over by the device row's
   * 打开聊天.  The page keeps owning which session is selected — it is the one
   * polling the list — so this is a request it answers, not a selection. */
  openSession?: string;
}>(), {
  receipts: () => ({}),
  openSession: "",
});
const emit = defineEmits<{ opened: [] }>();
/** The receipt to stamp on a bubble, or null when there is nothing to say. */
function receipt(entry: ChatEntry) {
  return chatReceipt(entry, props.receipts);
}
const receiptGlyphs = { check: Check, x: X, clock: Clock };
function receiptGlyph(entry: ChatEntry) {
  const status = receipt(entry);
  return status ? deliveryIcon(status) : null;
}
function receiptText(entry: ChatEntry) {
  const status = receipt(entry);
  return status ? deliveryLabel(status) : "";
}

const devices = ref<Device[]>([]);
const sessions = ref<ChatSession[]>([]);
const messages = ref<ChatEntry[]>([]);
const muted = ref<string[]>([]);
/** Whether nearby devices may send without asking — see `ChatSessionsPage`. */
const openToAll = ref(true);
const selectedId = ref("");
const draft = ref("");
const busy = ref(false);
const error = ref("");
// Not a failure and not an error: an invite whose link is still being dialed.
// It reads in the same band as one because the reader is looking at the same
// place, but it must not look like something went wrong.
const notice = ref("");
const attachmentError = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
let timer: ReturnType<typeof setInterval> | undefined;
let typingTimer: ReturnType<typeof setTimeout> | undefined;
const selected = computed(() => sessions.value.find((s) => s.session_id === selectedId.value));

/** The statuses a conversation is still live in — see `nearby`. */
const LIVE_STATUS = ["active", "inviting", "invited"];
/**
 * The devices this machine could start a conversation with.
 *
 * A device the reader is already talking to is left out: it is in the
 * conversation list below, where its row opens the same session this one would
 * hand back, and two rows for one device is how a list stops being readable.
 * A *closed* conversation does not count — that device is free to talk to
 * again, and dropping it from this list would leave no way to say so.
 */
const nearby = computed(() => devices.value.filter((device) =>
  !sessions.value.some((s) => s.peer_id === device.id && LIVE_STATUS.includes(s.status))));
const messageList = ref<HTMLElement | null>(null);
/** Whether the list is sitting at the newest message — see `trackScroll`. */
let following = true;

function label(s: ChatSession) {
  return ({ active: s.online ? t("聊天中") : t("离线"), inviting: t("邀请中"), invited: t("待确认"),
    declined_remote: t("已拒绝"), closed: t("已关闭") } as Record<string, string>)[s.status] || s.status;
}
function text(entry: ChatEntry) {
  return entry.kind === "text" ? entry.text : entry.kind === "file"
    ? t("{who}：{name}", { who: entry.outgoing ? t("发送") : t("接收"), name: entry.file_name })
    // A system row carries a dotted key, not a sentence — the session on
    // screen supplies the peer's name, which the payload leaves out.
    : systemText(entry, selected.value?.peer_name || "");
}
/**
 * The word for a file transfer's state, as the other two fronts word it.
 *
 * The wire values are the ones `ChatEntry.status` documents, plus the failure
 * reasons the transfer layer can end on — all of which the reader should see as
 * a failure rather than as a blank.  Mirrors `chatFileStatus` in
 * `internal/web/static/mobile.html`, so one transfer reads the same on the
 * phone, the web panel and here.
 */
function fileStatus(entry: ChatEntry) {
  const failed = ["failed", "peer_offline", "error_timeout", "error_size_mismatch",
    "error_security", "error_disk"];
  if (entry.status === "done" || entry.status === "completed" || entry.status === "success") {
    return entry.outgoing ? t("已发送") : t("已接收");
  }
  if (entry.status === "sending") return entry.outgoing ? t("发送中…") : t("接收中…");
  if (entry.status === "pending") return t("准备中…");
  if (entry.status === "await_accept") return t("等待接受");
  if (entry.status === "declined" || entry.status === "rejected") return t("已拒绝");
  if (entry.status === "cancelled" || entry.status === "cancelled_by_peer") return t("已取消");
  if (failed.includes(entry.status)) return t("失败");
  return "";
}

/** The size and state line under a file bubble. */
function fileMeta(entry: ChatEntry) {
  return [size(entry.file_size), fileStatus(entry)].filter(Boolean).join(" · ");
}

/** How full the in-flight progress bar is, clamped to the bar. */
function filePercent(entry: ChatEntry) {
  const fraction = Math.max(0, Math.min(1, Number(entry.fraction) || 0));
  return `${Math.round(fraction * 100)}%`;
}
async function refresh() {
  try {
    const [deviceResult, result] = await Promise.all([bridge.chatDevices(), bridge.chatSessions()]);
    devices.value = deviceResult.devices || [];
    sessions.value = result.sessions || [];
    muted.value = result.muted || [];
    openToAll.value = result.open_to_all !== false;
    if (!selectedId.value && sessions.value.length) await select(sessions.value[0]);
    if (selectedId.value) await load(selectedId.value);
    error.value = "";
    notice.value = "";
  } catch (reason: any) { error.value = reason?.message || t("无法读取聊天会话"); }
}
/** The refresh button, which says what it found.
 *
 * The same read runs every 1.5s to keep the page live, and that one has to stay
 * silent — a toast twice a second would be the loudest thing in the window — so
 * the report belongs to the button rather than to the read.
 */
async function refreshNow() {
  await refresh();
  props.notify?.(sessions.value.length
    ? t("已刷新 · {count} 个会话", { count: sessions.value.length })
    : t("已刷新 · 暂无会话"));
}
async function toggleMute(session: ChatSession) {
  const next = !muted.value.includes(session.peer_id);
  try {
    const result = await bridge.setChatMuted(session.peer_id, next);
    muted.value = result.muted || muted.value;
    // The row's bell says the same thing by its own shape, but the menu is
    // where the legacy panel reported it in words, and a click on a menu entry
    // that only flips a distant icon reads as having done nothing.
    announce(next ? t("已静音此设备的消息通知") : t("已恢复此设备的消息通知"));
  } catch (reason: any) { error.value = reason?.message || t("设置静音失败"); }
}
/** The right-click menu on a conversation, in the legacy menu's own order.
 *
 * The bell on the row and the X on the conversation header are the same two
 * actions; 标记为已读 is offered only when there is something to mark, because
 * a conversation with no unread count has nothing for it to do.
 */
function sessionMenu(event: MouseEvent, session: ChatSession) {
  const isMuted = muted.value.includes(session.peer_id);
  openContextMenu(event, [
    {
      id: "mute", label: isMuted ? t("取消静音") : t("静音"),
      icon: isMuted ? Bell : BellOff,
      run: () => toggleMute(session),
    },
    (session.unread || 0) > 0
      ? { id: "read", label: t("标记为已读"), icon: Check, run: () => markRead(session) }
      : null,
    {
      id: "close", label: t("关闭会话"), icon: X, divider: true, danger: true,
      run: () => close(session),
    },
  ]);
}

/** The right-click menu on a message.
 *
 * One entry, which is the whole of what a message can offer: the text on this
 * machine's clipboard.  A file message copies the name it arrived under,
 * because there is no text to copy and the name is what the reader would have
 * selected by hand.  A system line has neither, so it gets no menu at all —
 * the same silence the legacy menu kept for a target it could not copy.
 */
function messageMenu(event: MouseEvent, entry: ChatEntry) {
  const value = entry.kind === "text" ? entry.text : (entry.file_name || "");
  if (!value?.trim()) return;
  openContextMenu(event, [
    { id: "copy", label: t("复制"), icon: Copy, run: () => copyText(value) },
  ]);
}

async function load(id: string) {
  try {
    const result = await bridge.chatMessages(id);
    if (selectedId.value === id) messages.value = result.messages || [];
  }
  catch (reason: any) { error.value = reason?.message || t("无法读取聊天消息"); }
}
async function select(session: ChatSession) {
  messages.value = [];
  attachmentError.value = "";
  selectedId.value = session.session_id;
  // A conversation opens at its newest message, whichever one was left behind.
  following = true;
  await load(session.session_id);
  if (session.unread) await markRead(session);
}
/** The message list is a scroll container with a poll behind it, so it can move
 *  for two reasons that want opposite things: a message arriving should carry
 *  the view down to it, and a reader who has scrolled back through the
 *  conversation must not be yanked away from what they are reading.  A scroll
 *  listener is what tells the two apart — anything within a bubble's height of
 *  the end counts as following along, which is also where jumping to the bottom
 *  leaves it — and the list follows only while it is following.
 *
 *  The legacy panel jumped to the newest on every poll, unconditionally, and
 *  that was the only thing it could do: it destroyed and rebuilt the transcript
 *  each time, so a reader who had scrolled back lost their place either way.
 *  Landing on the newest message is the part that has to match; not moving under
 *  someone's hand is what rebuilding was costing. */
const STICK_SLACK = 40;
function trackScroll() {
  const list = messageList.value;
  if (!list) return;
  following = list.scrollHeight - list.scrollTop - list.clientHeight <= STICK_SLACK;
}
function toNewest() {
  const list = messageList.value;
  if (list && following) list.scrollTop = list.scrollHeight;
}
/** Clear one conversation's unread count, without opening it.
 *
 * The row's own badge disappears on selection; the menu's 标记为已读 has to do
 * the same for a conversation the reader is not looking at, which is the whole
 * point of it — a session that was noisy overnight is marked off from the list.
 * The count is cleared locally first so the badge goes at once, and the request
 * is fire-and-forget: a mark that fails is corrected by the next poll, which
 * carries the real count back.
 */
async function markRead(session: ChatSession) {
  session.unread = 0;
  await bridge.markChatRead(session.session_id).catch(() => {});
}
async function invite(session: ChatSession) {
  busy.value = true;
  try {
    const result = await bridge.inviteChat(session.peer_id, session.peer_name);
    // A session id means the link was already up and the conversation is open;
    // otherwise the invite is dialing, and the row appears on its own when it
    // answers — the poll is what puts it in the list, so there is nothing to
    // select yet.
    if (result.chat_session_id) selectedId.value = result.chat_session_id;
    else notice.value = t("正在连接对方，连接上以后会话会出现在列表里。");
    await refresh();
  }
  catch (reason: any) { error.value = reason?.message || t("发送邀请失败"); }
  finally { busy.value = false; }
}
/** Answer an invitation this user was asked to decide on. */
async function answer(session: ChatSession, accepted: boolean) {
  busy.value = true;
  try {
    const result = await (accepted ? bridge.acceptChatInvite(session.session_id) : bridge.declineChatInvite(session.session_id));
    if (!result.ok) throw new Error(t("操作未完成"));
    await refresh();
  } catch (reason: any) { error.value = reason?.message || t("处理邀请失败"); }
  finally { busy.value = false; }
}
async function send() {
  const session = selected.value;
  const value = draft.value.trim();
  if (!session || session.status !== "active" || !value || busy.value) return;
  busy.value = true;
  try {
    if (!(await bridge.sendChatText(session.session_id, value)).ok) throw new Error(t("消息未送达"));
    draft.value = ""; await load(session.session_id); await refresh();
  } catch (reason: any) { error.value = reason?.message || t("发送消息失败"); }
  finally { busy.value = false; }
}
async function resend(entry: ChatEntry) {
  if (!selected.value || busy.value) return;
  busy.value = true;
  try { await bridge.resendChatText(selected.value.session_id, entry.entry_id); await load(selected.value.session_id); }
  catch (reason: any) { error.value = reason?.message || t("重发消息失败"); }
  finally { busy.value = false; }
}
async function fileAction(entry: ChatEntry, action: "accept" | "decline" | "cancel") {
  if (!selected.value || busy.value) return;
  busy.value = true;
  try {
    const result = await bridge.chatFileAction(action, selected.value.session_id, entry.transfer_id);
    if (!result.ok) throw new Error(t("附件操作未完成"));
    await load(selected.value.session_id);
  } catch (reason: any) { error.value = reason?.message || t("附件操作失败"); }
  finally { busy.value = false; }
}
async function openFile(entry: ChatEntry) {
  const sessionId = selectedId.value;
  if (!selected.value || busy.value || entry.kind !== "file" || entry.outgoing
    || !entry.transfer_id || !entry.saved_path
    || !["done", "completed", "success"].includes(entry.status)) return;
  busy.value = true;
  attachmentError.value = "";
  try {
    const result = await bridge.openChatFile(sessionId, entry.transfer_id);
    if (!result.ok) throw new Error(t("无法打开附件"));
  } catch (reason: any) {
    if (selectedId.value === sessionId) attachmentError.value = reason?.message || t("无法打开附件");
  } finally { busy.value = false; }
}
/**
 * Show a received attachment in this machine's file manager.
 *
 * The legacy chat panel had this beside 打开 (`chat-panel.js`, its
 * 打开所在文件夹 button calling `revealFile(m.saved_path)`), and it is the
 * gentler of the two: opening hands the file to whatever app owns its type,
 * while revealing only points at where it landed — which is what someone
 * wants when they are about to move or rename it.
 */
async function revealFile(entry: ChatEntry) {
  const sessionId = selectedId.value;
  if (!selected.value || busy.value || entry.kind !== "file" || entry.outgoing
    || !entry.transfer_id || !entry.saved_path
    || !["done", "completed", "success"].includes(entry.status)) return;
  busy.value = true;
  attachmentError.value = "";
  try {
    const result = await bridge.revealChatFile(sessionId, entry.transfer_id);
    if (!result.ok) throw new Error(t("无法打开附件所在文件夹"));
  } catch (reason: any) {
    if (selectedId.value === sessionId) attachmentError.value = reason?.message || t("无法打开附件所在文件夹");
  } finally { busy.value = false; }
}
/** Close a conversation — the header's X names the selected one, the menu's
 * entry names whichever row was right-clicked, and both land here. */
async function close(session: ChatSession) {
  if (!session || busy.value) return;
  busy.value = true;
  try { if (!(await bridge.closeChat(session.session_id)).ok) throw new Error(t("会话未关闭")); await refresh(); }
  catch (reason: any) { error.value = reason?.message || t("关闭会话失败"); }
  finally { busy.value = false; }
}
async function sendFile() {
  const session = selected.value;
  const path = await bridge.chooseFile();
  if (!session || !path) return;
  busy.value = true;
  try { await bridge.sendChatFile(session.session_id, path); await load(session.session_id); }
  catch (reason: any) { error.value = reason?.message || t("附件发送失败"); }
  finally { busy.value = false; }
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
}
function typing() {
  const session = selected.value;
  if (!session || session.status !== "active") return;
  void bridge.sendChatTyping(session.session_id, true).catch(() => {});
  if (typingTimer) clearTimeout(typingTimer);
  typingTimer = setTimeout(() => {
    void bridge.sendChatTyping(session.session_id, false).catch(() => {});
  }, 1800);
}
/** Open the conversation another page asked for, once it exists.
 *
 * The request arrives before the first poll has answered, so it is held until
 * the session shows up in the list — a session id that the relay has not
 * reported yet is one that cannot be opened, and dropping the request on the
 * first empty list would lose it.  `immediate` because a device row on the
 * devices page can be right-clicked before this page has ever been drawn.
 */
watch([() => props.openSession, sessions], ([wanted]) => {
  if (!wanted) return;
  const match = sessions.value.find((session) => session.session_id === wanted);
  if (!match) return;
  void select(match);
  emit("opened");
}, { immediate: true });

// `post`, because the height that matters is the one the new messages give the
// list — the poll replaces the array on every tick, so this is also what keeps
// a conversation pinned to its newest message while it is arriving.
watch(messages, toNewest, { flush: "post" });

onMounted(() => { void refresh(); timer = setInterval(() => void refresh(), 1500); });
onUnmounted(() => { if (timer) clearInterval(timer); if (typingTimer) clearTimeout(typingTimer); });
</script>

<template>
  <section class="chat-view">
    <p v-if="error" class="error-band" role="alert">{{ error }}</p>
    <p v-if="notice" class="status-band" role="status">{{ notice }}</p>
    <p v-if="attachmentError" class="error-band" role="alert">{{ attachmentError }}</p>

    <!-- The devices and the conversations share one rail, devices first.
         They were two tabs, and then two sections of one rail with the devices
         underneath every conversation; both put the way to start a chat
         somewhere the reader had to already know about.  Above the
         conversations, the devices are the first thing the page says, and the
         rail scrolls as one list rather than the device list scrolling past
         under it. -->
    <div class="chat-layout">
      <aside class="chat-sessions">
        <div class="chat-heading">
          <h2>{{ t("附近设备") }}</h2>
          <button class="icon-button" :aria-label="t('刷新聊天')" :title="t('刷新聊天')" @click="refreshNow"><RefreshCw :size="17" /></button>
        </div>
        <button v-for="device in nearby" :key="`chat-${device.id}`" class="chat-device"
          :disabled="busy || device.connection_state === 'offline'"
          :title="device.connection_state === 'offline' ? t('{name} 当前不在线', { name: device.name }) : undefined"
          @click="invite({ peer_id: device.id, peer_name: device.name } as ChatSession)">
          <MessageCircle :size="16" /><span>{{ device.name }}</span>
          <small>{{ device.connection_state === 'offline' ? t("离线") : device.paired ? t("已配对") : t("未配对") }}</small>
        </button>
        <p v-if="!nearby.length" class="chat-nearby-empty">{{ t("附近没有可聊天的设备。") }}</p>

        <h3 class="chat-section">{{ t("会话") }}</h3>
        <div v-for="session in sessions" :key="session.session_id" class="chat-session"
          :class="{ active: selectedId === session.session_id }" role="button" tabindex="0"
          @click="select(session)" @keydown.enter="select(session)" @contextmenu.prevent="sessionMenu($event, session)">
          <MessageCircle :size="17" />
          <span><strong>{{ session.peer_name }}</strong><small>{{ label(session) }} · {{ session.last_preview || t("暂无消息") }}<template v-if="session.last_activity_ts"> · {{ shortTime(session.last_activity_ts) }}</template></small></span>
          <button class="icon-button" :aria-label="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" :title="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" @click.stop="toggleMute(session)"><BellOff v-if="muted.includes(session.peer_id)" :size="14" /><Bell v-else :size="14" /></button>
          <b v-if="session.unread">{{ session.unread }}</b>
        </div>
        <div v-if="!sessions.length" class="empty"><MessageCircle :size="30" /><p>{{ t("暂无聊天会话") }}</p></div>
      </aside>

      <div class="chat-conversation" v-if="selected">
        <header class="chat-heading">
          <div><h2>{{ selected.peer_name }}</h2><small class="muted">{{ t("{label} · 指纹 {code}", { label: selected.peer_typing ? t('对方正在输入…') : label(selected), code: selected.fingerprint_short || t('未提供') }) }}</small></div>
          <button class="icon-button" :aria-label="t('关闭会话')" :title="t('关闭会话')" @click="close(selected)"><X :size="18" /></button>
        </header>

        <div v-if="selected.status === 'invited'" class="chat-invite">
          <strong>{{ t("{name} 邀请你聊天", { name: selected.peer_name }) }}</strong><span>{{ t("请核对指纹后决定。") }}</span>
          <button class="primary" @click="answer(selected, true)" :disabled="busy"><Check :size="16" />{{ t("接受") }}</button>
          <button @click="answer(selected, false)" :disabled="busy">{{ t("拒绝") }}</button>
        </div>
        <div v-if="selected.status === 'inviting'" class="chat-invite">{{ t("正在等待对方接受邀请…") }}</div>

        <div ref="messageList" class="chat-messages" aria-live="polite" @scroll.passive="trackScroll">
          <div v-if="!messages.length" class="empty"><MessageCircle :size="28" /><p>{{ t("还没有消息") }}</p></div>
          <article v-for="entry in messages" :key="entry.entry_id" class="chat-message"
            :class="{ outgoing: entry.outgoing, system: entry.kind === 'system' }"
            @contextmenu.prevent="messageMenu($event, entry)">
            <span>{{ text(entry) }}</span>
            <small v-if="entry.kind === 'file'" class="chat-file-meta">{{ fileMeta(entry) }}</small>
            <!-- How far along a file is: the status word above says it is moving,
                 the bar says how fast it is getting there.  Decorative — the
                 word is the accessible form. -->
            <div v-if="entry.kind === 'file' && entry.status === 'sending'" class="chat-file-progress" aria-hidden="true">
              <i :style="{ width: filePercent(entry) }"></i>
            </div>
            <small>{{ shortTime(entry.ts) }}</small>
            <!-- How a relayed message ended, stamped on the bubble it belongs
                 to: the same delivered/failed/queued wording as the device's
                 send list, so one message reads the same in both places. -->
            <span v-if="receiptGlyph(entry)" class="chat-delivery" :class="`chat-delivery--${receipt(entry)}`">
              <component :is="receiptGlyphs[receiptGlyph(entry)!]" :size="12" />{{ receiptText(entry) }}
            </span>
            <div v-if="entry.kind === 'file' && entry.transfer_id" class="file-actions">
              <button v-if="!entry.outgoing && ['done','completed','success'].includes(entry.status) && entry.saved_path" :disabled="busy" @click="openFile(entry)">{{ t("打开") }}</button>
              <button v-if="!entry.outgoing && ['done','completed','success'].includes(entry.status) && entry.saved_path" :disabled="busy" @click="revealFile(entry)">{{ t("打开所在文件夹") }}</button>
              <button v-if="!openToAll && !entry.outgoing && ['pending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'accept')"><Check :size="14" />{{ t("接受") }}</button>
              <button v-if="!openToAll && !entry.outgoing && ['pending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'decline')"><X :size="14" />{{ t("拒绝") }}</button>
              <button v-if="entry.outgoing && ['pending','sending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'cancel')"><X :size="14" />{{ t("取消") }}</button>
            </div>
            <span v-if="entry.kind === 'text' && entry.outgoing && entry.status === 'failed'" class="chat-undelivered">{{ t("未送达") }}</span>
            <button v-if="entry.kind === 'text' && entry.outgoing && entry.status === 'failed'" class="icon-button" :aria-label="t('重发消息')" :title="t('重发消息')" @click="resend(entry)"><RefreshCw :size="14" /></button>
          </article>
        </div>

        <form v-if="selected.status === 'active'" class="chat-composer" @submit.prevent="send">
          <textarea v-model="draft" :aria-label="t('聊天消息')" maxlength="16000" :placeholder="t('输入消息，按 Enter 发送')" @keydown="keydown" @input="typing" />
          <button type="button" class="icon-button" :aria-label="t('发送附件')" :title="t('发送附件')" :disabled="busy" @click="sendFile"><Paperclip :size="18" /></button>
          <button class="primary icon-button" :aria-label="t('发送消息')" :title="t('发送消息')" :disabled="!draft.trim() || busy"><Send :size="18" /></button>
        </form>
      </div>
      <!-- Nothing selected, which on a machine that has never chatted is the
           page's first sight.  It names the device list rather than assuming
           the reader has found it. -->
      <div v-else class="empty chat-empty">
        <MessageCircle :size="42" />
        <h2>{{ t("选择一个会话") }}</h2>
        <p>{{ t("或者点左边的设备，开始一段新对话。") }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* The chat page is a page like any other inside `.content`: it takes the height
   it is given and scrolls its own message list.

   Each part of that is a rule of its own here, because the height has to be
   handed down the whole way.  `.chat-view` carries no floor of its own, so as a
   flex item it refused to shrink below its content and the pane grew past the
   window instead of the list scrolling inside it — a conversation of any real
   length ran off the bottom of the page and took the composer with it.
   `.chat-layout`'s single row was implicit (`auto`), so the conversation was
   sized by its messages rather than by the window, and the `1fr` inside it had
   no height left to divide.  Both are the same mistake one level apart: a height
   taken from the content it was meant to bound. */
.chat-view { flex: 1; min-height: 0; min-width: 0; display: flex; flex-direction: column; }
.chat-layout { display: grid; grid-template-columns: minmax(220px, 280px) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); flex: 1; min-height: 560px; margin: 14px var(--page-gutter) 28px; border: 1px solid var(--line); border-radius: var(--radius-lg); overflow: hidden; }
.chat-conversation { display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto; min-width: 0; min-height: 0; }
/* The four bands, pinned to their tracks rather than placed by document order.
   The invite band is optional — a session that is already active renders none —
   and under auto-placement its absence moved every band below it up one row: the
   message list took the second `auto` row and grew to its full content height
   instead of scrolling in the `1fr`, and the composer landed in the `1fr`, where
   a textarea that stretches to fill its row drew 125px of empty box under a
   44px field with the two send buttons stranded at its top. */
.chat-conversation > .chat-heading { grid-row: 1; border-bottom: 1px solid var(--line); }
.chat-conversation > .chat-invite { grid-row: 2; }
.chat-conversation > .chat-messages { grid-row: 3; }
.chat-conversation > .chat-composer { grid-row: 4; }
.chat-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; }
.chat-heading h2 { margin: 0; font-size: 17px; }
/* The rail: the devices this machine could talk to, then the conversations it
   has.  One scrolling list rather than two, with the devices on top — reaching
   them was the whole complaint, and putting them under every conversation is
   what made reaching them cost a scroll through the chats. */
.chat-sessions { border-right: 1px solid var(--line); padding: 8px; overflow: auto; }
/* The heading of the second section.  Smaller than the rail's own 附近设备 and
   quieter, because it labels what is under it rather than opening the page. */
.chat-section { margin: 14px 0 2px; padding: 0 8px; font-size: 13px; font-weight: 600; color: var(--muted); }
.chat-nearby-empty { margin: 2px 0 0; padding: 4px 8px 8px; color: var(--muted); font-size: 13px; }
.chat-session { width: 100%; display: flex; align-items: center; gap: 9px; padding: 11px 8px; text-align: left; border: 0; border-bottom: 1px solid var(--line); background: transparent; }
.chat-session.active { background: var(--accent-soft); color: var(--accent-text); }
.chat-session span { min-width: 0; flex: 1; display: grid; gap: 3px; }
.chat-session small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.chat-session b { background: var(--accent-solid); color: var(--on-accent); border-radius: 9px; padding: 2px 6px; font-size: 11px; }
.chat-device { width: 100%; display: flex; align-items: center; gap: 8px; padding: 9px 8px; border: 0; border-bottom: 1px solid var(--line); background: transparent; text-align: left; }
.chat-device span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-device small { color: var(--muted); }
.chat-device:disabled { opacity: .5; cursor: not-allowed; }
/* The band above the messages that offers the pairing, when there is one. */
.chat-invite { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 12px 16px; background: var(--surface); }
.chat-invite span { flex: 1 1 100%; }
.chat-messages { padding: 18px; overflow: auto; display: flex; flex-direction: column; gap: 9px; }
/* A bubble: three lines of text and the time under them. */
.chat-message { align-self: flex-start; max-width: 75%; padding: 9px 12px; border: 1px solid var(--line); border-radius: var(--radius-lg); display: grid; gap: 3px; overflow-wrap: anywhere; }
.chat-message.outgoing { align-self: flex-end; background: var(--accent-soft); border-color: var(--accent-line); }
/* A system line is not a message from either side, so it carries no bubble. */
.chat-message.system { align-self: center; border-color: transparent; background: transparent; color: var(--muted); font-size: 12px; }
.chat-message small { text-align: right; color: var(--muted); }
/* The size and state line belongs with the name, not with the clock. */
.chat-file-meta { text-align: left; }
.chat-file-progress { height: 4px; border-radius: 2px; background: var(--line); overflow: hidden; }
.chat-file-progress i { display: block; height: 100%; background: var(--accent-solid); transition: width 0.2s linear; }
.chat-undelivered { justify-self: end; font-size: 11px; color: var(--danger-text); }
/* The relay receipt on an outgoing message: right-aligned under the time, the
   way the legacy bubble carried its pill, and in the muted scale because it is
   a footnote on the message rather than part of it — except for the two states
   worth noticing, which take the accent and the danger colour. */
.chat-delivery { justify-self: end; display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--muted); }
.chat-delivery--delivered { color: var(--accent-text); }
.chat-delivery--failed { color: var(--danger-text); }
/* The accept/decline/open buttons under a file message. */
.file-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 3px; }
.chat-composer { display: flex; gap: 10px; padding: 14px 16px; border-top: 1px solid var(--line); }
/* The composer is not inside a card and not inside a dialog, so it carried none
   of the chrome the rest of the window gives a field: it was a bare area of the
   page's own surface with a caret in it, and nothing on screen said where a
   message could be typed or how long a box it had. */
.chat-composer textarea { flex: 1; min-height: 44px; max-height: 40vh; resize: vertical; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); padding: 10px 12px; line-height: 1.55; }
.chat-composer textarea::placeholder { color: var(--muted); }
.chat-empty { min-height: 100%; }
/* Narrow: the session list goes above the conversation rather than beside it.
   The rows are spelled out for the same reason the bands inside the conversation
   are: stacked, the conversation is the row that has to take what is left, and
   left to `auto` it would take its content's height instead. */
@media (max-width: 700px) {
  .chat-layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  /* The rail on top, and taller than it was: it carries the device list too,
     and a 220px window onto both sections shows the devices and nothing of the
     conversations they were meant to introduce. */
  .chat-sessions { border-right: 0; border-bottom: 1px solid var(--line); max-height: 46vh; }
}
</style>
