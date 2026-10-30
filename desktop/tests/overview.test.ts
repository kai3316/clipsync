import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import OverviewView from "../src/components/OverviewView.vue";
import { bridge } from "../src/api/bridge";
import type { Overview } from "../src/api/types";
import { clearStatus, statusMessage } from "../src/lib/status";

// The page reads two things of its own: the overview snapshot and the
// diagnostics summary its chip shows.  Everything else it does is a write to
// the runtime through the store, or an event the window answers.
vi.mock("../src/api/bridge", () => ({
  bridge: {
    overview: vi.fn(),
    diagnosticsReport: vi.fn().mockResolvedValue({ summary: "ok" }),
    copyText: vi.fn().mockResolvedValue({ copied: true }),
    companionQr: vi.fn(),
  },
}));

/** A runtime snapshot as the page reads one. */
function overview(overrides: Partial<Overview> = {}): Overview {
  return {
    connected_count: 0, paired_count: 0, discovered_count: 0, connected_names: [],
    history_count: 0, history_today: 0, history_pinned: 0, history_images: 0,
    active_transfers: 0, transfer_completed: 0, discovering: false, visible: false,
    sync_enabled: true, web_enabled: false, uptime_seconds: 0, local_ip: "",
    port: 8765, platform: "Windows", version: "1.0.2", network_type: "lan",
    network_detail: "", recent_items: [],
    ...overrides,
  };
}

/** The store the page reaches through.  Only what it touches is here: the reads
 * it makes go through `bridge`, and the switches are the store's own actions. */
function storeStub(capabilities: string[] = ["diagnostics.report"]) {
  return {
    state: {
      status: { device_id: "dev-1", device_name: "Laptop", capabilities, health: "ready" },
      pending: false,
      error: null as any,
      devices: [] as any[],
      updateCheck: null as any,
      update: { phase: "idle" },
    },
    // The chip reads the report the same way the window's dialog does, so the
    // page goes through the store for it (which is what holds it for a minute).
    diagnostics: vi.fn(async () => bridge.diagnosticsReport()),
    setSyncEnabled: vi.fn().mockResolvedValue(undefined),
    setDiscoveryEnabled: vi.fn().mockResolvedValue({ enabled: true, visible: true }),
    setDiscoveryVisible: vi.fn().mockResolvedValue({ enabled: true, visible: false }),
  };
}

type Store = ReturnType<typeof storeStub>;

function mountView(store: Store = storeStub(), props: Record<string, unknown> = {}) {
  return mount(OverviewView, {
    props: {
      store: store as any, enabled: true, syncRunning: true, companionOn: false,
      pauseLeftMs: 0, pauseBusy: false, ...props,
    },
  });
}

/** A button by the words a reader sees on it. */
function button(view: ReturnType<typeof mountView>, label: string) {
  return view.findAll("button").find(node => (node.text() || "").trim() === label)!;
}

/** The 开 or 关 button of one two-state switch row, by the row's label. */
function segment(view: ReturnType<typeof mountView>, label: string, state: "开" | "关") {
  return view.get(`[aria-label="${label}"]`)
    .findAll("button").find(node => (node.text() || "").trim() === state)!;
}

