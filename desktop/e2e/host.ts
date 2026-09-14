/**
 * A stand-in for the native host, for looking at the window in a browser.
 *
 * The window is a Vue app that talks to a sidecar through Tauri's `invoke`.
 * Outside Tauri every command refuses with `NATIVE_HOST_REQUIRED`, so the
 * browser shows one dead page and nothing else — which is correct behaviour and
 * useless for reviewing what the pages actually look like.
 *
 * This puts a host back: `isTauri()` is made to answer true, `invoke` answers
 * from the fixtures below, and `listen` records its listeners so a test can
 * push an event through them.  Nothing here is shipped in the app — it lives in
 * `e2e/` and is only ever injected into a page under Playwright.
 *
 * The fixtures are deliberately *believable rather than minimal*: a preview
 * built on empty arrays hides exactly the layout problems worth finding (a long
 * device name, a wrapped filename, a message with no spaces in it).
 */

/** Which language the preview host reports.  The window takes its locale from
 * the host's own answer, so photographing the app in English is this one
 * variable rather than a second set of fixtures: `CLIPSYNC_E2E_LANG=en npx
 * playwright test preview`.  Read here, in Node, because Playwright serializes
 * the fixtures into the page and `process` does not exist over there. */
export const LANGUAGE = process.env.CLIPSYNC_E2E_LANG ?? "zh-CN";

/** A sample string in the run's language.
 *
 *  Sample *data* is not chrome.  The window's own labels come from its string
 *  tables and switch with the locale by themselves, but a device name, a clip
 *  and a chat message are values the sidecar would have handed over — and a
 *  fixture that carried only Chinese put Chinese rows inside an English window,
 *  which is what every `en-*.png` did until this existed.  A reader looking at
 *  the English screenshot is reading a picture of the application, not of the
 *  fixture's author. */
const S = (zh: string, en: string) => (LANGUAGE === "en" ? en : zh);

/** The machines this sample data is about, named once.  A device in the list, a
 *  history row's source and a chat session all name the same peer, and three
 *  spellings of one machine is how a screenshot ends up disagreeing with
 *  itself. */
const LOC = S("工位台式机", "Workstation");
const MAC = "MacBook Pro";
const MINI = S("Mac mini（书房）", "Mac mini (study)");
const OLD_LAPTOP = S("办公室的那台旧笔记本（2019 年年中买的那个）", "The old office laptop (bought mid-2019)");
const HOME_PC = S("家里的台式机", "Home desktop");
const FRONT_DESK = S("前台那台一体机", "Front-desk all-in-one");
const OLD_DESKTOP = S("旧台式机", "Old desktop");

