<script setup lang="ts">
import { ref } from "vue";
import { X } from "@lucide/vue";
import { t } from "../i18n";

/**
 * The window's dialogs, one shape.
 *
 * Thirty-three `<dialog>` elements had thirty-three headers, and nineteen of
 * them had no close button at all: a reader had to work out from the buttons at
 * the bottom which one meant "never mind", and that answer differed per dialog
 * (取消 / 关闭 / 继续编辑 / 稍后).  The × is not a new control — the other
 * fourteen already carried `icon-button modal-close` — it is the one that was
 * missing, in the same place, on every dialog.
 *
 * What the wrapper owns, and why each part is not left to the caller:
 *
 * - **The heading and its id.** `aria-labelledby` pointing at nothing is worse
 *   than no label at all: the dialog reads as "dialog" and the reader cannot
 *   tell which one.  Taking the title as a prop means the id and the element
 *   cannot come apart, and a dialog cannot be written without a heading.
 * - **The close button.** Optional (`:closable="false"`) only where closing
 *   without answering is wrong — the certificate alert, whose two buttons are
 *   the whole point.  Everything else gets one.
 * - **The `@close` / `@cancel` pair.** Native `<dialog>` has two ways out (the
 *   X/Esc path is `cancel`, then `close`), and a dialog that listened to only
 *   one of them kept its flag set after Esc.  Both are forwarded, so the caller
 *   writes one handler and gets both.
 *
 * The body and the footer are slots: `default` and `actions`, the latter
 * rendered inside `.modal-actions` so the row's wrapping and spacing stay the
 * stylesheet's business.  A dialog that needs something else in its footer
 * still can — the slot is ordinary markup.
 *
 * `showModal()` and `close()` are exposed rather than the element, because that
 * is the whole of what `useDialog` and the two hand-driven dialogs do with it,
 * and exposing the element would invite a second way to open a dialog that has
 * already been opened (which throws).
 */
// `withDefaults` rather than `?` alone: Vue casts an absent Boolean prop to
// `false`, so `closable?: boolean` would have meant "no close button unless
// asked for" — the opposite of the point, and the reason the first render of
// this component drew no ×.
const props = withDefaults(
  defineProps<{
    /** The `<h2>`'s id, which `aria-labelledby` points at. */
    headingId: string;
    title: string;
    /** The wider variant, for dialogs holding a list or a code block. */
    wide?: boolean;
    /** False for a dialog whose two buttons are the answer. */
    closable?: boolean;
    /** Esc is refused: the reader must choose one of the buttons. */
    preventCancel?: boolean;
  }>(),
  { wide: false, closable: true, preventCancel: false },
);

const emit = defineEmits<{ close: []; cancel: [] }>();

const dialog = ref<HTMLDialogElement | null>(null);

function showModal() {
  // `showModal()` on an already-open dialog throws, and `useDialog` can be
  // reached twice by a flag that was set from two events in one tick.
  if (dialog.value && !dialog.value.open) dialog.value.showModal();
}

function close() {
  // No `open` guard, deliberately: `close()` on a closed dialog does nothing,
  // while `showModal()` on an open one throws.  Guarding the harmless call
  // would also make the component behave differently from the element it
  // replaces, which is the one thing a migration must not do.
  dialog.value?.close();
}

function onCancel(event: Event) {
  emit("cancel");
  if (props.preventCancel) event.preventDefault();
}

defineExpose({ showModal, close });
</script>

<template>
  <dialog
    ref="dialog"
    :class="['modal', { 'modal--wide': wide }]"
    :aria-labelledby="headingId"
    @close="emit('close')"
    @cancel="onCancel"
  >
    <h2 :id="headingId">{{ title }}</h2>
    <button
      v-if="closable"
      type="button"
      class="icon-button modal-close"
      :aria-label="t('关闭')"
      :title="t('关闭')"
      @click="close"
    >
      <X :size="18" />
    </button>
    <slot />
    <div v-if="$slots.actions" class="modal-actions"><slot name="actions" /></div>
  </dialog>
</template>
