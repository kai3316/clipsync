/** How long a search box waits before it asks.
 *
 * One keystroke used to be one round trip and one rebuild of the list, so a
 * five-letter word was five queries and five chances for the rows to be
 * replaced under the reader's eyes while they were still typing.  Waiting for
 * the typing to stop asks once, and nothing about it is slower that a reader
 * can feel.
 *
 * One number for every box that asks a list a question, because the reason is
 * the same in all of them and a per-page answer would only be a disagreement
 * about how long a pause is.  The phone page waits longer on its own — it asks
 * over the network rather than over a pipe to the sidecar on the same machine.
 */
export const SEARCH_DEBOUNCE_MS = 180;
