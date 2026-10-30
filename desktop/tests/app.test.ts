import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";
import { bridge } from "../src/api/bridge";
import { closeContextMenu } from "../src/lib/context-menu";
import type { HistoryItem, Overview } from "../src/api/types";
import { setLocale, t } from "../src/i18n";

// jsdom in this harness has no Storage on its opaque origin.  The window's
// relay-consent cases are the one place that uses it, and a real browser always
// has one, so give the suite the same small surface instead of special-casing
// those assertions.
if (!globalThis.localStorage) {
  const values = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, String(value)); },
      removeItem: (key: string) => { values.delete(key); },
      clear: () => { values.clear(); },
    },
  });
}

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
    // The refresh button asks the LAN who is here before reading the list, and
    // the answer carries the discovery switches: browsing switched off means the
    // round was a no-op, which the window has to say rather than report a count.
    scanDevices: vi.fn().mockResolvedValue({ enabled: true, visible: true }),
    history: vi.fn().mockResolvedValue(historyPage([historyRow({
      preview: '<img src=x onerror="window.injected=true">',
    })])),
    quit: vi.fn(),
    minimize: vi.fn().mockResolvedValue(null),
    settings: vi.fn().mockResolvedValue({ settings: {} }),
    pauseSync: vi.fn().mockResolvedValue({ enabled: false, until: 0 }),
    resumeSync: vi.fn().mockResolvedValue({ enabled: true }),
    setSyncEnabled: vi.fn().mockResolvedValue({ enabled: true }),
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
    phoneChatList: vi.fn().mockResolvedValue({ messages: [], last_id: 0 }),
    phoneChatSend: vi.fn().mockResolvedValue({ ok: true }),
    phoneChatRead: vi.fn().mockResolvedValue({ ok: true }),
    companionRemoteStatus: vi.fn().mockResolvedValue({ enabled: true, token_set: true }),
    // Saving remote access resolves with the status read that follows it, so
    // the default answer is the same shape with the relays it kept.
    companionRemoteConfigure: vi.fn().mockResolvedValue({ enabled: true, mode: "readonly",
      brokers: [], default_brokers: [], page_url: "", token_set: true, connected: false,
      broker: "", phone_online: false, phone_id: "", last_seen: 0, rx: 0, tx: 0, error: "" }),
    markChatRead: vi.fn().mockResolvedValue({ ok: true }),
    // "Connecting" is the quietest answer the invite can give: the sidecar
    // answers before its dial finishes, so a case about the row's own state
    // (see "inviting a device to chat") says what it wants the answer to be.
    inviteChat: vi.fn().mockResolvedValue({ chat_session_id: null, connecting: true }),
    // A walk that found nothing, which is the answer most cases can share: a
    // save re-walks now, so every case that saves the tools reads this.
    aiLocal: vi.fn().mockResolvedValue({ entries: [], roots: [] }),
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
    chooseFolder: vi.fn(),
    shareFileToPhone: vi.fn().mockResolvedValue({
      ok: true, name: "notes.txt", path: "C:/share/notes.txt", size: 5,
    }),
    // The library's list read.  Nothing on the app shell asked for it before
    // the quick search could hand a word over to that page, which then reads
    // the list with it; an empty library is the quietest answer.
    favorites: vi.fn().mockResolvedValue({
      items: [], total: 0, offset: 0, groups: [], session_id: "s", seq: 0,
    }),
    listBackups: vi.fn().mockResolvedValue({ backups: [] }),
    restoreBackup: vi.fn(),
    createBackup: vi.fn().mockResolvedValue({ backup_path: "C:/b/new.zip" }),
    exportHistory: vi.fn().mockResolvedValue({ filename: "history.json", filepath: "C:/b/history.json" }),
    importHistory: vi.fn().mockResolvedValue({ ok: true, imported: 0 }),
    confirmPairing: vi.fn().mockResolvedValue({ paired: false, status: "confirmed_waiting" }),
    unpairDevice: vi.fn().mockResolvedValue({ accepted: true }),
    // The two rename destinations: a saved LAN peer's note, and an internet
    // pairing's alias.  Which one a row writes is the case's assertion.
    setDeviceNote: vi.fn().mockResolvedValue({ ok: true }),
    renameInternetPeer: vi.fn().mockResolvedValue({ ok: true }),
    unpairInternetPeer: vi.fn().mockResolvedValue({ ok: true }),
    generateInternetPairingCode: vi.fn().mockResolvedValue({ code: "ABCD-EFGH-IJKL" }),
    connectDevice: vi.fn().mockResolvedValue({ accepted: true }),
    disconnectDevice: vi.fn().mockResolvedValue({ disconnected: true }),
    forgetDevice: vi.fn().mockResolvedValue({ forgotten: true }),
    offerDeviceUpdate: vi.fn().mockResolvedValue({ sent: true, reason: "accepted" }),
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
    // Installs the archive already on disk.  A reply means the install did not
    // replace this process, and `manual` is the one reply that is not a refusal:
    // the archive is staged and this build cannot swap itself for that kind of
    // file, so a case about the manual wording says so itself.
    updateInstallReady: vi.fn().mockResolvedValue({ ok: true, installed: false, reason: "up_to_date" }),
    openDataFolder: vi.fn().mockResolvedValue({ ok: true, folder: "C:/data" }),
    openAboutLink: vi.fn().mockResolvedValue({ ok: true, url: "https://github.com/kai3316/clipsync" }),
    onMenuAction: vi.fn().mockResolvedValue(() => {}),
    onFileDrop: vi.fn().mockResolvedValue(() => {}),
    onHotkey: vi.fn().mockResolvedValue(() => {}),
    // Answers with the actions the OS refused; a case about one of those
    // overrides it with a list.
    setHotkeys: vi.fn().mockResolvedValue([]),
    // The transfers page polls this the moment it is opened — including when a
    // drop is what opened it.
    transfers: vi.fn().mockResolvedValue({ active: [], history: [], speed_test: {} }),
    sendFiles: vi.fn().mockResolvedValue({ transfer_id: "t1" }),
    // A file dropped onto a conversation is sent as an attachment from the chat
    // page; the case says what the call should be.
    sendChatFile: vi.fn().mockResolvedValue({ ok: true, transfer_id: "chat-1" }),
    companionQr: vi.fn().mockResolvedValue({
      ok: true, url: "http://10.0.0.2:8080/mobile.html?token=t",
      qr: "data:image/png;base64,AAAA",
    }),
    discoveryStatus: vi.fn().mockResolvedValue({ enabled: true, visible: true }),
    setDiscoveryEnabled: vi.fn().mockResolvedValue({ enabled: false, visible: true }),
    setDiscoveryVisible: vi.fn().mockResolvedValue({ enabled: true, visible: false }),
    copyHistory: vi.fn().mockResolvedValue({ copied: true }),
    copyText: vi.fn().mockResolvedValue({ copied: true }),
    // The paste half of the hotkeys and of the row menu's 「粘贴到当前应用」.
    // Resolved by default, so a case about the copy path does not have to
    // say what the injection did; a case about the fallback overrides it.
    injectPaste: vi.fn().mockResolvedValue({ ok: true }),
    hideMainAndPaste: vi.fn().mockResolvedValue({ ok: true }),
    openQuickPaste: vi.fn().mockResolvedValue({ ok: true }),
    // The on-demand paste of a remote-file row (1.3.1): starting it, abandoning
    // it, and the forwarded events that say how it ended.  A case about the
    // fetch captures the event handler it registered.
    preparePasteEntry: vi.fn().mockResolvedValue({ requested: true }),
    cancelPasteEntry: vi.fn().mockResolvedValue({ cancelled: 1 }),
    onSidecarEvent: vi.fn().mockResolvedValue(() => {}),
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

/** Let the search box's debounce run out.
 *
 * Real time, because the case it is used in also needs the promises and the
 * microtasks the box's own read is made of; fake timers would hold those too.
 * Longer than `SEARCH_DEBOUNCE_MS` so a re-armed wait has run out as well. */
const searchSettles = () => new Promise((done) => { setTimeout(done, 260); });

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

/** A button, by the words a reader sees on it.
 *
 *  A device row's 连接设备 / 断开连接 / 聊天 / 发送更新 are written out now (U111),
 *  so their text *is* their label and there is no `aria-label` to look up.  The
 *  icon buttons that kept theirs are found by the same call, which is why it
 *  reads both: a test should not have to know which of the two shapes an entry
 *  has, and the row's shape is exactly what changed.
 */
function buttons(app: VueWrapper, label: string) {
  return app.findAll("button").filter(node =>
    !node.classes("context-menu-item")
    && ((node.text() || "").trim() === label || node.attributes("aria-label") === label));
}
/** The first of them, for a row that offers the entry once. */
function button(app: VueWrapper, label: string) {
  const found = buttons(app, label);
  if (!found.length) throw new Error(`no button labelled ${label}`);
  return found[0];
}

/** Open a device row's own menu — the 更多操作 button, not a right click. */
async function openDeviceRowMenu(app: VueWrapper, index = 0) {
  await app.findAll('[aria-label="更多操作"]')[index].trigger("click");
  await flushPromises();
}
/** Click an entry of the open context menu by its label. */
async function clickMenuItem(app: VueWrapper, label: string) {
  const item = app.findAll(".context-menu-item").find(node => node.text() === label);
  if (!item) throw new Error(`no menu entry named ${label}`);
  await item.trigger("click");
  await flushPromises();
}
/** Open a remote-file row's menu and hand back its 仅下载到本机 entry.
 *
 * The row's own button is 取回并粘贴 now (1.3.1): the explicit download — fetch
 * the files without pasting them — moved into the menu, which is where the
 * action for a file wanted on disk belongs.  The entry carries the tooltip the
 * row's old download button used to, so a case can still read what it says
 * about the route and the peer's reachability.
 */
