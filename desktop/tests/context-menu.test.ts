import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";
import ContextMenu from "../src/components/ContextMenu.vue";
import {
  closeContextMenu, contextMenuState, openContextMenu, runContextMenuItem,
  type ContextMenuItem,
} from "../src/lib/context-menu";
import { copyText } from "../src/lib/clipboard";
import { statusMessage } from "../src/lib/status";

vi.mock("../src/api/bridge", () => ({
  bridge: { copyText: vi.fn().mockResolvedValue({ copied: true }) },
}));
// Imported after the mock, so the helper under test reads the mocked bridge.
import { bridge } from "../src/api/bridge";

/** A click at a point, which is all the menu reads off the event. */
function click(x = 10, y = 20): MouseEvent {
  return { clientX: x, clientY: y } as MouseEvent;
}

function item(overrides: Partial<ContextMenuItem> = {}): ContextMenuItem {
  return { id: "a", label: "A", run: () => {}, ...overrides };
}

let menu: VueWrapper | null = null;

/** Mount the menu, and tear it down after the case.  The menu is module state
 * shared by the whole window, so a case that leaves it open would hand the next
 * one a menu it never opened. */
async function open(
  entries: Array<ContextMenuItem | null>,
  at: MouseEvent = click(),
): Promise<VueWrapper> {
  menu = mount(ContextMenu, { attachTo: document.body });
  openContextMenu(at, entries);
  await flushPromises();
  return menu;
}

/** Which entry the keyboard cursor is drawn on, or -1 for none. */
function focusedIndex(view: VueWrapper): number {
  return view.findAll(".context-menu-item")
    .findIndex((node) => node.classes().includes("is-focused"));
}

afterEach(() => {
  menu?.unmount();
  menu = null;
  closeContextMenu();
  document.body.innerHTML = "";
});

describe("context menu state", () => {
  it("opens at the click and keeps the entries it was given", () => {
    openContextMenu(click(40, 60), [item({ id: "copy", label: "复制" })]);
    expect(contextMenuState().open).toBe(true);
    expect(contextMenuState().x).toBe(40);
    expect(contextMenuState().y).toBe(60);
    expect(contextMenuState().items.map((entry) => entry.id)).toEqual(["copy"]);
  });

  it("does not open when nothing is applicable", () => {
    openContextMenu(click(), [null, null]);
    expect(contextMenuState().open).toBe(false);
  });

  it("closes before running, so an entry that opens a dialog is not painted over it", async () => {
    let openWhenRun: boolean | null = null;
    openContextMenu(click(), [item({ run: () => { openWhenRun = contextMenuState().open; } })]);
    await runContextMenuItem(contextMenuState().items[0]);
    expect(openWhenRun).toBe(false);
    expect(contextMenuState().open).toBe(false);
  });

  it("closes on a disabled entry without running it", async () => {
    const run = vi.fn();
    openContextMenu(click(), [item({ disabled: true, run })]);
    await runContextMenuItem(contextMenuState().items[0]);
    expect(run).not.toHaveBeenCalled();
    expect(contextMenuState().open).toBe(false);
  });
});

describe("context menu component", () => {
  it("clamps the keyboard cursor at both ends rather than wrapping", async () => {
    const view = await open([item({ id: "a" }), item({ id: "b" }), item({ id: "c" })]);
    for (let press = 0; press < 5; press += 1) {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
      await flushPromises();
    }
    expect(focusedIndex(view)).toBe(2);
    for (let press = 0; press < 5; press += 1) {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp" }));
      await flushPromises();
    }
    expect(focusedIndex(view)).toBe(0);
  });

  it("runs the entry under the keyboard cursor on Enter", async () => {
    const run = vi.fn();
    await open([item({ id: "a", label: "A" }), item({ id: "b", label: "B", run })]);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await flushPromises();
    expect(run).toHaveBeenCalledTimes(1);
    expect(contextMenuState().open).toBe(false);
  });

  it("closes on Escape", async () => {
    await open([item()]);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    expect(contextMenuState().open).toBe(false);
  });

  it("closes on a click outside but not on one inside", async () => {
    const view = await open([item()]);
    view.find(".context-menu-item").element.dispatchEvent(
      new MouseEvent("pointerdown", { bubbles: true }));
    await flushPromises();
    expect(contextMenuState().open).toBe(true);
    document.body.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true }));
    await flushPromises();
    expect(contextMenuState().open).toBe(false);
  });

  it("closes when the page scrolls under it", async () => {
    await open([item()]);
    window.dispatchEvent(new Event("scroll"));
    expect(contextMenuState().open).toBe(false);
  });

});

describe("the clipboard helper", () => {
  it("writes the text and reports it", async () => {
    vi.mocked(bridge.copyText).mockClear();
    expect(await copyText("hello")).toBe(true);
    expect(bridge.copyText).toHaveBeenCalledWith("hello");
    expect(statusMessage().value).toBe("已复制！");
  });

  it("refuses text with nothing in it rather than clearing the clipboard", async () => {
    vi.mocked(bridge.copyText).mockClear();
    expect(await copyText("   ")).toBe(false);
    expect(bridge.copyText).not.toHaveBeenCalled();
  });

  it("reports the host's own reason when the call itself fails", async () => {
    vi.mocked(bridge.copyText).mockRejectedValueOnce({ message: "剪贴板被占用" });
    expect(await copyText("hello")).toBe(false);
    expect(statusMessage().value).toBe("剪贴板被占用");
  });
});
