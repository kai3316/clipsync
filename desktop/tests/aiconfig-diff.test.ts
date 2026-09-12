import { describe, expect, it } from "vitest";
import { aiCompareState, aiDiffCounts, aiEntryKey, aiMtimeMs, buildAiLocalIndex } from "../src/lib/aiconfig-diff";

/** A local file entry as `/api/aiconfig/local` reports it. */
function local(rel_path: string, sha256: string, mtime: number, extra: Record<string, any> = {}) {
  return { tool: "custom", root: "", rel_path, sha256, size: 1, mtime, is_dir: false, ...extra };
}

const index = buildAiLocalIndex([
  local("same.md", "aaaa", 100),
  local("local-newer.md", "bbbb", 200),
  local("remote-newer.md", "cccc", 100),
  local("x.md", "dddd", 100, { tool: "claude", root: "skills" }),
  local("x.md", "eeee", 100, { tool: "claude", root: "commands" }),
  local("only-a-file.md", "ffff", 100, { is_dir: true, sha256: "" }),
]);

describe("AI config version diff", () => {
  it("reads a version state off the peer's row, and stays quiet where it cannot", () => {
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa" })).toBe("same");
    // Same content hash is "same" whatever the clocks say.
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa", mtime: 999 })).toBe("same");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "local-newer.md", sha256: "zz", mtime: 150 })).toBe("local_newer");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "remote-newer.md", sha256: "zz", mtime: 150 })).toBe("remote_newer");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "absent.md", sha256: "zz" })).toBe("missing");
  });

  it("compares a v3 row under its own root, never against a sibling root's file", () => {
    // Both roots hold x.md with different content, so a path-only match would
    // compare the wrong pair and could report "same" for two different files.
    expect(aiCompareState(index, { tool: "claude", root: "skills", rel_path: "x.md", sha256: "dddd" })).toBe("same");
    expect(aiCompareState(index, { tool: "claude", root: "commands", rel_path: "x.md", sha256: "dddd", mtime: 200 })).toBe("remote_newer");
  });

  it("falls back to matching by path for peers that name no usable root", () => {
    // A v2 peer sends no root at all.
    expect(aiCompareState(index, { tool: "claude", rel_path: "x.md", sha256: "dddd" })).toBe("same");
    // A legacy peer sends a per-device root_index, which means nothing here, and
    // spells the path `path`.
    expect(aiCompareState(index, { root_index: 0, path: "same.md", sha256: "aaaa" }, true)).toBe("same");
    expect(aiCompareState(index, { tool: "custom", root: "commands", rel_path: "same.md", sha256: "aaaa" }, true)).toBe("same");
  });

  it("reports nothing rather than guessing when half its input is missing", () => {
    // No local walk yet: "missing" here would flash on every row.
    expect(aiCompareState(null, { tool: "custom", root: "", rel_path: "absent.md", sha256: "zz" })).toBeNull();
    expect(aiCompareState(index, null)).toBeNull();
    // A folder carries no content hash, so it has no version to report.
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "only-a-file.md", is_dir: true })).toBeNull();
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "only-a-file.md" })).toBe("missing");
  });

  it("sorts a file with no mtime as the older side instead of crashing", () => {
    expect(aiMtimeMs(undefined)).toBe(-1);
    expect(aiMtimeMs("")).toBe(-1);
    expect(aiMtimeMs("not a number")).toBe(-1);
    expect(aiMtimeMs(1500)).toBe(1_500_000);
    // The wire carries seconds, but a millisecond value must not be read as a
    // timestamp tens of thousands of years out.
    expect(aiMtimeMs(1_500_000_000_000)).toBe(1_500_000_000_000);
    // A row that arrives without one is the older side, never the newer one:
    // a missing timestamp is not evidence of an edit.
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "remote-newer.md", sha256: "zz" })).toBe("local_newer");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "local-newer.md", sha256: "zz" })).toBe("local_newer");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "same.md", sha256: "zz", mtime: 50 })).toBe("local_newer");
    expect(aiCompareState(index, { tool: "custom", root: "", rel_path: "same.md", sha256: "zz", mtime: 1_500_000_000_000 })).toBe("remote_newer");
  });

  it("counts only the files that differ, and keys one entry one way", () => {
    const counts = aiDiffCounts(index, [
      { tool: "custom", root: "", rel_path: "same.md", sha256: "aaaa" },
      { tool: "custom", root: "", rel_path: "local-newer.md", sha256: "zz", mtime: 150 },
      { tool: "custom", root: "", rel_path: "remote-newer.md", sha256: "zz", mtime: 150 },
      { tool: "custom", root: "", rel_path: "absent.md", sha256: "zz" },
      { tool: "custom", root: "", rel_path: "a-folder", is_dir: true },
    ]);
    expect(counts).toEqual({ missing: 1, local_newer: 1, remote_newer: 1, total: 3 });
    expect(aiDiffCounts(index, [])).toEqual({ missing: 0, local_newer: 0, remote_newer: 0, total: 0 });
    expect(aiDiffCounts(null, [{ tool: "custom", rel_path: "absent.md" }]).total).toBe(0);
    expect(aiEntryKey({ tool: "claude", root: "skills", rel_path: "x.md" })).toBe(aiEntryKey({ tool: "claude", root: "skills", rel_path: "x.md" }));
    expect(aiEntryKey({ tool: "claude", root: "skills", rel_path: "x.md" })).not.toBe(aiEntryKey({ tool: "claude", root: "commands", rel_path: "x.md" }));
    // A row with no tool is the custom-profile root, as the collector reports it.
    expect(aiEntryKey({ rel_path: "x.md" })).toBe("custom::x.md");
  });
});