async function downloadRemoteEntry(app: VueWrapper, index = 0) {
  closeContextMenu();
  await app.findAll(".history-row")[index].trigger("contextmenu");
  await flushPromises();
  const entry = app.findAll(".context-menu-item").find(node => node.text().includes(t("仅下载到本机")));
  if (!entry) throw new Error("no 仅下载到本机 entry on the row menu");
  return entry;
}
/** Take that download through the menu. */
async function clickDownloadRemote(app: VueWrapper, index = 0) {
  const entry = await downloadRemoteEntry(app, index);
  await entry.trigger("click");
  await flushPromises();
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
  it("says which filter lines cannot match a process name, and still saves the rest", async () => {
    // The sidecar compares each line to a process name exactly, so a line that
    // carries a folder matches nothing and its rule simply never fires —
    // silently, which is the failure this field cannot otherwise show.
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      app_filter_enabled: true, app_filter_mode: "blacklist", app_filter_list: [],
    } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const list = () => app.get('[aria-label="应用过滤进程列表"]');
      // What the field matches against, said once; a mistake in it is silent.
      expect(app.text()).toContain("按名称精确匹配");
      await list().setValue("chrome.exe\nC:\\Program Files\\Foo\\bar.exe");
      await flushPromises();
      const warning = app.get("#app-filter-issues");
      expect(warning.text()).toContain("C:\\Program Files\\Foo\\bar.exe");
      expect(warning.text()).toContain("1 条");
      expect(list().attributes("aria-describedby")).toContain("app-filter-issues");
      // Corrected, and the line goes away without a save in between.
      await list().setValue("chrome.exe\nsecret*");
      await flushPromises();
      expect(app.find("#app-filter-issues").exists()).toBe(false);
      // A path is a warning, not a refusal: the list still saves.
      await list().setValue("chrome.exe\n/opt/foo/bar");
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        app_filter_list: ["chrome.exe", "/opt/foo/bar"],
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("stores the view settings where they are flipped, leaving the bar for the rest", async () => {
    // One rule, three settings: the theme, the language and the animations are
    // things a reader sees the effect of, so they are written the moment they
    // are picked and are never a pending edit.  The language used to stay in the
    // snapshot, so changing it raised a Save bar over a form whose only edit had
    // already been written.  See U183.
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // The foot of the form: where "有未保存的更改" appears and nowhere else.
      const bar = () => app.find(".settings-save-status");
      expect(bar().exists()).toBe(false);

      await app.get('[aria-label="语言"]').setValue("en");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith({ language: "en" });
      expect(bar().exists()).toBe(false);
      // Back to Chinese for the rest: the labels below are the zh-CN ones.
      await app.get('[aria-label="Language"]').setValue("zh-CN");
      await flushPromises();

      const motion = () => app.findAll("label").find(node => node.text().includes("启用动画"))!.get("input");
      // On, then off: the mock answers a settings read with nothing, so the box
      // starts unticked and a set-to-the-same-value is not a change at all.
      await motion().setValue(true);
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith({ ui_animation_enabled: true });
      expect(document.documentElement.classList.contains("no-motion")).toBe(false);
      await motion().setValue(false);
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith({ ui_animation_enabled: false });
      expect(bar().exists()).toBe(false);
      expect(document.documentElement.classList.contains("no-motion")).toBe(true);

      // A data setting still waits for the bar, and only that one is pending.
      await app.findAll("label").find(node => node.text().startsWith("设备名称"))!.get("input").setValue("Living room");
      await flushPromises();
      expect(bar().exists()).toBe(true);
      expect(bar().text()).toContain("有未保存的更改");
    } finally {
      app.unmount();
      document.documentElement.classList.remove("no-motion");
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("keeps the relay boxes folded until they are asked for, or searched for", async () => {
    // Six fields and two paragraphs of explanation sat between the two switches
    // the sync card is opened for and the rest of it, so the common settings
    // were pushed off the first screen.  The switch that turns internet sync on
    // is not folded: it is the reason the card is open.  See U186.
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const fold = () => app.get(".settings-fold");
      const body = () => app.get("#settings-relay").element as HTMLElement;
      expect(body().style.display).toBe("none");
      expect(fold().attributes("aria-expanded")).toBe("false");
      expect(fold().attributes("aria-controls")).toBe("settings-relay");
      // The two switches above it are still out in the open.
      expect(app.findAll("label").some(node => node.text().includes("启用互联网同步"))).toBe(true);
      expect(app.findAll("label").some(node => node.text().includes("仅同步纯文本"))).toBe(true);
      // And one click opens them where they would have been.
      await fold().trigger("click");
      expect(body().style.display).toBe("");
      expect(fold().attributes("aria-expanded")).toBe("true");
      expect(fold().text()).toContain("收起中继的高级设置");
      // A search opens the fold on its own: a match inside a closed box is a
      // match the reader cannot see.
      await fold().trigger("click");
      expect(body().style.display).toBe("none");
      await app.get('[aria-label="搜索设置…"]').setValue("中继");
      await flushPromises();
      expect(body().style.display).toBe("");
    } finally {
      app.unmount();
    }
  });

  it("fills the data directory from the folder picker", async () => {
    // The box takes an absolute path, and a typo in one is only discovered when
    // the next start cannot find its history.
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { data_dir: "" } });
    vi.mocked(bridge.chooseFolder).mockClear().mockResolvedValue("D:/ClipSync");
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const box = () => app.get('[aria-label="数据目录"]');
      await app.findAll("button").find(button => button.text().includes("选择目录"))!.trigger("click");
      await flushPromises();
      expect((box().element as HTMLInputElement).value).toBe("D:/ClipSync");
      // Cancel is not a value: an emptied picker leaves the box alone.
      vi.mocked(bridge.chooseFolder).mockResolvedValue(null);
      await app.findAll("button").find(button => button.text().includes("选择目录"))!.trigger("click");
      await flushPromises();
      expect((box().element as HTMLInputElement).value).toBe("D:/ClipSync");
    } finally {
      app.unmount();
      vi.mocked(bridge.chooseFolder).mockReset();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("puts the three primary pages first, then the collections and the tools", async () => {
    // 概览 / 剪贴板历史 / 设备 are the pages a reader lives in, so they sit at
    // the top; everything else is a collection kept by the history page or a
    // tool that configures the window.  The grouping is a word above a row, not
    // a different row, and the digit chords did not move with the order: the
    // sidebar advertises `PAGES`' own numbering, so old habits keep working.
    const app = mount(App);
    try {
      await flushPromises();
      const nav = app.get('nav[aria-label="主导航"]');
      const groups = nav.findAll('[role="group"]');
      expect(groups.map(group => group.attributes("aria-label"))).toEqual(["内容", "工具"]);
      // The visible line is the same words, hidden from the accessibility tree
      // so the group is named once rather than twice.
      const labels = groups.flatMap(group => group.findAll(".nav-group-label"));
      expect(labels.map(node => node.text())).toEqual(["内容", "工具"]);
      expect(labels.every(node => node.attributes("aria-hidden") === "true")).toBe(true);
      // The primary three are above both groups; 设置 stays at the bottom.
      expect(nav.findAll(":scope > button").map(node => node.attributes("aria-label")))
        .toEqual(["概览", "剪贴板历史", "设备"]);
      expect(groups[0].findAll("button").map(node => node.attributes("aria-label")))
        .toEqual(["收藏库", "附近聊天", "文件传输"]);
      expect(groups[1].findAll("button").map(node => node.attributes("aria-label")))
        .toEqual(["手机伴侣", "互联网同步", "AI 配置"]);
      expect(nav.get(".sidebar-settings button").attributes("aria-label")).toBe("设置");
      // Moving rows on screen did not move the chords they answer to.
      const chordOf = (label: string) =>
        nav.findAll("button").find(button => button.attributes("aria-label") === label)!
          .attributes("aria-keyshortcuts");
      expect(chordOf("概览")).toBe("Control+1");
      expect(chordOf("剪贴板历史")).toBe("Control+2");
      expect(chordOf("设备")).toBe("Control+3");
      expect(chordOf("收藏库")).toBe("Control+4");
      expect(chordOf("AI 配置")).toBe("Control+7");
      expect(chordOf("设置")).toBe("Control+8");
    } finally {
      app.unmount();
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
      // The highlight is a class, which says nothing to a screen reader: the
      // row is marked current and its position is announced through the
      // footer, since a position is the one thing a row cannot say about
      // itself.  See U67.
      expect(app.get(".history-row--kbd").attributes("aria-current")).toBe("true");
      expect(app.findAll(".history-row").filter(row => row.attributes("aria-current") === "true")).toHaveLength(1);
      await new Promise(resolve => setTimeout(resolve, 300));
      await flushPromises();
      expect(app.get(".status-message").text()).toBe(
        t("第 {index} 条，共 {total} 条：{text}", { index: 1, total: 3, text: "first" }));
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
      // Ctrl+A takes the whole visible page.  The count on the list heading is
      // not a live region any more; it is announced through the footer once the
      // ticking stops -- see U68 -- so what is asserted after the second press
      // is the sentence, not the number's own element.
      await press("a", { ctrlKey: true });
      expect(app.get("#history-select-reached").text()).toContain("3");
      // A reader typing in the search box owns the keys: no cursor moves, and
      // Ctrl+A means "select this text" rather than "select every record".
      const search = app.get('[aria-label="搜索历史记录"]');
      (search.element as HTMLInputElement).focus();
      await press("ArrowDown");
      expect(cursor()).toBe("third");
      await press("a", { ctrlKey: true });
      expect(app.get("#history-select-reached").text()).toContain("3");
      // One announcement for the run of clicks rather than one per click: the
      // debounce is 400 ms, so the footer has it a moment later.
      await new Promise(resolve => setTimeout(resolve, 450));
      await flushPromises();
      expect(app.get(".status-message").text()).toBe(t("已选择 {count} 条", { count: 3 }));
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([
        historyRow({ preview: '<img src=x onerror="window.injected=true">' }),
      ]));
    }
  });

  it("keeps accepting what is typed in the search box while the list is refreshing", async () => {
    // `busy` gates the writes on this page, and it also covers `refreshing`,
    // which an event from the sidecar sets before its debounced refresh even
    // starts -- a clip a peer pushed was enough to grey the box out under the
    // reader's cursor mid-word.  A read does not wait for a read.
    vi.mocked(bridge.history).mockResolvedValue(
      historyPage([historyRow({ id: "a", preview: "alpha" })]));
    let emit!: (event: any) => void;
    vi.mocked(bridge.subscribe).mockImplementationOnce(async (callback: any) => {
      emit = callback;
      return () => {};
    });
    const app = mountOn("history");
    try {
      await flushPromises();
      // The debounce after an event is 60 ms, so `refreshing` stays true for
      // the whole of this case without any timer to advance.
      emit({ type: "event", session_id: "s", name: "history.changed", data: {} });
      // A tick, so the attribute the reader sees is the one asserted: reading
      // it before Vue re-renders would pass whatever the template said before
      // the event, which is no test at all.
      await flushPromises();
      const box = app.get('[aria-label="搜索历史记录"]');
      expect(box.attributes("disabled")).toBeUndefined();
      // The type chips and the sort toggle are reads the same way: the chip a
      // reader reaches for right after typing was disabled by that search's own
      // request, because `historyBusy` covers `loading`.
      expect(app.get('[aria-label="按类型筛选历史记录"] .chip').attributes("disabled"))
        .toBeUndefined();

      await box.setValue("al");
      await flushPromises();
      expect((box.element as HTMLInputElement).value).toBe("al");
      // The × is on the field with the first letter, not a query later.
      expect(app.find(".search-clear").exists()).toBe(true);
      // And the read went out with it — once the typing stopped — rather than
      // the list staying on the unfiltered page.
      await searchSettles();
      expect(vi.mocked(bridge.history).mock.calls.at(-1)![0]).toBe("al");
    } finally {
      vi.mocked(bridge.history).mockResolvedValue(historyPage([
        historyRow({ preview: '<img src=x onerror="window.injected=true">' }),
      ]));
      app.unmount();
    }
  });

  it("asks for the words typed during a write once that write is over", async () => {
    // `store.search` answers nothing while a write is in flight: the rows it
    // would filter are the ones that write is about to replace.  The box keeps
    // the words either way, so a query dropped there has to be asked again —
    // or the box would be holding a search the list was never given.
    vi.mocked(bridge.history).mockResolvedValue(
      historyPage([historyRow({ id: "a", preview: "alpha" })]));
    let copied!: (value: { copied: boolean }) => void;
    vi.mocked(bridge.copyHistory).mockReturnValueOnce(
      new Promise((done) => { copied = done; }));
    const app = mountOn("history");
    try {
      await flushPromises();
      const copyRow = app.findAll("button")
        .find(button => button.attributes("aria-label") === "复制记录")!;
      await copyRow.trigger("click");
      await flushPromises();
      const box = app.get('[aria-label="搜索历史记录"]');
      await box.setValue("al");
      // Long enough for the box's wait to run out, which is the whole point:
      // it ran out while the write was still open, so nothing went out yet.
      await searchSettles();
      expect(vi.mocked(bridge.history).mock.calls.at(-1)![0]).toBe("");
      // The words are still there, which is the other half: whatever happens
      // to the request, the reader's own typing is never taken back.
      expect((box.element as HTMLInputElement).value).toBe("al");
      copied({ copied: true });
      await searchSettles();
      expect(vi.mocked(bridge.history).mock.calls.at(-1)![0]).toBe("al");
    } finally {
      vi.mocked(bridge.history).mockResolvedValue(historyPage([
        historyRow({ preview: '<img src=x onerror="window.injected=true">' }),
      ]));
      app.unmount();
    }
  });

  it("sends Ctrl+F to the search box of the page being read", async () => {
    // The library carries a box of its own, and the chord answered it by
    // throwing the reader at the history list instead — a find on the page in
    // front of them, reaching a page that was not.
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["favorites.list"] } as any);
    const app = mountOn("favorites", document.body);
    try {
      await flushPromises();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "f", ctrlKey: true }));
      await flushPromises();
      expect(app.find(".favorites-view").exists()).toBe(true);
      expect(app.find(".history-list").exists()).toBe(false);
      expect(document.activeElement).toBe(app.get('[aria-label="搜索收藏"]').element);
    } finally {
      app.unmount();
    }
  });

  it("empties the search box on Esc, the way the rest of the window clears", async () => {
    vi.mocked(bridge.history).mockResolvedValue(
      historyPage([historyRow({ id: "a", preview: "alpha" })]));
    // Attached, because the assertion is about which element the document
    // thinks has the caret: a detached input is never `activeElement`.
    const app = mountOn("history", document.body);
    try {
      await flushPromises();
      const box = app.get('[aria-label="搜索历史记录"]');
      (box.element as HTMLInputElement).focus();
      await box.setValue("al");
      await searchSettles();
      expect(vi.mocked(bridge.history).mock.calls.at(-1)![0]).toBe("al");
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await flushPromises();
      expect((box.element as HTMLInputElement).value).toBe("");
      expect(app.find(".search-clear").exists()).toBe(false);
      // And the list was asked again, so the rows are the unfiltered ones.
      expect(vi.mocked(bridge.history).mock.calls.at(-1)![0]).toBe("");
    } finally {
      vi.mocked(bridge.history).mockResolvedValue(historyPage([
        historyRow({ preview: '<img src=x onerror="window.injected=true">' }),
      ]));
      app.unmount();
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
      await app.get('[aria-label="同步发送间隔（秒）"]').setValue("");
      await app.get('[aria-label="显示历史条数"]').setValue("9999");
      await app.get('[aria-label="局域网发现标识"]').setValue("   ");
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

  it("saves the translation key from its own button without arming the page's save", async () => {
    // The key has a button of its own and never rides the form's save, so
    // saving it cannot leave the bar claiming there is something pending — the
    // card it belongs to would be the only place to mark, and the mark would be
    // a lie about a field the reader has already saved.
    vi.mocked(bridge.updateSettings).mockResolvedValue({ translate_key_set: true } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(app.text()).toContain("未设置密钥");
      await app.get('[aria-label="翻译 API 密钥"]').setValue("sk-test-key");
      await app.findAll("button").find(button => button.text() === "保存密钥")!.trigger("click");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith({ set_translate_key: "sk-test-key" });
      expect(app.text()).toContain("密钥已设置");
      expect(app.text()).not.toContain("有未保存的更改");
    } finally {
      app.unmount();
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
    }
  });

  /** The save bar is the only thing on screen that can say it: the rule being
   *  broken is on the 安全 card, and the button rides the foot of every one. */
  /** The dialogs that end a pairing are the last thing read before it goes,
   *  so the machine in them has to be the one the reader renamed. */
  /** A device's name was an `<h2>`, so a window listing eight devices offered
   *  eight headings at the same level as the cards that hold them — heading
   *  navigation landed on the rows and never on the sections.  See U78. */
  it("keeps a device's name out of the heading list, which is where the cards are", async () => {
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "LIVINGROOM-PC", note: "客厅主机", paired: true,
        connection_state: "online", pairing_status: "paired", pairing_code: null, sas: null },
      { id: "u", name: "STUDY-X1", note: "书房", paired: true,
        connection_state: "online", pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      const names = app.findAll(".device-name");
      expect(names.map(node => node.text())).toContain("客厅主机");
      expect(names.some(node => node.element.tagName === "H2")).toBe(false);
      expect(app.findAll(".device-identity h2").length).toBe(0);
      // The cards above them are still headings, so the heading list is the
      // page's structure rather than its rows.
      expect(app.findAll("h2").length).toBeGreaterThan(0);
      expect(app.findAll("h2").map(node => node.text())).not.toContain("客厅主机");
    } finally {
      app.unmount();
    }
  });

  it("names a device in its own confirm dialogs the way this machine renamed it", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "LIVINGROOM-PC", note: "客厅主机", paired: true,
        connection_state: "online", pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="全部：移除设备"]').trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="forget-title"]');
      expect(dialog.text()).toContain("客厅主机");
      // The published name is still on the row, under the one the reader typed
      // — it is not what a sentence about to remove the device calls it.
      expect(dialog.text()).not.toContain("LIVINGROOM-PC");
    } finally {
      app.unmount();
    }
  });

  it("says which card is holding the save back when a half-typed password blocks it", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { encryption_enabled: false, password_set: false },
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const saveButton = () => app.findAll("button").find(button => button.text().includes("保存设置"))!;
      await app.get('[aria-label="加密密码"]').setValue("Str0ng-Passw0rd!");
      await app.get('[aria-label="确认加密密码"]').setValue("Str0ng-Passw0rd?");
      expect(saveButton().attributes("disabled")).toBeDefined();
      expect(saveButton().attributes("title")).toContain("安全");
      expect(app.get(".settings-save-block").text()).toContain("安全");
      // The two boxes agreeing again is the whole of the fix, and it takes the
      // line away with the block.
      await app.get('[aria-label="确认加密密码"]').setValue("Str0ng-Passw0rd!");
      expect(saveButton().attributes("disabled")).toBeUndefined();
      expect(app.find(".settings-save-block").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });
  it("resets every data file only after the factory-reset word is typed", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.factoryReset).mockClear();
    vi.mocked(bridge.createBackup).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text().includes("恢复出厂设置"))!.trigger("click");
      await flushPromises();
      expect(bridge.factoryReset).not.toHaveBeenCalled();
      const dialog = app.get('[aria-labelledby="factory-reset-title"]');
      const confirm = () => dialog.findAll("button").find(button => button.text() === "确认重置")!;
      // One click used to be the whole confirmation for a request that deletes
      // the history, the favourites, the pairings and the device identity.
      expect(confirm().attributes("disabled")).toBeDefined();
      await confirm().trigger("click");
      await flushPromises();
      expect(bridge.factoryReset).not.toHaveBeenCalled();
      const word = dialog.get('input[type="text"]');
      await word.setValue("删");
      expect(confirm().attributes("disabled")).toBeDefined();
      // The dialog offers the one thing a reader may still want from the data
      // it is about to destroy: a copy of it.
      await dialog.findAll("button").find(button => button.text() === "先创建备份")!.trigger("click");
      await flushPromises();
      expect(bridge.createBackup).toHaveBeenCalled();
      await word.setValue("删除");
      expect(confirm().attributes("disabled")).toBeUndefined();
      await confirm().trigger("click");
      await flushPromises();
      expect(bridge.factoryReset).toHaveBeenCalledTimes(1);
    } finally {
      app.unmount();
    }
  });

  it("applies a picked theme at once, stores it at once, and puts it back when the store refuses", async () => {
    // The theme is the one setting where seeing the result *is* the decision,
    // so it is not held back behind 保存 — it is applied and stored together.
    // Applied-and-stored rather than previewed-then-rolled-back on leaving the
    // page: a preview that snaps back is a choice the reader made and lost, and
    // it leaves the window wearing a theme the config does not have.  What is
    // left to roll back is a write that failed, which is exactly the case this
    // covers at the end.
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { appearance_mode: "dark", auto_start: false },
    });
    vi.mocked(bridge.updateSettings)
      .mockRejectedValueOnce(new Error("save failed"))
      .mockResolvedValue({});
    const app = mount(App);
    try {
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("dark");
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const theme = app.get('[aria-label="主题"]');
      // Refused: the window goes back to the stored theme rather than wearing
      // a colour the sidecar does not have, and the field follows it back so
      // the box does not claim a value that is not in force.
      await theme.setValue("light");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("dark");
      expect((theme.element as HTMLSelectElement).value).toBe("dark");
      // Stored this time, and no Save bar offering to save what is already
      // saved: one theme is one decision, made once.
      await theme.setValue("light");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("light");
      expect(bridge.updateSettings).toHaveBeenLastCalledWith({ appearance_mode: "light" });
      expect(app.text()).not.toContain("有未保存的更改");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
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
    const remote = { enabled: false, token_set: true, mode: "readonly" as const, brokers: [], default_brokers: [], page_url: "", connected: false, broker: "", phone_online: false, phone_id: "", last_seen: 0, rx: 0, tx: 0, error: "" };
    const running = { enabled: true, running: true, port: 8080, state: "running", access_url: "old-url", url: "old-url-plain", remote };
    vi.mocked(bridge.companionStatus).mockReset().mockResolvedValue(running);
    vi.mocked(bridge.configureCompanion).mockReset().mockResolvedValue({ ...running, access_url: "new-url" });
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["companion.status"] } as any);
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="手机伴侣"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="刷新服务状态"]').trigger("click");
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

  it("saves the rest of the remote settings with the relay box left empty", async () => {
    // The relay box is one field of four.  Disabling the only save button while
    // it was empty meant clearing it to paste a new list also locked the page
    // address and the permissions out of being saved -- and the sidecar has its
    // own complaint for a request that carries no relay at all, which is the
    // one the reader should get to see.
    const remote = { enabled: true, token_set: true, mode: "readonly" as const,
      brokers: ["wss://one.example:8084/mqtt"], default_brokers: ["wss://one.example:8084/mqtt", "wss://two.example:8084/mqtt"],
      page_url: "https://companion.example/", connected: false, broker: "", phone_online: false,
      phone_id: "", last_seen: 0, rx: 0, tx: 0, error: "" };
    const running = { enabled: true, port: 8080, running: true, state: "running",
      access_url: "old-url", url: "old-url-plain", remote };
    vi.mocked(bridge.companionStatus).mockReset().mockResolvedValue({ ...running });
    vi.mocked(bridge.companionRemoteConfigure).mockClear().mockResolvedValue({
      ...remote, mode: "full", page_url: "https://saved.example/",
      brokers: remote.default_brokers,
    } as any);
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["companion.status"] } as any);
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="手机伴侣"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="刷新服务状态"]').trigger("click");
      await flushPromises();
      const brokerRows = () => app.findAll(".broker-list__item .broker-url").map((row) => row.text());
      expect(brokerRows()).toEqual(["wss://one.example:8084/mqtt"]);
      for (const remove of app.findAll(".broker-list__item button")) await remove.trigger("click");
      await flushPromises();
      expect(brokerRows()).toEqual([]);
      const save = app.findAll("button").find(button => button.text() === "保存设置")!;
      expect((save.element as HTMLButtonElement).disabled).toBe(false);
      await save.trigger("click");
      await flushPromises();
      // No `brokers` key rather than an empty one: the save is silent about
      // relays, which the sidecar reads as "keep the ones you have".
      expect(vi.mocked(bridge.companionRemoteConfigure)).toHaveBeenCalledExactlyOnceWith(
        expect.not.objectContaining({ brokers: expect.anything() }),
      );
      expect(vi.mocked(bridge.companionRemoteConfigure)).toHaveBeenCalledWith(
        expect.objectContaining({ mode: "readonly", pageUrl: "https://companion.example/" }),
      );
      // A save that carried no relay list still has the last word on the box,
      // so what the sidecar really holds is what is left on screen.
      expect(brokerRows()).toEqual([
        "wss://one.example:8084/mqtt",
        "wss://two.example:8084/mqtt",
      ]);
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
      // A page that loaded is read again on the next visit, because a value it
      // is showing may have been changed somewhere else in the meantime — the
      // overview card's own rename, the phone's panel, the web dashboard — and
      // the copy held here would otherwise be saved back over the newer one.
      vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "renamed-elsewhere" } });
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect((name().element as HTMLInputElement).value).toBe("renamed-elsewhere");
      // An edit on the page is the reader's until they save it, though: a visit
      // that finds one folds in only what another surface changed, by name, and
      // never re-reads the form over the top of what is being typed in it.
      await name().setValue("half-typed");
      const reads = vi.mocked(bridge.settings).mock.calls.length;
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect((name().element as HTMLInputElement).value).toBe("half-typed");
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

  it("adds a folder picked from disk to the custom path list, once", async () => {
    vi.mocked(bridge.aiProfiles).mockClear().mockResolvedValue({
      tools: [], enabled: [], custom_paths: ["~/one"],
    });
    vi.mocked(bridge.chooseFolder).mockClear().mockResolvedValue("D:/Notes");
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      const box = () => app.get('[placeholder="每行一个路径"]');
      const browse = () => app.findAll("button").find(button => button.text().includes("浏览目录"))!;
      await browse().trigger("click");
      await flushPromises();
      // Appended to the line already in the box, not put in its place: the
      // box is still the source of truth and still editable by hand.
      expect((box().element as HTMLTextAreaElement).value).toBe("~/one\nD:/Notes");
      expect(app.text()).toContain("已加入 D:/Notes");
      // The same folder twice is one directory, not two lines.
      await browse().trigger("click");
      await flushPromises();
      expect((box().element as HTMLTextAreaElement).value).toBe("~/one\nD:/Notes");
      expect(app.text()).toContain("这个目录已经在列表里：D:/Notes");
      // Typed into, the picked-folder note stops describing the box.
      await box().setValue("~/two");
      await flushPromises();
      expect(app.text()).not.toContain("这个目录已经在列表里");
    } finally {
      app.unmount();
      vi.mocked(bridge.chooseFolder).mockReset();
    }
  });

  it("names the watch paths the walk found nothing in, and re-walks after a save", async () => {
    // A path that is not on disk and a directory that holds nothing collect the
    // same zero rows, and only one of them is a mistake the reader can fix — so
    // the page says which.  And the tools and the paths are what the walk reads,
    // so a save has to walk again: a list left over from before it describes a
    // machine that no longer exists.
    vi.mocked(bridge.aiProfiles).mockClear().mockResolvedValue({
      tools: [{ key: "claude", label: "Claude" }], enabled: [], custom_paths: ["~/typo", "~/empty"],
    });
    vi.mocked(bridge.updateAiProfiles).mockClear().mockResolvedValue({});
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({
      entries: [], collected_at: 1, tools: [], custom_paths: ["~/typo", "~/empty"],
      roots: [
        { root_index: 0, tool: "custom", root: "r0", kind: "dir", path: "~/typo", count: 0, exists: false },
        { root_index: 1, tool: "custom", root: "r1", kind: "dir", path: "~/empty", count: 0, exists: true },
      ],
    } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      // Saving is what walks now, and it is the moment the paths above can
      // first be wrong.
      await app.findAll("button").find(button => button.text().includes("保存 AI 配置"))!.trigger("click");
      await flushPromises();
      expect(bridge.aiLocal).toHaveBeenLastCalledWith("listing");
      const note = app.get(".ai-root-issue").text();
      expect(note).toContain("~/typo");
      expect(note).toContain("目录不存在");
      expect(note).toContain("~/empty");
      expect(note).toContain("没有可同步的文件");
    } finally {
      app.unmount();
      vi.mocked(bridge.aiProfiles).mockResolvedValue({ tools: [], enabled: [], custom_paths: [] });
    }
  });

  it("names the paths a tool would share, and says a save does not sync by itself", async () => {
    // The checkbox only named the tool, so "sync Codex?" was a question about a
    // directory the card never mentioned.  And the card is a saved form whose
    // every other word is 同步, which reads as a switch that keeps two machines
    // in step — nothing here moves a file until the reader pulls one.
    vi.mocked(bridge.aiProfiles).mockClear().mockResolvedValue({
      tools: [{ key: "codex", label: "Codex", entries: [
        { id: "config", path: "~/.codex/config.toml", kind: "file" },
        { id: "rules", path: "~/.codex/rules", kind: "dir" },
      ] }], enabled: [], custom_paths: [],
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      const paths = app.get(".ai-tool-paths").text();
      expect(paths).toContain("~/.codex/config.toml");
      // A directory reads as one, so a whole folder is not taken for a file.
      expect(paths).toContain("~/.codex/rules/");
      expect(app.text()).toContain("配置不会自动同步");
      // The walk's answer is on the next tab, and a save is what walks, so
      // until one has happened there is nothing to go and look at.
      expect(app.findAll("button").some(button => button.text().includes("查看本机配置"))).toBe(false);
      vi.mocked(bridge.updateAiProfiles).mockClear().mockResolvedValue({});
      vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({
        entries: [{ tool: "codex", root: "config", rel_path: "config.toml" }],
        roots: [{ root_index: 0, tool: "codex", root: "config", kind: "file",
          path: "~/.codex/config.toml", count: 1, exists: true }],
      } as any);
      await app.findAll("button").find(button => button.text().includes("保存 AI 配置"))!.trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text().includes("查看本机配置（1）"))!.trigger("click");
      await flushPromises();
      // The button goes to the tab that holds the answer rather than only
      // reporting a count the reader must then go and find.
      expect(app.findAll('[aria-label="搜索本机配置"]').length).toBe(1);
    } finally {
      app.unmount();
      vi.mocked(bridge.aiProfiles).mockResolvedValue({ tools: [], enabled: [], custom_paths: [] });
    }
  });

  it("says which typed path a ticked tool already covers, instead of walking it twice", async () => {
    // The walk drops a custom path that is one of a ticked tool's own
    // directories — walking it again would advertise every file under it twice
    // — and the path stays in the box, so it looked saved and simply never
    // came up.
    vi.mocked(bridge.aiProfiles).mockClear().mockResolvedValue({
      tools: [{ key: "codex", label: "Codex" }], enabled: ["codex"],
      custom_paths: ["~/.codex/config.toml"],
    });
    vi.mocked(bridge.updateAiProfiles).mockClear().mockResolvedValue({});
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({
      entries: [], collected_at: 1, tools: [], custom_paths: ["~/.codex/config.toml"],
      roots: [{ root_index: 0, tool: "codex", root: "config", kind: "file",
        path: "~/.codex/config.toml", count: 0, exists: false }],
    } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text().includes("保存 AI 配置"))!.trigger("click");
      await flushPromises();
      expect(app.text()).toContain("这些路径已由勾选的工具包含");
      expect(app.text()).toContain("~/.codex/config.toml");
      // It is not a path something went wrong with, so it is not one of the
      // ones the page names as broken.
      expect(app.find(".ai-root-issue").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.aiProfiles).mockResolvedValue({ tools: [], enabled: [], custom_paths: [] });
    }
  });

  it("reads a remote file into a panel of its own, and drops it when the reader moves on", async () => {
    // The preview used to arrive in the card's status line — the same line that
    // reports 已读取 N 个远程配置项 and 拉取完成 — so what that line said
    // depended on which button the reader had clicked last.  A file is not a
    // status, and it is a file from the peer the picker names: choosing another
    // peer leaves a panel quoting a device the list is no longer showing.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "a", name: "First", paired: true } as any,
      { id: "b", name: "Second", paired: true } as any,
    ] });
    // Reset rather than clear, and the same answer however often it is asked:
    // opening the settings page primes the picker before the reader reads.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({
      peers: { a: { entries: [{ tool: "custom", root: "", rel_path: "remote.md" }] } },
    });
    vi.mocked(bridge.aiPreview).mockReset()
      .mockResolvedValue({ content: "the peer's own words", truncated: true });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="预览 remote.md"]').trigger("click");
      await flushPromises();
      const panel = app.get('[aria-label="远程配置预览"]');
      // The file, under the path it was read from, and a note that what is on
      // screen is not the whole of it.
      expect(panel.text()).toContain("remote.md");
      expect(panel.get("pre").text()).toBe("the peer's own words");
      expect(panel.text()).toContain("内容已截断");
      // The status line keeps the card's own report to itself.
      expect(app.get(".ai-pull-status").text()).not.toContain("the peer's own words");
      // Another device's list is not the one this file came from.
      await app.get('[aria-label="AI 配置远程设备"]').setValue("b");
      await flushPromises();
      expect(app.find('[aria-label="远程配置预览"]').exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.aiPreview).mockReset();
    }
  });

  it("stops the window's own polls while it is not on screen, and pays them back on return", async () => {
    // The chat badge and the internet card's online states are read on timers
    // for a reader who is on another page — which is nobody at all when the
    // window is minimized.  Both stand down, and both are read the moment it
    // comes back rather than at the end of the next interval.
    vi.useFakeTimers();
    vi.mocked(bridge.chatSessions).mockClear();
    vi.mocked(bridge.internetPairingStatus).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
      const before = vi.mocked(bridge.internetPairingStatus).mock.calls.length;
      await vi.advanceTimersByTimeAsync(11000);
      expect(vi.mocked(bridge.internetPairingStatus).mock.calls.length).toBeGreaterThan(before);
      const hide = async (hidden: boolean) => {
        Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
        await flushPromises();
      };
      await hide(true);
      const away = vi.mocked(bridge.internetPairingStatus).mock.calls.length;
      await vi.advanceTimersByTimeAsync(20000);
      expect(vi.mocked(bridge.internetPairingStatus).mock.calls.length).toBe(away);
      // Back: the card is read at once, without waiting out the interval.
      await hide(false);
      expect(vi.mocked(bridge.internetPairingStatus).mock.calls.length).toBe(away + 1);
    } finally {
      app.unmount();
      Object.defineProperty(document, "hidden", { value: false, configurable: true });
      vi.useRealTimers();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
    }
  });

  it("offers a way to investigate a relay that is down, from the card that reports it", async () => {
    // 本机中继：离线 made every device under it read 离线 and offered nothing to
    // press: the one control that can tell a broker that is down from a network
    // that blocks it is on the settings page's advanced card.  See U126.
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true, relay: "off" } as any);
    vi.mocked(bridge.testRelayBrokers).mockResolvedValue({ total: 1, reachable: 0, results: [] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();

      const why = app.get(".relay-state__why");
      expect(why.text()).toContain(t("查看原因"));
      await why.trigger("click");
      await flushPromises();

      // The card it lands on, and the test run on the way in: the reader asked
      // why, not where.
      expect(app.find("#settings-advanced").isVisible()).toBe(true);
      expect(bridge.testRelayBrokers).toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      vi.mocked(bridge.testRelayBrokers).mockReset();
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

  it("refuses to save a JSON config that no longer parses, and says so as it is fixed", async () => {
    vi.mocked(bridge.aiLocal).mockClear()
      .mockResolvedValueOnce({ entries: [{ tool: "custom", root: "", rel_path: "settings.json" }], roots: [] })
      .mockResolvedValueOnce({ content: '{"theme":"dark"}' });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 settings.json"]').trigger("click");
      await flushPromises();
      // The file is JSON, so the editor says who checks it before the save.
      expect(app.get(".ai-editor").text()).toContain("这是 JSON 文件");
      const editor = () => app.get('[aria-label="AI 配置编辑器"]');
      await editor().setValue('{"theme":}');
      await flushPromises();
      // Caught as it is typed, not at the save: the border and the line both.
      expect(editor().attributes("aria-invalid")).toBe("true");
      expect(app.get(".ai-editor .field-error").text()).toContain("这不是有效的 JSON");
      await app.findAll("button").find(button => button.text() === "保存配置")!.trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.aiLocal).mock.calls.some(call => call[0] === "save")).toBe(false);
      expect(app.get(".ai-editor").text()).toContain("未保存");
      // Fixed, and the message goes away without another save attempt.
      await editor().setValue('{"theme":"light"}');
      await flushPromises();
      expect(editor().attributes("aria-invalid")).toBe("false");
      expect(app.find(".ai-editor .field-error").exists()).toBe(false);
      await app.findAll("button").find(button => button.text() === "保存配置")!.trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.aiLocal).mock.calls).toContainEqual(["save", "custom", "", "settings.json", '{"theme":"light"}']);
    } finally { app.unmount(); }
  });

  it("puts the AI editor away, asking first when the file has unsaved changes", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiLocal).mockClear()
      .mockResolvedValueOnce({ entries: [{ tool: "custom", root: "", rel_path: "note.md" }], roots: [] })
      .mockResolvedValueOnce({ content: "as saved" });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 note.md"]').trigger("click");
      await flushPromises();
      // Untouched, so putting it away is just putting it away.
      await app.get('[aria-label="关闭编辑器"]').trigger("click");
      await flushPromises();
      expect(app.find(".ai-editor").exists()).toBe(false);
      // Reopened and typed into: the editor covers the card, so it needs a way
      // out — but a file with work in it is not dropped without asking, and
      // the question says what becomes of the work.
      await app.get('[aria-label="编辑 note.md"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="AI 配置编辑器"]').setValue("half a thought");
      await app.get('[aria-label="关闭编辑器"]').trigger("click");
      await flushPromises();
      const dialog = () => app.get('[aria-labelledby="ai-discard-title"]');
      expect(dialog().text()).toContain("放弃修改并关闭");
      await dialog().findAll("button").find(button => button.text().includes("继续编辑"))!.trigger("click");
      await flushPromises();
      expect(app.find(".ai-editor").exists()).toBe(true);
      await app.get('[aria-label="关闭编辑器"]').trigger("click");
      await flushPromises();
      await dialog().get(".danger").trigger("click");
      await flushPromises();
      // Put away, not left open on another file's text.
      expect(app.find(".ai-editor").exists()).toBe(false);
    } finally { app.unmount(); }
  });

  it("says a walk that found nothing found nothing, and says it only once there has been one", async () => {
    // An empty list and a list that was never read look the same on screen, and
    // the answer most machines give the first time IS empty: a tool writes its
    // config when it is first used, not when it is installed.
    vi.mocked(bridge.aiLocal).mockClear().mockResolvedValue({
      entries: [],
      roots: [{ tool: "codex", root: "config", kind: "file", path: "~/.codex/config.toml", count: 0, exists: false }],
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      // No walk yet, so nothing to report — not even this.
      expect(app.text()).not.toContain("本机还没有可同步的配置项");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      expect(app.text()).toContain("本机还没有可同步的配置项");
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

  it("offers one more try at only the part of a batch that failed", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "custom", root: "", rel_path: "one.md" },
      { tool: "custom", root: "", rel_path: "two.md" },
    ] } } });
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [], roots: [] });
    // The peer refused the whole batch, so nothing landed and nothing is
    // ambiguous about what to try again.
    vi.mocked(bridge.aiPull).mockReset().mockResolvedValue({ requested: 0, errors: ["legacy_peer_read_only"] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="选择本页全部远程配置"]').setValue(true);
      await flushPromises();
      await app.findAll("button").find(button => button.text().includes("拉取选中项"))!.trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledTimes(1);
      expect(app.text()).toContain("0 个成功、2 个失败");
      // The retry is a second request for the same two files, not a new
      // selection the reader has to rebuild from the list.
      const retry = app.findAll("button").find(button => button.text().includes("重试失败的 2 项"))!;
      expect(retry.exists()).toBe(true);
      await retry.trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledTimes(2);
      expect(bridge.aiPull).toHaveBeenLastCalledWith("a", [
        { tool: "custom", root: "", rel_path: "one.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "two.md", is_dir: false },
      ], "copy");
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
      // The number on the button is repeated as the names it stands for: the
      // step before a pull has to be checkable, because a migration that takes
      // the wrong files writes over configuration nobody can rebuild from
      // memory.  Grouped by the tool that owns them, since that is the decision
      // being made ("do I want this program's settings?").  See U162.
      expect(app.get(".migrate-checklist").text()).toContain("absent.md");
      expect(app.get(".migrate-group__head").text()).toContain("custom");
      // And each strategy says what choosing it does — the one that overwrites
      // was the least explained of the three, and the only one that loses
      // anything.  See U163.
      expect(app.get(".migrate-strategies").text()).toContain("本机已有的配置一个都不动");
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
    await app.get('[aria-label="批量置顶"]').trigger("click");
    await flushPromises();
    expect(bridge.batchPinHistory).toHaveBeenLastCalledWith(["1"], true);
    await app.get('[aria-label="批量取消置顶"]').trigger("click");
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
  it("says why a batch cannot grow past its bound", async () => {
    // The bound is the sidecar's (an id list of 1..100) and the reader meets it
    // as every unticked box on the page going grey at once: a limit that only
    // shows itself by disabling things reads as a page that has broken.
    vi.mocked(bridge.history).mockResolvedValue(historyPage(
      Array.from({ length: 100 }, (_, index) => historyRow({ id: String(index + 1) }))));
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="全选当前页"]').setValue(true);
      await flushPromises();
      expect(app.text()).toContain("已选择 100 条");
      // Filled exactly, which is the moment the wall arrives.
      expect(app.text()).toContain("一次最多选 100 条");
    } finally { app.unmount(); }
    // One row more and select-all cannot offer the page at all — the label
    // would promise rows the batch cannot hold.  It says why instead of only
    // going grey.
    vi.mocked(bridge.history).mockResolvedValue(historyPage(
      Array.from({ length: 101 }, (_, index) => historyRow({ id: String(index + 1) }))));
    const more = mountOn("history");
    try {
      await flushPromises();
      const all = more.get('[aria-label="全选当前页"]');
      expect(all.attributes("disabled")).toBeDefined();
      expect(all.attributes("title")).toContain("一次最多选 100 条");
    } finally { more.unmount(); }
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
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 2, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Studio", offer_entry: "42" }),
      // The same row as an offer this build cannot read: nothing to ask with.
      historyRow({ id: "9", content_type: "FILE_REMOTE", preview: "later.pdf",
        source_device: "peer-1", source_name: "Studio" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      // 复制 would clear this machine's clipboard for a file it does not have,
      // so the row offers the fetch-and-paste in its place rather than beside
      // it; the explicit download is the menu's secondary entry.
      expect(app.find('[aria-label="复制记录"]').exists()).toBe(false);
      await clickDownloadRemote(app, 0);
      // The id out of the row's offer, not the row's own: history ids are
      // numbered per machine, so "7" here names a different record over there —
      // the ask would come back as that record's file, or as a refusal.
      expect(bridge.requestEntryFiles).toHaveBeenCalledExactlyOnceWith("42", "peer-1");
      // The id on the wire is also what a refusal comes back with, so it is
      // what marks the row as waiting.
      const pending = await downloadRemoteEntry(app, 0);
      expect(pending.attributes("title")).toBe(t("已请求下载，正在等待那台设备"));
      // A row whose offer named no entry is not offered a click that could only
      // ask the peer for the wrong record.
      const unreadable = await downloadRemoteEntry(app, 1);
      expect(unreadable.attributes("aria-disabled")).toBeDefined();
      expect(unreadable.attributes("title")).toBe(t("这条记录无法下载"));
      app.unmount();
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });


  it("offers a download from a device that is only on the relay, and names the route", async () => {
    // The row reads online -- the relay's own view -- and has no LAN link behind it.  That used to
    // mean the files could not travel at all, because they were cut at 256 KiB for a channel whose
    // certificate is pinned.  They are cut to fit a broker message now and chunk-acked at the far
    // end, so the row offers the download again; what it must not do is promise a LAN route.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Studio away", paired: true, relay: true,
        connection_state: "online", pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Studio away", offer_entry: "42" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      const entry = await downloadRemoteEntry(app);
      expect(entry.attributes("aria-disabled"), "a relay peer is a download source now").toBeUndefined();
      // The route is named before the click, because a relay transfer is slower and the reader is
      // about to move a file of unknown size across the internet.
      expect(entry.attributes("title"))
        .toBe(t("从 {name} 下载这个文件（经互联网，比局域网慢）", { name: "Studio away" }));
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("offers the download when the device is reachable over the relay, not the local link", async () => {
    // The reported state: a LAN-owned device (relay false) that is away, so `connection_state` says
    // offline, while the relay join it also holds is up.  The clips arrive; the button must not say
    // 当前不在线.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Kais-MacBook", paired: true, relay: false,
        relay_paired: true, relay_online: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Kais-MacBook", offer_entry: "42" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      const entry = await downloadRemoteEntry(app);
      const title = entry.attributes("title");
      expect(title, "the row said the device was offline while its clips were arriving")
        .not.toContain(t("当前不在线"));
      expect(entry.attributes("aria-disabled")).toBeUndefined();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("still offers the download when neither route looks online, and lets the sidecar answer", async () => {
    // 严重-7.  The row cannot know whether an internet-paired device is
    // reachable: that reading is a 90-second in-process window over the last
    // frame heard from it, empty after a restart and lapsed while the peer is
    // merely idle.  It used to gate the button, so a download the backend would
    // have accepted was a grey button whose tooltip said the device was offline.
    // The hint stays; the request goes, and `NOT_CONNECTED` is what answers.
    vi.clearAllMocks();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Kais-MacBook", paired: true, relay: false,
        relay_paired: true, relay_online: false, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Kais-MacBook", offer_entry: "42" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      const entry = await downloadRemoteEntry(app);
      expect(entry.attributes("aria-disabled"), "the window is not the one deciding reachability").toBeUndefined();
      expect(entry.attributes("title")).toContain(t("可能不在线"));
      await entry.trigger("click");
      await flushPromises();
      expect(bridge.requestEntryFiles).toHaveBeenCalledExactlyOnceWith("42", "peer-1");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("still offers the download from the same device when it is here on this network", async () => {
    // The control.  `relay` marks the row, not the pairing: the same device paired by code *and*
    // seen on this network gets a LAN row, and there the file does travel.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Studio", paired: true, relay: false,
        connection_state: "online", pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
        source_device: "peer-1", source_name: "Studio", offer_entry: "42" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      const entry = await downloadRemoteEntry(app);
      expect(entry.attributes("aria-disabled")).toBeUndefined();
      expect(entry.attributes("title")).toBe(t("从 Studio 下载这个文件"));
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("offers the download on a row whose kind says text but whose offer is a file", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Studio", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    // What a Finder copy actually reaches this window as.  The Finder puts the
    // file's *name* on the pasteboard as plain text beside the file, and the
    // sidecar used to rank the text above the file — so a row that could be
    // downloaded the whole time was labelled TEXT and offered 复制, which is the
    // reported "no download button".  The offer is the evidence that survives
    // the label, so the button is offered on it either way.
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ id: "7", content_type: "TEXT", preview: "报告.pdf",
        source_device: "peer-1", source_name: "Studio", offer_entry: "42" }),
    ] });
    const app = mountOn("history");
    try {
      await flushPromises();
      expect(app.find('[aria-label="复制记录"]').exists()).toBe(false);
      // The row's own button is the paste; the menu holds the download.
      expect(app.find('[aria-label="取回并粘贴"]').exists()).toBe(true);
      await clickDownloadRemote(app);
      expect(bridge.requestEntryFiles).toHaveBeenCalledExactlyOnceWith("42", "peer-1");
      app.unmount();
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  /* ── The on-demand paste of a remote file (1.3.1) ────────────────────── */

  /** A remote-file row: its bytes are on the peer until a paste fetches them. */
  function remoteFileRow(overrides: Record<string, unknown> = {}) {
    return historyRow({
      id: "7", content_type: "FILE_REMOTE", preview: "report.pdf",
      source_device: "peer-1", source_name: "Studio", offer_entry: "42",
      ...overrides,
    });
  }

  /** Mount the history page with one remote-file row, and capture the
   *  `sidecar:event` handler the window registers for the fetch. */
  let sidecar: ((event: { type: string; name?: string; session_id: string; data?: unknown }) => void) | null = null;
  async function mountRemote(rows = [remoteFileRow()]) {
    sidecar = null;
    vi.mocked(bridge.hideMainAndPaste).mockClear();
    vi.mocked(bridge.cancelPasteEntry).mockReset().mockResolvedValue({ cancelled: 1 });
    vi.mocked(bridge.injectPaste).mockClear();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "peer-1", name: "Studio", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.history).mockResolvedValue({
      session_id: "s", seq: 0, offset: 0, total: rows.length, items: rows,
    } as any);
    vi.mocked(bridge.preparePasteEntry).mockReset().mockResolvedValue({ requested: true });
    vi.mocked(bridge.onSidecarEvent).mockReset().mockImplementation(async (handler: any) => {
      sidecar = handler;
      return () => { sidecar = null; };
    });
    const app = mountOn("history");
    await flushPromises();
    return app;
  }
  /** Deliver the event the host would forward for this paste. */
  async function emitPasteEvent(name: string, data: Record<string, unknown>) {
    sidecar?.({ type: "event", name, session_id: "s", data });
    await flushPromises();
    await flushPromises();
  }

  it("starts the fetch from the row's own button and draws the window's bar", async () => {
    vi.mocked(bridge.transfers).mockResolvedValue({ active: [
      { id: "t1", filename: "report.pdf", size: 2048, direction: "down", status: "running",
        progress: 50, speed: 1024, eta: "1 分钟", paste_entry: "7" },
    ], history: [], speed_test: {} } as any);
    const app = await mountRemote();
    try {
      // The row's 复制 is gone (the bytes are not here yet); its button starts
      // the fetch-and-paste instead.
      expect(app.find('[aria-label="复制记录"]').exists()).toBe(false);
      await app.get('[aria-label="取回并粘贴"]').trigger("click");
      await flushPromises();
      expect(bridge.preparePasteEntry).toHaveBeenCalledWith("7", false);
      // A paste that finishes after the click is visible from anywhere in the
      // window: a fixed bar above the status line, not a page-local spinner.
      expect(app.find(".paste-fetch").exists()).toBe(true);
      expect(app.get(".paste-fetch").text()).toContain("report.pdf");
      expect(app.get(".paste-fetch").text()).toContain("50%");
      // It is not pasted until the files are here.
      expect(bridge.hideMainAndPaste).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("hides the window and pastes once the files arrive", async () => {
    const app = await mountRemote();
    try {
      await app.get('[aria-label="取回并粘贴"]').trigger("click");
      await flushPromises();
      await emitPasteEvent("clip_files.paste_ready", { entry_id: "7", paths: ["C:/files/report.pdf"] });
      // The backend already wrote the clipboard and rewrote the row; the window
      // only has to step out of the way and let the host type the chord.
      expect(bridge.hideMainAndPaste).toHaveBeenCalledOnce();
      expect(app.find(".paste-fetch").exists()).toBe(false);
      expect(app.text()).toContain(t("已取回 {count} 个文件并粘贴", { count: 1 }));
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("says why a fetch failed, and takes the bar away", async () => {
    const app = await mountRemote();
    try {
      await app.get('[aria-label="取回并粘贴"]').trigger("click");
      await flushPromises();
      await emitPasteEvent("clip_files.paste_failed", { entry_id: "7", reason: "timeout" });
      expect(app.find(".paste-fetch").exists()).toBe(false);
      expect(app.text()).toContain(t("等待传输超时，请重试"));
      // Nothing was pasted and the row is still remote — the failure is the
      // whole answer, not a half-success.
      expect(bridge.hideMainAndPaste).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("cancels the fetch from the bar, on the runtime side too", async () => {
    const app = await mountRemote();
    try {
      await app.get('[aria-label="取回并粘贴"]').trigger("click");
      await flushPromises();
      await app.get(".paste-fetch").findAll("button").find(node => node.text() === t("取消取回"))!.trigger("click");
      await flushPromises();
      expect(bridge.cancelPasteEntry).toHaveBeenCalledWith("7");
      expect(app.find(".paste-fetch").exists()).toBe(false);
      expect(app.text()).toContain(t("已取消取回文件"));
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("starts the same fetch from the row menu's 粘贴到当前应用", async () => {
    const app = await mountRemote();
    try {
      await app.findAll(".history-row")[0].trigger("contextmenu");
      await flushPromises();
      // The primary entry is the paste on every kind of row; for a remote file
      // it means "fetch it, then paste it" rather than "copy it".  The menu
      // entry's text also carries its shortcut, so its label span is the name.
      const entry = app.findAll(".context-menu-item")
        .find(node => node.get(".context-menu-label").text() === t("粘贴到当前应用"));
      expect(entry, "no 粘贴到当前应用 entry on a remote row").toBeTruthy();
      await entry!.trigger("click");
      await flushPromises();
      expect(bridge.preparePasteEntry).toHaveBeenCalledWith("7", false);
      expect(app.find(".paste-fetch").exists()).toBe(true);
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("asks before a large paste and shows its size from the row's offer", async () => {
    // PASTE_CONFIRM_REQUIRED's error reaches the window without the sidecar's
    // `details`, so the size comes from the same preview read the hover card
    // makes: the offer's files, summed.
    vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
      kind: "files", image: "", width: 0, height: 0,
      files: [
        { name: "report.pdf", size: 1024, kind: "file", exists: false },
        { name: "photos.zip", size: 2048, kind: "file", exists: false },
      ],
      total: 2,
    } as any);
    const app = await mountRemote();
    try {
      vi.mocked(bridge.preparePasteEntry).mockRejectedValue({
        code: "PASTE_CONFIRM_REQUIRED", message: "too large", retryable: false,
      });
      await app.get('[aria-label="取回并粘贴"]').trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="paste-confirm-title"]');
      expect(dialog.text()).toContain(t("文件约 {size}，粘贴前需先从另一台设备传输。继续？", { size: "3.0 KB" }));
      await dialog.findAll("button").find(node => node.text() === t("继续"))!.trigger("click");
      await flushPromises();
      // The answer is the same call with the confirmation attached; the sidecar
      // remembers it for this attempt only.
      expect(bridge.preparePasteEntry).toHaveBeenLastCalledWith("7", true);
    } finally {
      vi.mocked(bridge.previewHistoryEntry).mockResolvedValue({
        kind: "", image: "", width: 0, height: 0, files: [], total: 0,
      } as any);
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });

  it("fetches and pastes a remote file from a paste shortcut, with no bar", async () => {
    // The reader pressed the key in another application, so there is nowhere to
    // draw progress: the fetch runs headless and only its outcome is said.  A
    // large-file question cannot be answered from here either, so it is a
    // sentence pointing at the surface that can ask.
    let fireHotkey!: (action: string) => void;
    let hotkeySidecar: ((event: any) => void) | undefined;
    vi.mocked(bridge.onHotkey).mockImplementation(async (handler: any) => {
      fireHotkey = handler;
      return () => {};
    });
    vi.mocked(bridge.onSidecarEvent).mockReset().mockImplementation(async (handler: any) => {
      hotkeySidecar = handler;
      return () => { hotkeySidecar = undefined; };
    });
    vi.mocked(bridge.history).mockResolvedValue({
      session_id: "s", seq: 0, offset: 0, total: 1, items: [remoteFileRow()],
    } as any);
    vi.mocked(bridge.preparePasteEntry).mockReset().mockResolvedValue({ requested: true });
    vi.mocked(bridge.injectPaste).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      fireHotkey("paste_1");
      await flushPromises();
      expect(bridge.preparePasteEntry).toHaveBeenCalledWith("7");
      expect(app.find(".paste-fetch").exists()).toBe(false);
      hotkeySidecar?.({
        type: "event", name: "clip_files.paste_ready", session_id: "s",
        data: { entry_id: "7", paths: ["C:/files/report.pdf"] },
      });
      await flushPromises();
      await flushPromises();
      expect(bridge.injectPaste).toHaveBeenCalledOnce();
      expect(app.text()).toContain(t("已取回 {count} 个文件并粘贴", { count: 1 }));
    } finally {
      app.unmount();
      vi.mocked(bridge.onHotkey).mockResolvedValue(() => {});
      vi.mocked(bridge.onSidecarEvent).mockResolvedValue(() => {});
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
      vi.mocked(bridge.injectPaste).mockResolvedValue({ ok: true } as any);
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
      // The row itself now asks for the small picture, before any pointer
      // reaches it; that is the row's thumbnail, not the hover card.
      expect(app.get(".history-thumb").attributes("src")).toBe("data:image/png;base64,AAAA");
      expect(bridge.previewHistoryEntry).toHaveBeenCalledWith("img-1");
      vi.mocked(bridge.previewHistoryEntry).mockClear();
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
  it("holds a row's card open on a double click, and lets it go on Esc", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 2, items: [
      historyRow({ id: "long-1", preview: "a note long enough to be worth keeping open" }),
      historyRow({ id: "long-2", preview: "another note" }),
    ] });
    const app = mountOn("history", document.body);
    try {
      await flushPromises();
      const rows = app.findAll(".history-row");
      // The card follows the pointer until it is asked to stay: leaving the row
      // closes it.
      await rows[0].trigger("mouseenter");
      await rows[0].trigger("mouseleave");
      expect(app.find(".history-preview").exists()).toBe(false);

      // A double click is the "stay" — and the second half of it must not be
      // read as a copy of the row.
      await rows[0].trigger("dblclick");
      const card = app.get(".history-preview");
      expect(card.attributes("aria-hidden")).toBe("true");
      expect(card.text()).toContain("a note long enough to be worth keeping open");
      // It says it is pinned, and how to let go: nothing else on screen does.
      expect(card.text()).toContain(t("已固定 · Esc 关闭"));
      await rows[0].trigger("mouseleave");
      expect(app.find(".history-preview").exists()).toBe(true);

      // A pointer crossing another row does not swap the card the reader pinned.
      await rows[1].trigger("mouseenter");
      expect(app.get(".history-preview").text()).toContain("a note long enough to be worth keeping open");

      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
      await flushPromises();
      expect(app.find(".history-preview").exists()).toBe(false);
      app.unmount();
    } finally {
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
    // Clearing the whole history is a settings action now: it left the
    // user-facing history toolbar.
    await app.get('[aria-label="设置"]').trigger("click");
    await flushPromises();
    await app.findAll("button").find(button => button.text() === "安全")!.trigger("click");
    await flushPromises();
    await app.findAll("button").find(button => button.text() === "清空全部历史")!.trigger("click");
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
    await openDeviceRowMenu(app);
    await clickMenuItem(app, "发送网址");
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
      // 发送网址 is on the row's menu now, and it carries the same explanation
      // the row button used to: dimmed, with the sentence as its tooltip.
      await openDeviceRowMenu(app);
      const sendUrl = app.findAll(".context-menu-item").find(node => node.text() === "发送网址")!;
      expect(sendUrl.attributes("aria-disabled")).toBe("true");
      expect(sendUrl.attributes("title")).toBe("同步引擎未运行，推送文本与发送网址不可用。");
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
    const logsDialog = app.get('[aria-labelledby="logs-title"]');
    await logsDialog.findAll("button").find((button) => button.text().includes("全部"))!.trigger("click");
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
    await app.get('[aria-label="局域网：撤销信任"]').trigger("click");
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
      await button(app, "断开连接").trigger("click");
      await flushPromises();
      expect(bridge.disconnectDevice).toHaveBeenCalledWith("t");
      await app.get('[aria-label="全部：移除设备"]').trigger("click");
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
      // A full value, because the dialog's job is a comparison and a short form
      // cannot be compared character by character.  It is shown in groups of
      // four -- see U122 -- with the short form beside it as the secondary
      // value the device list already uses.
      { device_id: "t", device_name: "Trusted", fingerprint_short: "ab12cd34...ef56ab12",
        fingerprint: "ab12cd34ef56ab12ab12cd34ef56ab12ab12cd34ef56ab12ab12cd34ef56ab12", paired: true },
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
      await openDeviceRowMenu(app);
      await clickMenuItem(app, "测试连接");
      expect(bridge.testDevice).toHaveBeenCalledWith("t");
      expect(app.text()).toContain("连接测试：局域网 13ms · 中继 超时");
      await app.get('[aria-label="查看证书指纹"]').trigger("click");
      await flushPromises();
      expect(bridge.deviceCerts).toHaveBeenCalledOnce();
      // The whole fingerprint, in groups of four, and the short form as the
      // secondary line beside it.
      expect(app.find(".fingerprint--full").text()).toContain("ab12 cd34 ef56 ab12");
      expect(app.text()).toContain("短号 ab12cd34...ef56ab12");
      expect(app.find(".modal--wide").exists()).toBe(true);
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.deviceCerts).mockResolvedValue({ devices: [] });
    }
  });
  it("carries an update in whichever direction the two builds are apart", async () => {
    vi.mocked(bridge.offerDeviceUpdate).mockResolvedValue({ sent: true, reason: "accepted" });
    vi.mocked(bridge.fetchDeviceUpdate).mockResolvedValue({ sent: true });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      // Online, because an update can only be offered down a live local link:
      // the sidecar refuses an offer that arrives over the relay, and the one
      // it sends has no relay fallback.  A sighting keeps saying what version
      // a device runs long after it stopped being dialable, so a row gated on
      // the sighting alone offered a button whose click could only fail.
      { id: "t", name: "Studio", paired: false, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: true },
      // The direction the feature is meant to run in: this device is the one
      // behind, so its row offers the fetch rather than the send.
      { id: "n", name: "Newer", paired: false, connection_state: "online",
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
      // The row itself no longer advertises the build; the version lives in
      // the row's menu, where it can be read without making every row carry a
      // release number.
      expect(app.get(".device-row").text()).not.toContain("版本 1.0.7");
      const studioRow = app.findAll(".device-row").find(row => row.text().includes("Studio"))!;
      await studioRow.trigger("contextmenu");
      await flushPromises();
      expect(app.findAll(".context-menu-item").map(item => item.text()))
        .toContain("版本 1.0.7");
      const offers = buttons(app, "发送更新");
      expect(offers).toHaveLength(1);
      await offers[0].trigger("click");
      await flushPromises();
      expect(bridge.offerDeviceUpdate).toHaveBeenCalledExactlyOnceWith("t");
      expect(app.text()).toContain("已把更新发送给 Studio");
      // And the other way round, off the same list: the device that is behind
      // asks, and says so while it waits for the blob -- which it is not asked
      // to install afterwards: a staged archive is applied by the app itself.
      const fetches = buttons(app, "获取更新");
      expect(fetches).toHaveLength(1);
      await fetches[0].trigger("click");
      await flushPromises();
      expect(bridge.fetchDeviceUpdate).toHaveBeenCalledExactlyOnceWith("n");
      expect(app.text()).toContain("已向 Newer 索取安装包，收到后会自动安装并重启");
      // The same two actions on the row's own menu, which is where a reader who
      // wants to push a version to a device looks first: what the version is,
      // and the entry that sends it.  Nothing is clicked here -- both actions
      // were just exercised through the row -- so what these hold is that the
      // menu offers them at all, on the row's own terms.
      // Found by name, not by position: the list is ordered by state and then
      // by name (`activeDevices`), so a row's index is the page's own choice
      // and nothing a test about the menu should be pinned to.  This machine's
      // own row heads the list and carries no menu, so it is skipped.
      const rowMenu = async (name: string) => {
        const row = app.findAll(".device-row").find(entry =>
          !entry.classes("device-row--local") && entry.text().includes(name))!;
        await row.trigger("contextmenu");
        await flushPromises();
        return app.findAll(".context-menu-item");
      };
      let entries = await rowMenu("Studio");
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
      entries = await rowMenu("Same");
      const blocked = entries.find(entry => entry.text().includes("发送更新"))!;
      expect(blocked.attributes("aria-disabled")).toBeDefined();
      expect(blocked.attributes("title")).toContain("两台设备版本相同");
      closeContextMenu();
      // And a device that advertises nothing gets none of it: there is no
      // version to name and no reason to give.
      entries = await rowMenu("Silent");
      expect(entries.filter(entry => entry.text().includes("更新"))).toHaveLength(0);
      expect(entries.filter(entry => entry.text().includes("版本"))).toHaveLength(0);
      closeContextMenu();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true, reason: "accepted" });
      vi.mocked(bridge.fetchDeviceUpdate).mockReset().mockResolvedValue({ sent: true });
    }
  });
  it("offers nothing when this machine has no installer to offer", async () => {
    // The send button carried the installer this machine's own upgrade kept, so
    // a machine that has kept none has nothing to send -- and says that in the
    // row's own sentence rather than in a dimmed button's tooltip, which is what
    // the entry used to be.  A control that cannot be pressed is not where the
    // reason belongs; the line that replaces it is.  Downloading one here to
    // pass on would be the same file from the same place, fetched twice, on the
    // machine that does not need it -- the device being offered can fetch it
    // itself.
    vi.mocked(bridge.offerDeviceUpdate).mockResolvedValue({ sent: true, reason: "accepted" });
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
      expect(app.find(".device-version-note").text()).toContain("本机没有可发送的安装包");
      expect(buttons(app, "发送更新")).toHaveLength(0);
      expect(bridge.offerDeviceUpdate).not.toHaveBeenCalled();
      expect(bridge.updateDownload).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true, reason: "accepted" });
      vi.mocked(bridge.updateDownload).mockClear();
    }
  });
  it("says why an update offer did not go, instead of leaving the click silent", async () => {
    vi.mocked(bridge.offerDeviceUpdate).mockRejectedValue({
      code: "update.peer_unreachable", message: "无法连接到该设备，它可能已离线。", retryable: false,
    });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      // Online, because an update can only be offered down a live local link:
      // the sidecar refuses an offer that arrives over the relay, and the one
      // it sends has no relay fallback.  A sighting keeps saying what version
      // a device runs long after it stopped being dialable, so a row gated on
      // the sighting alone offered a button whose click could only fail.
      { id: "t", name: "Studio", paired: false, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: true },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await button(app, "发送更新").trigger("click");
      await flushPromises();
      // The sidecar's own sentence, which it localized: the row shows the
      // reason rather than an English fallback of this file's own making.
      expect(app.text()).toContain("无法连接到该设备，它可能已离线。");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.offerDeviceUpdate).mockReset().mockResolvedValue({ sent: true, reason: "accepted" });
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
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
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
    //
    // The relay's first-use explanation already answered on this installation,
    // so the switch is a switch: what it does the first time is the next case.
    localStorage.setItem("clipsync.internetConsentSeen", "1");
    vi.mocked(bridge.internetPairingStatus).mockResolvedValueOnce({ peers: [], enabled: false } as any);
    const app = mountOn("history");
    try {
      await flushPromises();
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
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

  it("leaves the settings page's save alone after the devices page's switch saved the field", async () => {
    // The switch saves through a call of its own, so the field is stored the
    // moment it is flipped.  The settings page still has to show the new value —
    // but showing it is not the same as holding it unsaved, and a bar offering
    // to save what another page already saved would have no card to point at.
    vi.mocked(bridge.internetPairingStatus).mockResolvedValueOnce({ peers: [], enabled: false } as any);
    // Explained already, so both doors onto the field are plain fields here.
    localStorage.setItem("clipsync.internetConsentSeen", "1");
    // What the sidecar stores, and so what it answers with: the switch is a
    // save, so the field it saves has to come back changed.
    let syncEnabled = false;
    vi.mocked(bridge.settings)
      .mockImplementation(async () => ({ settings: { internet_sync_enabled: syncEnabled } } as any));
    vi.mocked(bridge.updateSettings).mockImplementation(async (patch: any) => {
      if ("internet_sync_enabled" in patch) syncEnabled = patch.internet_sync_enabled;
      return {};
    });
    const app = mountOn("history");
    try {
      await flushPromises();
      // The page has to have been read first: a form that has never loaded is
      // refilled whole on the next visit, which would hide what this is about.
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true } as any);
      await app.get('[aria-label="启用互联网同步"]').setValue(true);
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(app.text()).not.toContain("有未保存的更改");
      // It followed, though: the box on this page is the same field, and a
      // switch that saved it while showing the old value here would be saved
      // back over the top of it on the next 保存.
      const box = () => app.findAll("label")
        .find(node => node.text() === "启用互联网同步")!.get("input");
      expect((box().element as HTMLInputElement).checked).toBe(true);
    } finally {
      app.unmount();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
    }
  });

  it("explains the relay once, before the switch that sends the clipboard off the LAN", async () => {
    // The one setting in the window that takes clipboard text off the local
    // network, and the one that used to say nothing about doing so: two doors
    // onto it (this card and the settings page) and neither explained it.
    // Asked once and only once — a prompt that returns on every enable is a
    // prompt people learn to dismiss unread.
    localStorage.removeItem("clipsync.internetConsentSeen");
    vi.mocked(bridge.updateSettings).mockClear();
    vi.mocked(bridge.internetPairingStatus).mockResolvedValueOnce({ peers: [], enabled: false } as any);
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const showModal = vi.mocked(HTMLDialogElement.prototype.showModal);
    const app = mountOn("history");
    const consent = () => app.get('dialog[aria-labelledby="internet-consent-title"]');
    const inConsent = (label: string) => consent().findAll("button")
      .find(node => node.text() === t(label))!;
    const toggle = () => app.get('[aria-label="启用互联网同步"]');
    try {
      await flushPromises();
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
      // Ticking the box does not turn the relay on: it asks first, and a box
      // left ticked behind a modal would read as already on.
      await toggle().setValue(true);
      await flushPromises();
      expect(showModal.mock.calls.length).toBeGreaterThan(0);
      expect(bridge.updateSettings).not.toHaveBeenCalledWith({ internet_sync_enabled: true });
      expect((toggle().element as HTMLInputElement).checked).toBe(false);
      // Declining leaves the relay off and the question unanswered, so the next
      // attempt asks again rather than counting as consent.
      await inConsent("取消").trigger("click");
      await flushPromises();
      expect(bridge.updateSettings).not.toHaveBeenCalledWith({ internet_sync_enabled: true });
      expect(localStorage.getItem("clipsync.internetConsentSeen")).toBeNull();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: false } as any);
      await toggle().setValue(true);
      await flushPromises();
      expect((toggle().element as HTMLInputElement).checked).toBe(false);
      // Accepting saves the field the switch stands for, and remembers that it
      // was explained so the question does not come back.
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true } as any);
      await inConsent("我了解，开启").trigger("click");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith({ internet_sync_enabled: true });
      expect((toggle().element as HTMLInputElement).checked).toBe(true);
      expect(localStorage.getItem("clipsync.internetConsentSeen")).toBe("1");
      // Turning it back off is a decision to keep the clipboard at home; there
      // is nothing to explain, so nothing opens.
      const openedBefore = showModal.mock.calls.length;
      await toggle().setValue(false);
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith({ internet_sync_enabled: false });
      expect(showModal.mock.calls.length).toBe(openedBefore);
    } finally {
      app.unmount();
      localStorage.removeItem("clipsync.internetConsentSeen");
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
    }
  });

  it("asks before the settings page's save turns the relay on, and puts the box back when declined", async () => {
    // The same switch has a second door.  Saving the settings form is what
    // actually writes the field, so the explanation has to come before the
    // save — and a declined dialog cannot leave a ticked box behind, or the
    // next press would save it with the explanation never accepted.
    localStorage.removeItem("clipsync.internetConsentSeen");
    vi.mocked(bridge.updateSettings).mockClear();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: false } as any);
    const app = mountOn("history");
    const box = () => app.findAll("label")
      .find(node => node.text() === "启用互联网同步")!.get("input");
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await box().setValue(true);
      await app.findAll("button").find(node => node.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).not.toHaveBeenCalled();
      await app.get('dialog[aria-labelledby="internet-consent-title"]')
        .findAll("button").find(node => node.text() === t("取消"))!.trigger("click");
      await flushPromises();
      expect((box().element as HTMLInputElement).checked).toBe(false);
      expect(bridge.updateSettings).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      localStorage.removeItem("clipsync.internetConsentSeen");
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      vi.mocked(bridge.updateSettings).mockResolvedValue({});
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
    // The paragraph, not the row: this case is about the preview line, and the
    // row is where the line under it lives -- which no longer repeats the word,
    // because the preview said it first.
    expect(zh.find(".history-content p").text()).toBe("图片");
    expect(zh.find(".history-content .note").exists()).toBe(false);
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

describe("first-run onboarding", () => {
  const showModal = () => vi.mocked(HTMLDialogElement.prototype.showModal);
  const closeDialog = () => vi.mocked(HTMLDialogElement.prototype.close);

  it("previews language and theme, walks to the start page, then stores on 完成", async () => {
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
      const dialog = app.get('[aria-labelledby="onboarding-title"]');
      // Bilingual on purpose: the names of all three choices are visible in
      // their own scripts before the reader can read any one of them.
      expect(dialog.text()).toContain("简体中文");
      expect(dialog.text()).toContain("繁體中文");
      expect(dialog.text()).toContain("Simplified Chinese");
      expect(dialog.text()).toContain("暖色");
      const next = dialog.get(".onboarding-next");
      expect(next.attributes("disabled")).toBeDefined();

      await dialog.get('[data-locale="en"]').trigger("click");
      await flushPromises();
      // A pick previews the shell but does not save or close the dialog.
      expect(bridge.updateSettings).not.toHaveBeenCalled();
      expect(closeDialog()).not.toHaveBeenCalled();

      await dialog.get('[data-theme-choice="sepia"]').trigger("click");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("sepia");

      expect(next.attributes("disabled")).toBeUndefined();
      await next.trigger("click");
      await flushPromises();
      // The tutorial is the second page, still before anything is stored.
      expect(dialog.findAll(".onboarding-steps li")).toHaveLength(3);
      expect(bridge.updateSettings).not.toHaveBeenCalled();

      await dialog.get(".onboarding-finish").trigger("click");
      await flushPromises();
      // One explicit action writes both choices and retires the prompt.
      expect(bridge.updateSettings).toHaveBeenCalledExactlyOnceWith({
        language: "en", appearance_mode: "sepia",
      });
      expect(closeDialog()).toHaveBeenCalled();
      expect(app.find('[aria-label="Settings"]').exists()).toBe(true);
    } finally {
      app.unmount();
      setLocale("zh-CN");
      delete document.documentElement.dataset.theme;
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
      await button(app, "连接设备").trigger("click");
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

  it("does not call the settings form unsaved for a pause armed from the overview", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    const until = Math.floor(Date.now() / 1000) + 30 * 60;
    vi.mocked(bridge.status).mockResolvedValue(status("running"));
    // One payload for every read, so the only thing that can move between two
    // visits to the page is what the case itself moves — the pause.
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "Local" } } as any);
    vi.mocked(bridge.pauseSync).mockResolvedValue({ enabled: false, until } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // A save first, so the bar has a settled word to fall back to: an
      // untouched page says nothing at all rather than "everything is saved".
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(app.text()).toContain("所有更改都已保存");
      await app.get('[aria-label="概览"]').trigger("click");
      await flushPromises();
      await app.get(".overview-pause").findAll("button")
        .find(button => button.text() === "30 分钟")!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // The deadline is not a setting this page edits, so a pause is not a
      // change the save bar is holding: it armed, and the form is as it was.
      expect(app.text()).not.toContain("有未保存的更改");
      expect(app.text()).toContain("所有更改都已保存");
      expect(app.text()).toContain("已暂停 · 剩余 30 分钟");
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("sends files to the device a row named, instead of asking which device again", async () => {
    // The transfers page does not preselect on its own — a file cannot be taken
    // back, so the machine is named.  This is the case where the reader names it
    // first, in the row's menu; landing on the page only to be asked again is
    // answering a question they just answered.  The entry is in the menu, not on
    // the row: that line was already ten icons wide.  See U139.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Studio", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openDeviceRowMenu(app);
      await clickMenuItem(app, "发送文件…");
      await flushPromises();

      const picker = app.get('[aria-label="发送目标"]').element as HTMLSelectElement;
      expect(picker.value).toBe("t");
      expect(app.text()).toContain("Studio");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.transfers).mockReset();
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

  /** Open the chat page after the drop listener has been captured. */
  async function openChat(app: VueWrapper) {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "6", ctrlKey: true }));
    await flushPromises();
  }

  it("sends files dropped on a conversation to it, and stays on the chat page", async () => {
    // The reported bug: dropping a file onto a conversation moved the reader to
    // the transfers page, though the chat page can send an attachment to the
    // conversation they are in.  The drop belongs to the open session now.
    vi.clearAllMocks();
    setLocale("zh-CN");
    vi.mocked(bridge.sendChatFile).mockResolvedValue({ ok: true, transfer_id: "chat-1" });
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [{
      session_id: "s1", peer_id: "p1", peer_name: "Pixel", status: "active",
      unread: 0, online: true, created_ts: 1, last_activity_ts: 1,
      last_preview: "", fingerprint_short: "abcd",
    }], muted: [] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [] } as any);
    const { app, drop } = await mountWithDrop();
    try {
      await openChat(app);
      await app.get('[data-session-id="s1"] .chat-session__open').trigger("click");
      await flushPromises();

      drop({ type: "drop", paths: ["C:/a.txt", "C:/b.txt"], position: { x: 1, y: 1 } });
      await flushPromises();

      // One call per file, in the order they were dropped, to the open session.
      expect(bridge.sendChatFile).toHaveBeenNthCalledWith(1, "s1", "C:/a.txt");
      expect(bridge.sendChatFile).toHaveBeenNthCalledWith(2, "s1", "C:/b.txt");
      // The reader stays where they were, and nothing was staged for a device
      // picker they never asked for.
      expect(app.find(".chat-view").exists()).toBe(true);
      expect(app.find(".transfers-view").exists()).toBe(false);
      expect(bridge.sendFiles).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [], muted: [] });
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    }
  });

  it("hands a drop to the transfers page when the chat page has no conversation to send it to", async () => {
    // The fallback: nothing is lost when there is no live conversation.  The
    // staged paths go to the transfers page, which is where a drop went before
    // the chat page could take one.
    vi.clearAllMocks();
    setLocale("zh-CN");
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [], muted: [] } as any);
    vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    const { app, drop } = await mountWithDrop();
    try {
      await openChat(app);
      expect(app.find(".chat-view").exists()).toBe(true);

      drop({ type: "drop", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();

      expect(bridge.sendChatFile).not.toHaveBeenCalled();
      expect(app.find(".transfers-view").exists()).toBe(true);
      expect(app.get(".staged-drop").text()).toContain("a.txt");
    } finally {
      app.unmount();
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    }
  });

  it("reports a refused chat attachment instead of dropping the file silently", async () => {
    // A send that fails has to say so on the page: nothing else changes, so a
    // silent failure is a file the reader believes was sent.
    vi.clearAllMocks();
    setLocale("zh-CN");
    vi.mocked(bridge.sendChatFile).mockRejectedValue({ code: "NOT_CONNECTED", message: "offline" });
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [{
      session_id: "s1", peer_id: "p1", peer_name: "Pixel", status: "active",
      unread: 0, online: true, created_ts: 1, last_activity_ts: 1,
      last_preview: "", fingerprint_short: "abcd",
    }], muted: [] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [] } as any);
    const { app, drop } = await mountWithDrop();
    try {
      await openChat(app);
      await app.get('[data-session-id="s1"] .chat-session__open').trigger("click");
      await flushPromises();
      drop({ type: "drop", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();
      expect(app.get(".chat-view").text()).toContain("a.txt");
      expect(app.get(".chat-view").text()).toContain("发送失败");
      expect(app.find(".transfers-view").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [], muted: [] });
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
    }
  });

  it("names the size limit when an attachment is refused for being too large", async () => {
    // The one attachment refusal a reader can act on.  It reached the window as
    // "LAN operation failed" — the sidecar's sentence for every failure — so the
    // page reported a size problem as a network one.  See U146.
    vi.clearAllMocks();
    setLocale("zh-CN");
    vi.mocked(bridge.sendChatFile).mockRejectedValue({
      code: "CHAT_FILE_TOO_LARGE", message: "File exceeds the 2 GiB chat file limit",
    });
    vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [{
      session_id: "s1", peer_id: "p1", peer_name: "Pixel", status: "active",
      unread: 0, online: true, created_ts: 1, last_activity_ts: 1,
      last_preview: "", fingerprint_short: "abcd",
    }], muted: [] } as any);
    vi.mocked(bridge.chatMessages).mockResolvedValue({ messages: [] } as any);
    const { app, drop } = await mountWithDrop();
    try {
      await openChat(app);
      await app.get('[data-session-id="s1"] .chat-session__open').trigger("click");
      await flushPromises();
      drop({ type: "drop", paths: ["C:/big.iso"], position: { x: 1, y: 1 } });
      await flushPromises();
      // The file's name, the limit, and what to do instead — not the sidecar's
      // own English sentence.
      expect(app.get(".chat-view").text()).toContain("big.iso");
      expect(app.get(".chat-view").text()).toContain("文件超过 2 GB");
      expect(app.get(".chat-view").text()).not.toContain("GiB chat file limit");
    } finally {
      app.unmount();
      vi.mocked(bridge.chatSessions).mockResolvedValue({ sessions: [], muted: [] });
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [], speed_test: {} } as any);
      vi.mocked(bridge.sendChatFile).mockReset();
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
  afterEach(() => {
    app.unmount();
    closeContextMenu();
    vi.mocked(bridge.hideMainAndPaste).mockReset().mockResolvedValue({ ok: true });
    vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
      preview: '<img src=x onerror="window.injected=true">',
    })]));
  });

  it("copies the row it was opened on, not the first one", async () => {
    vi.mocked(bridge.copyHistory).mockClear();
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    await app.findAll(".context-menu-item")[0].trigger("click");
    await flushPromises();
    expect(bridge.copyHistory).toHaveBeenLastCalledWith("a");
  });

  it("offers the text form of a formatted clip, and only of a formatted one", async () => {
    // `content_type` is the row's single best label, so HTML/RTF is exactly
    // the case where "copy" and "copy the words" differ: pasting the first into
    // a terminal arrives as markup.  A text row would get two entries for one
    // call, and an image has no text form at all.
    app.unmount();
    closeContextMenu();
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "rich", preview: "hello", content_type: "HTML" }),
      historyRow({ id: "plain", preview: "hello", content_type: "TEXT" }),
    ]));
    app = mountOn("history");
    await flushPromises();

    const labels = () => app.findAll(".context-menu-item").map(node => node.text());
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    expect(labels()).toContain(t("复制为纯文本"));
    await app.findAll(".context-menu-item").find(node => node.text() === t("复制为纯文本"))!
      .trigger("click");
    await flushPromises();
    expect(bridge.copyHistory).toHaveBeenLastCalledWith("rich", true);

    closeContextMenu();
    await app.findAll(".history-row")[1].trigger("contextmenu");
    await flushPromises();
    expect(labels()).not.toContain(t("复制为纯文本"));
  });

  it("copies the row and then hands the OS the paste, from one entry", async () => {
    // The menu's 「粘贴到当前应用」 is 复制 plus the system chord: the copy has to
    // land before the paste is asked for, or the window behind would receive
    // whatever the clipboard held before.
    vi.mocked(bridge.copyHistory).mockClear();
    vi.mocked(bridge.hideMainAndPaste).mockClear();
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    // The entries' labels are their own span: the text of the button also
    // carries whatever shortcut is drawn at its right edge.
    const items = () => app.findAll(".context-menu-item");
    const labelOf = (node: DOMWrapper<Element>) =>
      node.get(".context-menu-label").text();
    const labels = items().map(labelOf);
    // Right after 复制, which is the action it extends.
    expect(labels.indexOf(t("粘贴到当前应用"))).toBe(labels.indexOf(t("复制")) + 1);
    await items().find(node => labelOf(node) === t("粘贴到当前应用"))!.trigger("click");
    await flushPromises();
    expect(bridge.copyHistory).toHaveBeenLastCalledWith("a");
    expect(bridge.hideMainAndPaste).toHaveBeenCalledOnce();
  });

  it("says which key to press when the paste half is refused", async () => {
    vi.mocked(bridge.copyHistory).mockClear();
    vi.mocked(bridge.hideMainAndPaste).mockRejectedValue({
      code: "PASTE_FAILED", message: "the OS refused the injection",
    });
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    await app.findAll(".context-menu-item")
      .find(node => node.get(".context-menu-label").text() === t("粘贴到当前应用"))!
      .trigger("click");
    await flushPromises();
    // The clipboard holds the clip either way — that is the half the sentence
    // reports, and the key it names is the system's own, not a configured one.
    expect(bridge.copyHistory).toHaveBeenCalledOnce();
    expect(app.text()).toContain(t("已复制，请手动按 {shortcut} 粘贴", { shortcut: "Ctrl+V" }));
  });

  it("leaves the paste entry out for a file that lives on another device", async () => {
    app.unmount();
    closeContextMenu();
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({
        id: "remote-file", preview: "report.pdf", content_type: "FILE_REMOTE",
        source_device: "peer-1", offer_entry: "7",
      }),
    ]));
    app = mountOn("history");
    await flushPromises();
    await app.findAll(".history-row")[0].trigger("contextmenu");
    await flushPromises();
    // There is nothing on this machine to put on the clipboard yet, so the
    // entry would only ever paste the previous clipboard's contents.
    const labels = app.findAll(".context-menu-item").map(node => node.text());
    expect(labels).not.toContain(t("粘贴到当前应用"));
  });

});


