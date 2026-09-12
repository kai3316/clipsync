import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bridge } from "../src/api/bridge";
import { t } from "../src/i18n";
import { createApplicationStore } from "../src/stores/application";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    status: vi.fn(), history: vi.fn(), devices: vi.fn(), subscribe: vi.fn(),
    unlock: vi.fn(), deleteHistory: vi.fn(), pinHistory: vi.fn(),
    startPairing: vi.fn(), confirmPairing: vi.fn(), rejectPairing: vi.fn(),
    unpairDevice: vi.fn(), setSyncEnabled: vi.fn(), copyHistory: vi.fn(),
    batchPinHistory: vi.fn(), batchDeleteHistory: vi.fn(),
    batchFavoriteHistory: vi.fn(), clearHistory: vi.fn(),
    connectDevice: vi.fn(), disconnectDevice: vi.fn(), forgetDevice: vi.fn(),
    restoreDevice: vi.fn(), purgeDevice: vi.fn(),
    testDevice: vi.fn(), deviceCerts: vi.fn(), retrustDevice: vi.fn(),
    openHistoryLink: vi.fn(),
    sendUrl: vi.fn(), pushText: vi.fn(), discoveryStatus: vi.fn(),
    setDiscoveryEnabled: vi.fn(), setDiscoveryVisible: vi.fn(),
    favorites: vi.fn(), favorite: vi.fn(),
    diagnosticsReport: vi.fn(), diagnosticsRequest: vi.fn(),
    updateCheck: vi.fn(), updateStatus: vi.fn(), updateDownload: vi.fn(),
    updateOpenFolder: vi.fn(), restartSidecar: vi.fn(), recoverDataDir: vi.fn(),
  },
}));

const status = {
  version: "test", health: "ready" as const, device_name: "Test", device_id: "a",
  language: "zh-CN", sync_state: "not_started", capabilities: [],
  session_id: "session", seq: 0,
};
const page = { session_id: "session", seq: 0, total: 0, offset: 0, items: [] };
const pairedDevice = { id: "peer", name: "Peer", paired: true, connection_state: "online",
  pairing_status: "paired", pairing_code: null, sas: null };

beforeEach(() => {
  vi.mocked(bridge.status).mockResolvedValue(status);
  vi.mocked(bridge.history).mockResolvedValue(page);
  vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
  vi.mocked(bridge.subscribe).mockResolvedValue(vi.fn());
});
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });

