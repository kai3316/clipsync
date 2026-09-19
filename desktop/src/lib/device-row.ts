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
