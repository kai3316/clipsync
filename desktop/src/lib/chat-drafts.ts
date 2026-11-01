/* What is being typed in each conversation, held outside the page.

 * The window draws one page at a time, so the chat page is unmounted the moment
 * its tab is left — and a draft map held inside it went with it.  A reader who
 * stepped to the history to check a clip and came back found the box empty,
 * which is the one thing a draft is for: a half-written message is the thing
 * most worth not losing, and it was the one thing leaving the page took.
 *
 * Keyed by conversation, which is the other half of it — one box shared by every
 * conversation carried a half-written message into whichever one was opened
 * next, and being sent from there is the worst thing a draft can do.
 *
 * Capped, because a conversation can end while its draft is still here: a Map
 * iterates in insertion order, so what the cap drops is the one that has not
 * been typed in longest.
 *
 * Reactive, and a `Map` rather than a plain object, for the same reason the
 * window's other bounded caches are: what the reader is typing in is drawn from
 * this, so a write has to be one the box can see.
 */

import { reactive } from "vue";

/** How many half-written messages the window holds on to. */
export const CHAT_DRAFTS_KEPT = 32;

export const chatDrafts = reactive(new Map<string, string>());

/** What is being typed in one conversation, "" for one with nothing in it. */
export function chatDraft(id: string): string {
  return id ? chatDrafts.get(id) || "" : "";
}

/** Remember what is being typed in one conversation — and forget it when the
 * box is emptied, because a draft of nothing is not a draft. */
export function setChatDraft(id: string, value: string): void {
  if (!id) return;
  // Deleted and put back: the insertion order is what the cap below reads, so
  // the conversation just typed in is the last one it would drop.
  chatDrafts.delete(id);
  if (value) chatDrafts.set(id, value);
  while (chatDrafts.size > CHAT_DRAFTS_KEPT) {
    const oldest = chatDrafts.keys().next().value;
    if (oldest === undefined) break;
    chatDrafts.delete(oldest);
  }
}
