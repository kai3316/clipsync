import { describe, expect, it, vi } from "vitest";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { bridge } from "../src/api/bridge";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(), isTauri: vi.fn(() => false) }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: vi.fn() }));

describe("browser isolation", () => {
  it("does not fall back to a localhost management API", async () => {
    await expect(bridge.status()).rejects.toMatchObject({ code: "NATIVE_HOST_REQUIRED" });
    expect(invoke).not.toHaveBeenCalled();
  });
  it("uses the fixed native command names and camelCase arguments", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    await bridge.startPairing("p");
    await bridge.confirmPairing("p", "123");
    await bridge.rejectPairing("p");
    await bridge.unpairDevice("p");
    await bridge.setSyncEnabled(false);
    await bridge.copyHistory("h");
    await bridge.batchPinHistory(["a", "a", "b"], false);
    await bridge.batchDeleteHistory(["a", "b"]);
    await bridge.batchFavoriteHistory(["a", "b"], "Work");
    await bridge.clearHistory();
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["start_pairing", { deviceId: "p" }],
      ["confirm_pairing", { deviceId: "p", code: "123" }],
      ["reject_pairing", { deviceId: "p" }],
      ["unpair_device", { deviceId: "p" }],
      ["set_sync_enabled", { enabled: false }],
      ["copy_history", { entryId: "h" }],
      ["batch_pin_history", { entryIds: ["a", "b"], pinned: false }],
      ["batch_delete_history", { entryIds: ["a", "b"] }],
      ["batch_favorite_history", { entryIds: ["a", "b"], group: "Work" }],
      ["clear_history", undefined],
    ]);
  });
  it.each([[], [""], [" "], Array.from({ length: 101 }, (_, i) => String(i))])(
    "rejects invalid batch IDs before invoking native commands (%j)", async (...args) => {
      const ids = args as string[];
      const before = vi.mocked(invoke).mock.calls.length;
      await expect(bridge.batchPinHistory(ids, true)).rejects.toMatchObject({ code: "INVALID_BATCH" });
      await expect(bridge.batchDeleteHistory(ids)).rejects.toMatchObject({ code: "INVALID_BATCH" });
      expect(invoke).toHaveBeenCalledTimes(before);
    },
  );
  it("uses the device management commands with the device id only", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.connectDevice("p");
    await bridge.disconnectDevice("p");
    await bridge.forgetDevice("p");
    await bridge.restoreDevice("p");
    await bridge.purgeDevice("p");
    await bridge.testDevice("p");
    await bridge.deviceCerts();
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["connect_device", { deviceId: "p" }],
      ["disconnect_device", { deviceId: "p" }],
      ["forget_device", { deviceId: "p" }],
      ["restore_device", { deviceId: "p" }],
      ["purge_device", { deviceId: "p" }],
      ["test_device", { deviceId: "p" }],
      ["device_certs", undefined],
    ]);
  });
  it("uses the URL and discovery commands with the backend contract fields", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.sendUrl("p", "https://example.com");
    await bridge.pushText("hello");
    await bridge.discoveryStatus();
    await bridge.setDiscoveryEnabled(false);
    await bridge.setDiscoveryVisible(true);
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["send_url", { deviceId: "p", url: "https://example.com" }],
      ["push_text", { text: "hello" }],
      ["discovery_status", undefined],
      ["set_discovery_enabled", { enabled: false }],
      ["set_discovery_visible", { enabled: true }],
    ]);
  });
  it("reads logs and restarts with the fixed native command names", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.readLogs();
    await bridge.readLogs(500);
    await bridge.restartApp();
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["read_logs", { lines: 200 }],
      ["read_logs", { lines: 500 }],
      ["restart_app", undefined],
    ]);
  });
  it("runs and repairs diagnostics with the fixed native command names", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.diagnosticsReport();
    await bridge.diagnosticsRequest("firewall");
    await bridge.diagnosticsRequest("local_network");
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["diagnostics_report", undefined],
      ["diagnostics_request", { action: "firewall" }],
      ["diagnostics_request", { action: "local_network" }],
    ]);
  });
  it("drives the update lifecycle with the fixed native command names", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.updateCheck();
    await bridge.updateStatus();
    await bridge.updateDownload();
    await bridge.updateOpenFolder();
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["update_check", undefined],
      ["update_status", undefined],
      ["update_download", undefined],
      ["update_open_folder", undefined],
    ]);
  });
  it("releases the first listener when the second subscription fails", async () => {
    const off = vi.fn();
    vi.mocked(listen).mockResolvedValueOnce(off).mockRejectedValueOnce(new Error("failed"));
    await expect(bridge.subscribe(vi.fn(), vi.fn())).rejects.toThrow("failed");
    expect(off).toHaveBeenCalledOnce();
  });
  it("uses independent favorites commands with full-body update arguments", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.favorites("q", "g", 0, 30);
    await bridge.favorite("f");
    await bridge.addFavorite("t", "body", "g");
    await bridge.updateFavorite("f", "t", "full body", "g", 3);
    await bridge.deleteFavorite("f");
    await bridge.copyFavorite("f");
    await bridge.exportFavorites("markdown");
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["list_favorites", { query: "q", group: "g", offset: 0, limit: 30 }],
      ["get_favorite", { favoriteId: "f" }],
      ["add_favorite", { title: "t", content: "body", group: "g" }],
      ["update_favorite", { favoriteId: "f", title: "t", content: "full body", group: "g", position: 3 }],
      ["delete_favorite", { favoriteId: "f" }],
      ["copy_favorite", { favoriteId: "f" }],
      ["export_favorites", { format: "markdown" }],
    ]);
  });
  it("uses the desktop chat commands with the backend contract fields", async () => {
    vi.mocked(invoke).mockClear();
    await bridge.chatDevices();
    await bridge.chatSessions();
    await bridge.chatMessages("s");
    await bridge.inviteChat("p", "Peer");
    await bridge.acceptChatInvite("s");
    await bridge.declineChatInvite("s");
    await bridge.sendChatText("s", "hello");
    await bridge.markChatRead("s");
    await bridge.closeChat("s");
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ["list_chat_devices", undefined],
      ["list_chat_sessions", undefined],
      ["list_chat_messages", { sessionId: "s" }],
      ["invite_chat", { peerId: "p", peerName: "Peer" }],
      ["accept_chat_invite", { sessionId: "s" }],
      ["decline_chat_invite", { sessionId: "s" }],
      ["send_chat_text", { sessionId: "s", text: "hello" }],
      ["mark_chat_read", { sessionId: "s" }],
      ["close_chat", { sessionId: "s" }],
    ]);
  });
  it("releases both event listeners", async () => {
    const offEvent = vi.fn();
    const offState = vi.fn();
    vi.mocked(listen).mockResolvedValueOnce(offEvent).mockResolvedValueOnce(offState);
    const off = await bridge.subscribe(vi.fn(), vi.fn());
    off();
    expect(offEvent).toHaveBeenCalledOnce();
    expect(offState).toHaveBeenCalledOnce();
  });
  it("asks the window for a file drop, and hands over the payload's own shape", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    const off = vi.fn();
    const onDragDropEvent = vi.fn().mockResolvedValue(off);
    vi.mocked(getCurrentWebview).mockReturnValue({ onDragDropEvent } as any);
    const handler = vi.fn();
    const unlisten = await bridge.onFileDrop(handler);
    expect(onDragDropEvent).toHaveBeenCalledOnce();
    // The native event's envelope is the webview's, not the shell's: the caller
    // gets the `type`/`paths` the OS sent, so it can read the four phases of one
    // gesture without knowing where they came from.
    const payload = { type: "drop", paths: ["C:/a.txt"], position: { x: 0, y: 0 } };
    onDragDropEvent.mock.calls[0][0]({ payload });
    expect(handler).toHaveBeenCalledWith(payload);
    unlisten();
    expect(off).toHaveBeenCalledOnce();
  });
  // Last, because it is the one case here that is not a desktop window: the
  // `isTauri` mock is module-wide, and a case after this one would find it false.
  it("subscribes to no drop target outside the desktop", async () => {
    vi.mocked(isTauri).mockReturnValue(false);
    vi.mocked(getCurrentWebview).mockClear();
    const handler = vi.fn();
    const off = await bridge.onFileDrop(handler);
    // The browser has no window to drop onto, so there is no listener to leave
    // behind — and the caller's cleanup still works.
    expect(getCurrentWebview).not.toHaveBeenCalled();
    off();
    expect(handler).not.toHaveBeenCalled();
  });
});
