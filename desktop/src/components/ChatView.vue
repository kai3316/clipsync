<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Bell, BellOff, Check, Clock, MessageCircle, Paperclip, RefreshCw, Send, X } from "@lucide/vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import { chatReceipt, deliveryIcon, deliveryLabel, type DeliveryStatus } from "../stores/delivery";
import type { ChatEntry, ChatSession, Device } from "../api/types";

/** Relay receipts by msg_id, owned by the application store.
 *
 * Optional so the view still renders on its own (a test mount, or a host that
 * never reports delivery): with no map every bubble simply has no pill.
 */
const props = withDefaults(defineProps<{ receipts?: Record<string, DeliveryStatus> }>(), {
  receipts: () => ({}),
});
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

function label(s: ChatSession) {
  return ({ active: s.online ? t("聊天中") : t("离线"), inviting: t("邀请中"), invited: t("待确认"),
    declined_remote: t("已拒绝"), closed: t("已关闭") } as Record<string, string>)[s.status] || s.status;
}
function text(entry: ChatEntry) {
  return entry.kind === "text" ? entry.text : entry.kind === "file"
    ? t("{who}：{name}", { who: entry.outgoing ? t("发送") : t("接收"), name: entry.file_name })
    : entry.text || entry.text_key || t("系统消息");
}
function time(value: number) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(value * 1000);
}
async function refresh() {
  try {
    const [deviceResult, result] = await Promise.all([bridge.chatDevices(), bridge.chatSessions()]);
    devices.value = deviceResult.devices || [];
    sessions.value = result.sessions || [];
    muted.value = result.muted || [];
    if (!selectedId.value && sessions.value.length) await select(sessions.value[0]);
    if (selectedId.value) await load(selectedId.value);
    error.value = "";
    notice.value = "";
  } catch (reason: any) { error.value = reason?.message || t("无法读取聊天会话"); }
}
async function toggleMute(session: ChatSession) {
  const next = !muted.value.includes(session.peer_id);
  try {
    const result = await bridge.setChatMuted(session.peer_id, next);
    muted.value = result.muted || muted.value;
  } catch (reason: any) { error.value = reason?.message || t("设置静音失败"); }
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
  await load(session.session_id);
  if (session.unread) {
    await bridge.markChatRead(session.session_id).catch(() => {});
    session.unread = 0;
  }
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
async function close() {
  if (!selected.value || busy.value) return;
  busy.value = true;
  try { if (!(await bridge.closeChat(selected.value.session_id)).ok) throw new Error(t("会话未关闭")); await refresh(); }
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
onMounted(() => { void refresh(); timer = setInterval(() => void refresh(), 1500); });
onUnmounted(() => { if (timer) clearInterval(timer); if (typingTimer) clearTimeout(typingTimer); });
</script>

<template>
  <section class="chat-view">
    <p v-if="error" class="error-band" role="alert">{{ error }}</p>
    <p v-if="notice" class="status-band" role="status">{{ notice }}</p>
    <p v-if="attachmentError" class="error-band" role="alert">{{ attachmentError }}</p>
    <div class="chat-layout">
      <aside class="chat-sessions">
        <div class="chat-heading"><h2>{{ t("会话") }}</h2><button class="icon-button" :aria-label="t('刷新聊天')" :title="t('刷新聊天')" @click="refresh"><RefreshCw :size="17" /></button></div>
        <div v-for="session in sessions" :key="session.session_id" class="chat-session" :class="{ active: selectedId === session.session_id }" role="button" tabindex="0" @click="select(session)" @keydown.enter="select(session)">
          <MessageCircle :size="17" /><span><strong>{{ session.peer_name }}</strong><small>{{ label(session) }} · {{ session.last_preview || t("暂无消息") }}</small></span><button class="icon-button" :aria-label="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" :title="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" @click.stop="toggleMute(session)"><BellOff v-if="muted.includes(session.peer_id)" :size="14" /><Bell v-else :size="14" /></button><b v-if="session.unread">{{ session.unread }}</b>
        </div>
        <div v-if="!sessions.length" class="empty"><MessageCircle :size="30" /><p>{{ t("暂无聊天会话") }}</p></div>
        <div class="chat-heading"><h2>{{ t("附近设备") }}</h2></div>
        <button v-for="device in devices" :key="`chat-${device.id}`" class="chat-device" :disabled="busy || device.connection_state === 'offline'" @click="invite({ peer_id: device.id, peer_name: device.name } as ChatSession)">
          <MessageCircle :size="16" /><span>{{ device.name }}</span><small>{{ device.paired ? t("已配对") : t("未配对") }}</small>
        </button>
      </aside>
      <div class="chat-conversation" v-if="selected">
        <header class="chat-heading"><div><h2>{{ selected.peer_name }}</h2><small class="muted">{{ t("{label} · 指纹 {code}", { label: selected.peer_typing ? t('对方正在输入…') : label(selected), code: selected.fingerprint_short || t('未提供') }) }}</small></div><button class="icon-button" :aria-label="t('关闭会话')" :title="t('关闭会话')" @click="close"><X :size="18" /></button></header>
        <div v-if="selected.status === 'invited'" class="chat-invite"><strong>{{ t("{name} 邀请你聊天", { name: selected.peer_name }) }}</strong><span>{{ t("请核对指纹后决定。") }}</span><button class="primary" @click="answer(selected, true)" :disabled="busy"><Check :size="16" />{{ t("接受") }}</button><button @click="answer(selected, false)" :disabled="busy">{{ t("拒绝") }}</button></div>
        <div v-if="selected.status === 'inviting'" class="chat-invite">{{ t("正在等待对方接受邀请…") }}</div>
        <div class="chat-messages" aria-live="polite"><div v-if="!messages.length" class="empty"><MessageCircle :size="28" /><p>{{ t("还没有消息") }}</p></div><article v-for="entry in messages" :key="entry.entry_id" class="chat-message" :class="{ outgoing: entry.outgoing, system: entry.kind === 'system' }"><span>{{ text(entry) }}</span><small>{{ time(entry.ts) }}</small><!-- How a relayed message ended, stamped on the bubble it belongs to: the same delivered/failed/queued wording as the device's send list, so one message reads the same in both places. --><span v-if="receiptGlyph(entry)" class="chat-delivery" :class="`chat-delivery--${receipt(entry)}`"><component :is="receiptGlyphs[receiptGlyph(entry)!]" :size="12" />{{ receiptText(entry) }}</span><div v-if="entry.kind === 'file' && entry.transfer_id" class="file-actions"><button v-if="!entry.outgoing && ['done','completed','success'].includes(entry.status) && entry.saved_path" :disabled="busy" @click="openFile(entry)">{{ t("打开") }}</button><button v-if="!entry.outgoing && ['pending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'accept')"><Check :size="14" />{{ t("接受") }}</button><button v-if="!entry.outgoing && ['pending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'decline')"><X :size="14" />{{ t("拒绝") }}</button><button v-if="entry.outgoing && ['pending','sending','await_accept'].includes(entry.status)" :disabled="busy" @click="fileAction(entry, 'cancel')"><X :size="14" />{{ t("取消") }}</button></div><button v-if="entry.kind === 'text' && entry.outgoing && entry.status === 'failed'" class="icon-button" :aria-label="t('重发消息')" :title="t('重发消息')" @click="resend(entry)"><RefreshCw :size="14" /></button></article></div>
        <form v-if="selected.status === 'active'" class="chat-composer" @submit.prevent="send"><textarea v-model="draft" :aria-label="t('聊天消息')" maxlength="16000" :placeholder="t('输入消息，按 Enter 发送')" @keydown="keydown" @input="typing" /><button type="button" class="icon-button" :aria-label="t('发送附件')" :title="t('发送附件')" :disabled="busy" @click="sendFile"><Paperclip :size="18" /></button><button class="primary icon-button" :aria-label="t('发送消息')" :title="t('发送消息')" :disabled="!draft.trim() || busy"><Send :size="18" /></button></form>
      </div>
      <div v-else class="empty chat-empty"><MessageCircle :size="42" /><h2>{{ t("选择一个会话") }}</h2></div>
    </div>
  </section>
</template>

<style scoped>
/* The chat page is a page like any other inside `.content`: it takes the height
   it is given and scrolls its own message list. */
.chat-view { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.chat-layout { display: grid; grid-template-columns: minmax(220px, 280px) minmax(0, 1fr); min-height: 560px; margin: 0 var(--page-gutter) 28px; border: 1px solid var(--line); border-radius: var(--radius-lg); overflow: hidden; }
.chat-conversation { display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto; min-width: 0; }
.chat-conversation > .chat-heading { border-bottom: 1px solid var(--line); }
.chat-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; }
.chat-heading h2 { margin: 0; font-size: 17px; }
/* The session list: one row per conversation, the unread count at its right. */
.chat-sessions { border-right: 1px solid var(--line); padding: 8px; overflow: auto; }
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
/* Narrow: the session list goes above the conversation rather than beside it. */
@media (max-width: 700px) {
  .chat-layout { grid-template-columns: minmax(0, 1fr); }
  .chat-sessions { border-right: 0; border-bottom: 1px solid var(--line); max-height: 220px; }
}
</style>
