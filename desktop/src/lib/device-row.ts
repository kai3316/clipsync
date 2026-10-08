/* How a device row reads: what it is called, what state it is in, whether a
 * conversation with it would connect, and which of those decides where it sits
 * in the list.
 *
 * These are the rules two surfaces already had to agree on.  The devices page
 * draws a row for a device and the chat page offers the same device a
 * conversation, and each carried its own copy of "can this be reached" — the
 * chat page's copy being the more complete of the two, which is how a device
 * paired on this network ended up with a chat button on one page and none on
 * the other.  The tray is the third surface, and its `classify` was written to
 * mirror the page's predicates by hand; the ordering below is the one that
 * function already sorts by, spelled once here.
 *
 * Pure, over the row DTO and nothing else, so all of it can be read and tested
 * without a window — the same reason `lib/pairing-code.ts` is a module.
 */

import type { Device } from "../api/types";

/** The pairing statuses a handshake is still in flight in.
 *
 * The three the sidecar records while a request is live; `paired`, `cancelled`
 * and `expired` are all endings, and `""` is the absence of a handshake rather
 * than one of its states. */
export const PAIRING_LIVE_STATUSES = ["pending", "peer_confirmed", "confirmed_waiting"];

/** Whether this device is mid-handshake — a code on screen, an answer owed. */
export function pairingInFlight(device: Device): boolean {
  return PAIRING_LIVE_STATUSES.includes(String(device.pairing_status || ""));
}

/** Whether a conversation with this device would connect right now.
 *
 * `connection_state` describes one route, and which route depends on the row: a
 * relay-only row carries the relay's own reading of the peer, a row with a
 * local link carries the local one.  A device can hold both pairings, so a row
 * whose local link is down may still be up on the relay — and calling that one
 * offline greys out a conversation that would connect. */
export function chatReachable(device: Device): boolean {
  if (device.relay) return device.connection_state !== "offline";
  return Boolean(device.relay_paired && device.relay_online) || device.connection_state !== "offline";
}

/** Whether a file could be sent to this device right now.
 *
 * The direct link alone, unlike `chatReachable`: a file travels as 256 KiB
 * chunks with pause/resume, which the public broker cannot carry and cannot
 * resume, so an internet-paired device is not a target here however online the
 * relay says it is.  Pairing is deliberately not part of it — the transfer page
 * sends to whatever is on this network, exactly as the chat page does.
 *
 * A link that *exists*, not one that could be made.  `chatReachable` accepts a
 * device that is merely discovered because an invitation is the thing that
 * dials it; a file has no such handshake — the sidecar sends to a peer it is
 * already connected to and refuses one it is not (``lan.py::_send_files``), so
 * `discovered` and `connecting` rows would be targets that answer with
 * NOT_CONNECTED the moment they are picked.
 *
 * `relay` marks a row that only exists on the relay, so its
 * `connection_state` describes that route and says nothing about a local link
 * that is not there. */
export function fileReachable(device: Device): boolean {
  return !device.relay && device.connection_state === "online";
}

/** What the device list calls this device's state, in one word.
 *
 * The row used to carry a pairing chip and a route chip side by side, so a
 * device that was paired *and* connected read as two chips that partly agreed
 * -- paired, and on the local link -- and the reader had to combine them.
 * There are six states a device can be in, and each is one word:
 *
 *   syncing     paired and linked — content moves in both directions
 *   connected   linked, not paired — chat and files work, nothing syncs
 *   paired      paired, not linked — known, and nothing is flowing
 *   connecting  a dial is in flight — the link is being made
 *   discovered  seen on this network, neither paired nor linked
 *   offline     nothing heard from it
 *
 * **Linked is its own predicate, and not `chatReachable`.**  It reached for that
 * function, which is the "would a conversation connect" rule and deliberately generous: it accepts
 * a device that is only discovered, because an invitation is the thing that dials it.  Reusing it
 * here gave 已连接 — a word promising content is moving — to a device nothing had connected to.
 * Reported as "every device on my network reads as connected", which is this line's fault.
 *
 * The two are still related, and the relation is one-directional: every linked device is
 * reachable, and a reachable one need not be linked.  `chatReachable` remains the chat button's
 * rule, because a device can be worth dialing without being connected to.
 *
 * The extra two words are not invented here.  `connectionLabel` already answers the sidecar's four
 * `connection_state` values with 已发现 / 连接中 / 在线 / 离线, and treats *seen* and *offline* as
 * different things — the state chip was the only surface that conflated them.
 *
 * A handshake in flight is not one of the six: it is something the user has to
 * answer, not a state the device settled into, so callers keep showing
 * `pairingInFlight` separately. */
