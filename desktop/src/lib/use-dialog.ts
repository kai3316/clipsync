import { nextTick, ref, watch, type Ref } from "vue";

/**
 * Keep a `<dialog>` in step with the boolean that says whether it is open.
 *
 * The window opens dialogs by setting a flag and closes them by clearing it, and
 * every one of them needed the same three lines to make that reach the element:
 * wait a tick for the element to exist, then `showModal()` or `close()`.
 * Twenty-six copies of that is twenty-six places for the `nextTick` to be
 * forgotten — a dialog whose element is not there yet on the first open — and
 * the modal state (top layer, backdrop, focus trap) belongs to the element
 * rather than to the flag, so the two can silently disagree.
 *
 * A truthy value opens; a falsy one closes. That covers the refs holding an
 * item or a path rather than a boolean, which is most of them.
 *
 * The returned ref is what the template's `ref="..."` binds, so this replaces
 * the element ref rather than sitting beside it:
 * ```ts
 * const renameDialog = useDialog(renameTarget);
 * ```
 *
 * *onClose* runs when the dialog is dismissed rather than left open, before the
 * element is closed — for the bookkeeping a dialog owes when it goes away
 * (invalidating a request in flight, clearing a selection).
 */
export function useDialog(
  open: Ref<unknown>,
  onClose?: () => void,
): Ref<HTMLDialogElement | null> {
  const dialog = ref<HTMLDialogElement | null>(null);
  watch(open, async (value) => {
    // The element is created by the same tick's render, so a flag set from a
    // click handler reaches a null here until the DOM has caught up.
    await nextTick();
    if (value) dialog.value?.showModal();
    else {
      onClose?.();
      dialog.value?.close();
    }
  });
  return dialog;
}
