import { reactive } from "vue";
import { bridge } from "../api/bridge";
import { t } from "../i18n";
import type { ChatEntry } from "../api/types";

/** The four states a relayed send can be in, in the ledger's own words. */
export type DeliveryStatus = "sent" | "delivered" | "failed" | "queued";
export const DELIVERY_STATUSES: readonly DeliveryStatus[] = ["sent", "delivered", "failed", "queued"];

/** How many relayed ids the bubble map remembers (legacy kept 250).
 *
 * The map is read by msg_id when a bubble renders, so it only ever needs to
 * cover the messages on screen; a cap keeps a long session from growing it
 * without bound.  Oldest id first out.
 */
export const DELIVERY_MESSAGE_CAP = 250;

/** A status the ledger actually uses, or null for anything else.
 *
 * Every entry point runs through this: the event stream and the RPC both carry
 * strings the shell does not know (a newer host's fifth state, an empty field
 * on a row the backend could not key), and an unrecognized one must leave the
 * row's state alone rather than stamp a bubble with a word no label covers.
 */
export function asDeliveryStatus(value: unknown): DeliveryStatus | null {
  return typeof value === "string" && (DELIVERY_STATUSES as readonly string[]).includes(value)
    ? (value as DeliveryStatus) : null;
}

/** The label a status is shown under, in the interface's own words. */
export function deliveryLabel(status: DeliveryStatus): string {
  if (status === "delivered") return t("已送达");
  if (status === "failed") return t("未送达");
  if (status === "queued") return t("待补发");
  return t("发送中");
}

/** Which glyph a status is shown with, matching the send list's own order. */
export function deliveryIcon(status: DeliveryStatus): "check" | "x" | "clock" {
  if (status === "delivered") return "check";
  if (status === "failed") return "x";
  return "clock";
}

/** The receipt to stamp on a chat bubble, or null when there is none to show.
 *
 * Ported from the legacy chat panel, rules and all, because each one keeps the
 * pill from asserting something untrue:
 *
 * * Outgoing **text** only.  A file bubble has its own transfer state, and an
 *   incoming message owes no receipt.
 * * Nothing when the entry itself is `failed`: that message never left, so the
 *   bubble keeps its failed label and its resend button — a "sending" pill over
 *   a message the transport refused would be the one lie worth avoiding here.
 * * Nothing without a `msg_id`.  Older hosts do not send one, and a receipt
 *   matched to no id would land on the wrong bubble (or on none).
 * * Nothing for an id the ledger never mentioned, which is every message sent
 *   over the LAN alone.
 */
export function chatReceipt(
  entry: Pick<ChatEntry, "outgoing" | "kind" | "status" | "msg_id"> | null | undefined,
  messages: Record<string, DeliveryStatus>,
): DeliveryStatus | null {
  if (!entry || !entry.outgoing || entry.kind !== "text") return null;
  if (entry.status === "failed") return null;
  const id = entry.msg_id;
  if (typeof id !== "string" || !id) return null;
  return messages[id] || null;
}

/** One peer's delivery state: what the send list and the card read.
 *
 * `pending` is the queued total; `lastStatus` is the newest transition;
 * `msgStatus` is the id → status map the fold uses to tell a re-broadcast
 * `queued` from a fresh one, so the count cannot drift.
 */
export interface PeerDelivery {
  pending: number;
  lastStatus: DeliveryStatus | null;
  msgStatus: Record<string, DeliveryStatus>;
  /** Whether a fetch has ever answered for this peer (a row renders only then). */
  loaded: boolean;
  /** The fetch was refused (no relay on this host, an older backend). */
  loadFailed: boolean;
}