describe("the history toolbar's quick-paste entry", () => {
  it("opens the overlay from the page that holds the rows", async () => {
    setLocale("zh-CN");
    vi.mocked(bridge.openQuickPaste).mockClear();
    const app = mountOn("history");
    try {
      await flushPromises();
      const entry = app.findAll("button")
        .find(node => node.attributes("aria-label") === t("快速粘贴"))!;
      expect(entry.exists(), "no quick-paste entry on the history toolbar").toBe(true);
      await entry.trigger("click");
      await flushPromises();
      // One command: the window, the focus bookkeeping and the paste into the
      // application behind it are all the host's, so the page opens nothing
      // itself and has nothing to say about it afterwards.
      expect(bridge.openQuickPaste).toHaveBeenCalledOnce();
    } finally {
      app.unmount();
      vi.mocked(bridge.openQuickPaste).mockClear();
    }
  });
});

describe("the update button on a device row", () => {
  /** The two devices the update cases need: one ahead of us, one behind. */
  async function devicesPage(app: VueWrapper) {
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await flushPromises();
  }

  it("says the request is on its way before the peer answers", async () => {
    // The ask is answered by the other machine, so the row must not wait for it to say anything.
    // A `deferred` rather than a resolved mock: with a resolved one the note is replaced in the
    // same tick and the interim message is never observable, which is how this went unnoticed.
    let release: (value: { sent: boolean }) => void = () => {};
    const pending = new Promise<{ sent: boolean }>((resolve) => {
      release = resolve;
    });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "n", name: "Newer", paired: true, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        update_available: false, update_fetchable: true },
    ] });
    vi.mocked(bridge.fetchDeviceUpdate).mockReturnValue(pending);

    const app = mount(App);
    try {
      await devicesPage(app);
      const fetch = button(app, "获取更新");
      await fetch.trigger("click");
      await flushPromises();

      // Before the peer has answered.  `flushPromises` drains the microtasks that are ready; the
      // deferred promise is deliberately not among them.
      expect(app.text()).toContain("正在向 Newer 索取安装包");
      expect(app.text()).not.toContain("已向 Newer 索取安装包");

      release({ sent: true });
      await flushPromises();
      // And the real answer replaces it rather than sitting beside it.
      expect(app.text()).toContain("已向 Newer 索取安装包");
      expect(app.text()).not.toContain("正在向 Newer 索取安装包");
    } finally {
      app.unmount();
    }
  });

  it("is not disabled by a background refresh", async () => {
    // `busy` is `state.pending || state.refreshing || !ready`, and a refresh runs on every sidecar
    // event -- so binding this button to it made the whole row flicker while anything was
    // happening.  What the button must respect is its own request and whether the device answers.
    //
    // The store is held *busy* for the assertion, not merely left alone: with `busy` false the old
    // binding would pass this test, and the point is that a control must not change state because
    // something unrelated is happening.  `state.pending` is one of the three things `busy` is made
    // of, and any store action sets it -- `pushText` is used because it needs nothing from the
    // device list and holds the flag open until its command answers.
    let releaseBusy: (value: { ok: boolean; len: number; sent: boolean }) => void = () => {};
    vi.mocked(bridge.pushText).mockReturnValue(
      new Promise<{ ok: boolean; len: number; sent: boolean }>((resolve) => {
        releaseBusy = resolve;
      }),
    );
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "n", name: "Newer", paired: true, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        update_available: false, update_fetchable: true },
      { id: "t", name: "Older", paired: true, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        update_available: true, update_cached: true },
    ] });

    const app = mount(App);
    try {
      await devicesPage(app);
      const fetch = button(app, "获取更新");
      const send = button(app, "发送更新");
      expect(fetch.attributes("disabled")).toBeUndefined();
      expect(send.attributes("disabled")).toBeUndefined();

      // Something else entirely is in flight, which is what `busy` reports.
      await app.get('[aria-label="推送文本"]').trigger("click");
      await flushPromises();
      const box = app.get('[aria-label="要推送的文本"]');
      await box.setValue("x");
      // Found and asserted rather than optional-chained: a lookup that quietly found nothing
      // would leave the store un-busy and the assertions below would pass for the wrong reason,
      // which is worse than failing.
      const confirm = app.findAll("button").find((b) => b.text() === t("推送"));
      expect(confirm, "the push-text confirm button was not found").toBeDefined();
      await confirm!.trigger("click");
      await flushPromises();

      // Nothing about either button changed, which is the whole claim: a control must not change
      // state because something unrelated is happening.
      expect(fetch.attributes("disabled")).toBeUndefined();
      expect(send.attributes("disabled")).toBeUndefined();

      releaseBusy({ ok: true, len: 1, sent: false });
      await flushPromises();
    } finally {
      app.unmount();
    }
  });

  it("says which way the two builds differ in a sentence, and offers one button", async () => {
    // The row carried two icons for this -- an up arrow and a down arrow, side
    // by side, on the same row -- and which one applied to whom was the arrow's
    // job to say.  It said nothing: both travel an installer over the same link
    // in the same shape of blob.  The direction is in the words now, and the row
    // offers the one action that direction has.  See U111.
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "t", name: "Older", paired: true, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.0.7", platform: "windows", arch: "amd64",
        update_available: true, update_cached: true },
      { id: "n", name: "Newer", paired: true, connection_state: "online",
        pairing_status: "", pairing_code: null, sas: null,
        version: "1.1.0", platform: "windows", arch: "amd64",
        update_available: false, update_fetchable: true },
    ] });

    const app = mount(App);
    try {
      await devicesPage(app);
      const rows = app.findAll(".device-row");
      const older = rows.find(row => row.text().includes("Older"))!;
      const newer = rows.find(row => row.text().includes("Newer"))!;

      expect(older.get(".device-version-note").text())
        .toContain("版本不一致：该设备是 1.0.7，比本机旧");
      expect(newer.get(".device-version-note").text())
        .toContain("版本不一致：该设备是 1.1.0，比本机新");
      // One line, one entry on it: the other direction is not drawn beside it.
      expect(older.findAll(".device-version-note button")).toHaveLength(1);
      expect(older.get(".device-version-note button").text()).toContain("发送更新");
      expect(newer.get(".device-version-note button").text()).toContain("获取更新");
      expect(newer.findAll(".device-version-note button")).toHaveLength(1);

      // And the row's own two verbs are words rather than two more grey
      // squares: the link and the conversation are what a device row is for.
      const actions = older.get(".row-actions");
      expect(actions.text()).toContain("断开连接");
      expect(actions.text()).toContain("聊天");
      expect(actions.findAll(".icon-button")).not.toHaveLength(0);
      // The removal is drawn apart from them -- same grey, same size and the
      // same distance from its neighbour is how a device gets archived while
      // its neighbour is being updated.  See U112.
      expect(actions.find('[aria-label="全部：移除设备"]').classes()).toContain("icon-button--danger");
    } finally {
      app.unmount();
    }
  });
});


