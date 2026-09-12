import { describe, expect, it } from "vitest";
import { aiItemCount, aiNodeKey, aiTreeGroups } from "../src/lib/aiconfig-tree";

/** An entry as the inventory sends one. */
function entry(rel_path: string, extra: Record<string, any> = {}) {
  return { tool: "claude", root: "skills", rel_path, is_dir: false, size: 1, ...extra };
}

/** Every folder in these entries opened, for the cases about the tree's shape
 * rather than about the fold: a folder is folded until someone opens it, so a
 * case that wants to see inside one has to say so. */
function opened(entries: Record<string, any>[]): Record<string, boolean> {
  const map: Record<string, boolean> = {};
  for (const item of entries) {
    const parts = String(item.rel_path ?? "").replace(/\/+$/, "").split("/");
    for (let index = 1; index <= parts.length; index += 1) {
      if (index === parts.length && !item.is_dir) continue;
      map[aiNodeKey(item.tool ?? "claude", item.root ?? "", parts.slice(0, index).join("/"))] = true;
    }
  }
  return map;
}

/** The rows of the only group, as `depth:name` pairs — the shape every case
 * below is really about. */
function shape(entries: Record<string, any>[], options: Parameters<typeof aiTreeGroups>[1] = {}) {
  const groups = aiTreeGroups(entries, options);
  return groups.flatMap(group => group.rows.map(row =>
    `${row.depth}:${row.label}${row.isDir ? "/" : ""}${row.rootHint ? `@${row.rootHint}` : ""}`));
}

