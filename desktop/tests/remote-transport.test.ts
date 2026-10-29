// @vitest-environment node
// @ts-nocheck
/**
 * Behavioural regression tests for the phone page's relay transport.
 *
 * `@ts-nocheck` is deliberate: this file drives the browser script through
 * Node's `vm`/`webcrypto`/`fs`, and this package does not depend on
 * `@types/node` (the frontend tsconfig is DOM + vitest only).  Adding that
 * dependency for one test file is not worth it, and the file is a harness of
 * untyped browser globals either way; what it must get right is the behaviour
 * asserted below, not its own types.
 *
 * The file under test is `internal/web/static/remote-transport.js`: a plain
 * ES5 IIFE that the Pages build serves before `mobile.html`'s own script.  It
 * speaks MQTT 3.1.1 over a WebSocket to a public broker and carries sealed
 * request/response frames to the desktop's CompanionTunnel, so it cannot be
 * imported as a module and it must not be reimplemented here.  Instead the real
 * file is read from disk and run in a Node `vm` context whose globals are the
 * browser APIs it expects: a fake `WebSocket`, node's WebCrypto (the same
 * `subtle` calls the browser would make), `TextEncoder`/`TextDecoder`,
 * `btoa`/`atob`, `URL`/`URLSearchParams`, `location`, `history`,
 * `sessionStorage`/`localStorage`, a minimal `document` and the page's timers
 * bridged to vitest's fake timers so timeouts advance deterministically.
 *
 * The fake broker implements just enough of the wire format to be the other
 * end of the protocol: it answers CONNECT with CONNACK, parses the phone's
 * QoS1 PUBLISH packets (fixed header, remaining-length varint, topic, packet
 * id, payload), records them, and answers the sealed `hello` with a sealed
 * `hello_ack` (and app-level `ping` with `pong`) minted through the *phone's
 * own* `tunnel.channel`, which is the same key the desktop holds (both sides
 * derive it from the pairing token).  No encryption is faked, so a frame that
 * the phone cannot open is a test failure rather than a convenience.
 *
 * What is being pinned down is the connection strategy and the defect it
 * fixes: `connect()` dials *every* configured broker at once and adopts the
 * first attempt whose sealed `hello_ack` arrives, closing the losers.  Public
 * brokers do not federate and the desktop connects to only one of them, so the
 * old sequential dialler spent a full hello timeout on every wrong broker
 * ("12-20 s of connecting"), and before that a broker's CONNACK alone was
 * treated as "relay online" — the page said "connected" while every request
 * timed out.  Only the sealed `hello_ack` proves the computer is on a broker,
 * and the race must not make the fastest broker wait behind a silent one.
 *
 * Cases 1/2/5 pin the handshake itself; case 3 pins the race's loser being
 * closed while the winner is adopted; case 6 pins that the fastest broker wins
 * without advancing HELLO_TIMEOUT_MS; case 7 pins the all-silent failure turning
 * into 'retrying' (reason 'computer'); case 4 pins the presence watchdog
 * leaving online and a fresh race bringing the tunnel back.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { webcrypto } from "node:crypto";
import { TextDecoder, TextEncoder } from "node:util";
import {
  setImmediate as realSetImmediate,
} from "node:timers";
import { URL as NodeURL, URLSearchParams as NodeURLSearchParams } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/*
 * The waits below are bounded in real time rather than in fixed flush rounds,
 * because WebCrypto settles on libuv's threadpool and a loaded CI machine can
 * schedule it late.  Give the test itself room to report a useful failure
 * instead of vitest's default 5 s timeout firing first.
 */
vi.setConfig({ testTimeout: 20_000 });

const SOURCE_PATH = fileURLToPath(
  new URL("../../internal/web/static/remote-transport.js", import.meta.url),
);

/**
 * The real transport, read once.  If this read fails the suite must fail: a
 * test that silently skipped would let the bug back in.
 */
const REMOTE_SOURCE = readFileSync(SOURCE_PATH, "utf8");

const TEST_TOKEN = "regression-test-pairing-token";
const BROKER_A = "wss://broker-a.example.com/mqtt";
const BROKER_B = "wss://broker-b.example.com/mqtt";
const DESKTOP_NAME = "Desk";

/** Real macrotask yield: vitest's fake timers replace the globals, not this. */
const realQueueMicrotask = globalThis.queueMicrotask.bind(globalThis);

/**
 * Real wall-clock time, captured at module load, before vitest installs its
 * fake timers (which replace both `Date` and `process.hrtime`).  Waiting for
 * WebCrypto has to happen in real time: a crypto op runs on libuv's threadpool
 * and a loaded CI machine can schedule it many event-loop turns later than a
 * fixed flush loop would wait.
 */
const realEpochNow: () => number = Date.now;


const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

// ---------------------------------------------------------------------------
// MQTT byte helpers (host side: the fake broker and its parser)
// ---------------------------------------------------------------------------

function bytesOf(values: number[]): Uint8Array {
  return new Uint8Array(values);
}