describe("desktop application store", () => {
  it("queues incoming transfer requests using the runtime event and filename", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "transfer.request", session_id: "session",
      data: { transfer_id: "incoming", filename: "report.pdf", size: 42 } });
    expect(store.state.notices).toEqual([
      expect.objectContaining({ title: "transfer.request", message: "report.pdf" }),
    ]);
    emit({ type: "event", name: "transfer.complete", session_id: "session",
      data: { transfer_id: "outgoing", success: true } });
    expect(store.state.notices).toHaveLength(1);
    store.dispose();
  });

  it("announces a relaunch without offering a retry the host is already doing", async () => {
    const store = createApplicationStore();
    await store.start();
    const state = vi.mocked(bridge.subscribe).mock.calls[0][1];
    // The snapshot marker is written onto whatever `state.status` holds, so
    // give this test its own copy instead of the module-level fixture.
    store.state.status = { ...status };
    store.state.devices = [pairedDevice];
    store.state.total = 7;
    state({ state: "restarting", attempt: 2, error: "SIDECAR_UNAVAILABLE" });
    expect(store.state.error).toEqual({
      code: "SIDECAR_RESTARTING",
      message: "后台进程已退出，正在重新启动（第 2 次）…",
      retryable: false,
    });
    // The snapshot came from the process that just died, so it is dropped
    // rather than left on screen looking live.
    expect(store.state.devices).toEqual([]);
    expect(store.state.total).toBe(0);
    store.dispose();
  });

  it("turns an exhausted relaunch into a retry that relaunches the sidecar", async () => {
    const store = createApplicationStore();
    await store.start();
    const state = vi.mocked(bridge.subscribe).mock.calls[0][1];
    store.state.status = { ...status };
    state({ state: "failed", error: "SIDECAR_UNAVAILABLE" });
    expect(store.state.error).toEqual({
      code: "SIDECAR_UNAVAILABLE", message: "后台进程不可用，请重试", retryable: true,
    });
    vi.mocked(bridge.restartSidecar).mockRejectedValueOnce({
      code: "SIDECAR_START_FAILED", message: "Could not launch the Python sidecar",
      retryable: false,
    });
    await store.reconnect();
    expect(bridge.restartSidecar).toHaveBeenCalledExactlyOnceWith();
    expect(store.state.error?.code).toBe("SIDECAR_START_FAILED");
    // A relaunch that works refreshes the snapshot it dropped.
    vi.mocked(bridge.restartSidecar).mockClear();
    vi.mocked(bridge.restartSidecar).mockResolvedValueOnce(undefined);
    vi.mocked(bridge.status).mockClear();
    await store.reconnect();
    expect(bridge.restartSidecar).toHaveBeenCalledExactlyOnceWith();
    expect(bridge.status).toHaveBeenCalled();
    expect(store.state.error).toBeNull();
    store.dispose();
  });

  it("refreshes a dead transport without relaunching a sidecar that is alive", async () => {
    const store = createApplicationStore();
    await store.start();
    store.state.error = { code: "REQUEST_TIMEOUT", message: "Result unknown", retryable: true };
    vi.mocked(bridge.status).mockClear();
    await store.reconnect();
    expect(bridge.restartSidecar).not.toHaveBeenCalled();
    expect(bridge.status).toHaveBeenCalled();
    store.dispose();
  });

  it("offers a retry even when the host called its own failure final", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.status).mockRejectedValueOnce({
      code: "SIDECAR_START_FAILED", message: "Could not launch the Python sidecar",
      retryable: false,
    });
    await store.refresh();
    // The host can relaunch the sidecar on demand, so a failure it reported as
    // non-retryable still gets the band's button.
    expect(store.state.error).toEqual({
      code: "SIDECAR_START_FAILED", message: "Could not launch the Python sidecar",
      retryable: true,
    });
    store.dispose();
  });

  it("recovers the snapshot when the relaunched sidecar reports ready", async () => {
    const store = createApplicationStore();
    await store.start();
    const state = vi.mocked(bridge.subscribe).mock.calls[0][1];
    store.state.status = { ...status };
    state({ state: "failed", error: "SIDECAR_UNAVAILABLE" });
    vi.mocked(bridge.status).mockClear();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [pairedDevice] });
    state({ state: "ready" });
    expect(store.state.error).toBeNull();
    await vi.waitFor(() => expect(store.state.devices).toEqual([pairedDevice]));
    store.dispose();
  });

  it("names the files a repair moved aside so they can be restored by hand", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.recoverDataDir).mockResolvedValueOnce({
      items: [
        { artifact: "config", reason: "unreadable", files: ["config.json.corrupt-20260912_101500"] },
        { artifact: "history", reason: "history_corrupt", files: ["clipboard_history.db.corrupt-20260912_101500"] },
      ],
    });
    expect(await store.recoverData()).toBe(true);
    expect(store.state.notices).toEqual([
      expect.objectContaining({
        title: "数据修复",
        message: "已将损坏的文件移到一旁：config.json.corrupt-20260912_101500, " +
          "clipboard_history.db.corrupt-20260912_101500",
      }),
    ]);
    // The repair replaces the data directory, so the snapshot is re-read.
    expect(bridge.status).toHaveBeenCalled();
    expect(store.state.error).toBeNull();
    store.dispose();
  });

  it("stays quiet when a repair finds nothing to move", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.recoverDataDir).mockResolvedValueOnce({ items: [] });
    expect(await store.recoverData()).toBe(true);
    expect(store.state.notices).toEqual([]);
    store.dispose();
  });

  it("reports a repair that could not run instead of pretending it worked", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.recoverDataDir).mockRejectedValueOnce({
      code: "RECOVERY_FAILED", message: "Could not repair the data directory", retryable: false,
    });
    expect(await store.recoverData()).toBe(false);
    expect(store.state.error).toEqual({
      code: "RECOVERY_FAILED", message: "Could not repair the data directory", retryable: false,
    });
    store.dispose();
  });

  it("expires each notice independently and clears timers on disposal", async () => {
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "runtime.error", session_id: "session",
      data: { message: "first" } });
    await vi.advanceTimersByTimeAsync(3000);
    emit({ type: "event", name: "runtime.error", session_id: "session",
      data: { message: "second" } });
    await vi.advanceTimersByTimeAsync(3000);
    expect(store.state.notices.map(item => item.message)).toEqual(["second"]);
    store.dispose();
    expect(store.state.notices).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });
  it("coalesces a sustained event burst into one snapshot fetch", async () => {
    // A bulk operation (a large import, a paste storm) emits far faster than the
    // renderer can re-read the snapshot. If every event fetched its own snapshot
    // the window would fall further behind the faster the sidecar worked.
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    const before = vi.mocked(bridge.status).mock.calls.length;
    for (let seq = 1; seq <= 500; seq += 1) {
      emit({ type: "event", name: "history.changed", session_id: "session", seq });
    }
    await vi.advanceTimersByTimeAsync(60);
    expect(vi.mocked(bridge.status).mock.calls.length).toBe(before + 1);
    store.dispose();
  });

  it("keeps the notice list and its timers bounded through an event storm", async () => {
    // Notices are the other unbounded surface: one per event, each with a
    // dismissal timer, would grow without limit in a slow renderer.
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    for (let index = 0; index < 500; index += 1) {
      emit({ type: "event", name: "device.connected", session_id: "session",
        data: { device_id: `peer-${index}`, name: `Peer ${index}` } });
    }
    expect(store.state.notices).toHaveLength(5);
    // Five dismissal timers plus the one pending refresh — not 500.
    expect(vi.getTimerCount()).toBeLessThanOrEqual(6);
    store.dispose();
  });

  it.each(["favorites.changed", "data.changed"])("routes %s through the authoritative favorites snapshot", async (name) => {
    vi.useFakeTimers();
    vi.mocked(bridge.status).mockResolvedValue({ ...status, capabilities: ["favorites.list"] });
    vi.mocked(bridge.favorites).mockResolvedValue({
      items: [], total: 0, offset: 0, groups: [], session_id: "session", seq: 0,
    });
    const store = createApplicationStore();
    await store.start();
    expect(bridge.favorites).toHaveBeenCalledOnce();
    vi.mocked(bridge.subscribe).mock.calls[0][0]({
      type: "event", name, session_id: "session", seq: 1,
    });
    await vi.advanceTimersByTimeAsync(60);
    expect(bridge.favorites).toHaveBeenCalledTimes(2);
    store.dispose();
    vi.mocked(bridge.subscribe).mock.calls[0][0]({
      type: "event", name, session_id: "session", seq: 2,
    });
    await vi.advanceTimersByTimeAsync(60);
    expect(bridge.favorites).toHaveBeenCalledTimes(2);
  });
  const items = ["a", "b"].map((id) => ({
    id, preview: id, timestamp: 0, content_type: "TEXT", pinned: false,
    source_name: "", source_app: "", source_title: "", paste_count: 0,
  }));
  async function selectedStore() {
    vi.mocked(bridge.history).mockResolvedValue({ ...page, total: 60, items });
    const store = createApplicationStore();
    await store.start();
    store.selectAllVisible(true);
    return store;
  }

  it("retains selection by ID across reloads and removes missing records", async () => {
    const store = await selectedStore();
    vi.mocked(bridge.history).mockResolvedValueOnce({ ...page, items: [...items].reverse() });
    await store.refreshHistory();
    expect(store.state.selectedIds).toEqual(["a", "b"]);
    vi.mocked(bridge.history).mockResolvedValueOnce({ ...page, items: [items[1]] });
    await store.refreshHistory();
    expect(store.state.selectedIds).toEqual(["b"]);
    store.dispose();
  });

  it("clears selection on query/page change and rejects old confirmation IDs", async () => {
    const store = await selectedStore();
    store.search("new");
    expect(store.state.selectedIds).toEqual([]);
    await Promise.resolve();
    expect(await store.batchDelete(["a", "b"])).toBe(false);
    expect(bridge.batchDeleteHistory).not.toHaveBeenCalled();
    store.selectAllVisible(true);
    store.page(1);
    expect(store.state.selectedIds).toEqual([]);
    store.dispose();
  });

  it("does not restore selection from a stale query response", async () => {
    const store = await selectedStore();
    let resolve!: (value: typeof page) => void;
    vi.mocked(bridge.history).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const old = store.refreshHistory();
    store.search("new");
    await Promise.resolve();
    resolve(page);
    await old;
    expect(store.state.selectedIds).toEqual([]);
    expect(store.state.history).toEqual(items);
    store.dispose();
  });

  it("sends one batch per pin/unpin and keeps selection after reload", async () => {
    const store = await selectedStore();
    vi.mocked(bridge.batchPinHistory).mockResolvedValue({ updated: 2 });
    await store.batchPin(true);
    await store.batchPin(false);
    expect(vi.mocked(bridge.batchPinHistory).mock.calls).toEqual([
      [["a", "b"], true], [["a", "b"], false],
    ]);
    expect(store.state.selectedIds).toEqual(["a", "b"]);
    store.dispose();
  });

  it("adds the selection to favorites with one batch call and reports the count", async () => {
    const store = await selectedStore();
    vi.mocked(bridge.batchFavoriteHistory).mockResolvedValue({ added: 2, ids: ["f1", "f2"] });
    expect(await store.batchFavorite(["a", "a", "b"])).toBe(2);
    expect(bridge.batchFavoriteHistory).toHaveBeenCalledExactlyOnceWith(["a", "b"], "");
    store.dispose();
  });

  it("reports nothing added when the favorites batch is rejected", async () => {
    const store = await selectedStore();
    vi.mocked(bridge.batchFavoriteHistory).mockRejectedValueOnce({
      code: "STORAGE_ERROR", message: "Favorites storage is unavailable", retryable: true,
    });
    expect(await store.batchFavorite(["a", "b"])).toBe(0);
    expect(store.state.error?.code).toBe("STORAGE_ERROR");
    store.dispose();
  });

  it("clears every history entry, resetting the page and selection", async () => {
    const store = await selectedStore();
    store.page(1);
    await Promise.resolve();
    expect(store.state.offset).toBe(30);
    vi.mocked(bridge.clearHistory).mockResolvedValue({ cleared: 60 });
    expect(await store.clearHistory()).toBe(60);
    expect(bridge.clearHistory).toHaveBeenCalledExactlyOnceWith();
    expect(store.state.offset).toBe(0);
    expect(store.state.selectedIds).toEqual([]);
    store.dispose();
  });

  it("surfaces a failed clear without reporting a count", async () => {
    const store = await selectedStore();
    vi.mocked(bridge.clearHistory).mockRejectedValueOnce({
      code: "STORAGE_ERROR", message: "History storage is unavailable", retryable: true,
    });
    expect(await store.clearHistory()).toBe(0);
    expect(store.state.error?.code).toBe("STORAGE_ERROR");
    store.dispose();
  });

  it("blocks duplicate mutation and selection/navigation changes while pending", async () => {
    const store = await selectedStore();
    let resolve!: (value: { deleted: number }) => void;
    vi.mocked(bridge.batchDeleteHistory).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const deleting = store.batchDelete(["a", "a", "b"]);
    await store.batchDelete(["a"]);
    store.selectAllVisible(false);
    store.select("a", false);
    store.search("changed");
    store.page(1);
    expect(store.state.selectedIds).toEqual(["a", "b"]);
    expect(store.state.query).toBe("");
    expect(store.state.offset).toBe(0);
    expect(bridge.batchDeleteHistory).toHaveBeenCalledExactlyOnceWith(["a", "b"]);
    vi.mocked(bridge.history).mockResolvedValueOnce(page);
    resolve({ deleted: 2 });
    await deleting;
    expect(store.state.selectedIds).toEqual([]);
    store.dispose();
  });

  it("preserves batch errors across autonomous refresh without replay", async () => {
    vi.useFakeTimers();
    const store = await selectedStore();
    vi.mocked(bridge.batchDeleteHistory).mockRejectedValueOnce({
      code: "REQUEST_TIMEOUT", message: "Result unknown", retryable: false,
    });
    await store.batchDelete(["a", "b"]);
    vi.mocked(bridge.subscribe).mock.calls[0][0]({
      type: "event", name: "history.changed", session_id: "session",
    });
    await vi.advanceTimersByTimeAsync(60);
    expect(store.state.error?.code).toBe("REQUEST_TIMEOUT");
    expect(bridge.batchDeleteHistory).toHaveBeenCalledOnce();
    expect(store.state.pending).toBe(false);
    store.dispose();
  });

  it("limits selection to 100 unique nonempty IDs", async () => {
    const store = await selectedStore();
    store.state.history = [...Array.from({ length: 101 }, (_, i) => ({ ...items[0], id: String(i) })),
      items[0], items[0], { ...items[0], id: "" }];
    store.selectAllVisible(true);
    expect(store.state.selectedIds).toHaveLength(100);
    store.select("100", true);
    expect(store.state.selectedIds).toHaveLength(100);
    store.selectAllVisible(false);
    expect(store.state.selectedIds).toEqual([]);
    store.dispose();
  });
  it("keeps two-sided confirmation pending until an authoritative snapshot pairs it", async () => {
    const device = { id: "peer", name: "Peer", paired: false, connection_state: "online",
      pairing_status: "confirmed_waiting", pairing_code: "123456", sas: "ABCD" };
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [device] });
    vi.mocked(bridge.confirmPairing).mockResolvedValueOnce({ paired: false, status: "confirmed_waiting" });
    const store = createApplicationStore();
    await store.confirmPairing(device);
    expect(bridge.confirmPairing).toHaveBeenCalledWith("peer", "123456");
    expect(store.state.devices[0].paired).toBe(false);
    expect(store.state.error).toBeNull();
    store.dispose();
  });

  it("sends a URL to the chosen device and reports that the frame left", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.sendUrl).mockResolvedValueOnce({ sent: true, device_id: "peer" });
    expect(await store.sendUrl(pairedDevice, "https://example.com")).toBe(true);
    expect(bridge.sendUrl).toHaveBeenCalledExactlyOnceWith("peer", "https://example.com");
    store.dispose();
  });

  it("rejects an empty or oversized URL without invoking the host", async () => {
    const store = createApplicationStore();
    expect(await store.sendUrl(pairedDevice, "   ")).toBe(false);
    expect(await store.sendUrl(pairedDevice, `https://example.com/${"x".repeat(2048)}`)).toBe(false);
    expect(bridge.sendUrl).not.toHaveBeenCalled();
    expect(store.state.error?.code).toBe("INVALID_URL");
    store.dispose();
  });

  it("surfaces a rejected URL send without claiming success", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.sendUrl).mockRejectedValueOnce({
      code: "SEND_FAILED", message: "URL was not sent", retryable: true,
    });
    expect(await store.sendUrl(pairedDevice, "https://example.com")).toBe(false);
    expect(store.state.error?.code).toBe("SEND_FAILED");
    store.dispose();
  });

  it("pushes text through the host and reports whether it was broadcast", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.pushText).mockResolvedValueOnce({ ok: true, len: 5, sent: true });
    expect(await store.pushText("hello")).toEqual({ ok: true, len: 5, sent: true });
    expect(bridge.pushText).toHaveBeenCalledExactlyOnceWith("hello");
    vi.mocked(bridge.pushText).mockResolvedValueOnce({ ok: true, len: 7, sent: false });
    expect(await store.pushText("offline")).toEqual({ ok: true, len: 7, sent: false });
    store.dispose();
  });

  it("rejects empty or oversized pushed text without invoking the host", async () => {
    const store = createApplicationStore();
    expect(await store.pushText("   ")).toBeNull();
    expect(await store.pushText("x".repeat(100001))).toBeNull();
    expect(bridge.pushText).not.toHaveBeenCalled();
    expect(store.state.error?.code).toBe("INVALID_TEXT");
    store.dispose();
  });

  it("surfaces a refused clipboard push", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.pushText).mockRejectedValueOnce({
      code: "CLIPBOARD_WRITE_FAILED", message: "Could not write to the clipboard", retryable: true,
    });
    expect(await store.pushText("blocked")).toBeNull();
    expect(store.state.error?.code).toBe("CLIPBOARD_WRITE_FAILED");
    store.dispose();
  });

  it("requires a ready host and discovery capability before toggling discovery", async () => {
    const store = createApplicationStore();
    expect(await store.discovery()).toBeNull();
    expect(await store.setDiscoveryEnabled(false)).toBeNull();
    expect(bridge.discoveryStatus).not.toHaveBeenCalled();
    expect(bridge.setDiscoveryEnabled).not.toHaveBeenCalled();
    store.state.status = { ...status, capabilities: ["discovery.status"] };
    vi.mocked(bridge.discoveryStatus).mockResolvedValueOnce({ enabled: true, visible: true });
    vi.mocked(bridge.setDiscoveryVisible).mockResolvedValueOnce({ enabled: true, visible: false });
    expect(await store.discovery()).toEqual({ enabled: true, visible: true });
    expect(await store.setDiscoveryVisible(false)).toEqual({ enabled: true, visible: false });
    expect(bridge.setDiscoveryVisible).toHaveBeenCalledWith(false);
    store.dispose();
  });

  it("reports a failed discovery toggle without a stale state", async () => {
    const store = createApplicationStore();
    store.state.status = { ...status, capabilities: ["discovery.status"] };
    vi.mocked(bridge.setDiscoveryEnabled).mockRejectedValueOnce({
      code: "DISCOVERY_TOGGLE_FAILED", message: "Could not change the LAN setting", retryable: true,
    });
    expect(await store.setDiscoveryEnabled(false)).toBeNull();
    expect(store.state.error?.code).toBe("DISCOVERY_TOGGLE_FAILED");
    store.dispose();
  });

  it("returns the diagnostics report and forwards repair actions verbatim", async () => {
    const store = createApplicationStore();
    const report = { v2: true, summary: "warn" as const, checks: [], groups: {},
      discovery_running: false, server_running: true, connected_count: 0, paired_count: 0,
      web_companion_running: false, web_port: 8080, lan_ip: "192.168.1.5", os: "Windows",
      version: "1.0.0" };
    vi.mocked(bridge.diagnosticsReport).mockResolvedValueOnce(report);
    expect(await store.diagnostics()).toEqual(report);
    expect(bridge.diagnosticsReport).toHaveBeenCalledExactlyOnceWith();
    vi.mocked(bridge.diagnosticsRequest).mockResolvedValueOnce({ ok: true });
    expect(await store.repairDiagnostics("firewall")).toEqual({ ok: true });
    expect(bridge.diagnosticsRequest).toHaveBeenCalledExactlyOnceWith("firewall");
    store.dispose();
  });

  it("surfaces diagnostics failures without a stale report", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.diagnosticsReport).mockRejectedValueOnce({
      code: "APP_LOCKED", message: "Unlock ClipSync to run diagnostics", retryable: false,
    });
    expect(await store.diagnostics()).toBeNull();
    expect(store.state.error?.code).toBe("APP_LOCKED");
    vi.mocked(bridge.diagnosticsRequest).mockRejectedValueOnce({
      code: "REPAIR_FAILED", message: "Could not open the settings", retryable: false,
    });
    expect(await store.repairDiagnostics("local_network")).toBeNull();
    expect(store.state.error?.code).toBe("REPAIR_FAILED");
    store.dispose();
  });

  it("queues an inbound URL as a notice carrying the URL text", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "url.received", session_id: "session",
      data: { device_id: "peer", url: "https://example.com/page" } });
    expect(store.state.notices).toEqual([
      expect.objectContaining({ title: "url.received", message: "https://example.com/page" }),
    ]);
    store.dispose();
  });

  it("queues pairing and presence notices naming the peer", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "pairing.request", session_id: "session",
      data: { device_id: "peer", name: "Pixel", code: "12345678" } });
    emit({ type: "event", name: "device.connected", session_id: "session",
      data: { device_id: "peer", name: "Pixel" } });
    emit({ type: "event", name: "device.disconnected", session_id: "session",
      data: { device_id: "peer", name: "Pixel" } });
    expect(store.state.notices.map(item => item.message)).toEqual([
      t("{name} 请求配对 — 代码：{code}", { name: "Pixel", code: "12345678" }),
      t("{name} 已连接", { name: "Pixel" }),
      t("{name} 已断开", { name: "Pixel" }),
    ]);
    store.dispose();
  });

  it("opens a link clip through the sidecar, which reads the row itself", async () => {
    const store = createApplicationStore();
    await store.start();
    const reads = vi.mocked(bridge.history).mock.calls.length;
    const item = { id: "7", timestamp: 0, content_type: "URL", pinned: false,
      preview: "https://example.com",
      source_name: "", source_app: "", source_title: "", paste_count: 0 };
    vi.mocked(bridge.openHistoryLink).mockResolvedValue(
      { opened: true, url: "https://example.com" });
    expect(await store.openLink(item)).toEqual({ opened: true, url: "https://example.com" });
    // The window names the row; no URL crosses the IPC from here, which is what
    // keeps the sidecar the one deciding what may reach the browser.
    expect(bridge.openHistoryLink).toHaveBeenCalledWith("7");
    // An open is not a mutation, so the list is not re-read for it.
    expect(vi.mocked(bridge.history).mock.calls.length).toBe(reads);

    vi.mocked(bridge.openHistoryLink).mockRejectedValue(
      { code: "INVALID_URL", message: "not an openable web link", retryable: false });
    expect(await store.openLink(item)).toBeNull();
    expect(store.state.error?.code).toBe("INVALID_URL");
    store.dispose();
  });

  it("says why a connect click came to nothing", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "device.connection_rejected", session_id: "session",
      data: { device_id: "peer", name: "Pixel" } });
    // A peer that could not be dialed is named from the snapshot, and only then
    // by its short id — legacy fell back the same way.
    store.state.devices = [pairedDevice];
    emit({ type: "event", name: "device.connection_unreachable", session_id: "session",
      data: { device_id: "peer", name: "" } });
    emit({ type: "event", name: "device.connection_unreachable", session_id: "session",
      data: { device_id: "0123456789abcdef", name: "" } });
    expect(store.state.notices.map((notice) => notice.message)).toEqual([
      t("{name} 拒绝了连接 — 该设备可能已将你移除", { name: "Pixel" }),
      t("找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络", { name: "Peer" }),
      t("找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络", { name: "0123456789ab" }),
    ]);
    store.dispose();
  });

  it("reports a connect that had nowhere to dial through its own notice", async () => {
    const store = createApplicationStore();
    await store.start();
    vi.mocked(bridge.connectDevice).mockResolvedValue({ accepted: false });
    expect(await store.connect(pairedDevice)).toBe(true);
    // Not an error band: the route answers whether a dial *started*, and
    // "refresh and try again" is advice a device that is not advertising on the
    // network cannot follow — the runtime's event is what says so.
    expect(store.state.error).toBeNull();
    expect(bridge.connectDevice).toHaveBeenCalledWith("peer");
    store.dispose();
  });

  it("still reports an action the runtime refused without an event", async () => {
    const store = createApplicationStore();
    await store.start();
    // Only the connect route's false is spoken for by an event; a pairing that
    // could not be started has nothing but this band to say it failed.
    vi.mocked(bridge.startPairing).mockResolvedValue({ accepted: false });
    expect(await store.startPairing(pairedDevice)).toBe(false);
    expect(store.state.error?.code).toBe("ACTION_REJECTED");
    store.dispose();
  });

  it("tells the user when the filter stripped a clip before it left the device", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    // The sidecar sends no fields with this event: the point is only that a
    // sensitive value was replaced, and naming it would put it back on screen.
    emit({ type: "event", name: "sync.redacted", session_id: "session", data: {} });
    expect(store.state.notices).toEqual([
      expect.objectContaining({ title: "sync.redacted", message: t("敏感内容未同步") }),
    ]);
    store.dispose();
  });

  it("prompts on a changed certificate until the answer is applied", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "device.security_alert", session_id: "session",
      data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: true } });
    expect(store.state.certAlert).toEqual({ device_id: "peer", name: "Pixel", can_trust: true });

    // An answer the runtime refused leaves the prompt up: the user has to be
    // able to try again once the device has connected with its new certificate.
    vi.mocked(bridge.retrustDevice).mockRejectedValue(
      { code: "VALIDATION_ERROR", message: "connect again", retryable: false });
    expect(await store.retrust("peer")).toBe(false);
    expect(store.state.error?.code).toBe("VALIDATION_ERROR");
    expect(store.state.certAlert).not.toBeNull();

    vi.mocked(bridge.retrustDevice).mockResolvedValue({ trusted: true });
    expect(await store.retrust("peer")).toBe(true);
    expect(bridge.retrustDevice).toHaveBeenLastCalledWith("peer");
    expect(store.state.certAlert).toBeNull();
    store.dispose();
  });

  it("gives up on an unanswered certificate prompt and leaves a notice", async () => {
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "device.security_alert", session_id: "session",
      data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: true } });
    expect(store.state.certAlert).not.toBeNull();
    // Legacy waited two minutes for an answer, then changed nothing and said
    // why; the prompt is not a trap the user has to escape.
    await vi.advanceTimersByTimeAsync(119_000);
    expect(store.state.certAlert).not.toBeNull();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(store.state.certAlert).toBeNull();
    expect(bridge.retrustDevice).not.toHaveBeenCalled();
    expect(bridge.unpairDevice).not.toHaveBeenCalled();
    expect(store.state.notices.map(item => item.message)).toEqual([
      t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: "Pixel" }),
    ]);
    store.dispose();
  });

  it("stops the certificate clock once the prompt is answered", async () => {
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "device.security_alert", session_id: "session",
      data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: true } });
    vi.mocked(bridge.retrustDevice).mockResolvedValue({ trusted: true });
    expect(await store.retrust("peer")).toBe(true);
    await vi.advanceTimersByTimeAsync(120_000);
    // No leftover notice about a decision that was made.
    expect(store.state.notices).toEqual([]);
    store.dispose();
  });

  it("answers a certificate prompt the other way by unpairing the device", async () => {
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    emit({ type: "event", name: "device.security_alert", session_id: "session",
      data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: false } });
    vi.mocked(bridge.unpairDevice).mockResolvedValue({ accepted: true });
    expect(await store.keepUnpaired("peer")).toBe(true);
    expect(bridge.unpairDevice).toHaveBeenCalledWith("peer");
    expect(store.state.certAlert).toBeNull();
    store.dispose();
  });

  it("applies update events in place without triggering a global refresh", async () => {
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const event = vi.mocked(bridge.subscribe).mock.calls[0][0];
    const statusCalls = vi.mocked(bridge.status).mock.calls.length;
    event({ type: "event", name: "update.state", session_id: "session", seq: 1,
      data: { state: { phase: "downloading", fraction: 0.5, downloaded: 50, total: 100,
        error: "", version: "", path: "" } } });
    expect(store.state.update).toEqual({ phase: "downloading", fraction: 0.5,
      downloaded: 50, total: 100, error: "", version: "", path: "" });
    // Progress must not be reset by a snapshot reload.
    await vi.advanceTimersByTimeAsync(120);
    expect(bridge.status).toHaveBeenCalledTimes(statusCalls);
    event({ type: "event", name: "update.available", session_id: "session", seq: 2,
      data: { latest: "v2.0.0", current: "1.0.0", url: "https://example.com" } });
    expect(store.state.updateCheck).toEqual({ available: true, latest: "v2.0.0",
      current: "1.0.0", url: "https://example.com" });
    await vi.advanceTimersByTimeAsync(120);
    expect(bridge.status).toHaveBeenCalledTimes(statusCalls);
    store.dispose();
  });

  it("ignores a malformed update state payload", async () => {
    const store = createApplicationStore();
    await store.start();
    const event = vi.mocked(bridge.subscribe).mock.calls[0][0];
    event({ type: "event", name: "update.state", session_id: "session", seq: 1,
      data: { state: { fraction: 0.5 } } });
    expect(store.state.update.phase).toBe("idle");
    store.dispose();
  });

  it("hydrates the update phase only with the update capability", async () => {
    const store = createApplicationStore();
    expect(await store.loadUpdateStatus()).toBeNull();
    expect(bridge.updateStatus).not.toHaveBeenCalled();
    store.state.status = { ...status, capabilities: ["update.status"] };
    vi.mocked(bridge.updateStatus).mockResolvedValueOnce({ state: { phase: "ready",
      fraction: 1, downloaded: 10, total: 10, error: "", version: "v2.0.0", path: "C:/a.zip" } });
    await store.loadUpdateStatus();
    expect(store.state.update).toEqual({ phase: "ready", fraction: 1, downloaded: 10,
      total: 10, error: "", version: "v2.0.0", path: "C:/a.zip" });
    store.dispose();
  });

  it("keeps unknown and up-to-date update checks distinguishable", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.updateCheck).mockResolvedValueOnce({ available: false, latest: "",
      current: "1.0.0", url: "" });
    expect(await store.checkUpdate()).toEqual({ available: false, latest: "",
      current: "1.0.0", url: "" });
    // An empty answer is "unknown", not "up to date": the panel must not claim
    // the app is current when the lookup never reached GitHub.
    expect(store.state.updateCheck).toBeNull();
    vi.mocked(bridge.updateCheck).mockResolvedValueOnce({ available: true, latest: "v2.0.0",
      current: "1.0.0", url: "https://example.com" });
    await store.checkUpdate();
    expect(store.state.updateCheck?.available).toBe(true);
    vi.mocked(bridge.updateCheck).mockRejectedValueOnce({
      code: "SIDECAR_UNAVAILABLE", message: "no host", retryable: true,
    });
    expect(await store.checkUpdate()).toBeNull();
    expect(store.state.error?.code).toBe("SIDECAR_UNAVAILABLE");
    store.dispose();
  });

  it("forwards the download and reveal results verbatim", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.updateDownload).mockResolvedValueOnce({ ok: false, started: false,
      error: "update already in progress" });
    expect(await store.downloadUpdate()).toEqual({ ok: false, started: false,
      error: "update already in progress" });
    expect(bridge.updateDownload).toHaveBeenCalledExactlyOnceWith();
    vi.mocked(bridge.updateOpenFolder).mockResolvedValueOnce({ ok: false, error: "no ready update" });
    expect(await store.openUpdateFolder()).toEqual({ ok: false, error: "no ready update" });
    expect(bridge.updateOpenFolder).toHaveBeenCalledExactlyOnceWith();
    store.dispose();
  });

  it("requires a ready host and sync capability", async () => {
    const store = createApplicationStore();
    await store.setSyncEnabled(true);
    await store.refresh();
    await store.setSyncEnabled(true);
    expect(bridge.setSyncEnabled).not.toHaveBeenCalled();
    store.state.status = { ...status, capabilities: ["sync.set_enabled"] };
    vi.mocked(bridge.setSyncEnabled).mockResolvedValueOnce({ enabled: true });
    await store.setSyncEnabled(true);
    expect(bridge.setSyncEnabled).toHaveBeenCalledWith(true);
    store.dispose();
  });

  it("reports an unsuccessful copy without claiming clipboard success", async () => {
    vi.mocked(bridge.copyHistory).mockResolvedValueOnce({ copied: false });
    const store = createApplicationStore();
    await store.copy({
      id: "1", preview: "x", timestamp: 0, content_type: "TEXT", pinned: false,
      source_name: "", source_app: "", source_title: "", paste_count: 0,
    });
    expect(store.state.copiedId).toBeNull();
    expect(store.state.error?.code).toBe("ACTION_REJECTED");
    store.dispose();
  });

  it.each(["device.changed", "pairing.changed", "app.status.changed", "history.changed"])(
    "invalidates in-flight snapshots immediately on %s", async (name) => {
      vi.useFakeTimers();
      const store = createApplicationStore();
      await store.start();
      let resolve!: (value: { items: never[] }) => void;
      vi.mocked(bridge.devices).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
      const old = store.refresh();
      await Promise.resolve();
      const event = vi.mocked(bridge.subscribe).mock.calls[0][0];
      event({ type: "event", name, session_id: "session", seq: 1 });
      const count = vi.mocked(bridge.history).mock.calls.length;
      resolve({ items: [] });
      await old;
      expect(bridge.history).toHaveBeenCalledTimes(count);
      await vi.advanceTimersByTimeAsync(60);
      expect(bridge.history).toHaveBeenCalledTimes(count + 1);
      store.dispose();
      event({ type: "event", name, session_id: "session", seq: 2 });
      await vi.advanceTimersByTimeAsync(100);
      expect(bridge.history).toHaveBeenCalledTimes(count + 1);
    },
  );

  it("does not refresh after a mutation completes following disposal", async () => {
    let resolve!: (value: { accepted: boolean }) => void;
    vi.mocked(bridge.startPairing).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const store = createApplicationStore();
    const pending = store.startPairing({ id: "p", name: "P", paired: false,
      connection_state: "online", pairing_status: "", pairing_code: null, sas: null });
    store.dispose();
    resolve({ accepted: true });
    await pending;
    expect(bridge.status).not.toHaveBeenCalled();
  });
  it("subscribes before reading authoritative snapshots", async () => {
    const store = createApplicationStore();
    await store.start();
    expect(vi.mocked(bridge.subscribe).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(bridge.status).mock.invocationCallOrder[0]);
    expect(store.state.status?.health).toBe("ready");
    store.dispose();
  });

  it("cleans up a listener installed after unmount", async () => {
    const off = vi.fn();
    let resolve!: (value: () => void) => void;
    vi.mocked(bridge.subscribe).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const store = createApplicationStore();
    const starting = store.start();
    store.dispose();
    resolve(off);
    await starting;
    expect(off).toHaveBeenCalledOnce();
    expect(bridge.status).not.toHaveBeenCalled();
  });

  it("does not allow stale search results to replace the newest query", async () => {
    let resolve!: (value: typeof page) => void;
    vi.mocked(bridge.history).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const store = createApplicationStore();
    const older = store.refreshHistory();
    await store.refreshHistory();
    resolve({ ...page, total: 99 });
    await older;
    expect(store.state.total).toBe(0);
  });

  it("does not query encrypted history while locked", async () => {
    vi.mocked(bridge.status).mockResolvedValueOnce({ ...status, health: "locked" });
    const store = createApplicationStore();
    await store.start();
    expect(bridge.history).not.toHaveBeenCalled();
    store.dispose();
  });

  it("routes device management actions through the matching native commands", async () => {
    const device = { id: "p", name: "P", paired: true, connection_state: "online",
      pairing_status: "paired", pairing_code: null, sas: null };
    const store = createApplicationStore();
    await store.start();
    await store.connect(device);
    await store.disconnect(device);
    await store.forget(device);
    await store.restore(device);
    await store.purge(device);
    expect(vi.mocked(bridge.connectDevice).mock.calls).toEqual([["p"]]);
    expect(vi.mocked(bridge.disconnectDevice).mock.calls).toEqual([["p"]]);
    expect(vi.mocked(bridge.forgetDevice).mock.calls).toEqual([["p"]]);
    expect(vi.mocked(bridge.restoreDevice).mock.calls).toEqual([["p"]]);
    expect(vi.mocked(bridge.purgeDevice).mock.calls).toEqual([["p"]]);
    expect(bridge.devices).toHaveBeenCalledTimes(6);
    store.dispose();
  });

  it("returns probe results and pinned certificates without refreshing devices", async () => {
    const device = { id: "p", name: "P", paired: true, connection_state: "online",
      pairing_status: "paired", pairing_code: null, sas: null };
    const probe = { ok: true, results: [{ channel: "lan" as const, ok: true, latency_ms: 12.5, error: null }] };
    vi.mocked(bridge.testDevice).mockResolvedValue(probe);
    vi.mocked(bridge.deviceCerts).mockResolvedValue({ devices: [
      { device_id: "p", device_name: "P", fingerprint_short: "abcdefgh...12345678",
        fingerprint: "abc", paired: true },
    ] });
    const store = createApplicationStore();
    await store.start();
    const devicesBefore = vi.mocked(bridge.devices).mock.calls.length;
    expect(await store.probe(device)).toEqual(probe);
    expect(await store.certificates()).toEqual([
      expect.objectContaining({ device_id: "p", fingerprint_short: "abcdefgh...12345678" }),
    ]);
    expect(vi.mocked(bridge.testDevice).mock.calls).toEqual([["p"]]);
    expect(bridge.deviceCerts).toHaveBeenCalledOnce();
    expect(vi.mocked(bridge.devices).mock.calls).toHaveLength(devicesBefore);
    expect(store.state.pending).toBe(false);
    store.dispose();
  });

  it("reports probe failures as errors instead of results", async () => {
    const device = { id: "p", name: "P", paired: true, connection_state: "online",
      pairing_status: "paired", pairing_code: null, sas: null };
    vi.mocked(bridge.testDevice).mockRejectedValue({ code: "NOT_FOUND", message: "Device not found", retryable: false });
    const store = createApplicationStore();
    await store.start();
    expect(await store.probe(device)).toBeNull();
    expect(store.state.error?.code).toBe("NOT_FOUND");
    expect(store.state.pending).toBe(false);
    store.dispose();
  });

  it("surfaces mutation errors without replaying the operation", async () => {
    vi.mocked(bridge.deleteHistory).mockRejectedValueOnce({
      code: "REQUEST_TIMEOUT", message: "Result unknown", retryable: false,
    });
    const store = createApplicationStore();
    await store.delete({
      id: "1", preview: "x", timestamp: 0, content_type: "TEXT", pinned: false,
      source_name: "", source_app: "", source_title: "", paste_count: 0,
    });
    expect(store.state.error?.code).toBe("REQUEST_TIMEOUT");
    expect(bridge.deleteHistory).toHaveBeenCalledOnce();
    expect(store.state.pending).toBe(false);
  });
});
