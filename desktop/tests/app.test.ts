import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";
import { bridge } from "../src/api/bridge";
import { closeContextMenu } from "../src/lib/context-menu";
import type { HistoryItem, Overview } from "../src/api/types";
import { setLocale, t } from "../src/i18n";

/** A history row as the sidecar sends one, with whatever a case asserts on.
 *
 * The row's provenance and paste count are part of every payload the real
 * sidecar builds, so spelling them out per case made each new field a change to
 * every literal here instead of to the one the case is about.
 */
function historyRow(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    id: "1", timestamp: 1, content_type: "TEXT", pinned: false, preview: "",
    source_name: "", source_app: "", source_title: "", paste_count: 0,
    ...overrides,
  };
}

/** A history page the sidecar would answer with, for the rows given. */
function historyPage(items: HistoryItem[]) {
  // A page with rows means there is history, which is what puts the chip row on
  // screen; a test that needs the chip row over an empty list says so itself.
  return {
    session_id: "s", seq: 0, offset: 0, total: items.length, items,
    has_history: items.length > 0,
  };
}

/** The counters the overview page is drawn from, as the sidecar sends them.
 *
 * Zero everywhere is the quietest answer — no devices, nothing copied, nothing
 * discovered — so a case that is about the page says which number it is about
 * and every other case gets a page that renders without claiming anything.
 */
function overview(overrides: Partial<Overview> = {}): Overview {
  return {
    connected_count: 0, paired_count: 0, discovered_count: 0, connected_names: [],
    history_count: 0, history_today: 0, history_pinned: 0, history_images: 0,
    active_transfers: 0, transfer_completed: 0, discovering: false, visible: false,
    sync_enabled: false, web_enabled: false, uptime_seconds: 0, local_ip: "",
    port: 8765, platform: "Windows", version: "test", network_type: "lan",
    network_detail: "", recent_items: [],
    ...overrides,
  };
}

vi.mock("../src/api/bridge", () => ({
  inDesktop: () => true,
  bridge: {
    subscribe: vi.fn().mockResolvedValue(() => {}),
    status: vi.fn().mockResolvedValue({
      version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: "not_started",
    }),
    // The window opens on the overview, so every case mounts it.  A count of
    // zero everywhere is the quietest answer it can give; a case about the
    // page itself says what it wants to see.
    overview: vi.fn().mockResolvedValue(overview({})),
    devices: vi.fn().mockResolvedValue({ items: [] }),
    history: vi.fn().mockResolvedValue(historyPage([historyRow({
      preview: '<img src=x onerror="window.injected=true">',
    })])),
    quit: vi.fn(),
    minimize: vi.fn().mockResolvedValue(null),
    settings: vi.fn().mockResolvedValue({ settings: {} }),
    pauseSync: vi.fn().mockResolvedValue({ enabled: false, until: 0 }),
    resumeSync: vi.fn().mockResolvedValue({ enabled: true }),
    updateSettings: vi.fn().mockResolvedValue({}),
    updateAiProfiles: vi.fn().mockResolvedValue({}),
    autostartStatus: vi.fn().mockResolvedValue(false),
    aiProfiles: vi.fn().mockResolvedValue({ tools: [], enabled: [], custom_paths: [] }),
    // `enabled` travels with every status the sidecar answers, and the card's
    // switch and every control under it are read from it.
    internetPairingStatus: vi.fn().mockResolvedValue({ peers: [], enabled: true }),
    // The answer names the provisional tag and admits the pairing is half done,
    // so the panel has no success to report even when the call succeeds.
    enterInternetPairingCode: vi.fn().mockResolvedValue({ peer_id: "", waiting: true }),
    relayDeliveryStatus: vi.fn().mockResolvedValue({ pending: 0, items: [] }),
    // The probe answered nothing, which is the quietest answer: a case about
    // the button says what it wants the test to have found.
    testRelayBrokers: vi.fn().mockResolvedValue({ results: [], reachable: 0, total: 0 }),
    chatDevices: vi.fn().mockResolvedValue({ devices: [] }),
    chatSessions: vi.fn().mockResolvedValue({ sessions: [], muted: [] }),
    chatMessages: vi.fn().mockResolvedValue({ messages: [] }),
    markChatRead: vi.fn().mockResolvedValue({ ok: true }),
    aiLocal: vi.fn(),
    aiInventory: vi.fn(),
    aiPreview: vi.fn(),
    aiPull: vi.fn(),
    translate: vi.fn(),
    readHistoryText: vi.fn(),
    // The hover card's read.  It answers an empty card rather than an error for
    // a row there is nothing to show about, so the default is the quietest
    // answer a case that is not about the card can get.
    previewHistoryEntry: vi.fn().mockResolvedValue(
      { kind: "", image: "", width: 0, height: 0, files: [], total: 0 },
    ),
    openHistoryLink: vi.fn().mockResolvedValue({ opened: true, url: "https://example.com" }),
    requestEntryFiles: vi.fn().mockResolvedValue({ requested: true }),
    // Opening the devices page reads the phone service's status whatever
    // sub-tab is showing, so an unresolved mock left every devices-page case
    // with a spurious error band ("cannot read properties of undefined").
    companionStatus: vi.fn().mockResolvedValue({
      enabled: false, port: 8765, running: false, state: "off", access_url: null,
    }),
    configureCompanion: vi.fn(),
    chooseFile: vi.fn(),
    shareFileToPhone: vi.fn().mockResolvedValue({
      ok: true, name: "notes.txt", path: "C:/share/notes.txt", size: 5,
    }),
    listBackups: vi.fn().mockResolvedValue({ backups: [] }),
    restoreBackup: vi.fn(),
    confirmPairing: vi.fn().mockResolvedValue({ paired: false, status: "confirmed_waiting" }),
    unpairDevice: vi.fn().mockResolvedValue({ accepted: true }),
    connectDevice: vi.fn().mockResolvedValue({ accepted: true }),
    disconnectDevice: vi.fn().mockResolvedValue({ disconnected: true }),
    forgetDevice: vi.fn().mockResolvedValue({ forgotten: true }),
    offerDeviceUpdate: vi.fn().mockResolvedValue({ sent: true }),
    fetchDeviceUpdate: vi.fn().mockResolvedValue({ sent: true }),
    restoreDevice: vi.fn().mockResolvedValue({ restored: true }),
    purgeDevice: vi.fn().mockResolvedValue({ purged: true }),
    testDevice: vi.fn(),
    deviceCerts: vi.fn().mockResolvedValue({ devices: [] }),
    retrustDevice: vi.fn().mockResolvedValue({ trusted: true }),
    sendUrl: vi.fn().mockResolvedValue({ sent: true, device_id: "p" }),
    pushText: vi.fn().mockResolvedValue({ ok: true, len: 5, sent: true }),
    readLogs: vi.fn().mockResolvedValue({ logs: ["line 1", "line 2"], problems: ["problem 1"] }),
    exportLogs: vi.fn().mockResolvedValue({ path: "C:/logs/clipsync.log", bytes: 12 }),
    restartApp: vi.fn().mockResolvedValue(null),
    factoryReset: vi.fn().mockResolvedValue(null),
    diagnosticsReport: vi.fn().mockResolvedValue({
      v2: true, summary: "ok", checks: [], groups: {}, discovery_running: false,
      server_running: true, connected_count: 0, paired_count: 0,
      web_companion_running: false, web_port: 8080, lan_ip: "", os: "Windows",
      version: "test",
    }),
    diagnosticsRequest: vi.fn().mockResolvedValue({ ok: true }),
    updateCheck: vi.fn().mockResolvedValue({ available: false, latest: "", current: "test", url: "" }),
    updateStatus: vi.fn().mockResolvedValue({ state: { phase: "idle", fraction: 0,
      downloaded: 0, total: 0, error: "", version: "", path: "" } }),
    updateDownload: vi.fn().mockResolvedValue({ ok: true, started: true, error: null }),
    updateOpenFolder: vi.fn().mockResolvedValue({ ok: true }),
    updateInstall: vi.fn().mockResolvedValue({ ok: true, installed: false, reason: "up_to_date" }),
    openDataFolder: vi.fn().mockResolvedValue({ ok: true, folder: "C:/data" }),
    openAboutLink: vi.fn().mockResolvedValue({ ok: true, url: "https://github.com/kai3316/clipsync" }),
    onMenuAction: vi.fn().mockResolvedValue(() => {}),
    onFileDrop: vi.fn().mockResolvedValue(() => {}),
    // The transfers page polls this the moment it is opened — including when a
    // drop is what opened it.
    transfers: vi.fn().mockResolvedValue({ active: [], history: [], speed_test: {} }),
    sendFiles: vi.fn().mockResolvedValue({ transfer_id: "t1" }),
    companionQr: vi.fn().mockResolvedValue({
      ok: true, url: "http://10.0.0.2:8080/mobile.html?token=t",
      qr: "data:image/png;base64,AAAA",
    }),
    discoveryStatus: vi.fn().mockResolvedValue({ enabled: true, visible: true }),
    setDiscoveryEnabled: vi.fn().mockResolvedValue({ enabled: false, visible: true }),
    setDiscoveryVisible: vi.fn().mockResolvedValue({ enabled: true, visible: false }),
    copyHistory: vi.fn().mockResolvedValue({ copied: true }),
    copyText: vi.fn().mockResolvedValue({ copied: true }),
    deleteHistory: vi.fn().mockResolvedValue({ deleted: true }),
    batchPinHistory: vi.fn().mockResolvedValue({ updated: 1 }),
    batchDeleteHistory: vi.fn().mockResolvedValue({ deleted: 1 }),
    batchFavoriteHistory: vi.fn().mockResolvedValue({ added: 1, ids: ["f1"] }),
    clearHistory: vi.fn().mockResolvedValue({ cleared: 1 }),
  },
}));

/** Open one of a page's panels.
 *
 * The devices page and the AI page are each two rows of navigation: the
 * sidebar picks the page, and the strip on it picks which of the page's panels
 * is rendered.  A panel that is not the open one is not in the tree at all, so
 * a test that reaches into one has to open it first — the settings page is the
 * other way round, keeping its cards as hidden nodes because its search has to
 * count them.
 */
/** The chord that opens each page, in the order the sidebar draws them.
 *
 * The window opens on the overview — the legacy dashboard did, and the phone's
 * Companion panel still does — so a case that reaches into another page's rows
 * has to go there first.  The digits count the sidebar's rows, which is the
 * same table the window writes into its tooltips and its `aria-keyshortcuts`.
 */
const PAGE_CHORDS = {
  overview: "1", history: "2", devices: "3", favorites: "4",
  transfers: "5", chat: "6", ai: "7", settings: "8",
} as const;

/** Mount the window on the page the case is about.  `attachTo` is passed
 * through for the cases that are about where the keyboard goes.
 *
 * The chord is dispatched rather than the sidebar row clicked so that the page
 * is where it is because the window says so, and the caller's own
 * `flushPromises` is what paints it — the same one the mount line used to be
 * followed by. */
function mountOn(page: keyof typeof PAGE_CHORDS, attachTo?: HTMLElement) {
  const app = attachTo ? mount(App, { attachTo }) : mount(App);
  if (page !== "overview") {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: PAGE_CHORDS[page], ctrlKey: true }));
  }
  return app;
}

async function openPanel(app: VueWrapper, label: string) {
  const tab = app.findAll(".page-tabs button").find(node => node.text() === label);
  if (!tab) throw new Error(`no panel tab named ${label}`);
  await tab.trigger("click");
  await flushPromises();
}