describe("the failure band", () => {
  /** Open the factory-reset dialog and confirm it, letting the command fail as told. */
  async function resetThatFails(app: VueWrapper) {
    await flushPromises();
    await app.get('[aria-label="设置"]').trigger("click");
    await flushPromises();
    await app.findAll("button").find(button => button.text().includes("恢复出厂设置"))!.trigger("click");
    await flushPromises();
    const dialog = app.get('[aria-labelledby="factory-reset-title"]');
    // The reset needs its word typed first; these cases are about what the
    // failure band does afterwards, so the word is answered here.
    await dialog.get('input[type="text"]').setValue("删除");
    await dialog.findAll("button").find(button => button.text() === "确认重置")!.trigger("click");
    await flushPromises();
  }

  it("offers a way out for an error that cannot be retried", async () => {
    // Reported as "VALIDATION_ERROR 这样的提示关不掉".  The band renders two buttons and both were
    // conditional -- the repair, for a data directory the sidecar refuses, and the retry, for a code
    // in `SIDECAR_DOWN_CODES` or `USER_FIXABLE_CODES` -- so an error in neither had no control at
    // all: it stayed until some later store action cleared it as a side effect of starting.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.factoryReset).mockRejectedValue({
      code: "VALIDATION_ERROR",
      message: t("请求的格式不符合预期，本次操作没有执行。"),
      retryable: false,
    });

    const app = mount(App);
    try {
      await resetThatFails(app);

      const band = app.get(".error-band");
      // The message reaches the reader first, which is what the band is for.
      expect(band.text()).toContain(t("请求的格式不符合预期，本次操作没有执行。"));
      expect(band.text()).toContain("VALIDATION_ERROR");

      // And there is something to press.  Found by label rather than by position: the band also
      // carries the repair and retry buttons, each behind its own condition.
      const dismiss = band.find('[aria-label="关闭提示"]');
      expect(dismiss.exists(), "no dismiss control on a non-retryable error").toBe(true);

      await dismiss.trigger("click");
      await flushPromises();
      expect(app.find(".error-band").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.factoryReset).mockReset().mockResolvedValue(null);
    }
  });

  it("says where to look for the application holding the data folder", async () => {
    // The sidecar's answer for this failure names the cause and nothing else,
    // and the window cannot enumerate processes — so the band's question opens
    // the steps instead of pretending to answer it.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.factoryReset).mockRejectedValue({
      code: "DATA_IN_USE",
      message: "Close the legacy ClipSync application first",
      retryable: false,
    });

    const app = mount(App);
    try {
      await resetThatFails(app);
      const why = app.get(".error-band").find(".error-band__why");
      expect(why.text()).toBe("谁占用了？");
      await why.trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="data-in-use-title"]');
      expect(dialog.text()).toContain("任务管理器");
      // Read-only: the one thing offered is a retry, because ending a process
      // on the reader's behalf is how a half-written database is lost.
      expect(dialog.findAll("button").map(button => button.text()).filter(Boolean)).toEqual(["关闭", "重新连接"]);
    } finally {
      app.unmount();
      vi.mocked(bridge.factoryReset).mockReset().mockResolvedValue(null);
    }
  });

  it("keeps the retry control, and offers no second way out beside it", async () => {    // The control: a retryable error is answered by 重新连接, and a dismiss button beside it would
    // be a second thing to explain for the same situation.  `setError` forces these codes retryable
    // even though the host reports them as not.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.factoryReset).mockRejectedValue({
      code: "SIDECAR_UNAVAILABLE",
      message: t("后台进程不可用，请重试"),
      retryable: false,
    });

    const app = mount(App);
    try {
      await resetThatFails(app);

      const band = app.get(".error-band");
      expect(band.find('[aria-label="重新连接"]').exists()).toBe(true);
      expect(band.find('[aria-label="关闭提示"]').exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.factoryReset).mockReset().mockResolvedValue(null);
    }
  });
});

