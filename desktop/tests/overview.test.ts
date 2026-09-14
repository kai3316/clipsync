import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import OverviewView from "../src/components/OverviewView.vue";
import { bridge } from "../src/api/bridge";
import type { Overview } from "../src/api/types";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    overview: vi.fn(),
    diagnosticsReport: vi.fn().mockResolvedValue({ summary: "ok" }),
    copyText: vi.fn().mockResolvedValue({ copied: true }),
    copyHistory: vi.fn().mockResolvedValue({ copied: true }),
  },
}));

/** A feed row's clip, as the sidecar now serves one: a history row, with the
 *  preview cut to what the feed's single line shows. */
function clip(id: string) {
  return {
    id, timestamp: 0, preview: "", content_type: "TEXT", pinned: false,
    source_name: "", source_app: "", source_title: "", transport: "", paste_count: 0,
  };
}

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

/** The store the page reaches through.  Only the four things it touches are
 * here: the page owns one read of its own, and everything else it can do is
 * either a write to the runtime or an event the window answers. */
function storeStub(capabilities: string[] = ["diagnostics.report"]) {
  return {
    state: {
      status: { device_id: "dev-1", device_name: "Laptop", capabilities },
      pending: false,
      error: null as any,
      copiedId: null as string | null,
    },
    copy: vi.fn(async function (this: any, item: { id: string }) {
      this.state.copiedId = item.id;
      await bridge.copyHistory(item.id);
    }),
    setSyncEnabled: vi.fn().mockResolvedValue(undefined),
    setDiscoveryEnabled: vi.fn().mockResolvedValue(undefined),
    setDiscoveryVisible: vi.fn().mockResolvedValue(undefined),
  };
}

type Store = ReturnType<typeof storeStub>;

function mountView(store: Store = storeStub(), props: Record<string, unknown> = {}) {
  return mount(OverviewView, {
    props: { store: store as any, enabled: true, syncRunning: true, companionOn: false,
      companionUrl: null, pauseLeftMs: 0, pauseBusy: false, ...props },
  });
}