describe("history rendering", () => {
  it("refreshes cached AI inventory when the selected peer replies without requesting another broadcast", async () => {
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiInventory).mockReset()
      // Opening the settings page primes the picker with a cached read of every
      // peer, before the reader has chosen one.
      .mockResolvedValueOnce({ peers: {} })
      // The reader's own read, which asks the selected peer to re-send.
      .mockResolvedValueOnce({ peers: {}, refreshed: ["a"] })
      .mockResolvedValueOnce({ peers: { a: { entries: [
        { tool: "custom", root: "", rel_path: "arrived.md" },
      ] } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "aiconfig.file",
        data: { type: "aiconfig_inventory", peer_id: "other" } });
      await flushPromises();
      // The two reads so far are the priming one and the reader's own; a peer
      // that is not the selected one costs no read of its own.
      expect(bridge.aiInventory).toHaveBeenCalledTimes(2);
      emit({ type: "event", session_id: "s", name: "aiconfig.file",
        data: { type: "aiconfig_inventory", peer_id: "a" } });
      await flushPromises();
      expect(bridge.aiInventory).toHaveBeenLastCalledWith(false, "a");
      expect(app.find('[aria-label="预览 arrived.md"]').exists()).toBe(true);
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });
  it("loads and saves application filtering without discarding disabled patterns", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      app_filter_enabled: true, app_filter_mode: "whitelist",
      app_filter_list: ["editor.exe", "code*"],
    } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect((app.get('[aria-label="应用过滤进程列表"]').element as HTMLTextAreaElement).value).toBe("editor.exe\ncode*");
      await app.get('[aria-label="应用过滤模式"]').setValue("blacklist");
      await app.get('[aria-label="应用过滤进程列表"]').setValue(" secret* \n\nchrome.exe");
      await app.get('[aria-label="启用应用过滤"]').setValue(false);
      expect(app.get('[aria-label="应用过滤模式"]').attributes("disabled")).toBeDefined();
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        app_filter_enabled: false, app_filter_mode: "blacklist",
        app_filter_list: ["secret*", "chrome.exe"],
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });
  it("answers the window's own chords, and only when no dialog owns the keyboard", async () => {
    // Attached, because this case is about where the keyboard goes: an
    // unfocused-app mount keeps the tree out of the document, and `focus()`
    // then has nothing to move `document.activeElement` to.
    const app = mount(App, { attachTo: document.body });
    const press = async (key: string, modifiers: { ctrlKey?: boolean } = { ctrlKey: true }) => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key, ...modifiers }));
      await flushPromises();
    };
    try {
      await flushPromises();
      // The numbers count the sidebar rows, in the order they are drawn, and
      // the row says which number it is — the chord is only discoverable if the
      // sidebar advertises it.  Settings is drawn after the seven content
      // pages, so the pages count 1–7 and settings is 8.
      const transfers = app.findAll("button").find(button => button.attributes("aria-label") === "文件传输")!;
      expect(transfers.attributes("aria-keyshortcuts")).toBe("Control+5");
      expect(transfers.attributes("title")).toContain("Ctrl+5");
      const aiRow = app.findAll("button").find(button => button.attributes("aria-label") === "AI 配置")!;
      expect(aiRow.attributes("aria-keyshortcuts")).toBe("Control+7");
      // The first row is the page the window opens on, so its chord is the way
      // back to where a reader who has wandered off started.
      const dashboard = app.findAll("button").find(button => button.attributes("aria-label") === "概览")!;
      expect(dashboard.attributes("aria-keyshortcuts")).toBe("Control+1");
      await press("5");
      expect(app.find(".transfers-view").exists()).toBe(true);
      await press("4");
      expect(app.find(".favorites-view").exists()).toBe(true);
      await press("1");
      expect(app.find(".overview-view").exists()).toBe(true);
      // Ctrl+F goes to the search box, from wherever it is pressed: the
      // dashboard in front of us has none of its own, so the history page's is
      // the one it reaches.
      await press("f");
      expect(app.find(".history-list").exists()).toBe(true);
      expect(document.activeElement).toBe(app.get('[aria-label="搜索历史记录"]').element);
      // The AI page is a page of content, and the settings page is not: the
      // chord reaches each of them and leaves the other alone.
      await press("7");
      expect(app.find(".ai-page").exists()).toBe(true);
      expect(app.find(".settings-panel").exists()).toBe(false);
      await press("8");
      expect(app.find(".settings-panel").exists()).toBe(true);
      // Preferences has the chord everyone tries first.
      await press(",");
      expect(app.find(".settings-panel").exists()).toBe(true);
      // Ctrl+F finds on the page being read, so from settings it reaches the
      // settings search instead of throwing the reader at the history list.
      await press("f");
      expect(app.find(".settings-panel").exists()).toBe(true);
      expect(document.activeElement).toBe(app.get('[aria-label="搜索设置…"]').element);
      // An open dialog owns the keyboard: nothing behind it may move.
      const dialog = document.createElement("dialog");
      dialog.setAttribute("open", "");
      document.body.append(dialog);
      await press("1");
      expect(app.find(".settings-panel").exists()).toBe(true);
      dialog.remove();
      // And a bare key is not a chord.
      await press("2", {});
      expect(app.find(".settings-panel").exists()).toBe(true);
    } finally {
      app.unmount();
    }
  });
  it("drives the history list from the keyboard, the way the legacy panel did", async () => {
    // Three rows, because every claim here is about moving between them.
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "a", preview: "first" }),
      historyRow({ id: "b", preview: "second" }),
      historyRow({ id: "c", preview: "third" }),
    ]));
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    // The arrow keys belong to the history list, so the window has to be on it.
    const app = mountOn("history", document.body);
    const press = async (key: string, modifiers: Record<string, boolean> = {}) => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key, ...modifiers }));
      await flushPromises();
    };
    /** Which row the cursor is on, read from the row itself: the highlight is
     * a class on the row, and the record it sits on is the only thing that
     * says the class landed where the arrow keys meant it to. */
    const cursor = () => {
      const row = app.find(".history-row--kbd");
      return row.exists() ? row.find(".history-content p").text() : null;
    };
    try {
      await flushPromises();
      // Nothing is chosen before the reader asks for anything.
      expect(cursor()).toBeNull();
      // The first ↓ enters at the top rather than the second row.
      await press("ArrowDown");
      expect(cursor()).toBe("first");
      await press("ArrowDown");
      expect(cursor()).toBe("second");
      await press("ArrowUp");
      expect(cursor()).toBe("first");
      // ↑ at the top and ↓ at the bottom clamp rather than wrap.
      await press("ArrowUp");
      expect(cursor()).toBe("first");
      await press("ArrowDown");
      await press("ArrowDown");
      await press("ArrowDown");
      expect(cursor()).toBe("third");
      // Enter copies the row under the cursor, not the first row.
      await press("Enter");
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("c");
      // Delete opens the same confirm the row's own trash does — it must not
      // remove anything on the keypress alone.
      await press("Delete");
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
      expect(bridge.deleteHistory).not.toHaveBeenCalled();
      // Ctrl+A takes the whole visible page.
      await press("a", { ctrlKey: true });
      expect(app.find('[role="status"]').text()).toContain("3");
      // A reader typing in the search box owns the keys: no cursor moves, and
      // Ctrl+A means "select this text" rather than "select every record".
      const search = app.get('[aria-label="搜索历史记录"]');
      (search.element as HTMLInputElement).focus();
      await press("ArrowDown");
      expect(cursor()).toBe("third");
      await press("a", { ctrlKey: true });
      expect(app.find('[role="status"]').text()).toContain("3");
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([
        historyRow({ preview: '<img src=x onerror="window.injected=true">' }),
      ]));
    }
  });

  /**
   * The relay test is the one control on the settings page that asks about a
   * broker rather than storing it, and it was the one capability the audit found
   * on the old web panel with no command behind it in this shell
   * (`settings-panel.js` 测试 → `POST /api/internetpair/test`).
   *
   * What it tests is the **staged** list, not the saved one: a reader checks
   * what they are about to save, and finds out a broker is unreachable before it
   * is written into the config the relay runs on.
   */
  it("probes the staged relay brokers and reports each one", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      relay_brokers: ["wss://one:8884/mqtt"],
      relay_private_brokers: ["wss://private:8884/mqtt"],
    } });
    vi.mocked(bridge.testRelayBrokers).mockResolvedValue({
      results: [
        { endpoint: "wss://private:8884/mqtt", ok: true, latency_ms: 42 },
        { endpoint: "wss://one:8884/mqtt", ok: false, latency_ms: null, detail: "connection refused" },
      ],
      reachable: 1,
      total: 2,
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // A broker staged in both lists is one broker, as it was for the panel.
      await app.get('[aria-label="公共中继地址"]').setValue("wss://one:8884/mqtt");
      const test = () => app.findAll("button").find(button => button.text().includes("测试中继连接"))!;
      await test().trigger("click");
      await flushPromises();
      expect(bridge.testRelayBrokers).toHaveBeenCalledExactlyOnceWith([
        "wss://one:8884/mqtt", "wss://private:8884/mqtt",
      ]);
      // Both counts, and a row per broker: which one answered, how fast, and
      // for the one that did not, the probe's own reason.
      expect(app.text()).toContain("2 个中继中 1 个可达");
      expect(app.get(".relay-test").text()).toContain("42 毫秒");
      expect(app.get(".relay-test").text()).toContain("connection refused");
      const rows = app.findAll(".relay-test-list > li");
      expect(rows.map(row => row.classes().join(" "))).toEqual([
        expect.stringContaining("relay-test--ok"),
        expect.stringContaining("relay-test--fail"),
      ]);
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
      vi.mocked(bridge.testRelayBrokers).mockReset().mockResolvedValue({ results: [], reachable: 0, total: 0 });
    }
  });

  it("round-trips the advanced network settings and clamps out-of-range numbers", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      port: 19990, service_type: "_clipsync._tcp.local.", web_history_limit: 30,
      sync_debounce: 0.3, clipboard_poll_interval: 1, file_receive_dir: "",
      transfer_timeout: 120, max_reconnect_attempts: 10, log_level: "INFO",
      low_memory_mode: false, retry_capture_enabled: true, dedup_method: "sha256", data_dir: "",
    } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect((app.get('[aria-label="TCP 端口"]').element as HTMLInputElement).value).toBe("19990");
      expect((app.get('[aria-label="重试剪贴板捕获"]').element as HTMLInputElement).checked).toBe(true);
      await app.get('[aria-label="TCP 端口"]').setValue("20000");
      // A cleared number falls back to the config default and an out-of-range
      // one clamps, so the save never trips the sidecar's bounds.
      await app.get('[aria-label="同步去抖（秒）"]').setValue("");
      await app.get('[aria-label="显示历史条数"]').setValue("9999");
      await app.get('[aria-label="mDNS 服务类型"]').setValue("   ");
      await app.get('[aria-label="日志级别"]').setValue("ERROR");
      await app.get('[aria-label="去重方式"]').setValue("simple");
      await app.get('[aria-label="低内存模式（更少预览、更慢轮询）"]').setValue(true);
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        port: 20000,
        service_type: "_clipsync._tcp.local.",
        web_history_limit: 500,
        sync_debounce: 0.3,
        log_level: "ERROR",
        dedup_method: "simple",
        low_memory_mode: true,
        retry_capture_enabled: true,
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });
  it("submits the encryption password only when it passes the strength rules", async () => {
    // jsdom implements neither method; the watchers call both directions.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { encryption_enabled: false, password_set: false },
    });
    vi.mocked(bridge.updateSettings).mockResolvedValue({ password_set: true });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const saveButton = () => app.findAll("button").find(button => button.text().includes("保存设置"))!;
      await app.get('[aria-label="加密密码"]').setValue("short");
      // A weak password blocks the save and lists the unmet rules.
      expect(saveButton().attributes("disabled")).toBeDefined();
      expect(app.text()).toContain("长度至少 12 位");
      await app.get('[aria-label="加密密码"]').setValue("Str0ng-Passw0rd!");
      await app.get('[aria-label="确认加密密码"]').setValue("Str0ng-Passw0rd!");
      expect(saveButton().attributes("disabled")).toBeUndefined();
      await saveButton().trigger("submit");
      await flushPromises();
      const payload = vi.mocked(bridge.updateSettings).mock.calls.at(-1)![0];
      expect(payload.password).toBe("Str0ng-Passw0rd!");
      // The toggle is unchanged, so it must not re-wire live encryption.
      expect("encryption_enabled" in payload).toBe(false);
      // The typed value is never echoed back: drop it and follow the response.
      expect((app.get('[aria-label="加密密码"]').element as HTMLInputElement).value).toBe("");
      expect(app.text()).toContain("已设置加密密码");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
    }
  });
  it("resets every data file only after the factory-reset dialog is confirmed", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text().includes("恢复出厂设置"))!.trigger("click");
      await flushPromises();
      expect(bridge.factoryReset).not.toHaveBeenCalled();
      const dialog = app.get('[aria-labelledby="factory-reset-title"]');
      await dialog.findAll("button").find(button => button.text() === "确认重置")!.trigger("click");
      await flushPromises();
      expect(bridge.factoryReset).toHaveBeenCalledTimes(1);
    } finally {
      app.unmount();
    }
  });

  it("loads the saved theme and changes it only after successful settings persistence", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { appearance_mode: "dark", auto_start: false },
    });
    vi.mocked(bridge.updateSettings)
      .mockRejectedValueOnce(new Error("save failed"))
      .mockResolvedValueOnce({});
    const app = mount(App);
    try {
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("dark");
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const theme = app.findAll("label").find(label => label.text().startsWith("主题"))!.get("select");
      await theme.setValue("light");
      expect(document.documentElement.dataset.theme).toBe("dark");
      const save = app.findAll("button").find(button => button.text() === "保存设置")!;
      await save.trigger("submit");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("dark");
      await save.trigger("submit");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("light");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
      delete document.documentElement.dataset.theme;
    }
  });

  it("does not restore a selected backup until explicitly confirmed", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.chooseFile).mockResolvedValueOnce("C:/fixture/backup.zip");
    vi.mocked(bridge.restoreBackup).mockClear().mockResolvedValueOnce({ restored: true });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "选择备份恢复")!.trigger("click");
      await flushPromises();
      expect(bridge.restoreBackup).not.toHaveBeenCalled();
      expect(app.get('[aria-labelledby="restore-title"]').text()).toContain("C:/fixture/backup.zip");
      vi.mocked(bridge.settings).mockResolvedValueOnce({
        settings: { appearance_mode: "dark", device_name: "Restored device" },
      });
      await app.get('[aria-labelledby="restore-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.restoreBackup).toHaveBeenCalledExactlyOnceWith("C:/fixture/backup.zip");
      expect(app.text()).toContain("恢复完成");
      expect(document.documentElement.dataset.theme).toBe("dark");
      expect(app.findAll("input").some(input =>
        (input.element as HTMLInputElement).value === "Restored device")).toBe(true);
    } finally { app.unmount(); }
  });

  it("requires confirmation to rotate the Companion token without applying an edited port", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const running = { enabled: true, running: true, port: 8080, state: "running", access_url: "old-url", url: "old-url-plain" };
    vi.mocked(bridge.companionStatus).mockReset().mockResolvedValue(running);
    vi.mocked(bridge.configureCompanion).mockReset().mockResolvedValue({ ...running, access_url: "new-url" });
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["companion.status"] } as any);
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openPanel(app, "手机 Companion");
      await app.findAll("button").find(button => button.text() === "读取手机服务状态")!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="手机服务端口"]').setValue(9090);
      await app.findAll("button").find(button => button.text() === "更换访问令牌")!.trigger("click");
      await flushPromises();
      expect(bridge.configureCompanion).not.toHaveBeenCalled();
      await app.get('[aria-labelledby="companion-rotate-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.configureCompanion).toHaveBeenCalledExactlyOnceWith(true, 8080, true, false);
      expect((app.get('[aria-label="手机访问地址"]').element as HTMLInputElement).value).toBe("new-url");
    } finally { app.unmount(); }
  });

  it("retries incomplete settings loading when opening settings again", async () => {
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    // Every read fails, the shell's own startup read included — it swallows its
    // failure and says a later visit may retry, which is what this case makes
    // the reader do.
    vi.mocked(bridge.settings).mockRejectedValue(new Error("temporary settings read failure"));
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const save = app.findAll("button").find(button => button.text() === "保存设置")!;
      const name = () => app.findAll("label").find(node => node.text().startsWith("设备名称"))!.get("input");
      // A form that never loaded is not a form that can be saved, and the button
      // says so rather than writing the empty defaults over what is stored.
      expect(save.attributes("disabled")).toBeDefined();
      await save.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).not.toHaveBeenCalled();
      // The read that failed is made again on the next visit, and this time it
      // answers: the field carries what is stored, and the page can be saved.
      vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "restored" } });
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(save.attributes("disabled")).toBeUndefined();
      expect((name().element as HTMLInputElement).value).toBe("restored");
      // And a page that loaded is not read again: the form is the reader's while
      // it is open, so a later visit keeps what is on screen.
      const reads = vi.mocked(bridge.settings).mock.calls.length;
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.settings).mock.calls.length).toBe(reads);
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("saves the AI tools from the page's own button, and leaves the settings save out of it", async () => {
    vi.mocked(bridge.updateAiProfiles).mockClear().mockResolvedValue({});
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    vi.mocked(bridge.aiProfiles).mockClear().mockResolvedValue({
      tools: [{ key: "claude", label: "Claude" }, { key: "codex", label: "Codex" }],
      enabled: ["claude"],
      custom_paths: ["~/one"],
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      const tool = (key: string) => app.findAll('input[type="checkbox"]')
        .find(input => (input.element as HTMLInputElement).value === key)!;
      const save = () => app.findAll("button").find(button => button.text().includes("保存 AI 配置"))!;
      // Loaded and untouched: the two fields are the stored ones.
      expect(app.text()).toContain("所有更改都已保存");
      expect((tool("claude").element as HTMLInputElement).checked).toBe(true);
      // A tool this machine syncs is dropped, one it does not is taken up, and a
      // path is typed under the stored one — blank lines and padding included,
      // because the list is paths and not lines.
      await tool("claude").setValue(false);
      await tool("codex").setValue(true);
      await app.get('[placeholder="每行一个路径"]').setValue("  ~/one\n\n~/two  \n");
      await flushPromises();
      expect(app.text()).toContain("有未保存的更改");
      await save().trigger("click");
      await flushPromises();
      expect(bridge.updateAiProfiles).toHaveBeenCalledExactlyOnceWith(["codex"], ["~/one", "~/two"]);
      expect(app.text()).toContain("所有更改都已保存");
      // The settings page's save does not carry them any more: a field saved by
      // another page's button is a field whose save the reader has to go and
      // find, and the form it belongs to is this one.
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledTimes(1);
      expect(bridge.updateAiProfiles).toHaveBeenCalledTimes(1);
    } finally {
      app.unmount();
      // The tool list is set here rather than queued, so it is put back: the
      // cases after this one read their own trees with no tools configured.
      vi.mocked(bridge.aiProfiles).mockResolvedValue({ tools: [], enabled: [], custom_paths: [] });
    }
  });

  it("submits the translation key separately and clears the password field after success", async () => {
    vi.mocked(bridge.updateSettings).mockClear()
      .mockResolvedValueOnce({ translate_key_set: true })
      .mockResolvedValueOnce({ translate_key_set: false });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const input = app.get('[aria-label="翻译 API 密钥"]');
      expect(input.attributes("type")).toBe("password");
      await input.setValue("test-key");
      await app.findAll("button").find(button => button.text() === "保存密钥")!.trigger("click");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledExactlyOnceWith({ set_translate_key: "test-key" });
      expect((input.element as HTMLInputElement).value).toBe("");
      expect(app.text()).toContain("密钥已设置");
      await app.findAll("button").find(button => button.text() === "清除密钥")!.trigger("click");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith({ clear_translate_key: true });
      expect(app.text()).toContain("未设置密钥");
    } finally { app.unmount(); }
  });

  it("passes selected translation languages and ignores results for edited input", async () => {
    let resolve!: (value: any) => void;
    vi.mocked(bridge.translate).mockClear().mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const input = app.get('textarea[placeholder="输入需要翻译的文本"]');
      await input.setValue("hello");
      await app.get('[aria-label="翻译源语言"]').setValue("en");
      await app.get('[aria-label="翻译目标语言"]').setValue("zh");
      // Scoped to the translation card: the settings rail carries a jump
      // labelled the same, and a global search would hit that one first.
      await app.get("#settings-translation").findAll("button")
        .find(button => button.text() === "翻译")!.trigger("click");
      expect(bridge.translate).toHaveBeenCalledExactlyOnceWith("hello", "zh", "en");
      await input.setValue("different text");
      resolve({ translated: "obsolete translation" });
      await flushPromises();
      expect(app.text()).not.toContain("obsolete translation");
    } finally { app.unmount(); }
  });

  it("retains the AI draft and dirty state when saving fails", async () => {
    vi.mocked(bridge.aiLocal).mockClear()
      .mockResolvedValueOnce({ entries: [{ tool: "custom", root: "", rel_path: "draft.md" }] })
      .mockResolvedValueOnce({ content: "original" })
      .mockRejectedValueOnce(new Error("disk write failed"));
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 draft.md"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="AI 配置编辑器"]').setValue("important draft");
      await app.findAll("button").find(button => button.text() === "保存配置")!.trigger("click");
      await flushPromises();
      expect(app.get(".ai-editor").text()).toContain("未保存");
      expect((app.get('[aria-label="AI 配置编辑器"]').element as HTMLTextAreaElement).value).toBe("important draft");
      expect(app.get('[aria-label="AI 配置编辑器"]').attributes("disabled")).toBeUndefined();
      expect(app.text()).not.toContain("配置已保存");
      expect(bridge.aiLocal).toHaveBeenLastCalledWith("save", "custom", "", "draft.md", "important draft");
    } finally { app.unmount(); }
  });

  it.each(["overwrite", "append"])("requires confirmation before an AI %s request", async (mode) => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 1 });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // Reset rather than clear, and the same answer however often it is asked:
    // opening settings primes the picker from the cache before the reader reads
    // anything, and that priming call would eat a queued once-value.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({
      peers: { a: { entries: [{ tool: "custom", root: "", rel_path: "remote.md" }] } },
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="AI 配置拉取方式"]').setValue(mode);
      await app.get('[aria-label="拉取 remote.md"]').trigger("click");
      await flushPromises();
      expect(bridge.aiPull).not.toHaveBeenCalled();
      await app.get('[aria-labelledby="ai-pull-title"] button[autofocus]').trigger("click");
      await flushPromises();
      expect(bridge.aiPull).not.toHaveBeenCalled();
      expect(app.get('[aria-labelledby="ai-pull-title"] .danger').attributes("disabled")).toBeDefined();
      await app.get('[aria-label="拉取 remote.md"]').trigger("click");
      await flushPromises();
      await app.get('[aria-labelledby="ai-pull-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "custom", root: "", rel_path: "remote.md", is_dir: false },
      ], mode);
    } finally { app.unmount(); }
  });

  it("runs the migration wizard as one pull per strategy, confirming only the one that overwrites", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 2 });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // Reset rather than clear: the wizard reads the inventory when it opens and
    // again if the device changes, and a queued once-value from an earlier case
    // would answer the first of those with the wrong machine's files.
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [
      { tool: "custom", root: "", rel_path: "here.md", sha256: "bbbb", mtime: 200, is_dir: false },
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
    ] });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "custom", root: "", rel_path: "absent.md", sha256: "cccc", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "here.md", sha256: "zzzz", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
    ] } } });
    const app = mount(App);
    const open = () => app.findAll("button").find(button => button.text().includes("迁移向导"))!.trigger("click");
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      const migrate = () => app.findAll("button").find(button => button.text().includes("开始迁移"))!;
      // Opening the wizard with no peer says so rather than offering a strategy
      // over an inventory it does not have.
      await openPanel(app, "其他设备");
      await open();
      expect(app.text()).toContain("先选择一台已配对设备");
      expect(app.findAll("button").some(button => button.text().includes("开始迁移"))).toBe(false);
      await app.get('[aria-label="迁移来源设备"]').setValue("a");
      await flushPromises();
      // The dialog reads the inventory itself, and the default strategy is the
      // one that cannot lose anything local.
      expect(bridge.aiInventory).toHaveBeenCalled();
      expect(migrate().text()).toContain("（1）");
      expect(app.text()).toContain("将补齐本机缺少的 1 个配置项");
      await migrate().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "custom", root: "", rel_path: "absent.md", is_dir: false },
      ], "copy");
      // "Copy" takes the differing files too, and still nothing is replaced, so
      // it also goes straight through.
      vi.mocked(bridge.aiPull).mockClear();
      await open();
      await app.get('[aria-label="全部拉取，已有的另存为副本"]').setValue(true);
      expect(migrate().text()).toContain("（2）");
      await migrate().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "custom", root: "", rel_path: "absent.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "here.md", is_dir: false },
      ], "copy");
      // Overwrite is the one strategy that destroys local content, so it stops
      // at the confirmation instead of sending.
      vi.mocked(bridge.aiPull).mockClear();
      await open();
      await app.get('[aria-label="全部拉取，覆盖本机文件"]').setValue(true);
      await migrate().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).not.toHaveBeenCalled();
      expect(app.get('[aria-labelledby="ai-pull-title"] .danger').attributes("disabled")).toBeUndefined();
      await app.get('[aria-labelledby="ai-pull-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "custom", root: "", rel_path: "absent.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "here.md", is_dir: false },
      ], "overwrite");
    } finally { app.unmount(); }
  });

  /**
   * A pre-v3 peer reports an inventory with no root id, so the sidecar refuses
   * to pull from it (`ai_config.py` — `legacy_peer_read_only`).  The older panel
   * never let a reader get that far: it filtered such peers out of the wizard's
   * source list entirely.  This shell lists every paired device, so it can, and
   * the refusal has to arrive as a reason before the work rather than as an
   * error code after it.
   */
  it("closes the migration wizard on a pre-v3 source instead of failing at the last step", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 0, errors: ["legacy_peer_read_only"] });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "old", name: "Old", paired: true } as any,
      { id: "new", name: "New", paired: true } as any,
    ] });
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
    ] });
    // The flag rides on the inventory the peer reports, not on the device row,
    // so it is only known once that peer has answered — which is why the label
    // cannot do the whole job and the note below has to exist.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: {
      old: { legacy: true, entries: [
        { tool: "custom", root: "", rel_path: "theirs.md", sha256: "bbbb", mtime: 100, is_dir: false },
      ] },
      new: { entries: [
        { tool: "custom", root: "", rel_path: "theirs.md", sha256: "bbbb", mtime: 100, is_dir: false },
      ] },
    } });
    const app = mount(App);
    const open = () => app.findAll("button").find(button => button.text().includes("迁移向导"))!.trigger("click");
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await open();
      const migrate = () => app.findAll("button").find(button => button.text().includes("开始迁移"))!;
      const source = () => app.get('[aria-label="迁移来源设备"]');
      await source().setValue("old");
      await flushPromises();
      // The diff is real and stays on screen: the peer's files are readable.
      expect(app.text()).toContain("将补齐本机缺少的 1 个配置项");
      expect(app.text()).toContain("该设备版本过旧，只能浏览，不能作为迁移来源");
      // ...but the step that cannot work is closed, and clicking it does nothing.
      expect(migrate().attributes("disabled")).toBeDefined();
      await migrate().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).not.toHaveBeenCalled();
      // A peer that answers while it is selected is labelled in the list too,
      // so the reason is visible before the next reader picks it.
      expect(source().text()).toContain("（只能浏览）");
      // The card behind the wizard lists the same peer's files, and closes its
      // pull there as well: the reason belongs with the list that shows what
      // cannot be pulled, not only with the wizard.
      await app.get('[aria-label="选择 theirs.md"]').setValue(true);
      await flushPromises();
      const batch = () => app.findAll("button").find(button => button.text().includes("拉取选中项"))!;
      expect(batch().attributes("disabled")).toBeDefined();
      await batch().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).not.toHaveBeenCalled();
      // Scoped to the card: the wizard's own note says the same words, and a
      // page-wide check would pass on that one alone.
      expect(app.get(".ai-remote").text()).toContain("该设备版本过旧，只能浏览，不能作为迁移来源");
      // The same shell pulls from a peer that is not pre-v3, so the closure is
      // about the source and not about the wizard.
      await source().setValue("new");
      await flushPromises();
      expect(migrate().attributes("disabled")).toBeUndefined();
      await migrate().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("new", [
        { tool: "custom", root: "", rel_path: "theirs.md", is_dir: false },
      ], "copy");
    } finally { app.unmount(); }
  });

  it("pulls the ticked remote entries as one batch, and drops a tick the peer no longer offers", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 2 });
    // Reading a peer's inventory walks this machine's own first, so that read has
    // to answer: the case above resets the mock, and an undefined answer is a
    // thrown read rather than a failing expectation.
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [] });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    const entries = ["one.md", "two.md", "three.md"].map(rel_path => ({ tool: "custom", root: "", rel_path }));
    // Both reads of the three files answer alike: opening settings primes the
    // picker from the cache, and the reader's own read asks the peer itself.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      // Nothing is ticked yet, so the batch button has nothing to send.
      const batch = () => app.findAll("button").find(button => button.text().includes("拉取选中项"))!;
      expect(batch().attributes("disabled")).toBeDefined();
      // The page box is the only way to tick more than one row at a time, and
      // its own state has to agree with the rows it just ticked.
      const pageBox = () => app.get('[aria-label="选择本页全部远程配置"]').element as HTMLInputElement;
      expect(pageBox().checked).toBe(false);
      await app.get('[aria-label="选择本页全部远程配置"]').setValue(true);
      expect(batch().text()).toContain("（3）");
      // "copy" is the one mode that does not ask first, so the batch arrives as
      // a single call carrying every ticked entry.
      await batch().trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "custom", root: "", rel_path: "one.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "two.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "three.md", is_dir: false },
      ], "copy");
      // What the peer accepted is the number of files to expect, and the page
      // reports those as they land — the line the reader waits on, rather than a
      // count of requests that never says whether anything arrived.
      expect(app.text()).toContain("正在接收 0 / 2 个文件");
      // A refresh that no longer lists one.md must not leave it ticked: a tick
      // the reader can see but that the batch cannot send is a lie about what
      // the button will do.
      vi.mocked(bridge.aiInventory).mockResolvedValueOnce({ peers: { a: { entries: entries.slice(1) } } });
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      expect(batch().text()).toContain("（2）");
      expect(app.find('[aria-label="选择 one.md"]').exists()).toBe(false);
      // With one of the two remaining rows ticked the box says "some, not all"
      // rather than claiming the page is fully ticked...
      await app.get('[aria-label="选择 two.md"]').setValue(false);
      expect(pageBox().checked).toBe(false);
      expect(pageBox().indeterminate).toBe(true);
      // ...and once every row is ticked it clears the page again.
      await app.get('[aria-label="选择本页全部远程配置"]').setValue(true);
      expect(batch().text()).toContain("（2）");
      await app.get('[aria-label="选择本页全部远程配置"]').setValue(false);
      expect(batch().attributes("disabled")).toBeDefined();
    } finally { app.unmount(); }
  });

  it("does not expose a writable editor for a truncated AI file", async () => {
    vi.mocked(bridge.aiLocal).mockResolvedValueOnce({
      entries: [{ tool: "custom", root: "fixture", rel_path: "large.md" }],
    }).mockResolvedValueOnce({ ok: true, content: "partial", truncated: true });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 large.md"]').trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="AI 配置编辑器"]').exists()).toBe(false);
      expect(app.text()).toContain("文件超过编辑器读取上限");
    } finally { app.unmount(); }
  });
  it("selects visible rows, pins/unpins, and requires confirmation for batch deletion", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mountOn("history");
    await flushPromises();
    expect(app.get('[aria-label="批量删除"]').attributes("disabled")).toBeDefined();
    await app.get('[aria-label="全选当前页"]').setValue(true);
    expect((app.get('[aria-label="选择记录 1"]').element as HTMLInputElement).checked).toBe(true);
    await app.get('[aria-label="批量收藏"]').trigger("click");
    await flushPromises();
    expect(bridge.batchPinHistory).toHaveBeenLastCalledWith(["1"], true);
    await app.get('[aria-label="批量取消收藏"]').trigger("click");
    await flushPromises();
    expect(bridge.batchPinHistory).toHaveBeenLastCalledWith(["1"], false);
    await app.get('[aria-label="批量删除"]').trigger("click");
    await flushPromises();
    expect(bridge.batchDeleteHistory).not.toHaveBeenCalled();
    expect(app.get('[aria-labelledby="batch-delete-title"]').text()).toContain("1 条记录");
    await app.get('[aria-labelledby="batch-delete-title"] .danger').trigger("click");
    await flushPromises();
    expect(bridge.batchDeleteHistory).toHaveBeenCalledExactlyOnceWith(["1"]);
    expect(app.find('[aria-label="复制记录"]').exists()).toBe(true);
    expect(app.find('[aria-label="删除记录"]').exists()).toBe(true);
    app.unmount();
  });
  it("merges the selected rows' full text, in list order, into one push", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    // A list row carries a truncated preview, so the two are spelled out and
    // differ: a merge that pushed previews would push cut-off clips, which is
    // the whole reason this fetches each row's text first.
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "1", preview: "第一条被截断的…" }),
      historyRow({ id: "2", preview: "第二条被截断的…" }),
    ]));
    vi.mocked(bridge.readHistoryText).mockImplementation(async (id: string) =>
      ({ id, text: id === "1" ? "第一条完整原文" : "第二条完整原文", truncated: false }));
    vi.mocked(bridge.pushText).mockResolvedValue({ ok: true, len: 21, sent: true } as any);
    const app = mountOn("history");
    await flushPromises();
    await app.get('[aria-label="全选当前页"]').setValue(true);
    await app.get('[aria-label="合并推送到电脑"]').trigger("click");
    await flushPromises();
    expect(bridge.readHistoryText).toHaveBeenCalledTimes(2);
    expect(bridge.pushText).toHaveBeenCalledExactlyOnceWith("第一条完整原文\n---\n第二条完整原文");
    app.unmount();
  });
  it("keeps a row whose text could not be read, using the preview it was listed with", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "1", preview: "第一条被截断的…" }),
      historyRow({ id: "2", preview: "第二条被截断的…" }),
    ]));
    // One entry vanishing mid-flight must not lose the other: the row that
    // cannot be read falls back to what the list showed for it.
    vi.mocked(bridge.readHistoryText).mockImplementation(async (id: string) => {
      if (id === "2") throw new Error("gone");
      return { id, text: "第一条完整原文", truncated: false };
    });
    vi.mocked(bridge.pushText).mockResolvedValue({ ok: true, len: 21, sent: true } as any);
    const app = mountOn("history");
    await flushPromises();
    await app.get('[aria-label="全选当前页"]').setValue(true);
    await app.get('[aria-label="合并推送到电脑"]').trigger("click");
    await flushPromises();
    expect(bridge.pushText).toHaveBeenCalledExactlyOnceWith("第一条完整原文\n---\n第二条被截断的…");
    app.unmount();
  });
  it("reads a clip's own text back to translate it, not the row's preview", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.readHistoryText).mockResolvedValue({
      id: "1", text: "一整段很长的原文", truncated: false,
    });
    vi.mocked(bridge.translate).mockResolvedValue({ translated: "a whole long source text" } as any);
    const app = mountOn("history");
    await flushPromises();
    await app.get('[aria-label="翻译记录"]').trigger("click");
    await flushPromises();
    // The row shows the escaped preview; the dialog must show the clip itself,
    // which is the whole reason the text is read back over IPC.
    expect(bridge.readHistoryText).toHaveBeenCalledExactlyOnceWith("1");
    const dialog = app.get('[aria-labelledby="translate-item-title"]');
    expect(dialog.get(".translate-source").text()).toBe("一整段很长的原文");
    await dialog.get(".modal-actions button:last-child").trigger("click");
    await flushPromises();
    expect(bridge.translate).toHaveBeenCalledExactlyOnceWith("一整段很长的原文", "en", "auto");
    expect(dialog.get(".translation-result").text()).toBe("a whole long source text");
    app.unmount();
  });
  it("offers to open a link clip in the browser, and only for a link", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ content_type: "URL", preview: "https://example.com/page" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="在浏览器打开"]').trigger("click");
      await flushPromises();
      // The window sends the row's id and nothing else: the sidecar reads the
      // clip and opens it only if the clip itself is a web link.
      expect(bridge.openHistoryLink).toHaveBeenCalledExactlyOnceWith("1");
      expect(app.text()).toContain(t("已在浏览器打开：{url}", { url: "https://example.com" }));
      app.unmount();

      // A text row that is not a link gets no such control, so the action only
      // ever appears where it can work.
      vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
        historyRow({ preview: "just a note" }),
      ] });
      const textApp = mount(App);
      await flushPromises();
      expect(textApp.find('[aria-label="在浏览器打开"]').exists()).toBe(false);
      textApp.unmount();
    } finally {
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("offers to download a file that lives on another device, and asks that device for it", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Studio", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Studio" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      // 复制 would clear this machine's clipboard for a file it does not have,
      // so the row offers the ask in its place rather than beside it.
      expect(app.find('[aria-label="复制记录"]').exists()).toBe(false);
      await app.get('[aria-label="下载文件"]').trigger("click");
      await flushPromises();
      // The row's own id and the device it came from, and nothing else: the peer
      // resolves which of its paths that row may reach.
      expect(bridge.requestEntryFiles).toHaveBeenCalledExactlyOnceWith("7", "peer-1");
      app.unmount();
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("opens the hover card on an image row with the picture the sidecar drew", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "img-1", content_type: "IMAGE_PNG", preview: "[Image]" }),
    ] });
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "image", image: "data:image/png;base64,AAAA", width: 1920, height: 1080,
      files: [], total: 0,
    });
    const app = mountOn("history", document.body);
    try {
      await flushPromises();
      // The row's own preview is the placeholder label, so the card has to come
      // from the sidecar rather than from anything the row already holds.
      vi.useFakeTimers();
      await app.get(".history-row").trigger("mouseenter");
      await vi.advanceTimersByTimeAsync(200);
      await flushPromises();
      const card = app.get(".history-preview");
      expect(bridge.previewHistoryEntry).toHaveBeenCalledExactlyOnceWith("img-1");
      expect(card.get("img").attributes("src")).toBe("data:image/png;base64,AAAA");
      // The *source's* size, which is the part a card at this width cannot say.
      expect(card.text()).toContain(t("{width} × {height} 像素", { width: 1920, height: 1080 }));
      app.unmount();
    } finally {
      vi.useRealTimers();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("asks the sidecar once per row, and not at all for a row whose text is the card", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 2, items: [
      historyRow({ id: "text-1", preview: "just a note" }),
      historyRow({ id: "file-1", content_type: "FILE", preview: "报告.pdf · 2.0 KB" }),
    ] });
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "files", image: "", width: 0, height: 0, total: 1,
      files: [{ name: "报告.pdf", size: 2048, kind: "file", exists: false }],
    });
    const app = mountOn("history", document.body);
    try {
      await flushPromises();
      vi.useFakeTimers();
      const rows = app.findAll(".history-row");
      // A text row's card is the row's own preview, so it opens with no read at
      // all — and opens at once, because there is nothing to wait for.
      await rows[0].trigger("mouseenter");
      expect(app.get(".history-preview").text()).toBe("just a note");
      expect(bridge.previewHistoryEntry).not.toHaveBeenCalled();

      // The pointer crosses a row on its way down the list: leaving before the
      // delay is up means the read never happens.
      await rows[0].trigger("mouseleave");
      await rows[1].trigger("mouseenter");
      await rows[1].trigger("mouseleave");
      await vi.advanceTimersByTimeAsync(400);
      expect(bridge.previewHistoryEntry).not.toHaveBeenCalled();

      // Resting on it is what asks, and the card says what the row cannot: the
      // file is gone.
      await rows[1].trigger("mouseenter");
      await vi.advanceTimersByTimeAsync(200);
      await flushPromises();
      expect(bridge.previewHistoryEntry).toHaveBeenCalledExactlyOnceWith("file-1");
      expect(app.get(".history-preview").text()).toContain(t("已不存在"));

      // Off and back on is the same card, read once.
      await rows[1].trigger("mouseleave");
      await rows[1].trigger("mouseenter");
      await vi.advanceTimersByTimeAsync(400);
      await flushPromises();
      expect(bridge.previewHistoryEntry).toHaveBeenCalledTimes(1);
      expect(app.find(".history-preview").exists()).toBe(true);
      app.unmount();
    } finally {
      vi.useRealTimers();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("leaves the card shut when the sidecar cannot say anything about a row", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "emf-1", content_type: "IMAGE_EMF", preview: "[Vector Image]" }),
    ] });
    // An empty card is what the sidecar answers for a row it cannot render,
    // rather than an error — so a hover over one is silent.
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "", image: "", width: 0, height: 0, files: [], total: 0,
    });
    const app = mountOn("history", document.body);
    try {
      await flushPromises();
      vi.useFakeTimers();
      await app.get(".history-row").trigger("mouseenter");
      await vi.advanceTimersByTimeAsync(200);
      await flushPromises();
      expect(app.find(".history-preview").exists()).toBe(false);
      app.unmount();
    } finally {
      vi.useRealTimers();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("adds the selection to favorites and clears all history only after confirmation", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mountOn("history");
    await flushPromises();
    await app.get('[aria-label="全选当前页"]').setValue(true);
    await app.get('[aria-label="加入收藏夹"]').trigger("click");
    await flushPromises();
    expect(bridge.batchFavoriteHistory).toHaveBeenCalledExactlyOnceWith(["1"], "");
    expect(app.text()).toContain("已加入收藏夹 1 条");
    await app.get('[aria-label="清空历史"]').trigger("click");
    await flushPromises();
    expect(bridge.clearHistory).not.toHaveBeenCalled();
    expect(app.get('[aria-labelledby="clear-history-title"]').text()).toContain("1 条记录");
    await app.get('[aria-labelledby="clear-history-title"] .danger').trigger("click");
    await flushPromises();
    expect(bridge.clearHistory).toHaveBeenCalledOnce();
    app.unmount();
  });
  it("sends a URL to a paired device only after the dialog is submitted", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["url.send"] } as any);
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "p", name: "Peer", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] as any });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await app.get('[aria-label="发送网址"]').trigger("click");
    await flushPromises();
    expect(bridge.sendUrl).not.toHaveBeenCalled();
    const dialog = app.get('[aria-labelledby="send-url-title"]');
    expect(dialog.text()).toContain("Peer");
    await dialog.get('[aria-label="要发送的网址"]').setValue("https://example.com/page");
    await dialog.findAll("button").find((button) => button.text() === "发送")!.trigger("click");
    await flushPromises();
    expect(bridge.sendUrl).toHaveBeenCalledExactlyOnceWith("p", "https://example.com/page");
    expect(app.text()).toContain("已发送网址到 Peer");
    app.unmount();
  });

  it("opens the Companion QR when the phone's panel asks for it", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    vi.mocked(bridge.companionQr).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      expect(bridge.companionQr).not.toHaveBeenCalled();
      emit({ type: "event", session_id: "s", name: "app.qr_requested" });
      await flushPromises();
      expect(bridge.companionQr).toHaveBeenCalledOnce();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
      expect(app.get('[aria-labelledby="qr-title"] img').attributes("src")).toBe("data:image/png;base64,AAAA");
      // An identical second request is a new request, not a no-op.
      emit({ type: "event", session_id: "s", name: "app.qr_requested" });
      await flushPromises();
      expect(bridge.companionQr).toHaveBeenCalledTimes(2);
    } finally {
      vi.mocked(bridge.companionQr).mockClear();
      app.unmount();
    }
  });

  it("hands a picked file to the phone from the Companion QR dialog", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    vi.mocked(bridge.chooseFile).mockResolvedValueOnce("C:/notes.txt");
    const app = mount(App);
    try {
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "app.qr_requested" });
      await flushPromises();
      await app.findAll("button").find(b => b.text().includes("发送文件到手机"))!.trigger("click");
      await flushPromises();
      // The single-file picker, which is the one the legacy 发送文件到手机 dialog
      // used — not the multi-select one the transfers page packs into an archive.
      // The path is all the window contributes: the copy, the sanitising and the
      // collision naming are the sidecar's, beside the directory it owns.
      expect(bridge.chooseFile).toHaveBeenCalledWith("any");
      expect(bridge.shareFileToPhone).toHaveBeenCalledWith("C:/notes.txt");
      expect(app.text()).toContain("已发送到手机：notes.txt");
    } finally {
      vi.mocked(bridge.chooseFile).mockReset();
      vi.mocked(bridge.shareFileToPhone).mockClear();
      app.unmount();
    }
  });

  it("offers every connected peer for a phone URL and sends to the picked one", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["url.send"] } as any);
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "p", name: "First", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
      { id: "q", name: "Second", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
      { id: "r", name: "Offline", paired: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] as any });
    vi.mocked(bridge.sendUrl).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "app.send_url_requested" });
      await flushPromises();
      const picker = app.get('[aria-label="目标设备"]');
      // The offline device is not a candidate, matching the legacy peer list.
      expect(picker.findAll("option").map((option) => option.text())).toEqual(["First", "Second"]);
      await picker.setValue("q");
      const dialog = app.get('[aria-labelledby="send-url-title"]');
      expect(dialog.text()).toContain("Second");
      await dialog.get('[aria-label="要发送的网址"]').setValue("https://example.com/second");
      await dialog.findAll("button").find((button) => button.text() === "发送")!.trigger("click");
      await flushPromises();
      expect(bridge.sendUrl).toHaveBeenCalledExactlyOnceWith("q", "https://example.com/second");
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
      app.unmount();
    }
  });

  it("says which control is grey and why when the engine is stopped", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      // One sentence for the two controls it explains: the toolbar's push
      // button is beside it, and every paired row's send-URL button is
      // described by it.
      expect(app.get("#devices-engine-note").text())
        .toBe("同步引擎未运行，推送文本与发送网址不可用。");
      expect(app.get('[aria-label="推送文本"]').attributes("disabled")).toBeDefined();
      const sendUrl = app.get('[aria-label="发送网址"]');
      expect(sendUrl.attributes("disabled")).toBeDefined();
      expect(sendUrl.attributes("aria-describedby")).toBe("devices-engine-note");
    } finally { app.unmount(); }
  });

  it("pushes text to the clipboard and peers only after the dialog is submitted", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["clipboard.push"] } as any);
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await app.get('[aria-label="推送文本"]').trigger("click");
    await flushPromises();
    expect(bridge.pushText).not.toHaveBeenCalled();
    const dialog = app.get('[aria-labelledby="push-text-title"]');
    await dialog.get('[aria-label="要推送的文本"]').setValue("hello");
    await dialog.findAll("button").find((button) => button.text() === "推送")!.trigger("click");
    await flushPromises();
    expect(bridge.pushText).toHaveBeenCalledExactlyOnceWith("hello");
    expect(app.text()).toContain("已推送到本机剪贴板并同步到所有设备");
    app.unmount();
  });

  it("opens the shared log tail and restarts only after confirmation", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设置"]').trigger("click");
    await flushPromises();
    await app.findAll("button").find((button) => button.text().includes("查看日志"))!.trigger("click");
    await flushPromises();
    expect(bridge.readLogs).toHaveBeenCalledExactlyOnceWith(200);
    // Opens on the failures: the tab that answers "why is this broken" is the
    // one worth showing first when it has anything in it.
    expect(app.get('[aria-label="日志内容"]').text()).toContain("problem 1");
    await app.findAll("button").find((button) => button.text().includes("全部"))!.trigger("click");
    await flushPromises();
    expect(app.get('[aria-label="日志内容"]').text()).toContain("line 1");
    await app.findAll("button").find((button) => button.text().includes("重启应用"))!.trigger("click");
    await flushPromises();
    expect(bridge.restartApp).not.toHaveBeenCalled();
    const dialog = app.get('[aria-labelledby="restart-title"]');
    await dialog.findAll("button").find((button) => button.text() === "重启")!.trigger("click");
    await flushPromises();
    expect(bridge.restartApp).toHaveBeenCalledOnce();
    app.unmount();
  });

  it("drives the update panel from the check, download and ready phases", async () => {
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["update.status"] } as any);
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    // A definite "this build has nothing it could install" is what keeps the
    // manual download in the card; the in-place install is the other case (see
    // the case below).  The download this drives is the sidecar's.
    vi.mocked(bridge.updateCheck).mockResolvedValueOnce({
      available: true, latest: "v2.0.0", current: "1.0.0", url: "https://example.com",
      installable: false });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // The panel hydrates the live phase instead of assuming idle.
      expect(bridge.updateStatus).toHaveBeenCalledOnce();
      const check = app.findAll("button").find((button) => button.text().includes("立即检查更新"))!;
      expect(check.attributes("disabled")).toBeUndefined();
      await check.trigger("click");
      await flushPromises();
      expect(bridge.updateCheck).toHaveBeenCalledOnce();
      expect(app.text()).toContain("发现新版本：v2.0.0");
      const download = app.findAll("button").find((button) => button.text() === "下载更新")!;
      await download.trigger("click");
      await flushPromises();
      expect(bridge.updateDownload).toHaveBeenCalledOnce();
      // Progress arrives as events, not from the command's return value.
      emit({ type: "event", session_id: "s", name: "update.state",
        data: { state: { phase: "downloading", fraction: 0.42, downloaded: 42, total: 100,
          error: "", version: "", path: "" } } });
      await flushPromises();
      expect(app.text()).toContain("正在下载… 42%");
      emit({ type: "event", session_id: "s", name: "update.state",
        data: { state: { phase: "ready", fraction: 1, downloaded: 100, total: 100,
          error: "", version: "v2.0.0", path: "C:/Users/me/Downloads/clipsync-update/a.exe" } } });
      await flushPromises();
      expect(app.text()).toContain("新版本 v2.0.0 已就绪");
      expect(app.text()).toContain("clipsync-update/a.exe");
      await app.findAll("button").find((button) => button.text().includes("打开所在文件夹"))!
        .trigger("click");
      await flushPromises();
      expect(bridge.updateOpenFolder).toHaveBeenCalledOnce();
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
      vi.mocked(bridge.subscribe).mockResolvedValue(() => {});
    }
  });

  it("offers to install in place unless the host says this build cannot", async () => {
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["update.status"] } as any);
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    // `installable` is the host's answer to a different question from
    // `available`, and only a definite no takes the install away.  A host that
    // could not read the release manifest leaves the field off entirely, which
    // is the first check here: a lookup that failed says nothing about what
    // this build can do, so the reader is offered the install this application
    // performs rather than told to replace the files by hand.
    vi.mocked(bridge.updateCheck).mockResolvedValueOnce({
      available: true, latest: "v2.0.0", current: "1.0.0", url: "https://example.com" });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const check = () => app.findAll("button")
        .find((button) => button.text().includes("立即检查更新"))!;
      const labels = () => app.findAll("button").map((button) => button.text());
      await check().trigger("click");
      await flushPromises();
      expect(labels()).toContain("下载并安装");
      expect(labels()).not.toContain("下载更新");

      // And the same offer for the host that answered "yes": there is a newer
      // release, and this build can replace itself with it.
      vi.mocked(bridge.updateCheck).mockResolvedValueOnce({
        available: true, latest: "v2.0.0", current: "1.0.0", url: "https://example.com",
        installable: true });
      await check().trigger("click");
      await flushPromises();
      expect(labels()).toContain("下载并安装");
      // The manual path must be gone, not merely joined: the card cannot both
      // promise to install and tell the reader to replace the files by hand.
      expect(labels()).not.toContain("下载更新");
      expect(app.text()).not.toContain("请退出当前应用");

      await app.findAll("button").find((button) => button.text() === "下载并安装")!
        .trigger("click");
      await flushPromises();
      expect(bridge.updateInstall).toHaveBeenCalledOnce();

      emit({ type: "event", session_id: "s", name: "update.state",
        data: { state: { phase: "installing", fraction: 1, downloaded: 100, total: 100,
          error: "", version: "v2.0.0", path: "" } } });
      await flushPromises();
      expect(app.text()).toContain("正在安装更新，完成后应用会自动重启。");
      // Nothing may be offered while the bundle is being replaced.
      expect(labels()).not.toContain("下载并安装");
      expect(labels()).not.toContain("下载更新");
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
      vi.mocked(bridge.subscribe).mockResolvedValue(() => {});
    }
  });

  it("toggles LAN discovery and visibility from the settings page", async () => {
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["discovery.status"] } as any);
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设置"]').trigger("click");
    await flushPromises();
    expect(bridge.discoveryStatus).toHaveBeenCalledOnce();
    const enabled = app.get('[aria-label="启用局域网发现"]');
    expect((enabled.element as HTMLInputElement).checked).toBe(true);
    await enabled.setValue(false);
    await flushPromises();
    expect(bridge.setDiscoveryEnabled).toHaveBeenCalledExactlyOnceWith(false);
    expect((app.get('[aria-label="启用局域网发现"]').element as HTMLInputElement).checked).toBe(false);
    const hidden = app.get('[aria-label="隐藏本机"]');
    expect((hidden.element as HTMLInputElement).checked).toBe(false);
    await hidden.setValue(true);
    await flushPromises();
    expect(bridge.setDiscoveryVisible).toHaveBeenCalledExactlyOnceWith(false);
    app.unmount();
  });

  it("shows codes only for pending unpaired devices and waits for the peer", async () => {
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "p", name: "Pending", paired: false, connection_state: "online",
        pairing_status: "confirmed_waiting", pairing_code: "123456", sas: "ABCD-EFGH" },
      { id: "t", name: "Trusted", paired: true, connection_state: "offline",
        pairing_status: "pending", pairing_code: "SECRET-TRUSTED", sas: "HIDDEN-SAS" },
      { id: "c", name: "Cancelled", paired: false, connection_state: "discovered",
        pairing_status: "cancelled", pairing_code: "SECRET-CANCELLED", sas: null },
    ] });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    expect(app.text()).toContain("123456");
    expect(app.text()).toContain("ABCD-EFGH");
    expect(app.text()).toContain("等待对方确认");
    expect(app.text()).not.toContain("SECRET");
    expect(app.text()).not.toContain("HIDDEN-SAS");
    const confirm = app.findAll("button").find((button) => button.text() === "确认配对")!;
    expect(confirm.attributes("disabled")).toBeDefined();
    // A second assertion stood here, on the footer's sync switch.  It went with
    // the switch on 2026-09-14, and it is not carried over to the overview's
    // replacement: it asked for `disabled` while this status payload advertises
    // no capabilities at all, so the switch was disabled for that reason and the
    // pending pairing it was placed here to observe was never what it measured.
    // The confirm above does carry that.
    app.unmount();
  });

  it("does not revoke trust before explicit dialog confirmation", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Trusted", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await app.get('[aria-label="撤销信任"]').trigger("click");
    await flushPromises();
    expect(bridge.unpairDevice).not.toHaveBeenCalled();
    await app.get('[aria-labelledby="revoke-title"] .danger').trigger("click");
    await flushPromises();
    expect(bridge.unpairDevice).toHaveBeenCalledWith("t");
    app.unmount();
  });
  it("manages device connections, removal and the archived list explicitly", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Trusted", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
      { id: "a", name: "Archived", paired: false, connection_state: "offline",
        pairing_status: "", pairing_code: null, sas: null, archived: true, removed_at: 1 },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      expect(app.text()).toContain("已移除的设备");
      await app.get('[aria-label="断开连接"]').trigger("click");
      await flushPromises();
      expect(bridge.disconnectDevice).toHaveBeenCalledWith("t");
      await app.get('[aria-label="移除设备"]').trigger("click");
      await flushPromises();
      expect(bridge.forgetDevice).not.toHaveBeenCalled();
      await app.get('[aria-labelledby="forget-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.forgetDevice).toHaveBeenCalledWith("t");
      await app.findAll("button").find(button => button.text() === "恢复")!.trigger("click");
      await flushPromises();
      expect(bridge.restoreDevice).toHaveBeenCalledWith("a");
      await app.findAll("button").find(button => button.text() === "彻底删除")!.trigger("click");
      await flushPromises();
      expect(bridge.purgeDevice).not.toHaveBeenCalled();
      await app.get('[aria-labelledby="purge-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.purgeDevice).toHaveBeenCalledWith("a");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });
  it("probes a paired device per channel and lists pinned certificate fingerprints", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.testDevice).mockResolvedValue({ ok: true, results: [
      { channel: "lan", ok: true, latency_ms: 12.5, error: null },
      { channel: "relay", ok: false, latency_ms: null, error: "timeout" },
    ] });
    vi.mocked(bridge.deviceCerts).mockResolvedValue({ devices: [
      { device_id: "t", device_name: "Trusted", fingerprint_short: "abcdefgh...12345678",
        fingerprint: "abc", paired: true },
    ] });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Trusted", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="测试连接"]').trigger("click");
      await flushPromises();
      expect(bridge.testDevice).toHaveBeenCalledWith("t");
      expect(app.text()).toContain("连接测试：局域网 13ms · 中继 超时");
      await app.get('[aria-label="查看证书指纹"]').trigger("click");
      await flushPromises();
      expect(bridge.deviceCerts).toHaveBeenCalledOnce();
      expect(app.text()).toContain("abcdefgh...12345678");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.deviceCerts).mockResolvedValue({ devices: [] });
    }
  });
  it("carries an update in whichever direction the two builds are apart", async () => {
    vi.mocked(bridge.offerDeviceUpdate).mockResolvedValue({ sent: true });
    vi.mocked(bridge.fetchDeviceUpdate).mockResolvedValue({ sent: true });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Studio", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: true },
      // The direction the feature is meant to run in: this device is the one
      // behind, so its row offers the fetch rather than the send.
      { id: "n", name: "Newer", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.9", platform: "windows", arch: "amd64",
        update_available: false, update_fetchable: true },
      // Same build, another platform, and one too old to advertise: three
      // different reasons not to offer, and none of them is a button.  The
      // decision is the sidecar's -- these rows only render it -- and so is the
      // reason, which rides along as a code for the row's menu to say in words.
      { id: "u", name: "Same", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.8", platform: "windows", arch: "amd64",
        update_available: false, update_cached: false, update_blocked: "level" },
      { id: "v", name: "Silent", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        update_available: false, update_cached: false },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      // The version is shown for the device that advertises one, and a device
      // that advertises nothing shows no chip rather than a guess.
      expect(app.text()).toContain("版本 1.0.7");
      const offers = app.findAll('[aria-label="发送更新"]');
      expect(offers).toHaveLength(1);
      await offers[0].trigger("click");
      await flushPromises();
      expect(bridge.offerDeviceUpdate).toHaveBeenCalledExactlyOnceWith("t");
      expect(app.text()).toContain("已把更新发送给 Studio");
      // And the other way round, off the same list: the device that is behind
      // asks, and says so while it waits for the blob.
      const fetches = app.findAll('[aria-label="获取更新"]');
      expect(fetches).toHaveLength(1);
      await fetches[0].trigger("click");
      await flushPromises();
      expect(bridge.fetchDeviceUpdate).toHaveBeenCalledExactlyOnceWith("n");
      expect(app.text()).toContain("已向 Newer 索取安装包，收到后可在更新页安装");
      // The same two actions on the row's own menu, which is where a reader who
      // wants to push a version to a device looks first: what the version is,
      // and the entry that sends it.  Nothing is clicked here -- both actions
      // were just exercised through the row -- so what these hold is that the
      // menu offers them at all, on the row's own terms.
      const rowMenu = async (index: number) => {
        await app.findAll(".device-row")[index].trigger("contextmenu");
        await flushPromises();
        return app.findAll(".context-menu-item");
      };
      let entries = await rowMenu(0);
      const version = entries.find(entry => entry.text().includes("版本 1.0.7"))!;
      // Information, not an action: the row is the version itself.
      expect(version.attributes("aria-disabled")).toBeDefined();
      const send = entries.find(entry => entry.text().includes("发送更新"))!;
      expect(send.attributes("aria-disabled")).toBeUndefined();
      expect(send.attributes("title")).toContain("把本机的安装包发送给该设备");
      closeContextMenu();
      // The row that has no action to offer says which of the reasons it is,
      // on the entry that would have been there.  Without it the whole state
      // was a button that was not drawn -- indistinguishable from a level pair,
      // another platform, or a device this machine has nothing to send to.
      entries = await rowMenu(2);
      const blocked = entries.find(entry => entry.text().includes("发送更新"))!;
      expect(blocked.attributes("aria-disabled")).toBeDefined();
      expect(blocked.attributes("title")).toContain("两台设备版本相同");
      closeContextMenu();
      // And a device that advertises nothing gets none of it: there is no
      // version to name and no reason to give.
      entries = await rowMenu(3);
      expect(entries.filter(entry => entry.text().includes("更新"))).toHaveLength(0);
      expect(entries.filter(entry => entry.text().includes("版本"))).toHaveLength(0);
      closeContextMenu();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true });
      vi.mocked(bridge.fetchDeviceUpdate).mockReset().mockResolvedValue({ sent: true });
    }
  });
  it("offers nothing when this machine has no installer to offer", async () => {
    // The send button carries the installer this machine's own upgrade kept, so
    // a machine that has kept none has nothing to send: the row says so and
    // starts nothing.  Downloading one here to pass on would be the same file
    // from the same place, fetched twice, on the machine that does not need it
    // -- the device being offered can fetch it itself.
    vi.mocked(bridge.offerDeviceUpdate).mockResolvedValue({ sent: true });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Studio", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: false },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      // The download mock is shared with the update-page case above and nothing
      // clears it between cases, so the count starts from here.
      vi.mocked(bridge.updateDownload).mockClear();
      const send = app.get('[aria-label="发送更新"]');
      expect(send.attributes("disabled")).toBeDefined();
      expect(send.attributes("title")).toContain("本机还没有安装包可发送");
      await send.trigger("click");
      await flushPromises();
      expect(bridge.offerDeviceUpdate).not.toHaveBeenCalled();
      expect(bridge.updateDownload).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true });
      vi.mocked(bridge.updateDownload).mockClear();
    }
  });
  it("says why an update offer did not go, instead of leaving the click silent", async () => {
    vi.mocked(bridge.offerDeviceUpdate).mockRejectedValue({
      code: "update.peer_unreachable", message: "无法连接到该设备，它可能已离线。", retryable: false,
    });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Studio", paired: false, connection_state: "discovered",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: true },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="发送更新"]').trigger("click");
      await flushPromises();
      // The sidecar's own sentence, which it localized: the row shows the
      // reason rather than an English fallback of this file's own making.
      expect(app.text()).toContain("无法连接到该设备，它可能已离线。");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true });
    }
  });
  it("stamps a relayed chat message with its receipt instead of repainting the clipboard", async () => {
    vi.useFakeTimers();
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback; return () => {};
    });
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [
      { session_id: "one", peer_id: "peer-one", peer_name: "Phone", status: "active",
        online: true, unread: 0 },
    ], muted: [] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [
      { entry_id: "e1", kind: "text", outgoing: true, status: "done", msg_id: "m1",
        text: "你好", ts: 1 },
    ] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="附近聊天"]').trigger("click");
      await flushPromises();
      // Nothing has been relayed yet, so the bubble carries no receipt at all.
      expect(app.find(".chat-delivery").exists()).toBe(false);
      const reads = vi.mocked(bridge.history).mock.calls.length;
      emit({ type: "event", session_id: "s", name: "relay.delivery.changed",
        data: { peer_id: "peer-one", msg_id: "m1", status: "delivered",
          kind: "chat_text", session_id: "one" } });
      await flushPromises();
      expect(app.get(".chat-delivery").text()).toContain(t("已送达"));
      expect(app.get(".chat-delivery").classes()).toContain("chat-delivery--delivered");
      // The receipt describes one send, not the clipboard.  Folded in place it
      // costs nothing; left to the store's fall-through it would repaint the
      // whole history list — once per ack of a large transfer, for nothing.
      await vi.advanceTimersByTimeAsync(200);
      await flushPromises();
      expect(vi.mocked(bridge.history).mock.calls.length).toBe(reads);
    } finally {
      app.unmount();
      vi.useRealTimers();
      vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [], muted: [] });
      vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [] });
    }
  });

  it("answers a submitted internet pairing code with a wait, a completion, or a reason", async () => {
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback; return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openPanel(app, "互联网配对");
      // Nothing entered yet, so there is nothing to wait on and nothing to say.
      expect(app.find(".peer-list--waiting").exists()).toBe(false);
      expect(app.find(".pairing-message").exists()).toBe(false);
      // Submitting is not pairing: the code goes out and the machine holding it
      // has to answer, so the entry appears as the wait it is.  Before this the
      // card said 已提交配对码 and then never changed again, whether the partner
      // answered or nobody ever did.
      const field = () => app.get('[aria-label="输入对方配对码"]');
      const submit = () => app.findAll("button").find(node => node.text() === t("提交配对码"))!;
      await field().setValue("ABCD-EFGH-JKLM");
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true, waiting: [
        { peer_id: "9EVR", name: "", since: 1700000000 },
      ] } as any);
      const submits = vi.mocked(bridge.internetPairingStatus).mock.calls.length;
      await submit().trigger("click");
      await flushPromises();
      expect(bridge.enterInternetPairingCode).toHaveBeenCalledWith("ABCD-EFGH-JKLM");
      expect(vi.mocked(bridge.internetPairingStatus).mock.calls.length).toBeGreaterThan(submits);
      expect(app.get(".peer-list--waiting").text()).toContain(t("等待对方确认…"));
      expect(app.text()).not.toContain(t("已提交配对码"));
      // The other machine answers, minutes later.  The card is the one place
      // that has to notice, and it hears about it from the event rather than
      // from a poll the reader would have to provoke by leaving and coming back.
      const reads = vi.mocked(bridge.internetPairingStatus).mock.calls.length;
      emit({ type: "event", session_id: "s", name: "netpair.peer.changed",
        data: { peer_id: "a1a2a3a4a5a6", name: "书房电脑", status: "paired", online: true } });
      await flushPromises();
      expect(vi.mocked(bridge.internetPairingStatus).mock.calls.length).toBeGreaterThan(reads);
      expect(app.get(".notice").text()).toContain(t("已与 {name} 完成互联网配对", { name: "书房电脑" }));
      // A code the sidecar refuses is answered at the box that holds it, in the
      // reader's language — not in the band at the top of the window, in English.
      vi.mocked(bridge.enterInternetPairingCode).mockRejectedValueOnce(
        { code: "INVALID_PAIRING_CODE", message: "invalid pairing code", retryable: false });
      await field().setValue("JKLM-ABCD-EFGH");
      await submit().trigger("click");
      await flushPromises();
      expect(app.get(".pairing-message").text()).toContain(t("配对码无效，请核对后重新输入。"));
      expect(app.get(".pairing-message").classes()).toContain("pairing-message--failed");
      expect(app.find(".error-band").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      vi.mocked(bridge.enterInternetPairingCode).mockResolvedValue({ peer_id: "", waiting: true });
    }
  });

  it("switches internet sync from the card it turns on, and says what that turns off", async () => {
    // The feature had no switch where it is used: a reader who had never opened
    // the settings page found a card whose buttons answered and whose list
    // stayed empty, and nothing on it said the relay was off.
    vi.mocked(bridge.internetPairingStatus).mockResolvedValueOnce({ peers: [], enabled: false } as any);
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openPanel(app, "互联网配对");
      const toggle = () => app.get('[aria-label="启用互联网同步"]');
      const generate = () => app.findAll("button").find(node => node.text() === t("生成配对码"))!;
      expect((toggle().element as HTMLInputElement).checked).toBe(false);
      // Everything on the card needs the relay, so nothing on it is offered.
      expect(generate().attributes("disabled")).toBeDefined();
      expect(app.get('[aria-label="输入对方配对码"]').attributes("disabled")).toBeDefined();
      expect(app.find(".relay-state").exists()).toBe(false);

      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true } as any);
      await toggle().setValue(true);
      await flushPromises();
      // The same field the settings page saves under 互联网同步, saved at once:
      // this page has no save button, and the sidecar applies it live.
      expect(bridge.updateSettings).toHaveBeenCalledWith({ internet_sync_enabled: true });
      expect((toggle().element as HTMLInputElement).checked).toBe(true);
      expect(generate().attributes("disabled")).toBeUndefined();
    } finally {
      app.unmount();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
    }
  });

  it("renders clipboard markup as text, never as executable HTML", async () => {
    const app = mountOn("history");
    await flushPromises();
    expect(app.find(".history-content").text()).toContain("<img");
    expect(app.find(".history-content img").exists()).toBe(false);
    expect(app.text()).toContain("同步引擎未启动");
    app.unmount();
  });

  it("previews a clip with no text of its own in the reader's language", async () => {
    // The sidecar previews an image with the label "[Image]", which is its own
    // word rather than anything the user copied — so the row says 图片 in a
    // Chinese window instead of the English it was stored under.
    const imagePage = () => historyPage([
      historyRow({ id: "img", content_type: "IMAGE", preview: "[Image]" }),
    ]);
    // Once per mount: the second one fetches again, and the shared fixture is
    // the markup row the neighbouring case is about.
    vi.mocked(bridge.history).mockResolvedValueOnce(imagePage());

    setLocale("zh-CN");
    const zh = mountOn("history");
    await flushPromises();
    // The paragraph, not the row: the kind chip beside it says 图片 too, as it
    // does on the panel, and this case is about the preview line.
    expect(zh.find(".history-content p").text()).toBe("图片");
    zh.unmount();

    // The window renders in the saved language, so that is what the second
    // mount has to be told -- `setLocale` alone is overwritten on mount.
    setLocale("en");
    vi.mocked(bridge.settings).mockResolvedValueOnce({ settings: { language: "en" } });
    vi.mocked(bridge.history).mockResolvedValueOnce(imagePage());
    const en = mountOn("history");
    await flushPromises();
    expect(en.find(".history-content p").text()).toBe("Image");
    en.unmount();
  });
});