// Reported as "设备页'附近聊天'按钮有时卡住".  The invite used to be the one row
// action with no busy state, and the sidecar used to hold its own answer until
// the dial it starts had finished — so the button looked dead for as long as
// that took, and a second click sent a second invite.  The sidecar now answers at
// once (the dial is its own thread's); what these hold is the window's half.
describe("inviting a device to chat", () => {
  /** One device that answers, so the row offers 打开聊天 at all. */
  function onlineDevice() {
    return {
      id: "n", name: "Studio", paired: true, connection_state: "online",
      pairing_status: "", pairing_code: null, sas: null,
    };
  }

  async function devicesPage(app: VueWrapper) {
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await flushPromises();
  }

  it("says it is connecting at once, and sends no second invite while it waits", async () => {
    // A deferred answer rather than a resolved one: with a resolved mock the
    // interim message is replaced in the same tick and is never observable,
    // which is exactly how "the button looks dead" went unnoticed.
    let release: (value: { chat_session_id: string; connecting: boolean }) => void = () => {};
    const pending = new Promise<{ chat_session_id: string; connecting: boolean }>((resolve) => {
      release = resolve;
    });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [onlineDevice()] });
    vi.mocked(bridge.inviteChat).mockReturnValue(pending);

    const app = mount(App);
    try {
      await devicesPage(app);
      const chat = button(app, "聊天");
      await chat.trigger("click");
      await flushPromises();

      // Before the sidecar has answered.
      expect(app.text()).toContain(t("正在连接 {name}…", { name: "Studio" }));
      // The row's chat button is closed while it waits...
      expect(chat.attributes("disabled")).toBeDefined();
      // ...and a click that lands anyway is not a second invite.
      await chat.trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.inviteChat)).toHaveBeenCalledTimes(1);

      release({ chat_session_id: "", connecting: true });
      await flushPromises();
      // And the row is released rather than left closed.  Read on the devices
      // page, which `chatWith` navigated away from: the element captured above
      // is detached by then, and a detached element keeps the last value it was
      // drawn with.
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      expect(button(app, "聊天").attributes("disabled")).toBeUndefined();
    } finally {
      app.unmount();
      vi.mocked(bridge.inviteChat).mockReset().mockResolvedValue({
        chat_session_id: null, connecting: true,
      });
    }
  });

  it("stops waiting after the timeout instead of leaving the row stuck", async () => {
    // The one thing the window can fix on its own: an engine that never answers
    // must not cost the reader the button.  `shouldAdvanceTime` keeps the rest of
    // the window's own timers working while the 8s one is fast-forwarded.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(bridge.devices).mockResolvedValue({ items: [onlineDevice()] });
    vi.mocked(bridge.inviteChat).mockReturnValue(new Promise(() => {}));

    const app = mount(App);
    try {
      await devicesPage(app);
      const chat = button(app, "聊天");
      await chat.trigger("click");
      await flushPromises();
      expect(app.text()).toContain(t("正在连接 {name}…", { name: "Studio" }));

      await vi.advanceTimersByTimeAsync(8_001);
      await flushPromises();

      expect(app.text()).toContain(t("连接超时，请重试"));
      expect(chat.attributes("disabled")).toBeUndefined();
    } finally {
      app.unmount();
      vi.useRealTimers();
      vi.mocked(bridge.inviteChat).mockReset().mockResolvedValue({
        chat_session_id: null, connecting: true,
      });
    }
  });
});

