<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { Bell, BellOff, Check, ChevronDown, ChevronUp, Clock, Copy, MessageCircle, Paperclip, RefreshCw, Send, Trash2, X } from "@lucide/vue";
import { bridge, type PhoneChatMessage } from "../api/bridge";
import { t } from "../i18n";
import { systemText } from "../i18n/chat";
import { dayLabel, shortTime, size } from "../i18n/format";
import { isMenuKey, keyboardMenuPoint, openContextMenu } from "../lib/context-menu";
import { chatReachable, deviceLabel } from "../lib/device-row";
import { chatDraft, setChatDraft } from "../lib/chat-drafts";
import { announce } from "../lib/status";
import { copyText } from "../lib/clipboard";
import EmptyState from "./EmptyState.vue";
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
  /** Files dropped on the window while this page is open, for this page to
   *  send to the selected conversation.  The window owns the drop target (the
   * native host is the only side that knows the paths), so it stages them here
   * instead of opening the transfers page: a drop on a conversation is an
   * attachment, and yanking the reader to another page over it was the reported
   * bug.  Answers with `dropped` once taken, or `fallback` when there is no
   * conversation that could take them. */
  dropped?: string[];
}>(), {
  receipts: () => ({}),
  openSession: "",
  dropped: () => [],
});
const emit = defineEmits<{
  opened: [];
  selected: [sessionId: string];
  /** An invitation was answered here, so the notice that announced it — if one
   *  is still up — is over: the same question must not be asked twice. */
  answered: [sessionId: string];
  /** The staged files have been taken (sent, or refused with a visible error). */
  dropped: [];
  /** Nothing here can take the files; the window should hand them to the
   *  transfers page instead, which is where a drop went everywhere before this
   *  page could take one. */
  fallback: [paths: string[]];
  /** An empty conversation list asked where conversations come from.  The page
   *  cannot answer — the devices that can start one live on the devices page —
   *  so the window opens it. */
  devices: [];
}>();
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
/** The local phone conversation, merged into this page as a fixed contact. */
const PHONE_CHAT_ID = "__phone__";
const phoneMessages = ref<PhoneChatMessage[]>([]);
const phoneSending = ref(false);
let lastPhoneMessageId = 0;
let phoneChatInitialized = false;
/** Remote access is what makes the phone conversation reachable at all. */
const remoteEnabled = ref(false);
const remoteTokenSet = ref(false);
const remoteAvailable = computed(() => remoteEnabled.value && remoteTokenSet.value);
/** Whether nearby devices may send without asking — see `ChatSessionsPage`. */
const openToAll = ref(true);
const selectedId = ref("");
/** What is being typed, per conversation.
 *
 * One box for every conversation meant that a half-written message followed
 * the reader into whichever conversation they opened next — and being sent
 * from there is the worst thing a draft can do.  Each keeps its own, so
 * switching away and back comes back to what was being written.
 *
 * Kept outside the page (`lib/chat-drafts.ts`) because this one is unmounted
 * the moment its tab is left: a draft held here went with it, so stepping to
 * the history to check a clip and coming back found the box empty. */
const draft = computed({
  get: () => chatDraft(selectedId.value),
  set: (value: string) => setChatDraft(selectedId.value, value),
});
/** The phone conversation's own box, kept the same way and for the same reason. */
const phoneDraft = computed({
  get: () => chatDraft(PHONE_CHAT_ID),
  set: (value: string) => setChatDraft(PHONE_CHAT_ID, value),
});
const busy = ref(false);
const error = ref("");
const attachmentError = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
let timer: ReturnType<typeof setInterval> | undefined;
let typingTimer: ReturnType<typeof setTimeout> | undefined;
const selected = computed(() => sessions.value.find((s) => s.session_id === selectedId.value));
const phoneSelected = computed(() => selectedId.value === PHONE_CHAT_ID);
const phoneUnread = computed(() =>
  phoneMessages.value.filter((m) => m.sender === "phone" && !m.read).length);
const phonePreview = computed(() => {
  const last = phoneMessages.value[phoneMessages.value.length - 1];
  return last ? last.text : t("暂无消息");
});

/** How many of the phone conversation's messages this page holds.
 *
 * The store keeps its own ceiling and the page has no reason to keep more;
 * both trim from the same end, so what the reader can scroll back through is
 * the same stretch either way. */
const PHONE_MESSAGES_KEPT = 500;

/** Put the list on its last row, once the rows are drawn.
 *
 * `scrollTop` rather than `scrollTo`: the same move, and the one the rest of
 * this file makes — a scroll container that is scrolled by two different
 * mechanisms is two behaviours to keep in step. */
async function scrollToEnd() {
  await nextTick();
  const list = messageList.value;
  if (list) list.scrollTop = list.scrollHeight;
}

async function loadPhoneChat(notifyNew = false) {
  if (!remoteAvailable.value) return;
  const firstLoad = !phoneChatInitialized;
  // Only what is new, once the conversation has been read once.  The store
  // answers `after_id` with the messages past it, and this read runs every
  // second and a half for as long as the page is open — asking for the whole
  // conversation each time meant an idle poll carried every message the phone
  // and this machine had ever exchanged.
  const result = await bridge.phoneChatList(firstLoad ? 0 : lastPhoneMessageId);
  const fresh = result?.messages ?? [];
  const incoming = fresh.filter((m) => m.sender === "phone");
  lastPhoneMessageId = result?.last_id ?? lastPhoneMessageId;
  phoneMessages.value = firstLoad
    ? fresh
    : fresh.length
      ? [...phoneMessages.value, ...fresh].slice(-PHONE_MESSAGES_KEPT)
      : phoneMessages.value;
  phoneChatInitialized = true;
  if (phoneSelected.value) {
    await bridge.phoneChatRead();
    // The store has marked them on its side; the badge counts *this* copy, and
    // a poll that now asks only for what is new would never bring the flag
    // back — so the page settles its own list rather than waiting for a read
    // that is no longer coming.
    phoneMessages.value = phoneMessages.value.map((message) =>
      message.sender === "phone" ? { ...message, read: true } : message);
    // A phone conversation that is being watched follows what comes in, the
    // way a peer's does: this one is read by the same poll and was the one
    // transcript that never moved, so a message arriving on a phone the reader
    // is holding a conversation with landed under the fold.  When they are not
    // at the end, it is counted instead — the same silence, and the same way
    // out of it, as a peer's conversation.
    if (!firstLoad) {
      if (following) await scrollToEnd();
      else unseen.value += incoming.length;
    }
  }
  if (notifyNew && !firstLoad && incoming.length && !phoneSelected.value) {
    props.notify?.(t("手机：{text}", { text: incoming[incoming.length - 1].text }));
  }
}

