/** The unit a migration moves: one config item, where a folder is one item
 * whatever is inside it.
 *
 * The card's list already counts that way (`aiItemCount` in `aiconfig-tree`),
 * and the wizard has to decide that way too, or the same skill reads as one
 * thing in the inventory and as twenty-one things in the migration.  So the
 * wizard's count, its strategy summary and the pull confirmation are all built
 * from targets, and a target that is a folder carries the files that differ
 * under it.
 *
 * What travels is still the files.  The strategy is decided per file — "this
 * machine does not have `SKILL.md`", "this machine's copy is the newer one" —
 * because that is the granularity the diff and the pull path both work in, and
 * a folder handed to the peer as a folder is expanded by the peer into *every*
 * file under it, identical ones included, which is not what `只补缺失` promises.
 * So a target is the unit of decision and of counting; its `items` are the unit
 * of transfer.
 *
 * Pure on purpose: which entries take part comes in as a function, so these
 * rules are testable without an inventory, a local walk or a window.
 */

import { aiNodeKey } from "./aiconfig-tree";

/** One config item to migrate. */
export interface AiTarget {
  /** The target's identity: `tool:root:name`, built the way the tree builds a
   * config item's key, so the card and the wizard can never disagree about how
   * many items there are.  The root is part of it because a rel_path is relative
   * to its own: one tool watching two roots can hold a folder of the same name
   * under each, and those are two targets. */
  key: string;
  tool: string;
  root: string;
  /** The top-level name — `settings.json`, or a folder's own name, without the
   * trailing slash the inventory uses for a folder. */
  rel_path: string;
  /** Whether that name is a folder.  Known from the inventory when it lists the
   * folder itself, and from any entry listed deeper inside it when it does
   * not. */
  isDir: boolean;
  /** The files this target moves, in inventory order.  Never empty: a name with
   * nothing to move is not a target. */
  items: Array<Record<string, any>>;
}

function text(value: unknown): string {
  return value === undefined || value === null ? "" : String(value);
}

/** The targets among *entries*, folded together by top-level name.
 *
 * *qualifies* decides which entries take part — that is the wizard's strategy
 * question, answered per file because that is what the diff can answer.  An
 * entry that does not qualify is left out, and a name left with no entries at
 * all is not a target: a folder with nothing to move never reaches the reader's
 * list, and neither does an empty one, since a folder carries no content of its
 * own to pull.
 *
 * A directory entry is never a target in its own right — it names a folder, and
 * what moves is files — but it does mark its name as a folder, which is how a
 * folder whose files are every one of them wanted still reads as a folder.
 */
export function aiTargets(
  entries: unknown,
  qualifies: (entry: Record<string, any>) => boolean,
): AiTarget[] {
  const order: string[] = [];
  const targets = new Map<string, AiTarget>();
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!entry || typeof entry !== "object") continue;
    const rel = text(entry.rel_path).replace(/\/+$/, "");
    // An empty rel_path names the root itself, which is the group the row sits
    // in rather than a config item inside it.
    if (!rel) continue;
    const tool = text(entry.tool) || "custom";
    const root = text(entry.root);
    const name = rel.split("/")[0];
    const key = aiNodeKey(tool, root, name);
    let target = targets.get(key);
    if (!target) {
      target = { key, tool, root, rel_path: name, isDir: false, items: [] };
      targets.set(key, target);
      order.push(key);
    }
    // A name is a folder when the inventory lists the folder itself, or when
    // something is listed inside it.
    if (rel.includes("/")) target.isDir = true;
    if (entry.is_dir) {
      target.isDir = true;
      continue;
    }
    if (qualifies(entry)) target.items.push(entry);
  }
  return order
    .map(key => targets.get(key)!)
    .filter(target => target.items.length > 0);
}