// 重新检测 counts what the scan decided.  A check still coming up (`pending`) is
// neither a pass nor a failure — `summarize` leaves it out of the verdict — so
// the count has to leave it out too, or the line contradicts the verdict printed
// directly above it.
describe("the diagnostics re-check count", () => {
  function report() {
    return {
      v2: true,
      summary: "ok",
      checks: [
        { id: "server_port", ok: true, detail: "listening" },
        { id: "discovery", ok: true, detail: "active" },
        { id: "advertising", ok: false, pending: true, detail: "announcing" },
        { id: "network", ok: true, detail: "private LAN" },
      ],
      groups: {},
      discovery_running: true, server_running: true, connected_count: 0, paired_count: 0,
      web_companion_running: false, web_port: 0, lan_ip: "10.0.0.2", os: "Windows",
      version: "test",
    };
  }

  it("counts the checks that settled, not the one still coming up", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({
      version: "test", health: "ready", device_name: "Local", session_id: "s", seq: 0,
      sync_state: "not_started", capabilities: ["diagnostics.report"],
    } as any);
    vi.mocked(bridge.diagnosticsReport).mockResolvedValue(report() as any);

    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const run = app.findAll("button").find((b) => b.text().includes(t("运行诊断")));
      expect(run, "the run-diagnostics button was not found").toBeDefined();
      await run!.trigger("click");
      await flushPromises();
      const rerun = app.findAll("button").find((b) => b.text().includes(t("重新检测")));
      expect(rerun, "the re-check button was not found").toBeDefined();
      await rerun!.trigger("click");
      await flushPromises();

      // Three settled checks passed and the fourth has not been decided: 3/3,
      // not 3/4 — which is what the verdict above it says too.
      expect(app.text()).toContain(t("已重新检测 · {ok}/{total} 项通过", { ok: 3, total: 3 }));
      // What went wrong, as text: a bug report used to be a screenshot of this
      // dialog with the version numbers retyped by hand.
      vi.mocked(bridge.copyText).mockClear().mockResolvedValue({ copied: true });
      await app.findAll("button").find((b) => b.text().includes(t("复制诊断结果")))!.trigger("click");
      await flushPromises();
      const copied = vi.mocked(bridge.copyText).mock.calls[0][0];
      expect(copied).toContain(t("ClipSync 诊断报告"));
      expect(copied).toContain("test");
      expect(copied).toContain(t("总体状态"));
      expect(app.text()).toContain(t("诊断结果已复制，可以直接粘贴到反馈里。"));
    } finally {
      app.unmount();
    }
  });

  /** A card whose whole subject is "find out what is wrong" is the last place
   *  a dead button can be left to speak for itself. */
  it("says why the button is dead when the sidecar cannot answer it", async () => {
    vi.mocked(bridge.status).mockResolvedValue({
      version: "test", health: "ready", device_name: "Local", session_id: "s", seq: 0,
      sync_state: "not_started", capabilities: [],
    } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const run = app.findAll("button").find((b) => b.text().includes(t("运行诊断")))!;
      expect(run.attributes("disabled")).toBeDefined();
      expect(run.attributes("title")).toBe(t("当前版本的服务端不支持诊断。"));
      expect(app.text()).toContain(t("当前版本的服务端不支持诊断。"));
    } finally {
      app.unmount();
    }
  });
});

/** The wiring the pairing audit's 严重-7/8, 中-27/28/29, 轻-3/4 fixed. */
describe("pairing and dialing wiring", () => {
  /** The window's event listener, as the host would call it. */
  async function mountWithEvents() {
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback;
      return () => {};
    });
    const app = mount(App);
    await flushPromises();
    return { app, emit: (event: any) => emit(event) };
  }

  afterEach(() => {
    setLocale("zh-CN");
    vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    vi.mocked(bridge.scanDevices).mockResolvedValue({ enabled: true, visible: true });
    vi.mocked(bridge.connectDevice).mockResolvedValue({ accepted: true });
    vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
      preview: '<img src=x onerror="window.injected=true">',
    })]));
    closeContextMenu();
  });

  it("shows the non-destructive identity notice, and no trust prompt, when the certificate is the pinned one", async () => {
    // 严重-8.  The alert used to be the certificate-change dialog whatever the
    // certificate was, so a host that merely copied a paired device's public
    // PEM could make a healthy device look "changed" — and the dialog's
    // "keep unpaired" answer really unpaired it.  `changed: false` is the same
    // packet with nothing to re-pin, and must not offer either answer.
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const { app, emit } = await mountWithEvents();
    try {
      emit({ type: "event", session_id: "s", name: "device.security_alert",
        data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED",
          can_trust: false, changed: false } });
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
      expect(app.text()).toContain(t(
        "无法验证 {name} 的身份，本次连接已拒绝；它的证书与已保存的一致。", { name: "Pixel" }));
      // The trust dialog is in the DOM whether or not it is open (it is one of
      // the window's permanent dialogs), so the evidence that it was not
      // *offered* is that `showModal` was never called — asserted above — and
      // that the notice carries the non-destructive sentence instead.
      expect(app.findAll(".notice").map(node => node.text())).toContainEqual(
        expect.stringContaining(t("设备身份未能验证")),
      );
    } finally {
      app.unmount();
    }
  });

  it("still asks about a certificate that really changed", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const { app, emit } = await mountWithEvents();
    try {
      emit({ type: "event", session_id: "s", name: "device.security_alert",
        data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED",
          can_trust: true, changed: true } });
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
      const dialog = app.get('[aria-labelledby="cert-alert-title"]');
      expect(dialog.text()).toContain(t("这是您信任的设备吗？"));
      expect(dialog.findAll(".modal-actions button")).toHaveLength(2);
    } finally {
      app.unmount();
    }
  });

  it("treats an alert with no `changed` field as the certificate change it always was", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const { app, emit } = await mountWithEvents();
    try {
      // A sidecar older than the field: its alert only ever meant the
      // destructive case, and reading the absent key as "unproven" would take
      // the trust prompt away from a reader who needs it.
      emit({ type: "event", session_id: "s", name: "device.security_alert",
        data: { device_id: "peer", name: "Pixel", code: "CERTIFICATE_CHANGED", can_trust: true } });
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
    } finally {
      app.unmount();
    }
  });

  it("says why a dial failed instead of letting the row go quiet", async () => {
    // 中-27.  A dial outlives the request that started it, so a failure is a
    // fact that arrives seconds later with nothing on screen to carry it.
    setLocale("zh-CN");
    vi.clearAllMocks();
    const { app, emit } = await mountWithEvents();
    try {
      emit({ type: "event", session_id: "s", name: "device.connect_failed",
        data: { device_id: "peer", name: "Pixel", reason: "timeout" } });
      await flushPromises();
      expect(app.text()).toContain(t("连接 {name} 超时，设备可能不在线", { name: "Pixel" }));
      // An unknown code from a newer transport still says a true sentence.
      emit({ type: "event", session_id: "s", name: "device.connect_failed",
        data: { device_id: "peer", name: "Pixel", reason: "something-new" } });
      await flushPromises();
      expect(app.text()).toContain(t("连接 {name} 失败", { name: "Pixel" }));
    } finally {
      app.unmount();
    }
  });

  it("holds the connect button while the dial is in flight, and starts only one", async () => {
    // 中-28.  The connect RPC answers when the dial *starts*, so `busy` was
    // false again immediately and every click started another dial.
    setLocale("zh-CN");
    vi.clearAllMocks();
    const offline = { id: "peer", name: "Pixel", paired: true, connection_state: "offline",
      dialable: true, pairing_status: "paired", pairing_code: null, sas: null };
    const dialing = { ...offline, connection_state: "connecting" };
    vi.mocked(bridge.devices)
      .mockResolvedValueOnce({ items: [offline] } as any)
      .mockResolvedValue({ items: [dialing] } as any);
    const app = mountOn("devices");
    try {
      await flushPromises();
      const connect = button(app, "连接设备");
      await connect.trigger("click");
      await flushPromises();
      expect(bridge.connectDevice).toHaveBeenCalledTimes(1);
      expect(button(app, "连接设备").attributes("disabled")).toBeDefined();
      // A second click inside the dial's window is refused, not another dial.
      await button(app, "连接设备").trigger("click");
      await flushPromises();
      expect(bridge.connectDevice).toHaveBeenCalledTimes(1);
    } finally {
      app.unmount();
    }
  });

  it("keeps the outcome of a pairing request on the row instead of letting the card vanish", async () => {
    // 中-29.  The ended request used to remove the confirm card and say
    // nothing: expired and rejected looked the same, and neither was named.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [{
      id: "peer", name: "Pixel", paired: false, connection_state: "online",
      pairing_status: "", pairing_code: null, sas: null,
    }] as any });
    const { app, emit } = await mountWithEvents();
    try {
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "pairing.resolved",
        data: { device_id: "peer", status: "expired" } });
      await flushPromises();
      expect(app.text()).toContain(t("{name} 的配对请求已过期，请重新配对", { name: "Pixel" }));
      expect(app.get(".device-row .device-channels").text()).toContain(t("已过期"));
    } finally {
      app.unmount();
    }
  });

  it("offers 移除设备 in the context menu on a row that is not paired", async () => {
    // 轻-3.  The row's own trash was drawn for an unpaired device, the menu was
    // not — one action with two different rules depending on how it is reached.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [{
      id: "seen", name: "Just seen", paired: false, connection_state: "discovered",
      dialable: false, pairing_status: "", pairing_code: null, sas: null,
    }] as any });
    const app = mountOn("devices");
    try {
      await flushPromises();
      await app.get(".device-row").trigger("contextmenu");
      await flushPromises();
      const labels = app.findAll(".context-menu-item").map(item => item.text());
      expect(labels).toContain(t("移除设备"));
    } finally {
      app.unmount();
    }
  });

  it("disables refresh while the scan runs, and says a switched-off scan scanned nothing", async () => {
    // 轻-4.  The scan is a blocking round on the sidecar (~1.5 s), so the first
    // click's round is what the button is; a second used to queue another.  And
    // the answer carries the discovery switches: browsing off is an empty round.
    setLocale("zh-CN");
    vi.clearAllMocks();
    let release: (value: { enabled: boolean; visible: boolean }) => void = () => {};
    const pending = new Promise<{ enabled: boolean; visible: boolean }>((resolve) => {
      release = resolve;
    });
    vi.mocked(bridge.scanDevices).mockReturnValue(pending as any);
    vi.mocked(bridge.devices).mockResolvedValue({ items: [{
      id: "a", name: "Alpha", paired: true, connection_state: "online",
      pairing_status: "paired", pairing_code: null, sas: null,
    }] as any });
    const app = mountOn("devices");
    try {
      await flushPromises();
      const refresh = app.get('[aria-label="刷新设备"]');
      await refresh.trigger("click");
      await flushPromises();
      expect(app.get('[aria-label="刷新设备"]').attributes("disabled")).toBeDefined();
      await app.get('[aria-label="刷新设备"]').trigger("click");
      await flushPromises();
      expect(bridge.scanDevices).toHaveBeenCalledTimes(1);
      release({ enabled: false, visible: true });
      await flushPromises();
      expect(bridge.scanDevices).toHaveBeenCalledTimes(1);
      expect(app.text()).toContain(t(
        "局域网发现已关闭，未扫描网络（已重新读取 {count} 台设备）", { count: 1 }));
    } finally {
      app.unmount();
    }
  });

  it("renames a merely-seen device through the note path, not the alias path", async () => {
    // 中-31.  A row that is only seen can be renamed from its menu; the write has
    // to be the note (the backend accepts it for a known sighting) and not the
    // internet alias, which would answer NOT_FOUND for a device with no internet
    // pairing.
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.devices).mockResolvedValue({ items: [{
      id: "seen", name: "Just seen", paired: false, connection_state: "discovered",
      relay_paired: false, dialable: false, pairing_status: "", pairing_code: null, sas: null,
    }] as any });
    const { app } = await mountWithEvents();
    try {
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await app.get(".device-row").trigger("contextmenu");
      await flushPromises();
      const rename = app.findAll(".context-menu-item").find(item => item.text() === t("重命名"));
      expect(rename, "the rename entry was not offered on a discovered row").toBeDefined();
      await rename!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="设备名称"]').setValue("Desk");
      await app.findAll("dialog button").find(button => button.text() === t("保存"))!.trigger("click");
      await flushPromises();
      expect(bridge.setDeviceNote).toHaveBeenCalledWith("seen", "Desk");
      expect(bridge.renameInternetPeer).not.toHaveBeenCalled();
    } finally {
      app.unmount();
    }
  });

  it("shows a generated code's remaining life, and clears it when the code expires", async () => {
    // The card can now say how long a code can still be typed, and says so when
    // it is over instead of letting the code vanish on the next poll.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue({
      enabled: true, peers: [], generated_code: "ABCD-EFGH-IJKL", pending_ttl: 90,
    } as any);
    const { app, emit } = await mountWithEvents();
    try {
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
      await flushPromises();
      expect(app.text()).toContain(t("配对码约 {seconds} 秒后过期，过期后需要重新生成", { seconds: 90 }));

      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ enabled: true, peers: [] } as any);
      emit({ type: "event", session_id: "s", name: "netpair.code.expired",
        data: { code: "ABCD-EFGH-IJKL", reason: "expired" } });
      await flushPromises();
      expect(app.text()).toContain(t("配对码已过期，请重新生成"));
      expect(app.text()).not.toContain("ABCD-EFGH-IJKL");
    } finally {
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      app.unmount();
    }
  });

  it("marks an entered code's wait as timed out instead of leaving it waiting", async () => {
    // A wait the partner never answered used to stay on the card saying
    // "waiting" until the sidecar pruned it.  The event says it failed; the row
    // has to say so too, even while the status snapshot still lists it.
    setLocale("zh-CN");
    vi.clearAllMocks();
    const deadline = Math.floor(Date.now() / 1000) + 60;
    const waiting = { enabled: true, peers: [], waiting: [
      { peer_id: "WXYZ", name: "", since: 1, deadline },
    ] };
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue(waiting as any);
    const { app, emit } = await mountWithEvents();
    try {
      await app.get('[aria-label="互联网同步"]').trigger("click");
      await flushPromises();
      await flushPromises();
      expect(app.text()).toContain(t("等待对方确认…"));

      vi.mocked(bridge.internetPairingStatus).mockResolvedValue(waiting as any);
      emit({ type: "event", session_id: "s", name: "netpair.waiting_failed",
        data: { peer_id: "WXYZ", name: "", deadline, reason: "timeout" } });
      await flushPromises();
      expect(app.text()).toContain(t("对方没有确认配对码，本次配对已超时"));
      expect(app.text()).toContain(t("对方没有确认配对码，本次配对已超时，请重新生成或输入配对码"));
    } finally {
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], enabled: true });
      app.unmount();
    }
  });
});

