/**
 * Phase-0 wire vectors for the companion tunnel, from the PWA's side.
 *
 * The phone is a browser, so its key must come from WebCrypto and its sealed
 * frames must be openable by the Python side.  These vectors pin the shared
 * derivation (salt/info/hash/output length), the envelope bytes and the
 * AES-256-GCM body shape; they mirror tests/test_companion_tunnel_wire.py.
 */

import { describe, expect, it } from "vitest";

const { subtle } = globalThis.crypto;
const encoder = new TextEncoder();

const TOKEN = "test-token-0123456789abcdef";
const KEY_HEX = "6670f02c7fb014934ef0f673dbcea400cc852ae0529ca0af283e931d60b941b4";
const PYTHON_ENVELOPE_B64 =
  "eyJ2IjoxLCJ0cyI6MTAwMDAwMC4wLCJkYXRhIjoiQVVOQ1JmbG10aEpPdGxJMzNlVjkySWh3YlF5aDNaNWI3K1A1" +
  "c2dwblcvMWY0ZDM3cTZvRzV3UnRwNzRxY29XaHZNRCtudTg9IiwibWFjIjoiOTY0OWEyZjE2MGMyMDA3NmFiNjUx" +
  "YTI4NzJlMzU4OGZkNjgzMTBkNjIwYzlhZWQ2MjVlZmNiZmJiMTM5YWQ4ZCJ9";
const JS_SEALED_B64 = "AUNCRQABAgMEBQYHCAkKC4KLolLmASD0AD93nGqGrT8j8G9gfj0gXwEBQCfnE9nY/Yiw3+g=";
const JS_PLAIN_B64 = "eyJ2IjoxLCJ0eXBlIjoicG9uZyJ9";

function b64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

function toB64(bytes: Uint8Array): string {
  let text = "";
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

function hex(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return Array.from(view)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function channelKey(): Promise<CryptoKey> {
  const ikm = await subtle.importKey("raw", encoder.encode(TOKEN), { name: "HKDF" }, false, [
    "deriveBits",
  ]);
  const bits = await subtle.deriveBits(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: encoder.encode("clipsync-companion-v1"),
      info: encoder.encode("channel-key"),
    },
    ikm,
    256,
  );
  return subtle.importKey("raw", bits, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

describe("companion tunnel wire vectors", () => {
  it("derives the same HKDF key as the Python side", async () => {
    const ikm = await subtle.importKey("raw", encoder.encode(TOKEN), { name: "HKDF" }, false, [
      "deriveBits",
    ]);
    const bits = await subtle.deriveBits(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt: encoder.encode("clipsync-companion-v1"),
        info: encoder.encode("channel-key"),
      },
      ikm,
      256,
    );
    expect(hex(bits)).toBe(KEY_HEX);
  });

  it("opens the envelope the desktop produced", async () => {
    const envelope = JSON.parse(new TextDecoder().decode(b64(PYTHON_ENVELOPE_B64)));
    const sealed = b64(envelope.data);
    expect(Array.from(sealed.slice(0, 4))).toEqual([1, 0x43, 0x42, 0x45]);
    const plain = await subtle.decrypt(
      { name: "AES-GCM", iv: sealed.slice(4, 16) },
      await channelKey(),
      sealed.slice(16),
    );
    expect(new TextDecoder().decode(plain)).toBe('{"v":1,"type":"ping"}');
  });

  it("produces the sealed body the Python side pins", async () => {
    const key = await channelKey();
    const iv = Uint8Array.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    const ct = new Uint8Array(
      await subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        b64(JS_PLAIN_B64) as unknown as BufferSource,
      ),
    );
    const sealed = new Uint8Array([0x01, 0x43, 0x42, 0x45, ...iv, ...ct]);
    expect(toB64(sealed)).toBe(JS_SEALED_B64);
  });
});
