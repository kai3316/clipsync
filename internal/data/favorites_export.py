"""Favorites export rendering shared by the legacy web API and the native sidecar.

The renderer lives here rather than in either caller so a favourite exported
from the Companion and one exported from the desktop app are byte-identical.
"""

import json
import re
from datetime import datetime

#: The version of the JSON document this module writes and the importer reads.
#: A file that says anything else is refused rather than guessed at: filling a
#: library from a shape nobody checked is how imports corrupt things.
FAVORITES_JSON_VERSION = 1


def build_favorites_export(favorites: list, fmt: str) -> str:
    """Render complete stored content with fences longer than content backticks."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    if fmt == "markdown":
        fence_len = max(
            [3]
            + [
                len(run) + 1
                for fav in favorites
                for run in re.findall(r"`+", fav.get("content", "") or "")
            ]
        )
        fence = "`" * fence_len
        lines.extend(["# ClipSync Favorites", "", f"_Exported {stamp} · {len(favorites)} items_"])
        current_group = object()
        for fav in favorites:
            group = fav.get("group") or "Ungrouped"
            if group != current_group:
                lines.extend(["", f"## {group}"])
                current_group = group
            lines.extend(["", f"**{fav.get('title') or '(untitled)'}**"])
            content = fav.get("content", "") or ""
            if content:
                lines.extend(["", fence, content, fence])
    else:
        lines.extend([f"ClipSync Favorites — exported {stamp} ({len(favorites)} items)", "=" * 48])
        for fav in favorites:
            group = fav.get("group") or "(no group)"
            lines.extend(["", f"[{group}] {fav.get('title') or '(untitled)'}"])
            content = fav.get("content", "") or ""
            if content:
                lines.append(content)
            lines.append("-" * 48)
    return "\n".join(lines) + "\n"


def build_favorites_json(favorites: list) -> str:
    """Render the library as a document this project can read back.

    Markdown and text are for reading and for handing to something else; this
    one exists so a library can leave one machine and land on another, which is
    why it carries the fields the importer needs — the full body, the group, and
    the order — and nothing about ids or positions.  An id is a fact about the
    library it was minted in and a position is a fact about the rows around it;
    both are the importing library's to decide.

    ``kind`` and ``version`` are what make "this is our file" a check rather
    than a hope.  The document is not pretty-printed into submission: two-space
    indentation keeps a hand-edited file readable without tripling the size of
    a 64 KiB favourite.
    """
    document = {
        "kind": "clipsync-favorites",
        "version": FAVORITES_JSON_VERSION,
        "exported": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(favorites),
        "favorites": [
            {
                "title": favorite.get("title") or "",
                "content": favorite.get("content") or "",
                "group": favorite.get("group") or "",
            }
            for favorite in favorites
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