describe("global hotkeys", () => {
  /** The window's hotkey listener, as the host calls it. */
  async function mountWithHotkeys(settings: Record<string, unknown> = {}) {
    let fire!: (action: string) => void;
    vi.mocked(bridge.onHotkey).mockImplementation(async (handler: (action: string) => void) => {
      fire = handler;
      return () => {};
    });
    vi.mocked(bridge.settings).mockResolvedValue({ settings } as any);
    const app = mount(App);
    await flushPromises();
    return { app, fire: (action: string) => fire(action) };
  }

  afterEach(() => {
    vi.mocked(bridge.onHotkey).mockResolvedValue(() => {});
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
    vi.mocked(bridge.setHotkeys).mockResolvedValue([]);
    vi.mocked(bridge.injectPaste).mockReset().mockResolvedValue({ ok: true });
    vi.mocked(bridge.status).mockResolvedValue({
      version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: "not_started",
    } as any);
    vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
      preview: '<img src=x onerror="window.injected=true">',
    })]));
    setLocale("zh-CN");
  });

  it("registers the bindings the settings hold, and marks the rows the OS refused", async () => {
    vi.mocked(bridge.setHotkeys).mockResolvedValue(["toggle_monitor"]);
    const { app } = await mountWithHotkeys({
      hotkeys_enabled: true,
      hotkeys: { paste_1: "Ctrl+Alt+1", show_window: "  " },
    });
    try {
      // The form -- and only the form -- is read when the page is opened, and
      // that is when the bindings reach the host.
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // A whitespace-only box is an emptied one, and handing it over would
      // register nothing under a name the user believes is cleared.
      expect(bridge.setHotkeys).toHaveBeenCalledWith(true, { paste_1: "Ctrl+Alt+1" });
      // A row the OS would not give up is marked where it can be retyped —
      // "another application already owns Ctrl+Shift+V" is an ordinary answer,
      // not a broken page.
      expect(app.find(".hotkey-row--refused").exists()).toBe(true);
      expect(app.text()).toContain(t("未生效"));
    } finally { app.unmount(); }
  });

  it("records a chord from one keypress instead of asking for its spelling", async () => {
    const { app } = await mountWithHotkeys({
      hotkeys_enabled: true,
      hotkeys: { paste_1: "Ctrl+Alt+1", paste_2: "" },
    });
    try {
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const row = () => app.get('[data-hotkey-row="paste_2"]');
      const record = () => app.findAll("button")
        .find(button => button.attributes("aria-label") === "录制 复制第 2 条 的组合键")!;
      expect(record().exists(), "no record button on a hotkey row").toBe(true);
      await record().trigger("click");
      await flushPromises();
      expect(row().attributes("placeholder")).toBe("按下组合键…");
      // A modifier on its own is not a chord: the row keeps waiting.
      await row().trigger("keydown", { key: "Shift", code: "ShiftLeft", shiftKey: true });
      await flushPromises();
      expect((row().element as HTMLInputElement).value).toBe("");
      // The real press, spelled the way the host takes it: the key is the
      // code's, not the character (Shift+1 would be "!").
      await row().trigger("keydown", { key: "V", code: "KeyV", ctrlKey: true, shiftKey: true });
      await flushPromises();
      expect((row().element as HTMLInputElement).value).toBe("Ctrl+Shift+V");
      expect(row().attributes("placeholder")).toBe("未绑定");
      // No modifier at all is refused in words rather than stored: an OS-wide
      // "V" would take that letter from every application.
      await record().trigger("click");
      await flushPromises();
      await row().trigger("keydown", { key: "v", code: "KeyV" });
      await flushPromises();
      expect((row().element as HTMLInputElement).value).toBe("Ctrl+Shift+V");
      expect(app.text()).toContain("至少要带一个修饰键");
      // Esc leaves the mode without changing anything.
      await row().trigger("keydown", { key: "Escape", code: "Escape" });
      await flushPromises();
      expect(row().attributes("placeholder")).toBe("未绑定");
      // And the recorded chord is what the save hands over.
      vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        hotkeys: expect.objectContaining({ paste_2: "Ctrl+Shift+V" }),
      }));
    } finally { app.unmount(); }
  });

  it("registers the saved bindings before the settings page is ever opened", async () => {
    const { app } = await mountWithHotkeys({
      hotkeys_enabled: true,
      hotkeys: { paste_1: "Ctrl+Alt+1", toggle_monitor: "Ctrl+Alt+M" },
    });
    try {
      // No visit to the page: a shortcut that starts working only after the
      // reader has been to Settings once per launch is one that does not work.
      expect(bridge.setHotkeys).toHaveBeenCalledWith(true, {
        paste_1: "Ctrl+Alt+1", toggle_monitor: "Ctrl+Alt+M",
      });
    } finally { app.unmount(); }
  });

  it("copies the row a shortcut names, and the newest one in plain text", async () => {
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "a", preview: "first" }),
      historyRow({ id: "b", preview: "second" }),
    ]));
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true });
    try {
      // One-based in the binding, and read from the same unfiltered list the
      // history page shows, so "the second one" is the same clip in both.
      fire("paste_2");
      await flushPromises();
      expect(bridge.history).toHaveBeenLastCalledWith("", 0, 2, "all", "newest");
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("b", false);

      // `Ctrl+Shift+V`: the newest clip, words only — the markup is what a
      // terminal turns into noise.
      fire("paste_plain");
      await flushPromises();
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("a", true);

      // `show_window` is the host's own: a binding whose whole job is to reach
      // a hidden window cannot depend on this one being awake, so it never
      // arrives here and must not be read as a copy.
      vi.mocked(bridge.copyHistory).mockClear();
      fire("show_window");
      await flushPromises();
      expect(bridge.copyHistory).not.toHaveBeenCalled();
    } finally {
      vi.mocked(bridge.copyHistory).mockClear();
      app.unmount();
    }
  });

  it("pastes what the shortcut copied, and says so, when auto-paste is on", async () => {
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "a", preview: "first" }),
      historyRow({ id: "b", preview: "second" }),
    ]));
    vi.mocked(bridge.injectPaste).mockClear();
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true, auto_paste_enabled: true });
    try {
      fire("paste_2");
      await flushPromises();
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("b", false);
      // The copy is only the first half now: the host types the system chord
      // into the application that had the focus, and the toast says pasted
      // rather than copied because that is what happened.
      expect(bridge.injectPaste).toHaveBeenCalledOnce();
      expect(app.text()).toContain(t("已粘贴第 {n} 条", { n: 2 }));
      expect(app.text()).not.toContain(t("已复制第 {n} 条", { n: 2 }));
    } finally {
      vi.mocked(bridge.copyHistory).mockClear();
      app.unmount();
    }
  });

  it("falls back to the clipboard when the paste is refused", async () => {
    vi.mocked(bridge.injectPaste).mockRejectedValue({
      code: "PASTE_FAILED", message: "the OS refused the injection",
    });
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true, auto_paste_enabled: true });
    try {
      fire("paste_1");
      await flushPromises();
      // The clipboard already holds the clip, so this is the one sentence that
      // leaves the reader something to do — not the failure band, which is for
      // a sidecar that stopped answering.
      expect(app.text()).toContain(t("已复制，请按 {shortcut} 手动粘贴", { shortcut: "Ctrl+V" }));
      expect(app.find(".error-band").exists()).toBe(false);
    } finally { app.unmount(); }
  });

  it("only copies when auto-paste is switched off", async () => {
    vi.mocked(bridge.injectPaste).mockClear();
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true, auto_paste_enabled: false });
    try {
      fire("paste_1");
      await flushPromises();
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("1", false);
      expect(bridge.injectPaste).not.toHaveBeenCalled();
      expect(app.text()).toContain(t("已复制第 {n} 条", { n: 1 }));
    } finally { app.unmount(); }
  });

  it("pastes the newest remote item, and says when there is none", async () => {
    // `source_device` marks the rows that arrived from another machine; this
    // shortcut is the one key that reaches them without reading the list.
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "local", preview: "here" }),
      historyRow({ id: "remote", preview: "from laptop", source_device: "peer-1" }),
    ]));
    vi.mocked(bridge.injectPaste).mockClear();
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true, auto_paste_enabled: true });
    try {
      fire("paste_remote_last");
      await flushPromises();
      // The newest fifty, because a local clip on top of the stack must not
      // hide the remote one this chord is for.
      expect(bridge.history).toHaveBeenLastCalledWith("", 0, 50, "all", "newest");
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("remote", false);
      expect(bridge.injectPaste).toHaveBeenCalledOnce();
      expect(app.text()).toContain(t("已粘贴最新远程项"));

      // Nothing from a peer in the window: the press is answered rather than
      // left silent, and the clipboard keeps whatever it held.
      vi.mocked(bridge.copyHistory).mockClear();
      vi.mocked(bridge.injectPaste).mockClear();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({ id: "local", preview: "here" })]));
      fire("paste_remote_last");
      await flushPromises();
      expect(bridge.copyHistory).not.toHaveBeenCalled();
      expect(bridge.injectPaste).not.toHaveBeenCalled();
      expect(app.text()).toContain(t("还没有可粘贴的远程内容"));
    } finally {
      vi.mocked(bridge.copyHistory).mockClear();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
      app.unmount();
    }
  });

  it("says a screenshot has no text to give, instead of raising a failure band", async () => {
    // `Ctrl+Shift+V` asks for the text form of the newest clip.  A screenshot
    // on top of the history is an ordinary state of affairs, so the refusal is
    // answered beside the key press and not with the band a dead sidecar gets.
    vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
      id: "shot", preview: "[图片]", content_type: "IMAGE",
    })]));
    vi.mocked(bridge.copyHistory).mockRejectedValue({
      code: "NOT_SUPPORTED", message: "History item has no text format", retryable: false,
    });
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true });
    try {
      fire("paste_plain");
      await flushPromises();
      expect(bridge.copyHistory).toHaveBeenLastCalledWith("shot", true);
      expect(app.text()).toContain(t("这一条没有文字可以复制"));
      expect(app.find(".error-band").exists()).toBe(false);
    } finally {
      vi.mocked(bridge.copyHistory).mockReset().mockResolvedValue({ copied: true });
      app.unmount();
    }
  });

  it("says when the shortcut names a row the history does not have", async () => {
    // Ctrl+9 over three clips, or any of them over an empty history: the read
    // answers with a shorter page and the press was answered with nothing at
    // all, which a reader cannot tell from a key that is not bound.
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "a", preview: "first" }),
      historyRow({ id: "b", preview: "second" }),
    ]));
    vi.mocked(bridge.copyHistory).mockClear();
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true });
    try {
      fire("paste_9");
      await flushPromises();
      expect(bridge.copyHistory).not.toHaveBeenCalled();
      expect(app.text()).toContain(t("还没有第 9 条记录"));
      // An empty history leaves the plain-text chord nothing to copy either.
      vi.mocked(bridge.history).mockResolvedValue(historyPage([]));
      fire("paste_plain");
      await flushPromises();
      expect(app.text()).toContain(t("还没有可复制的记录"));
    } finally {
      vi.mocked(bridge.copyHistory).mockClear();
      app.unmount();
    }
  });

  it("pauses and resumes sync from the shortcut, reading the state each time", async () => {
    // A sidecar that actually pauses: the next status read says so.  A double
    // that kept answering "running" would pass a window that toggled a local
    // flag instead of asking.
    let syncState = "running";
    vi.mocked(bridge.status).mockImplementation(async () => ({
      version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: syncState,
    } as any));
    vi.mocked(bridge.setSyncEnabled).mockImplementation(async (enabled: boolean) => {
      syncState = enabled ? "running" : "stopped";
      return { enabled };
    });
    const { app, fire } = await mountWithHotkeys({ hotkeys_enabled: true });
    try {
      fire("toggle_monitor");
      await flushPromises();
      expect(bridge.setSyncEnabled).toHaveBeenLastCalledWith(false);

      // The second press asks what the state is now rather than remembering
      // what it was: the sync can be paused from the page or the tray between
      // the two presses.
      fire("toggle_monitor");
      await flushPromises();
      expect(bridge.setSyncEnabled).toHaveBeenLastCalledWith(true);
    } finally {
      vi.mocked(bridge.setSyncEnabled).mockReset().mockResolvedValue({ enabled: true });
      app.unmount();
    }
  });
});

describe("backup and restore", () => {
  /** Mount on the settings page with one backup in the list.
   *
   * The list is read by its own refresh button rather than by opening the page,
   * so a case that reaches a row in it has to press that first — the same
   * button the panel puts beside its heading.
   */
  async function mountWithBackup(app: ReturnType<typeof mount>) {
    await flushPromises();
    await app.get('[aria-label="刷新备份"]').trigger("click");
    await flushPromises();
    await app.get('[aria-label="恢复备份 one.zip"]').trigger("click");
    await flushPromises();
  }
  it("says a long backup is still running, and locks the other five while it is", async () => {
    // Six buttons, each of which looks like the others and gives no sign of
    // life for tens of seconds -- the sidecar reports no progress for any of
    // them.  So the card says which one is running and how long ago the click
    // was, and the other five are shut: a second click would start a second
    // backup over the same directory.  See U23.
    setLocale("zh-CN");
    vi.clearAllMocks();
    let release: (value: any) => void = () => {};
    vi.mocked(bridge.listBackups).mockResolvedValue({ backups: [] } as any);
    vi.mocked(bridge.createBackup).mockReturnValue(new Promise((resolve) => {
      release = resolve;
    }) as any);
    const app = mountOn("settings");
    try {
      await flushPromises();
      await app.findAll("button").find(entry => entry.text() === "创建备份")!.trigger("click");
      await flushPromises();

      const line = app.get(".backup-progress");
      expect(line.text()).toContain("正在创建备份");
      expect(line.text()).toContain("已用时 0 秒");
      // The other five wait for it rather than running beside it.
      const others = app.findAll("button").filter(entry =>
        ["选择备份恢复", "导入历史", "导出 JSON", "导出 CSV", "导出 Markdown"].includes(entry.text()));
      expect(others).toHaveLength(5);
      expect(others.every(entry => entry.attributes("disabled") !== undefined)).toBe(true);

      release({ backup_path: "C:/b/two.zip" });
      await flushPromises();
      // The line is replaced by the result, not left beside it.
      expect(app.find(".backup-progress").exists()).toBe(false);
      expect(app.text()).toContain("备份已创建：C:/b/two.zip");
    } finally {
      app.unmount();
      vi.mocked(bridge.createBackup).mockReset();
      vi.mocked(bridge.listBackups).mockReset();
    }
  });
  it("says what a restore brought back in the reader's words, not the host's payload", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    // jsdom has no modal `<dialog>`; the window's dialogs are opened through it.
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.listBackups).mockResolvedValue({
      backups: [{ path: "C:/b/one.zip", filename: "one.zip" }],
    } as any);
    vi.mocked(bridge.restoreBackup).mockResolvedValue(
      { config: true, history: 120, favorites: 7 } as any);
    const app = mountOn("settings");
    try {
      await mountWithBackup(app);
      await app.findAll("button").find(button => button.text() === "确认恢复")!.trigger("click");
      await flushPromises();
      expect(bridge.restoreBackup).toHaveBeenCalledWith("C:/b/one.zip");
      // What came back, named — the answer used to be put on screen whole,
      // braces and all, which is a payload where a reader was looking for a
      // result.
      expect(app.text()).toContain("恢复完成 · 设置、历史 120 条、7 条收藏");
      expect(app.text()).not.toContain("history");
    } finally { app.unmount(); setLocale("zh-CN"); }
  });

  it("names the parts of a restore that did not finish", async () => {
    // The half the payload hid best: rows that landed beside settings that
    // could not be persisted are a partial restore, and without this the line
    // reports a success with a foreign word at the end of it.
    setLocale("zh-CN");
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.listBackups).mockResolvedValue({
      backups: [{ path: "C:/b/one.zip", filename: "one.zip" }],
    } as any);
    vi.mocked(bridge.restoreBackup).mockResolvedValue(
      { config: true, history: 120, favorites: 7, errors: ["config persistence"] } as any);
    const app = mountOn("settings");
    try {
      await mountWithBackup(app);
      await app.findAll("button").find(button => button.text() === "确认恢复")!.trigger("click");
      await flushPromises();
      expect(app.text()).toContain("有 1 项没有完成");
    } finally { app.unmount(); setLocale("zh-CN"); }
  });

  it("says so when the picker itself is refused", async () => {
    // The same two pickers elsewhere in this window (`shareFileToPhone`,
    // `restoreBackup`) both report a picker that throws.  This one called it
    // outside the try, so a refused dialog was an unhandled rejection and the
    // button had nothing after it.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.chooseFile).mockRejectedValue(
      { code: "PICKER_FAILED", message: "The file dialog could not be opened", retryable: true } as any);
    const app = mountOn("settings");
    try {
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "导入历史")!.trigger("click");
      await flushPromises();
      expect(bridge.importHistory).not.toHaveBeenCalled();
      expect(app.get(".error-band").text()).toContain("The file dialog could not be opened");
    } finally {
      vi.mocked(bridge.chooseFile).mockReset();
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("says how much of the history an import brought back", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.chooseFile).mockResolvedValue("C:/b/history.json" as any);
    vi.mocked(bridge.importHistory).mockResolvedValue({ ok: true, imported: 340 } as any);
    const app = mountOn("settings");
    try {
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "导入历史")!.trigger("click");
      await flushPromises();
      expect(bridge.importHistory).toHaveBeenCalledWith("C:/b/history.json");
      // The count, not the object it arrived in.
      expect(app.text()).toContain("导入完成 · 340 条记录");
      expect(app.text()).not.toContain("imported");
    } finally {
      vi.mocked(bridge.chooseFile).mockReset();
      app.unmount();
      setLocale("zh-CN");
    }
  });
});

