import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import TransfersView from "../src/components/TransfersView.vue";
import { bridge } from "../src/api/bridge";

vi.mock("../src/api/bridge", () => ({
  bridge: {
    transfers: vi.fn().mockResolvedValue({ active: [
      { id: "incoming", filename: "request.txt", direction: "down", status: "pending", progress: 0 },
      { id: "paused", filename: "half.txt", direction: "up", status: "paused", progress: 50 },
    ], history: [] }),
    transferAction: vi.fn().mockResolvedValue({}),
    cancelAllTransfers: vi.fn().mockResolvedValue({ cancelled: 2 }),
    clearTransferHistory: vi.fn().mockResolvedValue({ cleared: 2 }),
    chooseFile: vi.fn(),
    chooseFolder: vi.fn(),
    chooseFiles: vi.fn(),
    sendFiles: vi.fn(),
  },
}));

/** A peer as the devices list reports one. */
function device(id: string, name: string, overrides: Record<string, unknown> = {}) {
  return {
    id, name, paired: true, connection_state: "online",
    pairing_status: "paired", pairing_code: null, sas: null, ...overrides,
  };
}

describe("transfer controls", () => {
  it("ignores an older snapshot after a newer refresh completes", async () => {
    let resolve!: (value: any) => void;
    vi.mocked(bridge.transfers).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const view = mount(TransfersView);
    try {
      await view.get('[aria-label="刷新传输"]').trigger("click");
      await flushPromises();
      expect(view.text()).toContain("half.txt");
      resolve({ active: [], history: [] });
      await flushPromises();
      expect(view.text()).toContain("half.txt");
    } finally { view.unmount(); }
  });

  it("does not send a file selected after leaving the page", async () => {
    let resolve!: (value: string[]) => void;
    vi.mocked(bridge.chooseFiles).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    vi.mocked(bridge.sendFiles).mockClear();
    const view = mount(TransfersView, { props: { devices: [device("dev-1", "Laptop")] } });
    await flushPromises();
    await view.get("select").setValue("dev-1");
    await view.get("button.primary").trigger("click");
    view.unmount();
    resolve(["C:/test.txt"]);
    await flushPromises();
    expect(bridge.sendFiles).not.toHaveBeenCalled();
  });

  it("does not erase action failures on a successful status refresh", async () => {
    vi.mocked(bridge.transferAction).mockRejectedValueOnce(new Error("peer disconnected"));
    const view = mount(TransfersView);
    try {
      await flushPromises();
      await view.get('[aria-label="接受文件"]').trigger("click");
      await flushPromises();
      await view.get('[aria-label="刷新传输"]').trigger("click");
      await flushPromises();
      expect(view.get('[role="alert"]').text()).toBe("peer disconnected");
    } finally { view.unmount(); }
  });
  it("accepts or rejects incoming requests and resumes paused transfers", async () => {
    const view = mount(TransfersView);
    try {
      await flushPromises();
      expect(view.get('progress[aria-label="half.txt 进度"]').attributes("value")).toBe("50");
      await view.get('[aria-label="接受文件"]').trigger("click");
      await flushPromises();
      expect(bridge.transferAction).toHaveBeenLastCalledWith("accept", "incoming");
      await view.get('[aria-label="拒绝文件"]').trigger("click");
      await flushPromises();
      expect(bridge.transferAction).toHaveBeenLastCalledWith("reject", "incoming");
      await view.get('[aria-label="恢复传输"]').trigger("click");
      await flushPromises();
      expect(bridge.transferAction).toHaveBeenLastCalledWith("resume", "paused");
      expect(view.find('[aria-label="暂停传输"]').exists()).toBe(false);
    } finally { view.unmount(); }
  });

  it("opens and reveals a received file from the history", async () => {
    const page = { active: [], history: [
      { id: "done", filename: "report.pdf", direction: "down", status: "completed", progress: 100, path: "C:/recv/report.pdf" },
      { id: "sent", filename: "out.txt", direction: "up", status: "completed", progress: 100, path: "C:/src/out.txt" },
    ] };
    // Two snapshots: the mount poll and the refresh each action performs.
    vi.mocked(bridge.transfers).mockResolvedValueOnce(page as any).mockResolvedValueOnce(page as any);
    const view = mount(TransfersView);
    try {
      await flushPromises();
      // Only the received row offers open/reveal.
      expect(view.findAll('[aria-label="打开文件"]')).toHaveLength(1);
      await view.get('[aria-label="打开文件"]').trigger("click");
      await flushPromises();
      expect(bridge.transferAction).toHaveBeenLastCalledWith("open", "done");
      await view.get('[aria-label="打开所在文件夹"]').trigger("click");
      await flushPromises();
      expect(bridge.transferAction).toHaveBeenLastCalledWith("reveal", "done");
    } finally { view.unmount(); }
  });

  it("cancels every active transfer in one call and reports how many went", async () => {
    const view = mount(TransfersView);
    try {
      await flushPromises();
      // Scoped to the running card: the history card's header carries a
      // destructive button of its own, and a bare `button` would be whichever of
      // the two happens to come first in the document.  Both live in a card
      // header now — 全部取消 on the list it cancels, 清除传输历史 on the history
      // it clears — which is why the scope is the card rather than the bar.
      await view.get(".transfer-list--active button.danger-outline").trigger("click");
      await flushPromises();
      expect(bridge.cancelAllTransfers).toHaveBeenCalledTimes(1);
      // The count comes from the sidecar, which read the live list — not from
      // the rows this window happened to be showing.
      expect(view.get(".bulk-status").text()).toBe("已取消 2 个传输");
    } finally { view.unmount(); }
  });

  it("offers no cancel-all when nothing is running", async () => {
    // Shared module mock: the previous test's call would otherwise be counted.
    vi.mocked(bridge.cancelAllTransfers).mockClear();
    vi.mocked(bridge.transfers).mockResolvedValueOnce({ active: [], history: [] } as any);
    const view = mount(TransfersView);
    try {
      await flushPromises();
      expect(view.get(".transfer-list--active button.danger-outline").attributes("disabled")).toBeDefined();
      expect(bridge.cancelAllTransfers).not.toHaveBeenCalled();
    } finally { view.unmount(); }
  });

  it("reports a cancel-all that could not run instead of pretending it worked", async () => {
    vi.mocked(bridge.cancelAllTransfers).mockRejectedValueOnce(new Error("runtime is stopping"));
    const view = mount(TransfersView);
    try {
      await flushPromises();
      await view.get(".transfer-list--active button.danger-outline").trigger("click");
      await flushPromises();
      expect(view.get('[role="alert"]').text()).toBe("runtime is stopping");
      expect(view.get(".transfer-list--active button.danger-outline").attributes("disabled")).toBeUndefined();
    } finally { view.unmount(); }
  });

  it("asks before clearing the transfer history, then reports what the sidecar deleted", async () => {
    // jsdom has no `showModal`, so the shell's dialogs are observed through a
    // stub — the same one the App tests install.  It does not set `open`, which
    // is why the assertions below read the call rather than the attribute.
    const showModal = HTMLDialogElement.prototype.showModal;
    const close = HTMLDialogElement.prototype.close;
    HTMLDialogElement.prototype.showModal = vi.fn();
    HTMLDialogElement.prototype.close = vi.fn();
    vi.mocked(bridge.clearTransferHistory).mockClear();
    vi.mocked(bridge.transfers).mockResolvedValue({
      active: [],
      history: [
        { id: "done-1", filename: "sent.txt", direction: "up", status: "completed", size: 10, timestamp: 1700000000 },
        { id: "done-2", filename: "failed.txt", direction: "up", status: "failed", reason: "timeout", size: 10, timestamp: 1700000001 },
      ],
    } as any);
    const view = mount(TransfersView);
    try {
      await flushPromises();
      // Nothing goes on the click that opens the confirm.
      await view.get(".transfer-list--history .transfer-list-header button").trigger("click");
      await flushPromises();
      expect(HTMLDialogElement.prototype.showModal).toHaveBeenCalledTimes(1);
      expect(bridge.clearTransferHistory).not.toHaveBeenCalled();
      const dialog = view.get("dialog");
      // The count in the confirm is the list's own — what is about to go.
      expect(dialog.text()).toContain("共 2 条传输记录将被移除");
      // Declining leaves the history alone and says nothing.
      await dialog.findAll("button").at(-2)!.trigger("click");
      await flushPromises();
      expect(bridge.clearTransferHistory).not.toHaveBeenCalled();
      expect(HTMLDialogElement.prototype.close).toHaveBeenCalledTimes(1);
      expect(view.find(".bulk-status").exists()).toBe(false);
      // Confirming goes through the sidecar and reports its count, which is the
      // live list's rather than the one this page last read.
      vi.mocked(bridge.clearTransferHistory).mockResolvedValueOnce({ cleared: 1 } as any);
      await view.get(".transfer-list--history .transfer-list-header button").trigger("click");
      await flushPromises();
      await view.get("dialog button.danger").trigger("click");
      await flushPromises();
      expect(bridge.clearTransferHistory).toHaveBeenCalledTimes(1);
      expect(view.get(".bulk-status").text()).toBe("已清除 1 条传输记录");
      expect(HTMLDialogElement.prototype.close).toHaveBeenCalledTimes(2);
    } finally {
      view.unmount();
      HTMLDialogElement.prototype.showModal = showModal;
      HTMLDialogElement.prototype.close = close;
      vi.mocked(bridge.transfers).mockResolvedValue({ active: [], history: [] } as any);
    }
  });

  it("reports file picker errors and releases the busy state", async () => {
    vi.mocked(bridge.chooseFiles).mockRejectedValueOnce(new Error("picker unavailable"));
    const view = mount(TransfersView, { props: { devices: [device("dev-1", "Laptop")] } });
    try {
      await flushPromises();
      await view.get("select").setValue("dev-1");
      await view.get("button.primary").trigger("click");
      await flushPromises();
      expect(view.get('[role="alert"]').text()).toBe("picker unavailable");
      expect(view.get("button.primary").attributes("disabled")).toBeUndefined();
      expect(bridge.sendFiles).not.toHaveBeenCalled();
    } finally { view.unmount(); }
  });

  it("sends several picked files as one transfer, and says it is packaging them", async () => {
    let finish!: (value: any) => void;
    vi.mocked(bridge.chooseFolder).mockClear();
    vi.mocked(bridge.chooseFiles).mockClear()
      .mockResolvedValueOnce(["C:/a.txt", "C:/b.txt", "C:/c.txt"]);
    vi.mocked(bridge.sendFiles).mockClear().mockReturnValueOnce(new Promise(done => { finish = done; }));
    const view = mount(TransfersView, { props: { devices: [device("dev-1", "Laptop")] } });
    try {
      await flushPromises();
      await view.get("select").setValue("dev-1");
      await view.get("button.primary").trigger("click");
      await flushPromises();
      expect(bridge.chooseFolder).not.toHaveBeenCalled();
      // Several picks leave as one archive and one transfer, not N sends, so the
      // line counts what is being packed — and it appears only when there is
      // something to pack, which one file is not.
      expect(view.get(".bulk-status").text()).toBe("正在打包 3 个文件…");
      expect(bridge.sendFiles).toHaveBeenCalledTimes(1);
      expect(bridge.sendFiles).toHaveBeenCalledWith(["C:/a.txt", "C:/b.txt", "C:/c.txt"], "dev-1");
      finish({ transfer_id: "t1" });
      await flushPromises();
      expect(view.find(".bulk-status").exists()).toBe(false);
    } finally { view.unmount(); }
  });

  it("sends nothing when the file picker is cancelled", async () => {
    // A cancelled picker answers with no paths rather than with an error, and an
    // empty pick is not a send of nothing — there is no archive and no transfer.
    vi.mocked(bridge.chooseFiles).mockClear().mockResolvedValueOnce([]);
    vi.mocked(bridge.sendFiles).mockClear();
    const view = mount(TransfersView, { props: { devices: [device("dev-1", "Laptop")] } });
    try {
      await flushPromises();
      await view.get("select").setValue("dev-1");
      await view.get("button.primary").trigger("click");
      await flushPromises();
      expect(bridge.sendFiles).not.toHaveBeenCalled();
      expect(view.find(".bulk-status").exists()).toBe(false);
    } finally { view.unmount(); }
  });

  it("offers only paired, reachable devices as targets", async () => {
    const view = mount(TransfersView, {
      props: {
        devices: [
          device("dev-1", "Laptop"),
          device("dev-2", "Phone", { paired: false }),
          device("dev-3", "Old tower", { connection_state: "offline" }),
        ],
      },
    });
    try {
      await flushPromises();
      const options = view.findAll("select option");
      expect(options.map(option => option.text())).toEqual(["选择接收设备", "Laptop"]);
    } finally { view.unmount(); }
  });

  it("stages a dropped file, sends it to the named device, and hands the paths back", async () => {
    // Shared module mocks: an earlier case's picker call and send would
    // otherwise be counted here.
    vi.mocked(bridge.chooseFiles).mockClear();
    vi.mocked(bridge.sendFiles).mockClear().mockResolvedValueOnce({ transfer_id: "t1" } as any);
    const view = mount(TransfersView, {
      props: {
        devices: [device("dev-1", "Laptop")],
        dropped: ["C:/Users/me/Desktop/report.pdf"],
      },
    });
    try {
      await flushPromises();
      // The drop answered the picker and nothing else: the picker is not
      // reopened, and nothing goes out while no device is named.
      expect(bridge.chooseFiles).not.toHaveBeenCalled();
      expect(view.get(".staged-drop").text()).toContain("已拖入 1 个文件");
      // The name is the path's last segment, because that is what the file is
      // called everywhere else in this page.
      expect(view.get(".staged-drop-names").text()).toBe("report.pdf");
      expect(view.get(".staged-drop button.primary").attributes("disabled")).toBeDefined();
      expect(bridge.sendFiles).not.toHaveBeenCalled();

      await view.get("select").setValue("dev-1");
      await view.get(".staged-drop button.primary").trigger("click");
      await flushPromises();
      expect(bridge.sendFiles).toHaveBeenCalledTimes(1);
      expect(bridge.sendFiles).toHaveBeenCalledWith(["C:/Users/me/Desktop/report.pdf"], "dev-1");
      // Once it has the paths the window must forget them, or coming back to
      // this page would stage a drop that has already been sent.
      expect(view.emitted("dropped")).toHaveLength(1);
      expect(view.find(".staged-drop").exists()).toBe(false);
    } finally { view.unmount(); }
  });

  it("stages a drop that arrives after the page is already open", async () => {
    const view = mount(TransfersView, { props: { devices: [device("dev-1", "Laptop")] } });
    try {
      await flushPromises();
      expect(view.find(".staged-drop").exists()).toBe(false);
      await view.setProps({ dropped: ["C:/late.txt"] });
      await flushPromises();
      expect(view.get(".staged-drop").text()).toContain("late.txt");
    } finally { view.unmount(); }
  });
});