describe("first-run language picker", () => {
  const showModal = () => vi.mocked(HTMLDialogElement.prototype.showModal);
  const closeDialog = () => vi.mocked(HTMLDialogElement.prototype.close);

  it("prompts while no language has ever been chosen, then persists the pick", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    setLocale("zh-CN");
    vi.mocked(bridge.updateSettings).mockClear();
    vi.mocked(bridge.settings).mockResolvedValueOnce({
      settings: { language: "zh-CN", language_chosen: false },
    });
    const app = mount(App);
    try {
      await flushPromises();
      expect(showModal()).toHaveBeenCalled();
      const dialog = app.get('[aria-labelledby="language-title"]');
      // Bilingual on purpose: both names appear for every option.
      expect(dialog.text()).toContain("简体中文");
      expect(dialog.text()).toContain("Simplified Chinese");
      expect(dialog.text()).toContain("英语");
      await dialog.findAll("button").find(button => button.text().includes("English"))!.trigger("click");
      await flushPromises();
      // Persisting the choice is what retires the picker next launch.
      expect(bridge.updateSettings).toHaveBeenCalledExactlyOnceWith({ language: "en" });
      expect(closeDialog()).toHaveBeenCalled();
      // The shell switched immediately, without a reload.
      expect(app.find('[aria-label="Settings"]').exists()).toBe(true);
    } finally {
      app.unmount();
      setLocale("zh-CN");
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("shows runtime events as dismissible toasts, like the legacy webview", async () => {
    setLocale("zh-CN");
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      expect(app.find(".notice-stack").exists()).toBe(false);
      emit({ type: "event", session_id: "s", name: "pairing.request",
        data: { device_id: "p", name: "Pixel", code: "12345678" } });
      emit({ type: "event", session_id: "s", name: "url.received",
        data: { device_id: "p", url: "https://example.com/page" } });
      await flushPromises();
      const notices = app.findAll(".notice");
      expect(notices).toHaveLength(2);
      expect(notices[0].text()).toContain(t("配对请求"));
      expect(notices[0].text()).toContain(
        t("{name} 请求配对 — 代码：{code}", { name: "Pixel", code: "12345678" }),
      );
      expect(notices[1].text()).toContain(t("收到网址"));
      expect(notices[1].text()).toContain("https://example.com/page");
      // The store owns the expiry timer; the button dismisses immediately.
      await notices[0].get("button").trigger("click");
      expect(app.findAll(".notice")).toHaveLength(1);
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("says why a connect click came to nothing, in the notice stack", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.connectDevice).mockResolvedValue({ accepted: false });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Trusted", paired: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="连接设备"]').trigger("click");
      await flushPromises();
      // Nowhere to dial leaves no error band behind: the reason travels as its
      // own event, and that notice is what the user reads.
      expect(bridge.connectDevice).toHaveBeenCalledWith("t");
      expect(app.find(".error-band").exists()).toBe(false);
      emit({ type: "event", session_id: "s", name: "device.connection_unreachable",
        data: { device_id: "t", name: "Trusted" } });
      await flushPromises();
      const notice = app.get(".notice");
      expect(notice.text()).toContain(t("连接设备"));
      expect(notice.text()).toContain(
        t("找不到 {name} — 请确认该设备已开启 ClipSync 且在同一网络", { name: "Trusted" }),
      );
    } finally {
      app.unmount();
      setLocale("zh-CN");
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("says so when a confirm click did not pair, instead of just dropping the card", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    // The runtime answers `{paired, status}`, not the `accepted` the other
    // actions carry: a false is a *successful* call whose request was already
    // over, so `action` would have reported it as one.
    vi.mocked(bridge.confirmPairing).mockResolvedValue({ paired: false, status: "cancelled" });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "p", name: "Phone", paired: false, connection_state: "online",
        pairing_status: "pending", pairing_code: "4821", sas: "0421" },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      expect(app.get(".pairing-controls").text()).toContain("4821");
      await app.get(".pairing-actions").findAll("button")[0].trigger("click");
      await flushPromises();
      expect(bridge.confirmPairing).toHaveBeenCalledWith("p", "4821");
      const notice = app.get(".notice");
      expect(notice.text()).toContain(t("配对请求"));
      expect(notice.text()).toContain(t("配对失败。验证码可能已过期。请重新连接。"));
      // The call itself succeeded — the request was simply over — so this is
      // not the error band's business.
      expect(app.find(".error-band").exists()).toBe(false);
      // And the confirm that *did* pair says nothing: the row turning paired
      // and `device.connected` are the report, so a notice here would repeat it.
      vi.mocked(bridge.confirmPairing).mockResolvedValue({ paired: true, status: "paired" });
      vi.mocked(bridge.devices).mockResolvedValue({ items: [
        { id: "p", name: "Phone", paired: true, connection_state: "online",
          pairing_status: "paired", pairing_code: null, sas: null },
      ] });
      await flushPromises();
      const before = app.findAll(".notice").length;
      await app.get(".pairing-actions").findAll("button")[0].trigger("click");
      await flushPromises();
      expect(app.findAll(".notice")).toHaveLength(before);
    } finally {
      app.unmount();
      setLocale("zh-CN");
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.confirmPairing).mockResolvedValue({ paired: false, status: "confirmed_waiting" });
    }
  });

  it("asks about a changed device certificate and re-pins it on request", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.retrustDevice).mockResolvedValue({ trusted: true });
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      // A refused connection is otherwise invisible: the device just looks
      // offline, so this prompt is the only thing that explains it.
      emit({ type: "event", session_id: "s", name: "device.security_alert",
        data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: true } });
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
      const dialog = app.get('[aria-labelledby="cert-alert-title"]');
      expect(dialog.text()).toContain(
        t("设备“{name}”的证书已变更（可能已重装或重置）。", { name: "Pixel" }));
      await dialog.get(".modal-actions button:last-child").trigger("click");
      await flushPromises();
      expect(bridge.retrustDevice).toHaveBeenCalledWith("peer");
      // Answered, so the prompt closes itself rather than needing a dismissal.
      expect(HTMLDialogElement.prototype.close).toHaveBeenCalled();
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("does not offer to trust a certificate the alert never carried", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "device.security_alert",
        data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: false } });
      await flushPromises();
      const dialog = app.get('[aria-labelledby="cert-alert-title"]');
      expect(dialog.get(".modal-actions button:last-child").attributes("disabled")).toBeDefined();
      expect(dialog.text()).toContain(t("该设备需要再次连接后才能信任新证书。"));
      // The other answer needs nothing from the device and always works.
      await dialog.get(".modal-actions button:first-child").trigger("click");
      await flushPromises();
      expect(bridge.unpairDevice).toHaveBeenCalledWith("peer");
      expect(bridge.retrustDevice).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });
});

