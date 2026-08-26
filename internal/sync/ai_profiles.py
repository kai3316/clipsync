"""Canonical AI-tool config profiles — single source of truth for the AI-config
sync feature (refactor round 1).

Each profile describes one AI coding tool's config files and skills /
commands directories as watch *entries*.  The profile structure is identical
on every device (it ships in the binary), so a peer's inventory entries carry
their ``tool`` key and the receiving side resolves a landing target through
the SAME profile — root indices never cross the wire and the old
``ambiguous_root`` / ``no_local_root`` mapping errors disappear.

An entry is either a single config FILE (e.g. ``~/.claude/CLAUDE.md`` — that
exact file, keeping credentials like ``auth.json`` out) or a DIRECTORY that is
recursively walked (``~/.claude/skills``).

Besides the built-in tools there is always a pseudo-tool ``"custom"`` holding
user-added watch paths; its roots are whatever the user typed, so their file/
dir kind is inferred at collection time.
"""

from __future__ import annotations

from typing import Literal

# Entry kinds: "file" = exactly that file; "dir" = recursive directory walk.
EntryKind = Literal["file", "dir"]

# Pseudo-tool key for user-added watch paths (never a built-in profile).
CUSTOM_KEY = "custom"

# Built-in tool profiles, in display order.  ``entries`` paths are plain
# user-style paths ("~" expands against the real home at collection time),
# stored relative so the same table is meaningful on every OS.
TOOLS: list[dict] = [
    {
        "key": "claude_code",
        "label": "Claude Code",
        "entries": [
            {"path": "~/.claude/CLAUDE.md", "kind": "file"},
            {"path": "~/.claude/settings.json", "kind": "file"},
            {"path": "~/.claude/skills", "kind": "dir"},
        ],
    },
    {
        "key": "codex",
        "label": "Codex",
        "entries": [
            {"path": "~/.codex/config.toml", "kind": "file"},
        ],
    },
    {
        "key": "cursor",
        "label": "Cursor",
        "entries": [
            {"path": "~/.cursor/rules", "kind": "dir"},
            {"path": "~/.cursor/commands", "kind": "dir"},
        ],
    },
    {
        "key": "gemini",
        "label": "Gemini CLI",
        "entries": [
            {"path": "~/.gemini/settings.json", "kind": "file"},
            {"path": "~/.gemini/GEMINI.md", "kind": "file"},
        ],
    },
]

# Enabled-by-default tool keys (the four presets, in display order).
DEFAULT_TOOL_KEYS: list[str] = [t["key"] for t in TOOLS]

# Max built-in tools / user paths accepted — sanity bounds mirroring the old
# MAX_ROOTS cap (custom paths plus every built-in entry stay far below).
MAX_TOOL_KEYS = len(TOOLS) + 8
MAX_CUSTOM_PATHS = 50


def tool_by_key(key: str) -> dict | None:
    """Return the built-in profile dict for *key*, or None."""
    if not isinstance(key, str):
        return None
    for tool in TOOLS:
        if tool["key"] == key:
            return tool
    return None


def validate_tool_keys(keys) -> list[str]:
    """Dedupe + bound a list of tool keys against the known profiles."""
    if not isinstance(keys, list):
        return []
    seen: list[str] = []
    for raw in keys:
        if not isinstance(raw, str):
            continue
        k = raw.strip()
        if k and tool_by_key(k) is not None and k not in seen:
            seen.append(k)
    return seen[:MAX_TOOL_KEYS]


def validate_custom_paths(paths) -> list[str]:
    """Dedupe + bound a list of user custom watch paths."""
    if not isinstance(paths, list):
        return []
    cleaned: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if s and s not in cleaned:
            cleaned.append(s)
    return cleaned[:MAX_CUSTOM_PATHS]


def effective_roots(tool_keys, custom_paths):
    """Flatten enabled tool profiles + custom paths into watch roots.

    Returns a list of ``(tool, kind, path)`` tuples in deterministic order —
    one per built-in profile entry, then the custom paths (kind inferred by
    the caller via ``infer_kind`` when the path exists; ``"dir"`` here means
    "treat as directory root", which the collector falls back to for a custom
    path that does not exist yet).  Used to build the effective watch list
    that ``collect_roots`` walks and to resolve landing targets.
    """
    roots: list[tuple[str, EntryKind, str]] = []
    for key in validate_tool_keys(tool_keys):
        tool = tool_by_key(key)
        if tool is None:
            continue
        for entry in tool["entries"]:
            roots.append((key, entry["kind"], entry["path"]))
    for path in validate_custom_paths(custom_paths):
        roots.append((CUSTOM_KEY, "dir", path))
    return roots


def all_tool_entry_paths() -> set[str]:
    """Every built-in profile entry path, normalized — used to classify old
    ``ai_config_paths`` during migration so each path either enables a tool or
    becomes a custom path."""
    out: set[str] = set()
    for tool in TOOLS:
        for entry in tool["entries"]:
            out.add(_norm_path(entry["path"]))
    return out


def _norm_path(path: str) -> str:
    """Normalize a user-style path for equality comparison (migration only)."""
    return path.strip().replace("\\", "/").rstrip("/")


def migrate_watch_paths(old_paths) -> tuple[list[str], list[str]]:
    """Classify a legacy ``ai_config_paths`` list into (tool_keys, custom_paths).

    A tool is enabled when ANY of its profile entry paths appears in the old
    list (the user clearly wants that tool synced); the profile expansion then
    adds any entries the old list was missing.  Paths that match no built-in
    profile entry become custom paths.  Unknown/blank entries are dropped.
    """
    if not isinstance(old_paths, list):
        return list(DEFAULT_TOOL_KEYS), []
    normalized = [p.strip() for p in old_paths if isinstance(p, str) and p.strip()]
    known = all_tool_entry_paths()
    enabled: list[str] = []
    leftover: list[str] = []
    for tool in TOOLS:
        entry_paths = {_norm_path(e["path"]) for e in tool["entries"]}
        if any(_norm_path(p) in entry_paths for p in normalized):
            enabled.append(tool["key"])
    matched: set[str] = set()
    for key in enabled:
        tool = tool_by_key(key)
        for entry in tool["entries"]:
            matched.add(_norm_path(entry["path"]))
    for p in normalized:
        if _norm_path(p) in matched or _norm_path(p) in known:
            continue
        leftover.append(p)
    return enabled or list(DEFAULT_TOOL_KEYS), validate_custom_paths(leftover)
