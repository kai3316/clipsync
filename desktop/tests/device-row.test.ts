import { describe, expect, it } from "vitest";
import { chatReachable, deviceRank, deviceStatus, fileReachable, localLinkEstablished, renameWritesAlias } from "../src/lib/device-row";
import type { Device } from "../src/api/types";

/** A row with only the fields these three predicates read. */
function row(overrides: Partial<Device> = {}): Device {
  return {
    id: "d",
    name: "Device",
    paired: false,
    connection_state: "offline",
    ...overrides,
  } as Device;
}

// The four values the sidecar's `connection_state` takes (lan.py:4138), which is
// what these tests are about:
//
//   online      a link exists
//   connecting  a dial is in flight
//   discovered  seen on this network, not paired and not connected
//   offline     nothing heard from it
//
// Reported as "every device on my network reads as connected".  The cause was
// `deviceStatus` reusing `chatReachable`, which accepts anything but `offline`
// because an invitation is the thing that dials a device -- so `discovered` and
// `connecting` both earned a word that promises content is moving.
describe("a device's state word", () => {
  it("does not call a discovered device connected", () => {
    const discovered = row({ paired: false, connection_state: "discovered" });
    expect(deviceStatus(discovered)).toBe("discovered");
    // And it is still offered a conversation, which is the difference that made
    // reusing `chatReachable` look right: a device can be worth dialing without
    // being connected to.
    expect(chatReachable(discovered)).toBe(true);
  });

  it("does not call a dialing device offline", () => {
    // `_connecting` is written by `_connect_and_wait`, which needs an address and
    // not a pairing -- so an unpaired row reading `connecting` is real, and the
    // first version of this rule answered 离线 for it.
    const dialing = row({ paired: false, connection_state: "connecting" });
    expect(deviceStatus(dialing)).toBe("connecting");
    expect(chatReachable(dialing)).toBe(true);
  });

  it("keeps the two words the established states already had", () => {
    expect(deviceStatus(row({ paired: false, connection_state: "online" }))).toBe("connected");
    expect(deviceStatus(row({ paired: true, connection_state: "online" }))).toBe("syncing");
  });

  it("says a retry is in flight rather than calling the device settled", () => {
    // The word is about the pairing and the link, and a retry in flight makes it neither: nothing is
    // moving yet, so it is not syncing -- but it is not 已配对 either, which says nothing is
    // happening to a device that is being worked on right now.
    //
    // This case asserted `paired` for the `connecting` row, which contradicted the e2e case for the
    // retry count: the chip's title there says 重连中 3/10 while its text said 已配对, and one row
    // cannot claim both.  The title is the more specific fact, so the text was the one that was wrong.
    expect(deviceStatus(row({ paired: true, connection_state: "offline" }))).toBe("paired");
    expect(deviceStatus(row({ paired: true, connection_state: "connecting" }))).toBe("connecting");
    // The sidecar's own flag, which is the same fact and what it sends for a retry it is running.
    expect(
      deviceStatus(row({ paired: true, connection_state: "offline", reconnecting: true })),
    ).toBe("connecting");
    // A *linked* device is never reported as connecting, whatever a stale flag says: the link is the
    // stronger evidence.
    expect(
      deviceStatus(row({ paired: true, connection_state: "online", reconnecting: true })),
    ).toBe("syncing");
  });

  it("counts a relay link as a link, and the relay's own reading for a relay-only row", () => {
    // A device paired by code and reachable on the relay: this is the case
    // `relay_online` exists for, and it is not local.
    expect(
      deviceStatus(row({ paired: false, relay_paired: true, relay_online: true })),
    ).toBe("syncing");
    expect(
      deviceStatus(row({ paired: false, relay_paired: true, relay_online: false })),
    ).toBe("paired");
    // A row that exists only for the relay: `connection_state` is the relay's.
    expect(deviceStatus(row({ relay: true, relay_paired: true, connection_state: "online" }))).toBe(
      "syncing",
    );
    expect(
      deviceStatus(row({ relay: true, relay_paired: true, connection_state: "offline" })),
    ).toBe("paired");
  });

  it("agrees with the chat button on which way the difference runs", () => {
    // Linked implies reachable; reachable does not imply linked.  Both halves are
    // asserted, because the whole bug was treating them as the same predicate.
    //
    // The word must also follow `localLinkEstablished` exactly: the words that
    // claim a link (正在同步, 已连接) when it holds, and the words that do not
    // (已配对, 连接中, 发现, 离线) when it does not.  An earlier version of this
    // test asserted "never connected", which was wrong about the `online` row --
    // the one case where 已连接 is the right answer.
    const claiming = ["syncing", "connected"];
    for (const state of ["online", "connecting", "discovered", "offline"] as const) {
      const device = row({ connection_state: state });
      const linked = localLinkEstablished(device);
      const word = deviceStatus(device);
      if (linked) {
        expect(chatReachable(device)).toBe(true);
        expect(claiming).toContain(word);
      } else {
        expect(claiming).not.toContain(word);
      }
    }
  });
});

