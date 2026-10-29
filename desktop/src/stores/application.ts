import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { AppStatus, BridgeError, Device, DiagnosticAction, DiagnosticsReport, HistoryItem, RecoveryResult, SidecarEvent, UpdateCheckResult, UpdateState } from "../api/types";
import { createFavoritesStore } from "./favorites";
import { createDeliveryStore } from "./delivery";
import { requestAttention } from "../lib/attention";

/** A pairing handshake that has not finished yet.
 *
 * `confirm_pairing` answers `paired: false` for these, and that is not a
 * failure: the confirm landed here and the other device has not answered —
 * `confirmed_waiting` is the usual one, `peer_confirmed` is the same moment
 * from the other side.  Mirrors the closed `PAIRING_STATUS_*` set in
 * internal/security/pairing.py, and the three App.vue's `pairingPending`
 * already treats as a live card.
 */
/** The notices that are questions rather than reports.
 *
 * Everything else on the stack is news about something that has already
 * happened — a device connected, a sync was redacted, a copy landed — and
 * expires because the news goes stale.  These two ask the reader something, and
 * an unanswered question does not go stale: it fails.  They stay until they are
 * answered, dismissed, or withdrawn by the event that answers them.
 */
const ACTION_NOTICES: ReadonlySet<string> = new Set(["pairing.request", "transfer.request"]);

/** A button on a notice: an answer the reader can give where the question
 *
 * appeared.  Only some notices have one, and only the questions do — a report
 * about something that has already happened has nothing to press.  `run` is
 * awaited by the stack, which closes the notice first; the store owns what the
 * button means, so the title/name split stays intact.
 */
export interface NoticeAction {
  label: string;
  primary?: boolean;
  run: () => void | Promise<void>;
}

/** One card on the notice stack.
 *
 * `title` is a name, not a sentence: `NoticeStack` words it from its own table,
 * and an already-translated string could not be re-worded for the interface
 * language.  `message` is the sentence, resolved by the store because only the
 * store knows the event's payload.
 */