export function createDeliveryStore(enabled: () => boolean) {
  const state = reactive({
    /** msg_id → latest status, for the chat bubbles. */
    messages: {} as Record<string, DeliveryStatus>,
    /** peer_id → that peer's ledger summary. */
    peers: {} as Record<string, PeerDelivery>,
  });
  // Insertion order of `messages`, so the cap evicts the oldest id rather than
  // whichever the engine happens to enumerate first.
  const order: string[] = [];
  // Bumped by every folded event.  A fetch that started before the bump
  // abandons its write-back: a live transition is fresher than the snapshot it
  // was racing, and letting the snapshot win would walk the count backwards.
  const ticks: Record<string, number> = {};
  const inFlight: Record<string, boolean> = {};
  let disposed = false;
  const available = () => !disposed && enabled();

  function emptyPeer(loaded: boolean): PeerDelivery {
    return { pending: 0, lastStatus: null, msgStatus: {}, loaded, loadFailed: false };
  }

  /** Stamp one id in the bubble map, keeping it to the newest 250.
   *
   * Both entry points go through here — a live event and the fetch that fills
   * in what happened before this window opened — so the cap can never be
   * enforced on one path and forgotten on the other.
   */
  function stamp(msgId: string, status: DeliveryStatus) {
    if (!msgId) return;
    if (state.messages[msgId] === undefined) {
      order.push(msgId);
      while (order.length > DELIVERY_MESSAGE_CAP) {
        const oldest = order.shift();
        if (oldest !== undefined) delete state.messages[oldest];
      }
    }
    state.messages[msgId] = status;
  }

  /** Fold one `relay.delivery.changed` event into the mirror.
   *
   * The ledger tracks a clipboard send and a relayed chat message in exactly
   * the same rows, so the fold needs no branch on `kind`: the id map is keyed
   * by msg_id and a chat bubble looks up its own, while the per-peer summary is
   * what the send list shows either way.  (`kind` and `session_id` stayed on
   * the row for the surfaces that name what was sent; nothing in this mirror
   * has to read them to route a receipt.)
   *
   * Returns what it did, so a caller — and a test — can tell a folded event
   * from one this host does not understand.
   */
  function apply(data: Record<string, unknown> | null | undefined) {
    if (!available() || !data || typeof data !== "object") return null;
    const peerId = data.peer_id === undefined || data.peer_id === null ? "" : String(data.peer_id);
    const status = asDeliveryStatus(data.status);
    if (!peerId || !status) return null;
    const msgId = data.msg_id === undefined || data.msg_id === null ? "" : String(data.msg_id);
    stamp(msgId, status);
    const entry = state.peers[peerId] || emptyPeer(true);
    const wasQueued = !!msgId && entry.msgStatus[msgId] === "queued";
    if (msgId) entry.msgStatus[msgId] = status;
    // The queue count moves only on a real transition in or out of `queued`;
    // the ledger re-broadcasts a status on every retry, and counting each one
    // would inflate the badge.
    if (status === "queued") {
      if (!wasQueued) entry.pending += 1;
    } else if (wasQueued) {
      entry.pending = Math.max(0, entry.pending - 1);
    }
    entry.lastStatus = status;
    state.peers[peerId] = entry;
    ticks[peerId] = (ticks[peerId] || 0) + 1;
    return { peerId, msgId, status };
  }

  /** Seed one peer from the ledger the backend already holds.
   *
   * The event stream only carries transitions that happened while this window
   * was open, so without this a freshly opened window shows an empty send list
   * even though the host has queued rows — and the chat panel's receipts for
   * anything sent before the window opened.  A peer is fetched at most once at
   * a time; a second request while one is in flight is dropped rather than
   * queued, because the answer would be the same snapshot.
   */
  async function load(peerId: string) {
    if (!available() || !peerId) return false;
    if (inFlight[peerId]) return false;
    inFlight[peerId] = true;
    const entry = state.peers[peerId] || emptyPeer(false);
    entry.loaded = false;
    entry.loadFailed = false;
    const tick = ticks[peerId] || 0;
    try {
      const result = await bridge.relayDeliveryStatus(peerId);
      if (!available() || (ticks[peerId] || 0) !== tick) return false;
      const rows = Array.isArray(result.items) ? result.items : [];
      let queued = 0;
      let last: DeliveryStatus | null = null;
      const msgStatus: Record<string, DeliveryStatus> = {};
      rows.forEach((row, index) => {
        const id = (row as Record<string, unknown>)?.msg_id;
        // A row the shell cannot key is skipped whole: counted, it would put a
        // number on the badge that no later transition could ever retire.
        if (id === undefined || id === null) return;
        const status = asDeliveryStatus((row as Record<string, unknown>).status) || "sent";
        // Rows arrive newest first, so the first keyable one is the latest
        // result — the one the card names.
        if (index === 0) last = status;
        msgStatus[String(id)] = status;
        // The fetched rows feed the bubbles too: a receipt from before this
        // window opened is still the truth about that message, and the host
        // only remembers the newest rows, so this cannot outlive the ledger.
        stamp(String(id), status);
        if (status === "queued") queued += 1;
      });
      entry.pending = typeof result.pending === "number" ? result.pending : queued;
      entry.lastStatus = last;
      entry.msgStatus = msgStatus;
      entry.loaded = true;
      entry.loadFailed = false;
      state.peers[peerId] = entry;
      return true;
    } catch {
      // 404 / no relay / an older backend: settle so the row can hide itself,
      // and let a later event still surface live data.  The refusal is kept as
      // that peer's `loadFailed`, not as a store-wide error: a host with no
      // relay refuses this call for every peer, and legacy hid the line rather
      // than nag about a capability the reader may never have paired for.
      if (available() && (ticks[peerId] || 0) === tick) {
        entry.loaded = true;
        entry.loadFailed = true;
        state.peers[peerId] = entry;
      }
      return false;
    } finally {
      delete inFlight[peerId];
    }
  }

  return {
    state,
    apply,
    load,
    /** A peer's state, or null when nothing has been learned about it yet. */
    peer(peerId: string): PeerDelivery | null {
      return peerId ? state.peers[peerId] || null : null;
    },
    /** How many rows are queued across every peer that has reported. */
    get total(): number {
      return Object.values(state.peers).reduce((sum, peer) => sum + (peer.loaded ? peer.pending : 0), 0);
    },
    /** Whether a peer's delivery line has anything to say.
     *
     * "Nothing queued and nothing sent yet" and "this host cannot tell us" both
     * leave the row off, so the card never shows a zero it cannot stand behind.
     */
    shown(peerId: string): boolean {
      const peer = state.peers[peerId];
      if (!peer || !peer.loaded || peer.loadFailed) return false;
      return peer.pending > 0 || !!peer.lastStatus;
    },
    lastStatus(peerId: string): DeliveryStatus | null {
      return state.peers[peerId]?.lastStatus || null;
    },
    pending(peerId: string): number {
      return state.peers[peerId]?.pending || 0;
    },
    /** Drop a peer's state (unpair, or a peer the host no longer knows). */
    forget(peerId: string) {
      if (peerId) delete state.peers[peerId];
    },
    reset() {
      state.messages = {};
      state.peers = {};
      order.length = 0;
      for (const key of Object.keys(ticks)) delete ticks[key];
      for (const key of Object.keys(inFlight)) delete inFlight[key];
    },
    dispose() { disposed = true; },
  };
}
