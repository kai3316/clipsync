import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";
import { bridge } from "../src/api/bridge";
import type { HistoryItem } from "../src/api/types";
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

vi.mock("../src/api/bridge", () => ({
  inDesktop: () => true,
  bridge: {
    subscribe: vi.fn().mockResolvedValue(() => {}),
    status: vi.fn().mockResolvedValue({
      version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: "not_started",
    }),
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
    internetPairingStatus: vi.fn().mockResolvedValue({ peers: [] }),
    // The answer names the provisional tag and admits the pairing is half done,
    // so the panel has no success to report even when the call succeeds.
    enterInternetPairingCode: vi.fn().mockResolvedValue({ peer_id: "", waiting: true }),
    relayDeliveryStatus: vi.fn().mockResolvedValue({ pending: 0, items: [] }),
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
    openHistoryLink: vi.fn().mockResolvedValue({ opened: true, url: "https://example.com" }),
    // Opening the devices page reads the phone service's status whatever
    // sub-tab is showing, so an unresolved mock left every devices-page case
    // with a spurious error band ("cannot read properties of undefined").
    companionStatus: vi.fn().mockResolvedValue({
      enabled: false, port: 8765, running: false, state: "off", access_url: null,
    }),
    configureCompanion: vi.fn(),
    chooseFile: vi.fn(),
    listBackups: vi.fn().mockResolvedValue({ backups: [] }),
    restoreBackup: vi.fn(),
    confirmPairing: vi.fn().mockResolvedValue({ paired: false, status: "confirmed_waiting" }),
    unpairDevice: vi.fn().mockResolvedValue({ accepted: true }),
    connectDevice: vi.fn().mockResolvedValue({ accepted: true }),
    disconnectDevice: vi.fn().mockResolvedValue({ disconnected: true }),
    forgetDevice: vi.fn().mockResolvedValue({ forgotten: true }),
    restoreDevice: vi.fn().mockResolvedValue({ restored: true }),
    purgeDevice: vi.fn().mockResolvedValue({ purged: true }),
    testDevice: vi.fn(),
    deviceCerts: vi.fn().mockResolvedValue({ devices: [] }),
    retrustDevice: vi.fn().mockResolvedValue({ trusted: true }),
    sendUrl: vi.fn().mockResolvedValue({ sent: true, device_id: "p" }),
    pushText: vi.fn().mockResolvedValue({ ok: true, len: 5, sent: true }),
    readLogs: vi.fn().mockResolvedValue({ logs: ["line 1", "line 2"] }),
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
      // sidebar advertises it.  Settings is drawn after the six content pages,
      // so the pages count 1–6 and settings is 7.
      const transfers = app.findAll("button").find(button => button.attributes("aria-label") === "文件传输")!;
      expect(transfers.attributes("aria-keyshortcuts")).toBe("Control+4");
      expect(transfers.attributes("title")).toContain("Ctrl+4");
      const aiRow = app.findAll("button").find(button => button.attributes("aria-label") === "AI 配置")!;
      expect(aiRow.attributes("aria-keyshortcuts")).toBe("Control+6");
      await press("4");
      expect(app.find(".transfers-view").exists()).toBe(true);
      await press("3");
      expect(app.find(".favorites-view").exists()).toBe(true);
      // Ctrl+F goes to the search box, from wherever it is pressed: favourites
      // has none of its own, so the history page's is the one it reaches.
      await press("f");
      expect(app.find(".history-list").exists()).toBe(true);
      expect(document.activeElement).toBe(app.get('[aria-label="搜索历史记录"]').element);
      // The AI page is a page of content, and the settings page is not: the
      // chord reaches each of them and leaves the other alone.
      await press("6");
      expect(app.find(".ai-page").exists()).toBe(true);
      expect(app.find(".settings-panel").exists()).toBe(false);
      await press("7");
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
  it("names every page in the header, in both languages, with its own count", async () => {
    setLocale("zh-CN");
    const app = mount(App);
    try {
      await flushPromises();
      // The header's two lines come from one table keyed by page, so a page
      // that is drawn but not named there is the failure this catches.
      const open = async (digit: string) => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: digit, ctrlKey: true }));
        await flushPromises();
        return [app.get("header h1").text(), app.get("header p").text()];
      };
      expect(await open("1")).toEqual(["剪贴板历史", "1 条记录"]);
      expect(await open("2")).toEqual(["设备", "0 台设备"]);
      expect(await open("3")).toEqual(["收藏库", "0 条收藏"]);
      expect(await open("4")).toEqual(["文件传输", "文件发送与接收"]);
      expect(await open("5")).toEqual(["附近聊天", "与附近设备进行会话"]);
      expect(await open("6")).toEqual(["AI 配置", "读取、编辑本机与已配对设备的 AI 工具配置"]);
      expect(await open("7")).toEqual(["设置", "本地配置"]);
      // The counts and labels are both live: a table written once would leave
      // the header in the language it was first read in.
      setLocale("en");
      await flushPromises();
      expect(await open("1")).toEqual(["Clipboard History", "1 records"]);
      expect(await open("4")).toEqual(["File Transfer", "File sending and receiving"]);
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });
  it("says when the settings form holds something the sidecar has not been told", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "desk" } });
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // Loaded and untouched: nothing is pending, so the bar says nothing.
      expect(app.text()).not.toContain("有未保存的更改");
      const name = () => app.findAll("label").find(node => node.text().startsWith("设备名称"))!.get("input");
      await name().setValue("laptop");
      expect(app.text()).toContain("有未保存的更改");
      // Typed back to what was loaded, the form is the saved state again.
      await name().setValue("desk");
      expect(app.text()).not.toContain("有未保存的更改");
      await name().setValue("laptop");
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(app.text()).not.toContain("有未保存的更改");
      expect(app.text()).toContain("所有更改都已保存");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });
  it("marks the cards holding an edit the reader has walked away from", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { device_name: "desk", port: 19990 } });
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const rail = (label: string) =>
        app.findAll(".settings-nav button").find(button => button.text().startsWith(label))!;
      const dot = (label: string) => rail(label).find(".settings-edited");
      // The same reading the rail's own cases make: `v-show` hides with an
      // inline display, and that inline style is what a tree jsdom never
      // attached to the document can be read for.
      const onScreen = (id: string) =>
        (app.get(`#settings-${id}`).element as HTMLElement).style.display !== "none";
      const deviceName = () =>
        app.findAll("label").find(node => node.text().startsWith("设备名称"))!.get("input");
      // A form that is the saved state marks nothing, whichever card is on
      // screen: the marks answer the same question the Save bar does.
      expect(app.findAll(".settings-edited")).toHaveLength(0);
      await deviceName().setValue("laptop");
      expect(app.text()).toContain("有未保存的更改");
      expect(dot("常规").exists()).toBe(true);
      expect(dot("网络与高级").exists()).toBe(false);
      // The mark says what it means to a reader who cannot see a colour.
      expect(dot("常规").attributes("role")).toBe("img");
      expect(dot("常规").attributes("aria-label")).toBe("有未保存的更改");
      // The group tab above it says so too.  It has to: the strip below the
      // groups lists one group's cards at a time, so an edit left in another
      // group is a dot on that group's tab or it is nowhere.
      expect(app.findAll(".settings-nav-groups .settings-edited")).toHaveLength(1);
      // Walking to another card leaves the mark where the edit is.  That is the
      // point of it: the card holding the edit is no longer on screen.
      await rail("网络与高级").trigger("click");
      await flushPromises();
      expect(onScreen("general")).toBe(false);
      expect(dot("常规").exists()).toBe(true);
      // A second card's edit marks that one, and only that one.
      await app.get('[aria-label="TCP 端口"]').setValue("20001");
      expect(dot("网络与高级").exists()).toBe(true);
      // Counted per row, because the two rows answer different questions: two
      // cards hold an edit, and they sit in two different groups.
      expect(app.findAll(".settings-nav-group .settings-edited")).toHaveLength(2);
      expect(app.findAll(".settings-nav-groups .settings-edited")).toHaveLength(2);
      // A control inside a card that is not a form field changes nothing the save
      // writes, so it marks nothing ~ the translation card's API key is typed in
      // without anything else being true: it has its own button and its own
      // request, and the snapshot the save is compared against does not carry it.
      await rail("翻译").trigger("click");
      await flushPromises();
      await app.get('[aria-label="翻译 API 密钥"]').setValue("sk-typed");
      expect(dot("翻译").exists()).toBe(false);
      expect(app.findAll(".settings-nav-group .settings-edited")).toHaveLength(2);

      // Saving is the end of the question: the marks go with it.
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(app.findAll(".settings-edited")).toHaveLength(0);
      expect(app.text()).toContain("所有更改都已保存");
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("round-trips the notification switches the host gates its own toasts on", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      notifications_enabled: true, notify_transfer: true,
      notify_pairing: false, notify_device_connect: false,
    } });
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const toggle = (label: string) =>
        app.findAll("label").find(node => node.text() === label)!.get("input");
      // The switch has to show what the sidecar holds: a control that reads as
      // off every time it opens is a control the user flips twice.
      expect((toggle("配对请求通知").element as HTMLInputElement).checked).toBe(false);
      expect((toggle("设备连接通知").element as HTMLInputElement).checked).toBe(false);
      await toggle("配对请求通知").setValue(true);
      await toggle("设备连接通知").setValue(true);
      // Turning the master off greys the per-type choices without erasing them —
      // the host keeps honouring those flags, so re-enabling has to restore the
      // user's earlier answer rather than resetting it to on.
      await toggle("启用通知").setValue(false);
      expect(toggle("配对请求通知").attributes("disabled")).toBeDefined();
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        notifications_enabled: false, notify_transfer: true,
        notify_pairing: true, notify_device_connect: true,
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });
  it("shows default filtering categories but preserves an explicitly disabled selection", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { filter_enabled_categories: null } });
    vi.mocked(bridge.updateSettings).mockClear().mockResolvedValue({});
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const filters = app.findAll("fieldset").find(field => field.text().includes("敏感内容过滤"))!;
      const inputs = filters.findAll("input");
      expect(inputs).toHaveLength(6);
      expect(inputs.filter(input => (input.element as HTMLInputElement).checked)).toHaveLength(5);
      expect((filters.get('input[value="email"]').element as HTMLInputElement).checked).toBe(false);
      for (const input of inputs) await input.setValue(false);
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        filter_enabled_categories: [],
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("restores a backup from the list without a file dialog", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.listBackups).mockResolvedValue({ backups: [
      { path: "C:/data/backups/clipsync-20260912.zip", filename: "clipsync-20260912.zip", date: "2026-09-12 09:30:00", size: 2048 },
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="刷新备份"]').trigger("click");
      await flushPromises();
      // Age and size come back with the row, so the list says which backup is
      // which — the legacy panel printed the byte count raw.
      expect(app.get(".backup-list").text()).toContain("2026-09-12 09:30:00");
      expect(app.get(".backup-list").text()).toContain("2.0 KB");
      // A row's button says which backup it restores, so six rows are not six
      // buttons with the same name to a screen reader.
      expect(app.get(".backup-list button").attributes("aria-label"))
        .toBe(t("恢复备份 {name}", { name: "clipsync-20260912.zip" }));
      await app.get(".backup-list button").trigger("click");
      await flushPromises();
      // The listed path goes to the same confirmation the picker fills in: a
      // reader looking at the row should not have to find the file again.
      expect(app.get('[aria-labelledby="restore-title"]').text()).toContain("clipsync-20260912.zip");
      expect(bridge.chooseFile).not.toHaveBeenCalled();
    } finally {
      app.unmount();
      vi.mocked(bridge.listBackups).mockResolvedValue({ backups: [] });
    }
  });

  it("opens one settings card at a time from the rail", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const railButton = (label: string) =>
        app.findAll(".settings-nav button").find(button => button.text() === label)!;
      // `v-show` hides with an inline display, and that inline style is what
      // this reads: VTU's `isVisible()` answers from the computed style, and in
      // jsdom that comes from the default stylesheet rather than the inline one
      // for a tree that was never attached to the document.
      const onScreen = (id: string) =>
        (app.get(`#settings-${id}`).element as HTMLElement).style.display !== "none";
      // One card is the page.  The other ten are still in the document —
      // v-show, not v-if — so a control keeps whatever state the reader left on
      // it, but none of them is on screen.
      expect(onScreen("general")).toBe(true);
      expect(onScreen("sync")).toBe(false);
      expect(railButton("常规").attributes("aria-current")).toBe("true");
      // Picking a card shows it and puts the one before it away.
      await railButton("同步").trigger("click");
      await flushPromises();
      expect(onScreen("sync")).toBe(true);
      expect(onScreen("general")).toBe(false);
      expect(railButton("同步").attributes("aria-current")).toBe("true");
      expect(railButton("常规").attributes("aria-current")).toBeUndefined();
      // And back, from a card three groups away.
      await railButton("诊断与维护").trigger("click");
      await flushPromises();
      expect(onScreen("diagnostics")).toBe(true);
      expect(onScreen("sync")).toBe(false);
    } finally { app.unmount(); }
  });

  it("keeps the phone out of the settings page and its one setting with the history", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // The card that held the service's controls left this page for the
      // devices page; what remained was a card headed with a phone, holding one
      // number and a note about where everything else had gone.  It is gone, and
      // so is its entry in the rail.
      expect(app.find("#settings-companion").exists()).toBe(false);
      expect(app.findAll(".settings-nav button").map(button => button.text()))
        .not.toContain("手机 Companion");
      // What the card held is a bound on the history, and it sits with the two
      // bounds on the history in the card whose subject it shares rather than in
      // a card of its own.
      const limit = app.get('[aria-label="显示历史条数"]');
      expect((limit.element as HTMLElement).closest("section.settings-section")!.id)
        .toBe("settings-history");
      // And it is on this page for a reason that a move would have broken: it
      // rides this page's own save.  That is the claim, so it is the assertion.
      await limit.setValue("42");
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(
        expect.objectContaining({ web_history_limit: 42 }));
    } finally { app.unmount(); }
  });

  it("groups the rail so that no group holds a single card", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // A group is a question the reader arrives with, and a group with one
      // entry is a label with nothing to aim at.  The card that had to move is
      // the translation one: it is a service this machine calls rather than one
      // it hosts, so it belongs under what this machine talks to -- and the
      // group it left was the one the phone service emptied.
      //
      // The label is the group tab's own text now, and the cards are the run
      // under it: the two rows are the two halves of the question above.
      const groups = app.findAll(".settings-nav-groups button")
        .map(tab => tab.text());
      expect(groups).toEqual(["通用", "连接", "数据", "系统"]);
      const sections = app.findAll(".settings-nav-group")
        .map(run => run.findAll("button").map(button => button.text()));
      expect(sections.every(items => items.length > 1)).toBe(true);
      // The runs are in the order of the tabs above them, so the second one is
      // 连接's — the group 翻译 moved to.
      expect(sections[1]).toContain("翻译");
      // One run of cards is on screen at a time, and it is the open group's.
      // The others stay in the tree so the search can still count them, which
      // is why this reads the inline display rather than the number of runs.
      const shown = app.findAll(".settings-nav-group")
        .filter(run => (run.element as HTMLElement).style.display !== "none");
      expect(shown).toHaveLength(1);
      expect(shown[0].findAll("button").map(button => button.text())).toContain("常规");
    } finally { app.unmount(); }
  });

  it("browses the settings in two steps: a group, then a card inside it", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const tabs = () => app.findAll(".settings-nav-groups button");
      const tab = (label: string) =>
        tabs().find(node => node.text().replace(/ · \d+$/, "") === label)!;
      const activeTab = () => tabs().find(node => node.classes().includes("active"))!;
      const runs = () => app.findAll(".settings-nav-group");
      // `v-show` on the runs, read the way the cards' own visibility is.
      const shownRun = () =>
        runs().find(run => (run.element as HTMLElement).style.display !== "none")!;
      const cardOnScreen = (id: string) =>
        (app.get(`#settings-${id}`).element as HTMLElement).style.display !== "none";

      // The page opens in 通用 with that group's own cards under the tab: the
      // reader's first step is already made, and the row of cards says which
      // group it belongs to by sitting under it.
      expect(activeTab().text()).toBe("通用");
      expect(shownRun().findAll("button").map(button => button.text()))
        .toEqual(["常规", "剪贴板历史", "通知"]);

      // A group is a step on the way to a card rather than a card of its own,
      // so it lands on one: a reader who opens 连接 is asking for the settings
      // about connecting, and a row of names under a card from another group
      // would leave the two rows disagreeing about what is open.
      await tab("连接").trigger("click");
      await flushPromises();
      expect(cardOnScreen("sync")).toBe(true);
      expect(cardOnScreen("general")).toBe(false);
      expect(activeTab().text()).toBe("连接");
      expect(shownRun().findAll("button").map(button => button.text()))
        .toEqual(["同步", "局域网发现", "网络与高级", "翻译"]);

      // A card inside the open group is opened by its own tab, and the group
      // stays where it was: it was already the open one.
      await shownRun().findAll("button").find(button => button.text() === "翻译")!
        .trigger("click");
      await flushPromises();
      expect(cardOnScreen("translation")).toBe(true);
      expect(activeTab().text()).toBe("连接");

      // Clicking the group that is already open leaves the reader alone.  Its
      // cards are on screen already, and moving them to 同步 would be the tab
      // taking them somewhere they did not ask to go.
      await tab("连接").trigger("click");
      await flushPromises();
      expect(cardOnScreen("translation")).toBe(true);
      expect(cardOnScreen("sync")).toBe(false);
      expect(activeTab().text()).toBe("连接");
    } finally { app.unmount(); }
  });

  it("searches the settings page, showing the cards that hold the query", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const search = app.get('[aria-label="搜索设置…"]');
      const railButton = (label: string) =>
        app.findAll(".settings-nav button").find(button => button.text().startsWith(label))!;
      const onScreen = (id: string) =>
        (app.get(`#settings-${id}`).element as HTMLElement).style.display !== "none";
      // Nothing is claimed before anything is typed, and the page is one card.
      expect(app.find(".settings-search-status").exists()).toBe(false);
      expect(app.findAll(".settings-nav button.has-match").length).toBe(0);
      expect(app.findAll(".settings-nav button.dimmed").length).toBe(0);
      expect(onScreen("advanced")).toBe(false);
      // One card holds this, in one place: the row's own label.
      await search.setValue("低内存模式");
      await flushPromises();
      expect(app.text()).toContain("1 个分区匹配“低内存模式”");
      expect(railButton("网络与高级").text()).toContain("· 1");
      expect(app.findAll(".settings-hit").map(hit => hit.text()))
        .toEqual(["低内存模式（更少预览、更慢轮询）"]);
      // The page becomes the cards that hold the query, and only those: a match
      // the reader cannot see is a match they cannot act on.  The rail still
      // counts and dims the rest, so nothing is lost from the rail's own list,
      // and the entry for a match opens it like any other.
      expect(onScreen("advanced")).toBe(true);
      expect(onScreen("general")).toBe(false);
      // Counted per row.  Every card but the one holding the query is dimmed,
      // and the groups are dimmed except the one holding it -- which is also
      // the group whose cards the strip is listing, so the count a reader can
      // act on is the one on the card, not the one on the tab.
      expect(app.findAll(".settings-nav-group button.dimmed").length).toBe(10);
      expect(app.findAll(".settings-nav-groups button.dimmed").length).toBe(3);
      expect(app.findAll(".settings-nav-groups button.has-match").length).toBe(1);
      expect(app.findAll(".settings-nav-group button.has-match").length).toBe(1);
      // Enter opens the first card that holds it, in the rail's own order, and
      // ends the search: the reader asked for a card, not for a result list.
      await search.trigger("keydown.enter");
      await flushPromises();
      expect((search.element as HTMLInputElement).value).toBe("");
      expect(app.find(".settings-search-status").exists()).toBe(false);
      expect(railButton("网络与高级").classes()).toContain("active");
      expect(onScreen("advanced")).toBe(true);
      expect(onScreen("general")).toBe(false);
      // A query nothing holds says so, and leaves the reader on that card
      // rather than on an empty page under a rail that already said so.
      await search.setValue("绝无此物");
      await flushPromises();
      expect(app.text()).toContain("没有匹配“绝无此物”的设置项");
      expect(app.findAll(".settings-nav button.has-match").length).toBe(0);
      expect(onScreen("advanced")).toBe(true);
      // Opening a card from the rail while a query is up ends the search too,
      // so a dimmed entry is still a control that does something.
      await search.setValue("低内存模式");
      await flushPromises();
      await railButton("常规").trigger("click");
      await flushPromises();
      expect((search.element as HTMLInputElement).value).toBe("");
      expect(onScreen("general")).toBe(true);
      expect(onScreen("advanced")).toBe(false);
      // And it can be dropped: Escape leaves the page as it was found.
      await search.trigger("keydown.esc");
      await flushPromises();
      expect((search.element as HTMLInputElement).value).toBe("");
      expect(app.find(".settings-search-status").exists()).toBe(false);
      expect(app.findAll(".settings-hit").length).toBe(0);
      expect(app.findAll(".settings-nav button.dimmed").length).toBe(0);
    } finally { app.unmount(); }
  });

  it("edits the relay lists as the multi-line lists they are", async () => {
    vi.mocked(bridge.settings).mockResolvedValue({ settings: {
      relay_brokers: ["wss://one:8884/mqtt", "wss://two:8884/mqtt"],
      relay_private_brokers: [],
    } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      // One address per line: the control has to be able to hold a line break,
      // which is the one thing the single-line input it used to be could not.
      const free = app.get('[aria-label="公共中继地址"]');
      expect(free.element.tagName).toBe("TEXTAREA");
      expect((free.element as HTMLTextAreaElement).value).toBe("wss://one:8884/mqtt\nwss://two:8884/mqtt");
      await free.setValue("wss://one:8884/mqtt\nwss://three:8884/mqtt\n");
      await app.findAll("button").find(button => button.text() === "保存设置")!.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({
        // The blank last line is dropped rather than saved as an empty broker.
        relay_brokers: ["wss://one:8884/mqtt", "wss://three:8884/mqtt"],
        relay_private_brokers: [],
      }));
    } finally {
      app.unmount();
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
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
  it("sends the encryption toggle alone and clears the password through its dialog", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { encryption_enabled: false, password_set: true },
    });
    vi.mocked(bridge.updateSettings).mockResolvedValue({ password_set: false });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="启用端到端加密"]').setValue(true);
      await app.findAll("button").find(button => button.text().includes("保存设置"))!.trigger("submit");
      await flushPromises();
      expect(vi.mocked(bridge.updateSettings).mock.calls.at(-1)![0]).toMatchObject({
        encryption_enabled: true,
      });
      await app.findAll("button").find(button => button.text().includes("清除加密密码"))!.trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="clear-password-title"]');
      await dialog.findAll("button").find(button => button.text() === "清除")!.trigger("click");
      await flushPromises();
      // An empty password means "unchanged", so clearing needs the action key.
      expect(bridge.updateSettings).toHaveBeenLastCalledWith({ password: "", clear_password: true });
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

  it("does not let a delayed startup settings read overwrite a newer theme", async () => {
    let resolve!: (value: any) => void;
    vi.mocked(bridge.settings)
      .mockReturnValueOnce(new Promise(done => { resolve = done; }))
      .mockResolvedValueOnce({ settings: { appearance_mode: "light" } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("light");
      resolve({ settings: { appearance_mode: "dark" } });
      await flushPromises();
      expect(document.documentElement.dataset.theme).toBe("light");
    } finally {
      app.unmount();
      delete document.documentElement.dataset.theme;
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

  it("opens the data and backups folders from the settings page", async () => {
    vi.mocked(bridge.openDataFolder).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "打开数据文件夹")!.trigger("click");
      await flushPromises();
      expect(bridge.openDataFolder).toHaveBeenLastCalledWith("data");
      await app.findAll("button").find(button => button.text() === "打开备份文件夹")!.trigger("click");
      await flushPromises();
      expect(bridge.openDataFolder).toHaveBeenLastCalledWith("backups");
    } finally { app.unmount(); }
  });

  it("ignores a stale running snapshot after Companion shutdown completes", async () => {
    const running = { enabled: true, running: true, port: 8080, state: "running", access_url: "old-access-url" };
    let resolve!: (value: typeof running) => void;
    vi.mocked(bridge.companionStatus)
      .mockResolvedValueOnce(running)
      .mockReturnValueOnce(new Promise(done => { resolve = done; }));
    vi.mocked(bridge.configureCompanion).mockResolvedValueOnce({
      ...running, enabled: false, running: false, state: "stopped", access_url: null,
    });
    const app = mount(App);
    try {
      await flushPromises();
      // The phone service's own controls are on the devices page: the button
      // that starts it is how a phone is added to this machine.
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      // Opening the page reads the service once; the read the button below asks
      // for is the one left in flight while the stop is asked for.
      await openPanel(app, "手机 Companion");
      await app.findAll("button").find(button => button.text() === "读取手机服务状态")!.trigger("click");
      await flushPromises();
      await app.findAll("button").find(button => button.text() === "停止服务")!.trigger("click");
      await flushPromises();
      resolve(running);
      await flushPromises();
      expect(app.find('[aria-label="手机访问地址"]').exists()).toBe(false);
      expect(app.findAll("button").find(button => button.text() === "停止服务")!.attributes("disabled")).toBeDefined();
    } finally { app.unmount(); }
  });

  it("requires confirmation to rotate the Companion token without applying an edited port", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const running = { enabled: true, running: true, port: 8080, state: "running", access_url: "old-url" };
    vi.mocked(bridge.companionStatus).mockResolvedValue(running);
    vi.mocked(bridge.configureCompanion).mockClear().mockResolvedValueOnce({ ...running, access_url: "new-url" });
    const app = mount(App);
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
      expect(bridge.configureCompanion).toHaveBeenCalledExactlyOnceWith(true, 8080, true);
      expect((app.get('[aria-label="手机访问地址"]').element as HTMLInputElement).value).toBe("new-url");
    } finally { app.unmount(); }
  });

  it("stops the actual Companion even with an invalid unsaved port and removes its access URL", async () => {
    vi.mocked(bridge.companionStatus).mockResolvedValue({
      enabled: true, running: true, port: 8080, state: "running",
      access_url: "http://127.0.0.1:8080/mobile.html?token=fixture",
    });
    vi.mocked(bridge.configureCompanion).mockClear().mockResolvedValueOnce({
      enabled: false, running: false, port: 8080, state: "stopped", access_url: null,
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openPanel(app, "手机 Companion");
      await app.findAll("button").find(button => button.text() === "读取手机服务状态")!.trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="手机访问地址"]').exists()).toBe(true);
      await app.get('[aria-label="手机服务端口"]').setValue(0);
      expect(app.findAll("button").find(button => button.text() === "启动 / 应用端口")!.attributes("disabled")).toBeDefined();
      await app.findAll("button").find(button => button.text() === "停止服务")!.trigger("click");
      await flushPromises();
      expect(bridge.configureCompanion).toHaveBeenCalledExactlyOnceWith(false, 8080, false);
      expect(app.find('[aria-label="手机访问地址"]').exists()).toBe(false);
      expect((app.get('[aria-label="手机服务端口"]').element as HTMLInputElement).value).toBe("8080");
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

  it("reads the AI tools when the AI page opens, and retries a read that failed", async () => {
    vi.mocked(bridge.aiProfiles).mockClear()
      .mockRejectedValueOnce(new Error("temporary profile read failure"))
      .mockResolvedValueOnce({
        tools: [{ key: "claude", label: "Claude" }],
        enabled: ["claude"],
        custom_paths: ["restored-path"],
      });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      expect(bridge.aiProfiles).toHaveBeenCalledTimes(1);
      const save = () => app.findAll("button").find(button => button.text().includes("保存 AI 配置"))!;
      // No tools were read and nothing was stored by this page, so the button is
      // off: saving here would write an empty list over the stored one.
      expect(save().attributes("disabled")).toBeDefined();
      // Walking away and back is what retries it — the flag is set on success
      // only — and what arrives is what the sidecar has.
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      expect(bridge.aiProfiles).toHaveBeenCalledTimes(2);
      expect(save().attributes("disabled")).toBeUndefined();
      expect(app.findAll("textarea").some(field =>
        (field.element as HTMLTextAreaElement).value === "restored-path")).toBe(true);
      expect(app.text()).toContain("所有更改都已保存");
      // A page that read successfully is not read again, for the reason the
      // settings form is not: the fields are the reader's while it is open.
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      expect(bridge.aiProfiles).toHaveBeenCalledTimes(2);
    } finally {
      app.unmount();
      // The queued answers are spent by the two reads above; the shape the rest
      // of the file expects is put back either way.
      vi.mocked(bridge.aiProfiles).mockResolvedValue({ tools: [], enabled: [], custom_paths: [] });
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

  it("preserves an AI draft until switching files is confirmed", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiLocal).mockClear()
      .mockResolvedValueOnce({ entries: ["a.md", "b.md"].map(rel_path => ({
        tool: "custom", root: "", rel_path,
      })) })
      .mockResolvedValueOnce({ content: "original" })
      .mockResolvedValueOnce({ content: "second file" });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 a.md"]').trigger("click");
      await flushPromises();
      await app.get('[aria-label="AI 配置编辑器"]').setValue("unsaved draft");
      await app.get('[aria-label="编辑 b.md"]').trigger("click");
      await flushPromises();
      expect(bridge.aiLocal).toHaveBeenCalledTimes(2);
      await app.get('[aria-labelledby="ai-discard-title"] button[autofocus]').trigger("click");
      expect((app.get('[aria-label="AI 配置编辑器"]').element as HTMLTextAreaElement).value).toBe("unsaved draft");
      await app.get('[aria-label="编辑 b.md"]').trigger("click");
      await flushPromises();
      await app.get('[aria-labelledby="ai-discard-title"] .danger').trigger("click");
      await flushPromises();
      expect((app.get('[aria-label="AI 配置编辑器"]').element as HTMLTextAreaElement).value).toBe("second file");
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

  it("labels each remote row with how it differs from this machine, and reads the local index to do it", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // The local walk is what the badges compare against, and the reader must not
    // have to find the other button first for them to be honest.
    vi.mocked(bridge.aiLocal).mockClear().mockResolvedValueOnce({ entries: [
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "here.md", sha256: "bbbb", mtime: 200, is_dir: false },
      { tool: "custom", root: "", rel_path: "there.md", sha256: "cccc", mtime: 100, is_dir: false },
    ] });
    // Reset rather than clear, and one answer for both reads: the settings page
    // primes the picker before the reader asks for this peer itself.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "here.md", sha256: "zzzz", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "there.md", sha256: "zzzz", mtime: 200, is_dir: false },
      { tool: "custom", root: "", rel_path: "absent.md", sha256: "zzzz", mtime: 100, is_dir: false },
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
      expect(bridge.aiLocal).toHaveBeenCalledWith("listing");
      // Scoped to the remote half: both lists share `.ai-local-list`, and the
      // reader's own files must not be able to answer for the peer's.
      const row = (path: string) => app.findAll(".ai-remote .ai-local-list li").find(li => li.text().includes(path))!;
      expect(row("absent.md").text()).toContain("缺失");
      expect(row("here.md").text()).toContain("本机较新");
      expect(row("there.md").text()).toContain("对方较新");
      // A file that matches carries no badge at all — a badge on every row would
      // make the differing ones harder to find.
      expect(row("same.md").find(".ai-version").exists()).toBe(false);
      expect(app.text()).toContain("与对方不同：缺失 1、对方较新 1、本机较新 1");
      // One click on the missing ones only: a file present on both sides may be
      // newer here, so ticking it for the reader could overwrite their own work.
      await app.findAll("button").find(button => button.text().includes("选择缺失项"))!.trigger("click");
      expect(app.findAll("button").find(button => button.text().includes("拉取选中项"))!.text()).toContain("（1）");
      expect(app.get('[aria-label="选择 absent.md"]').attributes("checked")).toBeDefined();
      expect(app.get('[aria-label="选择 there.md"]').attributes("checked")).toBeUndefined();
    } finally { app.unmount(); }
  });

  it("ticks a folder as the files under it, and leaves a sibling root's folder alone", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 2 });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiLocal).mockClear().mockResolvedValueOnce({ entries: [] });
    // `foo` under two roots, and a `foobar` that must not be swept into either:
    // a folder belongs to exactly one root, and a prefix match that ignores the
    // separator would take `foobar/` for a child of `foo`.
    // Reset rather than clear, and one answer for both reads: the settings page
    // primes the picker before the reader asks for this peer itself.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "claude", root: "skills", rel_path: "foo", is_dir: true },
      { tool: "claude", root: "skills", rel_path: "foo/one.md", sha256: "a", mtime: 1, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "foo/two.md", sha256: "b", mtime: 1, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "foobar/other.md", sha256: "c", mtime: 1, is_dir: false },
      { tool: "claude", root: "commands", rel_path: "foo", is_dir: true },
      { tool: "claude", root: "commands", rel_path: "foo/three.md", sha256: "d", mtime: 1, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "empty", is_dir: true },
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
      const boxes = () => app.findAll('[aria-label="选择文件夹 foo 下的全部文件"]').map(box => box.element as HTMLInputElement);
      // A folder holding nothing offers no box at all — there is nothing to
      // shortcut to — but still marks the column.
      expect(app.findAll('[aria-label="选择文件夹 empty 下的全部文件"]').length).toBe(0);
      expect(boxes().length).toBe(2);
      // Every folder starts folded, so the files the ticks stand for are opened
      // first: the three folders that hold something, leaving `empty` out.
      for (const name of ["foo", "foo", "foobar"]) {
        const chevrons = app.findAll('[aria-label="展开或折叠 foo"], [aria-label="展开或折叠 foobar"]');
        await chevrons.find(box => box.attributes("aria-expanded") === "false")!.trigger("click");
      }
      expect(app.find('[aria-label="选择 foo/one.md"]').exists()).toBe(true);
      expect(app.find('[aria-label="选择 foobar/other.md"]').exists()).toBe(true);
      expect(boxes()[0].checked).toBe(false);
      await app.get('[aria-label="选择文件夹 foo 下的全部文件"]').setValue(true);
      const skillsBox = boxes()[0];
      expect(skillsBox.checked).toBe(true);
      expect(app.get('[aria-label="选择 foo/one.md"]').attributes("checked")).toBeDefined();
      expect(app.get('[aria-label="选择 foo/two.md"]').attributes("checked")).toBeDefined();
      // `foobar/other.md` is not under `foo`, and the other root's `foo` is a
      // different folder: neither may be swept in by the one tick.
      expect(app.get('[aria-label="选择 foobar/other.md"]').attributes("checked")).toBeUndefined();
      expect(app.get('[aria-label="选择 foo/three.md"]').attributes("checked")).toBeUndefined();
      // One file unticked afterwards leaves the folder reading "some, not all".
      await app.get('[aria-label="选择 foo/one.md"]').setValue(false);
      expect(skillsBox.checked).toBe(false);
      expect(skillsBox.indeterminate).toBe(true);
      // What is pulled is the files, not the folder row: the same keys the tick
      // stands for.
      await app.findAll("button").find(button => button.text().includes("拉取选中项"))!.trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "claude", root: "skills", rel_path: "foo/two.md", is_dir: false },
      ], "copy");
      // The two folders share a name, so each row says which root it is under —
      // `skills/foo` and `commands/foo` are different folders.
      const fooRows = () => app.findAll(".ai-remote .ai-local-list li.ai-row")
        .filter(li => li.find(".ai-path-btn").exists() && li.find(".ai-path-btn").text() === "📁 foo");
      expect(fooRows().map(li => li.find(".ai-root-hint").text())).toEqual(["skills", "commands"]);
    } finally { app.unmount(); }
  });

  it("draws each tool's config as a named tree, and folds a folder without losing its row", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // The inventory is flat — the folders exist only as path segments — so the
    // tree is the renderer's, built from where each file lives.
    vi.mocked(bridge.aiLocal).mockClear().mockResolvedValueOnce({ entries: [
      { tool: "claude", root: "skills", rel_path: "deep/one.md", is_dir: false },
      { tool: "claude", root: "skills", rel_path: "top.md", is_dir: false },
    ] });
    vi.mocked(bridge.aiInventory).mockResolvedValueOnce({ peers: { a: { entries: [
      { tool: "claude", root: "skills", rel_path: "deep/two.md", is_dir: false },
    ] } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      const localRows = () => app.findAll(".ai-local-list.setting-block li.ai-row").map(li => li.text());
      const heads = () => app.findAll(".ai-local-list.setting-block li.ai-group-head").map(li => li.text());
      // A header per tool over its own rows, then the folder and the file beside
      // it.  The folder starts folded: the two items are the folder and the
      // file, and `one.md` is the folder's business until the reader opens it.
      expect(heads()).toEqual(["claude2"]);
      expect(localRows()).toEqual(["📁 deep", "top.md"]);
      const chevron = () => app.get('[aria-label="展开或折叠 deep"]');
      expect(chevron().attributes("aria-expanded")).toBe("false");
      // Opening it — by its chevron or by its own name, the two halves of the
      // same control — shows what is inside without taking the folder away.
      await chevron().trigger("click");
      expect(localRows()).toEqual(["📁 deep", "one.md", "top.md"]);
      expect(chevron().attributes("aria-expanded")).toBe("true");
      await app.findAll(".ai-local-list.setting-block li.ai-row .ai-path-btn")[0].trigger("click");
      expect(localRows()).toEqual(["📁 deep", "top.md"]);
      await app.findAll(".ai-local-list.setting-block li.ai-row .ai-path-btn")[0].trigger("click");
      expect(localRows()).toEqual(["📁 deep", "one.md", "top.md"]);
      // A folder the inventory never listed has no file to edit and nothing it
      // listed to move, so it carries neither control — but it is still a place
      // on disk, so it can be opened.
      const folderRow = () => app.findAll(".ai-local-list.setting-block li.ai-row")[0];
      expect(folderRow().find('[aria-label="打开 deep"]').exists()).toBe(true);
      // Its fold control and its name, then the one action it has: nothing to
      // open in the editor and nothing listed to move.
      expect(folderRow().findAll("button").length).toBe(3);
      expect(folderRow().findAll(".icon-button").length).toBe(1);
      expect(localRows()[1]).toBe("one.md");
      expect(app.findAll(".ai-local-list.setting-block li.ai-row")[1].findAll(".icon-button").length).toBe(3);
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

  it("counts a folder as one migration target, whichever strategy is chosen", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 2 });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // One skill folder holding three files, two of which this machine already
    // has — so the wizard has two things to migrate, not four: the skill, and
    // the one loose file beside it.
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [
      { tool: "claude", root: "skills", rel_path: "skill/keep.md", sha256: "aa", mtime: 100, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "skill/mine.md", sha256: "bb", mtime: 500, is_dir: false },
    ] });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "claude", root: "skills", rel_path: "skill/keep.md", sha256: "aa", mtime: 100, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "skill/mine.md", sha256: "cc", mtime: 100, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "skill/new.md", sha256: "dd", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "absent.md", sha256: "ee", mtime: 100, is_dir: false },
    ] } } });
    const app = mount(App);
    const open = () => app.findAll("button").find(button => button.text().includes("迁移向导"))!.trigger("click");
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await open();
      await app.get('[aria-label="迁移来源设备"]').setValue("a");
      await flushPromises();
      const migrate = () => app.findAll("button").find(button => button.text().includes("开始迁移"))!;
      // The skill counts once, and so does the loose file: the number beside the
      // button is the number of config items, the way the card counts them.
      expect(migrate().text()).toContain("（2）");
      expect(app.text()).toContain("将补齐本机缺少的 2 个配置项");
      await migrate().trigger("click");
      await flushPromises();
      // What travels is the files — the skill's missing one and the loose file —
      // because "this machine does not have it" is a fact about a file, and a
      // folder sent as a folder would be expanded into its identical files too.
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "claude", root: "skills", rel_path: "skill/new.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "absent.md", is_dir: false },
      ], "copy");
      // Overwriting takes the differing files, and counts and lists items: the
      // skill is one line carrying how many files are behind it.
      vi.mocked(bridge.aiPull).mockClear();
      await open();
      await app.get('[aria-label="全部拉取，覆盖本机文件"]').setValue(true);
      expect(migrate().text()).toContain("（2）");
      await migrate().trigger("click");
      await flushPromises();
      expect(app.text()).toContain("将写入以下 2 个配置项：");
      const confirmText = app.get('[aria-labelledby="ai-pull-title"]').text();
      expect(confirmText).toContain("claude / skill");
      expect(confirmText).toContain("（2 个文件）");
      expect(confirmText).toContain("custom / absent.md");
      await app.get('[aria-labelledby="ai-pull-title"] .danger').trigger("click");
      await flushPromises();
      expect(bridge.aiPull).toHaveBeenCalledExactlyOnceWith("a", [
        { tool: "claude", root: "skills", rel_path: "skill/mine.md", is_dir: false },
        { tool: "claude", root: "skills", rel_path: "skill/new.md", is_dir: false },
        { tool: "custom", root: "", rel_path: "absent.md", is_dir: false },
      ], "overwrite");
    } finally { app.unmount(); }
  });

  it("narrows either list by path without disturbing the ticks or the count", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiLocal).mockClear().mockResolvedValueOnce({ entries: [
      { tool: "claude", root: "skills", rel_path: "alpha.md", sha256: "aaaa", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "beta.md", sha256: "bbbb", mtime: 100, is_dir: false },
    ] });
    // Reset rather than clear, and the same answer either way: opening settings
    // primes the picker from the cache and the read then asks the peer itself, so
    // a single queued once-value would only answer the priming one.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "claude", root: "skills", rel_path: "alpha.md", sha256: "aaaa", mtime: 100, is_dir: false },
      { tool: "custom", root: "", rel_path: "beta.md", sha256: "bbbb", mtime: 100, is_dir: false },
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
      // Rows only: the list also carries a header per tool, which is not a row
      // and has no tick to hold.
      const remoteRows = () => app.findAll(".ai-remote .ai-local-list li.ai-row").map(li => li.text());
      const remoteHeads = () => app.findAll(".ai-remote .ai-local-list li.ai-group-head").map(li => li.text());
      expect(remoteRows().length).toBe(2);
      // One header per tool, named the way the settings checkboxes name it,
      // carrying that tool's file count.
      expect(remoteHeads()).toEqual(["claude1", "custom1"]);
      // Tick one row, then narrow past it: the tick is held on the entry, not on
      // the row's position, so filtering cannot move it to another file.
      await app.get('[aria-label="选择 beta.md"]').setValue(true);
      await app.get('[aria-label="搜索远程配置"]').setValue("alpha");
      expect(remoteRows().length).toBe(1);
      expect(remoteRows()[0]).toContain("alpha.md");
      await app.get('[aria-label="搜索远程配置"]').setValue("");
      expect(remoteRows().length).toBe(2);
      expect(app.get('[aria-label="选择 beta.md"]').attributes("checked")).toBeDefined();
      // A filter that matches nothing says so, rather than looking like an empty
      // inventory — and the batch count still describes what is ticked.  The
      // header goes with the rows: a tool with nothing left to show is not a
      // heading over an empty list.
      await app.get('[aria-label="搜索远程配置"]').setValue("nothing-matches-this");
      expect(remoteRows().length).toBe(0);
      expect(remoteHeads().length).toBe(0);
      expect(app.text()).toContain("没有匹配的配置项");
      expect(app.findAll("button").find(button => button.text().includes("拉取选中项"))!.text()).toContain("（1）");
      // The two lists are one view each: the local filter is reached by putting
      // the card back on this machine, and then it narrows that list alone.
      await app.get('[aria-label="AI 配置远程设备"]').setValue("");
      await openPanel(app, "本机配置");
      await app.get('[aria-label="搜索本机配置"]').setValue("alpha");
      expect(app.findAll(".ai-local-list li").filter(li => li.text().includes("beta.md")).length).toBe(0);
    } finally { app.unmount(); }
  });

  it("keeps this machine's list and a chosen device's apart, and says when a device has not been read", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [
      { tool: "claude", root: "skills", rel_path: "mine.md" },
    ] });
    // Opening the page primes the picker, and the cache it finds holds nothing
    // for this device: a peer nobody has asked is not a peer with no files.
    vi.mocked(bridge.aiInventory).mockReset()
      .mockResolvedValueOnce({ peers: {} })
      .mockResolvedValueOnce({ peers: { a: { entries: [] } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      // This machine's list is a card of its own, and how a pull writes is the
      // peer's business: it stays in the peer's card, which has not been given
      // a device to be about yet.
      expect(app.find('[aria-label="编辑 mine.md"]').exists()).toBe(true);
      await openPanel(app, "其他设备");
      expect(app.find('[aria-label="AI 配置拉取方式"]').exists()).toBe(false);
      // Naming a device adds its card's rows without taking this machine's
      // away — the two lists are two cards, not one card showing one of them
      // at a time — and that device has not been read yet, which is not the
      // same thing as a device with no config files.
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await flushPromises();
      await openPanel(app, "本机配置");
      expect(app.find('[aria-label="编辑 mine.md"]').exists()).toBe(true);
      await openPanel(app, "其他设备");
      expect(app.find('[aria-label="AI 配置拉取方式"]').exists()).toBe(true);
      expect(app.text()).toContain("尚未读取该设备的配置");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      expect(app.text()).toContain("该设备还没有可同步的配置项");
      expect(app.text()).not.toContain("尚未读取该设备的配置");
    } finally {
      app.unmount();
      // The listing is left standing for the whole file otherwise, and the cases
      // after this one are about peers the reader has never listed.
      vi.mocked(bridge.aiLocal).mockReset();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("marks each device with how many files differ, and keeps what each device last said", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
      { id: "b", name: "Second", paired: true } as any,
    ] });
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
    ] });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: {
      a: { entries: [
        { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 100, is_dir: false },
        { tool: "custom", root: "", rel_path: "absent.md", sha256: "bbbb", mtime: 100, is_dir: false },
      ] },
      b: { entries: [
        { tool: "custom", root: "", rel_path: "same.md", sha256: "zzzz", mtime: 300, is_dir: false },
      ] },
    } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      const options = () => app.findAll('[aria-label="AI 配置远程设备"] option').map(option => option.text());
      // Neither side has been read, so the picker claims nothing about either
      // device: a count of zero here would read as "identical", which nobody
      // has established.
      expect(options()).toEqual(["请选择设备", "First", "Second"]);
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      // One read answers for every device, not just the one that was asked: a
      // file this machine lacks on First, one that is newer there on Second.
      expect(options()).toEqual(["请选择设备", "First（1 项不同）", "Second（1 项不同）"]);
      // Second's list arrived with that read, so looking at it costs no request
      // — what the reader sees is what that device last told this machine.
      vi.mocked(bridge.aiInventory).mockClear();
      await app.get('[aria-label="AI 配置远程设备"]').setValue("b");
      await flushPromises();
      expect(bridge.aiInventory).not.toHaveBeenCalled();
      const rows = () => app.findAll(".ai-remote .ai-local-list li.ai-row").map(li => li.text());
      expect(rows()).toEqual(["same.md对方较新"]);
    } finally {
      app.unmount();
      vi.mocked(bridge.aiLocal).mockReset();
      vi.mocked(bridge.aiInventory).mockReset();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
  });

  it("badges a folded folder with what its whole subtree holds, not just the rows on screen", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    // One skill folder holding a file this machine is missing, a file the peer's
    // copy is older than ours on, and one both sides agree about.
    const local = [
      { tool: "claude", root: "skills", rel_path: "skill/keep.md", sha256: "aa", mtime: 100, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "skill/mine.md", sha256: "bb", mtime: 500, is_dir: false },
    ];
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: local });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      local[0],
      { tool: "claude", root: "skills", rel_path: "skill/mine.md", sha256: "cc", mtime: 100, is_dir: false },
      { tool: "claude", root: "skills", rel_path: "skill/new.md", sha256: "dd", mtime: 100, is_dir: false },
    ] } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await flushPromises();
      // Reading the peer is what walks this machine's own list too, and the
      // badges are the comparison between the two: without it there is nothing
      // to compare against and every row would be silent.
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      // The folder is folded, so the files inside are not on screen at all: the
      // one row a reader can see has to carry what they would otherwise have to
      // open it to find out.
      const folder = app.findAll(".ai-remote .ai-local-list li.ai-row")
        .find(row => row.findAll(".ai-path-btn").some(button => button.text() === "📁 skill"));
      if (!folder) throw new Error("no skill folder row");
      expect(folder.findAll(".ai-version").map(node => node.text())).toEqual(["缺失1", "本机较新1"]);
      // Nothing is said about the file the two sides agree on: a badge on every
      // folder would make the differing ones harder to find, not easier.
      expect(folder.findAll(".ai-version--same")).toEqual([]);
      // Opening it moves the same facts onto the file rows, which is the other
      // half of the same rule.
      await folder.get(".ai-path-btn").trigger("click");
      await flushPromises();
      const rows = app.findAll(".ai-remote .ai-local-list li.ai-row").map(row => row.text());
      expect(rows).toContain("new.md缺失");
      expect(rows).toContain("mine.md本机较新");
    } finally {
      app.unmount();
      vi.mocked(bridge.aiLocal).mockReset();
      vi.mocked(bridge.aiInventory).mockReset();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
    }
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

  it("reports a pull while it runs and how it ended, rather than stopping at what it sent", async () => {
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback; return () => {};
    });
    vi.mocked(bridge.aiPull).mockClear().mockResolvedValue({ requested: 2 });
    vi.mocked(bridge.aiLocal).mockReset().mockResolvedValue({ entries: [] });
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({ peers: { a: { entries: [
      { tool: "custom", root: "", rel_path: "one.md", is_dir: false },
      { tool: "custom", root: "", rel_path: "two.md", is_dir: false },
    ] } } });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      await app.get('[aria-label="AI 配置远程设备"]').setValue("a");
      await flushPromises();
      await app.get('[aria-label="选择本页全部远程配置"]').setValue(true);
      await app.findAll("button").find(button => button.text().includes("拉取选中项"))!.trigger("click");
      await flushPromises();
      // The peer accepted two requests, so two files are on their way: the page
      // counts the ones that have landed rather than leaving the reader with the
      // count of requests it sent.
      const status = () => app.get(".ai-pull-status");
      expect(status().text()).toContain("正在接收 0 / 2 个文件");
      const arrival = (status: string) => emit({
        type: "event", session_id: "s", name: "aiconfig.file",
        data: { type: "aiconfig_file", peer_id: "a", status },
      } as any);
      arrival("ok");
      await flushPromises();
      expect(status().text()).toContain("正在接收 1 / 2 个文件");
      arrival("ok");
      await flushPromises();
      expect(status().text()).toContain("拉取完成：2 个文件已更新");
      expect(status().classes()).toContain("ai-pull-status--done");
      // A file that did not land is a different failure from a request the peer
      // refused, and the finished line names both halves rather than one total.
      await app.findAll("button").find(button => button.text().includes("拉取选中项"))!.trigger("click");
      await flushPromises();
      arrival("error");
      await flushPromises();
      arrival("ok");
      await flushPromises();
      expect(status().text()).toContain("拉取完成：1 个成功、1 个失败");
      expect(status().classes()).toContain("ai-pull-status--failed");
    } finally { app.unmount(); }
  });

  it.each(["preview", "pull"] as const)("ignores a delayed remote %s result after changing peers", async (operation) => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
      { id: "b", name: "Second", paired: true } as any,
    ] });
    // Reset rather than clear, and the same answer however often it is asked:
    // opening settings primes the picker from the cache before the reader reads
    // anything, and that priming call would eat a queued once-value.
    vi.mocked(bridge.aiInventory).mockReset().mockResolvedValue({
      peers: { a: { entries: [{ tool: "custom", root: "", rel_path: "remote.md" }] } },
    });
    let resolve!: (value: any) => void;
    const response = new Promise<any>(done => { resolve = done; });
    if (operation === "preview") vi.mocked(bridge.aiPreview).mockReturnValueOnce(response);
    else vi.mocked(bridge.aiPull).mockReturnValueOnce(response);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      const select = app.get('[aria-label="AI 配置远程设备"]');
      await select.setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await flushPromises();
      await app.get(`[aria-label="${operation === "preview" ? "预览" : "拉取"} remote.md"]`).trigger("click");
      await select.setValue("b");
      resolve(operation === "preview" ? { content: "obsolete preview" } : { requested: 1 });
      await flushPromises();
      expect(app.text()).not.toContain("obsolete preview");
      expect(app.text()).not.toContain("已发送 1 个拉取请求");
      expect(app.find('[aria-label="预览 remote.md"]').exists()).toBe(false);
    } finally { app.unmount(); }
  });

  it("does not show a previous peer's delayed inventory after switching devices", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
      { id: "b", name: "Second", paired: true } as any,
      { id: "c", name: "Untrusted", paired: false } as any,
    ] });
    let resolve!: (value: any) => void;
    // The priming read answers at once; the reader's own read is the one left
    // hanging, which is what switching devices then has to be safe against.
    vi.mocked(bridge.aiInventory).mockReset()
      .mockResolvedValueOnce({ peers: {} })
      .mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "其他设备");
      const select = app.get('[aria-label="AI 配置远程设备"]');
      expect(select.text()).not.toContain("Untrusted");
      await select.setValue("a");
      await app.findAll("button").find(button => button.text().includes("读取远程库存"))!.trigger("click");
      await select.setValue("b");
      resolve({ peers: { a: { entries: [{ tool: "custom", rel_path: "old-peer.md" }] } } });
      await flushPromises();
      expect(app.find('[aria-label="预览 old-peer.md"]').exists()).toBe(false);
    } finally { app.unmount(); }
  });

  it("only submits auto-start when the switch changes", async () => {
    vi.mocked(bridge.updateSettings).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const save = app.get('button[type="submit"]');
      await save.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledTimes(1);
      expect(vi.mocked(bridge.updateSettings).mock.calls[0][0]).not.toHaveProperty("auto_start");
      const toggle = app.findAll("label").find(label => label.text().includes("开机自动启动"))!;
      await toggle.get("input").setValue(true);
      await save.trigger("submit");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenLastCalledWith(expect.objectContaining({ auto_start: true }));
      await save.trigger("submit");
      await flushPromises();
      expect(vi.mocked(bridge.updateSettings).mock.calls[2][0]).not.toHaveProperty("auto_start");
    } finally { app.unmount(); }
  });

  it("closes the deleted file editor even after listing replaces entry objects", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const entry = { tool: "custom", root: "fixture", rel_path: "config.md" };
    vi.mocked(bridge.aiLocal)
      .mockResolvedValueOnce({ entries: [{ ...entry }] })
      .mockResolvedValueOnce({ content: "config" })
      .mockResolvedValueOnce({ entries: [{ ...entry }] })
      .mockResolvedValueOnce({ ok: true })
      .mockResolvedValueOnce({ entries: [] });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      const refresh = () => app.findAll("button").find(button => button.text().includes("读取本机配置"))!;
      await refresh().trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 config.md"]').trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="AI 配置编辑器"]').exists()).toBe(true);
      await refresh().trigger("click");
      await flushPromises();
      await app.get('[aria-label="移入回收区 config.md"]').trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="AI 配置编辑器"]').exists()).toBe(true);
      expect(bridge.aiLocal).not.toHaveBeenCalledWith("trash", "custom", "fixture", "config.md");
      await app.get('[aria-labelledby="ai-trash-title"] .danger').trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="AI 配置编辑器"]').exists()).toBe(false);
    } finally { app.unmount(); }
  });

  it.each(["success", "failure"] as const)("ignores an older AI read %s after selecting another file", async (outcome) => {
    let resolveOld!: (value: any) => void;
    let rejectOld!: (error: Error) => void;
    const oldRead = new Promise<any>((resolve, reject) => {
      resolveOld = resolve;
      rejectOld = reject;
    });
    vi.mocked(bridge.aiLocal).mockResolvedValueOnce({
      entries: ["old.md", "new.md"].map(rel_path => ({ tool: "custom", root: "fixture", rel_path })),
    }).mockReturnValueOnce(oldRead).mockResolvedValueOnce({ content: "new content" });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      await app.get('[aria-label="编辑 old.md"]').trigger("click");
      await app.get('[aria-label="编辑 new.md"]').trigger("click");
      await flushPromises();
      if (outcome === "success") resolveOld({ content: "stale content" });
      else rejectOld(new Error("obsolete read failed"));
      await flushPromises();
      expect((app.get('[aria-label="AI 配置编辑器"]').element as HTMLTextAreaElement).value).toBe("new content");
      expect(app.text()).not.toContain("obsolete read failed");
    } finally { app.unmount(); }
  });

  it("pages through all local AI files and resets pagination after refresh", async () => {
    vi.mocked(bridge.aiLocal).mockResolvedValue({
      entries: Array.from({ length: 41 }, (_, index) => ({
        tool: "custom", root: "fixture", rel_path: `file-${index + 1}.md`,
      })),
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      const refresh = () => app.findAll("button").find(button => button.text().includes("读取本机配置"))!;
      await refresh().trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="编辑 file-40.md"]').exists()).toBe(true);
      expect(app.find('[aria-label="编辑 file-41.md"]').exists()).toBe(false);
      await app.get('[aria-label="下一页本地配置"]').trigger("click");
      expect(app.find('[aria-label="编辑 file-41.md"]').exists()).toBe(true);
      expect(app.get('[aria-label="下一页本地配置"]').attributes("disabled")).toBeDefined();
      await refresh().trigger("click");
      await flushPromises();
      expect(app.find('[aria-label="编辑 file-1.md"]').exists()).toBe(true);
      expect(app.get('[aria-label="上一页本地配置"]').attributes("disabled")).toBeDefined();
    } finally { app.unmount(); }
  });

  it("counts a skill folder as one config item, not as the files inside it", async () => {
    vi.mocked(bridge.aiLocal).mockResolvedValueOnce({
      entries: [
        { tool: "claude_code", root: "settings", rel_path: "settings.json", is_dir: false },
        { tool: "claude_code", root: "skills", rel_path: "obe-softeng-report/", is_dir: true },
        ...Array.from({ length: 20 }, (_, index) => ({
          tool: "claude_code", root: "skills", rel_path: `obe-softeng-report/paper-${index}.docx`,
        })),
        { tool: "codex", root: "config", rel_path: "config.toml", is_dir: false },
      ],
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="AI 配置"]').trigger("click");
      await flushPromises();
      await openPanel(app, "本机配置");
      await app.findAll("button").find(button => button.text().includes("读取本机配置"))!.trigger("click");
      await flushPromises();
      // Twenty-three entries, three config items: the file, the skill, and the
      // other tool's file.  The skill's own files are counted once with the
      // folder rather than twenty times beside it.
      expect(app.text()).toContain("已读取 3 个配置项");
      const count = (tool: string) =>
        app.findAll(".ai-local-list .ai-group-head")
          .find(head => head.text().startsWith(tool))!.get(".ai-group-count").text();
      expect(count("claude_code")).toBe("2");
      expect(count("codex")).toBe("1");
      // The rows themselves are unchanged: the folder is still there to open, and
      // its files are still under it.  Only the count changed.
      expect(app.find('[aria-label="展开或折叠 obe-softeng-report"]').exists()).toBe(true);
      const folder = () => app.get('[aria-label="展开或折叠 obe-softeng-report"]');
      expect(folder().attributes("aria-expanded")).toBe("false");
      expect(app.find('[aria-label="打开 obe-softeng-report/paper-0.docx"]').exists()).toBe(false);
      await folder().trigger("click");
      expect(app.find('[aria-label="打开 obe-softeng-report/paper-0.docx"]').exists()).toBe(true);
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
    const app = mount(App);
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
  it("reads a clip's own text back to translate it, not the row's preview", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.readHistoryText).mockResolvedValue({
      id: "1", text: "一整段很长的原文", truncated: false,
    });
    vi.mocked(bridge.translate).mockResolvedValue({ translated: "a whole long source text" } as any);
    const app = mount(App);
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
    const app = mount(App);
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
  it("names a clip's kind, its provenance and its paste count the way the panel did", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 2, items: [
      historyRow({
        id: "1", content_type: "IMAGE_PNG", source_app: "chrome", source_title: "Inbox",
        source_name: "Studio", paste_count: 3,
      }),
      // A clip captured here, never pasted back: the badges a user would not
      // learn anything from stay off.
      historyRow({ id: "2", preview: "just a note" }),
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      const rows = app.findAll(".history-row");
      // The wire name is not a label: IMAGE_PNG reads as 图片, not as its type.
      expect(rows[0].text()).toContain(t("图片"));
      expect(rows[0].find(".history-meta").text()).toContain("chrome");
      expect(rows[0].find(".history-meta").text()).toContain("Inbox");
      expect(rows[0].find(".history-meta").text()).toContain("Studio");
      expect(rows[0].find(".history-meta").text()).toContain(t("{count} 次粘贴", { count: 3 }));
      expect(rows[1].text()).toContain(t("文本"));
      expect(rows[1].find(".badge").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("says which link a record came in on, beside the device it came from", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 5, items: [
      historyRow({ id: "1", source_name: "Away", transport: "relay" }),
      // The same words the device list uses for the same link, so one
      // vocabulary covers both pages.
      historyRow({ id: "2", source_name: "Next room", transport: "lan" }),
      // Captured here, and a row written before the route was recorded: both
      // say nothing rather than claiming a path.
      historyRow({ id: "3", source_name: "Local" }),
      historyRow({ id: "4", source_name: "Local", transport: "" }),
      // A push from the panel is neither of the two links: it came in over this
      // machine's own web server, and its name says only "Web" — reading that
      // as 本地 would claim the phone was on this network when the panel's whole
      // point is that it need not be.
      historyRow({ id: "5", source_name: "📱 Web", transport: "web" }),
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      const routes = app.findAll(".history-row .history-route").map(node => node.text());
      expect(routes).toEqual([t("互联网"), t("本地"), t("网页")]);
      const rows = app.findAll(".history-row");
      expect(rows[0].find(".history-meta").text()).toContain("Away");
      // The name alone cannot say it — "Away" over the relay and "Next room"
      // over the cable are the same badge without the chip beside it.
      expect(rows[1].find(".history-meta").text()).toContain("Next room");
      expect(rows[2].find(".history-route").exists()).toBe(false);
      expect(rows[3].find(".history-route").exists()).toBe(false);
      expect(rows[0].get(".history-route").attributes("title")).toBe(t("互联网中继"));
      expect(rows[1].get(".history-route").attributes("title")).toBe(t("本地连接"));
      expect(rows[4].find(".history-meta").text()).toContain("📱 Web");
      expect(rows[4].get(".history-route").attributes("title")).toBe(t("网页推送"));
      // One glyph per route, so a glance separates them before the words are
      // read — a plug on a pushed row would say the local link.
      expect(rows[0].get(".history-route svg").classes()).toContain("lucide-globe");
      expect(rows[1].get(".history-route svg").classes()).toContain("lucide-plug");
      expect(rows[4].get(".history-route svg").classes()).toContain("lucide-smartphone");
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("spells a single paste count as one, which English needs", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({ session_id: "s", seq: 0, offset: 0, total: 1, items: [
      historyRow({ paste_count: 1 }),
    ] });
    const app = mount(App);
    try {
      await flushPromises();
      expect(app.find(".history-meta").text()).toContain(t("1 次粘贴"));
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("filters the list by kind, with a badge on every chip", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({
      ...historyPage([historyRow({ id: "1" })]),
      counts: { all: 9, text: 5, image: 3, file: 1, link: 0 },
      has_history: true,
    });
    const app = mount(App);
    try {
      await flushPromises();
      const chips = app.findAll(".chip");
      // Five kinds and the sort toggle, in the panel's order.
      expect(chips.map((chip) => chip.text().replace(/[📝🖼📄🔗]/g, ""))).toEqual([
        t("全部") + "9", t("文本") + "5", t("图片") + "3", t("文件") + "1", t("链接") + "0",
        "↓ " + t("最新优先"),
      ]);
      // A kind with nothing behind it is disabled — the way back to 全部 is not.
      expect(chips[4].attributes("disabled")).toBeDefined();
      expect(chips[0].attributes("disabled")).toBeUndefined();
      await chips[2].trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.history)).toHaveBeenLastCalledWith("", 0, 30, "image", "newest");
      // The chip in effect says so, and 全部 stops saying it does.
      expect(app.findAll(".chip")[2].attributes("aria-pressed")).toBe("true");
      expect(app.findAll(".chip")[0].attributes("aria-pressed")).toBe("false");
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("reverses the order from the sort toggle and says which order it is in", async () => {
    vi.clearAllMocks();
    const app = mount(App);
    try {
      await flushPromises();
      const sort = app.get(".chip--sort");
      expect(sort.text()).toContain(t("最新优先"));
      await sort.trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.history)).toHaveBeenLastCalledWith("", 0, 30, "all", "oldest");
      expect(sort.text()).toContain(t("最旧优先"));
      await sort.trigger("click");
      await flushPromises();
      expect(vi.mocked(bridge.history)).toHaveBeenLastCalledWith("", 0, 30, "all", "newest");
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("keeps the chips on screen when a search matches nothing", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history).mockResolvedValue({
      ...historyPage([]),
      counts: { all: 0, text: 0, image: 0, file: 0, link: 0 },
      has_history: true,
    });
    const app = mount(App);
    try {
      await flushPromises();
      expect(app.findAll(".chip").length).toBe(6);
      await app.get(`[aria-label="${t("搜索历史记录")}"]`).setValue("zzz");
      await flushPromises();
      expect(app.text()).toContain(t("没有匹配的记录"));
      expect(app.findAll(".chip").length).toBe(6);
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("names the kind a chip filtered down to, rather than saying there is no history", async () => {
    vi.clearAllMocks();
    vi.mocked(bridge.history)
      .mockResolvedValueOnce({
        ...historyPage([historyRow({ id: "1", content_type: "IMAGE_PNG" })]),
        counts: { all: 4, text: 3, image: 1, file: 0, link: 0 },
        has_history: true,
      })
      // The picture is gone by the time the chip's own page comes back — other
      // kinds are still there, so there IS history; there is just none of this.
      .mockResolvedValueOnce({
        ...historyPage([]),
        counts: { all: 3, text: 3, image: 0, file: 0, link: 0 },
        has_history: true,
      });
    const app = mount(App);
    try {
      await flushPromises();
      await app.findAll(".chip")[2].trigger("click");
      await flushPromises();
      expect(app.text()).toContain(t("还没有{type}", { type: t("图片") }));
      expect(app.text()).not.toContain(t("暂无历史记录"));
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockReset();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("reads a clipped row in full on hover, and only the hovered row", async () => {
    vi.clearAllMocks();
    const long = "x".repeat(900);
    vi.mocked(bridge.history).mockResolvedValue(historyPage([
      historyRow({ id: "1", preview: long }),
      historyRow({ id: "2", preview: "short" }),
    ]));
    const app = mount(App);
    try {
      await flushPromises();
      const rows = app.findAll(".history-row");
      expect(app.find(".history-preview").exists()).toBe(false);
      await rows[0].trigger("mouseenter");
      // The row clamps to three lines in CSS; the card holds the same string
      // uncut, and only for the row under the pointer.
      expect(app.findAll(".history-preview").length).toBe(1);
      expect(app.get(".history-preview").text()).toBe(long);
      expect(app.get(".history-preview").attributes("aria-hidden")).toBe("true");
      await rows[0].trigger("mouseleave");
      expect(app.find(".history-preview").exists()).toBe(false);
      // Keyboard reach: the same card opens for a row the user tabs into,
      // since the clamp is invisible to a screen reader but not to its user.
      await rows[1].trigger("focusin");
      expect(app.get(".history-preview").text()).toBe("short");
    } finally {
      app.unmount();
      vi.mocked(bridge.history).mockResolvedValue(historyPage([historyRow({
        preview: '<img src=x onerror="window.injected=true">',
      })]));
    }
  });
  it("refuses to open the translator for a clip with no text of its own", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.readHistoryText).mockResolvedValue({ id: "1", text: "", truncated: false });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="翻译记录"]').trigger("click");
    await flushPromises();
    expect(app.text()).toContain("这条记录没有可翻译的文本");
    // An empty dialog over an image would be noise, so nothing opens.
    expect(HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
    expect(bridge.translate).not.toHaveBeenCalled();
    app.unmount();
  });
  it("tells the user when only part of a long clip could be read", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.readHistoryText).mockResolvedValue({
      id: "1", text: "cut here", truncated: true,
    });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="翻译记录"]').trigger("click");
    await flushPromises();
    const dialog = app.get('[aria-labelledby="translate-item-title"]');
    expect(dialog.get(".translate-truncated").text()).toContain("8");
    app.unmount();
  });
  it("adds the selection to favorites and clears all history only after confirmation", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mount(App);
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

  it("sends a URL from the phone to its only connected peer without a picker", async () => {
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
      { id: "p", name: "Peer", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] as any });
    vi.mocked(bridge.sendUrl).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "app.send_url_requested" });
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledOnce();
      // One candidate needs no picker — legacy sent straight to it.
      expect(app.find('[aria-label="目标设备"]').exists()).toBe(false);
      const dialog = app.get('[aria-labelledby="send-url-title"]');
      expect(dialog.text()).toContain("Peer");
      await dialog.get('[aria-label="要发送的网址"]').setValue("https://example.com/from-phone");
      await dialog.findAll("button").find((button) => button.text() === "发送")!.trigger("click");
      await flushPromises();
      expect(bridge.sendUrl).toHaveBeenCalledExactlyOnceWith("p", "https://example.com/from-phone");
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
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

  it("reports a phone URL request when no peer is connected", async () => {
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
      { id: "p", name: "Peer", paired: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] as any });
    const app = mount(App);
    try {
      await flushPromises();
      emit({ type: "event", session_id: "s", name: "app.send_url_requested" });
      await flushPromises();
      expect(app.text()).toContain("没有已连接的设备可以发送。");
      expect(HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
    } finally {
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
      app.unmount();
    }
  });

  it("lets the host hide its own window without refreshing anything", async () => {
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
      vi.mocked(bridge.devices).mockClear();
      // The Rust bridge hides the window; the panel only has to not react.
      emit({ type: "event", session_id: "s", name: "app.window_close_requested" });
      await flushPromises();
      expect(bridge.devices).not.toHaveBeenCalled();
      expect(HTMLDialogElement.prototype.showModal).not.toHaveBeenCalled();
      expect(app.text()).not.toContain("没有已连接的设备可以发送。");
    } finally {
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

  it("takes the engine line and the description away once the engine is up", async () => {
    vi.mocked(bridge.devices).mockResolvedValueOnce({ items: [
      { id: "a", name: "First", paired: true } as any,
    ] });
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "running",
      capabilities: ["clipboard.push", "url.send"] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      expect(app.find("#devices-engine-note").exists()).toBe(false);
      const sendUrl = app.get('[aria-label="发送网址"]');
      expect(sendUrl.attributes("disabled")).toBeUndefined();
      expect(sendUrl.attributes("aria-describedby")).toBeUndefined();
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

  it("reports a clipboard-only push when sync is off", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["clipboard.push"] } as any);
    vi.mocked(bridge.pushText).mockResolvedValueOnce({ ok: true, len: 7, sent: false });
    const app = mount(App);
    await flushPromises();
    await app.get('[aria-label="设备"]').trigger("click");
    await app.get('[aria-label="推送文本"]').trigger("click");
    await flushPromises();
    const dialog = app.get('[aria-labelledby="push-text-title"]');
    await dialog.get('[aria-label="要推送的文本"]').setValue("offline");
    await dialog.findAll("button").find((button) => button.text() === "推送")!.trigger("click");
    await flushPromises();
    expect(app.text()).toContain("已推送到本机剪贴板（同步未开启，未广播）");
    app.unmount();
  });

  it("shows the Companion QR from the devices page and the tray entry", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.companionStatus).mockResolvedValue({
      enabled: true, port: 8080, running: true, state: "running",
      access_url: "http://10.0.0.2:8080/mobile.html?token=t",
    } as any);
    let menuAction: ((action: string) => void) | undefined;
    vi.mocked(bridge.onMenuAction).mockImplementation(async (handler: (action: any) => void) => {
      menuAction = handler;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      await openPanel(app, "手机 Companion");
      await app.findAll("button").find((button) => button.text().includes("读取手机服务状态"))!.trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text().includes("显示二维码"))!.trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="qr-title"]');
      expect(dialog.get("img").attributes("src")).toBe("data:image/png;base64,AAAA");
      expect(dialog.text()).toContain("http://10.0.0.2:8080/mobile.html?token=t");
      await dialog.findAll("button").find((button) => button.text() === "关闭")!.trigger("click");
      await flushPromises();
      // The native tray entry reopens the same dialog through the host event.
      menuAction!("qr");
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledTimes(2);
    } finally {
      vi.mocked(bridge.onMenuAction).mockResolvedValue(() => {});
      vi.mocked(bridge.companionQr).mockClear();
      app.unmount();
    }
  });

  it("opens About from settings and the tray, and opens only its fixed links", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    let menuAction: ((action: string) => void) | undefined;
    vi.mocked(bridge.onMenuAction).mockImplementation(async (handler: (action: any) => void) => {
      menuAction = handler;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text().includes("关于"))!.trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="about-title"]');
      expect(dialog.text()).toContain("ClipSync test");
      await dialog.findAll("button").find((button) => button.text().includes("项目主页"))!.trigger("click");
      await flushPromises();
      expect(bridge.openAboutLink).toHaveBeenLastCalledWith("homepage");
      expect(dialog.get('[role="status"]').text())
        .toContain("已在浏览器打开：https://github.com/kai3316/clipsync");
      await dialog.findAll("button").find((button) => button.text() === "关闭")!.trigger("click");
      await flushPromises();
      // The native tray entry reopens the same dialog through the host event.
      menuAction!("about");
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledTimes(2);
    } finally {
      vi.mocked(bridge.onMenuAction).mockResolvedValue(() => {});
      vi.mocked(bridge.openAboutLink).mockClear();
      app.unmount();
    }
  });

  it("opens settings, logs and the update check from the tray, not just About and QR", async () => {
    vi.clearAllMocks();
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["url.send"] } as any);
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "p", name: "Peer", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] as any });
    let menuAction: ((action: string) => void) | undefined;
    vi.mocked(bridge.onMenuAction).mockImplementation(async (handler: (action: any) => void) => {
      menuAction = handler;
      return () => {};
    });
    const app = mount(App);
    try {
      await flushPromises();
      // Each entry runs the window's own handler for that surface: nothing is
      // opened until the tray asks for it, and what opens is the same page and
      // the same dialogs the buttons open rather than a second implementation.
      const settingsButton = () =>
        app.findAll("button").some((button) => button.text().includes("关于"));
      expect(settingsButton()).toBe(false);
      menuAction!("settings");
      await flushPromises();
      expect(settingsButton()).toBe(true);

      menuAction!("export-logs");
      await flushPromises();
      expect(bridge.readLogs).toHaveBeenCalledOnce();
      expect(app.get('[aria-labelledby="logs-title"]').text()).toContain("line 1");

      menuAction!("check-update");
      await flushPromises();
      expect(bridge.updateCheck).toHaveBeenCalledOnce();

      // 发送链接 needs a target: the tray entry resolves it the same way the
      // phone's request does, which is the picker, preselected to the one peer.
      menuAction!("send-url");
      await flushPromises();
      expect(app.get('[aria-labelledby="send-url-title"]').text()).toContain("Peer");
    } finally {
      vi.mocked(bridge.onMenuAction).mockResolvedValue(() => {});
      // These are the mocks later tests count calls on; leaving them dirty
      // would make this test's tray actions look like theirs.
      for (const call of [bridge.settings, bridge.readLogs, bridge.updateCheck, bridge.updateStatus]) {
        vi.mocked(call).mockClear();
      }
      app.unmount();
    }
  });

  it("exports the log to a save-dialog destination and stays quiet when cancelled", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text().includes("查看日志"))!.trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="logs-title"]');
      await dialog.findAll("button").find((button) => button.text().includes("导出日志"))!.trigger("click");
      await flushPromises();
      expect(bridge.exportLogs).toHaveBeenCalledTimes(1);
      // The host's dialog gets a timestamped default name, never a path.
      expect(vi.mocked(bridge.exportLogs).mock.calls[0][0]).toMatch(/^clipsync_\d{8}_\d{6}\.log$/);
      expect(dialog.get('[role="status"]').text()).toContain("已导出：C:/logs/clipsync.log");
      // Cancelling the native dialog reports nothing.
      vi.mocked(bridge.exportLogs).mockResolvedValueOnce({ cancelled: true });
      await dialog.findAll("button").find((button) => button.text().includes("导出日志"))!.trigger("click");
      await flushPromises();
      expect(dialog.find('[role="status"]').exists()).toBe(false);
    } finally {
      // Later tests count calls on the shared bridge mocks.
      vi.mocked(bridge.readLogs).mockClear();
      vi.mocked(bridge.exportLogs).mockClear();
      app.unmount();
    }
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

  it("runs diagnostics, renders the grouped report, and repairs the firewall", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["diagnostics.report"] } as any);
    vi.mocked(bridge.diagnosticsReport).mockResolvedValue({
      v2: true, summary: "fail",
      checks: [{ id: "permissions", ok: true, detail: "raw permissions" }],
      groups: {
        system: { label_key: "diag.v2.group.system", label_text: "系统", items: [
          { id: "app_version", status: "ok", detail: "raw", label_text: "应用版本",
            detail_text: "版本 1.0.0" }] },
        network: { label_key: "diag.v2.group.network", label_text: "网络", items: [
          { id: "firewall", status: "fail", detail: "raw", label_text: "防火墙",
            detail_text: "端口 8765 未放行", hint_text: "请放行端口 8765" }] },
      },
      discovery_running: false, server_running: true, connected_count: 0, paired_count: 1,
      web_companion_running: false, web_port: 8080, lan_ip: "192.168.1.5", os: "Windows",
      version: "1.0.0",
    });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text().includes("运行诊断"))!
        .trigger("click");
      await flushPromises();
      expect(bridge.diagnosticsReport).toHaveBeenCalledExactlyOnceWith();
      const dialog = app.get('[aria-labelledby="diagnostics-title"]');
      expect(dialog.text()).toContain("存在故障");
      expect(dialog.text()).toContain("已配对 1 台");
      expect(dialog.text()).toContain("应用版本");
      expect(dialog.text()).toContain("版本 1.0.0");
      expect(dialog.text()).toContain("端口 8765 未放行");
      expect(dialog.text()).toContain("请放行端口 8765");
      await dialog.findAll("button").find((button) => button.text().includes("修复防火墙"))!
        .trigger("click");
      await flushPromises();
      expect(bridge.diagnosticsRequest).toHaveBeenCalledExactlyOnceWith("firewall");
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
    }
  });

  it("offers the local-network repair when the permissions check fails", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["diagnostics.report"] } as any);
    vi.mocked(bridge.diagnosticsReport).mockResolvedValue({
      v2: true, summary: "warn", groups: {},
      checks: [{ id: "permissions", ok: false, detail: "raw",
        detail_text: "未授予本地网络权限", guidance_text: "请在系统设置中允许 ClipSync" }],
      discovery_running: false, server_running: true, connected_count: 0, paired_count: 0,
      web_companion_running: false, web_port: 8080, lan_ip: "", os: "Darwin", version: "1.0.0",
    });
    vi.mocked(bridge.diagnosticsRequest).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text().includes("运行诊断"))!
        .trigger("click");
      await flushPromises();
      const dialog = app.get('[aria-labelledby="diagnostics-title"]');
      expect(dialog.text()).toContain("未授予本地网络权限");
      expect(dialog.text()).toContain("请在系统设置中允许 ClipSync");
      await dialog.findAll("button").find((button) => button.text().includes("打开本地网络权限"))!
        .trigger("click");
      await flushPromises();
      expect(bridge.diagnosticsRequest).toHaveBeenCalledExactlyOnceWith("local_network");
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
    }
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
    vi.mocked(bridge.updateCheck).mockResolvedValueOnce({
      available: true, latest: "v2.0.0", current: "1.0.0", url: "https://example.com" });
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

  it("reports a failed update download with the sidecar's reason", async () => {
    vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
      device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started",
      capabilities: ["update.status"] } as any);
    vi.mocked(bridge.updateDownload).mockResolvedValueOnce({
      ok: false, started: false, error: "update already in progress" });
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      await app.findAll("button").find((button) => button.text() === "下载更新");
      // No download button without a confirmed newer release, so call the
      // store directly and assert the panel surfaces the refusal.
      await (app.vm as any).startUpdateDownload();
      await flushPromises();
      expect(app.text()).toContain("更新下载失败：update already in progress");
    } finally {
      app.unmount();
      vi.mocked(bridge.status).mockResolvedValue({ version: "test", health: "ready",
        device_name: "Local", session_id: "s", seq: 0, sync_state: "not_started" } as any);
    }
  });

  it("persists the auto-update preference immediately and keeps the stored value on failure", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      vi.mocked(bridge.updateSettings).mockClear();
      const toggle = app.get('[aria-label="自动检查更新"]');
      await toggle.setValue(false);
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledExactlyOnceWith({ auto_update_check: false });
      expect((toggle.element as HTMLInputElement).checked).toBe(false);
      vi.mocked(bridge.updateSettings).mockRejectedValueOnce({
        code: "SAVE_FAILED", message: "disk full", retryable: true });
      await toggle.setValue(true);
      await flushPromises();
      expect(app.text()).toContain("保存设置失败");
      expect((toggle.element as HTMLInputElement).checked).toBe(false);
    } finally {
      app.unmount();
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
    expect(app.get('[aria-label="启用同步"]').attributes("disabled")).toBeDefined();
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
  it("says which route a device row is reachable by, not just that it is paired", async () => {
    // Three machines the device list alone cannot tell apart: one here on this
    // network and absent from the relay, one the relay can reach and this
    // network cannot, and one this machine has only ever known locally.  The
    // local half answers 离线 for two of them, so the joined relay row is the
    // only thing that separates "away" from "no internet pairing at all".
    vi.mocked(bridge.devices).mockResolvedValue({ items: [
      { id: "near", name: "Next room", paired: true, connection_state: "online",
        pairing_status: "paired", pairing_code: null, sas: null },
      { id: "far", name: "Away", paired: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
      { id: "only", name: "Cable only", paired: true, connection_state: "offline",
        pairing_status: "paired", pairing_code: null, sas: null },
    ] });
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ relay: "online", peers: [
      { peer_id: "near", name: "Next room", online: false, last_seen: 1700000000 },
      { peer_id: "far", name: "Away", online: true, last_seen: 1700000000 },
    ] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      const chips = (name: string) => {
        const row = app.findAll(".device-row").find(node => node.get("h2").text() === name);
        if (!row) throw new Error(`no device row named ${name}`);
        return row.findAll(".channel").map(node => node.text());
      };
      expect(chips("Next room")).toEqual(["已配对", "本地·在线", "互联网·离线"]);
      expect(chips("Away")).toEqual(["已配对", "本地·离线", "互联网·在线"]);
      // The third state is not 离线: a device with no relay pairing has no
      // internet route to be away on, and the row says which of the two it is.
      expect(chips("Cable only")).toEqual(["已配对", "本地·离线", "互联网·未配对"]);
      // The relay's own link is the first line of the pairing card, because
      // every 在线 under it is the relay's view of another device: when ours is
      // down the whole list reads 离线 and nothing would say who is away.
      await openPanel(app, "互联网配对");
      expect(app.get(".relay-state").text()).toContain("本机中继：在线");
      expect(app.get(".relay-state").classes()).toContain("relay-state--online");
    } finally {
      app.unmount();
      vi.mocked(bridge.devices).mockResolvedValue({ items: [] });
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [] });
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

  it("shows what each internet peer's relay still holds, and folds a live receipt into it", async () => {
    let emit!: Parameters<typeof bridge.subscribe>[0];
    vi.mocked(bridge.subscribe).mockImplementationOnce(async callback => {
      emit = callback; return () => {};
    });
    vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [
      { peer_id: "p1", name: "Phone", online: false },
    ] } as any);
    // What the host queued before this window opened: the panel seeds itself
    // from the ledger, so a device that has been offline has something to show.
    vi.mocked(bridge.relayDeliveryStatus).mockResolvedValue({ pending: 1, items: [
      { msg_id: "m1", status: "queued" },
    ] } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设备"]').trigger("click");
      await flushPromises();
      // The peers the relay is holding for are the internet pairing panel's own
      // rows: the device list is about machines on this network.
      await openPanel(app, "互联网配对");
      expect(bridge.relayDeliveryStatus).toHaveBeenCalledWith("p1");
      expect(app.get(".delivery-badge").text()).toBe(t("待补发 {count}", { count: 1 }));
      const aggregate = app.findAll("p").find(node => node.text().startsWith(t("待投递消息：")));
      expect(aggregate?.text()).toBe(`${t("待投递消息：")}1`);
      // The peer comes back and the queue drains: the receipt retires the badge
      // and leaves the result the send ended on.
      emit({ type: "event", session_id: "s", name: "relay.delivery.changed",
        data: { peer_id: "p1", msg_id: "m1", status: "delivered",
          kind: "clipboard", session_id: "" } });
      await flushPromises();
      expect(app.find(".delivery-badge").exists()).toBe(false);
      expect(app.get(".delivery-result").text()).toContain(t("已送达"));
      expect(app.get(".delivery-result").classes()).toContain("delivery-result--delivered");
      expect(aggregate?.text()).toBe(`${t("待投递消息：")}0`);
    } finally {
      app.unmount();
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [] });
      vi.mocked(bridge.relayDeliveryStatus).mockResolvedValue({ pending: 0, items: [] });
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
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [], waiting: [
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
      vi.mocked(bridge.internetPairingStatus).mockResolvedValue({ peers: [] });
      vi.mocked(bridge.enterInternetPairingCode).mockResolvedValue({ peer_id: "", waiting: true });
    }
  });

  it("keeps the footer to this machine and this build, with no window action", async () => {
    const app = mount(App);
    try {
      await flushPromises();
      // 退出 belongs to the tray, and the window is decorated, so the title bar
      // already carries minimize.  A second control for the title bar's job was
      // a duplicate, and the footer's own two facts are what is left.
      const footer = app.get(".sidebar-footer");
      expect(footer.text()).toContain("Local");
      expect(footer.text()).toContain(t("版本 {version}", { version: "test" }));
      expect(footer.findAll("button")).toHaveLength(0);
      expect(bridge.minimize).not.toHaveBeenCalled();
      expect(bridge.quit).not.toHaveBeenCalled();
    } finally { app.unmount(); }
  });

  it("renders clipboard markup as text, never as executable HTML", async () => {
    const app = mount(App);
    await flushPromises();
    expect(app.find(".history-content").text()).toContain("<img");
    expect(app.find(".history-content img").exists()).toBe(false);
    expect(app.text()).toContain("同步引擎未启动");
    app.unmount();
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

  it("stays away once a language has been chosen, and follows that language", async () => {
    HTMLDialogElement.prototype.showModal = vi.fn();
    setLocale("zh-CN");
    vi.mocked(bridge.settings).mockResolvedValueOnce({
      settings: { language: "en", language_chosen: true },
    });
    const app = mount(App);
    try {
      await flushPromises();
      expect(showModal()).not.toHaveBeenCalled();
      expect(app.find('[aria-label="Settings"]').exists()).toBe(true);
    } finally {
      app.unmount();
      setLocale("zh-CN");
      vi.mocked(bridge.settings).mockResolvedValue({ settings: {} });
    }
  });

  it("switches and persists the language from the settings page", async () => {
    setLocale("zh-CN");
    vi.mocked(bridge.updateSettings).mockClear();
    const app = mount(App);
    try {
      await flushPromises();
      await app.get('[aria-label="设置"]').trigger("click");
      await flushPromises();
      const select = app.get('select[aria-label="语言"]');
      expect(select.findAll("option").map(option => option.text())).toEqual(["简体中文", "English"]);
      await select.setValue("en");
      await flushPromises();
      expect(bridge.updateSettings).toHaveBeenCalledExactlyOnceWith({ language: "en" });
      expect(app.find('[aria-label="Settings"]').exists()).toBe(true);
    } finally {
      app.unmount();
      setLocale("zh-CN");
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
  /** A status payload with sync in the state the case is about. */
  function status(syncState: string) {
    return { version: "test", health: "ready", device_name: "Local",
      session_id: "s", seq: 0, sync_state: syncState } as any;
  }

  it("offers the dashboard's pause presets while sync is running", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.status).mockResolvedValue(status("running"));
    const app = mount(App);
    try {
      await flushPromises();
      const row = app.get(".bottom-status");
      expect(row.text()).toContain("定时暂停同步");
      expect(row.findAll("button").map(button => button.text()))
        .toEqual(["15 分钟", "30 分钟", "1 小时"]);
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("pauses for the chosen preset and counts down the deadline the runtime armed", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    const until = Math.floor(Date.now() / 1000) + 30 * 60;
    vi.mocked(bridge.status).mockResolvedValue(status("running"));
    vi.mocked(bridge.pauseSync).mockResolvedValue({ enabled: false, until } as any);
    const app = mount(App);
    try {
      await flushPromises();
      await app.get(".bottom-status").findAll("button")
        .find(button => button.text() === "30 分钟")!.trigger("click");
      await flushPromises();
      expect(bridge.pauseSync).toHaveBeenCalledWith(30);
      // The countdown reads the deadline the sidecar reports, not the duration
      // this window asked for, so a pause armed elsewhere counts down too.
      expect(app.get(".bottom-status").text()).toContain("⏸ 已暂停 · 剩余 30 分钟");
      expect(app.text()).toContain("同步已暂停 30 分钟");
      // The presets give way to the way out of the pause.
      expect(app.get(".bottom-status").findAll("button").map(button => button.text()))
        .toEqual(["立即恢复"]);
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("resumes from the countdown and stops showing the deadline", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.status).mockResolvedValue(status("paused"));
    vi.mocked(bridge.settings).mockResolvedValue({
      settings: { timed_pause_until: Math.floor(Date.now() / 1000) + 600 },
    } as any);
    const app = mount(App);
    try {
      await flushPromises();
      expect(app.get(".bottom-status").text()).toContain("⏸ 已暂停 · 剩余 10 分钟");
      await app.get(".bottom-status").get("button").trigger("click");
      await flushPromises();
      expect(bridge.resumeSync).toHaveBeenCalledTimes(1);
      expect(app.get(".bottom-status").text()).not.toContain("剩余");
      expect(app.text()).toContain("同步已恢复");
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("keeps the plain resume when sync is off with no deadline armed", async () => {
    setLocale("zh-CN");
    vi.clearAllMocks();
    vi.mocked(bridge.status).mockResolvedValue(status("paused"));
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { timed_pause_until: 0 } } as any);
    const app = mount(App);
    try {
      await flushPromises();
      // Nothing was timed, so there is no countdown — only the way back to the
      // sync the user turned off from the toggle beside it.
      expect(app.get(".bottom-status").findAll("button").map(button => button.text()))
        .toEqual(["恢复同步"]);
      await app.get(".bottom-status").get("button").trigger("click");
      await flushPromises();
      expect(bridge.resumeSync).toHaveBeenCalledTimes(1);
    } finally {
      app.unmount();
      setLocale("zh-CN");
    }
  });

  it("asks the runtime again once the deadline passes, and stops counting down", async () => {
    vi.useFakeTimers();
    setLocale("zh-CN");
    vi.clearAllMocks();
    const until = Math.floor(Date.now() / 1000) + 600;
    vi.mocked(bridge.status).mockResolvedValue(status("paused"));
    vi.mocked(bridge.settings).mockResolvedValue({ settings: { timed_pause_until: until } } as any);
    const app = mount(App);
    try {
      await flushPromises();
      expect(app.get(".bottom-status").text()).toContain("已暂停 · 剩余 10 分钟");
      // The host resumes on its own at the deadline and clears it on disk; this
      // window only has to stop showing a deadline that is no longer there.
      vi.mocked(bridge.settings).mockResolvedValue({ settings: { timed_pause_until: 0 } } as any);
      const before = vi.mocked(bridge.settings).mock.calls.length;
      await vi.advanceTimersByTimeAsync(600000);
      await flushPromises();
      // One extra read: the tick that crosses the deadline asks, and the answer
      // it gets clears the countdown so the ticks after it ask nothing.
      expect(vi.mocked(bridge.settings).mock.calls.length).toBe(before + 1);
      expect(app.get(".bottom-status").text()).not.toContain("剩余");
    } finally {
      app.unmount();
      vi.useRealTimers();
      setLocale("zh-CN");
    }
  });
;

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

  it("treats a drop with no paths in it as no drop at all", async () => {
    vi.clearAllMocks();
    setLocale("zh-CN");
    const { app, drop } = await mountWithDrop();
    try {
      // Some platforms report a drop with nothing in it.  Opening a page and
      // asking which device to send nothing to would be a question with no
      // possible answer, so the window stays where it is.
      drop({ type: "drop", paths: [], position: { x: 1, y: 1 } });
      await flushPromises();
      expect(app.find(".transfers-view").exists()).toBe(false);
      expect(app.find(".history-list").exists()).toBe(true);
      expect(app.find(".drop-veil").exists()).toBe(false);
    } finally {
      app.unmount();
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
    }
  });

  it("stops listening when the window goes away", async () => {
    vi.clearAllMocks();
    setLocale("zh-CN");
    let drop!: (event: any) => void;
    const off = vi.fn();
    vi.mocked(bridge.onFileDrop).mockImplementation(async (handler: (event: any) => void) => {
      drop = handler;
      return off;
    });
    const app = mount(App);
    try {
      await flushPromises();
      drop({ type: "enter", paths: ["C:/a.txt"], position: { x: 1, y: 1 } });
      await flushPromises();
      expect(app.find(".drop-veil").exists()).toBe(true);
      app.unmount();
      expect(off).toHaveBeenCalledOnce();
    } finally {
      vi.mocked(bridge.onFileDrop).mockResolvedValue(() => {});
    }
  });
});
