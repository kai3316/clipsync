/**
 * Split a line of text into the parts a search matched and the parts it did not.
 *
 * Searching the history shortened the list and left every row looking exactly as
 * it had: the reader could see that a row was a match but not *where*, which
 * turned "find the clip with the invoice" into re-reading each of the thirty
 * rows. The phone page has marked its matches since it shipped; this is the same
 * idea for the window.
 *
 * It returns segments rather than markup on purpose. A clipboard's content is
 * arbitrary text that arrived from another machine — it may contain a `<script>`
 * tag or an `<img onerror>` — so nothing here builds HTML, and the template
 * renders these strings into `<mark>` elements it owns. `v-html` on a preview
 * would be an XSS hole with the whole clipboard as its input.
 */
export type HighlightSegment = { text: string; hit: boolean };

/** Case-insensitive, word-wise: a query of two words marks both. */
export function highlightSegments(text: string, query: string): HighlightSegment[] {
  const source = String(text ?? "");
  const terms = String(query ?? "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!source || !terms.length) return [{ text: source, hit: false }];

  const lower = source.toLowerCase();
  const ranges: Array<[number, number]> = [];
  for (const term of terms) {
    let from = 0;
    while (from <= lower.length - term.length) {
      const at = lower.indexOf(term, from);
      if (at < 0) break;
      ranges.push([at, at + term.length]);
      from = at + term.length;
    }
  }
  if (!ranges.length) return [{ text: source, hit: false }];

  // Two terms can overlap ("invo" and "voice"), and a match inside another is one
  // mark on screen: merge before slicing, or the second slice would split the
  // first one's text in the middle.
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: Array<[number, number]> = [];
  for (const [start, end] of ranges) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }

  const segments: HighlightSegment[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) segments.push({ text: source.slice(cursor, start), hit: false });
    segments.push({ text: source.slice(start, end), hit: true });
    cursor = end;
  }
  if (cursor < source.length) segments.push({ text: source.slice(cursor), hit: false });
  return segments;
}
