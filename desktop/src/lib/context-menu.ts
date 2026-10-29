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
  /** What had focus when it opened, which is the row it belongs to whenever
   *  it was opened from the keyboard.  The menu takes focus when it opens, so
   *  closing it has to hand the focus back — or a reader who opened one with
   *  `Shift+F10` and escaped out of it is left on the document, with Tab
   *  starting again from the top of the window they were in the middle of. */
  opener: HTMLElement | null;
}

const state = reactive<ContextMenuState>({ open: false, x: 0, y: 0, items: [], opener: null });

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
  // `state.opener` survives the close on purpose: the component that drew the
  // menu is the one that can tell whether the focus is still in it, and it is
  // the one that hands it back and clears the field.
}

/** Open the menu at a point, dropping any entry that is not applicable.
 *
 * The point is passed rather than the event because not every way of asking
 * for a menu has one: a right click carries its coordinates, and the keyboard's
 * menu key has none — it hands over the corner of the row it was pressed on.
 * Two fields is the whole of what this needs from either.
 *
 * Entries are built with conditions rather than filtered afterwards so a
 * factory can read top to bottom and say exactly which actions a given object
 * offers; this is the one place that catches a factory returning `null` for an
 * inapplicable line. */
export function openContextMenu(at: { clientX: number; clientY: number }, items: Array<ContextMenuItem | null>): void {
  const usable = items.filter((item): item is ContextMenuItem => !!item);
  if (!usable.length) return;
  state.items = usable;
  state.x = at.clientX;
  state.y = at.clientY;
  // A right click does not move focus, so whatever is focused when one lands
  // is usually the document — which is the honest answer for a menu opened by
  // mouse, and is why the close below can use this without asking how the
  // menu was opened.
  const active = document.activeElement;
  state.opener = active instanceof HTMLElement && active !== document.body ? active : null;
  state.open = true;
  focused.value = -1;
}

/** Whether this key press is the keyboard asking for a row's menu.
 *
 * The two spellings of it on a keyboard: the dedicated menu key, and
 * `Shift+F10`, which is the one that exists on keyboards without it.  A row's
 * menu is reached with the right button otherwise, and a list that can only be
 * acted on with one is a list half the readers cannot act on. */
export function isMenuKey(event: KeyboardEvent): boolean {
  return event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
}

/** Where a menu asked for with the keyboard should open: the row's own corner.
 *
 * A key press carries no pointer coordinates, and a menu opened at (0, 0)
 * would appear in the top-left of the window — on something else entirely.
 * The handler's `currentTarget` is the row the key was pressed on. */
export function keyboardMenuPoint(event: KeyboardEvent): { clientX: number; clientY: number } {
  const rect = (event.currentTarget as HTMLElement | null)?.getBoundingClientRect?.();
  return rect
    ? { clientX: Math.round(rect.left + 12), clientY: Math.round(rect.top + 12) }
    : { clientX: 0, clientY: 0 };
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