function concatBytes(parts: readonly Uint8Array[]): Uint8Array {
  let size = 0;
  for (const part of parts) size += part.length;
  const out = new Uint8Array(size);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** Remaining-length varint from MQTT 3.1.1 section 2.2.3. */
function remainingLengthBytes(length: number): Uint8Array {
  const out: number[] = [];
  let value = length;
  do {
    let byte = value % 128;
    value = Math.floor(value / 128);
    if (value > 0) byte |= 0x80;
    out.push(byte);
  } while (value > 0);
  return new Uint8Array(out);
}

function mqttPacket(header: number, parts: readonly Uint8Array[]): Uint8Array {
  const body = concatBytes(parts);
  return concatBytes([bytesOf([header]), remainingLengthBytes(body.length), body]);
}

function mqttString(text: string): Uint8Array {
  const raw = textEncoder.encode(text);
  const out = new Uint8Array(2 + raw.length);
  out[0] = raw.length >> 8;
  out[1] = raw.length & 0xff;
  out.set(raw, 2);
  return out;
}

/** A PUBLISH packet as the fake broker would send one (QoS0 by default). */
function publishPacket(topic: string, payload: Uint8Array, qos = 0, packetId = 0): Uint8Array {
  const parts = [mqttString(topic)];
  if (qos > 0) parts.push(bytesOf([packetId >> 8, packetId & 0xff]));
  parts.push(payload);
  return mqttPacket(0x30 | (qos << 1), parts);
}

function connackPacket(accepted: boolean): Uint8Array {
  return accepted ? bytesOf([0x20, 0x02, 0x00, 0x00]) : bytesOf([0x20, 0x02, 0x00, 0x05]);
}

function pubackPacket(packetId: number): Uint8Array {
  return bytesOf([0x40, 0x02, packetId >> 8, packetId & 0xff]);
}

function subackPacket(packetId: number): Uint8Array {
  return bytesOf([0x90, 0x03, packetId >> 8, packetId & 0xff, 0x00]);
}

function pingrespPacket(): Uint8Array {
  return bytesOf([0xd0, 0x00]);
}

interface RawPacket {
  /** The whole packet, fixed header included. */
  packet: Uint8Array;
  /** Length of the fixed header: one type byte plus the remaining-length varint. */
  headerLength: number;
  /** Bytes after this packet, still incomplete. */
  rest: Uint8Array;
}

/** Cut one complete MQTT packet off the front of `buffer`, or return null. */
function takePacket(buffer: Uint8Array): RawPacket | null {
  if (buffer.length < 2) return null;
  let multiplier = 1;
  let length = 0;
  let index = 1;
  let byte = 0;
  do {
    if (index >= buffer.length) return null;
    byte = buffer[index];
    index += 1;
    length += (byte & 0x7f) * multiplier;
    multiplier *= 128;
    if (multiplier > 128 * 128 * 128) return null;
  } while (byte & 0x80);
  const total = index + length;
  if (buffer.length < total) return null;
  return { packet: buffer.slice(0, total), headerLength: index, rest: buffer.slice(total) };
}

interface ParsedPublish {
  topic: string;
  qos: number;
  packetId: number;
  payload: Uint8Array;
}

function parsePublish(packet: Uint8Array, headerLength: number): ParsedPublish {
  const qos = (packet[0] >> 1) & 0x03;
  const topicLength = (packet[headerLength] << 8) | packet[headerLength + 1];
  const topic = textDecoder.decode(
    packet.slice(headerLength + 2, headerLength + 2 + topicLength),
  );
  let at = headerLength + 2 + topicLength;
  let packetId = 0;
  if (qos > 0) {
    packetId = (packet[at] << 8) | packet[at + 1];
    at += 2;
  }
  return { topic, qos, packetId, payload: packet.slice(at) };
}

interface ParsedSubscribe {
  topic: string;
  packetId: number;
  qos: number;
}

function parseSubscribe(packet: Uint8Array, headerLength: number): ParsedSubscribe {
  const packetId = (packet[headerLength] << 8) | packet[headerLength + 1];
  const topicLength = (packet[headerLength + 2] << 8) | packet[headerLength + 3];
  const topic = textDecoder.decode(
    packet.slice(headerLength + 4, headerLength + 4 + topicLength),
  );
  return { topic, packetId, qos: packet[headerLength + 4 + topicLength] };
}

/** Copy whatever the page handed to `WebSocket#send` into a host Uint8Array. */
function toHostBytes(data: unknown): Uint8Array {
  if (data instanceof Uint8Array) return new Uint8Array(data);
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) {
    const view = data as ArrayBufferView;
    const copy = new Uint8Array(view.byteLength);
    copy.set(new Uint8Array(view.buffer, view.byteOffset, view.byteLength));
    return copy;
  }
  if (typeof data === "string") return textEncoder.encode(data);
  throw new Error(`fake WebSocket cannot understand sent payload ${Object.prototype.toString.call(data)}`);
}

/** Exactly what the browser puts in `MessageEvent#data` for binaryType=arraybuffer. */
function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.length);
  copy.set(bytes);
  return copy.buffer;
}

// ---------------------------------------------------------------------------
// Fake broker behaviours, per broker URL
// ---------------------------------------------------------------------------

interface BrokerBehavior {
  /** Answer MQTT CONNECT with CONNACK (default), say no, or never answer. */
  connack?: "accept" | "refuse" | "silent";
  /** Answer the sealed hello with a sealed hello_ack. */
  hello?: "ack" | "ignore";
  /** Answer sealed app-level ping frames with sealed pong (default: yes). */
  pong?: boolean;
}

interface RecordedPublish extends ParsedPublish {
  socket: FakeSocket;
}

interface BootOptions {
  brokers?: string[];
  behaviors?: Record<string, BrokerBehavior>;
  token?: string;
}

/** URL + fragment the page is "opened" with, as the QR code would carry it. */
function pageHash(token: string, brokers: string[]): string {
  return (
    "#v=1&t=" +
    encodeURIComponent(token) +
    "&b=" +
    encodeURIComponent(JSON.stringify(brokers))
  );
}

// ---------------------------------------------------------------------------
// The fake WebSocket
// ---------------------------------------------------------------------------

/**
 * A WebSocket as far as the transport is concerned.
 *
 * Nothing opens by itself: the test calls `accept()` on each socket it wants to
 * see the MQTT handshake on.  Under the parallel race several sockets are
 * created in the same tick, so accepting them one at a time is also how "A has
 * answered but B has not" is driven.  Bytes the page sends are queued and
 * handled in microtasks, so a broker reply never re-enters the page from inside
 * `send()`.
 */
