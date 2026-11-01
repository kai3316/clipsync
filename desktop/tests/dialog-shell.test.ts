import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import AppDialog from "../src/components/AppDialog.vue";

// jsdom has `HTMLDialogElement` but not its two methods, so nothing in this
// suite would open.  The stub is the smallest one that keeps `open` honest —
// the rest of the window's tests replace `showModal` with a spy and never ask
// whether the dialog is really up.
let restore: Array<() => void> = [];
beforeEach(() => {
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  restore = [
    stub(proto, "showModal", function (this: HTMLDialogElement) {
      if (this.open) throw new Error("InvalidStateError");
      this.setAttribute("open", "");
    }),
    stub(proto, "close", function (this: HTMLDialogElement) {
      if (!this.open) return;
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    }),
  ];
});
afterEach(() => {
  for (const undo of restore) undo();
});

function stub(target: Record<string, unknown>, key: string, value: unknown) {
  const had = Object.prototype.hasOwnProperty.call(target, key);
  const before = target[key];
  target[key] = value;
  return () => {
    if (had) target[key] = before;
    else delete target[key];
  };
}

/** The dialog shell, on its own.
 *
 * Thirty-three `<dialog>` elements used to be hand-written, and nineteen of them
 * had no close button: which of the buttons at the bottom meant "never mind"
 * differed per dialog.  These cases pin the four things the wrapper guarantees —
 * a labelled heading, a close control, both ways out (button and Esc), and the
 * one dialog shape that must not be closable — so a future dialog cannot be
 * written without them.
 */
function mountDialog(props: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  return mount(AppDialog, {
    props: { headingId: "x-title", title: "确认", ...props },
    slots: { default: "<p>正文</p>", ...slots },
    attachTo: document.body,
  });
}

describe("AppDialog", () => {
  it("renders the heading the label points at", () => {
    const wrapper = mountDialog();
    const dialog = wrapper.get("dialog");
    expect(dialog.attributes("aria-labelledby")).toBe("x-title");
    // The id lives on the element the title is rendered into: an
    // `aria-labelledby` pointing at nothing reads as an unlabelled dialog.
    expect(wrapper.get("h2").attributes("id")).toBe("x-title");
    expect(wrapper.get("h2").text()).toBe("确认");
    wrapper.unmount();
  });

  it("always carries a close button, and it closes", () => {
    const wrapper = mountDialog();
    const close = wrapper.get(".modal-close");
    expect(close.attributes("aria-label")).toBe("关闭");
    (wrapper.vm as unknown as { showModal: () => void }).showModal();
    expect(wrapper.get("dialog").element.open).toBe(true);
    return close.trigger("click").then(() => {
      expect(wrapper.get("dialog").element.open).toBe(false);
      expect(wrapper.emitted("close")).toBeTruthy();
      wrapper.unmount();
    });
  });

  it("can be the one dialog whose two buttons are the answer", () => {
    // The certificate alert: closing it without answering would leave the
    // device neither trusted nor dropped.
    const wrapper = mountDialog({ closable: false, preventCancel: true });
    expect(wrapper.find(".modal-close").exists()).toBe(false);
    // The Esc route is refused too, or the button would be the only thing
    // holding the answer in place.
    const event = new Event("cancel", { cancelable: true });
    wrapper.get("dialog").element.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(wrapper.emitted("cancel")).toBeTruthy();
    wrapper.unmount();
  });

  it("forwards both ways out, so a caller writes one handler", () => {
    // Native <dialog> has two: `cancel` (Esc) and `close` (anything). A dialog
    // that listened to one of them kept its flag set after the other.
    const wrapper = mountDialog();
    const cancel = new Event("cancel", { cancelable: true });
    wrapper.get("dialog").element.dispatchEvent(cancel);
    expect(cancel.defaultPrevented).toBe(false);
    expect(wrapper.emitted("cancel")).toHaveLength(1);
    wrapper.get("dialog").element.dispatchEvent(new Event("close"));
    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
  });

  it("puts footer content in the row the stylesheet knows", () => {
    const wrapper = mountDialog({}, { actions: "<button>取消</button>" });
    const actions = wrapper.get(".modal-actions");
    expect(actions.text()).toBe("取消");
    // And nothing is drawn when a dialog has no footer: an empty row still
    // takes its gap.
    const bare = mountDialog();
    expect(bare.find(".modal-actions").exists()).toBe(false);
    wrapper.unmount();
    bare.unmount();
  });

  it("does not open a dialog that is already open", () => {
    // `showModal()` on an open dialog throws, and `useDialog` can be reached
    // twice in one tick by a flag set from two events.
    const wrapper = mountDialog();
    const handle = wrapper.vm as unknown as { showModal: () => void };
    handle.showModal();
    expect(() => handle.showModal()).not.toThrow();
    expect(wrapper.get("dialog").element.open).toBe(true);
    wrapper.unmount();
  });

  it("takes the wide shape when asked", () => {
    const wrapper = mountDialog({ wide: true });
    expect(wrapper.get("dialog").classes()).toContain("modal--wide");
    wrapper.unmount();
  });
});
