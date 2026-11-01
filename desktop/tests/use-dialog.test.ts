import { describe, expect, it, vi } from "vitest";
import { nextTick, ref } from "vue";
import { useDialog } from "../src/lib/use-dialog";

describe("useDialog", () => {
  it("opens and closes the element with the flag", async () => {
    const open = ref(false);
    const dialog = useDialog(open);
    const element = { showModal: vi.fn(), close: vi.fn() };
    dialog.value = element as unknown as HTMLDialogElement;

    open.value = true;
    await nextTick();
    await nextTick();
    expect(element.showModal).toHaveBeenCalledTimes(1);

    open.value = false;
    await nextTick();
    await nextTick();
    expect(element.close).toHaveBeenCalledTimes(1);
  });

  it("waits a tick, so a flag set before the element exists still opens it", async () => {
    // The case the twenty-six copies each had to remember: a click sets the
    // flag in the same tick the element is rendered in, so reading the ref
    // immediately finds null.  Without the tick the dialog simply never opens,
    // and only for the first open of each one.
    const open = ref(false);
    const dialog = useDialog(open);
    const element = { showModal: vi.fn(), close: vi.fn() };

    open.value = true;
    dialog.value = element as unknown as HTMLDialogElement; // rendered after the flag
    await nextTick();
    await nextTick();
    expect(element.showModal).toHaveBeenCalledTimes(1);
  });

  it("takes an item ref as well as a boolean one", async () => {
    const target = ref<{ id: string } | null>(null);
    const dialog = useDialog(target);
    const element = { showModal: vi.fn(), close: vi.fn() };
    dialog.value = element as unknown as HTMLDialogElement;

    target.value = { id: "peer" };
    await nextTick();
    await nextTick();
    expect(element.showModal).toHaveBeenCalledTimes(1);

    target.value = null;
    await nextTick();
    await nextTick();
    expect(element.close).toHaveBeenCalledTimes(1);
  });

  it("runs onClose before closing, so the bookkeeping sees the open dialog", async () => {
    const open = ref(true);
    const order: string[] = [];
    const dialog = useDialog(open, () => order.push("onClose"));
    const element = {
      showModal: vi.fn(),
      close: vi.fn(() => order.push("close")),
    };
    dialog.value = element as unknown as HTMLDialogElement;

    open.value = false;
    await nextTick();
    await nextTick();
    expect(order).toEqual(["onClose", "close"]);
  });

  it("does not run onClose when the dialog is being opened", async () => {
    const open = ref(false);
    const onClose = vi.fn();
    const dialog = useDialog(open, onClose);
    dialog.value = {
      showModal: vi.fn(),
      close: vi.fn(),
    } as unknown as HTMLDialogElement;

    open.value = true;
    await nextTick();
    await nextTick();
    expect(onClose).not.toHaveBeenCalled();
  });
});
