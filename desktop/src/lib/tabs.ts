import { nextTick } from "vue";

/**
 * Arrow keys inside a tab strip.
 *
 * Every strip in the window used to be a row of buttons with `aria-current`:
 * a reader heard each panel name announced as a plain button, three times over,
 * with no way to tell which one was showing — and the only way to change it was
 * a click.  As a tablist, the strip
 * is one stop in the tab order and the arrow keys move inside it — which is what
 * "one stop, many tabs" means to a keyboard, and the reason the buttons are
 * given a roving `tabindex`.
 *
 * Home/End jump to the ends, which is the convention a horizontal tablist
 * follows; anything else is left to the browser (Tab leaves the strip, Enter and
 * Space still activate the focused tab, and the click path is untouched).
 */
export function tabKeydown(
  event: KeyboardEvent,
  ids: readonly string[],
  current: string,
  select: (id: string) => void,
) {
  const index = ids.indexOf(current);
  if (index < 0) return;
  let next = index;
  if (event.key === "ArrowRight") next = (index + 1) % ids.length;
  else if (event.key === "ArrowLeft") next = (index - 1 + ids.length) % ids.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = ids.length - 1;
  else return;
  event.preventDefault();
  const id = ids[next];
  select(id);
  // Focus follows selection: the strip is one tab stop, and these keys are how
  // you move inside it.  The element is found by `data-tab-id`, which the strip
  // sets from the same id this function was handed — the label is translated and
  // cannot be looked up.
  void nextTick(() => {
    const strip = (event.currentTarget as HTMLElement | null)?.parentElement;
    strip?.querySelector<HTMLElement>(`[data-tab-id="${id}"]`)?.focus();
  });
}