describe("the overview", () => {
  beforeEach(() => {
    vi.mocked(bridge.overview).mockResolvedValue(overview());
    vi.mocked(bridge.diagnosticsReport).mockResolvedValue({ summary: "ok" } as any);
    vi.mocked(bridge.copyText).mockClear().mockResolvedValue({ copied: true } as any);
    vi.mocked(bridge.companionQr).mockReset().mockResolvedValue({
      ok: true, url: "http://192.168.1.5:19991/?token=x", qr: "data:image/png;base64,QR",
    });
    clearStatus();
  });

  afterEach(() => {
    vi.mocked(bridge.overview).mockReset();
    vi.mocked(bridge.diagnosticsReport).mockReset();
    vi.mocked(bridge.companionQr).mockReset();
    clearStatus();
    // Every case here is a page that must not outlive the document it was
    // mounted into.
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
  });

  /* ── The status line ──────────────────────────────────────────────── */

  it("says how many devices are online, and leads to the devices page", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ connected_count: 2, paired_count: 3 }));
    const view = mountView();
    try {
      await flushPromises();
      const line = view.get(".overview-device-line");
      expect(line.text()).toContain("2 台设备在线");
      await line.trigger("click");
      expect(view.emitted("devices")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("names paired-but-off devices rather than showing a zero", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ connected_count: 0, paired_count: 2 }));
    const view = mountView();
    try {
      await flushPromises();
      expect(view.get(".overview-device-line").text()).toContain("已配对 2 台 · 当前离线");
      expect(view.text()).not.toContain("0 台设备在线");
    } finally { view.unmount(); }
  });

  it("offers pairing instead of a zero when there is no device at all", async () => {
    // No big empty panel: a reader whose machine has no peers gets one line and
    // the QR in the card beside it, not a block of instructions between them.
    const view = mountView();
    try {
      await flushPromises();
      expect(view.get(".overview-device-line").text()).toContain("还没有配对设备 · 去配对");
      expect(view.find(".empty--page").exists()).toBe(false);
      expect(view.text()).not.toContain("暂无设备");
    } finally { view.unmount(); }
  });

  it("reports the pause deadline in the status line, and resumes from the card", async () => {
    const view = mountView(storeStub(), { pauseLeftMs: 20 * 60_000, syncRunning: false });
    try {
      await flushPromises();
      expect(view.get(".overview-status").text()).toContain("同步已暂停");
      expect(view.get(".overview-status").text()).toContain("剩余 20 分钟");
      // The resume control sits in the settings card, where the presets are, so
      // a paused reader has one obvious way back.
      await button(view, "立即恢复").trigger("click");
      expect(view.emitted("resume")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("shows the health chip from the report, and does not ask for one it cannot run", async () => {
    vi.mocked(bridge.diagnosticsReport).mockResolvedValue({ summary: "fail" } as any);
    const view = mountView();
    try {
      await flushPromises();
      expect(view.get(".overview-health").text()).toContain("网络异常");
      expect(view.get(".overview-health").classes()).toContain("overview-health--fail");
      await view.get(".overview-health").trigger("click");
      expect(view.emitted("diagnostics")).toHaveLength(1);
    } finally { view.unmount(); }

    vi.mocked(bridge.diagnosticsReport).mockClear();
    const without = mountView(storeStub([]));
    try {
      await flushPromises();
      expect(without.get(".overview-health").text()).toContain("检查网络");
      expect(bridge.diagnosticsReport).not.toHaveBeenCalled();
    } finally { without.unmount(); }
  });

  /* ── This machine ─────────────────────────────────────────────────── */

  it("shows this machine's facts, copies them and lets the name be edited", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({
      web_enabled: true, local_ip: "192.168.1.7", port: 8765, platform: "Windows", version: "1.2.3",
    }));
    const view = mountView();
    try {
      await flushPromises();
      const text = view.text();
      expect(text).toContain("设备 ID");
      expect(text).toContain("dev-1");
      expect(text).toContain("Windows · v1.2.3");
      expect(text).toContain("192.168.1.7:8765");

      await view.get('[aria-label="复制设备 ID"]').trigger("click");
      await flushPromises();
      expect(bridge.copyText).toHaveBeenLastCalledWith("dev-1");
      // `copyText` is the window's shared helper, so it says 已复制！ on the
      // status line — the same confirmation every other copy in the window gives.
      expect(statusMessage().value).toBe("已复制！");
      await view.get('[aria-label="复制本地地址"]').trigger("click");
      await flushPromises();
      expect(bridge.copyText).toHaveBeenLastCalledWith("192.168.1.7:8765");

      // Renaming happens where the name is, and only a changed name is sent.
      await view.get('[aria-label="编辑设备名称"]').trigger("click");
      const input = view.get("input.overview-name-input");
      expect((input.element as HTMLInputElement).value).toBe("Laptop");
      await input.setValue("  Study PC  ");
      await input.trigger("keydown.enter");
      expect(view.emitted("rename")).toEqual([["Study PC"]]);
      expect(view.emitted("qr")).toBeUndefined();
    } finally { view.unmount(); }
  });

  it("puts the two ways to reach this machine under its address", async () => {
    const view = mountView();
    try {
      await flushPromises();
      await button(view, "互联网同步").trigger("click");
      await button(view, "手机伴侣").trigger("click");
      expect(view.emitted("internet-page")).toHaveLength(1);
      expect(view.emitted("companion-page")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("offers the phone companion's switch where its address would be", async () => {
    // With no companion running there is no local address to copy, so the card
    // says why and offers the page that starts it — a bare 未启用 would be a
    // dead end.
    const view = mountView();
    try {
      await flushPromises();
      expect(view.text()).toContain("手机伴侣未启用");
      await button(view, "去开启").trigger("click");
      expect(view.emitted("companion-page")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("shows the phone QR in the machine card, with a way to enlarge it", async () => {
    // The card's right half used to be empty and the QR sat behind a dialog.
    // One picture, in place, is the thing a reader came to this card for.
    const view = mountView(storeStub(), { companionOn: true });
    try {
      await flushPromises();
      expect(view.get(".overview-qr__image").attributes("src")).toBe("data:image/png;base64,QR");
      expect(view.findAll("button").filter(node => node.text().trim() === "显示二维码")).toHaveLength(0);
      await button(view, "放大二维码").trigger("click");
      expect(view.emitted("qr")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("says why the QR is missing and offers to read it again", async () => {
    vi.mocked(bridge.companionQr).mockResolvedValue({
      ok: false, url: null, qr: null, error: "手机伴侣没有在运行",
    });
    const view = mountView(storeStub(), { companionOn: true });
    try {
      await flushPromises();
      expect(view.get(".overview-qr__hint").text()).toContain("手机伴侣没有在运行");
      await button(view, "重试").trigger("click");
      expect(bridge.companionQr).toHaveBeenCalledTimes(2);
    } finally { view.unmount(); }
  });

  /* ── The switches ─────────────────────────────────────────────────── */

  it("writes the four switches through the store and the window", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ discovering: false, visible: true }));
    const store = storeStub();
    const view = mountView(store);
    try {
      await flushPromises();
      await segment(view, "剪贴板同步", "关").trigger("click");
      expect(store.setSyncEnabled).toHaveBeenCalledWith(false);

      await segment(view, "局域网发现", "开").trigger("click");
      expect(store.setDiscoveryEnabled).toHaveBeenCalledWith(true);

      await segment(view, "在局域网可见", "关").trigger("click");
      expect(store.setDiscoveryVisible).toHaveBeenCalledWith(false);

      // The phone page's switch is the window's own: it owns the service's
      // status, so the click only reports what was asked for.
      await segment(view, "手机网页访问", "开").trigger("click");
      expect(view.emitted("companion")).toEqual([[true]]);
    } finally { view.unmount(); }
  });

  it("puts a refused switch back and says so, instead of leaving it flipped", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ discovering: false, visible: true }));
    const store = storeStub();
    // The store reports a refusal by recording it, and leaves the runtime
    // alone; the page has to survive that with the pair back where it was.
    store.setSyncEnabled.mockImplementation(async () => {
      store.state.error = { code: "RPC_FAILED", message: "同步没有切换", retryable: true };
    });
    store.setDiscoveryEnabled.mockResolvedValue(null as any);
    const view = mountView(store);
    try {
      await flushPromises();
      await segment(view, "剪贴板同步", "关").trigger("click");
      await flushPromises();
      expect(segment(view, "剪贴板同步", "开").attributes("aria-pressed")).toBe("true");
      expect(statusMessage().value).toBe("同步没有切换");

      await segment(view, "局域网发现", "开").trigger("click");
      await flushPromises();
      expect(segment(view, "局域网发现", "关").attributes("aria-pressed")).toBe("true");
      expect(statusMessage().value).toBe("操作没有生效，请稍后重试");
    } finally { view.unmount(); }
  });

  it("offers the timed pause presets while sync runs", async () => {
    const view = mountView();
    try {
      await flushPromises();
      const pause = view.get(".overview-pause");
      const labels = pause.findAll("button").map(node => node.text());
      expect(labels).toEqual(["15 分钟", "30 分钟", "1 小时"]);
      await pause.findAll("button")[1].trigger("click");
      expect(view.emitted("pause")).toEqual([[30]]);
    } finally { view.unmount(); }
  });

  /* ── The quick actions ────────────────────────────────────────────── */

  it("raises one event per quick action, reusing the window's own handlers", async () => {
    const view = mountView();
    // The paste panel button carries its default chord beside the name (the
    // panel is opened from a global hotkey, so the button is where a reader
    // learns it), which means its text is the label plus the key cap.
    const pastePanel = () => view.findAll("button")
      .find(node => (node.text() || "").includes("粘贴面板"))!;
    try {
      await flushPromises();
      await pastePanel().trigger("click");
      await button(view, "发送文件").trigger("click");
      // 设备 is one of the three primary pages, so it has an explicit door here
      // as well as the status line's clickable count.
      await button(view, "设备管理").trigger("click");
      await button(view, "导入历史").trigger("click");
      await button(view, "导出历史").trigger("click");
      await button(view, "快捷键设置").trigger("click");
      await button(view, "检查更新").trigger("click");
      await button(view, "更多设置").trigger("click");
      expect(view.emitted("quick-paste")).toHaveLength(1);
      expect(view.emitted("send-files")).toHaveLength(1);
      expect(view.emitted("devices")).toHaveLength(1);
      expect(view.emitted("import-history")).toHaveLength(1);
      // The settings card's export defaults to JSON; CSV and Markdown stay on
      // that card's own three buttons.
      expect(view.emitted("export-history")).toEqual([["json"]]);
      expect(view.emitted("settings-hotkeys")).toHaveLength(1);
      expect(view.emitted("check-update")).toHaveLength(1);
      expect(view.emitted("settings")).toHaveLength(1);
      // 粘贴面板 is the page's primary action, and 更多设置 is the way off it.
      expect(pastePanel().classes()).toContain("overview-tile--primary");
      expect(pastePanel().get(".overview-shortcut").text()).toBe("Ctrl+Shift+V");
      expect(button(view, "更多设置").classes()).toContain("overview-more-link");
    } finally { view.unmount(); }
  });

  /* ── The banner ───────────────────────────────────────────────────── */

  it("draws the attention banner only when it has something to say", async () => {
    // Nothing to report: no heading, no empty panel, no "0 项".
    const quiet = mountView();
    try {
      await flushPromises();
      expect(quiet.find(".overview-attention").exists()).toBe(false);
      expect(quiet.text()).not.toContain("需要处理");
    } finally { quiet.unmount(); }
  });

  it("sends an available update to the settings card instead of checking again", async () => {
    const store = storeStub();
    store.state.updateCheck = { available: true, latest: "1.3.0", current: "1.2.3", url: "" };
    vi.mocked(bridge.overview).mockResolvedValue(overview());
    const view = mountView(store);
    try {
      await flushPromises();
      const banner = view.get(".overview-attention");
      expect(banner.text()).toContain("1.3.0");
      await banner.findAll("button").find(node => node.text() === "查看更新")!.trigger("click");
      expect(view.emitted("settings-update")).toHaveLength(1);
      expect(view.emitted("check-update")).toBeUndefined();
    } finally { view.unmount(); }
  });

  it("reports a pairing request waiting on this window", async () => {
    const store = storeStub();
    store.state.devices = [{ id: "peer", paired: false, pairing_status: "pending", archived: false }];
    vi.mocked(bridge.overview).mockResolvedValue(overview());
    const view = mountView(store);
    try {
      await flushPromises();
      expect(view.get(".overview-attention").text()).toContain("1 个配对请求等待确认");
      await view.get(".overview-attention").findAll("button")[0].trigger("click");
      expect(view.emitted("devices")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("does not repeat the offline-device count in the banner", async () => {
    // The status line already says how many paired devices are away; a second
    // sentence saying it would be the same reading twice.
    vi.mocked(bridge.overview).mockResolvedValue(overview({ connected_count: 0, paired_count: 2 }));
    const view = mountView();
    try {
      await flushPromises();
      expect(view.get(".overview-status").text()).toContain("已配对 2 台 · 当前离线");
      expect(view.find(".overview-attention").exists()).toBe(false);
    } finally { view.unmount(); }
  });

  /* ── The pages that are gone ──────────────────────────────────────── */

  it("no longer draws the counters, the network map, the device list or the clip feed", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({
      connected_count: 2, paired_count: 2, history_count: 33, history_today: 12,
      history_images: 4, active_transfers: 1, transfer_completed: 3,
      connected_names: ["Phone"], recent_items: [],
    }));
    const view = mountView();
    try {
      await flushPromises();
      for (const selector of [".overview-stat", ".overview-stats", ".overview-ring", ".overview-feed"]) {
        expect(view.find(selector).exists(), `${selector} should be gone`).toBe(false);
      }
      for (const words of ["网络地图", "最近活动", "已连接设备", "共 2 台已配对", "历史记录"]) {
        expect(view.text()).not.toContain(words);
      }
    } finally { view.unmount(); }
  });

  /* ── Loading, failure and polling ──────────────────────────────────── */

  it("keeps the page's shape while loading, and offers a retry when the read fails", async () => {
    vi.mocked(bridge.overview).mockRejectedValueOnce(new Error("sidecar is not answering"));
    const view = mountView();
    try {
      await flushPromises();
      // The failure is a sentence with a way out, not a blank page.
      expect(view.text()).toContain("sidecar is not answering");
      expect(view.find(".overview-skeleton").exists()).toBe(true);
      vi.mocked(bridge.overview).mockResolvedValue(overview());
      await button(view, "重试").trigger("click");
      await flushPromises();
      expect(view.find(".overview-status").exists()).toBe(true);
      expect(view.find(".overview-skeleton").exists()).toBe(false);
    } finally { view.unmount(); }
  });

  it("stops reading once the page is off screen, and while the window is hidden", async () => {
    vi.useFakeTimers();
    vi.mocked(bridge.overview).mockClear().mockResolvedValue(overview());
    const view = mountView();
    try {
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(1);
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      vi.advanceTimersByTime(20000);
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(1);
      Object.defineProperty(document, "hidden", { value: false, configurable: true });
      vi.advanceTimersByTime(5000);
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("says the page is unavailable rather than pretending it read something", async () => {
    vi.mocked(bridge.overview).mockClear();
    const off = mountView(storeStub(), { enabled: false });
    try {
      await flushPromises();
      expect(off.text()).toContain("同步引擎未运行");
      expect(bridge.overview).not.toHaveBeenCalled();
    } finally { off.unmount(); }

    vi.mocked(bridge.overview).mockRejectedValue(new Error("sidecar is not answering"));
    const failed = mountView();
    try {
      await flushPromises();
      expect(failed.text()).toContain("sidecar is not answering");
    } finally { failed.unmount(); }
  });

  it("tells a booting window where it is rather than claiming a verdict", async () => {
    const booting = storeStub();
    delete (booting.state as { status?: unknown }).status;
    const view = mountView(booting, { enabled: false });
    try {
      await flushPromises();
      expect(view.text()).toContain("正在读取概览");
      expect(view.text()).not.toContain("同步引擎未运行");
    } finally { view.unmount(); }
  });

  it("does not run a whole diagnostics report for one word more often than it must", async () => {
    vi.useFakeTimers();
    vi.mocked(bridge.diagnosticsReport).mockClear().mockResolvedValue({ summary: "ok" } as any);
    const view = mountView();
    try {
      await flushPromises();
      expect(bridge.diagnosticsReport).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(90000);
      await flushPromises();
      expect(bridge.diagnosticsReport).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(40000);
      await flushPromises();
      expect(bridge.diagnosticsReport).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});
