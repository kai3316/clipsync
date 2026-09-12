import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWebview, type DragDropEvent } from "@tauri-apps/api/webview";
import { t } from "../i18n";
import type { AppStatus, ChatMessagesPage, ChatSessionsPage, Device, DeviceCertificate, DeviceProbeResult, DiagnosticAction, DiagnosticsReport, Favorite, FavoritesPage, HistoryPage, InternetPairingStatus, Settings, SidecarEvent, TransfersPage, UpdateCheckResult, UpdateDownloadResult, UpdateOpenFolderResult, UpdateStatusResult } from "./types";

export function inDesktop(): boolean {
  return isTauri();
}

/** The surfaces the native tray can ask this window to open. */
export type MenuAction = "about" | "qr" | "send-url" | "settings" | "export-logs" | "check-update";

/** Every one, so the listener and its test cannot miss a new entry. */
export const MENU_ACTIONS: readonly MenuAction[] = [
  "about", "qr", "send-url", "settings", "export-logs", "check-update",
];

async function command<T>(name: string, args?: Record<string, unknown>): Promise<T> {
  if (!inDesktop()) {
    throw { code: "NATIVE_HOST_REQUIRED", message: t("请在 ClipSync 桌面窗口中打开"), retryable: false };
  }
  return invoke<T>(name, args);
}

function batchIds(entryIds: string[]): string[] {
  const ids = [...new Set(entryIds)];
  if (!ids.length || ids.length > 100 || ids.some((id) => typeof id !== "string" || !id.trim())) {
    throw { code: "INVALID_BATCH", message: t("请选择 1 至 100 条有效记录"), retryable: false };
  }
  return ids;
}

export function requireAiSuccess(result: Record<string, unknown>): Record<string, unknown> {
  if (result.ok === false) {
    throw { code: "AI_OPERATION_FAILED", message: String(result.error || t("AI 配置操作失败")), retryable: false };
  }
  return result;
}

export function requireAiPull(result: Record<string, unknown>): Record<string, unknown> {
  requireAiSuccess(result);
  if (typeof result.requested !== "number" || !Number.isInteger(result.requested) || result.requested <= 0) {
    throw { code: "AI_PULL_REJECTED",
      message: Array.isArray(result.errors) && result.errors.length
        ? result.errors.map(String).join(", ") : t("未发送任何配置拉取请求"),
      retryable: false };
  }
  return result;
}