describe("the settings form's own numbers", () => {
  it("puts the number it stored back in the box, rather than the one that was typed", async () => {
    // `clampNumber` keeps an out-of-range box from failing the whole save —
    // the sidecar refuses anything past the bound outright — but the clamped
    // value was sent and never put back.  A box holding 9999 saved 500 and went
    // on showing 9999 beside a bar saying everything was saved, and the number
    // on screen is the one the reader believes is in force.
    setLocale("zh-CN");
    vi.clearAllMocks();
    // One payload for every read, so the only thing that can move is the box.
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { device_name: "Local", web_history_limit: 30 },
    } as any);
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="显示历史条数"]').setValue("9999");
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      // The bound, not the number that was typed: 500 is what the sidecar
      // stored and what is in force from here on.
      expect(bridge.updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ web_history_limit: 500 }));
      expect((app.get('[aria-label="显示历史条数"]').element as HTMLInputElement).value)
        .toBe("500");
      // And the save is still a save — putting the number back is not an edit
      // the bar is holding.
      expect(app.text()).toContain("所有更改都已保存");
      expect(app.text()).not.toContain("有未保存的更改");
    } finally { app.unmount(); setLocale("zh-CN"); }
  });

  it("marks a field the sidecar would refuse, and holds the save until it is fixed", async () => {
    // The three hand-typed fields stop being clamped silently.  A port below
    // 1024 used to come back as 1024 with the box still showing what was typed,
    // beside a bar saying everything was saved — two different numbers, and
    // nothing saying which was in force.  See U51 and U52.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { device_name: "Local", port: 45871, data_dir: "", relay_brokers: ["broker.example.com:8883"] },
    } as any);
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();

      const port = app.get('[aria-label="TCP 端口"]');
      await port.setValue("80");
      await flushPromises();
      // The field says so, the box says so to a screen reader, and the save is
      // held rather than sending something else.
      expect(app.find("#settings-error-port").text()).toContain("1024–65535");
      expect(port.attributes("aria-invalid")).toBe("true");
      expect(port.attributes("aria-describedby")).toBe("settings-error-port");
      expect(app.text()).toContain("有 1 个字段还不能保存");
      expect(app.findAll("button").find(entry => entry.text() === "保存设置")!.attributes("disabled"))
        .toBeDefined();
      await app.findAll("button").find(entry => entry.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).not.toHaveBeenCalled();

      // Fixed, and the marks go with it.
      await port.setValue("45871");
      await flushPromises();
      expect(app.find("#settings-error-port").exists()).toBe(false);
      // ARIA's own way of saying "valid again": the attribute stays, the value
      // turns false.
      expect(port.attributes("aria-invalid")).toBe("false");
      await app.findAll("button").find(entry => entry.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledWith(expect.objectContaining({ port: 45871 }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      vi.mocked(bridge.updateSettings).mockReset();
      setLocale("zh-CN");
    }
  });

  it("says which broker entries are not addresses, instead of saving them", async () => {
    // The common paste on a one-per-line field: two addresses separated by a
    // space, or a whole sentence.  The sidecar would take them and the relay
    // would silently never come up.  See U51/U52.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "Local" } } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="公共中继地址"]').setValue("broker.example.com:8883 and another one");
      await flushPromises();
      const error = app.get("#settings-error-relay-brokers").text();
      expect(error).toContain("公共中继地址");
      expect(error).toContain("broker.example.com:8883 and another one");
      expect(app.text()).toContain("有 1 个字段还不能保存");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });
});

describe("the paste and recording card", () => {
  /** The checkbox under a label, by the words the reader sees. */
  const check = (app: VueWrapper, label: string) => app.findAll("label")
    .find(node => (node.text() || "").trim() === label)!.get("input");

  it("shows the stored rules, and saves exactly the five fields", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      device_name: "Local",
      auto_paste_enabled: false,
      skip_password_manager_clips: true,
      skip_prelaunch_clipboard: false,
      max_non_file_paste_mb: 12,
      source_exclusion_patterns: ["kdenlive", "Signal"],
    } } as any);
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // The card lives in 通用, after 剪贴板历史.
      await app.findAll(".settings-nav-group button")
        .find(chip => chip.text() === t("粘贴与录制"))!.trigger("click");
      await flushPromises();
      expect(app.findAll(".settings-section")
        .filter(card => (card.element as HTMLElement).style.display !== "none")
        .map(card => card.attributes("id"))).toEqual(["settings-recording"]);

      // The stored values, including the list as one pattern per line.
      expect((check(app, t("自动粘贴到当前应用")).element as HTMLInputElement).checked).toBe(false);
      expect((check(app, t("密码管理器内容不记录")).element as HTMLInputElement).checked).toBe(true);
      expect((check(app, t("跳过启动前复制的内容")).element as HTMLInputElement).checked).toBe(false);
      expect((app.get('[aria-label="非文件类单条大小上限"]').element as HTMLInputElement).value).toBe("12");
      expect((app.get('[aria-label="来源应用排除列表"]').element as HTMLTextAreaElement).value)
        .toBe("kdenlive\nSignal");

      // Changed: the switch on, a number past the ceiling, a list with padding
      // and an empty line, and one rule off.
      await check(app, t("自动粘贴到当前应用")).setValue(true);
      await check(app, t("跳过启动前复制的内容")).setValue(true);
      await app.get('[aria-label="非文件类单条大小上限"]').setValue("99999");
      await app.get('[aria-label="来源应用排除列表"]').setValue("  kdenlive \n\n Signal \n");
      await flushPromises();
      expect(app.text()).toContain(t("有未保存的更改"));
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        auto_paste_enabled: true,
        skip_password_manager_clips: true,
        skip_prelaunch_clipboard: true,
        // Clamped to the sidecar's ceiling, and the box is put back to it: a
        // form still showing 99999 beside a bar saying "saved" would be a
        // number the reader believes is in force.
        max_non_file_paste_mb: 1024,
        source_exclusion_patterns: ["kdenlive", "Signal"],
      }));
      expect((app.get('[aria-label="非文件类单条大小上限"]').element as HTMLInputElement).value)
        .toBe("1024");
      expect(app.text()).toContain(t("所有更改都已保存"));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });

  it("draws what the sidecar would do for a config that never stored the fields", async () => {
    // The rules ship on and the limit is 8 MB, so an older config draws the
    // shipped policy rather than four empty boxes the reader has to interpret.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "Local" } } as any);
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect((check(app, t("自动粘贴到当前应用")).element as HTMLInputElement).checked).toBe(true);
      expect((check(app, t("密码管理器内容不记录")).element as HTMLInputElement).checked).toBe(true);
      expect((check(app, t("跳过启动前复制的内容")).element as HTMLInputElement).checked).toBe(true);
      expect((app.get('[aria-label="非文件类单条大小上限"]').element as HTMLInputElement).value).toBe("8");
      expect((app.get('[aria-label="来源应用排除列表"]').element as HTMLTextAreaElement).value).toBe("");
      // And a save with nothing typed sends those defaults, which is the same
      // policy the sidecar holds — not a config the visit rewrote.
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        auto_paste_enabled: true,
        max_non_file_paste_mb: 8,
        source_exclusion_patterns: [],
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });
});

describe("the file card", () => {
  /** A settings read with the three 1.3.1 limits and the receive folder. */
  function stored(overrides: Record<string, unknown> = {}) {
    return { settings: {
      device_name: "Local", max_file_mb: 100, confirm_large_file_mb: 0,
      relay_paste_max_mb: 25, file_receive_dir: "D:/incoming", ...overrides,
    } } as any;
  }
  async function openFilesCard(app: ReturnType<typeof mount>) {
    await app.get('[aria-label="设置"]').trigger("click");
    await flushPromises();
    await app.findAll(".settings-nav-group button")
      .find(chip => chip.text() === t("文件"))!.trigger("click");
    await flushPromises();
  }
  const input = (app: VueWrapper, label: string) => app.get(`[aria-label="${label}"]`);

  it("shows the stored limits and saves them, clamping past the ceiling", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue(stored());
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    const app = mount(App);
    try {
      await flushPromises();
      await openFilesCard(app);
      expect((input(app, t("单个文件大小上限")).element as HTMLInputElement).value).toBe("100");
      expect((input(app, t("粘贴前确认的文件大小")).element as HTMLInputElement).value).toBe("0");
      expect((input(app, t("中继取回文件上限")).element as HTMLInputElement).value).toBe("25");
      expect((input(app, t("文件接收目录")).element as HTMLInputElement).value).toBe("D:/incoming");

      // 0 is a real value on all three (no cap / never ask / no extra relay
      // cap), so the form must keep it rather than fall back to the default.
      await input(app, t("单个文件大小上限")).setValue("999999");
      await input(app, t("粘贴前确认的文件大小")).setValue("512");
      await input(app, t("文件接收目录")).setValue("  E:/files  ");
      await flushPromises();
      expect(app.text()).toContain(t("有未保存的更改"));
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        max_file_mb: 65536,
        confirm_large_file_mb: 512,
        relay_paste_max_mb: 25,
        file_receive_dir: "E:/files",
      }));
      // The clamped number is what the box shows after a save: the form and
      // the stored policy cannot be two different numbers.
      expect((input(app, t("单个文件大小上限")).element as HTMLInputElement).value).toBe("65536");
      expect(app.text()).toContain(t("所有更改都已保存"));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });

  it("draws the shipped defaults, and fills the receive folder from the picker", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "Local" } } as any);
    vi.mocked(bridge.updateSettings).mockResolvedValue({} as any);
    vi.mocked(bridge.chooseFolder).mockResolvedValue("E:/received");
    const app = mount(App);
    try {
      await flushPromises();
      await openFilesCard(app);
      expect((input(app, t("单个文件大小上限")).element as HTMLInputElement).value).toBe("2048");
      expect((input(app, t("粘贴前确认的文件大小")).element as HTMLInputElement).value).toBe("512");
      expect((input(app, t("中继取回文件上限")).element as HTMLInputElement).value).toBe("50");
      expect((input(app, t("文件接收目录")).element as HTMLInputElement).value).toBe("");

      // The picker replaces the box rather than appending, like the data
      // directory's own row.
      await input(app, t("选择接收目录")).trigger("click");
      await flushPromises();
      expect((input(app, t("文件接收目录")).element as HTMLInputElement).value).toBe("E:/received");
    } finally {
      app.unmount();
      vi.mocked(bridge.chooseFolder).mockReset();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });
});

describe("dialog chrome", () => {
  // jsdom has `HTMLDialogElement` but not its two methods, and this case is the
  // only one in this file that asks whether the element is really open: the
  // rest replace `showModal` with a spy.  The stub keeps `open` honest and puts
  // back whatever was there before.
  let undo: Array<() => void> = [];
  beforeEach(() => {
    const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
    const swap = (key: string, value: unknown) => {
      const had = Object.prototype.hasOwnProperty.call(proto, key);
      const before = proto[key];
      proto[key] = value;
      undo.push(() => {
        if (had) proto[key] = before;
        else delete proto[key];
      });
    };
    swap("showModal", function (this: HTMLDialogElement) {
      if (this.open) throw new Error("InvalidStateError");
      this.setAttribute("open", "");
    });
    swap("close", function (this: HTMLDialogElement) {
      if (!this.open) return;
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    });
  });
  afterEach(() => {
    for (const restore of undo) restore();
    undo = [];
  });

  it("gives a migrated dialog the close button the others have", async () => {
    // U32's first batch: six dialogs moved onto AppDialog.  The shell's own
    // tests cannot see this — the point is that these particular dialogs now
    // have a way out that does not require reading the two buttons at the
    // bottom, and that closing that way leaves the same state as Esc does.
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { device_name: "Local", password_set: true },
    } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await buttons(app, "清除加密密码")[0].trigger("click");
      await flushPromises();
      const selector = 'dialog[aria-labelledby="clear-password-title"]';
      const isOpen = () => (app.get(selector).element as HTMLDialogElement).open;
      expect(isOpen()).toBe(true);
      await app.get(selector).get(".modal-close").trigger("click");
      await flushPromises();
      expect(isOpen()).toBe(false);
      // And the flag behind it is cleared too, so the same dialog opens again
      // rather than needing a page switch first.
      await buttons(app, "清除加密密码")[0].trigger("click");
      await flushPromises();
      expect(isOpen()).toBe(true);
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
      setLocale("zh-CN");
    }
  });
});

describe("the settings rail", () => {
  /** The strip's chips, in the order they are drawn. */
  const chips = (app: VueWrapper) => app.findAll(".settings-nav-group button");
  /** The cards on screen, by the id the page gave them. */
  const shown = (app: VueWrapper) => app.findAll(".settings-section")
    .filter(card => (card.element as HTMLElement).style.display !== "none")
    .map(card => card.attributes("id"));
  /** The cluster a name heads, found by the words inside it. */
  const cluster = (app: VueWrapper, word: string) =>
    app.findAll(".settings-nav-group").find(node => node.text().includes(word))!;

  it("puts every card one click from the strip, with the group names as labels", async () => {
    // Ten cards behind two rows of controls: four groups, then the open
    // group's cards under them.  Every card but a group's first was two clicks
    // away, and the group names were a second name for a place the cards
    // already named — 系统 held 软件更新 and 诊断与维护, and neither of those
    // words is 系统.  One row of chips is the whole of the page's navigation
    // now, and a group is a word beside the chips it introduces.  See U37.
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(chips(app).map(chip => chip.text())).toEqual([
        "常规", "剪贴板历史", "粘贴与录制", "文件", "同步", "局域网发现", "网络与高级", "翻译",
        "安全", "数据备份", "软件更新", "诊断与维护",
      ]);
      // One click from the strip to the last card, which used to be a group
      // tab and then a chip.
      await chips(app)[11].trigger("click");
      expect(shown(app)).toEqual(["settings-diagnostics"]);
      // The four names are over their runs of chips and are not controls: no
      // chip carries a group's name, and nothing at that level opens anything.
      const labels = app.findAll(".settings-nav-label");
      expect(labels.map(label => label.text())).toEqual(["通用", "连接", "数据", "系统"]);
      expect(labels.map(label => label.element.tagName)).toEqual(["P", "P", "P", "P"]);
      expect(chips(app).some(chip => labels.map(l => l.text()).includes(chip.text()))).toBe(false);
      // Each name is the accessible name of the cluster it heads, and the
      // visible copy is hidden from the tree so the group is named once.
      const groups = app.findAll('.settings-nav [role="group"]');
      expect(groups.map(group => group.attributes("aria-label"))).toEqual(["通用", "连接", "数据", "系统"]);
      expect(groups[0].get(".settings-nav-label").attributes("aria-hidden")).toBe("true");
    } finally {
      app.unmount();
    }
  });

  it("dims the chips a query misses, and the names over them", async () => {
    // The counts stayed where they were — a chip still says how many places in
    // its card hold the query, and the line under the box still counts cards.
    // What changed is the level the dimming happens at: a group is no longer a
    // control that can be dimmed, so the name dims with the chips it introduces
    // and a bright name over dimmed chips is never left behind.  See U37.
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="搜索设置…"]').setValue("中继");
      // The box applies a word after the pause a reader takes between words,
      // so the test waits that pause out rather than pretending to type faster
      // than anyone does.
      await new Promise(resolve => setTimeout(resolve, 200));
      await flushPromises();
      // 中继 is named by two cards now — the sync card's own limits and the
      // file card's relay-paste limit — so two chips hold it, one in each of
      // two groups.
      const matched = chips(app).filter(chip => chip.classes("has-match"))
        .map(chip => chip.text().split(" · ")[0]);
      const dimmed = chips(app).filter(chip => chip.classes("dimmed")).map(chip => chip.text());
      expect(matched).toEqual(["文件", "同步"]);
      // Nothing is left neutral while a query is up: every chip either holds
      // the query or is reported as not holding it.
      expect(matched.length + dimmed.length).toBe(chips(app).length);
      expect(dimmed).toContain("剪贴板历史");
      // A group's name dims only when nothing in it matches.
      expect(cluster(app, "通用").get(".settings-nav-label").classes("dimmed")).toBe(false);
      expect(cluster(app, "连接").get(".settings-nav-label").classes("dimmed")).toBe(false);
      expect(cluster(app, "数据").get(".settings-nav-label").classes("dimmed")).toBe(true);
      // And with the query dropped, every name is bright again.
      await app.get(".settings-search-clear").trigger("click");
      await flushPromises();
      expect(app.findAll(".settings-nav-label.dimmed")).toHaveLength(0);
    } finally {
      app.unmount();
    }
  });

  it("keeps the unsaved-edit dot on the chip, now that every chip is on screen", async () => {
    // The dot moved down a level with the chips.  It sat on the group tab as
    // well as on the card because a card inside a closed group was a card the
    // reader could not see; with all ten on screen the dot on the card is the
    // whole answer, and the group's copy could only ever say "somewhere in
    // here".  See U37.
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(app.findAll(".settings-nav .settings-edited")).toHaveLength(0);
      await app.get("#settings-general input").setValue("Desktop");
      await flushPromises();
      const chip = (name: string) => chips(app).find(node => node.text() === name)!;
      expect(chip("常规").find(".settings-edited").exists()).toBe(true);
      // One dot, on the card whose field changed.
      expect(app.findAll(".settings-nav .settings-edited")).toHaveLength(1);
      expect(chip("诊断与维护").find(".settings-edited").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} } as any);
    }
  });
});

describe("the quick search chord", () => {
  /** The palette's dialog, and whether it is really open. */
  const palette = (app: VueWrapper) => 'dialog[aria-labelledby="quick-search-title"]';
  const isOpen = (app: VueWrapper) =>
    (app.get(palette(app)).element as HTMLDialogElement).open;
  const chord = async (app: VueWrapper) => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }));
    await flushPromises();
    return app;
  };
  /** Everything an open dialog needs from jsdom, which has neither method. */
  const stubs = () => {
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      if (this.open) throw new Error("InvalidStateError");
      this.setAttribute("open", "");
    };
    HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    };
  };

  it("opens on Ctrl+K from any page, and hands the word to the history list", async () => {
    // Three searches and no way to reach either of them by word: the reader had
    // to pick a page before they were allowed to look, and the page they picked
    // was a guess.  One chord opens the palette from wherever they are.  See U50.
    stubs();
    const app = mountOn("overview", document.body);
    try {
      await flushPromises();
      // From the overview, which has no search box of its own.
      await chord(app);
      expect(isOpen(app)).toBe(true);
      // Typed and confirmed with Enter: the word belongs to a page, and the
      // page is where the full set and the row actions are.
      await app.get('[aria-label="搜索历史与收藏"]').setValue("会议");
      await app.get('[aria-label="搜索历史与收藏"]').trigger("keydown", { key: "Enter" });
      await flushPromises();
      expect(isOpen(app)).toBe(false);
      expect(app.find(".history-list").exists()).toBe(true);
      expect((app.get('[aria-label="搜索历史记录"]').element as HTMLInputElement).value).toBe("会议");
    } finally {
      app.unmount();
    }
  });

  it("hands the same word to the library when that is the scope", async () => {
    // The other half of the same question: a snippet the reader saved rather
    // than copied.  The library's own search is fed with no group, because the
    // count the palette printed was every favourite that held the word.
    stubs();
    vi.mocked(bridge.status).mockResolvedValue({
      version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["favorites.list"],
    } as any);
    vi.mocked(bridge.favorites).mockClear();
    const app = mountOn("overview", document.body);
    try {
      await flushPromises();
      await chord(app);
      await app.get('[aria-label="搜索历史与收藏"]').setValue("周报");
      await app.findAll(".quick-search-scopes button")[1].trigger("click");
      await flushPromises();
      // Enter, since an empty library answers with no rows and the button under
      // them is only there when there are some.
      await app.get('[aria-label="搜索历史与收藏"]').trigger("keydown", { key: "Enter" });
      await flushPromises();
      expect(isOpen(app)).toBe(false);
      expect(app.find(".favorites-view").exists()).toBe(true);
      // The store's own page size, not the palette's six: the page reads the
      // list the way it always does, and with no group.
      expect(vi.mocked(bridge.favorites).mock.calls).toContainEqual(["周报", "", 0, 30]);
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({
        version: "test", health: "ready", device_name: "Local",
        session_id: "s", seq: 0, sync_state: "not_started",
      } as any);
    }
  });
});