class FakeSocket {
  readonly url: string;
  readonly protocols: string | string[] | undefined;
  binaryType = "blob";
  readyState = 0; // 0 CONNECTING, 1 OPEN, 3 CLOSED
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  /** Set once the broker has dealt with the phone's first publish (the hello). */
  helloHandled = false;
  /** How many times the page asked this socket to close (race losers, failures). */
  closeCalls = 0;

  private readonly page: RelayPage;

  constructor(page: RelayPage, url: string, protocols?: string | string[]) {
    this.page = page;
    this.url = url;
    this.protocols = protocols;
    // The harness counts every dial: rotation assertions read this list.
    page.dials.push(url);
    page.sockets.push(this);
  }

  send(data: unknown): void {
    this.page.receiveFromPhone(this, toHostBytes(data));
  }

  /** Fire the browser's `open` event: the page then writes MQTT CONNECT. */
  accept(): void {
    if (this.readyState !== 0) return;
    this.readyState = 1;
    if (this.onopen) this.onopen();
  }

  /** Deliver one complete broker→phone packet. */
  deliver(packet: Uint8Array): void {
    if (this.readyState !== 1 || !this.onmessage) return;
    this.onmessage({ data: toArrayBuffer(packet) });
  }

  close(): void {
    this.closeCalls += 1;
    if (this.readyState === 3) return;
    this.readyState = 3;
    // The browser dispatches `close` asynchronously; keeping that ordering is
    // what lets the transport notice a stale socket and ignore its handlers.
    realQueueMicrotask(() => {
      if (this.onclose) this.onclose();
    });
  }
}

// ---------------------------------------------------------------------------
// The page harness: vm context + fake browser + fake broker in one
// ---------------------------------------------------------------------------

function memoryStorage(): {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  clear(): void;
} {
  const data = new Map<string, string>();
  return {
    getItem(key: string): string | null {
      return data.has(key) ? (data.get(key) as string) : null;
    },
    setItem(key: string, value: string): void {
      data.set(key, String(value));
    },
    removeItem(key: string): void {
      data.delete(key);
    },
    clear(): void {
      data.clear();
    },
  };
}

interface FrameRecord {
  type?: string;
  [key: string]: unknown;
}

interface DecodedFrame extends FrameRecord {
  /** The attempt socket whose PUBLISH carried this frame. */
  socket: FakeSocket;
}

interface StateRecord {
  state?: string;
  broker?: string;
  reason?: string;
  computer?: string;
  /** `connecting` reports the parallel race: every broker is dialled at once. */
  parallel?: boolean;
  total?: number;
  /** How many hello_acks the broker had delivered when this state was reported. */
  acksAtState: number;
}

/**
 * One loaded phone page: its sandbox, its tunnel, and the broker on the other
 * end of the fake socket(s).  `boot()` is the only entry point that runs the
 * real script.
 */
class RelayPage {
  readonly token: string;
  readonly brokers: string[];
  readonly behaviors = new Map<string, BrokerBehavior>();
  readonly hash: string;
  readonly href: string;

  sandbox: Record<string, any> = {};
  remote: any = null;

  // Things the fake broker observed.
  sockets: FakeSocket[] = [];
  dials: string[] = [];
  /** Broker URL of every CONNACK the broker actually sent. */
  connacks: string[] = [];
  subscribes: { socket: FakeSocket; topic: string; packetId: number; qos: number }[] = [];
  publishes: RecordedPublish[] = [];
  /** Frames the fake broker successfully decrypted, tagged with their attempt. */
  decoded: DecodedFrame[] = [];
  /** Number of hello_ack frames delivered to the page. */
  helloAcks = 0;
  /** Ordered markers for assertions that care about "before"/"after". */
  events: string[] = [];
  states: StateRecord[] = [];
  errors: unknown[] = [];

  /** Virtual time advanced through this page's fake timers, in ms. */
  private clock = 0;
  private readonly clientBuffers = new Map<FakeSocket, Uint8Array>();
  private taskQueue: Array<() => void | Promise<void>> = [];
  private draining = false;
  /** Packet-id source for the broker->phone QoS1 publishes (hello_ack). */
  private downPacketId = 1;

  constructor(options: BootOptions) {
    this.token = options.token ?? TEST_TOKEN;
    this.brokers = options.brokers ?? [BROKER_A];
    this.hash = pageHash(this.token, this.brokers);
    this.href = `https://127.0.0.1:8765/mobile.html${this.hash}`;
    for (const url of this.brokers) {
      this.behaviors.set(url, {
        connack: "accept",
        hello: "ignore",
        pong: true,
        ...(options.behaviors?.[url] ?? {}),
      });
    }
  }

  async boot(): Promise<this> {
    this.sandbox = this.createSandbox();
    vm.createContext(this.sandbox);
    vm.runInContext(REMOTE_SOURCE, this.sandbox, { filename: "remote-transport.js" });

    this.remote = this.sandbox.__CLIPSYNC_REMOTE__;
    if (!this.remote) {
      throw new Error(
        `remote-transport.js did not install window.__CLIPSYNC_REMOTE__ (source: ${SOURCE_PATH})`,
      );
    }
    // Record the page's state timeline from the first connecting report on.
    this.remote.onState((info: any) => {
      this.states.push({
        state: info?.state,
        broker: info?.broker,
        reason: info?.reason,
        computer: info?.computer,
        parallel: info?.parallel,
        total: info?.total,
        acksAtState: this.helloAcks,
      });
      this.events.push(`state:${String(info?.state)}`);
    });

    // `Tunnel#start()` derives the channel and topic before dialling, so the
    // whole race appears a few async crypto steps after the script ran.  Every
    // configured broker is dialled in the same tick, so boot only completes
    // once the full field is on the wire.
    await this.waitFor(
      () => this.dials.length >= this.brokers.length,
      "every configured broker to be dialled",
    );
    return this;
  }

  // -- browser globals ------------------------------------------------------