describe("timed sync pause", () => {
  /** A status payload with sync in the state the case is about.
   *
   * `overview.get` is advertised because the pause controls live on the
   * overview page and this suite drives them there: the footer keeps the
   * countdown and nothing else, by the user's request of 2026-09-14.
   */
  function status(syncState: string) {
    return { version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: syncState,
      capabilities: ["overview.get"] } as any;
  }

  it("arms the pause from the overview and counts down the deadline the runtime reports", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    const until = Math.floor(Date.now() / 1000) + 30 * 60;
    vi.mocked(bridge.status).mockResolvedValue(status("running"));
    // No deadline armed, so the card offers the presets rather than a
    // countdown — and said explicitly, because `clearAllMocks` clears calls
    // without putting an earlier case's implementation back.
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { timed_pause_until: 0 } } as any);
    vi.mocked(bridge.pauseSync).mockResolvedValue({ enabled: false, until } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get(".overview-pause").findAll("button")
        .find(button => button.text() === "30 分钟")!.trigger("click");
      await flushPromises();
      expect(bridge.pauseSync).toHaveBeenCalledWith(30);
      // The countdown reads the deadline the sidecar reports, not the duration
      // this window asked for, so a pause armed elsewhere counts down too.
      expect(app.get(".bottom-status").text()).toContain("⏸ 已暂停 · 剩余 30 分钟");
      expect(app.text()).toContain("同步已暂停 30 分钟");
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

});

