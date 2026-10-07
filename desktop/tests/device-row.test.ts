import { describe, expect, it } from "vitest";
import { chatReachable, deviceStatus, localLinkEstablished } from "../src/lib/device-row";
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

  it("calls a paired device with no link paired, whatever the attempt is doing", () => {
    // The word is about the pairing and the link, and a retry in flight does not
    // make it syncing: nothing is moving yet.
    expect(deviceStatus(row({ paired: true, connection_state: "offline" }))).toBe("paired");
    expect(deviceStatus(row({ paired: true, connection_state: "connecting" }))).toBe("paired");
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
