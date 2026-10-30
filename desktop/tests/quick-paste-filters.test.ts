import { describe, expect, it } from "vitest";
import type { HistoryItem } from "../src/api/types";
import {
  filterPasteRows, isLocalRow, matchesPasteDevice, matchesPasteTime, pasteDeviceChoices,
} from "../src/lib/quick-paste-filters";

/** A row as the overlay receives one; only the fields the filters read matter. */
function row(overrides: Partial<HistoryItem> = {}): HistoryItem {
  return {
    id: "h1", timestamp: 1, content_type: "TEXT", pinned: false, preview: "x",
    source_name: "", source_app: "", source_title: "", paste_count: 0,
    ...overrides,
  };
}

/** Noon on a fixed local day, in milliseconds.  Local rather than UTC so
 *  "today" means the reader's today in whatever zone the suite runs in. */
const NOON = new Date(2026, 4, 20, 12, 0, 0).getTime();
const at = (msAgo: number) => (NOON - msAgo) / 1000;

describe("quick-paste filters", () => {
  it("tells a local row from a received one by the source device", () => {
    expect(isLocalRow(row({ source_device: "" }))).toBe(true);
    expect(isLocalRow(row())).toBe(true);
    expect(isLocalRow(row({ source_device: "peer-1" }))).toBe(false);
  });

  it("keeps the windows widening: hour, today, week", () => {
    expect(matchesPasteTime(at(30 * 60 * 1000), "hour", NOON)).toBe(true);
    expect(matchesPasteTime(at(90 * 60 * 1000), "hour", NOON)).toBe(false);
    expect(matchesPasteTime(at(90 * 60 * 1000), "today", NOON)).toBe(true);
    expect(matchesPasteTime(at(30 * 60 * 60 * 1000), "today", NOON)).toBe(false);
    expect(matchesPasteTime(at(30 * 60 * 60 * 1000), "week", NOON)).toBe(true);
    expect(matchesPasteTime(at(8 * 24 * 60 * 60 * 1000), "week", NOON)).toBe(false);
    expect(matchesPasteTime(1, "hour", NOON)).toBe(false);
  });

  it("reads 今天 as the calendar day, not the last 24 hours", () => {
    // Ten minutes ago, but on yesterday's date: a row drawn as "23:50" belongs
    // to yesterday and a chip labelled 今天 that shows it would be lying.
    const justAfterMidnight = new Date(2026, 4, 20, 0, 9, 0).getTime();
    const yesterdayLate = new Date(2026, 4, 19, 23, 59, 0).getTime() / 1000;
    expect(matchesPasteTime(yesterdayLate, "today", justAfterMidnight)).toBe(false);
    // It is inside the last hour, though: the two filters answer different
    // questions and the calendar one is the narrower here.
    expect(matchesPasteTime(yesterdayLate, "hour", justAfterMidnight)).toBe(true);
  });

  it("accepts 全部 and drops a row with no readable stamp from a window", () => {
    expect(matchesPasteTime(0, "all", NOON)).toBe(true);
    expect(matchesPasteTime(0, "week", NOON)).toBe(false);
  });

  it("matches the device filter against local rows and one peer id", () => {
    expect(matchesPasteDevice(row({ source_device: "" }), "all")).toBe(true);
    expect(matchesPasteDevice(row({ source_device: "" }), "local")).toBe(true);
    expect(matchesPasteDevice(row({ source_device: "peer-1" }), "local")).toBe(false);
    expect(matchesPasteDevice(row({ source_device: "peer-1" }), "peer-1")).toBe(true);
    expect(matchesPasteDevice(row({ source_device: "peer-1" }), "peer-2")).toBe(false);
  });

  it("filters a page with both filters at once, without reordering it", () => {
    const page = [
      row({ id: "a", timestamp: at(60 * 1000), source_device: "" }),
      row({ id: "b", timestamp: at(60 * 1000), source_device: "peer-1" }),
      row({ id: "c", timestamp: at(30 * 60 * 60 * 1000), source_device: "" }),
    ];
    expect(filterPasteRows(page, { time: "hour", device: "all" }, NOON).map((item) => item.id))
      .toEqual(["a", "b"]);
    expect(filterPasteRows(page, { time: "all", device: "local" }, NOON).map((item) => item.id))
      .toEqual(["a", "c"]);
    expect(filterPasteRows(page, { time: "hour", device: "peer-1" }, NOON).map((item) => item.id))
      .toEqual(["b"]);
  });

  it("offers the devices the page contains, once each and named", () => {
    const choices = pasteDeviceChoices([
      row({ id: "a", source_device: "" }),
      row({ id: "b", source_device: "peer-1", source_name: "Laptop" }),
      row({ id: "c", source_device: "peer-1", source_name: "Laptop" }),
      row({ id: "d", source_device: "peer-2", source_name: "" }),
    ]);
    expect(choices).toEqual([
      { id: "all", name: "", local: false },
      { id: "local", name: "", local: true },
      { id: "peer-1", name: "Laptop", local: false },
      // A peer with no name is named by its id: an unnamed option would read as
      // the same empty row as the local one.
      { id: "peer-2", name: "peer-2", local: false },
    ]);
  });

  it("does not advertise the local option when every row came from elsewhere", () => {
    const choices = pasteDeviceChoices([row({ source_device: "peer-1", source_name: "Laptop" })]);
    expect(choices.map((choice) => choice.id)).toEqual(["all", "peer-1"]);
  });
});