describe("files dropped on the window", () => {
  /** The window's drop listener, as the host would call it. */
  async function mountWithDrop() {
    let drop!: (event: any) => void;
    vi.mocked(bridge.onFileDrop).mockImplementation(async (handler: (event: any) => void) => {
      drop = handler;
      return () => {};
    });
    vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    const app = mount(App);
    await flushPromises();
    return { app, drop: (event: any) => drop(event) };
  }

  it("shows the window is a target while files are over it, and takes the drop to the transfers page", async () => {
    vi.clearAllMocks();
    setLocale("zh-CN");
    const { app, drop } = await mountWithDrop();
    try {
      // Four events, one gesture: the hint is up while the files are over the
      // window and gone the moment they are not — on the drop as well as on the
      // leave, because the drop is the gesture ending.
      drop({ type: "enter", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();
      expect(app.find(".drop-veil").exists()).toBe(true);
      drop({ type: "over", position: { x: 2, y: 2 } });
      await flushPromises();
      expect(app.find(".drop-veil").exists()).toBe(true);
      drop({ type: "leave" });
      await flushPromises();
      expect(app.find(".drop-veil").exists()).toBe(false);

      drop({ type: "enter", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();
      drop({ type: "drop", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();
      expect(app.find(".drop-veil").exists()).toBe(false);
      // The drop answers the file picker, not the target: it opens the page
      // that names the machine and stages what came in, and nothing is sent
      // from the window on the drop itself.
      expect(app.find(".transfers-view").exists()).toBe(true);
      expect(app.get(".staged-drop").text()).toContain("已拖入 1 个文件");
      expect(app.get(".staged-drop").text()).toContain("a.txt");
      expect(bridge.sendFiles).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    }
  });

});

describe("the window's right-click menus", () => {

  let app: ReturnType<typeof mount>;
  beforeEach(async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    setLocale("zh-CN");
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "a", preview: "https://example.com/a", content_type: "URL" }),
    ]));
    vi.mocked(bridge.devices).mockResolvedValue({ items: [{
      id: "dev-1", name: "Laptop", paired: true, connection_state: "online",
      pairing_status: "paired", pairing_code: null, sas: null, note: "",
    }] as any });
    // Every case here right-clicks a row of one of the two list pages.
    app = mountOn("history");
    await flushPromises();
  });
  afterEach(() => { app.unmount(); closeContextMenu(); });

  it("copies the row it was opened on, not the first one", async () => {
    vi.mocked(bridge.copyHistory).mockClear();
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    await app.findAll(".context-menu-item")[0].trigger("click");
    await flushPromises();
    expect(bridge.copyHistory).toHaveBeenLastCalledWith("a");
  });

});
