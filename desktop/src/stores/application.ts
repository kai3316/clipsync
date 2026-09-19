import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { AppStatus, BridgeError, Device, DiagnosticAction, HistoryItem, RecoveryResult, SidecarEvent, UpdateCheckResult, UpdateState } from "../api/types";
import { createFavoritesStore } from "./favorites";
import { createDeliveryStore } from "./delivery";

/** A pairing handshake that has not finished yet.
 *
 * `confirm_pairing` answers `paired: false` for these, and that is not a
 * failure: the confirm landed here and the other device has not answered —
 * `confirmed_waiting` is the usual one, `peer_confirmed` is the same moment
 * from the other side.  Mirrors the closed `PAIRING_STATUS_*` set in
 * internal/security/pairing.py, and the three App.vue's `pairingPending`
 * already treats as a live card.
 */
const PAIRING_IN_FLIGHT: readonly string[] = [
  "pending",
  "peer_confirmed",
  "confirmed_waiting",
];

export function createApplicationStore() {
  const state = reactive({
    status: null as AppStatus | null,
    history: [] as HistoryItem[],
    selectedIds: [] as string[],
    // Where the arrow keys have walked the history list to, or -1 for nowhere.
    // The legacy panel kept the same cursor: ↑/↓ moved it, Enter copied the row
    // it sat on and Delete removed it.  It is an index into `history` — the
    // visible page — rather than an id, because every paging move refetches and
    // an id from the page before would survive into a list that no longer holds
    // it.  -1 rather than 0 so the first ↓ enters the list at the top instead of
    // opening with a row already chosen.
    kbdIndex: -1,
    devices: [] as Device[],
    query: "",
    // The history toolbar's filter, kept in the store beside the query because
    // it belongs to the same request: every paging move refetches, and the
    // filter has to travel with it or page two would silently be unfiltered.
    kind: "all",
    sort: "newest",
    counts: {} as Record<string, number>,
    hasHistory: false,
    offset: 0,
    total: 0,
    limit: 30,
    loading: false,
    pending: false,
    refreshing: false,
    copiedId: null as string | null,
    // The rows whose 下载 was answered, keyed `device_id:entry_id`.  A download
    // is a request to another machine and the file arrives minutes later as a
    // transfer, so the row has to say something in between or the click reads as
    // a button that did nothing.  Not `copiedId`: that marks a clip that is now
    // on the clipboard, which this is not — and it is one row, where several
    // downloads can be outstanding.  Dropped when the peer refuses, and dropped
    // again on a timer — see `clearRemoteFilePending` for why the second one is
    // not belt-and-braces but the thing that keeps the button usable.
    remoteFilePending: [] as string[],
    error: null as BridgeError | null,
    notices: [] as Array<{ id: number; title: string; message: string }>,
    // The conversation the chat page currently has open, or "" for none.
    // Reported by that page rather than owned here — it is the one polling the
    // session list, so it is the one that knows — and read by the message
    // event, which does not raise a notice for a message arriving in the very
    // conversation on screen.
    openChatSession: "",
    aiInventoryEvent: null as { revision: number; event: SidecarEvent } | null,
    // One pulled AI-config file landing (or failing to).  The pull itself
    // answers with how many requests the peer accepted; the files arrive
    // afterwards, one event each, so this is the only way a page can say how a
    // pull is going rather than that it was sent.
    aiFileEvent: null as { revision: number; data: Record<string, any> } | null,
    // A pairing code finally answering.  The handshake is two frames across two
    // machines, so the half that arrives here is the *other* machine's, minutes
    // or seconds after the code was submitted and long after the call that
    // submitted it returned.  Nothing else tells the pairing card to re-read:
    // its own pull happened before the partner had said anything.
    netpairEvent: null as { revision: number; data: Record<string, any> } | null,
    // This machine's own relay link, mirrored from `relay.state.changed`.  The
    // pairing card's line reads it live: the sidecar publishes every transition
    // as it happens, where the status snapshot only says what was true when it
    // was read — and for a relay that comes up seconds after the window opened,
    // that was the moment before it connected, so the line sat on 连接中 until
    // the reader pressed 刷新.  Empty = this session has not been told, and the
    // snapshot is all there is.
    relayState: "",
    // A dialog the phone's panel asked this window to open. The request travels
    // as an event because the host has no window of its own; `revision` makes
    // two identical requests distinguishable.
    hostRequest: null as { kind: "qr" | "send_url"; revision: number } | null,
    // A device presenting a certificate that no longer matches its pin. The
    // connection was refused, so this is the only place the user learns why —
    // and the answer either re-pins the certificate or unpairs the device.
    // `can_trust` is false when the alert carried no certificate to pin.
    certAlert: null as { device_id: string; name: string; can_trust: boolean } | null,
    // A peer's answer to the update this window asked it for, already worded.
    // Worded here rather than in the page because the notice stack says the same
    // thing in the same words, and two spellings of one answer is how a reader
    // ends up wondering whether they are looking at two answers.
    updatePeerEvent: null as { revision: number; device_id: string; message: string } | null,
    // Update lifecycle, mirrored from `update.state` events and hydrated from
    // `update.status`; `updateCheck` holds the last manual lookup (null = never
    // checked), which is what drives the new-version line.
    update: {
      phase: "idle",
      fraction: 0,
      downloaded: 0,
      total: 0,
      error: "",
      version: "",
      path: "",
    } as UpdateState,
    updateCheck: null as UpdateCheckResult | null,
  });
  let disposed = false;
  let started = false;
  let unlisten: (() => void) | undefined;
  let requestSequence = 0;
  let statusSequence = 0;
  let noticeSequence = 0;
  const noticeTimers = new Map<number, ReturnType<typeof setTimeout>>();
  /** A peer's name for a notice, falling back the way legacy did.
   *
   * Two of these events are published from a path that has no name handy — a
   * refused handshake carries whatever the peer announced, and "nowhere to dial"
   * carries whatever the runtime could look up — so an unnamed peer is named
   * from the snapshot, and only then by its short id.
   *
   * A chat message names its device `peer_name`/`peer_id` rather than
   * `name`/`device_id`: it is published from the session it went into, where
   * those are the field names, and the runtime says so in `_chat_entry`.
   */
  function peerLabel(data: Record<string, unknown>): string {
    const name = String(data.name || data.peer_name || "");
    if (name) return name;
    const id = String(data.device_id || data.peer_id || "");
    return state.devices.find((known) => known.id === id)?.name || id.slice(0, 12);
  }
  /** The text of a notice; peer-supplied fields are inserted, never parsed. */
  function noticeMessage(name: string, data: Record<string, unknown>): string {
    const device = String(data.name || "");
    if (name === "pairing.request") {
      return t("{name} 请求配对 — 代码：{code}", { name: device, code: String(data.code || "") });
    }
    if (name === "chat.connect_timeout") {
      return t("无法连接到 {name}，设备可能已离线。", { name: device });
    }
    // A message arriving in a nearby conversation. The sentence is the message:
    // this notice used to carry the event's own name, so a reader was told that
    // a message had arrived and had to open the conversation to find out what
    // it said — a notification doing none of the work. An attachment has no
    // text to show, so it says what arrived instead.
    if (name === "chat.message") {
      const entry = (data.entry || {}) as Record<string, unknown>;
      if (entry.kind === "file") {
        return t("{name} 发来文件：{file}", {
          name: peerLabel(data), file: String(entry.file_name || ""),
        });
      }
      return t("{name}：{text}", { name: peerLabel(data), text: String(entry.text || "") });
    }
    // The two ways a Connect click comes to nothing. The runtime publishes the
    // reason next to the bare `{accepted: false}` answer, so the click never
    // looks like a silent no-op; the wording is legacy's, advice included.
    if (name === "device.connection_rejected") {
      // The other device answered our connection with an explicit refusal,
      // which it only sends for a peer its user removed. Say what happened and
      // where it can be undone: the repair is at the other device, and this
      // side cannot make it. The sentence this replaces hedged over whether a
      // removal had happened at all, over a marker that names it exactly.
      return t("{name} 已将本机移除 — 需要重新在对方配对", { name: peerLabel(data) });
    }
    if (name === "device.connection_unreachable") {
      return t(
        "找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络",
        { name: peerLabel(data) },
      );
    }
    if (name === "pairing.failed") return t("配对失败。验证码可能已过期。请重新连接。");
    // The engine's own failures.  The sidecar publishes one generic English
    // sentence beside the code because it cannot know which language this
    // window is in, and this is where that becomes a sentence a reader can act
    // on — the title above already says 错误, so all this owes is which one.
    if (name === "runtime.error") return t(runtimeErrorReason(String(data.code || "")));
    // The answer to "did it connect?", arriving whenever the other machine
    // answers — which is the one moment the internet-pairing card has something
    // good to report, and the only thing that distinguishes a pairing that
    // completed from one that is still waiting.  The naming is the legacy
    // panel's toast, for the same event.
    if (name === "netpair.peer.changed") {
      return t("已与 {name} 完成互联网配对", { name: peerLabel(data) });
    }
    if (name === "device.connected") return t("{name} 已连接", { name: device });
    if (name === "device.disconnected") return t("{name} 已断开", { name: device });
    // The two answers to "get me that other device's installer". Both exist
    // because a click that produced neither would be indistinguishable from a
    // broken button: the request went out, and this is what came back.
    if (name === "update.peer_unavailable") {
      // The device that was asked keeps only the installer its own upgrade
      // downloaded, so "nothing to send" means the asking device is the one
      // that has to fetch it -- from the same release, which its own update
      // page does.
      return t("{name} 上没有可发送的安装包，请在本机检查更新", { name: peerLabel(data) });
    }
    if (name === "update.peer_notice") {
      // The other direction: a peer announcing a build. Its own device list
      // already offers the fetch, so this only says which of the two happened.
      return data.has_asset === false
        ? t("{name} 有新版本 {version}，可在设备列表向它获取", {
            name: peerLabel(data),
            version: String(data.version || ""),
          })
        : t("{name} 正在把新版本 {version} 发送过来", {
            name: peerLabel(data),
            version: String(data.version || ""),
          });
    }
    // The sidecar published this from the day the filter was wired and nothing
    // ever read it, so a clip that left the device with its sensitive values
    // replaced left silently. Legacy said so; the wording is legacy's.
    if (name === "sync.redacted") return t("敏感内容未同步");
    // A peer would not hand over a file this window asked for. The reason
    // crosses the wire as a code rather than a sentence — the peer does not
    // know which language this window is in — so it is worded here, and an
    // unrecognised code still says something true rather than showing the code.
    if (name === "clip.file.denied") {
      return t("{name} 没能发送这个文件：{reason}", {
        name: peerLabel(data),
        reason: remoteFileReason(String(data.reason || "")),
      });
    }
    // The same wording the prompt used, so the notice left behind when nobody
    // answers still says what happened and what to do about it.
    if (name === "device.security_alert") {
      return t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: device });
    }
    return String(data.message || data.text || data.filename || data.name || data.url || name);
  }
  /** Why a download did not happen, in words, from the code a peer sent back.
   *
   * One place, because the same codes reach the window twice: as an event when
   * the peer answers after the fact, and as an error when this machine never
   * managed to ask.  The two the window raises itself are last, and the default
   * covers a code from a peer newer than this build — said rather than shown,
   * because a code is not something a reader can act on.
   *
   * `too_many` has no producer in this build: it is what the previous one
   * answered a request for more files than it would serve, and a peer that
   * still runs it is telling this window something true.
   */
  function remoteFileReason(reason: string): string {
    switch (reason) {
      case "not_found": return t("那台设备上已经没有这条记录了");
      case "not_local": return t("这个文件不在那台设备上");
      case "not_a_file": return t("这条记录不是文件");
      case "gone": return t("文件已被移动或删除");
      case "too_many": return t("文件太多，请分成几次下载");
      case "too_large": return t("文件太大，无法传输");
      case "empty": return t("文件夹里没有可发送的文件");
      case "failed": return t("那台设备没能把文件发出来");
      // The two the window words for itself, because it is the one that knows:
      // the request never left this machine, so no peer ever answered.
      case "offline": return t("那台设备当前不在线");
      case "not_paired": return t("已与那台设备解除配对");
      default: return t("请稍后重试");
    }
  }
  /** What the engine's own failure codes mean, in words.
   *
   * Every one of these is a thing that went wrong on this machine rather than
   * between two of them, so each says which part of the sync engine stopped
   * working and, where it is the reader's to fix, what to do about it.  An
   * unrecognised code still says something true: a code is not an answer.
   */
  function runtimeErrorReason(code: string): string {
    switch (code) {
      case "CLIPBOARD_READ_FAILED": return t("无法读取本机剪贴板，可能被其他程序占用。");
      case "CLIPBOARD_WRITE_FAILED": return t("无法写入本机剪贴板，同步的内容没有粘贴过来。");
      case "CLIPBOARD_TOO_LARGE": return t("剪贴板内容太大，本次同步已跳过。");
      case "HISTORY_WRITE_FAILED": return t("历史记录写入失败，本机磁盘可能已满。");
      case "PAIRING_SEND_FAILED": return t("配对请求没有发出去，请确认设备仍在同一网络。");
      case "RELAY_FRAME_INVALID": return t("中继收到一条无法解析的消息，已丢弃。");
      case "LAN_CALLBACK_FAILED": return t("局域网同步的内部处理出错，本次操作未完成。");
      default: return t("同步引擎遇到错误。");
    }
  }
  /** The standing notice for each device's pairing request, by notice id.
   *
   * A pairing request is the one notice that asks the reader to go do something
   * in another part of the window, so it has to go when the thing it points at
   * goes. It is also the one notice the sidecar retracts (`pairing.resolved`),
   * and a retraction is the only way this window hears about a prompt that was
   * never its own to answer — a chat invite to an unpaired device cancels it,
   * and so does the other machine's answer.
   */
  const pairingNotices = new Map<string, number>();
  /**
   * Peers whose removal this window has already reported.
   *
   * The runtime publishes the refusal on every attempt it makes — the peer's
   * reconnects and the reader's own clicks alike — and a removal cannot be
   * undone from here, so the second telling is not news. One per peer per
   * session, which is also what the reader asked for: say it, do not repeat it.
   */
  const removalNotices = new Set<string>();
  /** Forget that this peer's removal was reported, so a refusal may say it again.
   *
   * The rule above is for the refusals this window did not ask for: a peer the
   * user removed keeps dialing us, and the second telling is not news. A refusal
   * that answers the reader's own click is the opposite case — it is the only
   * thing that click produced — so the clicks that dial clear the mark first and
   * let the answer through. Without this the first refusal of the session was
   * also the last thing this window ever said about that device, and every later
   * 配对 or 连接 was a click with no reply: the bug where a device that removed
   * this one could not be re-paired from here, silently.
   */
  function rearmRemovalNotice(deviceId: string) {
    const device = String(deviceId || "");
    if (device) removalNotices.delete(device);
  }
  function pushNotice(name: string, data: Record<string, unknown>) {
    const id = ++noticeSequence;
    state.notices.push({ id, title: name, message: noticeMessage(name, data) });
    if (name === "pairing.request") {
      const device = String(data.device_id || "");
      if (device) pairingNotices.set(device, id);
    }
    if (state.notices.length > 5) dismissNotice(state.notices[0].id);
    noticeTimers.set(id, setTimeout(() => dismissNotice(id), 6000));
    return id;
  }
  /** Retract a device's pairing notice, if one is standing. */
  function dismissPairingNotice(deviceId: string) {
    const id = pairingNotices.get(deviceId);
    if (id === undefined) return;
    pairingNotices.delete(deviceId);
    dismissNotice(id);
  }

  /** Say that a click did something, for the clicks that show nothing.
   *
   * Most of the window's buttons announce themselves: a page opens, a row
   * leaves the list, a switch flips, a dialog closes. The rest — a refresh that
   * found the same rows, a copy whose only trace was a tick in a list the eye
   * had already left, a save that leaves the form exactly as it was — answered
   * the click with nothing at all, and a reader cannot tell those apart from a
   * button that is broken. Those say so here.
   *
   * They ride the notice stack rather than a surface of their own, so a copy
   * and a peer's pairing request queue up, expire and dismiss the same way, and
   * they are keyed the same way too: `key` names the area (`ui.history`) and
   * `NoticeStack` translates it, which keeps the message the only thing a
   * caller has to word. They also go sooner than an event's six seconds —
   * nothing here needs reading twice.
   */
  function toast(key: string, message: string, ms = 3500) {
    const id = ++noticeSequence;
    state.notices.push({ id, title: key, message });
    if (state.notices.length > 5) dismissNotice(state.notices[0].id);
    noticeTimers.set(id, setTimeout(() => dismissNotice(id), ms));
  }
  function dismissNotice(id: number) {
    clearTimeout(noticeTimers.get(id));
    noticeTimers.delete(id);
    // A dismissed notice is nobody's to retract any more: the map is keyed by
    // device, so an entry left behind would let a later `pairing.resolved`
    // dismiss whatever notice happens to hold that id now.
    for (const [device, notice] of pairingNotices) {
      if (notice === id) pairingNotices.delete(device);
    }
    state.notices = state.notices.filter((notice) => notice.id !== id);
  }
  /** How long the certificate prompt waits for an answer before it gives up.
   *
   * Legacy's web confirm dialog waited the same two minutes, then answered
   * "nobody decided": nothing changed on either side, and an info toast said
   * why. The prompt is still raised again by the peer's next connection.
   */
  const CERT_PROMPT_TIMEOUT = 120_000;
  let certPromptTimer: ReturnType<typeof setTimeout> | undefined;
  function clearCertPrompt() {
    clearTimeout(certPromptTimer);
    certPromptTimer = undefined;
    state.certAlert = null;
  }

  /** How long a 下载 request stays marked as outstanding.
   *
   * The mark exists to stop a second click from asking twice, and the peer's
   * answer is not the only way it ends: a peer that serves the file simply
   * starts sending, and nothing on this side carries the entry id back — the
   * transfers list knows a transfer, not which row asked for it.  So the mark
   * is released on a clock as well as on a refusal.  Waiting forever would be
   * the one outcome worth avoiding: the row's 下载 would stay greyed out over a
   * file that had already arrived — after a restart, permanently.
   */
  const REMOTE_FILE_TIMEOUT = 30_000;
  const remoteFileTimers = new Map<string, ReturnType<typeof setTimeout>>();
  function clearRemoteFilePending(key: string) {
    const timer = remoteFileTimers.get(key);
    if (timer !== undefined) clearTimeout(timer);
    remoteFileTimers.delete(key);
    state.remoteFilePending = state.remoteFilePending.filter((open) => open !== key);
  }
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  const favorites = createFavoritesStore(() => state.status?.health === "ready" &&
    !!state.status.capabilities?.includes("favorites.list"),
  (message) => toast("ui.favorites", message));
  // Relay delivery receipts ride the same event stream as everything else, so
  // the mirror lives beside the store rather than in the window: a receipt has
  // to outlive the settings section it is rendered in, and the chat tab reads
  // the same map.
  const delivery = createDeliveryStore(() => state.status?.health === "ready");

  /** Error codes that mean the sidecar itself is down, not the request. */
  const SIDECAR_DOWN_CODES = ["SIDECAR_UNAVAILABLE", "SIDECAR_START_FAILED", "STARTUP_TIMEOUT"];
  /** Codes the user clears from outside the application and then retries. */
  const USER_FIXABLE_CODES = ["DATA_IN_USE"];

  /** The line the failure band shows for a sidecar that gave up.
   *
   * A refused data directory is the one failure here the user can act on --
   * the legacy application is still running and holding it -- so it gets a
   * translated line naming the cause and the fix, rather than the sidecar's
   * English sentence or a generic notice beside a retry button that cannot
   * work until the other application is closed. */
  function sidecarFailureMessage(code?: string, message?: string): string {
    if (code === "DATA_IN_USE") {
      return t("数据目录正被旧版 ClipSync 占用。请关闭旧版应用后重试。");
    }
    return message || t("后台进程不可用，请重试");
  }

  function setError(error: unknown) {
    const value = typeof error === "object" && error !== null && "code" in error
      ? error as BridgeError
      : { code: "CONNECTION_FAILED", message: t("连接失败，请重试"), retryable: true };
    // A dead sidecar is recoverable now that the host can relaunch it, whatever
    // the error's own flag says — the host reports these as non-retryable. A
    // refused data directory reports itself as non-retryable too, and is kept
    // retryable for the opposite reason: closing the other application is what
    // makes the next attempt succeed.
    state.error = SIDECAR_DOWN_CODES.includes(value.code) || USER_FIXABLE_CODES.includes(value.code)
      ? { ...value, retryable: true }
      : value;
  }

  async function refreshHistory() {
    if (disposed) return;
    const request = ++requestSequence;
    state.loading = true;
    try {
      const page = await bridge.history(state.query, state.offset, state.limit, state.kind, state.sort);
      if (disposed || request !== requestSequence) return;
      state.history = page.items;
      const visibleIds = new Set(page.items.map((item) => item.id));
      state.selectedIds = state.selectedIds.filter((id) => visibleIds.has(id));
      // Clamp rather than reset: a delete shortens the list under the cursor and
      // a continued Delete should walk on down without skipping a row.  The
      // callers that replace the whole list (search, chip, sort, page) reset it
      // themselves, where a clamp would leave the cursor pointing at a stranger.
      if (state.kbdIndex >= state.history.length) state.kbdIndex = state.history.length - 1;
      state.total = page.total;
      state.counts = page.counts || {};
      state.hasHistory = !!page.has_history;
    } catch (error) {
      if (!disposed && request === requestSequence) setError(error);
    } finally {
      if (!disposed && request === requestSequence) state.loading = false;
    }
  }

  async function refresh() {
    if (disposed) return;
    const request = ++statusSequence;
    ++requestSequence;
    state.loading = false;
    state.refreshing = true;
    try {
      const status = await bridge.status();
      if (disposed || request !== statusSequence) return;
      state.status = status;
      if (status.health === "ready" && status.capabilities?.includes("favorites.list")) {
        void favorites.refresh();
      } else {
        favorites.reset();
        // Receipts came from that process; a new one has an empty ledger, and a
        // bubble stamped from the old one would keep claiming 已送达.
        delivery.reset();
      }
      if (status.health === "ready") {
        const devices = await bridge.devices();
        if (disposed || request !== statusSequence) return;
        state.devices = devices.items;
        await refreshHistory();
      } else {
        state.devices = [];
        state.history = [];
        state.selectedIds = [];
        state.total = 0;
      }
    } catch (error) {
      if (!disposed && request === statusSequence) setError(error);
    } finally {
      if (!disposed && request === statusSequence) state.refreshing = false;
    }
  }

  async function start() {
    if (started || disposed) return;
    started = true;
    try {
      const off = await bridge.subscribe(
      (event) => {
          if (disposed) return;
          const data = event.data || {};
          // Update events describe a background job, not clipboard data: apply
          // them in place and stop — a global refresh would only add latency
          // and would reset the progress the panel is rendering.
          if (event.name === "update.state") {
            const next = data.state as UpdateState | undefined;
            if (next && typeof next.phase === "string") Object.assign(state.update, next);
            return;
          }
          if (event.name === "update.available") {
            state.updateCheck = {
              available: true,
              latest: String(data.latest || ""),
              current: String(data.current || ""),
              url: String(data.url || ""),
              // The host answers this before forwarding the notice (see
              // `bridge.rs`), so by the time the window sees it the answer is
              // the plugin's own.  Spelled through a boolean test rather than
              // copied: the field is absent when the manifest could not be read,
              // and a truthy string would otherwise read as "yes".  Absent is
              // kept as `undefined` — the card offers the install for anything
              // but a definite `false`.
              installable: typeof data.installable === "boolean" ? data.installable : undefined,
            };
            return;
          }
          // UI requests from the phone's panel: the host owns no window, so it
          // asks this one. Nothing in the data changed — skip the refresh.
          if (event.name === "app.window_close_requested") return;
          if (event.name === "app.qr_requested" || event.name === "app.send_url_requested") {
            state.hostRequest = {
              kind: event.name === "app.qr_requested" ? "qr" : "send_url",
              revision: (state.hostRequest?.revision || 0) + 1,
            };
            return;
          }
          if (event.name === "device.security_alert") {
            // A security decision, not a toast: the last alert wins, and it
            // stands until the user answers it or the wait runs out.
            state.certAlert = {
              device_id: String(data.device_id || ""),
              name: String(data.name || ""),
              can_trust: data.can_trust === true,
            };
            clearTimeout(certPromptTimer);
            const asked = { ...data };
            certPromptTimer = setTimeout(() => {
              if (disposed || !state.certAlert) return;
              clearCertPrompt();
              pushNotice("device.security_alert", asked);
            }, CERT_PROMPT_TIMEOUT);
          }
          if ((event.name === "aiconfig.file" && data.type === "aiconfig_inventory") || event.type === "resync") {
            state.aiInventoryEvent = { revision: (state.aiInventoryEvent?.revision || 0) + 1, event };
          }
          // A file the peer sent because this window asked for it.  Kept apart
          // from the inventory above: the inventory says what the peer has, and
          // this says what has actually arrived.
          if (event.name === "aiconfig.file" && data.type === "aiconfig_file") {
            state.aiFileEvent = { revision: (state.aiFileEvent?.revision || 0) + 1, data };
          }
          // The relay link itself: coming up, dropping, or failing.  Folded in
          // place, since the payload *is* the state — there is nothing to
          // re-read.  It is also the one line on that card that changes with no
          // user action behind it, so an event is the only thing that can keep
          // it honest between reads.
          if (event.name === "relay.state.changed") {
            state.relayState = String(data.state || "");
          }
          // A delivery receipt describes one send, not the clipboard: fold it
          // in place and stop.  Left to the fall-through below it would repaint
          // the whole history list on every transition — a screenshot of a
          // large transfer's acks, for nothing.
          if (event.name === "relay.delivery.changed") {
            delivery.apply(data);
            return;
          }
          if (event.name === "netpair.peer.changed") {
            // Kept, and then left to fall through: the card has to re-read the
            // peers (the event carries one row, not the list), and a confirmed
            // pairing is worth saying out loud.  `unpaired` is the same event
            // with the other status and is reported by whoever asked for it, so
            // it only re-reads.
            state.netpairEvent = {
              revision: (state.netpairEvent?.revision || 0) + 1,
              data,
            };
            if (data.status === "paired") pushNotice(event.name, data);
          }
          // A download this window asked for that the peer could not serve.  It
          // is reported rather than left to the refresh: nothing on screen
          // changes when a file does not arrive, so without this the 下载 button
          // would be the one control in the window that can fail silently.
          if (event.name === "clip.file.denied") {
            pushNotice(event.name, data);
            clearRemoteFilePending(`${String(data.device_id || "")}:${String(data.entry_id || "")}`);
          }
          if (event.name === "pairing.resolved") {
            // The prompt is over — answered, cancelled, or dropped because the
            // peer was only inviting this device to a chat, which is not a
            // pairing request at all.  The notice told the reader to go answer
            // a card on the devices page, so it has to go with the card: six
            // seconds of "wants to pair" over a pairing that no longer exists
            // is how a chat invite reads as one.
            dismissPairingNotice(String(data.device_id || ""));
          }
          // Suppressed, not dropped: the rest of the handler still has to run —
          // the refusal changes this device's connection state like any other
          // disconnect event.
          if (event.name === "update.peer_unavailable" || event.name === "update.peer_notice") {
            // The device row carries a note from the click that started this, and
            // it says the transfer is on its way.  The notice stack says the same
            // thing for six seconds; the note stays until this overwrites it, so
            // an answer that is never written here leaves the row promising a
            // file that is not coming.
            state.updatePeerEvent = {
              revision: (state.updatePeerEvent?.revision || 0) + 1,
              device_id: String(data.device_id || ""),
              message: noticeMessage(event.name, data),
            };
          }
          // A nearby-chat message arriving.  Two ways it is not news, and both
          // are silence rather than a shorter notice:
          //
          //   - it is this window's own message coming back.  The engine fires
          //     its message callback for outgoing entries too — the sender's
          //     own echo — so the sender was told about the message they had
          //     just typed, on the same screen they typed it on.
          //   - the conversation it belongs to is already open in front of
          //     them, where the bubble arriving *is* the notification.  Only
          //     that conversation: a message in another one is still news, and
          //     the chat page is unmounted the moment its tab is left, which is
          //     when the open session is forgotten.
          if (event.name === "chat.message") {
            const entry = (data.entry || {}) as Record<string, unknown>;
            if (entry.outgoing !== true && String(data.session_id || "") !== state.openChatSession) {
              pushNotice(event.name, data);
            }
          }
          let alreadyReportedRemoval = false;
          if (event.name === "device.connection_rejected") {
            const peer = String(data.device_id || "");
            alreadyReportedRemoval = removalNotices.has(peer);
            removalNotices.add(peer);
          }
          if (!alreadyReportedRemoval && event.name && ["runtime.error", "pairing.request", "transfer.request", "chat.connect_timeout", "url.received", "device.connected", "device.disconnected", "sync.redacted", "device.connection_rejected", "device.connection_unreachable", "update.peer_unavailable", "update.peer_notice"].includes(event.name)) {
            pushNotice(event.name, data);
          }
          if (event.name === "favorites.changed" || event.name === "data.changed" || event.type === "resync") favorites.invalidate();
          ++statusSequence;
          ++requestSequence;
          state.loading = false;
          state.refreshing = true;
          // Events invalidate queries; snapshots remain authoritative. Subscribe
          // before the first query so an in-flight mutation cannot be missed.
          clearTimeout(refreshTimer);
          refreshTimer = setTimeout(() => { if (!disposed) void refresh(); }, 60);
        },
        (update) => {
          if (disposed) return;
          if (update.state === "failed" || update.state === "restarting") {
            // Both mean the sidecar is not answering, so drop everything that
            // came from it rather than leaving half a snapshot on screen. The
            // host relaunches on its own after `restarting`; `failed` means it
            // gave up, and the band's retry button asks for one more attempt.
            const restarting = update.state === "restarting";
            favorites.reset();
            delivery.reset();
            // The relay state too: it is a claim about a process that is gone,
            // and the relaunched one publishes its own as it comes up.  Kept, it
            // would outlive the sidecar and outrank the first status read of the
            // new one — the card line is drawn from this value before anything
            // else, so a stale 连接中 would sit over a relay that is already up.
            state.relayState = "";
            clearTimeout(refreshTimer);
            ++statusSequence;
            ++requestSequence;
            state.loading = false;
            state.refreshing = false;
            state.devices = [];
            state.history = [];
            state.selectedIds = [];
            state.total = 0;
            // The chips read the dead process's numbers otherwise: 暂无历史记录
            // over an empty list, with 全部 128 · 文本 90 still on screen and
            // still clickable, against a sidecar that is not there.
            state.counts = {};
            state.hasHistory = false;
            state.kbdIndex = -1;
            if (state.status) state.status.health = "stopped";
            setError(restarting
              ? { code: "SIDECAR_RESTARTING",
                message: t("后台进程已退出，正在重新启动（第 {attempt} 次）…", { attempt: update.attempt || 1 }),
                retryable: false }
              : { code: update.error || "SIDECAR_UNAVAILABLE",
                message: sidecarFailureMessage(update.error, update.message),
                retryable: true });
          } else if (update.state === "ready" && state.error) {
            // The relaunched sidecar is up: clear the failure and pull the
            // snapshot the dropped UI is missing.
            state.error = null;
            ++statusSequence;
            ++requestSequence;
            void refresh();
          }
        },
      );
      if (disposed) { off(); return; }
      unlisten = off;
      await refresh();
    } catch (error) {
      if (!disposed) setError(error);
    }
  }

  /**
   * The retry behind the error band. A dead sidecar needs a relaunch first —
   * refreshing against a process that is gone would only fail again — while any
   * other retryable error is a transport hiccup that a refresh settles.
   */
  async function reconnect() {
    const down = !!state.error && SIDECAR_DOWN_CODES.includes(state.error.code);
    state.error = null;
    if (down && !disposed) {
      state.pending = true;
      try {
        await bridge.restartSidecar();
      } catch (error) {
        if (!disposed) setError(error);
        return;
      } finally {
        if (!disposed) state.pending = false;
      }
    }
    await refresh();
  }

  /**
   * Move damaged configuration, identity or history aside so the app can start.
   *
   * Offered when the sidecar reports its data directory as invalid: it refuses
   * to start on exactly that damage, so no RPC call — not even the factory
   * reset — can be used to get out of it. The host runs the repair as a
   * one-shot process and relaunches the sidecar, so success only needs the
   * snapshot re-read; the files moved are named in a notice, because they are
   * renamed rather than deleted and a user may want them back.
   */
  async function recoverData() {
    if (disposed || state.pending) return false;
    state.pending = true;
    state.error = null;
    try {
      const result = await bridge.recoverDataDir() as RecoveryResult;
      if (disposed) return false;
      const files = (result.items || []).flatMap((item) => item.files || []);
      if (files.length) {
        const id = ++noticeSequence;
        state.notices.push({
          id,
          title: t("数据修复"),
          message: t("已将损坏的文件移到一旁：{files}", { files: files.join(", ") }),
        });
        if (state.notices.length > 5) dismissNotice(state.notices[0].id);
        // Long enough to copy a file name out of, unlike a passing event.
        noticeTimers.set(id, setTimeout(() => dismissNotice(id), 60000));
      }
      await refresh();
      return true;
    } catch (error) {
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  /** Run a route and report its outcome.
   *
   * `eventReported` names result fields whose `false` is not a failure to report
   * here, because the runtime publishes the reason as an event of its own — the
   * connect route does exactly that for "there was nowhere to dial", and legacy
   * left the same click to its own toasts for the same reason.
   */
  async function action(run: () => Promise<unknown>, eventReported: string[] = []) {
    if (disposed || state.pending) return false;
    state.pending = true;
    state.error = null;
    try {
      const result = await run();
      if (disposed) return false;
      const refused = (key: string) =>
        key in (result as Record<string, unknown>) &&
        (result as Record<string, unknown>)[key] === false &&
        !eventReported.includes(key);
      if (result && typeof result === "object" && (refused("accepted") || refused("copied"))) {
        throw { code: "ACTION_REJECTED", message: t("操作未完成，请刷新后重试"), retryable: true };
      }
      await refresh();
      return true;
    } catch (error) {
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  function historyBusy() {
    return disposed || state.pending || state.loading || state.refreshing || state.status?.health !== "ready";
  }

  function selectedBatch(ids: string[]) {
    const unique = [...new Set(ids)];
    if (!unique.length || unique.length > 100 || unique.some((id) =>
      !id.trim() || !state.selectedIds.includes(id) || !state.history.some((item) => item.id === id))) {
      setError({ code: "INVALID_SELECTION", message: t("所选记录已变化，请重新选择"), retryable: false });
      return null;
    }
    return unique;
  }

  function discoveryAvailable() {
    return state.status?.health === "ready" &&
      !!state.status.capabilities?.includes("discovery.status");
  }

  async function discoveryToggle(kind: "enabled" | "visible", enabled: boolean) {
    // Both switches return the resulting discovery state so the UI reflects
    // what the runtime reports rather than what was requested.
    if (disposed || state.pending || !discoveryAvailable()) return null;
    state.pending = true;
    state.error = null;
    try {
      return kind === "enabled"
        ? await bridge.setDiscoveryEnabled(enabled)
        : await bridge.setDiscoveryVisible(enabled);
    } catch (error) {
      if (!disposed) setError(error);
      return null;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  return {
    state, favorites, delivery, start, refreshHistory,
    dismissNotice, toast,
    /** Which conversation the chat page has on screen, or "" for none. */
    setOpenChatSession(sessionId: string) {
      state.openChatSession = String(sessionId || "");
    },
    refresh() { state.error = null; return refresh(); },
    // The error band's retry: relaunches a dead sidecar, then refreshes.
    reconnect,
    // The error band's repair, offered only for a data directory the sidecar
    // refuses to start on.
    recoverData,
    select(id: string, selected: boolean) {
      if (historyBusy() || !id.trim() || !state.history.some((item) => item.id === id)) return;
      if (!selected) state.selectedIds = state.selectedIds.filter((value) => value !== id);
      else if (!state.selectedIds.includes(id) && state.selectedIds.length < 100) state.selectedIds.push(id);
    },
    selectAllVisible(selected: boolean) {
      if (historyBusy()) return;
      state.selectedIds = selected
        ? [...new Set(state.history.map((item) => item.id).filter((id) => id.trim()))].slice(0, 100)
        : [];
    },
    /** Walk the keyboard cursor down (`+1`) or up (`-1`) the visible page.
     * A cursor at -1 enters the list at the top rather than the second row, so
     * nobody opens the page with a row already chosen; the ends clamp rather
     * than wrap, which is what the legacy list did too. */
    kbdStep(delta: number) {
      if (historyBusy() || !state.history.length) return;
      const next = state.kbdIndex < 0 ? 0 : state.kbdIndex + delta;
      state.kbdIndex = Math.max(0, Math.min(next, state.history.length - 1));
    },
    batchPin(pinned: boolean) {
      if (historyBusy()) return Promise.resolve(false);
      const ids = selectedBatch(state.selectedIds);
      return ids ? action(() => bridge.batchPinHistory(ids, pinned)) : Promise.resolve(false);
    },
    batchDelete(entryIds: string[]) {
      if (historyBusy()) return Promise.resolve(false);
      const ids = selectedBatch(entryIds);
      return ids ? action(() => bridge.batchDeleteHistory(ids)) : Promise.resolve(false);
    },
    async batchFavorite(entryIds: string[], group = "") {
      // Returns how many entries were copied into the favorites store (0 on
      // failure) so the caller can report the count, like the legacy toast.
      if (historyBusy()) return 0;
      const ids = selectedBatch(entryIds);
      if (!ids) return 0;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.batchFavoriteHistory(ids, group);
        if (disposed) return 0;
        await refresh();
        return result.added;
      } catch (error) {
        if (!disposed) setError(error);
        return 0;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async clearHistory() {
      if (historyBusy()) return 0;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.clearHistory();
        if (disposed) return 0;
        state.selectedIds = [];
        state.kbdIndex = -1;
        state.offset = 0;
        await refresh();
        return result.cleared;
      } catch (error) {
        if (!disposed) setError(error);
        return 0;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    search(query: string) {
      if (disposed || state.pending) return;
      if (query !== state.query || state.offset !== 0) state.selectedIds = [];
      state.kbdIndex = -1;
      state.query = query;
      state.offset = 0;
      void refreshHistory();
    },
    /** Narrow the list to one of the panel's kind chips. */
    setKind(kind: string) {
      if (disposed || state.pending || kind === state.kind) return;
      // Back to the first page either way: a narrowing filter applied to the
      // thirty-first row of the old list would open on an empty page, and the
      // selection cannot survive rows that the filter just removed.
      state.selectedIds = [];
      state.kbdIndex = -1;
      state.kind = kind;
      state.offset = 0;
      void refreshHistory();
    },
    /** Flip between newest- and oldest-first. */
    toggleSort() {
      if (disposed || state.pending) return;
      state.sort = state.sort === "newest" ? "oldest" : "newest";
      state.selectedIds = [];
      state.kbdIndex = -1;
      state.offset = 0;
      void refreshHistory();
    },
    page(direction: number) {
      if (historyBusy()) return;
      const offset = Math.max(0, state.offset + direction * state.limit);
      if (offset !== state.offset) state.selectedIds = [];
      if (offset !== state.offset) state.kbdIndex = -1;
      state.offset = offset;
      void refreshHistory();
    },
    unlock: (password: string) => action(() => bridge.unlock(password)),
    async pin(item: HistoryItem) {
      if (!(await action(() => bridge.pinHistory(item.id, !item.pinned)))) return false;
      toast("ui.history", item.pinned ? t("已取消置顶这条记录") : t("已置顶这条记录"));
      return true;
    },
    async delete(item: HistoryItem) {
      if (!(await action(() => bridge.deleteHistory(item.id)))) return false;
      toast("ui.history", t("已删除这条记录"));
      return true;
    },
    /** Put a clip back on the clipboard.
     *
     * The row's own answer is a tick that replaces its copy icon, which is the
     * whole of the confirmation — and the row is in a list the reader is
     * looking past by the time it lands, or on a feed that has already been
     * scrolled. So it says so in words as well, and names what was copied when
     * the clip is short enough to read at a glance.
     */
    async copy(item: HistoryItem) {
      state.copiedId = null;
      if (!(await action(() => bridge.copyHistory(item.id)))) return;
      state.copiedId = item.id;
      toast("ui.history", t("已复制"));
    },
    /** Ask the device holding a file to send it.
     *
     * The row's own button on a file that lives on another machine.  Nothing is
     * copied and nothing is written here: the request goes out, the peer starts
     * a transfer, and the file lands in the receive folder like any other
     * download.  So the answer is a word rather than the tick `copy` shows —
     * a tick on a row means the clip is on this clipboard, and this one is not
     * on this machine at all yet.
     *
     * A row with no source device has nobody to ask: the clip was captured here,
     * so its file is already on this disk and 复制 is the action for it.
     */
    async downloadRemoteFile(item: HistoryItem) {
      const deviceId = item.source_device || "";
      if (!deviceId) return false;
      const key = `${deviceId}:${item.id}`;
      state.remoteFilePending = [key, ...state.remoteFilePending];
      try {
        await bridge.requestEntryFiles(item.id, deviceId);
      } catch (error) {
        clearRemoteFilePending(key);
        // The three ways the ask never left, named rather than shown as a code:
        // the peer is offline, the peer is not paired any more, or the request
        // itself was refused.  A peer that answered and could not serve it
        // arrives later as `clip.file.denied` and is reported there.
        const code = (error as BridgeError | undefined)?.code || "";
        if (code === "NOT_CONNECTED") {
          pushNotice("clip.file.denied", { device_id: deviceId, reason: "offline" });
        } else if (code === "NOT_PAIRED") {
          pushNotice("clip.file.denied", { device_id: deviceId, reason: "not_paired" });
        } else {
          setError(error);
        }
        return false;
      }
      // Asked, answered: the peer will either start sending or refuse, and the
      // refusal clears the mark early.  This is the backstop for the other half.
      const timer = remoteFileTimers.get(key);
      if (timer !== undefined) clearTimeout(timer);
      remoteFileTimers.set(key, setTimeout(() => {
        if (!disposed) clearRemoteFilePending(key);
      }, REMOTE_FILE_TIMEOUT));
      return true;
    },
    startPairing: (device: Device) =>
      action(() => {
        rearmRemovalNotice(device.id);
        return bridge.startPairing(device.id);
      }),
    /** Confirm the code, and say so when it did not take.
     *
     * The runtime answers `{paired, status}` rather than the `accepted` the
     * other actions carry, so a confirm that did not pair is not an error and
     * `action` would report it as a plain success.  It means the request was
     * already over — the code expired before it was answered, which is exactly
     * what the legacy dialog `notify.pairing_failed` was there to say.  Without
     * it the card simply vanishes and the click looks like it did nothing.
     *
     * A confirm that *did* pair says nothing: the row turns paired and the
     * runtime publishes `device.connected`, so a second message for the same
     * moment would only be noise.
     *
     * `paired: false` alone is not that case.  Confirming on this device while
     * the other one has not answered yet answers `confirmed_waiting` — a
     * handshake in flight, which the row already renders as 等待对方确认.  Read
     * as failure it told the user the opposite of what was happening, so the
     * status decides: only a handshake that is over (`cancelled`, or none at
     * all) means the confirm did not take.  The set is closed and lives in
     * `PAIRING_STATUS_*`, internal/security/pairing.py.
     */
    async confirmPairing(device: Device) {
      if (disposed || state.pending) return false;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.confirmPairing(device.id, device.pairing_code || "");
        if (disposed) return false;
        await refresh();
        if (result.paired === false && !PAIRING_IN_FLIGHT.includes(result.status)) {
          pushNotice("pairing.failed", {});
        }
        return true;
      } catch (error) {
        if (!disposed) setError(error);
        return false;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    rejectPairing: (device: Device) => action(() => bridge.rejectPairing(device.id)),
    unpair: (device: Device) => action(() => bridge.unpairDevice(device.id)),
    /** Answer a certificate-change prompt by pinning what the device presented.
     *
     * The prompt closes only when the runtime accepted the answer: a device that
     * has not connected with its new certificate yet cannot be trusted, and
     * leaving the prompt up (with the error in the band) is what lets the user
     * try again once it has.
     */
    async retrust(deviceId: string) {
      const ok = await action(() => bridge.retrustDevice(deviceId));
      if (ok && state.certAlert?.device_id === deviceId) clearCertPrompt();
      return ok;
    },
    /** Answer it the other way: the changed device stays unpaired. Unpairing is
     * what that answer means, so it runs the existing path — and the runtime
     * voids the prompt with it. Takes an id, because a device that only ever
     * refused its handshake may not be in the snapshot at all. */
    async keepUnpaired(deviceId: string) {
      const ok = await action(() => bridge.unpairDevice(deviceId));
      if (ok && state.certAlert?.device_id === deviceId) clearCertPrompt();
      return ok;
    },
    /** Ask the runtime to dial a device.
     *
     * The route answers whether a dial was *started*, not whether it connected,
     * so `accepted: false` is not reported as a failed action: it means there was
     * nowhere to dial, and the runtime says so itself in
     * `device.connection_unreachable` — a generic "refresh and try again" beside
     * that notice would be advice that cannot help. The outcome of a dial that did
     * start arrives as the card's own state (or a refusal from the peer), never as
     * an optimistic "connected" from here.
     */
    connect: (device: Device) =>
      action(() => {
        rearmRemovalNotice(device.id);
        return bridge.connectDevice(device.id);
      }, ["accepted"]),
    disconnect: (device: Device) => action(() => bridge.disconnectDevice(device.id)),
    /** Open a clip that is a web link in the browser.
     *
     * The window names the row and the sidecar reads it: the URL never crosses
     * the IPC from here, and the app only opens what the row already holds. Not
     * run through `action` because nothing about the list changes — an open is
     * not a mutation, so there is no snapshot to re-read afterwards.
     */
    async openLink(item: HistoryItem) {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.openHistoryLink(item.id);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    forget: (device: Device) => action(() => bridge.forgetDevice(device.id)),
    restore: (device: Device) => action(() => bridge.restoreDevice(device.id)),
    purge: (device: Device) => action(() => bridge.purgeDevice(device.id)),
    async probe(device: Device) {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.testDevice(device.id);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async certificates() {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return (await bridge.deviceCerts()).devices;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async setSyncEnabled(enabled: boolean) {
      if (state.status?.health !== "ready" ||
        !state.status.capabilities?.includes("sync.set_enabled")) return;
      if (!(await action(() => bridge.setSyncEnabled(enabled)))) return;
      // The switch is on the overview and the footer's word for the same fact
      // is at the bottom of the window, so the answer is worth saying next to
      // the click as well.
      toast("ui.sync", enabled ? t("同步已开启") : t("同步已关闭"));
    },
    async sendUrl(device: Device, url: string) {
      // Returns whether the frame actually left this machine. The runtime
      // rejects non-http(s) URLs, so the only local check is the length bound
      // the closed RPC enforces.
      if (disposed || state.pending) return false;
      if (!url.trim() || url.length > 2048) {
        setError({ code: "INVALID_URL", message: t("请输入有效的网址"), retryable: false });
        return false;
      }
      state.pending = true;
      state.error = null;
      try {
        await bridge.sendUrl(device.id, url);
        return !disposed;
      } catch (error) {
        if (!disposed) setError(error);
        return false;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async pushText(text: string) {
      // Writes the local clipboard AND broadcasts: `sent` is false when sync
      // is off, which is still a success for the clipboard half.
      if (disposed || state.pending) return null;
      if (!text.trim() || text.length > 100000) {
        setError({ code: "INVALID_TEXT", message: t("请输入要推送的文本"), retryable: false });
        return null;
      }
      state.pending = true;
      state.error = null;
      try {
        return await bridge.pushText(text);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async discovery() {
      if (disposed || state.pending || !discoveryAvailable()) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.discoveryStatus();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async diagnostics() {
      // Live snapshot built by the same use case the legacy panel renders, so
      // the two surfaces cannot disagree about the state of this machine.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.diagnosticsReport();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async repairDiagnostics(kind: DiagnosticAction) {
      // The caller re-runs diagnostics afterwards: the repair only opens the
      // OS settings or applies the rule, it does not prove the check passes.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.diagnosticsRequest(kind);
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async loadUpdateStatus() {
      // Hydration after a reload: the sidecar keeps the live phase, so a
      // download that started before this window opened still renders.
      if (disposed || !state.status?.capabilities?.includes("update.status")) return null;
      try {
        const result = await bridge.updateStatus();
        // Opening settings mid-install must not undo it. The sidecar only knows
        // its own four phases and would answer `idle`, putting the download
        // button back while the bundle is being replaced underneath it.
        if (!disposed && result?.state && state.update.phase !== "installing") {
          Object.assign(state.update, result.state);
        }
        return result;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      }
    },
    async checkUpdate() {
      // Bounded server-side (~8s); a failure is reported as "unknown", never
      // as "up to date", so the caller can tell the two apart.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        const result = await bridge.updateCheck();
        if (disposed) return null;
        state.updateCheck = result.latest || result.available ? result : null;
        return result;
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async installUpdate() {
      // There is no success path that returns to render: installing replaces
      // this installation and relaunches the app. A reply therefore means the
      // install did not happen, and the host has already published the failing
      // phase, so the caller only has to decide what to say about it.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.updateInstall();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async downloadUpdate() {
      // Returns immediately; progress arrives as `update.state` events.
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.updateDownload();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    async openUpdateFolder() {
      if (disposed || state.pending) return null;
      state.pending = true;
      state.error = null;
      try {
        return await bridge.updateOpenFolder();
      } catch (error) {
        if (!disposed) setError(error);
        return null;
      } finally {
        if (!disposed) state.pending = false;
      }
    },
    setDiscoveryEnabled: (enabled: boolean) => discoveryToggle("enabled", enabled),
    setDiscoveryVisible: (enabled: boolean) => discoveryToggle("visible", enabled),
    dispose() {
      for (const id of noticeTimers.keys()) dismissNotice(id);
      favorites.dispose();
      delivery.dispose();
      disposed = true;
      ++requestSequence;
      ++statusSequence;
      clearTimeout(refreshTimer);
      clearTimeout(certPromptTimer);
      for (const timer of remoteFileTimers.values()) clearTimeout(timer);
      remoteFileTimers.clear();
      unlisten?.();
    },
  };
}
