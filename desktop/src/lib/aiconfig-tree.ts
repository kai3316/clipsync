/** The AI-config lists as the legacy panel drew them: grouped by tool, and —
 * with no search running — as a tree of folders with their files under them.
 *
 * The panel's own builder is the reference (`aiconfig-panel.js::groupedRows` /
 * `rowsForGroup`), including its two shapes: a search *flattens* the list to
 * matching rows at depth 0, because a match inside a folded folder would
 * otherwise be invisible; without a search the entries are folded back into
 * nodes and walked, skipping the children of a folder the reader has not
 * opened.  Folded is the state a folder starts in, which is what makes the list
 * a list of config items rather than of every file behind them: a skill is one
 * row until the reader asks to see inside it.
 *
 * Pure on purpose: the fold map, the search text and the tool labels all
 * come in as arguments, so this is testable without a window or a catalog.
 */

/** One entry as the inventory sends it. */
export type AiEntry = Record<string, any>;

/** A folder in the tree.  `entry` is set when the inventory listed the folder
 * itself; a folder that exists only as a path segment has none, and its `path`
 * is still where its files live. */
export interface AiNode {
  key: string;
  tool: string;
  root: string;
  name: string;
  path: string;
  is_dir: boolean;
  entry: AiEntry | null;
  children: AiNode[];
}

/** One drawn row.  A tree row carries its node; a flattened search row carries
 * only its entry, which is why both fields are nullable. */
export interface AiRow {
  key: string;
  label: string;
  depth: number;
  isDir: boolean;
  entry: AiEntry | null;
  node: AiNode | null;
  tool: string;
  root: string;
  /** The root id, shown only where a tool watches more than one root and only
   * on a top-level row: two roots of one tool can hold the same relative path,
   * so without it the reader cannot tell `skills/foo` from `commands/foo`. */
  rootHint: string;
}

export interface AiGroup {
  key: string;
  label: string;
  /** Config items, as the reader counts them: a folder is one item whatever is
   * inside it.  See `aiItemCount`. */
  itemCount: number;
  rows: AiRow[];
}

/** A node's identity: (tool, root, path).  The root is part of it because a
 * rel_path is relative to its own root — `skills/foo/x.md` and
 * `commands/foo/x.md` are two folders, not one. */
export function aiNodeKey(tool: string, root: string, path: string): string {
  return `${tool || "custom"}:${root || ""}:${path || ""}`;
}

/** How many config items a list of entries holds, as the reader counts them.
 *
 * A config item is a top-level name: `settings.json` is one, and a folder is
 * one however much is inside it.  The files under a folder are that folder's
 * own internals — a skill with twenty-one files in it is one skill, not
 * twenty-one config entries — and counting them separately made an inventory of
 * two things read as twenty-four.
 *
 * So this counts the distinct (tool, root, first path segment) among the
 * entries, which is exactly what `rowsForGroup` builds its top level from: the
 * same rel_path under two roots of one tool is two items, because those are two
 * rows with their own root hint.
 *
 * Entries with an empty rel_path name the root itself, which the group is
 * already; the tree skips those, and so does this.  The count is about the
 * inventory in hand, not about the view of it, so neither a search nor a
 * collapsed folder changes it.
 */
export function aiItemCount(entries: AiEntry[]): number {
  const seen = new Set<string>();
  for (const entry of Array.isArray(entries) ? entries : []) {
    const rel = text(entry?.rel_path).replace(/\/+$/, "");
    if (!rel) continue;
    seen.add(aiNodeKey(text(entry?.tool) || "custom", text(entry?.root), rel.split("/")[0]));
  }
  return seen.size;
}

