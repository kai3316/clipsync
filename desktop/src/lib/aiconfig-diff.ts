/* Which of a peer's config files differ from this machine's, and in which
   direction.

   Ported from the 1.x `internal/web/static/js/aiconfig-helpers.js`, whose rules
   were arrived at one at a time and are all load-bearing — each one below is
   there because the obvious simpler version misreports a real case:

   · Directory entries take no part.  A folder carries no sha256, so comparing
     it would report every folder as differing from every other folder.
   · A v3 entry names its root, so it must match the local file under *that*
     root.  Falling back to a path-only match would compare `commands/x.md`
     against a local `rules/x.md` and call them the same file.  v2 peers send no
     root and legacy peers send a per-device `root_index` that means nothing
     here, so those two still match by path.
   · Missing mtime sorts before everything rather than crashing the comparison:
     an entry without one is not evidence of being newer.
   · An unloaded local index yields `null`, not `missing` — otherwise every row
     would flash "missing" for the moment before the local walk returns.

   Pure functions with no store or Vue dependency, so the states can be tested
   directly instead of through a rendered list. */

export type AiDiffState = "same" | "missing" | "local_newer" | "remote_newer";

export interface AiDiffCounts {
  missing: number;
  local_newer: number;
  remote_newer: number;
  /** missing + local_newer + remote_newer — the files that actually differ. */
  total: number;
}

export interface AiLocalIndex {
  /** v3 local entries by tool:root:rel_path. */
  byKey: Record<string, Record<string, any>>;
  /** Every local file entry by rel_path, for peers that name no usable root. */
  byPath: Record<string, Record<string, any>>;
}

function text(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

/** Identity of one entry.  The root is part of the key because a rel_path is
 * relative to its own root, so two roots of one tool (Claude Code's skills and
 * commands) can each hold a different file at the same rel_path. */
export function aiEntryKey(entry: Record<string, any> | null | undefined): string {
  return `${text(entry?.tool) || "custom"}:${text(entry?.root)}:${text(entry?.rel_path)}`;
}

/** mtime in epoch milliseconds.  The wire carries seconds, but a value past
 * ~year 33658 in seconds is already milliseconds, so both units are accepted;
 * absent or unparseable becomes -1, which is older than any real file. */
export function aiMtimeMs(value: unknown): number {
  if (value === undefined || value === null || value === "") return -1;
  const num = Number(value);
  if (!Number.isFinite(num)) return -1;
  return num > 1e12 ? num : num * 1000;
}

export function buildAiLocalIndex(entries: unknown): AiLocalIndex {
  const byKey: Record<string, Record<string, any>> = {};
  const byPath: Record<string, Record<string, any>> = {};
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!entry || typeof entry !== "object" || entry.is_dir) continue;
    const rel = text(entry.rel_path);
    if (!rel) continue;
    if (!(rel in byPath)) byPath[rel] = entry;
    if (entry.tool) {
      const key = aiEntryKey(entry);
      if (!(key in byKey)) byKey[key] = entry;
    }
  }
  return { byKey, byPath };
}

/** The version state of one remote row against the local index, or `null` when
 * the row cannot be compared (a folder, or no local index yet). */
export function aiCompareState(
  index: AiLocalIndex | null | undefined,
  entry: Record<string, any> | null | undefined,
  legacy = false,
): AiDiffState | null {
  if (!entry || entry.is_dir) return null;
  if (!index) return null;
  // A legacy entry names its file `path`; the modern shapes use `rel_path`.
  const rel = text(entry.rel_path) || text(entry.path);
  const local = (legacy || !entry.root) ? index.byPath[rel] : index.byKey[aiEntryKey(entry)];
  if (!local) return "missing";
  if (text(local.sha256) === text(entry.sha256)) return "same";
  return aiMtimeMs(local.mtime) > aiMtimeMs(entry.mtime) ? "local_newer" : "remote_newer";
}

export function aiDiffCounts(
  index: AiLocalIndex | null | undefined,
  entries: unknown,
  legacy = false,
): AiDiffCounts {
  const counts: AiDiffCounts = { missing: 0, local_newer: 0, remote_newer: 0, total: 0 };
  for (const entry of Array.isArray(entries) ? entries : []) {
    const state = aiCompareState(index, entry, legacy);
    if (state === "missing") counts.missing++;
    else if (state === "local_newer") counts.local_newer++;
    else if (state === "remote_newer") counts.remote_newer++;
  }
  counts.total = counts.missing + counts.local_newer + counts.remote_newer;
  return counts;
}
