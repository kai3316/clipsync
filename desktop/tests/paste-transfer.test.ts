import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bridge } from "../src/api/bridge";
import type { SidecarEvent, TransfersPage } from "../src/api/types";
import {
  errorCode, errorMessage, followPasteFetch, isPasteConfirmRequired, isRemoteFileRow,
  pasteFailureText, pastePrepareErrorText, pasteProgressFrom, pasteReadyEntry, pasteReadyPaths,
} from "../src/lib/paste-transfer";
import { setLocale } from "../src/i18n";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    transfers: vi.fn(),
    onSidecarEvent: vi.fn(),
    cancelPasteEntry: vi.fn(),
  },
}));

function transfer(overrides: Record<string, unknown> = {}) {
  return {
    id: "t1", filename: "a.bin", size: 1000, direction: "down", status: "running",
    progress: 0, speed: 0, eta: "", paste_entry: "entry-1",
    ...overrides,
  };
}

function page(active: Array<Record<string, unknown>> = [], history: Array<Record<string, unknown>> = []): TransfersPage {
  return { active: active as never[], history: history as never[], speed_test: {} };
}

function event(name: string, data: Record<string, unknown>): SidecarEvent {
  return { type: "event", name, session_id: "s", data };
}

describe("remote-file paste helpers", () => {
  beforeEach(() => { setLocale("zh-CN"); });
  afterEach(() => { setLocale("zh-CN"); });

  it("recognises a file that lives on another device", () => {
    // The kind is the row's best label, the offer id is the proof: a copy that
    // carries the file's name as text *and* the offer is labelled TEXT and can
    // still be fetched.
    expect(isRemoteFileRow({ content_type: "FILE_REMOTE" })).toBe(true);
    expect(isRemoteFileRow({ content_type: "TEXT", offer_entry: "peer-7" })).toBe(true);
    expect(isRemoteFileRow({ content_type: "file_remote" })).toBe(true);
    expect(isRemoteFileRow({ content_type: "TEXT" })).toBe(false);
    expect(isRemoteFileRow({})).toBe(false);
  });

  it("collapses the transfer rows of one paste into one size-weighted reading", () => {
    const reading = pasteProgressFrom(page([
      transfer({ id: "a", filename: "a.bin", size: 300, progress: 50, speed: 100, eta: "2 分钟" }),
      transfer({ id: "b", filename: "b.bin", size: 100, progress: 100, speed: 50 }),
      transfer({ id: "other", filename: "other.bin", paste_entry: "entry-2" }),
    ]), "entry-1");
    expect(reading).not.toBeNull();
    // The row other transfers do not count towards this paste.
    expect(reading!.files).toBe(2);
    expect(reading!.filename).toBe("a.bin");
    // (50×300 + 100×100) / 400
    expect(reading!.progress).toBeCloseTo(62.5, 5);
    expect(reading!.speed).toBe(150);
    expect(reading!.eta).toBe("2 分钟");
    expect(reading!.size).toBe(400);
  });

  it("keeps reading rows that have already moved into the history list", () => {
    // The last file of a paste leaves the active list a moment before
    // `paste_ready`; a panel that dropped to 0 % in that gap would look like a
    // restart.
    const reading = pasteProgressFrom(page([], [transfer({ progress: 100 })]), "entry-1");
    expect(reading!.progress).toBe(100);
  });

  it("answers nothing for another paste's rows or an empty page", () => {
    expect(pasteProgressFrom(page([transfer({ paste_entry: "entry-2" })]), "entry-1")).toBeNull();
    expect(pasteProgressFrom(page(), "entry-1")).toBeNull();
    expect(pasteProgressFrom(null, "entry-1")).toBeNull();
  });

  it("reads the paste events and ignores everything else", () => {
    const ready = event("clip_files.paste_ready", { entry_id: "entry-1", paths: ["C:/a.bin"] });
    expect(pasteReadyEntry(ready)).toBe("entry-1");
    expect(pasteReadyPaths(ready)).toEqual(["C:/a.bin"]);

    const failed = event("clip_files.paste_failed", { entry_id: "entry-1", reason: "timeout" });
    expect(pasteReadyEntry(failed)).toBe("");
    expect(pasteReadyPaths(failed)).toEqual([]);

    expect(pasteReadyEntry(event("history.changed", {}))).toBe("");
    expect(pasteReadyEntry({ type: "event" } as SidecarEvent)).toBe("");
    expect(pasteReadyEntry(null)).toBe("");
  });

  it("names every failure reason the sidecar can send", () => {
    expect(pasteFailureText("cancelled")).toBe("已取消取回");
    expect(pasteFailureText("timeout")).toBe("等待传输超时，请重试");
    expect(pasteFailureText("too_large")).toContain("大小上限");
    expect(pasteFailureText("relay_too_large")).toContain("中继");
    expect(pasteFailureText("not_available")).toContain("无法从另一台设备取回");
    expect(pasteFailureText("clipboard_write_failed")).toContain("写入剪贴板失败");
    // The one that lands after the bytes are here: the message says the paste
    // can still be done, because it can.
    expect(pasteFailureText("history_update_failed")).toContain("可以直接粘贴");
    expect(pasteFailureText("")).toBe("取回失败，请重试");
    expect(pasteFailureText("peer_rejected")).toContain("peer_rejected");
  });

  it("names every refusal the sidecar can answer a start with", () => {
    expect(pastePrepareErrorText("PASTE_TOO_LARGE")).toContain("大小上限");
    expect(pastePrepareErrorText("PASTE_RELAY_TOO_LARGE")).toContain("中继");
    expect(pastePrepareErrorText("PASTE_NOT_AVAILABLE")).toContain("无法从另一台设备取回");
    expect(pastePrepareErrorText("NOT_CONNECTED")).toContain("不在线");
    expect(pastePrepareErrorText("NOT_PAIRED")).toContain("配对已失效");
    // The confirm question is the one code the caller handles itself.
    expect(pastePrepareErrorText("PASTE_CONFIRM_REQUIRED")).toContain("文件较大");
    expect(isPasteConfirmRequired({ code: "PASTE_CONFIRM_REQUIRED" })).toBe(true);
    expect(isPasteConfirmRequired({ code: "PASTE_TOO_LARGE" })).toBe(false);
    expect(isPasteConfirmRequired(null)).toBe(false);
    expect(pastePrepareErrorText("SOMETHING", "the host's words")).toBe("the host's words");
    expect(errorCode({ code: "NOT_CONNECTED" })).toBe("NOT_CONNECTED");
    expect(errorMessage({ message: "boom" })).toBe("boom");
    expect(errorCode(null)).toBe("");
  });
});