// A file is reachable over either route now: the direct link, or an internet
// pairing whose relay sees the peer up (the sidecar cuts the chunks to a broker
// message).  It is still a link that exists — a discovered or dialing row is not
// a target, because the send goes to a peer the transport can reach.
describe("what can receive a file", () => {
  it("accepts a device on the direct link", () => {
    expect(fileReachable(row({ connection_state: "online" }))).toBe(true);
    // Paired or not: the transfers page sends to whatever is on this network.
    expect(fileReachable(row({ connection_state: "online", paired: false }))).toBe(true);
  });

  it("accepts an internet-paired device when the relay sees it up", () => {
    expect(fileReachable(row({
      relay: true, relay_paired: true, relay_online: true, connection_state: "offline",
    }))).toBe(true);
    // A LAN-owned row that is away but still reachable over the relay it holds.
    expect(fileReachable(row({
      relay: false, relay_paired: true, relay_online: true, connection_state: "offline",
    }))).toBe(true);
  });

  it("refuses a device with no route up, and one that is only seen", () => {
    expect(fileReachable(row({
      relay: true, relay_paired: true, relay_online: false, connection_state: "offline",
    }))).toBe(false);
    expect(fileReachable(row({ connection_state: "discovered" }))).toBe(false);
    expect(fileReachable(row({ connection_state: "connecting" }))).toBe(false);
  });
});

// A rename has two destinations and they are not interchangeable: the alias an
// internet pairing carries, and the note a saved LAN peer carries.  The backend
// that writes a note needs that peer to exist (`set_device_note` answers
// NOT_FOUND without one), so the question is whether the row holds an *internet*
// pairing — which is `relay_paired`, not `paired`.
describe("where a row sorts", () => {
  it("ranks a handshake in flight above a live link, the way the tray does", () => {
    // `tray.rs::classify` asks `is_pairing` before `online`; a handshake runs
    // over a link that is already up, so this is the ordinary mid-pairing row.
    // The page read the connection state first and put the same device in front
    // of the connected one there and behind it in the tray.
    const pairing = row({ paired: false, connection_state: "online", pairing_status: "pending" });
    const connected = row({ paired: false, connection_state: "online" });
    expect(deviceRank(pairing)).toBe(1);
    expect(deviceRank(connected)).toBe(0);
    expect(deviceRank(pairing)).toBeGreaterThan(deviceRank(connected));
  });

  it("keeps the tray's grouping for the settled states", () => {
    // The numbers are `tray.rs::DeviceState::rank`: connected/paired-and-online
    // 0, a pairing 1, known-but-away 2, merely seen 3.
    expect(deviceRank(row({ paired: true, connection_state: "online" }))).toBe(0);
    expect(deviceRank(row({ paired: true, connection_state: "offline" }))).toBe(2);
    expect(deviceRank(row({ paired: false, connection_state: "discovered" }))).toBe(3);
    expect(deviceRank(row({ paired: false, relay_paired: true, relay_online: true }))).toBe(0);
    expect(deviceRank(row({ paired: false, relay_paired: true, relay_online: false }))).toBe(2);
  });
});

describe("which name field a rename writes", () => {
  it("writes the note for a device this machine dialed but never paired", () => {
    // `connect_to_peer` records the certificate of every peer it meets, paired
    // or not, so a device chatted with once is in `config.peers` and can hold a
    // note.  Asking `paired` sent it to the alias call instead, where a device
    // with no internet pairing has nothing to write — the one rename in the
    // window that could not be saved.
    expect(renameWritesAlias(row({ paired: false, relay_paired: false }))).toBe(false);
    expect(renameWritesAlias(row({ paired: false }))).toBe(false);
  });

  it("writes the note for a LAN pairing, which is where its name is kept", () => {
    expect(renameWritesAlias(row({ paired: true, relay_paired: false }))).toBe(false);
  });

  it("writes the alias when the row holds an internet pairing", () => {
    // Paired both ways: either destination works, and the alias is the one this
    // row's own card writes.
    expect(renameWritesAlias(row({ paired: true, relay_paired: true }))).toBe(true);
    // The case that `paired` got right by luck and must keep working: an
    // internet-only pairing seen on this network has no saved peer to hold a
    // note, so the note call would answer NOT_FOUND.
    expect(renameWritesAlias(row({ paired: false, relay_paired: true }))).toBe(true);
    // And the relay-only row, which `deviceMenu` sends straight to the alias
    // entry without asking this at all.
    expect(renameWritesAlias(row({ relay: true, paired: true, relay_paired: true }))).toBe(true);
  });

  it("does not follow the paired flag, which is the wrong question", () => {
    // The two rows that differ only in which pairing they hold, asserted side by
    // side: this is the pair the old rule answered identically and wrongly.
    const dialedButUnpaired = row({ paired: false, relay_paired: false });
    const internetOnly = row({ paired: false, relay_paired: true });
    expect(renameWritesAlias(dialedButUnpaired)).not.toBe(renameWritesAlias(internetOnly));
  });
});
