import { ref } from "vue";

/**
 * The window's one status line.
 *
 * It lives outside any component because more than one surface now reports on
 * it: the pages own the actions, but the context menu is mounted once at the
 * root and the clipboard helper is a plain module, so a message raised from
 * either has to reach the footer that draws it.  The footer is the right
 * surface for the same reason the legacy panel's toast was — it is the part of
 * the window that stays on screen, so a report about a row the user has since
 * scrolled past, or a page they have since left, is still readable.
 */

const message = ref("");
let timer: ReturnType<typeof setTimeout> | undefined;

/** How long a report stays up.  Long enough to read a sentence, short enough
 * that it is gone before the next one is worth saying. */
const LIFETIME_MS = 5000;

/** Say something on the status line, replacing whatever was there.
 *
 * Replacing rather than queueing is deliberate: these are reports about what
 * just happened, and the newest is the only one the reader is looking for.  A
 * queue would show them the result of their *previous* click.
 */
export function announce(text: string): void {
  message.value = text;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { message.value = ""; }, LIFETIME_MS);
}

/** The line's current text; empty when nothing has been said recently. */
export function statusMessage() {
  return message;
}

/** Cancel a pending expiry.  The window's own teardown calls this so a timer
 * cannot outlive the page it belongs to — the same reason every other timer in
 * the shell is cleared on unmount. */
export function clearStatus(): void {
  if (timer) clearTimeout(timer);
  timer = undefined;
  message.value = "";
}