describe("following a paste fetch", () => {
  let handler: ((event: SidecarEvent) => void) | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    handler = undefined;
    vi.mocked(bridge.transfers).mockReset().mockResolvedValue(page([transfer()]));
    vi.mocked(bridge.onSidecarEvent).mockReset().mockImplementation(async (next) => {
      handler = next;
      return () => { handler = undefined; };
    });
    vi.mocked(bridge.cancelPasteEntry).mockReset().mockResolvedValue({ cancelled: 1 });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Give the registration and the first poll their microtasks. */
  async function settle() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  }

  it("polls the transfer list and reports the reading", async () => {
    const onProgress = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress, onReady: vi.fn(), onFailed: vi.fn() });
    try {
      await settle();
      expect(bridge.transfers).toHaveBeenCalled();
      expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({ entryId: "entry-1", files: 1 }));
      // Four reads a second: a small file's bar still moves, and the local pipe
      // is not a busy loop.
      vi.advanceTimersByTime(400);
      await settle();
      expect(bridge.transfers).toHaveBeenCalledTimes(2);
    } finally { run.stop(); }
  });

  it("ends on the ready event, and stops reading the list", async () => {
    const onProgress = vi.fn();
    const onReady = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress, onReady, onFailed: vi.fn() });
    try {
      await settle();
      const reads = vi.mocked(bridge.transfers).mock.calls.length;
      handler?.(event("clip_files.paste_ready", { entry_id: "entry-1", paths: ["C:/a.bin"] }));
      expect(onReady).toHaveBeenCalledWith(["C:/a.bin"]);
      vi.advanceTimersByTime(1000);
      await settle();
      // The outcome is the event's, not a poll's: nothing reads on afterwards.
      expect(vi.mocked(bridge.transfers).mock.calls.length).toBe(reads);
    } finally { run.stop(); }
  });

  it("ends on the failed event with the sidecar's reason", async () => {
    const onFailed = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress: vi.fn(), onReady: vi.fn(), onFailed });
    try {
      await settle();
      handler?.(event("clip_files.paste_failed", { entry_id: "entry-1", reason: "timeout" }));
      expect(onFailed).toHaveBeenCalledWith("timeout");
    } finally { run.stop(); }
  });

  it("ignores events about another paste", async () => {
    const onReady = vi.fn();
    const onFailed = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress: vi.fn(), onReady, onFailed });
    try {
      await settle();
      handler?.(event("clip_files.paste_ready", { entry_id: "entry-2", paths: [] }));
      handler?.(event("clip_files.paste_failed", { entry_id: "entry-2", reason: "cancelled" }));
      expect(onReady).not.toHaveBeenCalled();
      expect(onFailed).not.toHaveBeenCalled();
    } finally { run.stop(); }
  });

  it("falls back to the event's fraction when the transfer list cannot be read", async () => {
    // The overlay polls through a capability the host grants it; a window that
    // cannot read the list still has the forwarded progress events, and a bar
    // that never moves because one read is unavailable is worse than none.
    vi.mocked(bridge.transfers).mockRejectedValue({ code: "PERMISSION_DENIED" });
    const onProgress = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress, onReady: vi.fn(), onFailed: vi.fn() });
    try {
      await settle();
      handler?.(event("transfer.progress", { paste_entry: "entry-1", progress: 0.25 }));
      vi.advanceTimersByTime(400);
      await settle();
      expect(onProgress).toHaveBeenLastCalledWith(expect.objectContaining({ progress: 25 }));
    } finally { run.stop(); }
  });

  it("cancel abandons the fetch on the runtime side and reads nothing more", async () => {
    const onProgress = vi.fn();
    const run = followPasteFetch("entry-1", { onProgress, onReady: vi.fn(), onFailed: vi.fn() });
    await settle();
    await run.cancel();
    expect(bridge.cancelPasteEntry).toHaveBeenCalledWith("entry-1");
    const reads = vi.mocked(bridge.transfers).mock.calls.length;
    vi.advanceTimersByTime(1000);
    await settle();
    expect(vi.mocked(bridge.transfers).mock.calls.length).toBe(reads);
  });
});