export const bridge = {
  favorites: (query: string, group: string, offset: number, limit: number) =>
    command<FavoritesPage>("list_favorites", { query, group, offset, limit }),
  favorite: (favoriteId: string) => command<{ favorite: Favorite }>("get_favorite", { favoriteId }),
  addFavorite: (title: string, content: string, group: string) =>
    command<{ favorite: Favorite }>("add_favorite", { title, content, group }),
  updateFavorite: (favoriteId: string, title: string, content: string, group: string, position: number) =>
    command<{ favorite: Favorite }>("update_favorite", { favoriteId, title, content, group, position }),
  deleteFavorite: (favoriteId: string) => command<{ deleted: boolean }>("delete_favorite", { favoriteId }),
  copyFavorite: (favoriteId: string) => command<{ copied: boolean }>("copy_favorite", { favoriteId }),
  exportFavorites: (format: string) =>
    command<{ filepath: string; filename: string; count: number; format: string }>(
      "export_favorites", { format }),
  status: () => command<AppStatus>("get_app_status"),
  history: (query: string, offset: number, limit: number, kind: string, sort: string) =>
    command<HistoryPage>("list_history", { query, offset, limit, kind, sort }),
  devices: () => command<{ items: Device[] }>("list_devices"),
  setDeviceNote: (deviceId: string, note: string) => command<{ ok: boolean }>("set_device_note", { deviceId, note }),
  connectDevice: (deviceId: string) => command<{ accepted: boolean }>("connect_device", { deviceId }),
  disconnectDevice: (deviceId: string) => command<{ disconnected: boolean }>("disconnect_device", { deviceId }),
  forgetDevice: (deviceId: string) => command<{ forgotten: boolean }>("forget_device", { deviceId }),
  restoreDevice: (deviceId: string) => command<{ restored: boolean }>("restore_device", { deviceId }),
  purgeDevice: (deviceId: string) => command<{ purged: boolean }>("purge_device", { deviceId }),
  testDevice: (deviceId: string) => command<DeviceProbeResult>("test_device", { deviceId }),
  deviceCerts: () => command<{ devices: DeviceCertificate[] }>("device_certs"),
  retrustDevice: (deviceId: string) => command<{ trusted: boolean }>("device_retrust", { deviceId }),
  sendUrl: (deviceId: string, url: string) =>
    command<{ sent: boolean; device_id: string }>("send_url", { deviceId, url }),
  pushText: (text: string) =>
    command<{ ok: boolean; len: number; sent: boolean }>("push_text", { text }),
  discoveryStatus: () => command<{ enabled: boolean; visible: boolean }>("discovery_status"),
  setDiscoveryEnabled: (enabled: boolean) =>
    command<{ enabled: boolean; visible: boolean }>("set_discovery_enabled", { enabled }),
  setDiscoveryVisible: (enabled: boolean) =>
    command<{ enabled: boolean; visible: boolean }>("set_discovery_visible", { enabled }),
  settings: () => command<Settings>("get_settings"),
  companionStatus: () => command<{ enabled: boolean; port: number; running: boolean; state: string; access_url: string | null }>("companion_status"),
  configureCompanion: (enabled: boolean, port: number, rotateToken = false) =>
    command<{ enabled: boolean; port: number; running: boolean; state: string; access_url: string | null }>("configure_companion", { enabled, port, rotateToken }),
  updateSettings: (values: Record<string, unknown>) => command("update_settings", { values }),
  translate: (text: string, targetLang = "en", sourceLang = "auto") =>
    command<Record<string, unknown>>("translate_text", { text, targetLang, sourceLang }),
  aiProfiles: () => command<Record<string, unknown>>("ai_profiles"),
  updateAiProfiles: (tools: string[], customPaths: string[]) =>
    command<Record<string, unknown>>("update_ai_profiles", { tools, customPaths }),
  aiInventory: (refresh = false, peerId = "") =>
    command<Record<string, unknown>>("ai_inventory", { refresh, peerId }),
  aiPreview: async (peerId: string, tool: string, root: string, relPath: string) =>
    requireAiSuccess(await command<Record<string, unknown>>("ai_preview", { peerId, tool, root, relPath })),
  aiPull: async (peerId: string, items: Array<Record<string, unknown>>, mode = "copy", batchId = "") =>
    requireAiPull(await command<Record<string, unknown>>("ai_pull", { peerId, items, mode, batchId })),
  aiLocal: async (action: string, tool = "", root = "", relPath = "", content?: string) =>
    requireAiSuccess(await command<Record<string, unknown>>("ai_local", { action, tool, root, relPath, content })),
  autostartStatus: () => command<boolean>("autostart_status"),
  transfers: () => command<TransfersPage>("list_transfers"),
  // One path or several: several are the picker's multi-select, and the sidecar
  // puts them in one archive so they arrive as one transfer rather than N.
  sendFiles: (paths: string[], deviceId: string) => command<{ transfer_id: string }>("send_files", { paths, deviceId }),
  chooseFile: (kind?: "history" | "backup" | "any") => command<string | null>("choose_file", { kind }),
  // The send picker, unlike the history/backup one above, is multi-select — the
  // legacy 发送文件 dialog was, and cancelling answers with no paths rather than
  // with an error.
  chooseFiles: () => command<string[]>("choose_files"),
  // A folder goes to the sidecar as a path like any other pick; the sidecar
  // archives it, because the wire carries files and the archive has to outlive
  // the transfer it is sent by.
  chooseFolder: () => command<string | null>("choose_folder"),
  transferAction: (action: string, transferId: string) =>
    command("transfer_action", { action, transferId }),
  // The sidecar reads the live transfer list itself, so this cancels transfers
  // the window has not seen yet too — and it is one round trip, not one per row.
  cancelAllTransfers: () => command<{ cancelled: number }>("cancel_all_transfers"),
  // Records only, and counted by the sidecar: a transfer that finished since the
  // page's last poll is one the page's own list would not have counted.
  clearTransferHistory: () => command<{ cleared: number }>("clear_transfer_history"),
  startSpeedTest: () => command<{ test_id: string }>("start_speed_test"),
  sendChatFile: (sessionId: string, path: string) =>
    command<{ ok: boolean; transfer_id: string }>("send_chat_file", { sessionId, path }),
  chatFileAction: (action: string, sessionId: string, transferId: string) =>
    command<{ ok: boolean }>("chat_file_action", { action, sessionId, transferId }),
  listBackups: () => command<{ backups: Array<Record<string, unknown>> }>("list_backups"),
  createBackup: () => command<{ backup_path: string }>("create_backup"),
  restoreBackup: (path: string) => command<Record<string, unknown>>("restore_backup", { path }),
  exportHistory: (format: string) => command<Record<string, unknown>>("export_history", { format }),
  importHistory: (path: string) => command<Record<string, unknown>>("import_history", { path }),
  chatDevices: () => command<{ devices: Device[] }>("list_chat_devices"),
  chatSessions: () => command<ChatSessionsPage>("list_chat_sessions"),
  setChatMuted: (peerId: string, muted: boolean) =>
    command<{ ok: boolean; muted: string[] }>("set_chat_muted", { peerId, muted }),
  chatMessages: (sessionId: string) => command<ChatMessagesPage>("list_chat_messages", { sessionId }),
  openChatFile: (sessionId: string, transferId: string) =>
    command<{ ok: boolean }>("open_chat_file", { sessionId, transferId }),
  // `chat_session_id`, not `session_id`: the frame envelope reads a top-level
  // `session_id` as the sidecar's own session, and a result putting a chat
  // session there is refused as an invalid frame — which the host treats as
  // fatal. The sidecar validates its results for the same reason
  // (`validate_result` in `internal/adapters/sidecar/rpc.py`).
  inviteChat: (peerId: string, peerName: string) =>
    command<{ chat_session_id: string | null; connecting: boolean }>("invite_chat", { peerId, peerName }),
  acceptChatInvite: (sessionId: string) => command<{ ok: boolean }>("accept_chat_invite", { sessionId }),
  declineChatInvite: (sessionId: string) => command<{ ok: boolean }>("decline_chat_invite", { sessionId }),
  sendChatText: (sessionId: string, text: string) =>
    command<{ ok: boolean }>("send_chat_text", { sessionId, text }),
  resendChatText: (sessionId: string, entryId: string) =>
    command<{ ok: boolean }>("resend_chat_text", { sessionId, entryId }),
  sendChatTyping: (sessionId: string, typing: boolean) =>
    command<{ ok: boolean }>("chat_typing", { sessionId, typing }),
  markChatRead: (sessionId: string) => command<{ ok: boolean }>("mark_chat_read", { sessionId }),
  closeChat: (sessionId: string) => command<{ ok: boolean }>("close_chat", { sessionId }),
  startPairing: (deviceId: string) => command<{ accepted: boolean }>("start_pairing", { deviceId }),
  confirmPairing: (deviceId: string, code: string) =>
    command<{ paired: boolean; status: string }>("confirm_pairing", { deviceId, code }),
  rejectPairing: (deviceId: string) => command<{ accepted: boolean }>("reject_pairing", { deviceId }),
  unpairDevice: (deviceId: string) => command<{ accepted: boolean }>("unpair_device", { deviceId }),
  internetPairingStatus: () => command<InternetPairingStatus>("internet_pairing_status"),
  generateInternetPairingCode: () => command<{ code: string }>("internet_pairing_generate"),
  // The answer names the provisional tag the code was written under and says
  // the pairing is only half done — the partner's reply is what makes it a
  // device.  Returning here is not a success, and the panel must not report one.
  enterInternetPairingCode: (code: string) =>
    command<{ peer_id: string; waiting?: boolean }>("internet_pairing_enter", { code }),
  renameInternetPeer: (peerId: string, name: string) =>
    command<{ ok: boolean }>("internet_pairing_rename", { peerId, name }),
  unpairInternetPeer: (peerId: string) =>
    command<{ ok: boolean }>("internet_pairing_unpair", { peerId }),
  relayDeliveryStatus: (peerId = "") =>
    command<{ pending: number; items: Array<Record<string, unknown>> }>(
      "relay_delivery_status", { peerId }),
  setSyncEnabled: (enabled: boolean) => command<{ enabled: boolean }>("set_sync_enabled", { enabled }),
  pauseSync: (minutes: number) => command<{ enabled: boolean; until: number }>("pause_sync", { minutes }),
  resumeSync: () => command<{ enabled: boolean }>("resume_sync"),
  copyHistory: (entryId: string) => command<{ copied: boolean }>("copy_history", { entryId }),
  readHistoryText: (entryId: string) =>
    command<{ id: string; text: string; truncated: boolean }>("read_history_text", { entryId }),
  openHistoryLink: (entryId: string) =>
    command<{ opened: boolean; url: string }>("open_history_link", { entryId }),
  unlock: (password: string) => command("unlock_app", { password }),
  deleteHistory: (entryId: string) => command("delete_history", { entryId }),
  pinHistory: (entryId: string, pinned: boolean) =>
    command("set_history_pinned", { entryId, pinned }),
  batchPinHistory: async (entryIds: string[], pinned: boolean) =>
    command<{ updated: number }>("batch_pin_history", { entryIds: batchIds(entryIds), pinned }),
  batchDeleteHistory: async (entryIds: string[]) =>
    command<{ deleted: number }>("batch_delete_history", { entryIds: batchIds(entryIds) }),
  batchFavoriteHistory: async (entryIds: string[], group: string) =>
    command<{ added: number; ids: string[] }>(
      "batch_favorite_history", { entryIds: batchIds(entryIds), group }),
  clearHistory: () => command<{ cleared: number }>("clear_history"),
  readLogs: (lines = 200) => command<{ logs: string[] }>("read_logs", { lines }),
  exportLogs: (defaultName: string) =>
    command<{ cancelled?: boolean; path?: string; bytes?: number }>("export_logs", { defaultName }),
  diagnosticsReport: () => command<DiagnosticsReport>("diagnostics_report"),
  diagnosticsRequest: (action: DiagnosticAction) =>
    command<{ ok: boolean; error?: string }>("diagnostics_request", { action }),
  updateCheck: () => command<UpdateCheckResult>("update_check"),
  updateStatus: () => command<UpdateStatusResult>("update_status"),
  updateDownload: () => command<UpdateDownloadResult>("update_download"),
  updateOpenFolder: () => command<UpdateOpenFolderResult>("update_open_folder"),
  openDataFolder: (which: "data" | "backups") =>
    command<{ ok: boolean; folder: string }>("open_data_folder", { which }),
  openAboutLink: (target: "homepage" | "releases") =>
    command<{ ok: boolean; url: string }>("open_about_link", { target }),
  companionQr: () =>
    command<{ ok: boolean; url: string | null; qr: string | null; error?: string }>("companion_qr"),
  onMenuAction: async (handler: (action: MenuAction) => void): Promise<UnlistenFn> => {
    // The native tray's entries that open one of this window's own surfaces.
    // One listener per action, because Tauri matches an event name exactly —
    // and one unlisten, so the caller holds one handle however many these are.
    if (!inDesktop()) return () => {};
    const offs = await Promise.all(
      MENU_ACTIONS.map((action) =>
        listen(`ui:${action}`, () => handler(action)),
      ),
    );
    return () => { for (const off of offs) off(); };
  },
  // Files dragged onto the window.  The window is the drop target rather than
  // the page: the native side is what holds the real paths, and the webview's
  // own HTML5 drop event carries File objects whose paths the browser does not
  // expose — which is why `dragDropEnabled` is true in `tauri.conf.json`, since
  // with it false Tauri hands the drop to the webview and the paths are gone.
  // One listener for all four phases (enter / over / drop / leave): they are one
  // gesture, and the caller's state reads them together.
  onFileDrop: async (handler: (event: DragDropEvent) => void): Promise<UnlistenFn> => {
    if (!inDesktop()) return () => {};
    return getCurrentWebview().onDragDropEvent(({ payload }) => handler(payload));
  },
  restartApp: () => command("restart_app"),
  // Relaunches only the background process; the window and its state survive.
  restartSidecar: () => command("restart_sidecar"),
  // Moves damaged configuration, identity or history aside — never deletes —
  // and brings the background process back on the repaired directory. The only
  // way out of a data directory the sidecar itself refuses to start on.
  recoverDataDir: () => command("recover_data_dir"),
  factoryReset: () => command("factory_reset"),
  // Puts the window away without ending the session; quitting is the tray's.
  minimize: () => command("minimize_app"),
  quit: () => command("quit_app"),
  subscribe: async (
    onEvent: (event: SidecarEvent) => void,
    onState: (state: { state: string; error?: string; attempt?: number }) => void,
  ): Promise<UnlistenFn> => {
    if (!inDesktop()) return () => {};
    const offEvent = await listen<SidecarEvent>("sidecar:event", ({ payload }) => onEvent(payload));
    try {
      const offState = await listen<{ state: string; error?: string; attempt?: number }>(
        "sidecar:state", ({ payload }) => onState(payload),
      );
      return () => { offEvent(); offState(); };
    } catch (error) {
      offEvent();
      throw error;
    }
  },
};
