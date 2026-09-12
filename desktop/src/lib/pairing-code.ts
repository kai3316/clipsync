/* The internet pairing code, as the field that takes one should show it.
 *
 * The code is generated as `XXXX-XXXX-XXXX` from an alphabet with the
 * look-alikes taken out (`NETPAIR_ALPHABET` in `internal/transport/relay.py`,
 * which is what the sidecar's own decoder accepts).  The field the reader types
 * one into has to agree with that, or the reader is left comparing a code they
 * were shown against a box that renders it differently — the dashes are part of
 * the format, the alphabet has no lowercase in it, and a reader who types the
 * code in the case they were shown it should see the box agree with the screen
 * they are reading it from.
 *
 * So this is the one place the shape of the field is decided: everything the
 * code cannot contain is dropped, what is left is upper-cased and grouped in
 * fours.  A paste of `abcd efgh ijkl`, a paste of `ABCD-EFGH-IJKL` and twelve
 * characters typed one at a time all arrive at the same string.
 *
 * Pure, so the rule can be tested without a window.
 */

/** The code's own alphabet, as the sidecar generates and decodes it. */
export const PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

/** Characters in a code, without its dashes: 4 + 4 + 4. */
export const PAIRING_CODE_CHARS = 12;

/** How the code is grouped, and so where the dashes go. */
const GROUP = 4;

/** What the field shows for what the reader has typed or pasted so far.
 *
 * Never longer than a whole code, and never anything a code cannot hold: the
 * field is a view of the code, and a character the decoder would reject is not
 * part of one.  Silently dropping a character is the risk of that — a reader
 * who typed `I` (not in the alphabet, because it reads as 1) sees nothing
 * appear — so the field carries the alphabet in its placeholder and its title
 * rather than only enforcing it. */
export function formatPairingCode(value: unknown): string {
  const source =
    value === undefined || value === null ? "" : String(value).toUpperCase();
  const kept: string[] = [];
  for (const character of source) {
    if (PAIRING_ALPHABET.includes(character)) kept.push(character);
    if (kept.length === PAIRING_CODE_CHARS) break;
  }
  const groups: string[] = [];
  for (let index = 0; index < kept.length; index += GROUP) {
    groups.push(kept.slice(index, index + GROUP).join(""));
  }
  return groups.join("-");
}

/** Whether the field holds a whole code — twelve characters, whatever the
 * reader has typed of the dashes. */
export function isPairingCodeComplete(value: unknown): boolean {
  return formatPairingCode(value).replace(/-/g, "").length === PAIRING_CODE_CHARS;
}