/** Realistic sample data, keyed by the command that returns it. */
export const fixtures: Record<string, unknown> = {
  // Not a command, and never answered as one: the names above, for the parts of
  // the harness that build their own rows in the page and have no way to read
  // this module (`install-host.ts`).  See its own `S`.
  sample: { lang: LANGUAGE, local: LOC, mac: MAC, mini: MINI },
  get_app_status: {
    version: "1.0.5",
    health: "ready",
    device_name: LOC,
    device_id: "7f3c1a92-4e8b-4d21-9a55-2c6e0b7d4f18",
    language: LANGUAGE,
    sync_state: "running",
    capabilities: [
      "history.list", "history.delete", "history.set_pinned", "history.copy",
      "history.text", "history.open_link", "history.batch_delete",
      "history.batch_set_pinned", "history.clear",
      "favorites.list", "favorites.get", "favorites.add", "favorites.update",
      "favorites.delete", "favorites.copy", "favorites.batch_add", "favorites.export",
      "favorites.reorder", "favorites.group_create", "favorites.group_rename",
      "favorites.group_delete",
      "settings.get", "settings.update",
      "companion.status", "companion.qr",
      "backups.list", "backups.create", "backups.restore",
      "history.export", "history.import", "clipboard.copy",
      "translate.text", "ai.profiles", "ai.profiles.update",
      "logs.tail", "logs.export", "overview.get",
      "app.open_link", "app.factory_reset",
      "diagnostics.report", "diagnostics.request",
      "update.check", "update.status", "update.download", "update.open_folder",
      "data.open_folder",
      "companion.share_file",
      "internet_pairing.test",
      "sync.set_enabled", "sync.pause", "sync.resume",
      "pairing.start", "pairing.confirm", "pairing.reject", "pairing.unpair",
      "devices.note", "devices.connect", "devices.disconnect", "devices.forget",
      "devices.restore", "devices.purge", "devices.test", "devices.certs",
      "devices.retrust", "companion.configure",
      "url.send", "clipboard.push", "discovery.status",
      "discovery.set_enabled", "discovery.set_visible",
      "transfers.list", "transfers.send", "transfers.action",
      "transfers.cancel_all", "transfers.speed_test", "transfers.clear_history",
      "transfers.request_entry_files",
      "chat.devices", "chat.sessions", "chat.messages", "chat.invite",
      "chat.action", "chat.file", "chat.typing", "chat.mute", "chat.open_file",
      "chat.reveal_file",
      // The sidecar appends these only while the engine is up
      // (`internal/application/bootstrap.py`), and this fixture reports
      // `sync_state: "running"` -- so leaving them out would describe an app
      // state the real sidecar never produces.  Nothing in the shell gates on
      // them today, which is exactly why the drift went unnoticed:
      // `tests/sidecar/test_e2e_host_capabilities.py` holds this list to the
      // sidecar's from now on.
      "ai.inventory", "ai.preview", "ai.pull",
      "ai.local.listing", "ai.local.read", "ai.local.save",
      "ai.local.trash", "ai.local.open",
      "internet_pairing.status", "internet_pairing.generate",
      "internet_pairing.enter", "internet_pairing.rename",
      "internet_pairing.unpair", "relay.delivery_status",
    ],
    session_id: "s-1",
    seq: 42,
  },
  get_settings: {
    settings: {
      device_id: "7f3c1a92-4e8b-4d21-9a55-2c6e0b7d4f18",
      device_name: LOC,
      language: LANGUAGE,
      language_chosen: true,
      appearance_mode: "system",
      sync_enabled: true,
      plain_text_only: false,
      encryption_enabled: true,
      password_set: true,
      notifications_enabled: true,
      notify_device_connect: true,
      notify_transfer: true,
      notify_pairing: true,
      notify_sync: false,
      web_enabled: true,
      web_port: 8765,
      web_history_limit: 50,
      history_max_entries: 1000,
      history_max_age_days: 30,
      // The directory the app actually falls back to (`web/server.py` —
      // `_get_upload_dir`): `~/Downloads/ClipSync`.  A fixture is free to pick a
      // drive, but not to invent a folder name the settings card would then
      // contradict when a reader compares the two screenshots.
      file_receive_dir: "C:\\Users\\sukai\\Downloads\\ClipSync",
      sync_debounce: 300,
      clipboard_poll_interval: 500,
      max_reconnect_attempts: 5,
      transfer_timeout: 120,
      log_level: "INFO",
      auto_start: true,
      port: 51888,
      service_type: "clipsync",
      app_filter_enabled: false,
      app_filter_mode: "blacklist",
      app_filter_list: [],
      filter_enabled_categories: ["credit_card", "api_key"],
      source_tracking_enabled: true,
      sound_enabled: true,
      ui_animation_enabled: true,
      ui_backend: "auto",
      translate_url: "https://api.example.com/translate",
      translate_key_set: false,
      paste_to_top: true,
      low_memory_mode: false,
      retry_capture_enabled: true,
      dedup_method: "hash",
      auto_update_check: true,
      internet_sync_enabled: false,
      internet_sync_state: "off",
      relay_brokers: ["wss://relay.example.com/ws"],
      relay_private_brokers: [],
      relay_username: "",
      relay_password_set: false,
      netpair_password_set: false,
      current_relay_broker: "",
      data_dir: "C:\\Users\\sukai\\AppData\\Roaming\\ClipSync",
      hotkeys_enabled: true,
      hotkeys: { toggle_window: "CommandOrControl+Shift+V", paste_latest: "CommandOrControl+Shift+B" },
      ai_tools: ["claude-code", "cursor"],
      ai_custom_paths: [],
    },
  },
  // Peers, all of them machines that could run this application.  A phone can
  // reach this app through the browser companion, which is a token and an HTTP
  // page — it never becomes a *paired device*, so a device list containing one
  // described a state the transport cannot produce.
  list_devices: {
    items: [
      {
        id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30",
        name: MAC,
        note: S("客厅", "Living room"),
        paired: true,
        connection_state: "online",
        pairing_status: "paired",
        pairing_code: null,
        sas: null,
      },
      {
        id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13",
        name: MINI,
        note: S("书房", "Study"),
        paired: true,
        connection_state: "offline",
        pairing_status: "paired",
        pairing_code: null,
        sas: null,
      },
      // The one row the transport is retrying right now, which is the state
      // the reconnect count exists for: without it this row would read exactly
      // like the one above it, and the two are not the same answer.
      {
        id: "f6b8c0e2-5a7b-9c14-3f25-6b8c0e2a4c57",
        name: HOME_PC,
        note: "",
        paired: true,
        connection_state: "connecting",
        pairing_status: "paired",
        pairing_code: null,
        sas: null,
        reconnecting: true,
        reconnect_attempt: 3,
        reconnect_max: 10,
      },
      {
        id: "c3e5a7b9-2d4f-6a81-0c92-3e5a7b9d1f24",
        name: OLD_LAPTOP,
        note: S("备用机", "Spare"),
        paired: false,
        connection_state: "offline",
        pairing_status: "waiting_confirm",
        pairing_code: "48291370",
        sas: null,
      },
      {
        id: "d4f6b8c0-3e5a-7b92-1d03-4f6b8c0e2a35",
        name: FRONT_DESK,
        note: "",
        paired: false,
        connection_state: "discovered",
        pairing_status: "none",
        pairing_code: null,
        sas: null,
      },
      // The tail of the list: a device that was taken away rather than one that
      // is here.  Its row is the only place the page draws 彻底删除, so leaving
      // it out of the fixture left that button out of every screenshot.
      {
        id: "e5a7b9c1-4f6b-8c03-2e14-5a7b9c1f3b46",
        name: OLD_DESKTOP,
        note: "",
        paired: false,
        connection_state: "offline",
        pairing_status: "paired",
        pairing_code: null,
        sas: null,
        archived: true,
        // A wall-clock stamp, not a flag: an archived row shows when it was
        // taken away, and zero reads as 1970.
        removed_at: Math.floor(Date.now() / 1000) - 6 * 86400,
      },
    ],
  },
  list_chat_devices: {
    devices: [
      { id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30", name: MAC, paired: true },
      { id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13", name: MINI, paired: true },
    ],
  },
  device_certs: {
    devices: [
      {
        device_id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30",
        device_name: MAC,
        fingerprint_short: "9F:2C:41:7B",
        fingerprint: "9F2C417BA0D3E6589174C2B0A6F3D81E5C7B2049A3F6D18B2E04C75961A3B8D0",
        paired: true,
      },
      {
        device_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13",
        device_name: MINI,
        fingerprint_short: "41:08:DA:36",
        fingerprint: "4108DA365B9C07E214F8A3D06B4E79C1F5083A2D6E94B07C15283DF6A90C4E71",
        paired: true,
      },
    ],
  },
  discovery_status: { enabled: true, visible: true },
  companion_status: {
    enabled: true,
    port: 8765,
    running: true,
    state: "running",
    access_url: "http://192.168.1.7:8765/mobile.html?token=prv-8f21c4d9",
  },
  internet_pairing_status: {
    generated_code: "K3M7-9QDA",
    peers: [
      { peer_id: "p-1", name: S("公司服务器", "Office server"), alias: "srv-a", online: true, last_seen: 1789342000, paired: true },
      { peer_id: "p-2", name: S("老家那台机器", "Home machine"), alias: "home-b", online: false, last_seen: 1789250000, paired: false },
    ],
    waiting: [{ peer_id: "t-9f2", name: "", since: 1789341000 }],
  },
  list_backups: {
    backups: [
      { path: "C:\\Users\\sukai\\AppData\\Roaming\\ClipSync\\backups\\backup-2026-09-14.zip", filename: "backup-2026-09-14.zip", date: "2026-09-14 09:12", size: 1843200 },
      { path: "C:\\Users\\sukai\\AppData\\Roaming\\ClipSync\\backups\\backup-2026-09-01.zip", filename: "backup-2026-09-01.zip", date: "2026-09-01 21:04", size: 1720320 },
    ],
  },
  read_logs: {
    logs: [
      "2026-09-14 09:12:03 INFO  clipsync.startup: sidecar ready in 412ms",
      `2026-09-14 09:12:04 INFO  clipsync.discovery: advertising as ${LOC} on 51888`,
      "2026-09-14 09:12:05 INFO  clipsync.pairing: MacBook Pro connected over LAN (192.168.1.22)",
      "2026-09-14 09:13:19 INFO  clipsync.history: captured 218 bytes, type=text, app=Code.exe",
      "2026-09-14 09:14:02 WARN  clipsync.transfer: retrying chunk 41/128 after 2.1s",
      "2026-09-14 09:14:11 INFO  clipsync.companion: phone panel served to 192.168.1.33",
    ],
  },
  autostart_status: true,
  update_status: { state: { phase: "idle", fraction: 0, downloaded: 0, total: 0, error: "", version: "", path: "" } },
  update_check: { available: false, latest: "1.0.5", current: "1.0.5", url: "", installable: false },
  // A reply at all means the install did not happen, so this is the shape the
  // preview shows when there is nothing newer to install.
  update_install: { ok: true, installed: false, reason: "up_to_date" },
  relay_delivery_status: {
    pending: 2,
    items: [
      { peer_id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30", name: MAC, count: 1, oldest: 1789340000 },
      { peer_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13", name: MINI, count: 1, oldest: 1789339000 },
    ],
  },
  // `direction` is up/down and `progress` is a 0–100 percentage, which is what
  // `internal/web/api/transfer.py` sends (`round(progress * 100, 1)`); the page
  // renders the number with a `%` and feeds the same value to `<progress
  // max="100">`.
  list_transfers: {
    active: [
      { id: "t-1", filename: S("2026-09-14 项目周报（含附录与数据表）.pptx", "2026-09-14 weekly report (with appendix).pptx"), size: 18874368, direction: "up", status: "transferring", progress: 42.6, speed: 2359296, eta: S("4 秒", "4s"), peer_id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30", timestamp: 1789342000 },
      { id: "t-2", filename: "IMG_20260914_091233.jpg", size: 4194304, direction: "down", status: "waiting_accept", progress: 0, speed: 0, eta: "", peer_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13", timestamp: 1789342010 },
    ],
    history: [
      { id: "t-3", filename: "archive-2026-09.zip", size: 52428800, direction: "up", status: "completed", progress: 100, speed: 0, peer_id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30", timestamp: 1789331000, path: "C:\\Users\\sukai\\Downloads\\ClipSync\\archive-2026-09.zip" },
      { id: "t-4", filename: "notes.md", size: 2048, direction: "down", status: "completed", progress: 100, speed: 0, peer_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13", timestamp: 1789320000, path: "C:\\Users\\sukai\\Downloads\\ClipSync\\notes.md" },
      { id: "t-5", filename: "installer.dmg", size: 157286400, direction: "down", status: "failed", progress: 18, speed: 0, peer_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13", timestamp: 1789310000, reason: S("对端在传输中断开", "The peer disconnected mid-transfer") },
    ],
    // A finished test with a real measurement, which is the one state where the
    // page shows both halves of the panel's reading: the number and the word for
    // it.  The size of the fixture's history is 200.0 MB across its three rows.
    speed_test: { state: "done", result_mbps: 42.5, chunks_sent: 64, total_chunks: 64 },
  },
  // The 下载 button on a row whose file lives on another device.  The answer is
  // only "the request went out"; the bytes arrive later as a transfer, so there
  // is nothing here to await.
  request_entry_files: { requested: true },
  list_chat_sessions: {
    sessions: [
      {
        session_id: "cs-1",
        peer_id: "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30",
        peer_name: MAC,
        fingerprint_short: "9F:2C:41:7B",
        status: "active",
        created_ts: 1757800000,
        last_activity_ts: 1757811900,
        unread: 2,
        online: true,
        last_preview: S("好的，我这边也试一下", "All right, let me try it here too"),
      },
      {
        session_id: "cs-2",
        peer_id: "b2d4e6f8-1a3c-5e70-9b81-2d4f6a8c0e13",
        peer_name: MINI,
        fingerprint_short: "41:08:DA:36",
        status: "active",
        created_ts: 1757700000,
        last_activity_ts: 1757799000,
        unread: 0,
        online: false,
        last_preview: S("[文件] 合影.png", "[File] group-photo.png"),
      },
      // The one session state that puts the optional band on screen, and the
      // only one whose row carries a name long enough to test the list's own
      // ellipsis — the same peer 附近设备 names in full underneath.
      {
        session_id: "cs-3",
        peer_id: "c3e5a7b9-2d4f-6a81-0c92-3e5a7b9d1f24",
        peer_name: OLD_LAPTOP,
        fingerprint_short: "7E:31:90:A2",
        status: "invited",
        created_ts: 1757814000,
        last_activity_ts: 1757814000,
        unread: 1,
        online: true,
        last_preview: "",
      },
    ],
    muted: [],
  },
  // A conversation with a real backlog rather than four lines: the pane is a
  // scrolling list, and a fixture short enough to fit on screen never made it
  // scroll, never showed the composer under a full list, and never showed a
  // file waiting to be accepted.  Every state the bubble can carry appears once
  // — incoming, outgoing, a failed send, a system line, a completed attachment
  // and one awaiting an answer.
  list_chat_messages: {
    messages: [
      { entry_id: "m-1", kind: "text", outgoing: false, ts: 1757810000, text: S("在吗？帮我看一下昨天那个配置", "Are you there? Can you look at that config from yesterday?"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x1" },
      { entry_id: "m-2", kind: "text", outgoing: true, ts: 1757810100, text: S("在的，稍等", "Here, one moment"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x2" },
      { entry_id: "m-3", kind: "file", outgoing: false, ts: 1757810200, text: "", text_key: "", fmt: {}, file_name: "config.json", file_size: 4096, mime: "application/json", status: "completed", fraction: 1, saved_path: "C:\\Users\\sukai\\Downloads\\ClipSync\\config.json", transfer_id: "t-9", msg_id: "x3" },
      { entry_id: "m-4", kind: "text", outgoing: false, ts: 1757811900, text: S("好的，我这边也试一下", "All right, let me try it here too"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x4" },
      { entry_id: "m-5", kind: "system", outgoing: false, ts: 1757812000, text: S(`${MINI} 已加入会话`, `${MINI} joined the conversation`), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "", fraction: 1, saved_path: "", transfer_id: "", msg_id: "" },
      { entry_id: "m-6", kind: "text", outgoing: true, ts: 1757812100, text: S("配置文件我改好了，把 sync.interval 从 30 调到 5，日志里那两个超时就没了。你再跑一遍看看？", "I fixed the config — sync.interval from 30 down to 5, and those two timeouts dropped out of the log. Run it again and see?"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x4" },
      { entry_id: "m-7", kind: "text", outgoing: true, ts: 1757812200, text: "https://github.com/kai3316/copyboard/pull/214", text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "failed", fraction: 0, saved_path: "", transfer_id: "", msg_id: "x6" },
      { entry_id: "m-8", kind: "text", outgoing: false, ts: 1757812800, text: S("看到了，interval 那段和我猜的一样", "Saw it — the interval block, just as I guessed"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x7" },
      { entry_id: "m-9", kind: "file", outgoing: true, ts: 1757812900, text: "", text_key: "", fmt: {}, file_name: "copyboard-1.0.2-win.zip", file_size: 18790400, mime: "application/zip", status: "sending", fraction: 0.42, saved_path: "", transfer_id: "t-11", msg_id: "x8" },
      { entry_id: "m-10", kind: "text", outgoing: false, ts: 1757813000, text: S("装上了，Windows 这边没有问题", "Installed — no trouble on the Windows side"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x9" },
      { entry_id: "m-11", kind: "text", outgoing: false, ts: 1757813100, text: S("Mac 上还是提示已损坏，是不是没签名？", "On the Mac it still says the app is damaged. Is it unsigned?"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x10" },
      { entry_id: "m-12", kind: "text", outgoing: true, ts: 1757813200, text: S("对，现在只能 ad-hoc 签名。右键打开，或者跑一次 xattr -dr com.apple.quarantine /Applications/ClipSync.app", "Right — ad-hoc signing is all it has for now. Right-click to open, or run xattr -dr com.apple.quarantine /Applications/ClipSync.app once"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x11" },
      { entry_id: "m-13", kind: "file", outgoing: false, ts: 1757813300, text: "", text_key: "", fmt: {}, file_name: S("截图 2026-09-14 091233.png", "screenshot 2026-09-14 091233.png"), file_size: 262144, mime: "image/png", status: "await_accept", fraction: 0, saved_path: "", transfer_id: "t-12", msg_id: "x12" },
      { entry_id: "m-14", kind: "text", outgoing: true, ts: 1757813400, text: S("嗯，那就先这样，明天再看", "Fine, let's leave it there and look again tomorrow"), text_key: "", fmt: {}, file_name: "", file_size: 0, mime: "", status: "sent", fraction: 1, saved_path: "", transfer_id: "", msg_id: "x13" },
    ],
  },
  // The catalog is what `ai_profiles.TOOLS` holds, so the four checkboxes, their
  // labels and their entry lists are the real ones.
  ai_profiles: {
    ok: true,
    tools: [
      { key: "claude_code", label: "Claude Code", entries: [
        { id: "memory", path: "~/.claude/CLAUDE.md", kind: "file" },
        { id: "settings", path: "~/.claude/settings.json", kind: "file" },
        { id: "skills", path: "~/.claude/skills", kind: "dir" },
        { id: "commands", path: "~/.claude/commands", kind: "dir" },
        { id: "agents", path: "~/.claude/agents", kind: "dir" },
      ] },
      { key: "codex", label: "Codex", entries: [
        { id: "config", path: "~/.codex/config.toml", kind: "file" },
      ] },
      { key: "cursor", label: "Cursor", entries: [
        { id: "rules", path: "~/.cursor/rules", kind: "dir" },
        { id: "commands", path: "~/.cursor/commands", kind: "dir" },
      ] },
      { key: "gemini", label: "Gemini CLI", entries: [
        { id: "settings", path: "~/.gemini/settings.json", kind: "file" },
        { id: "memory", path: "~/.gemini/GEMINI.md", kind: "file" },
        { id: "skills", path: "~/.gemini/skills", kind: "dir" },
        { id: "commands", path: "~/.gemini/commands", kind: "dir" },
      ] },
    ],
    // Cursor is enabled but has nothing on disk, so the catalog shows both
    // states a checkbox can be in without inventing a fourth tool.
    enabled: ["claude_code", "gemini"],
    custom_paths: [],
  },
  // The peer half of the AI config page: `{peers, refreshed, local}`, keyed on
  // the paired device ids, exactly as `ai_inventory` answers.  It used to be
  // `{tools, items, roots}` — a shape nothing has ever returned, so the picker's
  // per-device counts read zero and the peer list photographed empty.  The
  // entries are the remote side's own relative paths, which is what the local
  // listing is compared against.
  ai_inventory: {
    ok: true,
    peers: {
      "3a91f0c4-77b2-4d5e-8f01-6b2c9d4e7a30": {
        legacy: false,
        entries: [
          { tool: "claude_code", root: "memory", rel_path: "CLAUDE.md", size: 4711, mtime: 1757810000, sha256: "b77c1e5a9d3f2048", is_dir: false },
          { tool: "claude_code", root: "settings", rel_path: "settings.json", size: 2310, mtime: 1757809000, sha256: "c1d2e3f4a5b6c7d8", is_dir: false },
          { tool: "claude_code", root: "skills", rel_path: "code-review", size: 0, mtime: 1757808000, sha256: "", is_dir: true },
          { tool: "claude_code", root: "skills", rel_path: "code-review/SKILL.md", size: 1893, mtime: 1757808000, sha256: "d4e5f6a7b8c9d0e1", is_dir: false },
          { tool: "claude_code", root: "skills", rel_path: "release-notes/SKILL.md", size: 2201, mtime: 1757807000, sha256: "e5f6a7b8c9d0e1f2", is_dir: false },
          { tool: "claude_code", root: "agents", rel_path: "reviewer.md", size: 980, mtime: 1757806000, sha256: "f6a7b8c9d0e1f2a3", is_dir: false },
          { tool: "codex", root: "config", rel_path: "config.toml", size: 1420, mtime: 1757805000, sha256: "a7b8c9d0e1f2a3b4", is_dir: false },
          { tool: "gemini", root: "memory", rel_path: "GEMINI.md", size: 3120, mtime: 1757804000, sha256: "b8c9d0e1f2a3b4c5", is_dir: false },
        ],
      },
    },
    refreshed: [],
    local: { collected_at: 1757812000, entry_count: 15, tools: ["claude_code", "codex", "gemini"], custom_paths: [] },
  },
  diagnostics_report: {
    v2: true,
    summary: "warn",
    checks: [
      { id: "engine", ok: true, detail: S("后台进程运行中", "Background process running"), detail_text: S("后台进程运行中", "Background process running") },
      { id: "firewall", ok: true, detail: S("局域网端口 51888 已放通", "LAN port 51888 is open"), detail_text: S("局域网端口 51888 已放通", "LAN port 51888 is open") },
      {
        id: "relay",
        ok: false,
        detail: S("互联网中继未连接", "Internet relay not connected"),
        detail_text: S("互联网中继未连接", "Internet relay not connected"),
        guidance: S("在设置里填写中继地址后重试", "Enter a relay address in Settings and try again"),
        guidance_text: S("在设置里填写中继地址后重试", "Enter a relay address in Settings and try again"),
      },
    ],
    groups: {
      network: {
        label_key: S("网络", "network"),
        label_text: S("网络", "Network"),
        items: [
          { id: "lan", status: "ok", detail: "192.168.1.7:51888", detail_text: "192.168.1.7:51888" },
          { id: "relay", status: "warn", detail: S("未连接", "Not connected"), detail_text: S("未连接", "Not connected"), hint: S("在设置里填写中继地址", "Enter a relay address in Settings"), hint_text: S("在设置里填写中继地址", "Enter a relay address in Settings") },
        ],
      },
      storage: {
        label_key: S("存储", "storage"),
        label_text: S("存储", "Storage"),
        items: [
          { id: "data_dir", status: "ok", detail: "C:\\Users\\sukai\\AppData\\Roaming\\ClipSync", detail_text: "C:\\Users\\sukai\\AppData\\Roaming\\ClipSync" },
          { id: "history_size", status: "fail", detail: S("历史目录不可写", "History directory is not writable"), detail_text: S("历史目录不可写", "History directory is not writable"), hint: S("检查磁盘剩余空间", "Check the free space on the disk"), hint_text: S("检查磁盘剩余空间", "Check the free space on the disk") },
        ],
      },
    },
    discovery_running: true,
    server_running: true,
    connected_count: 1,
    paired_count: 2,
    web_companion_running: true,
    web_port: 8765,
    lan_ip: "192.168.1.7",
    os: "Windows 11",
    version: "1.0.5",
  },
  get_overview: {
    connected_count: 1,
    paired_count: 2,
    discovered_count: 4,
    connected_names: ["MacBook Pro"],
    history_count: 1284,
    history_today: 37,
    history_pinned: 6,
    history_images: 214,
    active_transfers: 2,
    transfer_completed: 341,
    discovering: true,
    visible: true,
    sync_enabled: true,
    web_enabled: true,
    uptime_seconds: 5400,
    local_ip: "192.168.1.7",
    port: 51888,
    platform: "Windows",
    version: "1.0.5",
    network_type: "lan",
    network_detail: S("有线连接 · 1 Gbps", "Wired · 1 Gbps"),
    // No `recent_items` here: the feed is the newest history rows, and
    // `install-host.ts` serves it by slicing the same generated rows the history
    // page reads, so a second hand-written copy would be one more thing to keep
    // in step with `Row` for nothing — it was never rendered.
  },
  list_favorites: {
    items: [
      { id: "f-1", title: S("公司 VPN 地址", "Office VPN address"), preview: S("vpn.example.com — 账号 kai，密码见 1Password", "vpn.example.com — user kai, password in 1Password"), group: S("工作", "Work"), position: 0, created: 1757000000, updated: 1757000000 },
      { id: "f-2", title: S("部署命令", "Deploy command"), preview: "docker compose -f docker-compose.prod.yml up -d --build", group: S("工作", "Work"), position: 1, created: 1757000000, updated: 1757000000 },
      { id: "f-3", title: S("身份证号", "National ID number"), preview: "110101199001011234", group: S("私人", "Personal"), position: 0, created: 1757000000, updated: 1757000000 },
      { id: "f-4", title: S("常用 SQL：按天统计", "Everyday SQL: count by day"), preview: "SELECT date(created_at) d, count(*) FROM history GROUP BY d ORDER BY d DESC", group: S("工作", "Work"), position: 2, created: 1757000000, updated: 1757000000 },
      { id: "f-5", title: S("服务器的 SSH 公钥", "Server's SSH public key"), preview: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH8k...", group: S("工作", "Work"), position: 3, created: 1757000000, updated: 1757000000 },
    ],
    total: 5,
    offset: 0,
    groups: [S("工作", "Work"), S("私人", "Personal"), S("临时", "Scratch")],
    group_counts: { [S("工作", "Work")]: 4, [S("私人", "Personal")]: 1, [S("临时", "Scratch")]: 0 },
    library_total: 5,
    session_id: "s-1",
    seq: 42,
  },
  update_settings: { ok: true },
  set_sync_enabled: { enabled: true },
  pause_sync: { enabled: true, until: 1757814000 },
  resume_sync: { enabled: true },
  copy_text: { copied: true },
  copy_history: { copied: true },
  copy_favorite: { copied: true },
  push_text: { ok: true, len: 12, sent: true },
};