export interface Notice {
  id: number;
  title: string;
  message: string;
  actions?: NoticeAction[];
}

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
    // When the window stopped being able to reach the sidecar, after two
    // refusals in a row.  A single failure is a blip and says nothing; two is the
    // moment the page on screen became a photograph of an older one, and it was
    // the one thing the window never admitted.
    staleSince: null as number | null,
    pollFailures: 0,
    // Clicks that arrived while a write was already in flight.
    //
    // The guards below drop the second call outright, and dropping it is
    // correct — the flag is what stops one request being sent twice.  What was
    // missing was any account of it: a reader who pressed 复制 twice watched the
    // second press do nothing at all, which is indistinguishable from a button
    // that has stopped working.  This counter is the account.  The shell watches
    // it, shakes the control that was pressed and says so in the status bar;
    // nothing else reads it, and it never gates a request.
    refused: 0,
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
    notices: [] as Notice[],
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
    // its own pull happened before the partner had said anything.  `name` is the
    // event that bumped it, because the card reacts differently to a completion,
    // a wait that timed out and a code that expired.
    netpairEvent: null as { revision: number; name: string; data: Record<string, any> } | null,
    // This machine's own relay link, mirrored from `relay.state.changed`.  The
    // pairing card's line reads it live: the sidecar publishes every transition
    // as it happens, where the status snapshot only says what was true when it
    // was read — and for a relay that comes up seconds after the window opened,
    // that was the moment before it connected, so the line sat on 连接中 until
    // the reader pressed 刷新.  Empty = this session has not been told, and the
    // snapshot is all there is.
    relayState: "",
    // Settings the sidecar has applied and this window has not folded in, named.
    // Both hosts name what they changed — `settings.changed` for a save through
    // the web/phone API, `settings.live_applied` for the live apply behind every
    // save — and the settings page is a form hydrated once and then edited in
    // place, so a name is what lets it take a change made on another surface
    // without re-reading the whole form over whatever else is being typed in
    // it.  Kept until that page takes them (see `consumeSettingsFields`), which
    // is what makes a change made while the page was closed arrive when it is
    // opened.  `fields` is empty once taken; `revision` marks each arrival.
    settingsEvent: null as { revision: number; fields: string[] } | null,
    // The LAN discovery switches as last announced, mirrored from
    // `discovery.changed`.  The same two switches are drawn on the phone's
    // panel and the web dashboard, and flipping one there left this window
    // showing the state it read last — for as long as it stayed open, since
    // nothing else re-read them.
    discoveryState: null as { enabled: boolean; visible: boolean } | null,
    // When the sync pause runs out, mirrored from `sync.state.changed`.  The
    // countdown is drawn from the settings form, which is read when the window
    // starts and on the way into the settings page — so a pause armed from the
    // phone's panel, or ended early from one, went unseen here until this
    // window's own deadline had already passed, and a pause it had never heard
    // of (deadline 0) was never picked up at all.  Zero = no pause, which is
    // also what an event carrying no deadline says.
    syncPauseUntil: 0,
    // The folder send the sidecar is archiving right now, mirrored from
    // `transfers.packing`.  A folder or a multi-file pick is zipped before any
    // transfer exists, and that zip is as long as the selection is big — the
    // page could only say 正在打包 and wait, which on a few gigabytes is
    // indistinguishable from a hang.  The sidecar reports the totals before it
    // starts and how far it has got, and this is that report.  It is not part
    // of the transfers snapshot: there is no transfer yet to hold it.
    // `done` ends the run — on a failed pack too, so the line cannot outlive it.
    packing: null as {
      label: string;
      files_done: number;
      files_total: number;
      bytes_done: number;
      bytes_total: number;
      done: boolean;
    } | null,
    // A dialog the phone's panel asked this window to open. The request travels
    // as an event because the host has no window of its own; `revision` makes
    // two identical requests distinguishable.
    hostRequest: null as { kind: "qr" | "send_url"; revision: number } | null,
    // A device presenting a certificate that no longer matches its pin. The
    // connection was refused, so this is the only place the user learns why —
    // and the answer either re-pins the certificate or unpairs the device.
    // `can_trust` is false when the alert carried no certificate to pin, and
    // `changed` is true here: an alert whose certificate is the pinned one is
    // not a certificate change at all and never reaches this prompt (see
    // `ui.device_identity` below).
    certAlert: null as { device_id: string; name: string; can_trust: boolean; changed: true } | null,
    // Pairing requests that ended without pairing — the code expired, or the
    // peer turned it down.  Kept for a short while as a result the row can show,
    // because the alternative was the confirm card simply vanishing and a reader
    // unable to tell "expired" from "rejected" from "the click failed".  A new
    // request for the same device supersedes it; so does a completed pairing and
    // the window's own remove/unpair paths (which republish the row without it).
    pairingEnded: {} as Record<string, { status: string }>,
    // A peer's answer to the update this window asked it for, already worded.
    // Worded here rather than in the page because the notice stack says the same
    // thing in the same words, and two spellings of one answer is how a reader
    // ends up wondering whether they are looking at two answers.
    updatePeerEvent: null as { revision: number; device_id: string; message: string } | null,
    // What became of a log this window asked a device for: it arrived, the
    // device keeps its log to itself, or the file could not be filed here. All
    // three are the same row's answer, so all three land on the same ref.
    logEvent: null as { revision: number; device_id: string; message: string } | null,
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
  /** When each timed notice's countdown last started, so hovering can stop it.
   *
   * A notice used to be armed once, at creation, and nothing could hold it: the
   * pointer resting on the stack — a reader reading it — did not stop the six
   * seconds, and a pairing request expired under the hand that was reaching for
   * it.  The remaining time is kept here so pausing is one subtraction and
   * resuming is one `setTimeout`, rather than a second schedule to reconcile. */
  const noticeRemaining = new Map<number, number>();
  const noticeArmedAt = new Map<number, number>();
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
  /** What a dial that started and then failed says, from the transport's code.
   *
   * A dial is answered seconds after the click, from a thread nobody is
   * watching: without this the only trace of a failure was the row going from
   * 连接中 back to 离线 with nothing said.  The codes are the transport's own
   * (`connection.py`); a code from a newer build still says *something* true
   * rather than showing the wire word.
   */
  function connectFailedMessage(reason: string, name: string): string {
    switch (reason) {
      case "unreachable": return t("无法连接到 {name}：地址不可达，设备可能不在线", { name });
      case "timeout": return t("连接 {name} 超时，设备可能不在线", { name });
      case "tls": return t("与 {name} 建立加密连接失败", { name });
      case "closed": return t("{name} 断开了连接", { name });
      case "identity": return t("{name} 的身份验证没有通过，连接已拒绝", { name });
      case "crypto": return t("与 {name} 的加密设置不一致，连接已拒绝", { name });
      case "port": return t("无法连接 {name}：连接端口被拒绝", { name });
      default: return t("连接 {name} 失败", { name });
    }
  }
  /** The text of a notice; peer-supplied fields are inserted, never parsed. */
  function noticeMessage(name: string, data: Record<string, unknown>): string {
    const device = String(data.name || "");
    if (name === "pairing.request") {
      return t("{name} 请求配对 — 代码：{code}", { name: device, code: String(data.code || "") });
    }
    // A dial that started and failed.  The reason picks the sentence, so the
    // reader learns whether to wait, check the network, or re-pair.
    if (name === "device.connect_failed") {
      return connectFailedMessage(String(data.reason || ""), peerLabel(data));
    }
    // A pairing that is over without having paired.  The same event also fires
    // for a paused/in-flight confirm, which is not news, so only the two
    // terminal statuses get a sentence.
    if (name === "pairing.resolved") {
      const status = String(data.status || "");
      if (status === "expired") return t("{name} 的配对请求已过期，请重新配对", { name: peerLabel(data) });
      return t("{name} 的配对请求已取消", { name: peerLabel(data) });
    }
    // This machine entered a code and the partner never answered; the wait the
    // card was drawing is over, and saying so is what lets the reader try again
    // instead of watching a row that will never move.
    if (name === "netpair.waiting_failed") {
      return t("对方没有确认配对码，本次配对已超时，请重新生成或输入配对码");
    }
    // The code this machine generated outlived its window, so it can no longer
    // be typed by anybody: the card's copy of it goes as well.
    if (name === "netpair.code.expired") {
      return t("配对码已过期，请重新生成");
    }
    // Two devices hold different encryption passwords, so a relay channel they
    // both believe in cannot be opened.  The transport can see it and says
    // nothing on screen otherwise: every later frame is silently dropped.
    if (name === "netpair.decrypt_failed") {
      return t("与 {name} 的加密密码不一致，无法同步；请让两端使用同一个加密密码", {
        name: peerLabel(data),
      });
    }
    // A device presented the certificate this machine already pins but could
    // not prove it holds that key.  Non-destructive: nothing about the pin
    // needs answering, so this is a notice rather than the trust prompt.
    if (name === "ui.device_identity") {
      return t("无法验证 {name} 的身份，本次连接已拒绝；它的证书与已保存的一致。", {
        name: peerLabel(data),
      });
    }
    // An invitation to talk.  It is the same sentence the conversation's own
    // banner uses, because it is the same question: the reader has to compare
    // a fingerprint before answering either way, and the notice says who is
    // asking so that step can start without opening anything.
    if (name === "chat.invite") {
      return t("{name} 邀请你聊天", { name: peerLabel(data) });
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
    // A file arriving over the internet relay.  The route changes what the
    // reader is agreeing to — the same two devices on one network move it much
    // faster — so the notice says which route it is on its way by.  The
    // accept/cancel pair that answers it is on the transfers page, which is
    // where the notice points; a LAN arrival keeps the filename it always
    // showed, because there is nothing new for that reader to weigh.
    if (name === "transfer.request" && data.relay === true) {
      return t("{file} 经互联网传输，速度比同一网络慢，请在文件传输页确认接收。", {
        file: String(data.filename || ""),
      });
    }
    // The two ways a Connect click comes to nothing. The runtime publishes the
    // reason next to the bare `{accepted: false}` answer, so the click never
    // looks like a silent no-op; the wording is legacy's, advice included.
    if (name === "device.connection_rejected") {
      // The other device answered our connection with an explicit refusal, and it
      // tells us which refusal: one of these can be repaired here and the other
      // only at the other device, so a single sentence would send half the users
      // to the wrong screen.
      //
      // `reason` is the name the sidecar sends, not a number, so this switch and
      // the one in Python cannot drift apart on a constant that has to be written
      // twice.  An unrecognised name (a newer sidecar) falls through to the
      // removal wording, which is what every refusal meant before the reason
      // existed.
      const reason = String((data as Record<string, unknown>).reason || "");
      if (reason === "identity-unproven") {
        // This side has that device pinned, and the connection could not prove
        // it holds the key behind the certificate it presented.  Usually an older
        // build on the other machine; occasionally something copying its
        // certificate, which is exactly why the two are treated alike.
        return t(
          "{name} 无法证明自己的身份 — 请在「设备」页对它重新配对（该设备若为旧版本，升级后即可正常连接）",
          { name: peerLabel(data) },
        );
      }
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
      // Four different refusals, and the row has to tell them apart: the peer is not behind
      // (nothing is wrong on either side), the peer is a different platform (this installer could
      // never run there), it has no installer of its own to hand over, or the one it kept is not
      // newer than this device.  The last is its own sentence because the others all point at the
      // asking device ("go and check for updates yourself") and this one does not help there: the
      // sending device's cache holds the build *it* upgraded from, so it lags the running version
      // by one and a device can be ahead while having nothing newer to give.
      const reason = String(data.reason || "");
      if (reason === "not_newer") {
        return t("{name} 的版本不比本机旧，不需要这个更新", { name: peerLabel(data) });
      }
      if (reason === "other_platform") {
        return t("{name} 与本机不是同一个平台，装不了这个安装包", { name: peerLabel(data) });
      }
      if (reason === "cached_not_newer") {
        return t(
          "{name} 保留的安装包（{asset}）不比本机新，所以没有可发送的更新；它需要先自己升级一次",
          { name: peerLabel(data), asset: String(data.asset || "") || t("未知") },
        );
      }
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
    // The three answers to "send me your log".  A device that has sharing off
    // says so in a word rather than staying silent, because the alternative —
    // nothing arriving — is indistinguishable from a request that never left.
    if (name === "log.collected") {
      return t("已收到 {name} 的日志", { name: peerLabel(data) });
    }
    if (name === "log.unavailable") {
      return t("{name} 没有共享日志", { name: peerLabel(data) });
    }
    if (name === "log.failed") {
      return t("{name} 的日志没能保存到本机", { name: peerLabel(data) });
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
  /** The standing notice for each conversation's invitation, by session id.
   *
   * One invitation is one question, and it is answered in at least three
   * places: the notice's own two buttons, the banner inside the conversation,
   * and the far end giving up.  Every one of them has to take the card away,
   * or the window goes on offering to accept an invitation that is over.
   */
  const inviteNotices = new Map<string, number>();
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
  function pushNotice(name: string, data: Record<string, unknown>, actions: NoticeAction[] = []) {
    const id = ++noticeSequence;
    state.notices.push({
      id, title: name, message: noticeMessage(name, data),
      ...(actions.length ? { actions } : {}),
    });
    if (name === "pairing.request") {
      const device = String(data.device_id || "");
      if (device) pairingNotices.set(device, id);
    }
    if (name === "chat.invite") {
      // A second telling for the same conversation replaces the first rather
      // than sitting beside it: the engine announces a session again on every
      // status it passes through, and two cards offering the same two buttons
      // is a stack nobody can read.
      const session = String(data.session_id || "");
      if (session) {
        const previous = inviteNotices.get(session);
        if (previous !== undefined) dismissNotice(previous);
        inviteNotices.set(session, id);
      }
    }
    if (state.notices.length > 5) dismissNotice(state.notices[0].id);
    // The requests are questions, not reports: they stay until answered or
    // closed, because a question that times out in six seconds has one possible
    // answer — the one the reader never gave.  Everything else is a report about
    // something that has already happened, and expires on its own.  A notice
    // that carries buttons is a question by construction, whatever it is called.
    if (ACTION_NOTICES.has(name) || actions.length) return id;
    armNotice(id, 6000);
    return id;
  }
  /** Take down the invitation notice for a conversation, if one is standing.
   *
   * Called by every way an invitation can be answered — the notice's own
   * buttons, and the banner inside the conversation — because the card and the
   * banner are the same question and only one of them can be answered first.
   */
  function dismissInviteNotice(sessionId: string) {
    const id = inviteNotices.get(String(sessionId || ""));
    if (id !== undefined) dismissNotice(id);
  }
  /**
   * Answer an invitation from the notice that announced it.
   *
   * The two commands are the conversation banner's own; what is new is that
   * they can be reached from a notice, which is the only surface that exists
   * when the conversation is not open.  The card goes either way: an
   * invitation that could not be answered is over as far as this window is
   * concerned, and leaving the buttons there would invite a second try that
   * fails the same way.
   */
  async function answerChatInvite(sessionId: string, accepted: boolean) {
    const id = String(sessionId || "");
    if (!id) return;
    try {
      const result = await (accepted ? bridge.acceptChatInvite(id) : bridge.declineChatInvite(id));
      if (!result.ok) throw new Error(t("操作未完成"));
      dismissInviteNotice(id);
      toast("ui.chat", accepted ? t("已接受聊天邀请") : t("已拒绝聊天邀请"));
      void refresh();
    } catch (error) {
      dismissInviteNotice(id);
      if (!disposed) setError(error);
    }
  }
  /** Start (or restart) a notice's countdown, remembering how much is left. */
  function armNotice(id: number, ms: number) {
    clearTimeout(noticeTimers.get(id));
    noticeArmedAt.set(id, Date.now());
    noticeRemaining.set(id, ms);
    noticeTimers.set(id, setTimeout(() => dismissNotice(id), ms));
  }
  /** Stop the clock while the reader is on the notice: the pointer over it, or
   *  focus inside it.  A notice that leaves while it is being read is the one
   *  thing a fixed lifetime gets wrong. */
  function pauseNotice(id: number) {
    const timer = noticeTimers.get(id);
    const left = noticeRemaining.get(id);
    if (timer === undefined || left === undefined) return;
    clearTimeout(timer);
    noticeTimers.delete(id);
    const spent = Date.now() - (noticeArmedAt.get(id) ?? Date.now());
    noticeRemaining.set(id, Math.max(1200, left - spent));
  }
  /** Resume a paused countdown from where it stopped. */
  function resumeNotice(id: number) {
    const left = noticeRemaining.get(id);
    if (left === undefined || noticeTimers.has(id)) return;
    noticeArmedAt.set(id, Date.now());
    noticeTimers.set(id, setTimeout(() => dismissNotice(id), left));
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
    // The same answer twice in a row is one notice, not two: five copies in a
    // row (a context menu, the keyboard, a run of rows) used to leave five
    // identical cards covering the list they were about.
    const last = state.notices[state.notices.length - 1];
    if (last && last.title === key && last.message === message) {
      armNotice(last.id, ms);
      return last.id;
    }
    const id = ++noticeSequence;
    state.notices.push({ id, title: key, message });
    if (state.notices.length > 5) dismissNotice(state.notices[0].id);
    armNotice(id, ms);
    return id;
  }
  function dismissNotice(id: number) {
    clearTimeout(noticeTimers.get(id));
    noticeTimers.delete(id);
    noticeRemaining.delete(id);
    noticeArmedAt.delete(id);
    // A dismissed notice is nobody's to retract any more: the map is keyed by
    // device, so an entry left behind would let a later `pairing.resolved`
    // dismiss whatever notice happens to hold that id now.
    for (const [device, notice] of pairingNotices) {
      if (notice === id) pairingNotices.delete(device);
    }
    for (const [session, notice] of inviteNotices) {
      if (notice === id) inviteNotices.delete(session);
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

  /** How long a row keeps saying that its pairing request ended unpairing. */
  const PAIRING_ENDED_VISIBLE_MS = 60_000;
  const pairingEndedTimers = new Map<string, ReturnType<typeof setTimeout>>();
  /** Record that a pairing request ended without pairing, and let it fade.
   *
   * The row keeps the word 已过期/已取消 for a minute, which is long enough for
   * a reader who was looking at the card to see what became of it, and short
   * enough that it is not a permanent label on a device that may pair tomorrow.
   * A new request, a completed pairing, or a removal clears it sooner.
   */
  function markPairingEnded(deviceId: string, status: string) {
    const device = String(deviceId || "");
    if (!device) return;
    const pending = pairingEndedTimers.get(device);
    if (pending !== undefined) clearTimeout(pending);
    state.pairingEnded = { ...state.pairingEnded, [device]: { status } };
    pairingEndedTimers.set(device, setTimeout(() => {
      pairingEndedTimers.delete(device);
      if (disposed) return;
      const next = { ...state.pairingEnded };
      delete next[device];
      state.pairingEnded = next;
    }, PAIRING_ENDED_VISIBLE_MS));
  }
  function clearPairingEnded(deviceId: string) {
    const device = String(deviceId || "");
    const timer = pairingEndedTimers.get(device);
    if (timer !== undefined) clearTimeout(timer);
    pairingEndedTimers.delete(device);
    if (!(device in state.pairingEnded)) return;
    const next = { ...state.pairingEnded };
    delete next[device];
    state.pairingEnded = next;
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
  /** One diagnostics report, kept for as long as it is still a fair answer.
   *
   * See `diagnostics()` below: the overview's chip and the diagnostics dialog
   * read the same report, and the chip is how a reader opens the dialog, so the
   * second read was usually the first one over again.  A minute is short enough
   * that a reader who presses 重新检测 after changing something is not shown a
   * stale verdict, and the button forces a fresh one anyway. */
  const DIAGNOSTICS_TTL_MS = 60_000;
  let diagnosticsCache: { at: number; report: DiagnosticsReport } | null = null;
  const remoteFileTimers = new Map<string, ReturnType<typeof setTimeout>>();
  function clearRemoteFilePending(key: string) {
    const timer = remoteFileTimers.get(key);
    if (timer !== undefined) clearTimeout(timer);
    remoteFileTimers.delete(key);
    state.remoteFilePending = state.remoteFilePending.filter((open) => open !== key);
  }
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  /** A refresh put off until there is someone to owe it to.
   *
   * Every event ends in one, and every page's own periodic read already stands
   * down while the window is minimized or behind another one — the chat poll,
   * the transfers poll, the overview's two timers, the internet card's.  This
   * one did not: it is a status, a device list, a history page and the
   * favourites read together, and the sidecar publishes whether or not anyone
   * is looking, so a clipboard copied every few seconds was a whole snapshot
   * read every few seconds into a window nobody had opened.
   *
   * The debt is paid the moment the window is on screen again, which is also
   * the only moment the answer can be seen — and one read then, rather than
   * however many arrived while it was away.
   */
  let refreshOwed = false;
  /** Whether the window is on screen at all.  Focus is deliberately not
   * consulted, for the reason the pages' own timers give: a window kept open
   * beside the work is usually not the focused one, and that is exactly when it
   * is being watched. */
  function onScreen() {
    return typeof document === "undefined" || !document.hidden;
  }
  function onVisibility() {
    if (disposed || !onScreen() || !refreshOwed) return;
    refreshOwed = false;
    void refresh();
  }
  document.addEventListener("visibilitychange", onVisibility);
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

  /** Report a failure that did not come from the bridge.
   *
   * The error band renders `state.error.message`, `.code` and `.retryable`, so
   * `state.error` has to be a bridge-shaped object.  Several callers used to
   * assign a plain string (``state.error = reason?.message``), which rendered as
   * an empty red band with no text and no retry button — a TypeScript-visible
   * mistake the moment `reason` stops being `any`, and until then a silent one.
   * Callers outside the store go through this; the store's own paths already use
   * `setError`. */
  function fail(message: string, code = "CONNECTION_FAILED", retryable = true) {
    setError({ code, message, retryable });
  }

  /** Is another write already in flight?  Counted, because the answer is a click
   * the reader made.
   *
   * Every guard in this file used to read `if (disposed || state.pending)
   * return …`.  The second half of that test is a press the window dropped on
   * purpose — one request at a time — but it dropped it without a word, which is
   * the same thing the reader sees from a button that has stopped working.
   * Asking through here keeps the identical answer and records the drop, so the
   * shell has something to shake and something to say.
   */
  function inFlight(): boolean {
    if (disposed) return true;
    if (!state.pending) return false;
    state.refused++;
    return true;
  }

  /**
   * Run one bridge call and own the window's busy flag while it is in flight.
   *
   * The flag is what stops a second click doubling a request, and the `finally`
   * is what lets the window go again -- after a failure too, and even when the
   * failure arrives after the store was disposed. Written out a dozen times, the
   * `finally` is a line a reader has to check every time; here it is a line that
   * cannot be left out.
   */
  async function guardNull<T>(run: () => Promise<T>): Promise<T | null> {
    if (inFlight()) return null;
    state.pending = true;
    state.error = null;
    try {
      return await run();
    } catch (error) {
      if (!disposed) setError(error);
      return null;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  /** `guardNull` for the calls whose failure is a plain no. */
  async function guardFlag(run: () => Promise<boolean>): Promise<boolean> {
    if (inFlight()) return false;
    state.pending = true;
    state.error = null;
    try {
      return await run();
    } catch (error) {
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  async function refreshHistory() {
    if (disposed) return;
    const request = ++requestSequence;
    state.loading = true;
    try {
      const page = await bridge.history(state.query, state.offset, state.limit, state.kind, state.sort);
      if (disposed || request !== requestSequence) return;
      // A page past the end of the list.  The list shrinks under the window:
      // rows deleted, a retention age or limit pruning them, a lowered
      // `history_max_entries` taking effect the moment it is saved.  The read
      // for a page that no longer exists answers with no rows and a total that
      // is smaller than the offset, and the page read that as an empty history
      // — 暂无历史记录 over a pager still saying 31–30 / 30.
      //
      // So the window steps back to the last page there is and reads again —
      // the same step the favourites list makes — rather than leaving the
      // reader on a page with nothing on it.  One step: the offset this moves
      // to is the start of a page that has rows, which is strictly before the
      // one that had none.
      if (state.offset && state.offset >= page.total) {
        state.offset = Math.max(0, Math.floor((page.total - 1) / state.limit) * state.limit);
        state.selectedIds = [];
        state.kbdIndex = -1;
        void refreshHistory();
        return;
      }
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
      state.pollFailures = 0;
      state.staleSince = null;
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
      if (!disposed && request === statusSequence) {
        setError(error);
        state.pollFailures += 1;
        if (state.pollFailures >= 2 && !state.staleSince) state.staleSince = Date.now();
      }
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
            // Which of two very different things this is, as the publisher
            // decides it: the presented certificate is either a different one
            // from the pin (the reinstall/reset case — the destructive prompt
            // is the right answer) or exactly the pinned one, in which case
            // the device simply could not prove it holds that key.  The second
            // case used to reach this prompt too, so a healthy device could be
            // unpaired by a host that merely copied its public certificate;
            // it is now news, not a decision.  `changed` absent means a sidecar
            // older than the field, whose alert only ever meant the first case.
            if (data.changed === false) {
              pushNotice("ui.device_identity", data);
            } else {
              // A security decision, not a toast: the last alert wins, and it
              // stands until the user answers it or the wait runs out.
              state.certAlert = {
                device_id: String(data.device_id || ""),
                name: String(data.name || ""),
                can_trust: data.can_trust === true,
                changed: true,
              };
              clearTimeout(certPromptTimer);
              const asked = { ...data };
              certPromptTimer = setTimeout(() => {
                if (disposed || !state.certAlert) return;
                clearCertPrompt();
                pushNotice("device.security_alert", asked);
              }, CERT_PROMPT_TIMEOUT);
            }
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
          // The LAN switches, folded in place: the event *is* the state, so
          // there is nothing to re-read — and it is published from whichever
          // surface threw the switch, which is the whole point of mirroring it.
          if (event.name === "discovery.changed") {
            state.discoveryState = {
              enabled: data.enabled === true,
              visible: data.visible === true,
            };
          }
          // What a save from another surface changed, by name.  Kept for the
          // settings page rather than applied here: that page holds the only
          // copy of these fields, and it is the one that can put a new value in
          // its own box without disturbing the rest of what is on screen.  Left
          // to fall through as well — the snapshot carries the rest of what a
          // save moves.
          if (event.name === "settings.changed" || event.name === "settings.live_applied") {
            const named = Array.isArray(data.fields) ? data.fields.map(String) : [];
            const fields = [...(state.settingsEvent?.fields || [])];
            for (const field of named) if (!fields.includes(field)) fields.push(field);
            state.settingsEvent = { revision: (state.settingsEvent?.revision || 0) + 1, fields };
          }
          // The one settings value with a clock attached, so it is folded in
          // place: the page draws it as a countdown and nothing else has to be
          // re-read to put it on screen.
          if (event.name === "sync.state.changed") {
            state.syncPauseUntil = Number(data.until || 0);
          }
          // The zip behind a folder send, while it is being made.  Applied in
          // place and stopped at, like the update job above: these arrive a few
          // times a second and every one of them that fell through would pull a
          // whole snapshot — status, devices, transfers — to redraw a number the
          // event already carries.  `done` clears it, and a pack that fails
          // publishes one too, so the line cannot outlive the run that raised
          // it.  See U131/U132.
          if (event.name === "transfers.packing") {
            state.packing = data.done === true
              ? null
              : {
                label: String(data.label || ""),
                files_done: Number(data.files_done || 0),
                files_total: Number(data.files_total || 0),
                bytes_done: Number(data.bytes_done || 0),
                bytes_total: Number(data.bytes_total || 0),
                done: false,
              };
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
              name: event.name,
              data,
            };
            if (data.status === "paired") pushNotice(event.name, data);
          }
          if (
            event.name === "netpair.waiting_failed"
            || event.name === "netpair.code.expired"
            || event.name === "netpair.decrypt_failed"
          ) {
            // A wait that ran out, a generated code that expired, or two
            // devices that cannot open each other's channel: the card has to
            // re-read (the waiting row and the code both live in the status
            // snapshot), and the notice below is what says why in words.  The
            // payloads carry the peer or the code, never the snapshot itself.
            state.netpairEvent = {
              revision: (state.netpairEvent?.revision || 0) + 1,
              name: event.name,
              data,
            };
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
            const resolvedFor = String(data.device_id || "");
            dismissPairingNotice(resolvedFor);
            const status = String(data.status || "");
            if (status === "expired" || status === "cancelled") {
              // The request is over and the card is about to vanish.  Keep the
              // outcome on the row for a while and say it out loud; without
              // this the only thing a reader who missed the card saw was that
              // it had gone, with no way to tell expired from rejected.
              markPairingEnded(resolvedFor, status);
              pushNotice(event.name, data);
            } else if (status === "paired" || status === "confirmed_waiting"
              || status === "peer_confirmed") {
              // A request that moved again supersedes the ended label.
              clearPairingEnded(resolvedFor);
            }
          }
          if (event.name === "pairing.request") {
            // A fresh request for a device whose last one expired or was
            // rejected: the row must stop claiming the old outcome before it
            // starts asking for this one.
            clearPairingEnded(String(data.device_id || ""));
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
          // The answer to a log this window asked for.  All three are the row's
          // answer and all three are worth a notice: the log is filed silently
          // otherwise, and the two refusals would leave the reader watching a
          // folder that is not going to fill.
          if (
            event.name === "log.collected"
            || event.name === "log.unavailable"
            || event.name === "log.failed"
          ) {
            state.logEvent = {
              revision: (state.logEvent?.revision || 0) + 1,
              device_id: String(data.device_id || ""),
              message: noticeMessage(event.name, data),
            };
          }
          // An invitation to talk, arriving while the reader is somewhere
          // else.  The conversation's own banner is the place to answer it, and
          // that is exactly the problem: it is only on screen once the
          // conversation is open, so an invitation nobody opened expired
          // unanswered.  The notice carries the two answers, so the question can
          // be settled from wherever the reader is — and `App` asks the taskbar
          // for attention on it, because this window closes to the tray and the
          // invitation is the one chat event with a deadline.
          //
          // Silent when that conversation is already in front of them: the
          // banner there is the same question with the same two buttons.
          if (event.name === "chat.invite") {
            const sessionId = String(data.session_id || "");
            if (sessionId && sessionId !== state.openChatSession) {
              pushNotice(event.name, data, [
                { label: t("接受"), primary: true, run: () => answerChatInvite(sessionId, true) },
                { label: t("拒绝"), run: () => answerChatInvite(sessionId, false) },
              ]);
            }
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
          if (!alreadyReportedRemoval && event.name && ["runtime.error", "pairing.request", "transfer.request", "chat.connect_timeout", "url.received", "device.connected", "device.disconnected", "sync.redacted", "device.connection_rejected", "device.connection_unreachable", "device.connect_failed", "update.peer_unavailable", "update.peer_notice", "log.collected", "log.failed", "log.unavailable", "netpair.waiting_failed", "netpair.code.expired", "netpair.decrypt_failed"].includes(event.name)) {
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
          if (!onScreen()) { refreshOwed = true; return; }
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
      // Reset: `started` is what makes start() a no-op, so leaving it true
      // after a failed subscribe means neither `start()` nor `reconnect()`
      // ever subscribes again — and every event (device up/down, pairing,
      // transfers, notices) stays silent for the rest of the window's life.
      // This is the one failure here that cannot recover on its own.
      started = false;
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
    // A subscribe that failed left no listener, so a plain refresh here would
    // repaint the snapshot and never deliver another event.  Re-run start(),
    // which is a no-op only once it has actually subscribed.
    if (unlisten === undefined) {
      await start();
      return;
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
    if (inFlight()) return false;
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
          // A key, not a sentence: `NoticeStack.title` is a table keyed by name
          // and its default branch returns the name unchanged, so a translated
          // string here was already resolved and the table could not re-word it
          // — the English interface showed 数据修复 for this one notice.
          title: "ui.data_recovery",
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
    if (inFlight()) return false;
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
      if (!disposed && (error as { code?: string })?.code === "INVALID_SELECTION") {
        // The sidecar saw the window change between the tick and the write.
        // Refresh the list and forget the stale ticks; this is not a failure
        // the reader has to dismiss from the error band.
        state.selectedIds = [];
        await refresh();
        return false;
      }
      if (!disposed) setError(error);
      return false;
    } finally {
      if (!disposed) state.pending = false;
    }
  }

  function historyBusy() {
    return disposed || state.pending || state.loading || state.refreshing || state.status?.health !== "ready";
  }

  function selectedBatch(ids: string[], allowUnselected = false) {
    const unique = [...new Set(ids)];
    const current = new Set(state.history.map((item) => item.id));
    const kept = unique.filter((id) =>
      id.trim() && (allowUnselected || state.selectedIds.includes(id)) && current.has(id));
    // A live sync can remove a row while it is ticked.  Carry on with the rows
    // that still exist instead of rejecting the whole action with a selection
    // error the reader cannot act on; if none are left, leave the list ready for
    // a fresh selection rather than putting an error band over it.
    if (!kept.length) {
      state.selectedIds = state.selectedIds.filter((id) => current.has(id));
      return null;
    }
    if (kept.length !== state.selectedIds.length) state.selectedIds = kept;
    return kept.slice(0, 100);
  }

  function discoveryAvailable() {
    return state.status?.health === "ready" &&
      !!state.status.capabilities?.includes("discovery.status");
  }

  async function discoveryToggle(kind: "enabled" | "visible", enabled: boolean) {
    // Both switches return the resulting discovery state so the UI reflects
    // what the runtime reports rather than what was requested.
    if (inFlight() || !discoveryAvailable()) return null;
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
    dismissNotice, toast, setError, fail,
    /** Hold a notice's countdown while it is being read, and let it go after. */
    pauseNotice, resumeNotice,
    /** Which conversation the chat page has on screen, or "" for none. */
    setOpenChatSession(sessionId: string) {
      state.openChatSession = String(sessionId || "");
    },
    /** The conversation page answered an invitation; take the notice down.
     *
     * Both surfaces offer the same two buttons, and whichever one the reader
     * reaches for, the other has to stop asking.
     */
    retractInviteNotice(sessionId: string) {
      dismissInviteNotice(sessionId);
    },
    /** The outcome a pairing request ended with, while its row still shows it.
     *
     * "" for a device with no ended request on screen; "expired" and
     * "cancelled" otherwise.  The label is dropped when the row is asked for
     * again, when a pairing completes, or after a minute (see
     * `PAIRING_ENDED_VISIBLE_MS`), so it cannot outlive the event that caused
     * it by more than a reader's glance. */
    pairingEndedStatus(deviceId: string): string {
      return state.pairingEnded[String(deviceId || "")]?.status || "";
    },
    /** Take the settings another surface changed, by name, for the settings
     * page to fold in.  Empty when there is nothing waiting, which is also what
     * a second call after one arrival returns: the revision is deliberately left
     * standing, since it marks the arrival the page is already answering and
     * bumping it here would wake the watcher a second time. */
    consumeSettingsFields(): string[] {
      const fields = state.settingsEvent?.fields || [];
      if (fields.length) state.settingsEvent = { ...state.settingsEvent!, fields: [] };
      return fields;
    },
    refresh() { state.error = null; return refresh(); },
    /** The error band's close, for an error that has nothing to retry.
     *
     * Every other path that clears `state.error` does it as a side effect of starting something --
     * a command's own `guardFlag`, a reconnect, a settings save.  A reader who hit a request the
     * sidecar refused has none of those to reach for, so the band could not be dismissed at all,
     * which is how it was reported: a validation error with no way to close it.
     */
    dismissError() { state.error = null; },
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
    async batchFavorite(entryIds: string[], group = "", allowUnselected = false) {
      // Returns how many entries were copied into the favorites store (0 on
      // failure) so the caller can report the count, like the legacy toast.
      if (historyBusy()) return 0;
      const ids = selectedBatch(entryIds, allowUnselected);
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
    async copy(item: HistoryItem, announce = true) {
      state.copiedId = null;
      if (!(await action(() => bridge.copyHistory(item.id)))) return;
      state.copiedId = item.id;
      // The tick replacing the row's copy icon is the whole answer when the row
      // is on screen, and the row's own button is exactly that case — a notice
      // there is the same news twice, and five copies in a row was five cards
      // over the list.  The notice stays for the paths whose row the reader
      // cannot see: the context menu, the keyboard, another page.
      if (announce) toast("ui.history", t("已复制"));
    },
    /** Copy the words out of a clip that also carries formatting.
     *
     * A clip with markup pasted into a terminal or an editor arrives as its
     * markup, and the fix is the clipboard's own: put the text form on it
     * instead.  This is the menu entry for what `Ctrl+Shift+V` does — same
     * call, same flag, so the two cannot disagree about what "plain" means.
     */
    async copyPlain(item: HistoryItem) {
      state.copiedId = null;
      if (!(await action(() => bridge.copyHistory(item.id, true)))) return;
      state.copiedId = item.id;
      toast("ui.history", t("已复制为纯文本"));
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
     *
     * What travels is the id from the row's *offer* (`offer_entry`), not the id
     * of the row on screen.  History ids are numbered per machine, so the two
     * are different numbers for the same clip: asked with the local one, the
     * peer resolves it against its own rows and answers with whatever its row
     * of that number happens to be — an older file, or a refusal.  That was the
     * 下载 button's whole failure mode.  The two ids also key the pending mark
     * apart from each other, which is why `key` is built from the id that goes
     * on the wire: the refusal that clears the mark carries that same id back.
     *
     * A row whose offer this build cannot read has no id to ask with, and the
     * click is refused rather than answered with a guess.
     */
    async downloadRemoteFile(item: HistoryItem) {
      const deviceId = item.source_device || "";
      const entryId = item.offer_entry || "";
      if (!deviceId || !entryId) return false;
      const key = `${deviceId}:${entryId}`;
      state.remoteFilePending = [key, ...state.remoteFilePending];
      try {
        await bridge.requestEntryFiles(entryId, deviceId);
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
      if (inFlight()) return false;
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
      return guardNull(() => bridge.openHistoryLink(item.id));
    },
    forget: (device: Device) => action(() => bridge.forgetDevice(device.id)),
    restore: (device: Device) => action(() => bridge.restoreDevice(device.id)),
    purge: (device: Device) => action(() => bridge.purgeDevice(device.id)),
    async probe(device: Device) {
      return guardNull(() => bridge.testDevice(device.id));
    },
    async certificates() {
      if (inFlight()) return null;
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
      if (inFlight()) return false;
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
      if (inFlight()) return null;
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
      if (inFlight() || !discoveryAvailable()) return null;
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
    async diagnostics(force = false) {
      // Live snapshot built by the same use case the legacy panel renders, so
      // the two surfaces cannot disagree about the state of this machine.
      //
      // Cached for a minute, because a report is not one read: every check runs
      // again — the port, discovery, the relay, and on macOS a subprocess that
      // asks the firewall — and two surfaces read it.  The overview's chip asks
      // on a clock, and the chip is exactly what a reader presses to open the
      // dialog: without this, pressing it ran the whole suite a second time for
      // an answer that had come back seconds earlier.  重新检测 passes `true`,
      // because that button's whole point is a fresh one.
      if (!force && diagnosticsCache && Date.now() - diagnosticsCache.at < DIAGNOSTICS_TTL_MS) {
        return diagnosticsCache.report;
      }
      const report = await guardNull(() => bridge.diagnosticsReport());
      if (report) diagnosticsCache = { at: Date.now(), report };
      return report;
    },
    async repairDiagnostics(kind: DiagnosticAction) {
      // The caller re-runs diagnostics afterwards: the repair only opens the
      // OS settings or applies the rule, it does not prove the check passes.
      return guardNull(() => bridge.diagnosticsRequest(kind));
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
      if (inFlight()) return null;
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
      return guardNull(() => bridge.updateInstall());
    },
    async installReadyUpdate() {
      // The install the card can run on an archive that is already on disk.
      // Unlike `installUpdate` a reply here can be good news: what this build
      // cannot install itself it hands to the reader, and the caller has to say
      // which of the two happened rather than leave a click with nothing after
      // it.
      return guardNull(() => bridge.updateInstallReady());
    },
    async downloadUpdate() {
      // Returns immediately; progress arrives as `update.state` events.
      return guardNull(() => bridge.updateDownload());
    },
    async openUpdateFolder() {
      return guardNull(() => bridge.updateOpenFolder());
    },
    setDiscoveryEnabled: (enabled: boolean) => discoveryToggle("enabled", enabled),
    setDiscoveryVisible: (enabled: boolean) => discoveryToggle("visible", enabled),
    dispose() {
      for (const id of noticeTimers.keys()) dismissNotice(id);
      favorites.dispose();
      delivery.dispose();
      disposed = true;
      refreshOwed = false;
      document.removeEventListener("visibilitychange", onVisibility);
      ++requestSequence;
      ++statusSequence;
      clearTimeout(refreshTimer);
      clearTimeout(certPromptTimer);
      for (const timer of remoteFileTimers.values()) clearTimeout(timer);
      remoteFileTimers.clear();
      for (const timer of pairingEndedTimers.values()) clearTimeout(timer);
      pairingEndedTimers.clear();
      unlisten?.();
    },
  };
}