async function openPhoneConversation() {
  if (!remoteAvailable.value) return;
  attachmentError.value = "";
  selectedId.value = PHONE_CHAT_ID;
  // A conversation opens at its end, whichever one was left behind — and the
  // count the other one was keeping is not about this one.
  following = true;
  unseen.value = 0;
  await loadPhoneChat();
  await scrollToEnd();
}

async function sendPhoneConversation() {
  const text = phoneDraft.value.trim();
  if (!text || phoneSending.value) return;
  phoneSending.value = true;
  try {
    await bridge.phoneChatSend(text);
    phoneDraft.value = "";
    // Sending is the reader joining the conversation at its end, so the list
    // follows from here even if they had scrolled away from it.
    following = true;
    await loadPhoneChat();
    await scrollToEnd();
  } catch (reason: any) {
    error.value = reason?.message || t("无法发送消息");
  } finally { phoneSending.value = false; }
}

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

/** Whether a row in 附近设备 can be reached right now, over either of its
 *  routes — the devices page's own rule, which offers the conversation from a
 *  device's row on the same answer; see `lib/device-row.ts`, where it is spelled
 *  once so the two pages cannot disagree about a device they both list. */
const reachable = chatReachable;

/** The route in the row's own small print — the device list's two chips in one
 *  word, because this list gives that slot one word.  A device holding an
 *  internet pairing is named as one whether or not the relay currently sees it:
 *  未配对 is a claim about pairing, and it is the one answer that would be
 *  false about such a device, whose reachability the device list already
 *  reports as 互联网·离线. */
function nearbyRoute(device: Device) {
  if (device.relay || device.relay_paired) return reachable(device) ? t("互联网") : t("离线");
  if (device.connection_state === "offline") return t("离线");
  return device.paired ? t("已配对") : t("未配对");
}

/** Whether the list is sitting at the newest message — see `trackScroll`. */
let following = true;
/** The rows that arrived below the reader while they were up in the history.
 *
 * The list deliberately does not move under a reader who has scrolled back, so
 * a message that lands meanwhile is one they cannot see and were not told
 * about: the transcript grows quietly under a viewport that is looking at
 * yesterday.  This is the count the way back down is labelled with, and it goes
 * to zero the moment they are at the end again, by any route.
 *
 * Whichever conversation is open keeps it — the peer's from its page reads and
 * the phone's from its own — and opening either one clears it, because the
 * count a reader left behind in one is not about the other. */
const unseen = ref(0);

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
    const [deviceResult, result, remote] = await Promise.all([
      bridge.chatDevices(), bridge.chatSessions(), bridge.companionRemoteStatus(),
    ]);
    devices.value = deviceResult.devices || [];
    sessions.value = result.sessions || [];
    muted.value = result.muted || [];
    openToAll.value = result.open_to_all !== false;
    // A conversation the runtime has dropped — an invitation nobody answered, a
    // closed one reaped an hour after its last message — takes its row out of
    // the list, and the transcript beside it was left open on nothing: no name
    // in the header, no messages, and no row anywhere saying why.  Letting go
    // of it is what lets the open below land on a conversation that is still
    // there, or on the page's own empty state when none is.
    if (selectedId.value && selectedId.value !== PHONE_CHAT_ID
      && !sessions.value.some((s) => s.session_id === selectedId.value)) {
      selectedId.value = "";
    }
    remoteEnabled.value = !!remote?.enabled;
    remoteTokenSet.value = !!remote?.token_set;
    if (!remoteAvailable.value && selectedId.value === PHONE_CHAT_ID) selectedId.value = "";
    await loadPhoneChat(true);
    // A conversation opened by this read has just been read; the line below
    // would ask for the same page a second time, on the one poll the page is
    // least likely to have anything cached for — the cold start.
    const opened = !selectedId.value && sessions.value.length > 0;
    if (opened) await select(sessions.value[0]);
    if (selectedId.value === PHONE_CHAT_ID) { error.value = ""; return; }
    if (selectedId.value && !opened) await load(selectedId.value);
    error.value = "";
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
 *
 * It is opened by position rather than by an event, because the keyboard opens
 * it too — a *menu key* has no pointer coordinates to hand over.
 */
