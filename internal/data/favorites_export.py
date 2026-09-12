"""Favorites export rendering shared by the legacy web API and the native sidecar.

The renderer lives here rather than in either caller so a favourite exported
from the Companion and one exported from the desktop app are byte-identical.
"""

import re
from datetime import datetime


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
