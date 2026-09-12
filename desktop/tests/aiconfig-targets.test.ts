import { describe, expect, it } from "vitest";
import { aiItemCount } from "../src/lib/aiconfig-tree";
import { aiEntryKey } from "../src/lib/aiconfig-diff";
import { aiTargets } from "../src/lib/aiconfig-targets";

/** An entry as the inventory sends one. */
function entry(rel_path: string, extra: Record<string, any> = {}) {
  return { tool: "claude", root: "skills", rel_path, is_dir: false, ...extra };
}

/** Everything qualifies — the shape a pull from the card's ticks has, where the
 * reader has already chosen. */
const all = () => true;

/** The targets as `name:count` pairs — the shape every case below is about. */
function shape(entries: unknown, qualifies: (entry: Record<string, any>) => boolean = all) {
  return aiTargets(entries, qualifies)
    .map(target => `${target.rel_path}${target.isDir ? "/" : ""}:${target.items.length}`);
}

describe("AI migration targets", () => {
  it("makes one target of a folder and folds its files into it", () => {
    expect(shape([
      entry("obe-softeng-report/", { is_dir: true }),
      entry("obe-softeng-report/SKILL.md"),
      entry("obe-softeng-report/ref/paper-0.docx"),
      entry("obe-softeng-report/ref/paper-1.docx"),
      entry("settings.json", { root: "config" }),
    ])).toEqual(["obe-softeng-report/:3", "settings.json:1"]);
  });

  it("knows a name is a folder from what is inside it, listed folder or not", () => {
    // The inventory lists the folder itself *and* its files, but a peer may
    // send only the files — the name is still a folder either way.
    expect(aiTargets([entry("a/one.md")], all)[0].isDir).toBe(true);
    expect(aiTargets([entry("a", { is_dir: true }), entry("a/one.md")], all)[0].isDir).toBe(true);
    expect(aiTargets([entry("top.md")], all)[0].isDir).toBe(false);
  });

  it("counts the same items the card counts, and keys them the same way", () => {
    const entries = [
      entry("a/one.md"),
      entry("a/two.md"),
      entry("b/three.md"),
      entry("settings.json", { root: "config" }),
    ];
    const targets = aiTargets(entries, all);
    // The card's number and the wizard's list are one rule, so they cannot
    // disagree — which is the whole point of a folder being one item.
    expect(targets).toHaveLength(aiItemCount(entries));
    expect(targets[2].key).toBe(aiEntryKey(entries[3]));
  });

  it("leaves a name that has nothing to move out of the list", () => {
    const entries = [
      entry("a/one.md"),
      entry("a/two.md"),
      entry("b/three.md"),
    ];
    // `b` is wholly the machine's already: a target the wizard would count and
    // then send nothing for is a target that is not there.
    const onlyA = (item: Record<string, any>) => item.rel_path.startsWith("a/");
    expect(shape(entries, onlyA)).toEqual(["a/:2"]);
    expect(shape(entries, () => false)).toEqual([]);
    // An empty folder has no file to carry, so it is not a target either.
    expect(shape([entry("empty", { is_dir: true })])).toEqual([]);
  });

  it("keeps a folder's files together and each root's folder apart", () => {
    // The same folder name under one tool's two roots is two folders, so two
    // targets — the same rule the tree draws two rows by.
    const twoRoots = [
      entry("foo/x.md", { root: "skills" }),
      entry("foo/y.md", { root: "skills" }),
      entry("foo/z.md", { root: "commands" }),
      entry("foobar/other.md"),
    ];
    const targets = aiTargets(twoRoots, all);
    expect(targets.map(target => [target.rel_path, target.root, target.items.length])).toEqual([
      ["foo", "skills", 2],
      ["foo", "commands", 1],
      ["foobar", "skills", 1],
    ]);
    // A prefix match that ignored the separator would sweep `foobar` into `foo`.
    expect(targets[0].items.map(item => item.rel_path)).toEqual(["foo/x.md", "foo/y.md"]);
    expect(new Set(targets.map(target => target.key)).size).toBe(3);
  });

  it("skips what names no config item, and keeps the order the inventory gave", () => {
    const targets = aiTargets([
      null,
      "not-an-entry",
      entry(""),
      entry("z.md"),
      entry("a.md", { root: "config" }),
      entry("m.md"),
    ], all);
    expect(targets.map(target => target.rel_path)).toEqual(["z.md", "a.md", "m.md"]);
    // A tool the inventory never names is still a tool, keyed the way the tree
    // keys it, rather than dropped or grouped with another.
    expect(aiTargets([{ rel_path: "x.md" }], all)[0]).toMatchObject({ tool: "custom", root: "" });
  });
});