export function localLinkEstablished(device: Device): boolean {
  if (device.relay) return device.connection_state !== "offline";
  return (
    Boolean(device.relay_paired && device.relay_online) || device.connection_state === "online"
  );
}

export function deviceStatus(
  device: Device,
): "syncing" | "connected" | "paired" | "connecting" | "discovered" | "offline" {
  const paired = Boolean(device.paired || device.relay_paired);
  const linked = localLinkEstablished(device);
  // A dial in flight is asked about *before* the settled states, because it is the one state that is
  // not settled: a paired device being retried has no link, so reading it as 已配对 reported a
  // machine that is actively being worked on as one nothing is happening to.  The chip's own title
  // said 重连中 3/10 while its text said 已配对 -- two claims about one row that cannot both be
  // true, and the e2e case for the retry count is what caught it.
  //
  // `reconnecting` is the sidecar's explicit flag; `connection_state === "connecting"` is the same
  // fact without it, which is what an older peer sends.
  if (!linked && (device.reconnecting || device.connection_state === "connecting")) {
    return "connecting";
  }
  if (paired) return linked ? "syncing" : "paired";
  if (linked) return "connected";
  // Reachable but not linked and not being dialled: a device only seen.
  return device.connection_state === "discovered" ? "discovered" : "offline";
}

/** What this list calls the device.
 *
 * The name the user gave it — the note on a local peer, the alias on one paired
 * by code — outranks the one the peer published about itself, which is the rule
 * the sidecar already applies to the alias on a relay row, and the rule the
 * rename dialog states in words: the name is kept on this device and the peer
 * still sees its own.  A row that showed the peer's name while 重命名 wrote
 * somewhere else was a rename the reader could not see; the name the peer goes
 * by is still on the row, under this one.
 *
 * A note of nothing but spaces is not a name, so it falls through. */
export function deviceLabel(device: Device): string {
  return String(device.note || "").trim() || device.name;
}

/** Where a row sorts, most present first.
 *
 * The tray's own ranking (`tray.rs::DeviceState::rank`), which is itself the
 * legacy tray's grouping: what is up now, then the handshake on its way, then
 * the devices this machine knows but cannot reach, then the ones merely seen.
 * The order is the ranking's, not each row's — a device being paired does not
 * outrank one in the next room, because a live link is what a reader scans the
 * list for.
 *
 * Returns the same numbers the tray's ranks are, so the two native lists can be
 * read side by side. */
export function deviceRank(device: Device): number {
  const relayPaired = device.relay_paired === true;
  const online = device.connection_state === "online" || (relayPaired && device.relay_online === true);
  if (device.paired || relayPaired) return online ? 0 : 2;
  if (pairingInFlight(device)) return 1;
  return online ? 0 : 3;
}

/** A platform name as a reader knows it, from the os a peer advertises.
 *
 * `platform.system()` values — "Darwin" on macOS, "Windows", "Linux" — spelled
 * the way the sidecar's own `friendly_platform_name` spells them, because these
 * are brand names and a second spelling of one would describe a device
 * differently here than the 发送更新 entry beside it does.  Unknown values pass
 * through as the peer advertised them rather than being called unknown.
 *
 * The order is the sidecar's too, and it matters: "darwin" contains "win". */
export function platformLabel(value: unknown): string {
  const advertised = String(value ?? "").trim();
  const system = advertised.toLowerCase();
  if (system.includes("mac") || system.includes("darwin")) return "macOS";
  if (system.includes("win")) return "Windows";
  if (system.includes("linux")) return "Linux";
  if (system.includes("android")) return "Android";
  if (system.includes("ios")) return "iOS";
  return advertised;
}