describe("the overview", () => {
  it("says it cannot tell rather than guessing when the report is unavailable", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview());
    vi.mocked(bridge.diagnosticsReport).mockRejectedValue(new Error("no report"));
    const view = mountView();
    try {
      await flushPromises();
      expect(view.get(".overview-health").text()).toContain("检查网络");
    } finally { view.unmount(); }

    // A runtime that does not run the suite is not asked: the probes are socket
    // work, and a report nobody can have is not worth asking twice for.
    vi.mocked(bridge.diagnosticsReport).mockClear();
    const without = mountView(storeStub([]));
    try {
      await flushPromises();
      expect(bridge.diagnosticsReport).not.toHaveBeenCalled();
      expect(without.get(".overview-health").text()).toContain("检查网络");
    } finally { without.unmount(); }
  });

  it("shows the runtime's truth on a switch, not the reader's click", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ discovering: false }));
    const store = storeStub();
    // What the box reads at the moment the write is sent, which is the whole
    // point: the browser has already flipped it under the reader's finger.
    let checkedWhenWritten: boolean | undefined;
    let view!: ReturnType<typeof mountView>;
    store.setDiscoveryEnabled.mockImplementation(async () => {
      checkedWhenWritten = (view.get('[aria-label="发现"]').element as HTMLInputElement).checked;
    });
    view = mountView(store);
    try {
      await flushPromises();
      await view.get('[aria-label="发现"]').setValue(true);
      await flushPromises();
      expect(store.setDiscoveryEnabled).toHaveBeenCalledWith(true);
      expect(checkedWhenWritten).toBe(false);
      // And the read that follows puts it where the runtime says it is — which
      // is still off, because discovery never came up.
      expect((view.get('[aria-label="发现"]').element as HTMLInputElement).checked).toBe(false);
    } finally { view.unmount(); }
  });

  it("keeps a refused write from leaving the sync switch flipped", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ sync_enabled: true }));
    const store = storeStub();
    // The store's actions report a refusal by recording it, and leave the
    // runtime alone — which is what the page has to survive.
    store.setSyncEnabled.mockImplementation(async () => { store.state.error = new Error("refused"); });
    const view = mountView(store);
    try {
      await flushPromises();
      await view.get('[aria-label="剪贴板同步"]').setValue(false);
      await flushPromises();
      expect(store.setSyncEnabled).toHaveBeenCalledWith(false);
      expect((view.get('[aria-label="剪贴板同步"]').element as HTMLInputElement).checked).toBe(true);
    } finally { view.unmount(); }
  });

  it("renames this device, and only when the name actually changed", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview());
    const view = mountView();
    try {
      await flushPromises();
      await view.get('[aria-label="编辑设备名称"]').trigger("click");
      const input = view.get("input.overview-name-input");
      expect((input.element as HTMLInputElement).value).toBe("Laptop");
      await input.setValue("  Study PC  ");
      await input.trigger("keydown.enter");
      expect(view.emitted("rename")).toEqual([["Study PC"]]);

      // Escape takes the editor away, and the blur that follows it must not
      // commit what the reader just cancelled.
      await view.get('[aria-label="编辑设备名称"]').trigger("click");
      await view.get("input.overview-name-input").setValue("Renamed by mistake");
      await view.get("input.overview-name-input").trigger("keydown.escape");
      await flushPromises();
      expect(view.emitted("rename")).toHaveLength(1);
      expect(view.text()).toContain("Laptop");
    } finally { view.unmount(); }
  });

  it("copies the address the phone opens", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({
      web_enabled: true, local_ip: "192.168.1.7", port: 8765,
    }));
    const view = mountView(storeStub(), { companionUrl: "http://192.168.1.7:8765/mobile.html?token=abc" });
    try {
      await flushPromises();
      expect(view.get(".overview-address code").text()).toBe("192.168.1.7:8765");
      await view.get(".overview-address button").trigger("click");
      await flushPromises();
      // The token-carrying address when the service is up, because that is the
      // one that works without the phone being told a token.
      expect(bridge.copyText).toHaveBeenCalledWith("http://192.168.1.7:8765/mobile.html?token=abc");
    } finally { view.unmount(); }

    vi.mocked(bridge.overview).mockResolvedValue(overview({ web_enabled: false, local_ip: "192.168.1.7" }));
    const hidden = mountView();
    try {
      await flushPromises();
      expect(hidden.find(".overview-address").exists()).toBe(false);
    } finally { hidden.unmount(); }
  });

  it("lists recent activity, copies on a click and leads on from the card", async () => {
    const now = Math.floor(Date.now() / 1000);
    vi.mocked(bridge.overview).mockResolvedValue(overview({ recent_items: [
      { ...clip("a"), preview: "https://example.com", content_type: "URL", timestamp: now - 30, pinned: true },
      { ...clip("b"), preview: "", content_type: "IMAGE", timestamp: now - 600 },
    ] }));
    const view = mountView();
    try {
      await flushPromises();
      const rows = view.findAll(".overview-feed-row");
      expect(rows).toHaveLength(2);
      expect(rows[0].text()).toContain("https://example.com");
      expect(rows[0].text()).toContain("刚刚");
      expect(rows[0].find('[aria-label="已置顶"]').exists()).toBe(true);
      // A clip with no text of its own still says something, rather than
      // showing a row with nothing in it.
      expect(rows[1].text()).toContain("（无内容）");
      expect(rows[1].text()).toContain("10 分钟前");
      // A row copies — the clip itself, through the history page's own action —
      // rather than walking the reader to the page they were already looking at
      // the newest of.  The card carries the way there.
      await rows[1].trigger("click");
      await flushPromises();
      expect(view.emitted("history")).toBeUndefined();
      expect(bridge.copyHistory).toHaveBeenCalledWith("b");
      expect(view.get(".overview-card-head button").text()).toBe("查看全部");
      await view.get(".overview-card-head button").trigger("click");
      expect(view.emitted("history")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("hands the actions it does not own up to the window", async () => {
    vi.mocked(bridge.overview).mockResolvedValue(overview({ connected_names: ["Phone", "Tablet"] }));
    const view = mountView(storeStub(), { companionOn: true });
    try {
      await flushPromises();
      const names = view.findAll(".overview-chip--link");
      expect(names.map(chip => chip.text())).toEqual(["Phone", "Tablet"]);
      await names[0].trigger("click");
      expect(view.emitted("devices")).toHaveLength(1);

      // The phone service's switch is the window's to answer: the page reports
      // the click and shows what the window's read says, which is still on.
      const remote = view.get('[aria-label="远程访问"]');
      await remote.setValue(false);
      expect(view.emitted("companion")).toEqual([[false]]);
      expect((remote.element as HTMLInputElement).checked).toBe(true);

      await view.findAll("button").find(button => button.text() === "显示二维码")!.trigger("click");
      await view.findAll("button").find(button => button.text() === "发送链接到设备")!.trigger("click");
      expect(view.emitted("qr")).toHaveLength(1);
      expect(view.emitted("send-url")).toHaveLength(1);
    } finally { view.unmount(); }
  });

  it("stops reading once the page is off screen, and while the window is hidden", async () => {
    vi.useFakeTimers();
    vi.mocked(bridge.overview).mockClear().mockResolvedValue(overview());
    const view = mountView();
    try {
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(1);
      // A report nobody is looking at is a read run for nobody.
      Object.defineProperty(document, "hidden", { value: true, configurable: true });
      vi.advanceTimersByTime(20000);
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(1);
      Object.defineProperty(document, "hidden", { value: false, configurable: true });
      vi.advanceTimersByTime(5000);
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(2);
      view.unmount();
      // A read that resolves after the page is gone has nowhere to go.
      vi.advanceTimersByTime(20000);
      await flushPromises();
      expect(bridge.overview).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("says the page is unavailable rather than pretending it read something", async () => {
    vi.mocked(bridge.overview).mockClear().mockResolvedValue(overview());
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
      expect(failed.text()).toContain("概览暂不可用");
      expect(failed.text()).toContain("sidecar is not answering");
    } finally { failed.unmount(); }
  });

  it("tells the reader where it is rather than claiming a verdict it does not have", async () => {
    // A window that has just opened has no status at all, and the shell calls
    // that 正在启动.  Reporting "同步引擎未运行" for it is a verdict the page does
    // not have — and it is wrong: the engine is coming up.
    vi.mocked(bridge.overview).mockClear();
    const booting = storeStub();
    delete (booting.state as { status?: unknown }).status;
    const view = mountView(booting, { enabled: false });
    try {
      await flushPromises();
      expect(view.text()).toContain("正在读取概览");
      expect(view.text()).not.toContain("同步引擎未运行");
    } finally { view.unmount(); }

    // A sidecar that died leaves no status either, but the shell's own chip
    // already says it cannot connect: a spinner under it that never resolves is
    // the one thing a reader cannot act on, so the error ends the claim.
    const down = storeStub();
    delete (down.state as { status?: unknown }).status;
    down.state.error = { code: "NATIVE_HOST_REQUIRED", message: "请在 ClipSync 桌面窗口中打开", retryable: false };
    const failed = mountView(down, { enabled: false });
    try {
      await flushPromises();
      expect(failed.text()).toContain("概览暂不可用");
      expect(failed.text()).not.toContain("正在读取概览");
    } finally { failed.unmount(); }
  });
});