  private createSandbox(): Record<string, any> {
    const sandbox: Record<string, any> = {};
    const page = this;
    const timerBridge = {
      setTimeout: (handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
        (globalThis as any).setTimeout(handler, timeout, ...args),
      clearTimeout: (handle?: any) => (globalThis as any).clearTimeout(handle),
      setInterval: (handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
        (globalThis as any).setInterval(handler, timeout, ...args),
      clearInterval: (handle?: any) => (globalThis as any).clearInterval(handle),
    };

    // `window` is the contextified global itself: the page assigns
    // `window.__CLIPSYNC_REMOTE__` and `window.fetch`, and both must be
    // reachable from here and from inside the script.
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    // `location` and friends are stubbed below; window.__CLIPSYNC_REMOTE__ is
    // installed by the script into this same object.
    sandbox.crypto = webcrypto;
    sandbox.TextEncoder = TextEncoder;
    sandbox.TextDecoder = TextDecoder;
    sandbox.URL = NodeURL;
    sandbox.URLSearchParams = NodeURLSearchParams;
    sandbox.btoa = (text: string) => Buffer.from(String(text), "binary").toString("base64");
    sandbox.atob = (value: string) => Buffer.from(String(value), "base64").toString("binary");
    // Let the script's Date.now() share the fake clock the page reports.
    sandbox.Date = globalThis.Date;
    Object.assign(sandbox, timerBridge);
    sandbox.fetch = () =>
      Promise.reject(new Error("the harness does not route real fetches"));
    sandbox.Response = globalThis.Response;
    sandbox.Blob = globalThis.Blob;
    sandbox.console = console;
    sandbox.addEventListener = () => undefined;
    sandbox.removeEventListener = () => undefined;
    sandbox.history = {
      replaceState: () => undefined,
    };
    sandbox.sessionStorage = memoryStorage();
    sandbox.localStorage = memoryStorage();
    sandbox.location = {
      href: this.href,
      hash: this.hash,
      search: "",
      pathname: "/mobile.html",
      hostname: "127.0.0.1",
      protocol: "https:",
    };
    sandbox.document = {
      readyState: "complete",
      visibilityState: "visible",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      getElementById: () => null,
      createElement: () => ({
        id: "",
        textContent: "",
        style: {} as Record<string, string>,
        setAttribute: () => undefined,
        appendChild: () => undefined,
        click: () => undefined,
      }),
      body: {
        appendChild: () => undefined,
        removeChild: () => undefined,
      },
    };
    // `new WebSocket(url, ['mqtt'])` — the script keeps the instance and its
    // handlers; the harness records every dial.
    sandbox.WebSocket = function WebSocket(
      this: unknown,
      url: string,
      protocols?: string | string[],
    ) {
      return new FakeSocket(page, String(url), protocols);
    };
    return sandbox;
  }

  // -- fake broker ----------------------------------------------------------

  /** Bytes the page wrote to its socket: buffer them, then handle whole packets. */
  receiveFromPhone(socket: FakeSocket, chunk: Uint8Array): void {
    let buffer = concatBytes([this.clientBuffers.get(socket) ?? new Uint8Array(0), chunk]);
    for (;;) {
      const taken = takePacket(buffer);
      if (!taken) break;
      buffer = taken.rest;
      const { packet, headerLength } = taken;
      this.enqueue(() => {
        this.handleClientPacket(socket, packet, headerLength);
      });
    }
    this.clientBuffers.set(socket, buffer);
  }

  /**
   * Serialise the fake broker's work: a task may itself cause the page to send
   * (via `deliver`), which enqueues more tasks, so no broker reply re-enters
   * the page from inside a `send()` callback.
   */
  private enqueue(task: () => void | Promise<void>): void {
    this.taskQueue.push(task);
    if (this.draining) return;
    this.draining = true;
    realQueueMicrotask(() => {
      void this.drain();
    });
  }

  private async drain(): Promise<void> {
    try {
      while (this.taskQueue.length) {
        const task = this.taskQueue.shift();
        if (!task) continue;
        try {
          await task();
        } catch (error) {
          this.errors.push(error);
        }
      }
    } finally {
      this.draining = false;
    }
  }

  private handleClientPacket(socket: FakeSocket, packet: Uint8Array, headerLength: number): void {
    const type = packet[0] >> 4;
    const behavior = this.behaviorOf(socket.url);
    if (type === 1) {
      // CONNECT: the broker's one and only proof that a socket reached it.
      if (behavior.connack === "refuse") {
        this.connacks.push(socket.url);
        this.events.push("connack:refused");
        this.enqueue(() => socket.deliver(connackPacket(false)));
        return;
      }
      if (behavior.connack === "silent") return;
      this.connacks.push(socket.url);
      this.events.push("connack");
      this.enqueue(() => socket.deliver(connackPacket(true)));
      return;
    }
    if (type === 8) {
      // SUBSCRIBE: record what the phone wants to listen on, then SUBACK.
      const subscribe = parseSubscribe(packet, headerLength);
      this.subscribes.push({ socket, ...subscribe });
      this.enqueue(() => socket.deliver(subackPacket(subscribe.packetId)));
      return;
    }
    if (type === 3) {
      // PUBLISH: a hello, a ping, or (in tests that go that far) a request.
      const publish = parsePublish(packet, headerLength);
      this.publishes.push({ socket, ...publish });
      if (publish.qos === 1) {
        this.enqueue(() => socket.deliver(pubackPacket(publish.packetId)));
      }
      this.enqueue(() => this.handlePublish(socket, publish));
      return;
    }
    if (type === 12) {
      // PINGREQ: the broker itself is alive; only says the socket is not dead.
      this.enqueue(() => socket.deliver(pingrespPacket()));
    }
  }

  private async handlePublish(socket: FakeSocket, publish: ParsedPublish): Promise<void> {
    const frame = await this.decodeFrame(publish.payload);
    if (!frame) {
      this.errors.push(new Error("the fake broker received a PUBLISH it could not decrypt"));
      return;
    }
    this.decoded.push({ ...frame, socket });
    if (!socket.helloHandled) {
      socket.helloHandled = true;
      if (frame.type === "hello" && this.behaviorOf(socket.url).hello === "ack") {
        await this.answerHello(socket);
      }
      return;
    }
    // After the handshake the phone's only unattended frame is the app-level
    // presence ping.  Answer it exactly as CompanionTunnel.handle_blob does.
    if (frame.type !== "ping") return;
    if (this.behaviorOf(socket.url).pong === false) return;
    const pong = await this.sealFrame({
      v: 1,
      type: "pong",
      ts: Math.round(Date.now()) / 1000,
    });
    // The race may have closed this attempt while the pong was sealed; a frame
    // for a closed socket is not an answer.
    if (socket.readyState !== 1) return;
    this.events.push("pong");
    socket.deliver(publishPacket(this.downTopic(), pong, 0));
  }

  private async answerHello(socket: FakeSocket): Promise<void> {
    const blob = await this.sealFrame({
      v: 1,
      type: "hello_ack",
      computer_name: DESKTOP_NAME,
      caps: [],
      ts: Math.round(Date.now()) / 1000,
    });
    // The page may have adopted a different attempt (and closed this socket)
    // while the ack was being sealed; only a delivered ack can win a race.
    if (socket.readyState !== 1) return;
    this.helloAcks += 1;
    this.events.push("hello_ack");
    // CompanionTunnel._handle_hello answers at QoS1: the broker delivers it
    // with a packet id, and the phone PUBACKs it before consume() sees it.
    socket.deliver(publishPacket(this.downTopic(), blob, 1, this.nextDownPacketId()));
  }

  private nextDownPacketId(): number {
    const packetId = this.downPacketId;
    this.downPacketId = (this.downPacketId % 65_535) + 1;
    return packetId;
  }

  /** Seal a frame with the phone's own channel, i.e. as the desktop would. */
  async sealFrame(frame: Record<string, unknown>): Promise<Uint8Array> {
    const blob = await this.remote.tunnel.channel.seal(frame);
    return toHostBytes(blob);
  }

  async decodeFrame(blob: Uint8Array): Promise<FrameRecord | null> {
    try {
      const frame = await this.remote.tunnel.channel.open(blob);
      return (frame as FrameRecord) ?? null;
    } catch (error) {
      this.errors.push(error);
      return null;
    }
  }

  /** A compact dump used in failure messages when a timing assertion misses. */
  describeForFailure(): Record<string, unknown> {
    const tunnel = this.remote?.tunnel ?? {};
    return {
      clock: this.clock,
      states: this.states,
      events: this.events,
      dials: this.dials,
      sockets: this.sockets.length,
      socketStates: this.sockets.map((socket) => ({
        url: socket.url,
        readyState: socket.readyState,
        closeCalls: socket.closeCalls,
        helloHandled: socket.helloHandled,
      })),
      helloAcks: this.helloAcks,
      publishes: this.publishes.map((publish) => ({
        url: publish.socket.url,
        topic: publish.topic,
        qos: publish.qos,
        bytes: publish.payload.length,
      })),
      decoded: this.decoded.map((frame) => `${frame.socket.url}:${String(frame.type)}`),
      errors: this.errors.map((error) => String(error)),
      tunnel: {
        ready: tunnel.ready,
        connected: tunnel.connected,
        pongOutstanding: tunnel.pongOutstanding,
        pongMisses: tunnel.pongMisses,
        hasPresenceTimer: Boolean(tunnel.presenceTimer),
        hasHelloTimer: Boolean(tunnel.helloTimer),
        brokerIndex: tunnel.brokerIndex,
      },
    };
  }

  // -- reading the page's state --------------------------------------------

  behaviorOf(url: string): BrokerBehavior {
    const behavior = this.behaviors.get(url);
    if (!behavior) throw new Error(`test never configured broker ${url}`);
    return behavior;
  }

  setBehavior(url: string, patch: BrokerBehavior): void {
    this.behaviors.set(url, { ...this.behaviorOf(url), ...patch });
  }

  upTopic(): string {
    return String(this.remote.tunnel.upTopic ?? "");
  }

  downTopic(): string {
    return String(this.remote.tunnel.downTopic ?? "");
  }

  /** The object `window.__CLIPSYNC_REMOTE__.state()` returns. */
  pageState(): StateRecord | null {
    const state = this.remote.state();
    return (state as StateRecord | null) ?? null;
  }

  /** Virtual time advanced through this page's fake clock, in milliseconds. */
  now(): number {
    return this.clock;
  }

  isOnline(): boolean {
    return this.remote.online === true && this.pageState()?.state === "online";
  }

  /** Accept the oldest socket still waiting for its `open` event. */
  acceptNext(): FakeSocket {
    const socket = this.sockets.find((candidate) => candidate.readyState === 0);
    if (!socket) {
      throw new Error(
        `no CONNECTING socket to accept (${this.sockets.length} socket(s), ` +
          this.sockets.map((candidate) => candidate.readyState).join(",") + ")",
      );
    }
    socket.accept();
    return socket;
  }

  /** Accept every socket still waiting for its `open` event (a whole race). */
  acceptAll(): FakeSocket[] {
    const accepted: FakeSocket[] = [];
    for (const socket of this.sockets) {
      if (socket.readyState !== 0) continue;
      socket.accept();
      accepted.push(socket);
    }
    return accepted;
  }

  /** Every socket dialled to `url`, in creation order. */
  socketsFor(url: string): FakeSocket[] {
    return this.sockets.filter((socket) => socket.url === url);
  }

  /** Sockets the page still holds open: the live tunnel should be the only one. */
  openSockets(): FakeSocket[] {
    return this.sockets.filter((socket) => socket.readyState === 1);
  }

  // -- time and async plumbing ---------------------------------------------

  /**
   * Let real macrotasks run (WebCrypto is genuinely async: its promises only
   * settle on a later event-loop turn, which fake timers do not provide).
   */
  async flush(rounds = 6): Promise<void> {
    for (let index = 0; index < rounds; index += 1) {
      await new Promise<void>((resolve) => {
        realSetImmediate(resolve);
      });
    }
  }

  /**
   * Poll `predicate` while letting queued microtasks and crypto finish.
   *
   * The budget is real wall-clock time, not a fixed number of loop turns: the
   * fake clock does not move here, so this only gives WebCrypto and the fake
   * broker's promise chain room to settle.  A loaded machine must not turn a
   * slow-but-correct handshake into a test failure.
   */
  async waitFor(predicate: () => boolean, label: string, timeoutMs = 5_000): Promise<void> {
    const deadline = realEpochNow() + timeoutMs;
    for (;;) {
      if (predicate()) return;
      if (realEpochNow() > deadline) {
        if (predicate()) return;
        throw new Error(
          `timed out after ${timeoutMs}ms waiting for ${label}: ` +
            JSON.stringify(this.describeForFailure()),
        );
      }
      await this.flush(2);
    }
  }

  /** Advance the fake clock in small steps so async broker replies keep up. */
  async advance(ms: number): Promise<void> {
    let remaining = ms;
    while (remaining > 0) {
      const step = Math.min(remaining, 250);
      remaining -= step;
      await vi.advanceTimersByTimeAsync(step);
      this.clock += step;
      await this.flush(6);
    }
  }

  /**
   * Advance until `predicate()` holds, at most `maxMs` of virtual time.
   * Returns the virtual time consumed, so a test can assert *when* something
   * happened rather than only that it did.
   */
  async advanceUntil(
    predicate: () => boolean,
    maxMs: number,
    label: string,
    stepMs = 250,
  ): Promise<number> {
    const startedAt = this.clock;
    while (this.clock - startedAt <= maxMs) {
      if (predicate()) return this.clock - startedAt;
      await this.advance(stepMs);
    }
    if (predicate()) return this.clock - startedAt;
    throw new Error(`timed out after ${maxMs}ms of virtual time waiting for ${label}`);
  }
}

/** Every "online" the page reported must follow a broker-delivered hello_ack. */
function expectNoOnlineBeforeHelloAck(page: RelayPage): void {
  const premature = page.states.filter(
    (state) => state.state === "online" && state.acksAtState === 0,
  );
  expect(premature).toEqual([]);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("phone relay transport handshake (internal/web/static/remote-transport.js)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("case 1: a CONNACK alone does not make the relay online; hello goes out and is unanswered", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A],
      behaviors: { [BROKER_A]: { connack: "accept", hello: "ignore" } },
    }).boot();

    // One broker in the QR code: one dial, one attempt.
    expect(page.dials).toEqual([BROKER_A]);
    page.acceptAll();
    await page.waitFor(
      () => page.connacks.length === 1 && page.publishes.length >= 1 && page.decoded.length >= 1,
      "CONNACK and the sealed hello publish",
    );

    // The broker accepted the MQTT connection and the phone asked the computer
    // to introduce itself — but no hello_ack has come back yet:
    expect(page.connacks).toEqual([BROKER_A]);
    expect(page.remote.online).toBe(false);
    expect(page.pageState()?.state).not.toBe("online");

    // The publish really is the sealed hello, on the up topic CompanionTunnel
    // reads, not merely some opaque frame.
    const hello = page.publishes[0];
    expect(hello.topic).toBe(page.upTopic());
    expect(hello.topic.endsWith("/up")).toBe(true);
    expect(page.decoded[0].type).toBe("hello");
    expect(page.decoded[0].socket.url).toBe(BROKER_A);

    // The down topic was subscribed before the hello went out.
    expect(page.subscribes.length).toBe(1);
    expect(page.subscribes[0].topic).toBe(page.downTopic());
    expect(page.subscribes[0].topic.endsWith("/down")).toBe(true);

    // A second later — long before the hello deadline — the phone is still not
    // pretending: the broker never produced a computer.  (This is the pre-fix
    // failing point: a CONNACK used to mark the tunnel online.)
    await page.advance(1_000);
    expect(page.remote.online).toBe(false);
    expect(page.pageState()?.state).not.toBe("online");
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 2: the sealed hello_ack is what makes the tunnel online", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A],
      behaviors: { [BROKER_A]: { connack: "accept", hello: "ack" } },
    }).boot();

    page.acceptAll();
    await page.waitFor(() => page.remote.online === true, "the tunnel to come online");

    const state = page.pageState();
    expect(state?.state).toBe("online");
    expect(state?.broker).toBe(BROKER_A);
    expect(page.remote.online).toBe(true);
    expect(page.isOnline()).toBe(true);

    // One broker, one dial, one CONNACK, one ack: coming online must be the
    // handshake, not a retry or a second connection.
    expect(page.dials).toEqual([BROKER_A]);
    expect(page.sockets.length).toBe(1);
    expect(page.connacks).toEqual([BROKER_A]);
    expect(page.helloAcks).toBe(1);
    expect(page.openSockets().map((socket) => socket.url)).toEqual([BROKER_A]);

    // Ordering: the page was never online before that ack was delivered.
    const ackIndex = page.events.indexOf("hello_ack");
    const firstOnlineIndex = page.events.indexOf("state:online");
    expect(ackIndex).toBeGreaterThanOrEqual(0);
    expect(firstOnlineIndex).toBeGreaterThan(ackIndex);
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 3: parallel race — the silent broker is closed and the broker that answers is adopted", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A, BROKER_B],
      behaviors: {
        // Broker A accepts the socket but no computer is on it: this is the
        // public-broker-does-not-federate situation from the bug report.
        [BROKER_A]: { connack: "accept", hello: "ignore" },
        // The desktop is actually connected here.
        [BROKER_B]: { connack: "accept", hello: "ack" },
      },
    }).boot();

    // Both brokers are dialled in the same tick, in the QR list order.
    expect(page.dials).toEqual([BROKER_A, BROKER_B]);
    expect(page.sockets.map((socket) => socket.url)).toEqual([BROKER_A, BROKER_B]);
    const socketA = page.socketsFor(BROKER_A)[0];
    const socketB = page.socketsFor(BROKER_B)[0];
    // The page says it is running a race across every broker.
    expect(page.pageState()?.state).toBe("connecting");
    expect(page.pageState()?.parallel).toBe(true);
    expect(page.pageState()?.total).toBe(2);

    // Only broker A has answered so far.  Its CONNACK is not "online": the
    // phone asked the computer and nothing has come back.
    socketA.accept();
    await page.waitFor(
      () =>
        page.connacks.includes(BROKER_A) &&
        page.publishes.some((publish) => publish.socket === socketA) &&
        page.decoded.some((frame) => frame.socket === socketA && frame.type === "hello"),
      "CONNACK and the sealed hello on broker A",
    );
    expect(page.remote.online).toBe(false);
    expect(page.pageState()?.state).not.toBe("online");
    const verifying = page.states.filter((state) => state.state === "verifying");
    expect(verifying).toHaveLength(1);
    expect(verifying[0].broker).toBe(BROKER_A);

    // Broker B is where the computer is.  Its CONNACK + sealed hello_ack must
    // win the race, become the live tunnel, and close loser A on the spot.
    socketB.accept();
    await page.waitFor(() => page.remote.online === true, "the tunnel to come online on broker B");
    expect(page.pageState()?.state).toBe("online");
    expect(page.pageState()?.broker).toBe(BROKER_B);
    expect(page.connacks).toHaveLength(2);
    expect(page.connacks).toEqual(expect.arrayContaining([BROKER_A, BROKER_B]));
    expect(page.publishes.some((publish) => publish.socket === socketB)).toBe(true);
    expect(
      page.decoded.some((frame) => frame.socket === socketB && frame.type === "hello"),
    ).toBe(true);

    // The loser is closed by the page; the winner is the only socket left open.
    expect(socketA.readyState).toBe(3);
    expect(socketA.closeCalls).toBeGreaterThanOrEqual(1);
    expect(page.openSockets().map((socket) => socket.url)).toEqual([BROKER_B]);

    // Only the first CONNACK announces "verifying"; B's win is the online report.
    expect(page.states.filter((state) => state.state === "verifying")).toHaveLength(1);

    // The false-online failure mode this test exists for: broker A must never
    // have been presented as online, and nothing was online before an ack.
    const onlineOnA = page.states.filter(
      (state) => state.state === "online" && state.broker === BROKER_A,
    );
    expect(onlineOnA).toEqual([]);
    const firstAckIndex = page.events.indexOf("hello_ack");
    const firstOnlineIndex = page.events.indexOf("state:online");
    expect(firstAckIndex).toBeGreaterThanOrEqual(0);
    expect(firstOnlineIndex).toBeGreaterThan(firstAckIndex);
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 4: unanswered app pings take the tunnel offline and a fresh race brings it back", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A, BROKER_B],
      behaviors: {
        [BROKER_A]: { connack: "accept", hello: "ack", pong: true },
        [BROKER_B]: { connack: "accept", hello: "ack", pong: true },
      },
    }).boot();

    page.acceptAll();
    await page.waitFor(() => page.remote.online === true, "the race to produce a live tunnel");
    const firstBroker = String(page.pageState()?.broker);
    expect([BROKER_A, BROKER_B]).toContain(firstBroker);
    // Exactly one attempt survived the race.
    expect(page.openSockets()).toHaveLength(1);

    // A live computer answers the app-level presence ping.  Prove that cycle
    // once *before* breaking it: the later loss is then unambiguously "pongs
    // stopped", not "the ping path never worked".
    await page.advance(10_000);
    await page.waitFor(
      () =>
        page.decoded.some((frame) => frame.type === "ping") &&
        page.remote.tunnel.pongOutstanding === false,
      "the first presence ping to be answered",
    );

    // The computer stops answering app-level pings (sleep, cell handover, or
    // the desktop tunnel moving to another broker).  The MQTT socket itself
    // stays perfectly healthy: only the pong silence says the computer is gone.
    page.setBehavior(firstBroker, { pong: false });
    const pongsStoppedAt = page.now();

    // The next interval's ping is the first one that goes unanswered; an app
    // ping is on the wire, so the presence mechanism itself is exercised.
    await page.advance(10_250);
    await page.waitFor(
      () => page.decoded.filter((frame) => frame.type === "ping").length >= 2,
      "the first unanswered presence ping",
    );

    const lossMs = await page.advanceUntil(
      () => page.remote.online === false && page.pageState()?.state !== "online",
      30_000,
      "the presence watchdog to take the tunnel offline",
    );
    // One lost pong is not enough; the second consecutive miss is.  The
    // presence interval is 5 s, so the loss lands two intervals after the
    // computer stopped answering -- roughly 10-20 s depending on where the
    // stop falls between ticks (the old 10 s interval made this 20-30 s, which
    // is a visible half minute of "connecting" on every broker hop).
    const silentMs = page.now() - pongsStoppedAt;
    expect(silentMs).toBeGreaterThanOrEqual(5_000);
    expect(silentMs).toBeLessThanOrEqual(20_000);
    expect(lossMs).toBeLessThanOrEqual(20_000);
    expect(page.decoded.filter((frame) => frame.type === "ping").length).toBeGreaterThanOrEqual(2);

    // It left online honestly: the flag and the state both stopped claiming it.
    expect(page.remote.online).toBe(false);
    const afterLoss = page.pageState();
    expect(afterLoss?.state).not.toBe("online");
    expect(["retrying", "connecting"]).toContain(String(afterLoss?.state));

    // ...and a fresh race starts: every configured broker is dialled again (not
    // just the remembered one), and the first hello_ack brings the page online.
    const dialsBefore = page.dials.length;
    await page.advanceUntil(
      () => page.dials.length >= dialsBefore + page.brokers.length,
      5_000,
      "the reconnect race to dial every broker again",
    );
    expect(page.dials.slice(dialsBefore).sort()).toEqual([...page.brokers].sort());
    page.acceptAll();
    await page.waitFor(() => page.remote.online === true, "the reconnect race to come online");
    expect(page.pageState()?.state).toBe("online");
    expect([BROKER_A, BROKER_B]).toContain(String(page.pageState()?.broker));
    // One winner, losers closed, again.
    expect(page.openSockets()).toHaveLength(1);

    // State timeline proof: online -> retrying -> online, and the second online
    // is again preceded by a broker-delivered hello_ack.
    const onlineEvents = page.events
      .map((event, index) => ({ event, index }))
      .filter((entry) => entry.event === "state:online");
    expect(onlineEvents).toHaveLength(2);
    const retryIndex = page.events.indexOf("state:retrying");
    expect(retryIndex).toBeGreaterThan(onlineEvents[0].index);
    expect(onlineEvents[1].index).toBeGreaterThan(retryIndex);
    expect(page.events.slice(retryIndex, onlineEvents[1].index)).toContain("hello_ack");
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 5: a hello_ack on the first broker clears the hello deadline instead of being pre-empted", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A],
      behaviors: { [BROKER_A]: { connack: "accept", hello: "ack", pong: true } },
    }).boot();

    page.acceptAll();
    await page.waitFor(() => page.remote.online === true, "the tunnel to come online");
    expect(page.pageState()?.broker).toBe(BROKER_A);

    // The normal single-broker success path: the ack was a round trip, not a
    // timeout, so the hello deadline must have been cleared.  Advancing far
    // past it later must not rotate, retry or dial anything again.
    await page.advance(15_000);

    // Presence kept running in that window: let the pings reach the broker.
    await page.waitFor(
      () => page.decoded.filter((frame) => frame.type === "ping").length >= 1,
      "a presence ping while still online",
    );

    expect(page.remote.online).toBe(true);
    expect(page.isOnline()).toBe(true);
    expect(page.pageState()?.broker).toBe(BROKER_A);
    expect(page.dials).toEqual([BROKER_A]);
    expect(page.sockets.length).toBe(1);
    expect(page.connacks).toEqual([BROKER_A]);
    expect(page.states.some((state) => state.state === "retrying")).toBe(false);
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 6: the fastest hello_ack wins the race without waiting out the silent broker's timeout", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A, BROKER_B],
      behaviors: {
        [BROKER_A]: { connack: "accept", hello: "ignore" },
        [BROKER_B]: { connack: "accept", hello: "ack" },
      },
    }).boot();
    expect(page.dials).toEqual([BROKER_A, BROKER_B]);

    // Both attempts are accepted in the same tick: the race is fully in flight
    // from t=0, and the fake clock is never advanced.
    page.acceptAll();
    await page.waitFor(
      () => page.remote.online === true,
      "the fastest broker's hello_ack to win the race",
    );

    // HELLO_TIMEOUT_MS has NOT elapsed — no fake time was advanced at all — so
    // the adoption cannot be the silent broker's deadline rotating to B.
    expect(page.now()).toBe(0);
    expect(page.pageState()?.state).toBe("online");
    expect(page.pageState()?.broker).toBe(BROKER_B);
    expect(page.remote.online).toBe(true);

    // B won; A was closed.  Both accepted brokers were asked for the computer,
    // but only B answered.
    const socketA = page.socketsFor(BROKER_A)[0];
    const socketB = page.socketsFor(BROKER_B)[0];
    expect(socketA.readyState).toBe(3);
    expect(socketA.closeCalls).toBeGreaterThanOrEqual(1);
    expect(page.openSockets().map((socket) => socket.url)).toEqual([BROKER_B]);
    expect(
      page.decoded.filter((frame) => frame.socket === socketA && frame.type === "hello"),
    ).toHaveLength(1);
    expect(
      page.decoded.filter((frame) => frame.socket === socketB && frame.type === "hello"),
    ).toHaveLength(1);
    expectNoOnlineBeforeHelloAck(page);
  });

  it("case 7: when every broker accepts but none produces the computer, the race ends in retrying('computer')", async () => {
    const page = await new RelayPage({
      brokers: [BROKER_A, BROKER_B],
      behaviors: {
        [BROKER_A]: { connack: "accept", hello: "ignore" },
        [BROKER_B]: { connack: "accept", hello: "ignore" },
      },
    }).boot();
    expect(page.dials).toEqual([BROKER_A, BROKER_B]);

    // Every attempt answers CONNECT, so the race is a page-alone-on-the-broker
    // world: the hello deadline is armed once none is still connecting.
    page.acceptAll();
    await page.waitFor(
      () =>
        page.connacks.length === 2 &&
        page.decoded.filter((frame) => frame.type === "hello").length === 2,
      "both brokers to accept the phone and receive its hello",
    );
    expect(page.remote.online).toBe(false);

    const elapsed = await page.advanceUntil(
      () => page.states.some((state) => state.state === "retrying"),
      8_000,
      "the all-silent race to give up and retry",
    );
    // HELLO_TIMEOUT_MS is 6000: the race must give up around then, not instantly
    // and not after a full connect timeout.
    expect(elapsed).toBeGreaterThanOrEqual(5_000);
    expect(elapsed).toBeLessThanOrEqual(8_000);
    const retrying = page.states.find((state) => state.state === "retrying");
    expect(retrying?.reason).toBe("computer");

    // It never claimed to be online, and every probe socket is closed.
    expect(page.remote.online).toBe(false);
    expect(page.states.filter((state) => state.state === "online")).toEqual([]);
    expect(page.openSockets()).toEqual([]);
    expectNoOnlineBeforeHelloAck(page);
  });
});