function sessionMenu(at: { clientX: number; clientY: number }, session: ChatSession) {
  const isMuted = muted.value.includes(session.peer_id);
  openContextMenu(at, [
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
/** A conversation row's own keys.
 *
 * The row is a `role="button"`, which is a promise to answer both keys a button
 * answers — it only answered Enter, so Space did nothing on a focused row.  The
 * menu had no keyboard way in at all: 静音 and 关闭会话 were reachable with a
 * mouse and by nothing else, which is the same wall as a button that is dimmed
 * with no reason given.  `Shift+F10` and the menu key are the two spellings of
 * "the menu for this", and both open it on the row's own corner.
 */
/** The row's menu key.  Enter and Space are not handled here any more: opening
 * a conversation is a real button now (see U80), so the browser already answers
 * both keys with a click and doing it here as well would open it twice.
 *
 * No `target !== currentTarget` guard either.  It was there because the row held
 * the bell inside its own button, and a key pressed on the bell had to stay on
 * the bell; the bell is a sibling now, and the menu belongs to the row it sits
 * on — a right-click anywhere in the row already offers it, so the menu key on
 * the bell should too rather than doing nothing at all. */
function sessionKeydown(event: KeyboardEvent, session: ChatSession) {
  if (isMenuKey(event)) {
    event.preventDefault();
    sessionMenu(keyboardMenuPoint(event), session);
  }
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
  openContextMenu(event, [
    // Copy needs something to copy; delete only needs a row, which is why one
    // of the two can be absent and the other never is.
    value?.trim() ? { id: "copy", label: t("复制"), icon: Copy, run: () => copyText(value) } : null,
    { id: "delete", label: t("从本机删除"), icon: Trash2, danger: true, divider: !!value?.trim(),
      disabled: busy.value, run: () => askDeleteMessage(entry) },
  ]);
}

/** The row a delete is being confirmed for. */
const deletingMessage = ref<ChatEntry | null>(null);
const deleteMessageDialog = ref<HTMLDialogElement | null>(null);

function askDeleteMessage(entry: ChatEntry) {
  if (busy.value) return;
  deletingMessage.value = entry;
  deleteMessageDialog.value?.showModal();
}
/** Take one row out of this machine's copy of the conversation.
 *
 * The sidecar answers `ok` for a row it found and removed; the transcript is
 * re-read afterwards rather than having the row spliced out here, because the
 * page is a page — the row that moves up into its place may be one it does not
 * hold, and a list that closed the gap itself would be showing the reader a
 * conversation with a hole it invented.
 */
async function confirmDeleteMessage() {
  const entry = deletingMessage.value;
  const id = selectedId.value;
  deleteMessageDialog.value?.close();
  if (!entry || !id) return;
  busy.value = true;
  try {
    const result = await bridge.deleteChatMessage(id, entry.entry_id);
    if (!result.ok) throw new Error(t("这条消息没有删除"));
    await load(id);
  } catch (reason: any) {
    error.value = reason?.message || t("删除消息失败");
  } finally { busy.value = false; }
}

/** How much of a conversation the page holds at a time.
 *
 * The poll behind this page reads the open conversation once every second and a
 * half, and it used to ask for the whole transcript every time — so a
 * conversation that had been going for a month made a poll that had nothing new
 * to say more expensive the longer it went on, and did it in a loop nobody
 * could see.  What the reader is looking at is the end of the conversation, so
 * that is what the poll carries; the rest is one click away and arrives only
 * when it is asked for. */
const CHAT_PAGE_SIZE = 200;
const messageLimit = ref(CHAT_PAGE_SIZE);
/** Whether there is more of the conversation above the page.
 *
 * The sidecar answers a page, not a total, so the question has to be framed to
 * have a yes in it: the page asks for one row more than it will show, and a
 * read that comes back with that row is a read that came back full.  Counting
 * the ceiling alone got a conversation of exactly one page wrong — it claimed
 * history it did not have, and the click that would find out changed nothing
 * on screen. */
const hasEarlier = ref(false);

/** How many rows of the new page were not on the old one, and only while the
 *  reader is above the end — those are the ones that arrived without being
 *  seen.  A page is always the tail of the conversation (`limit` only ever
 *  grows upwards), so where the previous page's last row now sits is where the
 *  new rows begin.  A last row that is gone from the page is a page this
 *  cannot speak about, and saying nothing beats counting wrong. */
function arrived(previous: ChatEntry[], next: ChatEntry[]) {
  if (following || !previous.length) return 0;
  const at = next.findIndex((entry) => entry.entry_id === previous[previous.length - 1].entry_id);
  return at < 0 ? 0 : next.length - at - 1;
}

async function load(id: string) {
  try {
    const result = await bridge.chatMessages(id, messageLimit.value + 1);
    if (selectedId.value !== id) return;
    const all = result.messages || [];
    // The row that answers the question is not part of the page it proves.
    const next = all.length > messageLimit.value ? all.slice(-messageLimit.value) : all;
    unseen.value += arrived(messages.value, next);
    messages.value = next;
    hasEarlier.value = all.length > messageLimit.value;
  }
  catch (reason: any) { error.value = reason?.message || t("无法读取聊天消息"); }
}
/** Whether a stretch is being fetched right now.
 *
 * The button is the way in and the click is cheap, so it is clicked twice: two
 * reads then run with two different ceilings, and the smaller one can answer
 * last — which would put the shorter page back on screen after the longer one
 * had already been drawn, with the reader's place measured against neither.
 * One read at a time, and the button says it is working. */
const paging = ref(false);

/** Ask for the stretch of the conversation above the one on screen.
 *
 * The page grows rather than being replaced: a reader who wanted yesterday's
 * messages is reading upwards, and a second click keeps going the same way.
 *
 * The scroll anchor is the whole of the work.  Rows going in above the ones
 * being read make a taller list under the same `scrollTop`, which lands the
 * reader somewhere else in the conversation than where they asked for more of
 * it — the opposite of what the click is for.  Adding back the height that
 * arrived leaves them on the row they were reading. */
async function loadEarlier() {
  if (busy.value || paging.value || !selectedId.value || !hasEarlier.value) return;
  const list = messageList.value;
  const heightBefore = list ? list.scrollHeight : 0;
  paging.value = true;
  try {
    // Asking for what is above is the reader saying they are reading upwards,
    // and the list follows only while it is following: without this, the same
    // read that adds rows above them would also pin them to the newest message.
    following = false;
    messageLimit.value += CHAT_PAGE_SIZE;
    await load(selectedId.value);
  } finally { paging.value = false; }
  if (!list) return;
  await nextTick();
  list.scrollTop += list.scrollHeight - heightBefore;
}
async function select(session: ChatSession) {
  messages.value = [];
  // A conversation opens at its end, whichever one was left behind: the page
  // it starts on is a fresh one, not the one the last conversation had grown
  // to by being scrolled through.
  messageLimit.value = CHAT_PAGE_SIZE;
  hasEarlier.value = false;
  unseen.value = 0;
  attachmentError.value = "";
  // Taken before `markRead` below clears it: the line is drawn from this, and
  // the count it describes is gone a moment after the conversation opens.
  unreadCount.value = session.unread || 0;
  jumpedToUnread.value = false;
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
  // Scrolling back down to the end is the reader having read what was below,
  // whatever brought them there — including the wheel, not just the button.
  if (following) unseen.value = 0;
}
function toNewest() {
  const list = messageList.value;
  if (!list || !following) return;
  list.scrollTop = list.scrollHeight;
  unseen.value = 0;
}
/** Take the offer under the transcript: the end of the conversation, and the
 *  list following it again from here on. */
function jumpToNewest() {
  const list = messageList.value;
  following = true;
  unseen.value = 0;
  if (list) list.scrollTop = list.scrollHeight;
}
/* ── Where the unread messages begin ─────────────────────────────────── */

/** How many messages this conversation had waiting when it was opened.
 *
 * The sidecar answers a count and no marker, so the place the reader left off
 * is arithmetic: `unread` counts the incoming entries at the end of the
 * transcript, and the first of those is that many from the end.  It has to be
 * a snapshot taken *before* `markRead` clears it — read from the live count,
 * the line would be computed from a zero and land at the top of everything.
 */
const unreadCount = ref(0);
/** Whether the reader has been taken to that line already, so the way there
 *  stops offering itself.  Reset per conversation. */
const jumpedToUnread = ref(false);

/** The entry the unread run starts at, or "" when this conversation had none. */
const firstUnreadId = computed(() => {
  if (unreadCount.value <= 0) return "";
  const incoming = messages.value.filter((entry) => !entry.outgoing && entry.kind !== "system");
  const at = incoming.length - unreadCount.value;
  return at >= 0 && at < incoming.length ? incoming[at].entry_id : "";
});

/** The transcript as it is drawn: the same entries, a break between two days,
 *  and a line where the unread run starts.
 *
 * The rows are a list of their own rather than separators spliced into
 * `messages`, because `messages` is what paging, the arrival count and the
 * scroll anchor all measure — a computed that grew rows in the middle of it
 * would make every one of those numbers wrong.
 */
type StreamRow =
  | { key: string; kind: "day"; label: string }
  | { key: string; kind: "unread" }
  | { key: string; kind: "entry"; entry: ChatEntry };

const stream = computed<StreamRow[]>(() => {
  const rows: StreamRow[] = [];
  const unreadAt = firstUnreadId.value;
  let day = "";
  for (const entry of messages.value) {
    if (unreadAt && entry.entry_id === unreadAt) {
      rows.push({ key: `unread-${entry.entry_id}`, kind: "unread" });
    }
    const label = dayLabel(entry.ts);
    if (label && label !== day) {
      day = label;
      rows.push({ key: `day-${entry.entry_id}`, kind: "day", label });
    }
    rows.push({ key: entry.entry_id, kind: "entry", entry });
  }
  return rows;
});

/** Take the reader to the first message they had not seen.
 *
 * A button and not an automatic landing: opening a conversation at its newest
 * message is what makes catching up quick, and a page that scrolled itself
 * into the middle of a backlog every time would make that slow instead.
 */
function jumpToUnread() {
  const list = messageList.value;
  jumpedToUnread.value = true;
  if (!list) return;
  const target = [...list.querySelectorAll<HTMLElement>("[data-entry-id]")]
    .find((node) => node.dataset.entryId === firstUnreadId.value);
  if (!target) return;
  // Above the end is not following the end: the next message to arrive must
  // not pull the page away from what the reader just asked to look at.
  following = false;
  target.scrollIntoView?.({ block: "center" });
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
    // select yet.  That has to be said on the window's status line rather than
    // in a band this page owns: the refresh below is what the 1.5s poll runs
    // too, and it clears the page's own bands, so a sentence held there was
    // gone before the click's effect could be seen.
    if (result.chat_session_id) selectedId.value = result.chat_session_id;
    else announce(t("正在连接对方，连接上以后会话会出现在列表里。"));
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
    // Answered here, so the notice that may still be offering these two
    // buttons is answered too — whichever surface the reader reached for.
    emit("answered", session.session_id);
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
    // A reader who had scrolled back through the conversation before typing
    // was left looking at the old messages after sending: the list only
    // follows while it is following, and sending is the one thing that is
    // always about the newest message.
    following = true;
    draft.value = "";
    // The refresh is the read: it carries the transcript as well as the rail
    // and the route the message took, so asking for the transcript here as
    // well was the same page fetched twice on every message sent.
    await refresh();
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
  try {
    // The conversation is over, so what was being written to it is too: a
    // draft kept for a closed session would still be there if the peer is
    // ever talked to again, under a new session id, and would then come back
    // as text typed minutes or days ago.
    setChatDraft(session.session_id, "");
    if (!(await bridge.closeChat(session.session_id)).ok) throw new Error(t("会话未关闭")); await refresh(); }
  catch (reason: any) { error.value = reason?.message || t("关闭会话失败"); }
  finally { busy.value = false; }
}
/** Why an attachment did not go, in words that say what to do about it.
 *
 * The one attachment refusal a reader can act on is a file past the app's cap,
 * and it arrived here as "LAN operation failed" — the sidecar folded it into the
 * sentence it uses for every failure, so the window had nothing to translate and
 * a size problem read as a network one.  The code is raised for it now (see
 * `chat_file` in the runtime) and this is the sentence it becomes.  See U146.
 */
function attachmentFailure(reason: any): string {
  if (reason?.code === "CHAT_FILE_TOO_LARGE") {
    return t("文件超过 2 GB：聊天一次只能发这么大的单个文件，更大的内容请拆开或压缩后再发。");
  }
  return reason?.message || t("附件发送失败");
}
async function sendFile() {
  const session = selected.value;
  const path = await bridge.chooseFile();
  if (!session || !path) return;
  busy.value = true;
  try { following = true; await bridge.sendChatFile(session.session_id, path); await load(session.session_id); }
  catch (reason: any) { error.value = attachmentFailure(reason); }
  finally { busy.value = false; }
}

/** Files handed over by the window's drop target, waiting to be sent. */
const dropQueue = ref<string[]>([]);
/** Whether a drop is being drained right now (the queue may outlive one await). */
let drainingDrops = false;

/** The last path segment, for naming a file in an error band. */
function baseName(path: string) {
  return path.split(/[\\/]/).pop() || path;
}
/**
 * Send dropped files to the conversation on screen, one after another.
 *
 * A drop on a conversation is the same act as the composer's 发送附件, only
 * without the picker: the files go to the selected session and the reader stays
 * on the page.  The window used to open the transfers page for every drop, so
 * dropping a file onto a chat moved the reader away from the conversation they
 * were having.
 *
 * The queue is drained rather than iterated in place because a second drop can
 * land while the first is still being sent: the files are kept in order, and a
 * failure stops the drain with the reason on screen — the remaining files stay
 * queued, which is better than sending a conversation that silently skipped
 * the one before them.
 */
async function drainDropped() {
  if (drainingDrops) return;
  drainingDrops = true;
  busy.value = true;
  try {
    while (dropQueue.value.length) {
      const path = dropQueue.value[0];
      const session = selected.value;
      // A conversation can close under a slow send; drop the rest to the page
      // that can still take them rather than sending into a closed session.
      if (!session || !LIVE_STATUS.includes(session.status)) {
        const rest = [...dropQueue.value];
        dropQueue.value = [];
        attachmentError.value = "";
        emit("fallback", rest);
        return;
      }
      try {
        const result = await bridge.sendChatFile(session.session_id, path);
        if (!result || result.ok === false) throw new Error(t("附件发送失败"));
      } catch (reason: any) {
        attachmentError.value = t("{file} 发送失败：{reason}", {
          file: baseName(path),
          reason: attachmentFailure(reason),
        });
        return;
      }
      dropQueue.value = dropQueue.value.slice(1);
      await load(session.session_id);
    }
    await refresh();
  } finally {
    drainingDrops = false;
    busy.value = false;
  }
}
/** Take what the window staged, and tell it either way.
 *
 * `dropped` is the acknowledgement: the window clears its staged paths so a
 * later visit to the chat page does not send them a second time.  `fallback`
 * carries the same array back, so the window can hand it to the transfers page
 * without a second allocation (and without re-triggering this watcher). */
watch(() => props.dropped, (paths) => {
  if (!paths || !paths.length) return;
  emit("dropped");
  dropQueue.value = [...dropQueue.value, ...paths];
  void drainDropped();
});
function keydown(event: KeyboardEvent) {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); }
}
/** How often a reader who is still typing is reported as still typing.
 *
 * One keystroke is not a change of state, and the sidecar answers a second
 * "still typing" inside two seconds by suppressing it — so every letter was a
 * round trip whose answer was known before it was asked for.  The peer's
 * indicator is a flag with a four-second life, refreshed by any one of these;
 * a second is well inside both numbers. */
const TYPING_PING_MS = 1200;
let typingPingedAt = 0;

function typing() {
  const session = selected.value;
  if (!session || session.status !== "active") return;
  const now = Date.now();
  if (now - typingPingedAt >= TYPING_PING_MS) {
    typingPingedAt = now;
    void bridge.sendChatTyping(session.session_id, true).catch(() => {});
  }
  if (typingTimer) clearTimeout(typingTimer);
  // Stopping is the one that always has to get through, whatever the clock
  // above decided: it is what takes the word off the peer's screen.
  typingTimer = setTimeout(() => {
    typingPingedAt = 0;
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

// Which conversation is on screen, told to the window.  The message event uses
// it to stay quiet for a message that arrives in the conversation the reader is
// already looking at — the bubble appearing in front of them is the
// notification — and this page is the only thing that knows.  Reported on the
// way out too (`""`), because the page is unmounted the moment its tab is left
// and a session kept from there would silence the notice for a conversation
// nobody is looking at any more.
watch(selectedId, (id) => emit("selected", id), { immediate: true });
onUnmounted(() => emit("selected", ""));

/** Whether this window is on screen to be read at all.
 *
 * The poll below is a read of the whole conversation — every message, every
 * session, the device list — once every second and a half, and a window that
 * is minimized or behind another one has nobody looking at any of it.  Focus
 * is not consulted, for the same reason the overview's own two timers are not:
 * a window kept open beside the work is usually not the focused one, and that
 * is exactly when it is being watched.
 */
let awake = true;
function wake() {
  awake = !document.hidden;
  // Coming back is the moment the list is looked at again, so it is read then
  // rather than at the end of the next interval — a conversation that moved
  // while the window was away shows that at once.
  if (awake) void refresh();
}
onMounted(() => {
  document.addEventListener("visibilitychange", wake);
  void refresh();
  timer = setInterval(() => { if (awake) void refresh(); }, 1500);
});
onUnmounted(() => {
  document.removeEventListener("visibilitychange", wake);
  if (timer) clearInterval(timer);
  if (typingTimer) clearTimeout(typingTimer);
});
</script>

<template>
  <section class="chat-view">
    <p v-if="error" class="error-band" role="alert">{{ error }}</p>
    <p v-if="attachmentError" class="error-band" role="alert">{{ attachmentError }}</p>

    <!-- The devices and the conversations share one rail, devices first.
         They were two tabs, and then two sections of one rail with the devices
         underneath every conversation; both put the way to start a chat
         somewhere the reader had to already know about.  Above the
         conversations, the devices are the first thing the page says, and the
         rail scrolls as one list rather than the device list scrolling past
         under it. -->
    <div class="chat-layout card card--flush">
      <aside class="chat-sessions">
        <div class="chat-heading card-head">
          <h2>{{ t("附近设备") }}</h2>
          <button class="icon-button" :aria-label="t('刷新聊天')" :title="t('刷新聊天')" @click="refreshNow"><RefreshCw :size="17" /></button>
        </div>
        <button class="chat-device" v-for="device in nearby" :key="`chat-${device.id}`"
          :disabled="busy || !reachable(device)"
          :title="reachable(device) ? undefined : t('{name} 当前不在线', { name: deviceLabel(device) })"
          @click="invite({ peer_id: device.id, peer_name: deviceLabel(device) } as ChatSession)">
          <MessageCircle :size="16" /><span>{{ deviceLabel(device) }}</span>
          <!-- A device paired by code is named as that rather than 已配对: it is
               the one row here whose conversation crosses the internet, and the
               reader should know before the first message which route it takes.
               It is offered on the same rule as the rest — reachable now or
               not — which for it is the relay's own view of the peer. -->
          <small>{{ reachable(device) ? nearbyRoute(device) : t("离线") }}</small>
        </button>
        <p v-if="!nearby.length" class="note chat-nearby-empty">{{ t("附近没有可聊天的设备。") }}</p>

        <h3 class="chat-section card-sub">{{ t("会话") }}</h3>
        <button v-if="remoteAvailable" type="button" class="chat-session phone-chat-row" data-session-id="__phone__" :class="{ active: phoneSelected }" @click="openPhoneConversation">
          <MessageCircle :size="17" />
          <span><strong>{{ t("手机对话") }}</strong><small>{{ t("与手机直接聊天") }} · {{ phonePreview }}</small></span>
          <b v-if="phoneUnread" class="badge badge--count">{{ phoneUnread }}</b>
        </button>
        <!-- A button inside a `role="button"` is invalid ARIA, and the inner one
             is the one a reader loses: 静音 sat inside the row's own button, so
             some screen readers announced the row and never the control.  The
             row is a container now, and opening it is a real button covering
             the part the row's click covered.  See U80. -->
        <div v-for="session in sessions" :key="session.session_id" class="chat-session"
          :data-session-id="session.session_id"
          :class="{ active: selectedId === session.session_id, 'chat-session--muted': muted.includes(session.peer_id) }"
          @keydown="sessionKeydown($event, session)" @contextmenu.prevent="sessionMenu($event, session)">
          <button type="button" class="chat-session__open" :aria-current="selectedId === session.session_id ? 'true' : undefined" @click="select(session)">
            <MessageCircle :size="17" />
            <span><strong>{{ session.peer_name }}</strong><small>{{ label(session) }} · {{ session.last_preview || t("暂无消息") }}<template v-if="session.last_activity_ts"> · {{ shortTime(session.last_activity_ts) }}</template></small></span>
          </button>
          <button class="icon-button" :aria-label="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" :title="muted.includes(session.peer_id) ? t('取消静音') : t('静音')" @click.stop="toggleMute(session)"><BellOff v-if="muted.includes(session.peer_id)" :size="14" /><Bell v-else :size="14" /></button>
          <b v-if="session.unread" class="badge badge--count">{{ session.unread }}</b>
        </div>
        <!-- A conversation starts from a device, and this column holds none:
             the sentence alone said 暂无聊天会话 and left the way in to be
             guessed at. -->
        <EmptyState v-if="!sessions.length && !remoteAvailable" :icon="MessageCircle" size="inset"
          :title="t('暂无聊天会话')"
          :description="t('会话在设备之间建立：挑一台在线的机器，或在手机伴侣上发起。')"
          :action="t('去设备页')" @action="emit('devices')" />
      </aside>

      <div class="chat-conversation" v-if="selected">
        <header class="chat-heading card-head">
          <div><h2>{{ selected.peer_name }}</h2><small class="muted" :title="t('安全码是这台设备的身份摘要：两台设备上的码一致，才说明对面确实是它。')">{{ t("{label} · 安全码 {code}", { label: selected.peer_typing ? t('对方正在输入…') : label(selected), code: selected.fingerprint_short || t('该设备不支持核对') }) }}</small></div>
          <button class="icon-button" :aria-label="t('关闭会话')" :title="t('关闭会话')" @click="close(selected)"><X :size="18" /></button>
        </header>

        <!-- A conversation that ended.  The composer is gone for these two and
             nothing said why: the reader is left looking at a transcript with
             no way to answer it and no word about what happened.  Both are the
             last state of a session the peer's row can reopen, so the way out
             is offered here, next to the transcript it would continue. -->
        <div v-if="selected.status === 'closed'" class="chat-invite strip strip--band">
          <strong>{{ t("会话已关闭") }}</strong><span class="note">{{ t("消息记录还在，可以重新发起。") }}</span>
          <button :disabled="busy" @click="invite(selected)"><MessageCircle :size="16" />{{ t("重新发起") }}</button>
        </div>
        <div v-else-if="selected.status === 'declined_remote'" class="chat-invite strip strip--band">
          <strong>{{ t("{name} 拒绝了邀请", { name: selected.peer_name }) }}</strong><span class="note">{{ t("可以再发一次。") }}</span>
          <button :disabled="busy" @click="invite(selected)"><MessageCircle :size="16" />{{ t("重新发起") }}</button>
        </div>
        <div v-else-if="selected.status === 'invited'" class="chat-invite strip strip--band">
          <strong>{{ t("{name} 邀请你聊天", { name: selected.peer_name }) }}</strong><span class="note">{{ t("请核对指纹后决定。") }}</span>
          <button class="primary" @click="answer(selected, true)" :disabled="busy"><Check :size="16" />{{ t("接受") }}</button>
          <button @click="answer(selected, false)" :disabled="busy">{{ t("拒绝") }}</button>
        </div>
        <div v-else-if="selected.status === 'inviting'" class="chat-invite strip strip--band note">{{ t("正在等待对方接受邀请…") }}</div>

        <div ref="messageList" class="chat-messages" aria-live="polite" @scroll.passive="trackScroll">
          <div v-if="!messages.length" class="empty"><MessageCircle :size="28" /><p class="note">{{ t("还没有消息") }}</p></div>
          <!-- The part of the conversation the page is not carrying, said
               rather than left out: a reader who walked back up a long
               transcript and met the top of it would otherwise be entitled to
               read what is on screen as the whole conversation. -->
          <p v-if="hasEarlier" class="chat-earlier" role="status">
            <span class="note">{{ t("只显示最近 {count} 条消息", { count: messages.length }) }}</span>
            <button type="button" :disabled="busy || paging" @click="loadEarlier">{{ t("加载更早的 {count} 条", { count: CHAT_PAGE_SIZE }) }}</button>
          </p>
          <!-- The transcript is drawn from `stream`, not from `messages`:
               the same rows, with a break between two days and a line where
               this reader's unread messages begin.  Both are rows of the
               list rather than anything spliced into the data, because
               `messages` is what paging, the arrival count and the scroll
               anchor all measure. -->
          <template v-for="row in stream" :key="row.key">
            <div v-if="row.kind === 'day'" class="chat-day" role="separator">{{ row.label }}</div>
            <div v-else-if="row.kind === 'unread'" class="chat-unread-line" role="separator"><span>{{ t("未读消息") }}</span></div>
            <article v-else class="chat-message" :data-entry-id="row.entry.entry_id"
              :class="{ outgoing: row.entry.outgoing, system: row.entry.kind === 'system' }"
              @contextmenu.prevent="messageMenu($event, row.entry)">
              <span>{{ text(row.entry) }}</span>
              <small v-if="row.entry.kind === 'file'" class="chat-file-meta">{{ fileMeta(row.entry) }}</small>
              <!-- How far along a file is: the status word above says it is moving,
                   the bar says how fast it is getting there.  Decorative — the
                   word is the accessible form. -->
              <div v-if="row.entry.kind === 'file' && row.entry.status === 'sending'" class="chat-file-progress" aria-hidden="true">
                <i :style="{ width: filePercent(row.entry) }"></i>
              </div>
              <small>{{ shortTime(row.entry.ts) }}</small>
              <!-- How a relayed message ended, stamped on the bubble it belongs
                   to: the same delivered/failed/queued wording as the device's
                   send list, so one message reads the same in both places. -->
              <span v-if="receiptGlyph(row.entry)" class="chat-delivery" :class="`chat-delivery--${receipt(row.entry)}`">
                <component :is="receiptGlyphs[receiptGlyph(row.entry)!]" :size="12" />{{ receiptText(row.entry) }}
              </span>
              <div v-if="row.entry.kind === 'file' && row.entry.transfer_id" class="file-actions">
                <button v-if="!row.entry.outgoing && ['done','completed','success'].includes(row.entry.status) && row.entry.saved_path" :disabled="busy" @click="openFile(row.entry)">{{ t("打开") }}</button>
                <button v-if="!row.entry.outgoing && ['done','completed','success'].includes(row.entry.status) && row.entry.saved_path" :disabled="busy" @click="revealFile(row.entry)">{{ t("打开所在文件夹") }}</button>
                <button v-if="!openToAll && !row.entry.outgoing && ['pending','await_accept'].includes(row.entry.status)" :disabled="busy" @click="fileAction(row.entry, 'accept')"><Check :size="14" />{{ t("接受") }}</button>
                <button v-if="!openToAll && !row.entry.outgoing && ['pending','await_accept'].includes(row.entry.status)" :disabled="busy" @click="fileAction(row.entry, 'decline')"><X :size="14" />{{ t("拒绝") }}</button>
                <button v-if="row.entry.outgoing && ['pending','sending','await_accept'].includes(row.entry.status)" :disabled="busy" @click="fileAction(row.entry, 'cancel')"><X :size="14" />{{ t("取消") }}</button>
              </div>
              <span v-if="row.entry.kind === 'text' && row.entry.outgoing && row.entry.status === 'failed'" class="chat-undelivered">{{ t("未送达") }}</span>
              <button v-if="row.entry.kind === 'text' && row.entry.outgoing && row.entry.status === 'failed'" class="icon-button" :aria-label="t('重发消息')" :title="t('重发消息')" @click="resend(row.entry)"><RefreshCw :size="14" /></button>
            </article>
          </template>
        </div>

        <!-- The way to the backlog, offered rather than taken: a conversation
             that opens at its newest message keeps catching up quick, and one
             that scrolled itself into the middle of sixty unread would make it
             slow.  Above the transcript, where the row it leads to is. -->
        <button v-if="firstUnreadId && !jumpedToUnread" type="button" class="chat-unread-jump" @click="jumpToUnread">
          <ChevronUp :size="14" />{{ t("跳到最早未读") }}
        </button>

        <!-- What the list did not do, said rather than left to be discovered:
             a message landing while the reader is up in the history does not
             move the transcript under them, so the only way to learn about it
             is a line where the new rows went in.  It sits in the transcript's
             own grid cell, over the bottom of it, so nothing reflows. -->
        <button v-if="unseen" type="button" class="chat-unseen" @click="jumpToNewest">
          <ChevronDown :size="14" />{{ t("{count} 条新消息", { count: unseen }) }}
        </button>

        <!-- Said before the reader types, not after.  The composer stays open
             for an offline peer because a box closing under a half-written
             message is worse, but the runtime refuses a text sent to one — so
             what used to be a failure reported after Enter is here instead. -->
        <p v-if="selected.status === 'active' && !selected.online" class="chat-offline note" role="status">{{ t("对方当前离线，消息暂时发不出去。") }}</p>
        <form v-if="selected.status === 'active'" class="chat-composer" @submit.prevent="send">
          <textarea v-model="draft" :aria-label="t('聊天消息')" maxlength="16000"
            :placeholder="selected.online ? t('输入消息，按 Enter 发送') : t('对方离线，暂时发不出去')" @keydown="keydown" @input="typing" />
          <button type="button" class="icon-button" :aria-label="t('发送附件')" :title="t('发送附件')" :disabled="busy" @click="sendFile"><Paperclip :size="18" /></button>
          <button class="primary icon-button" :aria-label="t('发送消息')"
            :title="!selected.online ? t('对方当前离线') : t('发送消息')"
            :disabled="!draft.trim() || busy || !selected.online"><Send :size="18" /></button>
        </form>
      </div>
      <div class="chat-conversation" v-else-if="phoneSelected">
        <header class="chat-heading card-head">
          <div><h2>{{ t("手机 Companion") }}</h2><small class="muted">{{ t("远程访问 · 与手机直接聊天") }}</small></div>
        </header>
        <div ref="messageList" class="chat-messages" aria-live="polite" @scroll.passive="trackScroll">
          <div v-if="!phoneMessages.length" class="empty"><MessageCircle :size="28" /><p class="note">{{ t("暂无消息") }}</p></div>
          <article v-for="message in phoneMessages" :key="message.id" class="chat-message"
            :class="{ outgoing: message.sender === 'desktop' }">
            <span>{{ message.text }}</span>
            <small>{{ shortTime(message.ts) }}</small>
          </article>
        </div>
        <button v-if="unseen" type="button" class="chat-unseen" @click="jumpToNewest">
          <ChevronDown :size="14" />{{ t("{count} 条新消息", { count: unseen }) }}
        </button>
        <form class="chat-composer" @submit.prevent="sendPhoneConversation">
          <textarea v-model="phoneDraft" :aria-label="t('聊天消息')" maxlength="4000" :placeholder="t('输入消息…')" @keydown.enter.exact.prevent="sendPhoneConversation" />
          <button class="primary icon-button" :aria-label="t('发送消息')" :title="t('发送消息')" :disabled="!phoneDraft.trim() || phoneSending"><Send :size="18" /></button>
        </form>
      </div>
      <!-- Nothing selected, which on a machine that has never chatted is the
           page's first sight.  It names the device list rather than assuming
           the reader has found it. -->
      <div v-else class="empty chat-empty">
        <MessageCircle :size="42" />
        <h2>{{ t("选择一个会话") }}</h2>
        <p class="note">{{ t("或者点左边的设备，开始一段新对话。") }}</p>
      </div>
    </div>
    <!-- Deleting a message is deleting *this machine's copy* of it, and the
         dialog says so before it does it: a reader who expects a recall would
         otherwise learn the difference from the peer's screen. -->
    <dialog ref="deleteMessageDialog" class="modal" aria-labelledby="chat-delete-title"
      @close="deletingMessage = null" @cancel="deletingMessage = null">
      <h2 id="chat-delete-title">{{ t("从本机删除这条消息？") }}</h2>
      <p>{{ t("它只会从这台设备上消失，对方仍然能看到它。") }}</p>
      <div class="modal-actions">
        <button autofocus @click="deleteMessageDialog?.close()">{{ t("取消") }}</button>
        <button class="danger" :disabled="busy" @click="confirmDeleteMessage">{{ t("删除") }}</button>
      </div>
    </dialog>
  </section>
</template>

<style scoped>
/* The chat page is a page like any other inside `.content`: it takes the height
   it is given and scrolls its own message list.

   What is left here is only what the chat page has and no other page does — the
   height handed down the whole way, the bubbles, the two file bars, the rail's
   own indent inside the card.  Everything the page shares with the rest of the
   window is worn as a class in the template: the two panes are the window's
   `.card--flush` (a card that holds panes, so it pads none of them), both heads
   are `.card-head`, the second section's label is `.card-sub`, the invite band
   is a `.strip--band`, and the three sentences are `.note`.

   Each part of the height is a rule of its own, because it has to be handed down
   the whole way.  `.chat-view` carries no floor of its own, so as a flex item it
   refused to shrink below its content and the pane grew past the window instead
   of the list scrolling inside it — a conversation of any real length ran off
   the bottom of the page and took the composer with it.  `.chat-layout`'s single
   row was implicit (`auto`), so the conversation was sized by its messages
   rather than by the window, and the `1fr` inside it had no height left to
   divide.  Both are the same mistake one level apart: a height taken from the
   content it was meant to bound. */
.chat-view { flex: 1; min-height: 0; min-width: 0; display: flex; flex-direction: column; }
.chat-layout { display: grid; grid-template-columns: minmax(220px, 280px) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); flex: 1; min-height: 560px; margin: var(--card-gap) var(--page-gutter) 28px; }
.chat-conversation { display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto auto; min-width: 0; min-height: 0; }
/* The four bands, pinned to their tracks rather than placed by document order.
   The invite band is optional — a session that is already active renders none —
   and under auto-placement its absence moved every band below it up one row: the
   message list took the second `auto` row and grew to its full content height
   instead of scrolling in the `1fr`, and the composer landed in the `1fr`, where
   a textarea that stretches to fill its row drew 125px of empty box under a
   44px field with the two send buttons stranded at its top. */
.chat-conversation > .chat-heading { grid-row: 1; }
.chat-conversation > .chat-invite { grid-row: 2; }
.chat-conversation > .chat-messages { grid-row: 3; }
/* Why there is nothing to send with, when there isn't: one quiet line between
   the transcript and the composer it is about, in its own row so its coming
   and going moves nothing else. */
.chat-conversation > .chat-offline { grid-row: 4; padding: 8px 16px 0; text-align: right; }
.chat-conversation > .chat-composer { grid-row: 5; }
/* Both heads are the window's `.card-head`; what is theirs is only how far in
   from the card's edge they start.  The rail's is inset by the rail's own 8px so
   that its title, the rules under it and the rows' names all share one left
   edge; the conversation's takes the messages' 16px. */
.chat-sessions > .chat-heading { padding: 14px 8px 11px; }
.chat-conversation > .chat-heading { padding: 14px 16px 11px; }
.chat-heading > div { min-width: 0; }
.chat-heading small { font-size: 12px; }
/* The rail: the devices this machine could talk to, then the conversations it
   has.  One scrolling list rather than two, with the devices on top — reaching
   them was the whole complaint, and putting them under every conversation is
   what made reaching them cost a scroll through the chats.

   It pads itself because the card around it pads nothing: the rows' names and
   the rules between them come from here, not from the card. */
.chat-sessions { border-right: 1px solid var(--line); padding: 8px; overflow: auto; }
/* The heading of the second section, on the rail's own indent: it labels what is
   under it rather than opening the page, so it is the card's part-heading with
   the rail's 8px rather than the card's 22px. */
.chat-section { padding: 14px 8px 2px; }
.chat-nearby-empty { padding: 4px 8px 8px; }
/* A row is a button, and a button with no hover is a row a reader cannot tell is
   one.  Selected is the window's selected: the accent's own wash, and the
   accent's own text so the name reads as chosen rather than merely filled.

   The row itself is a plain container now, not a button: 静音 lived inside the
   row's own `role="button"`, which is invalid ARIA and cost a screen reader the
   control entirely.  The padding moved onto the inner 打开 button, so the whole
   area that used to open the row still opens it, and the bell keeps its own
   strip at the right that only it answers.  See U80. */
.chat-session { width: 100%; display: flex; align-items: center; padding: 0 8px 0 0; border-bottom: 1px solid var(--line); }
/* The phone conversation is the one row that is its own button rather than a
   `.chat-session__open` inside one; it still has to read left-aligned and with
   the same gutter as the peer rows. */
.phone-chat-row { gap: 9px; padding: 0 8px; text-align: left; }
.chat-session:hover:not(.active) { background: var(--surface); }
.chat-session.active { background: var(--accent-soft); color: var(--accent-text); }
/* The part that opens the conversation: everything left of the bell.  It is a
   real button, so Enter and Space are the browser's to answer and the row's
   keydown handler no longer has to guess.  `font: inherit` keeps the name and
   the preview on the window's own type rather than the browser's default. */
.chat-session__open { flex: 1; min-width: 0; display: flex; align-items: center; gap: 9px; padding: 11px 8px; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.chat-session__open:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: 6px; }
/* Muted, which the bell on the row already says by its own shape — a reader
   glancing down the list has to be able to see which conversations are quiet
   without reading one icon on each row.  The name keeps its contrast; it is
   the preview under it that goes quiet, because that is the part that would
   otherwise tug at them. */
.chat-session--muted span strong { color: var(--muted); font-weight: 500; }
.chat-session span { min-width: 0; flex: 1; display: grid; gap: 3px; }
.chat-session small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.chat-device { width: 100%; display: flex; align-items: center; gap: 8px; padding: 9px 8px; border: 0; border-bottom: 1px solid var(--line); background: transparent; text-align: left; }
.chat-device:hover:not(:disabled) { background: var(--surface); }
.chat-device span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-device small { color: var(--muted); }
.chat-device:disabled { opacity: .5; cursor: not-allowed; }
/* The band above the messages that offers the pairing, when there is one.  Its
   shape is the window's `.strip--band`; all that is the invite's is the note
   under the sentence taking a line of its own, so the two buttons sit together
   rather than trailing the sentence. */
.chat-invite span { flex: 1 1 100%; }
.chat-messages { padding: 16px; overflow: auto; display: flex; flex-direction: column; gap: 9px; }
/* The line at the top of a paged transcript: what the page is holding, and the
   way to ask for the rest.  It is the only row in the list that is not a
   message, so it takes no bubble and no width of its own — it reads as one
   sentence and one button the reader walks past on the way up. */
.chat-earlier { align-self: center; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: center; font-size: 12px; }
.chat-earlier button { padding: 4px 10px; font-size: 12px; }
/* A day, once, over the run of messages that belongs to it.  The rows carry
   only a clock, so a transcript that crossed midnight read as one continuous
   afternoon; the rule on either side makes the break something the eye lands
   on while scrolling rather than a line of small text it reads past. */
.chat-day { align-self: stretch; display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.chat-day::before, .chat-day::after { content: ""; flex: 1; height: 1px; background: var(--line); }
/* Where this reader left off: the same rule, in the accent, and only over the
   first message they had not seen. */
.chat-unread-line { align-self: stretch; display: flex; align-items: center; gap: 10px; color: var(--accent-text); font-size: 12px; font-weight: 600; }
.chat-unread-line::before, .chat-unread-line::after { content: ""; flex: 1; height: 1px; background: var(--accent-line); }
/* The way up to that line, over the head of the transcript — the mirror of the
   pill over its foot, and in the same grid row so neither moves anything. */
.chat-unread-jump { grid-row: 3; align-self: start; justify-self: center; margin-top: 8px; z-index: 1;
  display: inline-flex; align-items: center; gap: 4px; padding: 5px 12px; font-size: 12px;
  border: 1px solid var(--accent-line); border-radius: 999px;
  background: var(--accent-soft); color: var(--accent-text); box-shadow: var(--shadow-soft); }
/* The way back down to a message the reader did not see arrive: a pill over the
   foot of the transcript.  It shares the transcript's grid row and is only ever
   as tall as itself, so nothing above it moves. */
.chat-unseen { grid-row: 3; align-self: end; justify-self: center; margin-bottom: 12px; z-index: 1;
  display: inline-flex; align-items: center; gap: 4px; padding: 5px 12px; font-size: 12px;
  border: 1px solid var(--accent-solid); border-radius: 999px;
  background: var(--accent-solid); color: var(--on-accent); box-shadow: var(--shadow-soft); }
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
