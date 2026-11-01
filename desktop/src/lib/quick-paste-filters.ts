import type { HistoryItem } from "../api/types";

/** How the host orders the overlay's one page of rows. */
export type PasteSort = "newest" | "oldest";

/** How far back the panel shows rows.  The host still sends its 20 newest (or
 *  oldest); these narrow them on this side, because "today" is a calendar
 *  question the sidecar does not have a clock for in the history query. */
export type PasteTimeFilter = "all" | "hour" | "today" | "week";

/** Which machine a clip came from: `all`, `local` (this machine), or a
 *  `source_device` id for one remote device. */
export type PasteDeviceFilter = "all" | "local" | string;

const HOUR_MS = 60 * 60 * 1000;
const WEEK_MS = 7 * 24 * HOUR_MS;

/** Whether the row was recorded here rather than received from a peer. */
export function isLocalRow(item: HistoryItem): boolean {
  return !String(item.source_device || "").trim();
}

/** Whether a row falls inside the time window.
 *
 * `timestamp` is seconds (the history DTO's unit); `now` is milliseconds, so a
 * case can put the clock where it needs it.  A row with no readable stamp is
 * out of every window except 全部: it cannot be shown to be inside one. */
export function matchesPasteTime(timestamp: number, filter: PasteTimeFilter, now = Date.now()): boolean {
  if (filter === "all") return true;
  const seconds = Number(timestamp) || 0;
  if (!seconds) return false;
  const at = seconds * 1000;
  if (filter === "hour") return now - at <= HOUR_MS;
  if (filter === "week") return now - at <= WEEK_MS;
  // 今天 is the reader's calendar day, not "the last 24 hours": the row's own
  // stamp is drawn as a clock time, and a clip from 23:50 yesterday is not
  // "today" even when it is only ten minutes ago.
  const rowDate = new Date(at);
  const today = new Date(now);
  return rowDate.getFullYear() === today.getFullYear()
    && rowDate.getMonth() === today.getMonth()
    && rowDate.getDate() === today.getDate();
}

/** Whether the row came from the selected machine. */
export function matchesPasteDevice(item: HistoryItem, filter: PasteDeviceFilter): boolean {
  if (filter === "all") return true;
  if (filter === "local") return isLocalRow(item);
  return String(item.source_device || "").trim() === filter;
}

/** The rows the panel draws: the host's page, narrowed by the two client-side
 *  filters.  Order is the host's and is not touched here — `sort` is passed to
 *  it because reordering one already-cut page cannot show older rows. */
export function filterPasteRows(
  items: HistoryItem[],
  filters: { time: PasteTimeFilter; device: PasteDeviceFilter },
  now = Date.now(),
): HistoryItem[] {
  return items.filter((item) =>
    matchesPasteTime(item.timestamp, filters.time, now)
    && matchesPasteDevice(item, filters.device));
}

/** One device entry for the filter, as the component draws it. */
export interface PasteDeviceChoice {
  id: PasteDeviceFilter;
  /** `source_name`, or the id for a peer whose name is missing.  Empty for
   *  `all`; the local option has a name only in the component's own words. */
  name: string;
  local: boolean;
}

/** The device options the current result set can offer.
 *
 * Built from the host's page, not the filtered one: narrowing the time window
 * must not make the device that produced a row disappear from the list the
 * reader would use to find it again.  The local option is offered only when a
 * local row is present, so a page of received clips does not advertise an
 * empty choice. */
export function pasteDeviceChoices(items: HistoryItem[]): PasteDeviceChoice[] {
  const choices: PasteDeviceChoice[] = [{ id: "all", name: "", local: false }];
  if (items.some(isLocalRow)) choices.push({ id: "local", name: "", local: true });
  const seen = new Map<string, string>();
  for (const item of items) {
    const id = String(item.source_device || "").trim();
    if (!id || seen.has(id)) continue;
    seen.set(id, String(item.source_name || "").trim() || id);
  }
  for (const [id, name] of seen) choices.push({ id, name, local: false });
  return choices;
}