function text(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

/** Rows for one tool's entries. */
function rowsForGroup(
  entries: AiEntry[],
  tool: string,
  expanded: Record<string, boolean>,
  query: string,
): AiRow[] {
  // A tool can watch several roots, and a rel_path is relative to its own.
  const rootsSeen = new Set<string>();
  for (const entry of entries) rootsSeen.add(text(entry?.root));
  const hint = (root: string) => (rootsSeen.size > 1 ? text(root) : "");

  const needle = query.trim().toLowerCase();
  if (needle) {
    const rows: AiRow[] = [];
    for (const entry of entries) {
      if (!text(entry?.rel_path).toLowerCase().includes(needle)) continue;
      rows.push({
        key: aiNodeKey(tool, text(entry?.root), text(entry?.rel_path)),
        label: text(entry?.rel_path),
        depth: 0,
        isDir: !!entry?.is_dir,
        entry,
        node: null,
        tool,
        root: text(entry?.root),
        rootHint: hint(entry?.root),
      });
    }
    rows.sort((a, b) =>
      a.label.localeCompare(b.label) || a.root.localeCompare(b.root));
    return rows;
  }

  const top = new Map<string, AiNode>();
  const roots: AiNode[] = [];
  for (const entry of entries) {
    const rel = text(entry?.rel_path).replace(/\/+$/, "");
    // An empty rel_path names the root itself, which is already the group.
    if (!rel) continue;
    const root = text(entry?.root);
    const parts = rel.split("/");
    let parent: AiNode | null = null;
    let leaf: AiNode | null = null;
    for (let index = 0; index < parts.length; index += 1) {
      const path = parts.slice(0, index + 1).join("/");
      const key = aiNodeKey(tool, root, path);
      // Only the last segment's `is_dir` is settled here; a file's own entry
      // decides that below, because `a/b.md` and a listed folder both make a
      // node at `a`.
      const isDir = index < parts.length - 1 || !!entry?.is_dir;
      if (index === 0) {
        let node = top.get(key);
        if (!node) {
          node = { key, tool, root, name: parts[0], path, is_dir: isDir, entry: null, children: [] };
          top.set(key, node);
          roots.push(node);
        }
        parent = node;
        leaf = node;
        continue;
      }
      let child: AiNode | null = parent!.children.find(candidate => candidate.key === key) ?? null;
      if (!child) {
        child = { key, tool, root, name: parts[index], path, is_dir: isDir, entry: null, children: [] };
        parent!.children.push(child);
      }
      parent = child;
      leaf = child;
    }
    if (leaf) {
      leaf.entry = entry;
      leaf.is_dir = !!entry?.is_dir;
    }
  }

  const rows: AiRow[] = [];
  const walk = (nodes: AiNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({
        key: node.key,
        label: node.name,
        depth,
        isDir: node.is_dir,
        entry: node.entry,
        node,
        tool: node.tool,
        root: node.root,
        rootHint: depth === 0 ? hint(node.root) : "",
      });
      // A folded folder is not walked: that is the whole of what folding does,
      // and it is why a folder with nothing in it is a row like any other.
      if (node.is_dir && expanded[node.key]) walk(node.children, depth + 1);
    }
  };
  walk(roots, 0);
  return rows;
}

/** The whole list: one group per tool, in the profile order the caller gives,
 * with any tool the profiles do not name last and alphabetical among
 * themselves. */
export function aiTreeGroups(
  entries: AiEntry[],
  options: {
    expanded?: Record<string, boolean>;
    query?: string;
    order?: string[];
    label?: (tool: string) => string;
  } = {},
): AiGroup[] {
  const expanded = options.expanded ?? {};
  const query = options.query ?? "";
  const order = options.order ?? [];
  const label = options.label ?? ((tool: string) => tool);

  const grouped = new Map<string, AiEntry[]>();
  for (const entry of Array.isArray(entries) ? entries : []) {
    const tool = text(entry?.tool) || "custom";
    const bucket = grouped.get(tool);
    if (bucket) bucket.push(entry);
    else grouped.set(tool, [entry]);
  }
  const rank = (tool: string) => {
    const index = order.indexOf(tool);
    return index < 0 ? order.length : index;
  };
  return [...grouped.keys()]
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map(tool => {
      const bucket = grouped.get(tool)!;
      return {
        key: tool,
        label: label(tool),
        itemCount: aiItemCount(bucket),
        rows: rowsForGroup(bucket, tool, expanded, query),
      };
    });
}