describe("AI config tree", () => {
  it("folds a flat listing back into folders, and lists a folder before its files", () => {
    const entries = [
      entry("a/one.md"),
      entry("a/two.md"),
      entry("b/deep/three.md"),
      entry("top.md"),
    ];
    expect(shape(entries, { expanded: opened(entries) })).toEqual([
      "0:a/",
      "1:one.md",
      "1:two.md",
      "0:b/",
      "1:deep/",
      "2:three.md",
      "0:top.md",
    ]);
  });

  it("sorts groups by the profile order, with a tool the profiles never named last", () => {
    const groups = aiTreeGroups(
      [entry("x.md", { tool: "zeta" }), entry("y.md"), entry("z.md", { tool: "alpha" })],
      { order: ["claude", "alpha"], label: tool => `tool:${tool}` },
    );
    expect(groups.map(group => group.label)).toEqual(["tool:claude", "tool:alpha", "tool:zeta"]);
  });

  it("shows a root hint only where one tool watches more than one root, and only at the top", () => {
    // One root: the paths are unambiguous and a hint on every row would be noise.
    expect(shape([entry("a/one.md")], { expanded: opened([entry("a/one.md")]) }))
      .toEqual(["0:a/", "1:one.md"]);
    // Two roots: a rel_path is relative to its own, so `foo/x.md` means two
    // different files and the reader has to be told which one this is.
    const twoRoots = [
      entry("foo/x.md", { root: "skills" }),
      entry("foo/x.md", { root: "commands" }),
    ];
    expect(shape(twoRoots, { expanded: opened(twoRoots) })).toEqual([
      "0:foo/@skills",
      "1:x.md",
      "0:foo/@commands",
      "1:x.md",
    ]);
  });

  it("folds every folder until it is opened, and keeps the folder itself on screen", () => {
    const entries = [entry("a/one.md"), entry("a/two.md")];
    // A folder starts folded: the list is a list of config items, and what is
    // inside one is that item's own business.
    expect(shape(entries)).toEqual(["0:a/"]);
    const aOpened = { [aiNodeKey("claude", "skills", "a")]: true };
    expect(shape(entries, { expanded: aOpened })).toEqual(["0:a/", "1:one.md", "1:two.md"]);
    // Opening a name that is not a folder changes nothing.
    const leaf = { [aiNodeKey("claude", "skills", "top.md")]: true };
    expect(shape([entry("top.md")], { expanded: leaf })).toEqual(["0:top.md"]);
    // And a folder inside an opened one is folded in turn until it too is
    // opened: opening a skill shows the folders it is made of, not every file
    // in the tree under it.
    expect(shape([entry("a/deep/one.md")], {
      expanded: { [aiNodeKey("claude", "skills", "a")]: true },
    })).toEqual(["0:a/", "1:deep/"]);
    expect(shape([entry("a/deep/one.md")], {
      expanded: {
        [aiNodeKey("claude", "skills", "a")]: true,
        [aiNodeKey("claude", "skills", "a/deep")]: true,
      },
    })).toEqual(["0:a/", "1:deep/", "2:one.md"]);
  });

  it("flattens a search to matching rows at depth 0, across every folder", () => {
    const entries = [entry("a/one.md"), entry("b/two.md"), entry("c/three.md")];
    // A match inside a folded folder would otherwise be invisible, which is
    // why the panel dropped the tree entirely while a search was running.
    const rows = aiTreeGroups(entries, { query: "t" }).flatMap(group => group.rows);
    expect(rows.map(row => [row.depth, row.label, row.isDir])).toEqual([
      [0, "b/two.md", false],
      [0, "c/three.md", false],
    ]);
    // And the search folds case the way the module does everywhere else.
    expect(shape(entries, { query: "THREE" })).toEqual(["0:c/three.md"]);
  });

  it("counts a group's config items, where a folder is one however full it is", () => {
    const groups = aiTreeGroups([
      entry("a/one.md"), entry("a/two.md"), entry("b/three.md"), entry("a", { is_dir: true }),
    ]);
    // Two items, not four: `a` is a folder the reader opens, and the two files
    // inside it are its internals rather than config entries of their own.
    expect(groups[0].itemCount).toBe(2);
    // The listed folder is a row like any other, and the files under it are not
    // listed twice.
    expect(groups[0].rows.filter(row => row.isDir).map(row => row.label)).toEqual(["a", "b"]);
  });

  it("counts one skill as one item, and sums the tools into the card's own count", () => {
    // The shape that made this necessary: one skill folder holding twenty-one
    // files, counted as twenty-one config items and reported as twenty-four
    // when the one file beside it and the other tool's one file were added.
    const entries = [
      entry("settings.json", { root: "settings" }),
      entry("obe-softeng-report/", { is_dir: true }),
      entry("obe-softeng-report/SKILL.md"),
      ...Array.from({ length: 19 }, (_, index) => entry(`obe-softeng-report/ref/paper-${index}.docx`)),
      entry("obe-softeng-report/deep/nested/note.md"),
      entry("config.toml", { tool: "codex", root: "config" }),
    ];
    expect(entries).toHaveLength(24);
    const groups = aiTreeGroups(entries, { order: ["claude", "codex"] });
    expect(groups.map(group => [group.label, group.itemCount])).toEqual([["claude", 2], ["codex", 1]]);
    // The card's own total is the same count over every entry, which is why the
    // header and the numbers beside the tool names can never disagree.
    expect(aiItemCount(entries)).toBe(3);
    // A folder inside a folder is not a second item either: it is inside the
    // skill like everything else under it.
    expect(aiItemCount([entry("a/deep/nested/one.md"), entry("a/deep/two.md")])).toBe(1);
    // A tool that watches two roots holding the same folder name has two items,
    // because it draws two rows with their own root hint.
    expect(aiItemCount([entry("docs/x.md"), entry("docs/y.md", { root: "commands" })])).toBe(2);
    // Nothing to count is nothing, and an entry naming its root itself is the
    // group rather than an item in it.
    expect(aiItemCount([])).toBe(0);
    expect(aiItemCount([entry(""), entry("a")])).toBe(1);
  });

  it("makes one folder of a listed directory and the files under it", () => {
    // The inventory lists the folder itself *and* what is inside it, so the
    // two meet on one node — whichever arrives first — and the row has to be a
    // folder either way.
    for (const entries of [
      [entry("a", { is_dir: true }), entry("a/one.md")],
      [entry("a/one.md"), entry("a", { is_dir: true })],
    ]) {
      const rows = aiTreeGroups(entries, { expanded: opened(entries) }).flatMap(group => group.rows);
      expect(rows.map(row => [row.label, row.isDir])).toEqual([["a", true], ["one.md", false]]);
      expect(rows[0].entry).toMatchObject({ rel_path: "a", is_dir: true });
      // Folded, the folder is the only row either way: the two arrivals meet on
      // one node, so one fold closes both.
      expect(aiTreeGroups(entries, {})
        .flatMap(group => group.rows).map(row => row.label)).toEqual(["a"]);
      expect(aiTreeGroups(entries, { expanded: { [rows[0].key]: true } })
        .flatMap(group => group.rows).map(row => row.label)).toEqual(["a", "one.md"]);
    }
  });
});
