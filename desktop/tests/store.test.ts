import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bridge } from "../src/api/bridge";
import { t } from "../src/i18n";
import { createApplicationStore } from "../src/stores/application";
import type { HistoryItem } from "../src/api/types";

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
    requestEntryFiles: vi.fn(),
    sendUrl: vi.fn(), pushText: vi.fn(), discoveryStatus: vi.fn(),
    setDiscoveryEnabled: vi.fn(), setDiscoveryVisible: vi.fn(),
    favorites: vi.fn(), favorite: vi.fn(),
    diagnosticsReport: vi.fn(), diagnosticsRequest: vi.fn(),
    updateCheck: vi.fn(), updateStatus: vi.fn(), updateDownload: vi.fn(),
    updateOpenFolder: vi.fn(), updateInstall: vi.fn(),
    restartSidecar: vi.fn(), recoverDataDir: vi.fn(),
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
  it("asks a device for a row's files, then words that device's refusal", async () => {
    vi.mocked(bridge.requestEntryFiles).mockResolvedValue({ requested: true });
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    store.state.devices = [{ ...pairedDevice, id: "peer-1", name: "Studio" }];
    const row = { id: "7", source_device: "peer-1" } as HistoryItem;

    expect(await store.downloadRemoteFile(row)).toBe(true);
    expect(bridge.requestEntryFiles).toHaveBeenCalledExactlyOnceWith("7", "peer-1");
    // The row waits — the button shows it is waiting — until the peer either
    // starts sending or says why it cannot.
    expect(store.state.remoteFilePending).toEqual(["peer-1:7"]);

    // The reason crosses the wire as a code, because the peer does not know
    // which language this window is in; it is worded here.
    emit({ type: "event", name: "clip.file.denied", session_id: "session",
      data: { device_id: "peer-1", entry_id: "7", reason: "gone" } });
    expect(store.state.remoteFilePending).toEqual([]);
    expect(store.state.notices.map((notice) => notice.message)).toEqual([
      t("{name} 没能发送这个文件：{reason}", {
        name: "Studio", reason: t("文件已被移动或删除"),
      }),
    ]);
  });
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

  it("names the application holding the data folder instead of a generic retry", async () => {
    const store = createApplicationStore();
    await store.start();
    const state = vi.mocked(bridge.subscribe).mock.calls[0][1];
    // A failure write-back sets `health` on whatever `state.status` holds, and
    // `start()` has already pointed that at the shared fixture -- give this
    // test its own copy or the mark lands on every test after it.
    store.state.status = { ...status };
    // What the sidecar sends when the legacy application still holds the data
    // directory: a code, its own English sentence, and retryable false.  The
    // window has to turn that into something the user can act on -- the old
    // generic line left them a retry button and no reason for the failure.
    state({
      state: "failed",
      error: "DATA_IN_USE",
      message: "Close the legacy ClipSync application first",
      retryable: false,
    });
    expect(store.state.error).toEqual({
      code: "DATA_IN_USE",
      message: "数据目录正被旧版 ClipSync 占用。请关闭旧版应用后重试。",
      // Retryable in spite of what the sidecar said: closing the other
      // application is exactly what makes the next attempt succeed.
      retryable: true,
    });
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

  it("expires each notice independently and clears timers on disposal", async () => {
    vi.useFakeTimers();
    const store = createApplicationStore();
    await store.start();
    const emit = vi.mocked(bridge.subscribe).mock.calls[0][0];
    // The engine's failures are worded from their code, so two codes are two
    // different notices — which is what makes the expiry of one visible against
    // the other still standing.
    emit({ type: "event", name: "runtime.error", session_id: "session",
      data: { code: "CLIPBOARD_READ_FAILED" } });
    await vi.advanceTimersByTimeAsync(3000);
    emit({ type: "event", name: "runtime.error", session_id: "session",
      data: { code: "HISTORY_WRITE_FAILED" } });
    await vi.advanceTimersByTimeAsync(3000);
    expect(store.state.notices.map(item => item.message))
      .toEqual(["历史记录写入失败，本机磁盘可能已满。"]);
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

  it("does not call a confirm that is waiting on the peer a failure", async () => {
    // `paired: false` is also what a handshake in flight answers.  Read as
    // failure it told the user the opposite of what was happening, while the
    // row underneath already said 等待对方确认.
    const device = { id: "peer", name: "Peer", paired: false, connection_state: "online",
      pairing_status: "confirmed_waiting", pairing_code: "123456", sas: "ABCD" };
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [device] });
    vi.mocked(bridge.confirmPairing).mockResolvedValueOnce({ paired: false, status: "confirmed_waiting" });
    const store = createApplicationStore();
    await store.confirmPairing(device);
    expect(store.state.notices).toEqual([]);
    store.dispose();
  });

  it("still says so when the confirm lands after the handshake is over", async () => {
    const device = { id: "peer", name: "Peer", paired: false, connection_state: "online",
      pairing_status: "cancelled", pairing_code: "123456", sas: "ABCD" };
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [device] });
    vi.mocked(bridge.confirmPairing).mockResolvedValueOnce({ paired: false, status: "cancelled" });
    const store = createApplicationStore();
    await store.confirmPairing(device);
    expect(store.state.notices.map((notice) => notice.title)).toEqual(["pairing.failed"]);
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

  it("pushes text through the host and reports whether it was broadcast", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.pushText).mockResolvedValueOnce({ ok: true, len: 5, sent: true });
    expect(await store.pushText("hello")).toEqual({ ok: true, len: 5, sent: true });
    expect(bridge.pushText).toHaveBeenCalledExactlyOnceWith("hello");
    vi.mocked(bridge.pushText).mockResolvedValueOnce({ ok: true, len: 7, sent: false });
    expect(await store.pushText("offline")).toEqual({ ok: true, len: 7, sent: false });
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
      data: { latest: "v2.0.0", current: "1.0.0", url: "https://example.com",
        installable: true } });
    // `installable` rides with the notice: without it the card reads the host's
    // silence as "cannot install in place" and offers the manual download, which
    // is how a self-installing build sent the user another application.
    expect(store.state.updateCheck).toEqual({ available: true, latest: "v2.0.0",
      current: "1.0.0", url: "https://example.com", installable: true });
    await vi.advanceTimersByTimeAsync(120);
    expect(bridge.status).toHaveBeenCalledTimes(statusCalls);
    store.dispose();
  });

  it("does not let a status hydration undo an install in flight", async () => {
    const store = createApplicationStore();
    vi.mocked(bridge.status).mockResolvedValue({
      ...status, capabilities: ["update.status"],
    });
    await store.start();
    const event = vi.mocked(bridge.subscribe).mock.calls[0][0];
    event({ type: "event", name: "update.state", session_id: "session", seq: 1,
      data: { state: { phase: "installing", fraction: 1, downloaded: 100, total: 100,
        error: "", version: "v2.0.0", path: "" } } });
    expect(store.state.update.phase).toBe("installing");
    // Opening settings re-reads the sidecar's phase, and the sidecar only ever
    // reports its own four -- it would answer `idle` and put the download
    // button back while the bundle is being replaced underneath it.
    vi.mocked(bridge.updateStatus).mockResolvedValueOnce({ state: { phase: "idle",
      fraction: 0, downloaded: 0, total: 0, error: "", version: "", path: "" } });
    await store.loadUpdateStatus();
    expect(store.state.update.phase).toBe("installing");
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

  it("says a copied row was copied, and says nothing when it was not", async () => {
    const item = {
      id: "1", preview: "x", timestamp: 0, content_type: "TEXT", pinned: false,
      source_name: "", source_app: "", source_title: "", paste_count: 0,
    };
    const store = createApplicationStore();
    vi.mocked(bridge.copyHistory).mockResolvedValueOnce({ copied: true });
    await store.copy(item);
    // The row's own answer is a tick that replaces its copy icon — in a list
    // the reader has looked away from by the time it lands. The notice is the
    // half that travels.
    expect(store.state.notices.map((notice) => [notice.title, notice.message]))
      .toEqual([["ui.history", t("已复制")]]);

    store.dismissNotice(store.state.notices[0].id);
    vi.mocked(bridge.copyHistory).mockResolvedValueOnce({ copied: false });
    await store.copy(item);
    expect(store.state.notices).toEqual([]);
    store.dispose();
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
});
