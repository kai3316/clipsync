import { reactive, ref, type Component } from "vue";

/**
 * The window's one right-click menu.
 *
 * The legacy panel opened a menu on five kinds of object — a history row, a
 * device card, a chat conversation, a chat message and a transfer record
 * (`context-menu.js`, modes `history-item` / `device` / `chat-session` /
 * `chat-message` / `transfer`).  This is that menu, with the same five
 * surfaces, and it is state rather than a component-instance concern for the
 * same reason it was state in the legacy panel's store: the menu is opened by
 * a row deep in a page and drawn once, at the root, so that it is never
 * clipped by a scrolling list or stacked under a card.
 *
 * A row does not build the menu itself.  Each surface has a factory below —
 * `historyMenu`, `deviceMenu`, and so on — so the wording, the order and the
 * conditions live in one readable place per object, instead of being spread
 * across five templates.
 */

/** One line in the menu. */
export interface ContextMenuItem {
  /** Stable within its menu; the key the tests and the keyboard cursor use. */
  id: string;
  label: string;
  icon?: Component;
  /** Shown right-aligned, e.g. `Ctrl+C`. */
  shortcut?: string;
  /** A tooltip, for the entries whose label cannot hold the whole answer — a
   *  dimmed entry that has to say *why* it is dimmed, in a menu 224px wide that
   *  truncates anything longer than a few words. */
  title?: string;
  /** Drawn in the danger colour, for the one action that destroys something. */
  danger?: boolean;
  /** Shown but not runnable — a row with no id to act on. */
  disabled?: boolean;
  /** A rule above this entry, separating it from the group before it. */
  divider?: boolean;
  /** What clicking it does.  Awaited so the menu cannot be re-entered mid
   * action; the result is discarded, because the entry does not report — the
   * action's own surface does. */
  run: () => unknown;
}

interface ContextMenuState {
  open: boolean;
  /** Viewport coordinates of the click that opened it, before clamping. */
  x: number;
  y: number;
  items: ContextMenuItem[];
}

const state = reactive<ContextMenuState>({ open: false, x: 0, y: 0, items: [] });

/** Which entry the keyboard is on, or -1 when the menu was opened by mouse and
 * nothing has been highlighted yet — the same "enter at the top" the legacy
 * menu used for its roving tabindex. */
const focused = ref(-1);

export function contextMenuState(): ContextMenuState {
  return state;
}

export function contextMenuFocus(): typeof focused {
  return focused;
}

export function closeContextMenu(): void {
  state.open = false;
  state.items = [];
  focused.value = -1;
}

/** Open the menu at a click, dropping any entry that is not applicable.
 *
 * Entries are built with conditions rather than filtered afterwards so a
 * factory can read top to bottom and say exactly which actions a given object
 * offers; this is the one place that catches a factory returning `null` for an
 * inapplicable line. */
export function openContextMenu(event: MouseEvent, items: Array<ContextMenuItem | null>): void {
  const usable = items.filter((item): item is ContextMenuItem => !!item);
  if (!usable.length) return;
  state.items = usable;
  state.x = event.clientX;
  state.y = event.clientY;
  state.open = true;
  focused.value = -1;
}

/** Move the keyboard cursor, wrapping nowhere: the ends clamp, the way the
 * legacy menu's did. */
export function moveContextFocus(delta: number): void {
  if (!state.items.length) return;
  const next = focused.value < 0
    ? (delta > 0 ? 0 : state.items.length - 1)
    : focused.value + delta;
  focused.value = Math.max(0, Math.min(next, state.items.length - 1));
}

/** Run one entry, closing the menu first.
 *
 * Closing before running matters: the actions open dialogs and change pages,
 * and a menu still painted over the result would be the last thing between the
 * reader and what they just asked for.  A disabled entry closes too, because
 * the click was still a click and leaving the menu up reads as a dead button.
 */
export async function runContextMenuItem(item: ContextMenuItem): Promise<void> {
  closeContextMenu();
  if (item.disabled) return;
  await item.run();
}
