import { describe, expect, it, vi } from "vitest";
import { bridge } from "../src/api/bridge";
import {
  asDeliveryStatus, chatReceipt, createDeliveryStore, DELIVERY_MESSAGE_CAP,
} from "../src/stores/delivery";

vi.mock("../src/api/bridge", () => ({
  bridge: { relayDeliveryStatus: vi.fn() },
}));

/** The store reads a peer's ledger; these two are its only gates.
 *
 * `apply` is the fold behind the `relay.delivery.changed` event and `load` the
 * one-shot fetch that fills in whatever happened before this window opened, so
 * every case here is about one of those two agreeing with the other.
 */
describe("relay delivery receipts", () => {
  it("recognizes the four statuses the ledger uses and nothing else", () => {
    expect(asDeliveryStatus("sent")).toBe("sent");
    expect(asDeliveryStatus("delivered")).toBe("delivered");
    expect(asDeliveryStatus("failed")).toBe("failed");
    expect(asDeliveryStatus("queued")).toBe("queued");
    // A newer host's extra state, an emptied field, a number: each would stamp
    // a bubble with a word no label covers.
    expect(asDeliveryStatus("read")).toBeNull();
    expect(asDeliveryStatus("")).toBeNull();
    expect(asDeliveryStatus(undefined)).toBeNull();
    expect(asDeliveryStatus(3)).toBeNull();
  });

  it("stamps an outgoing text bubble, and leaves every other bubble alone", () => {
    const messages = { m1: "delivered" as const };
    const bubble = { outgoing: true, kind: "text", status: "done", msg_id: "m1" };
    expect(chatReceipt(bubble, messages)).toBe("delivered");
    // Incoming messages owe no receipt, and a file bubble has its own state.
    expect(chatReceipt({ ...bubble, outgoing: false }, messages)).toBeNull();
    expect(chatReceipt({ ...bubble, kind: "file" }, messages)).toBeNull();
    // A message that never left keeps its failed label and its resend button.
    expect(chatReceipt({ ...bubble, status: "failed" }, messages)).toBeNull();
    // No id (an older host), or an id the ledger never mentioned: no pill.
    expect(chatReceipt({ ...bubble, msg_id: "" }, messages)).toBeNull();
    expect(chatReceipt({ ...bubble, msg_id: "m2" }, messages)).toBeNull();
    expect(chatReceipt(null, messages)).toBeNull();
  });

  it("counts a queued send once however often the ledger repeats it", () => {
    const store = createDeliveryStore(() => true);
    expect(store.apply({ peer_id: "a", msg_id: "m1", status: "queued" })).toEqual(
      { peerId: "a", msgId: "m1", status: "queued" });
    // The ledger re-broadcasts a status on every retry; counting each one would
    // inflate the badge for as long as the peer stays offline.
    store.apply({ peer_id: "a", msg_id: "m1", status: "queued" });
    expect(store.pending("a")).toBe(1);
    store.apply({ peer_id: "a", msg_id: "m1", status: "sent" });
    expect(store.pending("a")).toBe(0);
    store.apply({ peer_id: "a", msg_id: "m1", status: "delivered" });
    expect(store.lastStatus("a")).toBe("delivered");
    expect(store.state.messages.m1).toBe("delivered");
  });

  it("ignores an event it cannot key or place", () => {
    const store = createDeliveryStore(() => true);
    expect(store.apply({ peer_id: "", msg_id: "m1", status: "sent" })).toBeNull();
    expect(store.apply({ peer_id: "a", msg_id: "m1", status: "read" })).toBeNull();
    expect(store.apply(null)).toBeNull();
    expect(store.pending("a")).toBe(0);
    expect(store.shown("a")).toBe(false);
  });

  it("keeps the bubble map to the tail of a long session", () => {
    const store = createDeliveryStore(() => true);
    for (let index = 0; index <= DELIVERY_MESSAGE_CAP; index += 1) {
      store.apply({ peer_id: "a", msg_id: `m${index}`, status: "sent" });
    }
    expect(store.state.messages.m0).toBeUndefined();
    expect(store.state.messages[`m${DELIVERY_MESSAGE_CAP}`]).toBe("sent");
  });

  it("seeds a peer from the ledger the host already holds, newest first", async () => {
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockResolvedValue({
      pending: 1,
      items: [
        { msg_id: "m2", status: "delivered" },
        { msg_id: "m1", status: "queued" },
      ],
    });
    const store = createDeliveryStore(() => true);
    expect(await store.load("a")).toBe(true);
    expect(bridge.relayDeliveryStatus).toHaveBeenCalledWith("a");
    // Rows arrive newest first, so the first one is the result the card names.
    expect(store.lastStatus("a")).toBe("delivered");
    expect(store.pending("a")).toBe(1);
    expect(store.shown("a")).toBe(true);
    // The fetched rows are what the chat bubbles read too, so a message sent
    // before this window opened still shows how it ended.
    expect(store.state.messages.m2).toBe("delivered");
  });

  it("never counts a row it cannot key", async () => {
    // No total on the answer: this is the older backend, so the count has to
    // come from the rows themselves.
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockResolvedValue({
      items: [{ status: "queued" }, { msg_id: "m1", status: "delivered" }],
    } as any);
    const store = createDeliveryStore(() => true);
    await store.load("a");
    // The unkeyable row is skipped whole: counted, it would put a number on the
    // badge that no later transition could ever retire — and, being first, it
    // also cannot stand as the peer's latest result.
    expect(store.pending("a")).toBe(0);
    expect(store.lastStatus("a")).toBeNull();
    // The keyable row is still there for the bubbles, though.
    expect(store.state.messages.m1).toBe("delivered");
  });

  it("takes the host's own queued total over the rows it can count", async () => {
    // The ledger's total is the one the host retires as items drain, so it wins
    // over counting the rows this window happens to have been handed.
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockResolvedValue({
      pending: 3,
      items: [{ msg_id: "m1", status: "queued" }],
    });
    const store = createDeliveryStore(() => true);
    await store.load("a");
    expect(store.pending("a")).toBe(3);
  });

  it("lets a live transition win the race with a snapshot that started first", async () => {
    let resolve!: (value: { pending: number; items: Record<string, unknown>[] }) => void;
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockImplementation(
      () => new Promise((done) => { resolve = done; }));
    const store = createDeliveryStore(() => true);
    const loading = store.load("a");
    store.apply({ peer_id: "a", msg_id: "m1", status: "delivered" });
    resolve({ pending: 0, items: [{ msg_id: "m1", status: "queued" }] });
    expect(await loading).toBe(false);
    // The snapshot described a moment before the receipt; letting it write back
    // would walk the badge and the bubble backwards.
    expect(store.lastStatus("a")).toBe("delivered");
    expect(store.pending("a")).toBe(0);
  });

  it("hides the line when this host cannot report delivery at all", async () => {
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockRejectedValue(
      { code: "NOT_FOUND", message: "no relay", retryable: false });
    const store = createDeliveryStore(() => true);
    expect(await store.load("a")).toBe(false);
    // "Nothing queued" and "cannot tell" both leave the row off, so a host
    // without a relay never reads as a device with nothing pending.  The
    // refusal stays per-peer: nothing is raised above the row it belongs to.
    expect(store.shown("a")).toBe(false);
    expect(store.peer("a")?.loadFailed).toBe(true);
    // A later real transition still counts, refusal or not.
    store.apply({ peer_id: "a", msg_id: "m1", status: "queued" });
    expect(store.pending("a")).toBe(1);
  });

  it("reads nothing until the sidecar says it is ready", async () => {
    vi.mocked(bridge.relayDeliveryStatus).mockReset().mockResolvedValue({ pending: 0, items: [] });
    let ready = false;
    const store = createDeliveryStore(() => ready);
    expect(await store.load("a")).toBe(false);
    expect(bridge.relayDeliveryStatus).not.toHaveBeenCalled();
    ready = true;
    expect(await store.load("a")).toBe(true);
  });

  it("forgets a peer that is gone, and everything when the sidecar restarts", () => {
    const store = createDeliveryStore(() => true);
    store.apply({ peer_id: "a", msg_id: "m1", status: "delivered" });
    store.apply({ peer_id: "b", msg_id: "m2", status: "queued" });
    store.forget("a");
    expect(store.peer("a")).toBeNull();
    expect(store.pending("b")).toBe(1);
    expect(store.state.messages.m1).toBe("delivered");
    store.reset();
    expect(store.pending("b")).toBe(0);
    expect(store.state.messages.m2).toBeUndefined();
    expect(store.shown("b")).toBe(false);
  });
});
